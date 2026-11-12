"""
MyraHub controls for the EventBot.

Adds slash commands that let people in a voice channel vote to skip or go back
in the mpv playlist that MyraHub is streaming, plus a simple /suggest command
that collects YouTube links for later download.

Communication with the Windows machine running mpv happens by posting a
control message in a dedicated channel that the Windows controller script
watches.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("myrahub")

# ---------------------------------------------------------------------------
# Configuration (can be overridden via env)
# ---------------------------------------------------------------------------

# Channel where the bot posts control signals for the Windows script.
# Set MYRAHUB_CONTROL_CHANNEL_ID in the environment.
CONTROL_CHANNEL_ID = int(os.environ.get("MYRAHUB_CONTROL_CHANNEL_ID", "0")) or None

# Voice channel that must be occupied for votes to count.
# If unset, any voice channel the invoker is in is used.
VOICE_CHANNEL_ID = int(os.environ.get("MYRAHUB_VOICE_CHANNEL_ID", "0")) or None

# How long a vote stays open (seconds).
VOTE_SECONDS = int(os.environ.get("MYRAHUB_VOTE_SECONDS", "60"))

# File that stores suggested YouTube links (one per line).
SUGGESTIONS_FILE = Path(os.environ.get("MYRAHUB_SUGGESTIONS_FILE", "myrahub_suggestions.txt"))

# ---------------------------------------------------------------------------
# In-memory vote state
# ---------------------------------------------------------------------------

class ActiveVote:
    def __init__(self, action: str, message: discord.Message, voice_members: set[int]):
        self.action = action          # "skip" or "back"
        self.message = message
        self.voice_members = voice_members
        self.yes: set[int] = set()
        self.no: set[int] = set()
        self.ended = False

active_vote: ActiveVote | None = None


def get_voice_members(guild: discord.Guild) -> set[int]:
    """Return the set of non-bot user IDs currently in the target voice channel."""
    if VOICE_CHANNEL_ID:
        channel = guild.get_channel(VOICE_CHANNEL_ID)
    else:
        # Fall back to the invoker's voice channel (caller must check)
        return set()
    if not channel or not isinstance(channel, discord.VoiceChannel):
        return set()
    return {m.id for m in channel.members if not m.bot}


async def start_vote(interaction: discord.Interaction, action: str):
    global active_vote
    if active_vote and not active_vote.ended:
        await interaction.response.send_message(
            "A vote is already running. Wait for it to finish.", ephemeral=True
        )
        return

    if not interaction.guild:
        await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
        return

    # Determine voice members
    if VOICE_CHANNEL_ID:
        voice_members = get_voice_members(interaction.guild)
    else:
        # Use the invoker's current voice channel
        voice = interaction.user.voice
        if not voice or not voice.channel:
            await interaction.response.send_message(
                "You must be in a voice channel to start a vote.", ephemeral=True
            )
            return
        voice_members = {m.id for m in voice.channel.members if not m.bot}

    if len(voice_members) < 1:
        await interaction.response.send_message(
            "No one is in the voice channel to vote.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"Vote to {action.upper()} the video",
        description=(
            f"React with ✅ to {action} or ❌ to keep the current video.\n"
            f"Only people currently in the voice channel can vote.\n"
            f"Voting ends in {VOTE_SECONDS} seconds. Majority of voice members must vote yes."
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"Started by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

    active_vote = ActiveVote(action, msg, voice_members)
    log.info("Started %s vote (message %s, %d eligible)", action, msg.id, len(voice_members))

    # Schedule the end of the vote
    bot = interaction.client
    bot.loop.create_task(_end_vote_after(bot, VOTE_SECONDS))


async def _end_vote_after(bot: commands.Bot, seconds: int):
    await asyncio.sleep(seconds)
    global active_vote
    if not active_vote or active_vote.ended:
        return
    active_vote.ended = True
    vote = active_vote
    active_vote = None

    yes = len(vote.yes)
    no = len(vote.no)
    total_eligible = len(vote.voice_members)
    # Majority of people currently in the voice channel must vote yes
    majority = yes > (total_eligible / 2)

    result_text = (
        f"Vote finished: **{yes}** yes, **{no}** no "
        f"({total_eligible} people were in voice).\n"
    )
    if majority:
        result_text += f"**{vote.action.upper()}** approved!"
        await _send_control_signal(bot, vote.action)
    else:
        result_text += f"**{vote.action.upper()}** rejected."

    try:
        await vote.message.edit(embed=discord.Embed(
            title=f"Vote to {vote.action.upper()} — finished",
            description=result_text,
            color=discord.Color.green() if majority else discord.Color.red(),
        ))
        await vote.message.clear_reactions()
    except discord.DiscordException:
        pass
    log.info("Vote %s ended: yes=%d no=%d majority=%s", vote.action, yes, no, majority)


async def _send_control_signal(bot: commands.Bot, action: str):
    """Post a control message that the Windows script watches for."""
    if not CONTROL_CHANNEL_ID:
        log.warning("No MYRAHUB_CONTROL_CHANNEL_ID set; cannot signal Windows controller")
        return
    channel = bot.get_channel(CONTROL_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CONTROL_CHANNEL_ID)
        except discord.DiscordException:
            log.error("Could not find control channel %s", CONTROL_CHANNEL_ID)
            return
    payload = json.dumps({"action": action, "ts": datetime.utcnow().isoformat()})
    try:
        await channel.send(f"MYRAHUB_CONTROL {payload}")
        log.info("Sent control signal: %s", action)
    except discord.DiscordException as e:
        log.error("Failed to send control signal: %s", e)


# ---------------------------------------------------------------------------
# Reaction listener
# ---------------------------------------------------------------------------

async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    global active_vote
    if not active_vote or active_vote.ended:
        return
    if reaction.message.id != active_vote.message.id:
        return
    if user.bot:
        return
    if user.id not in active_vote.voice_members:
        # Not in the voice channel — ignore
        try:
            await reaction.remove(user)
        except discord.DiscordException:
            pass
        return

    emoji = str(reaction.emoji)
    if emoji == "✅":
        active_vote.yes.add(user.id)
        active_vote.no.discard(user.id)
    elif emoji == "❌":
        active_vote.no.add(user.id)
        active_vote.yes.discard(user.id)


async def on_reaction_remove(reaction: discord.Reaction, user: discord.User):
    global active_vote
    if not active_vote or active_vote.ended:
        return
    if reaction.message.id != active_vote.message.id or user.bot:
        return
    emoji = str(reaction.emoji)
    if emoji == "✅":
        active_vote.yes.discard(user.id)
    elif emoji == "❌":
        active_vote.no.discard(user.id)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

def setup_commands(tree: app_commands.CommandTree):
    @tree.command(name="skip", description="Vote to skip to the next video")
    async def skip(interaction: discord.Interaction):
        await start_vote(interaction, "skip")

    @tree.command(name="back", description="Vote to go back to the previous video")
    async def back(interaction: discord.Interaction):
        await start_vote(interaction, "back")

    @tree.command(name="suggest", description="Suggest a YouTube video to add later")
    @app_commands.describe(link="YouTube URL")
    async def suggest(interaction: discord.Interaction, link: str):
        link = link.strip()
        if not (link.startswith("http://") or link.startswith("https://")) or "youtu" not in link.lower():
            await interaction.response.send_message(
                "Please provide a valid YouTube link.", ephemeral=True
            )
            return
        try:
            SUGGESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with SUGGESTIONS_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()} | {interaction.user} | {link}\n")
        except OSError as e:
            log.error("Failed to write suggestion: %s", e)
            await interaction.response.send_message("Could not save the suggestion.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Added to the suggestion list: {link}", ephemeral=True
        )
        log.info("Suggestion from %s: %s", interaction.user, link)
