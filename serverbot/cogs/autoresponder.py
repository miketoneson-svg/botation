import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils import make_embed

NO_MENTIONS = discord.AllowedMentions.none()


@app_commands.guild_only()
class AutoResponder(commands.GroupCog, group_name="autoresponder", group_description="Automatic replies to trigger words"):
    def __init__(self, bot):
        self.bot = bot
        self._triggers: dict[int, list] = {}
        self._settings: dict[int, dict] = {}
        self._cooldowns: dict[tuple[int, int], float] = {}
        super().__init__()

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS autoresponders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, trigger TEXT, response TEXT,
                staff_only INTEGER DEFAULT 0, exact INTEGER DEFAULT 0
            )"""
        )

    # ---------- cached data ----------
    async def triggers(self, guild_id: int) -> list:
        if guild_id not in self._triggers:
            rows = await self.bot.db.fetchall("SELECT * FROM autoresponders WHERE guild_id=? ORDER BY id", (guild_id,))
            self._triggers[guild_id] = [
                (r["id"], r["trigger"], r["response"], bool(r["staff_only"]), bool(r["exact"]),
                 re.compile(rf"(?<!\w){re.escape(r['trigger'])}(?!\w)"))
                for r in rows
            ]
        return self._triggers[guild_id]

    async def settings(self, guild_id: int) -> dict:
        if guild_id not in self._settings:
            get = self.bot.db.get_config
            role, channel = await get(guild_id, "autoresponder_staff_role"), await get(guild_id, "autoresponder_staff_channel")
            self._settings[guild_id] = {
                "enabled": await get(guild_id, "autoresponder_enabled", "1") == "1",
                "role": int(role) if role else None,
                "channel": int(channel) if channel else None,
            }
        return self._settings[guild_id]

    def _invalidate(self, guild_id: int):
        self._triggers.pop(guild_id, None)
        self._settings.pop(guild_id, None)

    # ---------- commands ----------
    @app_commands.command(description="Add a new auto-response trigger")
    @app_commands.describe(trigger="Word or phrase to look for", response="What to reply (use \\n for a new line)",
                           staff_only="Only staff can trigger it, and the reply goes to the staff channel",
                           exact="The whole message must match (default: message just contains it)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(self, interaction: discord.Interaction, trigger: app_commands.Range[str, 1, 100], response: app_commands.Range[str, 1, 1500],
                  staff_only: bool = False, exact: bool = False):
        trigger = trigger.strip().lower()
        gid = interaction.guild.id
        await self.bot.db.execute("DELETE FROM autoresponders WHERE guild_id=? AND trigger=?", (gid, trigger))
        rid = await self.bot.db.execute(
            "INSERT INTO autoresponders(guild_id, trigger, response, staff_only, exact) VALUES(?,?,?,?,?)",
            (gid, trigger, response, int(staff_only), int(exact)),
        )
        self._invalidate(gid)
        await interaction.response.send_message(f"Added auto-response **#{rid}** for `{trigger}`.", ephemeral=True)

    @app_commands.command(description="Remove an auto-response trigger")
    @app_commands.describe(trigger="The trigger text or its # number from /autoresponder list")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, trigger: str):
        gid, text = interaction.guild.id, trigger.strip().lower().lstrip("#")
        row = await self.bot.db.fetchone(
            "SELECT id FROM autoresponders WHERE guild_id=? AND (trigger=? OR id=?)", (gid, text, int(text) if text.isdigit() else -1)
        )
        if not row:
            return await interaction.response.send_message("I couldn't find that trigger. Check `/autoresponder list`.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM autoresponders WHERE id=?", (row["id"],))
        self._invalidate(gid)
        await interaction.response.send_message(f"Removed auto-response **#{row['id']}**.", ephemeral=True)

    @app_commands.command(description="View all configured auto-response triggers")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list(self, interaction: discord.Interaction):
        rows = await self.triggers(interaction.guild.id)
        if not rows:
            return await interaction.response.send_message("No triggers yet. Add one with `/autoresponder add`.", ephemeral=True)
        s = await self.settings(interaction.guild.id)
        lines = []
        for tid, trigger, response, staff_only, exact, _ in rows:
            tags = ("staff-only " if staff_only else "") + ("exact" if exact else "contains")
            preview = response if len(response) <= 80 else response[:77] + "..."
            lines.append(f"**#{tid}** `{trigger}` ({tags})\n> {preview}")
        embed = make_embed(f"Auto-responses ({'ON' if s['enabled'] else 'OFF'})", "\n".join(lines)[:4000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Set which role counts as 'staff' for staff-only triggers")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def role(self, interaction: discord.Interaction, role: discord.Role):
        await self.bot.db.set_config(interaction.guild.id, "autoresponder_staff_role", role.id)
        self._invalidate(interaction.guild.id)
        await interaction.response.send_message(f"Staff-only triggers now work for {role.mention} (and admins).", ephemeral=True)

    @app_commands.command(description="Set the channel where staff-only auto-responses get sent")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def staffchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "autoresponder_staff_channel", channel.id)
        self._invalidate(interaction.guild.id)
        await interaction.response.send_message(f"Staff-only replies will be sent to {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Enable or disable the auto-responder entirely")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggle(self, interaction: discord.Interaction):
        s = await self.settings(interaction.guild.id)
        new = not s["enabled"]
        await self.bot.db.set_config(interaction.guild.id, "autoresponder_enabled", "1" if new else "0")
        self._invalidate(interaction.guild.id)
        await interaction.response.send_message(f"Auto-responder is now **{'ON' if new else 'OFF'}**.", ephemeral=True)

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        gid = message.guild.id
        s = await self.settings(gid)
        if not s["enabled"]:
            return
        triggers = await self.triggers(gid)
        if not triggers:
            return
        member = message.author
        is_staff = isinstance(member, discord.Member) and (
            member.guild_permissions.administrator or (s["role"] and any(r.id == s["role"] for r in member.roles))
        )
        content = message.content.lower().strip()
        for tid, trigger, response, staff_only, exact, pattern in triggers:
            if staff_only and not is_staff:
                continue
            if not (content == trigger if exact else pattern.search(content)):
                continue
            now = time.monotonic()
            if now - self._cooldowns.get((message.channel.id, tid), 0) < 5:
                return
            self._cooldowns[(message.channel.id, tid)] = now
            target = message.channel
            if staff_only and s["channel"]:
                target = message.guild.get_channel(s["channel"]) or message.channel
            try:
                await target.send(response.replace("\\n", "\n"), allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass
            return


async def setup(bot):
    await bot.add_cog(AutoResponder(bot))
