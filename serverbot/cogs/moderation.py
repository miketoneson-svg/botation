import asyncio
import json
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils import format_duration, hierarchy_error, make_embed, parse_duration, send_log

NO_REASON = "No reason provided"


@app_commands.guild_only()
class Moderation(commands.GroupCog, group_name="mod", group_description="Moderation commands"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS mod_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, user_id INTEGER, moderator_id INTEGER,
                action TEXT, reason TEXT, created_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, user_id INTEGER, moderator_id INTEGER,
                reason TEXT, created_at TEXT
            )"""
        )

    # ---------- helpers ----------
    async def _record(self, interaction, user, action: str, reason: str, extra: str = ""):
        now = discord.utils.utcnow()
        await self.bot.db.execute(
            "INSERT INTO mod_actions(guild_id, user_id, moderator_id, action, reason, created_at) VALUES(?,?,?,?,?,?)",
            (interaction.guild.id, user.id, interaction.user.id, action, reason, now.isoformat()),
        )
        embed = make_embed(action.title(), color=discord.Color.orange(), timestamp=now)
        embed.add_field(name="User", value=f"{user} ({user.id})")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        if extra:
            embed.add_field(name="Details", value=extra, inline=False)
        await send_log(self.bot, interaction.guild, embed)

    async def _dm(self, user, guild, verb: str, reason: str, extra: str = ""):
        try:
            await user.send(embed=make_embed(f"You were {verb} in {guild.name}", f"**Reason:** {reason}{extra}", discord.Color.red()))
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _deny(self, interaction, msg: str):
        await interaction.response.send_message(msg, ephemeral=True)

    # ---------- bans / kicks ----------
    @app_commands.command(description="Ban a member from the server")
    @app_commands.describe(user="Who to ban", reason="Why", delete_days="Days of their messages to delete (0-7)")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: str = NO_REASON,
                  delete_days: app_commands.Range[int, 0, 7] = 0):
        if isinstance(user, discord.Member):
            if err := hierarchy_error(interaction, user):
                return await self._deny(interaction, err)
            await self._dm(user, interaction.guild, "banned", reason)
        await interaction.guild.ban(user, reason=f"{interaction.user}: {reason}", delete_message_seconds=delete_days * 86400)
        await self._record(interaction, user, "ban", reason)
        await interaction.response.send_message(embed=make_embed("Member banned", f"{user.mention} was banned.\n**Reason:** {reason}", discord.Color.red()))

    @app_commands.command(description="Ban then immediately unban a member (wipes their messages)")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def softban(self, interaction: discord.Interaction, member: discord.Member, reason: str = NO_REASON):
        if err := hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        await self._dm(member, interaction.guild, "softbanned (you can rejoin)", reason)
        await interaction.guild.ban(member, reason=f"Softban by {interaction.user}: {reason}", delete_message_seconds=7 * 86400)
        await interaction.guild.unban(member, reason="Softban")
        await self._record(interaction, member, "softban", reason)
        await interaction.response.send_message(embed=make_embed("Member softbanned", f"{member.mention} was softbanned.\n**Reason:** {reason}", discord.Color.red()))

    @app_commands.command(description="Unban a user by their ID")
    @app_commands.describe(user_id="The user's ID")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = NO_REASON):
        if not user_id.strip().isdigit():
            return await self._deny(interaction, "That isn't a valid user ID.")
        user = discord.Object(int(user_id))
        try:
            await interaction.guild.unban(user, reason=f"{interaction.user}: {reason}")
        except discord.NotFound:
            return await self._deny(interaction, "That user isn't banned.")
        await self._record(interaction, user, "unban", reason)
        await interaction.response.send_message(embed=make_embed("User unbanned", f"<@{user_id}> was unbanned.", discord.Color.green()))

    @app_commands.command(description="Kick a member from the server")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = NO_REASON):
        if err := hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        await self._dm(member, interaction.guild, "kicked", reason)
        await interaction.guild.kick(member, reason=f"{interaction.user}: {reason}")
        await self._record(interaction, member, "kick", reason)
        await interaction.response.send_message(embed=make_embed("Member kicked", f"{member.mention} was kicked.\n**Reason:** {reason}", discord.Color.red()))

    # ---------- timeouts ----------
    @app_commands.command(description="Timeout (mute) a member")
    @app_commands.describe(duration="e.g. 10m, 2h, 1d (max 28d)")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = NO_REASON):
        delta = parse_duration(duration)
        if not delta or delta > timedelta(days=28):
            return await self._deny(interaction, "Use a duration like `10m`, `2h` or `1d` (max 28 days).")
        if err := hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        await member.timeout(delta, reason=f"{interaction.user}: {reason}")
        await self._dm(member, interaction.guild, "timed out", reason, f"\n**Duration:** {format_duration(delta)}")
        await self._record(interaction, member, "timeout", reason, f"Duration: {format_duration(delta)}")
        await interaction.response.send_message(embed=make_embed("Member timed out", f"{member.mention} for **{format_duration(delta)}**.\n**Reason:** {reason}", discord.Color.orange()))

    @app_commands.command(description="Remove a timeout from a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = NO_REASON):
        await member.timeout(None, reason=f"{interaction.user}: {reason}")
        await self._record(interaction, member, "untimeout", reason)
        await interaction.response.send_message(embed=make_embed("Timeout removed", f"{member.mention} can talk again.", discord.Color.green()))

    # ---------- warnings ----------
    @app_commands.command(description="Warn a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if err := hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        warn_id = await self.bot.db.execute(
            "INSERT INTO warnings(guild_id, user_id, moderator_id, reason, created_at) VALUES(?,?,?,?,?)",
            (interaction.guild.id, member.id, interaction.user.id, reason, discord.utils.utcnow().isoformat()),
        )
        await self._dm(member, interaction.guild, "warned", reason)
        await self._record(interaction, member, "warn", reason, f"Warning #{warn_id}")
        await interaction.response.send_message(embed=make_embed("Member warned", f"{member.mention} (warning **#{warn_id}**)\n**Reason:** {reason}", discord.Color.orange()))

    @app_commands.command(description="View warnings for a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 20",
            (interaction.guild.id, member.id),
        )
        if not rows:
            return await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        lines = [f"**#{r['id']}** · <@{r['moderator_id']}> · {discord.utils.format_dt(discord.utils.parse_time(r['created_at']), 'R')}\n{r['reason']}" for r in rows]
        await interaction.response.send_message(embed=make_embed(f"Warnings for {member}", "\n\n".join(lines)[:4000], discord.Color.orange()), ephemeral=True)

    @app_commands.command(description="Delete a single warning by its number")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def delwarn(self, interaction: discord.Interaction, member: discord.Member, number: int):
        row = await self.bot.db.fetchone(
            "SELECT id FROM warnings WHERE id=? AND guild_id=? AND user_id=?", (number, interaction.guild.id, member.id)
        )
        if not row:
            return await self._deny(interaction, f"{member.mention} has no warning #{number}.")
        await self.bot.db.execute("DELETE FROM warnings WHERE id=?", (number,))
        await interaction.response.send_message(f"Deleted warning **#{number}** for {member.mention}.", ephemeral=True)

    @app_commands.command(description="Clear all warnings for a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await interaction.response.send_message(f"Cleared all warnings for {member.mention}.", ephemeral=True)

    @app_commands.command(description="View a member's full moderation history (warnings)")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def modlogs(self, interaction: discord.Interaction, user: discord.User):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM mod_actions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
            (interaction.guild.id, user.id),
        )
        if not rows:
            return await interaction.response.send_message(f"No moderation history for {user.mention}.", ephemeral=True)
        lines = [f"**{r['action'].title()}** · <@{r['moderator_id']}> · {discord.utils.format_dt(discord.utils.parse_time(r['created_at']), 'R')}\n{r['reason']}" for r in rows]
        await interaction.response.send_message(embed=make_embed(f"Mod history for {user} (latest 15)", "\n\n".join(lines)[:4000]), ephemeral=True)

    # ---------- channels ----------
    @app_commands.command(description="Lock a channel so members can't send messages")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None, reason: str = NO_REASON):
        channel = channel or interaction.channel
        await channel.set_permissions(interaction.guild.default_role, send_messages=False, reason=f"{interaction.user}: {reason}")
        await interaction.response.send_message(embed=make_embed("Channel locked", f"{channel.mention} is locked.\n**Reason:** {reason}", discord.Color.red()))

    @app_commands.command(description="Unlock a channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        await channel.set_permissions(interaction.guild.default_role, send_messages=None, reason=f"Unlocked by {interaction.user}")
        await interaction.response.send_message(embed=make_embed("Channel unlocked", f"{channel.mention} is unlocked.", discord.Color.green()))

    @app_commands.command(description="Lock ALL channels in the server")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lockdown(self, interaction: discord.Interaction):
        await interaction.response.defer()
        everyone = interaction.guild.default_role
        raw = await self.bot.db.get_config(interaction.guild.id, "_lockdown_channels", "[]")
        locked = set(json.loads(raw))
        count = 0
        for ch in interaction.guild.text_channels:
            overwrite = ch.overwrites_for(everyone)
            if overwrite.send_messages is False:
                continue  # already locked by someone, so we leave it alone on unlock too
            overwrite.send_messages = False
            try:
                await ch.set_permissions(everyone, overwrite=overwrite, reason=f"Lockdown by {interaction.user}")
            except discord.HTTPException:
                continue
            locked.add(ch.id)
            count += 1
        await self.bot.db.set_config(interaction.guild.id, "_lockdown_channels", json.dumps(sorted(locked)))
        await interaction.followup.send(embed=make_embed("Server locked down", f"Locked **{count}** channels. Use `/mod unlockdown` to reverse it.", discord.Color.red()))

    @app_commands.command(description="Unlock ALL channels in the server")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlockdown(self, interaction: discord.Interaction):
        await interaction.response.defer()
        everyone = interaction.guild.default_role
        raw = await self.bot.db.get_config(interaction.guild.id, "_lockdown_channels", "[]")
        count = 0
        for cid in json.loads(raw):
            ch = interaction.guild.get_channel(cid)
            if not ch:
                continue
            try:
                await ch.set_permissions(everyone, send_messages=None, reason=f"Lockdown lifted by {interaction.user}")
                count += 1
            except discord.HTTPException:
                continue
        await self.bot.db.set_config(interaction.guild.id, "_lockdown_channels", "[]")
        await interaction.followup.send(embed=make_embed("Lockdown lifted", f"Unlocked **{count}** channels.", discord.Color.green()))

    @app_commands.command(description="Set slowmode for a channel")
    @app_commands.describe(seconds="0 turns it off (max 21600)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600], channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
        text = "Slowmode turned off" if seconds == 0 else f"Slowmode set to {seconds}s"
        await interaction.response.send_message(f"{text} in {channel.mention}.")

    @app_commands.command(description="Delete a number of messages")
    @app_commands.describe(amount="How many messages to scan (max 500)", member="Only delete this member's messages")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500], member: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount, check=lambda m: member is None or m.author.id == member.id)
        await interaction.followup.send(f"Deleted **{len(deleted)}** messages.", ephemeral=True)

    # ---------- members / roles ----------
    @app_commands.command(description="Change a member's nickname")
    @app_commands.describe(nickname="Leave empty to reset")
    @app_commands.checks.has_permissions(manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    async def nick(self, interaction: discord.Interaction, member: discord.Member, nickname: app_commands.Range[str, 1, 32] | None = None):
        if member.id != interaction.user.id:
            if err := hierarchy_error(interaction, member):
                return await self._deny(interaction, err)
        await member.edit(nick=nickname, reason=f"Nick change by {interaction.user}")
        await interaction.response.send_message(f"Nickname for {member.mention} {'set to **' + nickname + '**' if nickname else 'reset'}.")

    def _role_error(self, interaction, role: discord.Role) -> str | None:
        if role.is_default() or role.managed:
            return "I can't hand out that role (it's @everyone or managed by an integration)."
        if role >= interaction.guild.me.top_role:
            return "That role is equal to or higher than my top role. Move my role up."
        if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
            return "That role is equal to or higher than your top role."
        return None

    @app_commands.command(description="Add a role to a member")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def roleadd(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if err := self._role_error(interaction, role):
            return await self._deny(interaction, err)
        await member.add_roles(role, reason=f"By {interaction.user}")
        await interaction.response.send_message(f"Added {role.mention} to {member.mention}.", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Remove a role from a member")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def roleremove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if err := self._role_error(interaction, role):
            return await self._deny(interaction, err)
        await member.remove_roles(role, reason=f"By {interaction.user}")
        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}.", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Give the Active Member role to all online members")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def giveroles(self, interaction: discord.Interaction):
        role = discord.utils.get(interaction.guild.roles, name="Active Member")
        if not role:
            return await self._deny(interaction, "Create a role named **Active Member** first.")
        if err := self._role_error(interaction, role):
            return await self._deny(interaction, err)
        await interaction.response.defer()
        given = 0
        for m in interaction.guild.members:
            if m.bot or role in m.roles or m.status == discord.Status.offline:
                continue
            try:
                await m.add_roles(role, reason=f"giveroles by {interaction.user}")
                given += 1
                await asyncio.sleep(0.4)
            except discord.HTTPException:
                continue
        await interaction.followup.send(f"Gave {role.mention} to **{given}** online members.", allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Moderation(bot))
