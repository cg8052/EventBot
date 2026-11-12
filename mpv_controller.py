"""
mpv_controller.py

Runs on the Windows machine that is streaming with mpv.
Listens for control messages posted by the EventBot in a Discord channel
and sends playlist-next / playlist-prev commands to mpv via its IPC named pipe.

Usage:
  1. Launch mpv with the IPC server enabled:
       mpv --input-ipc-server=\\.\pipe\mpv-socket  (or use a batch file)
  2. Set environment variables:
       DISCORD_BOT_TOKEN=...   (same token the EventBot uses, or a separate bot)
       MYRAHUB_CONTROL_CHANNEL_ID=123456789
  3. python mpv_controller.py

The script uses discord.py and talks to mpv over a Windows named pipe.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mpv_controller")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
CONTROL_CHANNEL_ID = int(os.environ.get("MYRAHUB_CONTROL_CHANNEL_ID", "0")) or None
MPV_PIPE = os.environ.get("MPV_IPC_PIPE", r"\\.\pipe\mpv-socket")

if not TOKEN or not CONTROL_CHANNEL_ID:
    log.error("Set DISCORD_BOT_TOKEN and MYRAHUB_CONTROL_CHANNEL_ID")
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def send_mpv_command(command: list):
    """Send a JSON command to mpv's IPC pipe."""
    try:
        import win32file  # pywin32
    except ImportError:
        log.error("pywin32 is required on Windows. pip install pywin32")
        return False
    try:
        handle = win32file.CreateFile(
            MPV_PIPE,
            win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
        payload = json.dumps({"command": command}) + "\n"
        win32file.WriteFile(handle, payload.encode())
        win32file.CloseHandle(handle)
        log.info("Sent to mpv: %s", command)
        return True
    except Exception as e:
        log.error("Failed to talk to mpv pipe %s: %s", MPV_PIPE, e)
        return False


@bot.event
async def on_ready():
    log.info("Controller logged in as %s", bot.user)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id != CONTROL_CHANNEL_ID:
        return
    content = message.content.strip()
    if not content.startswith("MYRAHUB_CONTROL "):
        return
    try:
        payload = json.loads(content.split(" ", 1)[1])
        action = payload.get("action")
    except Exception:
        log.warning("Bad control payload: %s", content)
        return

    if action == "skip":
        ok = send_mpv_command(["playlist-next"])
    elif action == "back":
        ok = send_mpv_command(["playlist-prev"])
    else:
        log.warning("Unknown action: %s", action)
        return

    if ok:
        try:
            await message.add_reaction("✅")
        except discord.DiscordException:
            pass
    else:
        try:
            await message.add_reaction("❌")
        except discord.DiscordException:
            pass


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
