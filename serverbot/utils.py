import os
import re
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands

# Where the database and uploaded files live. On Railway, attach a Volume: its mount path is picked up automatically.
DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ".")

DEFAULT_COLOR = discord.Color.blurple()
_DURATION_RE = re.compile(r"(\d+)([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def make_embed(title=None, description=None, color=None, **kwargs) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=color or DEFAULT_COLOR,
        timestamp=kwargs.pop("timestamp", None),
        **kwargs,
    )


def parse_duration(text: str) -> timedelta | None:
    """'10m', '1h30m', '2d' -> timedelta. Returns None if it can't be parsed."""
    text = text.strip().lower().replace(" ", "")
    matches = _DURATION_RE.findall(text)
    if not matches or "".join(f"{n}{u}" for n, u in matches) != text:
        return None
    seconds = sum(int(n) * _UNITS[u] for n, u in matches)
    return timedelta(seconds=seconds) if seconds > 0 else None


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        value, total = divmod(total, size)
        if value:
            parts.append(f"{value}{name}")
    return " ".join(parts) or "0s"


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)

    return app_commands.check(predicate)


def hierarchy_error(interaction: discord.Interaction, target: discord.Member) -> str | None:
    """Returns an error message if the moderator/bot can't act on target, else None."""
    guild = interaction.guild
    if target.id == interaction.user.id:
        return "You can't do that to yourself."
    if target.id == interaction.client.user.id:
        return "I can't do that to myself."
    if target.id == guild.owner_id:
        return "You can't do that to the server owner."
    if interaction.user.id != guild.owner_id and target.top_role >= interaction.user.top_role:
        return "That member's top role is equal to or higher than yours."
    if target.top_role >= guild.me.top_role:
        return "That member's top role is equal to or higher than mine. Move my role up."
    return None


async def send_log(bot, guild: discord.Guild, embed: discord.Embed) -> None:
    """Posts to the channel set with /config log-channel (silently skips if unset)."""
    channel_id = await bot.db.get_config(guild.id, "log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


def fill(text: str, **values) -> str:
    """Replaces {placeholders} safely (unknown ones are left alone, no format() errors)."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def age_text(dt) -> str:
    """'2 years, 3 months' / '12 days' style age for a datetime."""
    from discord.utils import utcnow

    days = max((utcnow() - dt).days, 0)
    years, rest = divmod(days, 365)
    months, days = divmod(rest, 30)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if not parts:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts[:2])


class Confirm(discord.ui.View):
    """Reusable 'are you sure?' prompt. `action(interaction)` runs on confirm (reply with interaction.followup)."""

    def __init__(self, user_id: int, action, confirm_label: str = "Confirm", timeout: float = 60):
        super().__init__(timeout=timeout)
        self.user_id, self.action = user_id, action
        self.confirm_button.label = confirm_label

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
        await interaction.response.edit_message(content="Working on it...", embed=None, view=None)
        await self.action(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
