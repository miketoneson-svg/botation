import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from db import Database
from utils import DATA_DIR

load_dotenv()
# stdout so Railway doesn't flag normal logs as errors
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


class Bot(commands.Bot):
    def __init__(self):
        # Needs Server Members, Message Content and Presence intents enabled in the dev portal
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.all(),
            help_command=None,
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(DATA_DIR / "bot.db"))
        self.start_time = discord.utils.utcnow()
        self.tree.error(self.on_tree_error)

    async def setup_hook(self):
        if os.getenv("RAILWAY_ENVIRONMENT") and not (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR")):
            log.warning("No Railway volume attached: the database will be wiped on every deploy. Add a volume.")
        log.info("Data directory: %s", DATA_DIR.resolve())
        await self.db.connect()
        # Every .py file in cogs/ is loaded automatically (files starting with _ are skipped)
        for path in sorted(Path("cogs").glob("*.py")):
            if path.stem.startswith("_"):
                continue
            await self.load_extension(f"cogs.{path.stem}")
            log.info("Loaded cog %s", path.stem)

        # This bot is for servers only, so hide every command from DMs
        for cmd in self.tree.get_commands():
            cmd.guild_only = True
        await self.sync_commands()

    async def sync_commands(self):
        """Syncs slash commands only when they changed since the last start (avoids Discord rate limits on restarts)."""
        sig = []
        for cmd in self.tree.walk_commands():
            if isinstance(cmd, app_commands.Group):
                continue
            params = [
                (p.name, p.description, p.required, str(p.type), p.min_value, p.max_value, [c.name for c in p.choices])
                for p in cmd.parameters
            ]
            sig.append([cmd.qualified_name, cmd.description, params])
        digest = hashlib.sha256(json.dumps(sorted(sig), default=str).encode()).hexdigest()

        dev_guild = os.getenv("DEV_GUILD_ID")
        key = f"_command_hash_{dev_guild or 'global'}"
        if await self.db.get_config(0, key) == digest:
            log.info("Slash commands unchanged, skipping sync")
            return
        if dev_guild:
            guild = discord.Object(int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(sig), dev_guild)
        else:
            await self.tree.sync()
            log.info("Synced %d commands globally", len(sig))
        await self.db.set_config(0, key, digest)

    async def on_ready(self):
        log.info("Logged in as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))

    async def close(self):
        await self.db.close()
        await super().close()

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use that command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "I'm missing permissions: " + ", ".join(error.missing_permissions)
        elif isinstance(error, app_commands.CheckFailure):
            msg = "You can't use that command."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down, try again in {error.retry_after:.0f}s."
        else:
            log.error("Command error in /%s", getattr(interaction.command, "qualified_name", "?"), exc_info=error)
            msg = "Something went wrong running that command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable (or put it in a .env file).")
    Bot().run(token, log_handler=None)
