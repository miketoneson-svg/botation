import asyncio
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from utils import age_text, make_embed

MEDALS = ["🥇", "🥈", "🥉"]


class InvitePanel(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="My Invites", emoji="📊", style=discord.ButtonStyle.primary, custom_id="invites:stats")
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=await self.cog.stats_embed(interaction.guild, interaction.user), ephemeral=True)

    @discord.ui.button(label="Create Invite", emoji="🔗", style=discord.ButtonStyle.success, custom_id="invites:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.send_personal_invite(interaction)

    @discord.ui.button(label="Leaderboard", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="invites:leaderboard")
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=await self.cog.leaderboard_embed(interaction.guild), ephemeral=True)


class Invites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {code: (uses, inviter_id, max_uses)}
        self.cache: dict[int, dict[str, tuple[int, int | None, int]]] = {}
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self):
        db = self.bot.db
        # One row per member (latest join), so leaving and rejoining can't farm invites
        await db.execute(
            """CREATE TABLE IF NOT EXISTS invite_joins (
                guild_id INTEGER, member_id INTEGER, inviter_id INTEGER, code TEXT,
                joined_at TEXT, left INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, member_id)
            )"""
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS invite_links (guild_id INTEGER, code TEXT, user_id INTEGER, PRIMARY KEY (guild_id, code))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS invite_rewards (guild_id INTEGER, invites INTEGER, role_id INTEGER, PRIMARY KEY (guild_id, invites))"
        )
        self.bot.add_view(InvitePanel(self))

    # ---------- invite cache ----------
    async def snapshot(self, guild: discord.Guild):
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None
        rows = await self.bot.db.fetchall("SELECT code, user_id FROM invite_links WHERE guild_id=?", (guild.id,))
        owners = {r["code"]: r["user_id"] for r in rows}
        return {
            i.code: (i.uses or 0, owners.get(i.code) or (i.inviter.id if i.inviter else None), i.max_uses or 0)
            for i in invites
        }

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            snap = await self.snapshot(guild)
            if snap is not None:
                self.cache[guild.id] = snap

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        snap = await self.snapshot(guild)
        if snap is not None:
            self.cache[guild.id] = snap

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            self.cache.setdefault(invite.guild.id, {})[invite.code] = (
                invite.uses or 0, invite.inviter.id if invite.inviter else None, invite.max_uses or 0
            )

    async def find_inviter(self, guild: discord.Guild):
        old = self.cache.get(guild.id, {})
        new = await self.snapshot(guild)
        if new is None:
            return None, None
        self.cache[guild.id] = new
        used = [c for c, (uses, _, _) in new.items() if uses > old.get(c, (0,))[0]]
        if not used:  # a single-use invite disappears once it's consumed
            used = [c for c, (uses, _, mx) in old.items() if c not in new and mx and uses + 1 >= mx]
        if not used:
            return None, None
        code = used[0]
        inviter = (new.get(code) or old.get(code))[1]
        return inviter, code

    # ---------- join / leave tracking ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        async with self.locks[member.guild.id]:
            inviter_id, code = await self.find_inviter(member.guild)
        if inviter_id == member.id:
            inviter_id = None
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO invite_joins(guild_id, member_id, inviter_id, code, joined_at, left) VALUES(?,?,?,?,?,0)",
            (member.guild.id, member.id, inviter_id, code, discord.utils.utcnow().isoformat()),
        )
        if inviter_id:
            await self.apply_rewards(member.guild, inviter_id)
        self.bot.dispatch("tracked_join", member, inviter_id, code)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        row = await self.bot.db.fetchone(
            "SELECT inviter_id FROM invite_joins WHERE guild_id=? AND member_id=?", (member.guild.id, member.id)
        )
        if not row:
            return
        await self.bot.db.execute("UPDATE invite_joins SET left=1 WHERE guild_id=? AND member_id=?", (member.guild.id, member.id))
        if row["inviter_id"]:
            revoke = await self.bot.db.get_config(member.guild.id, "invite_revoke", "0") == "1"
            await self.apply_rewards(member.guild, row["inviter_id"], revoke=revoke)

    # ---------- stats / rewards ----------
    async def stats(self, guild_id: int, user_id: int):
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS total, COALESCE(SUM(left), 0) AS gone FROM invite_joins WHERE guild_id=? AND inviter_id=?",
            (guild_id, user_id),
        )
        total, gone = row["total"], row["gone"]
        return total, gone, total - gone

    async def apply_rewards(self, guild: discord.Guild, user_id: int, revoke: bool = False):
        member = guild.get_member(user_id)
        if not member:
            return
        _, _, effective = await self.stats(guild.id, user_id)
        rows = await self.bot.db.fetchall("SELECT invites, role_id FROM invite_rewards WHERE guild_id=?", (guild.id,))
        add, remove = [], []
        for r in rows:
            role = guild.get_role(r["role_id"])
            if not role or role >= guild.me.top_role:
                continue
            if effective >= r["invites"] and role not in member.roles:
                add.append(role)
            elif revoke and effective < r["invites"] and role in member.roles:
                remove.append(role)
        try:
            if add:
                await member.add_roles(*add, reason="Invite milestone reward")
            if remove:
                await member.remove_roles(*remove, reason="No longer qualifies for invite reward")
        except discord.HTTPException:
            pass

    async def stats_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        total, gone, effective = await self.stats(guild.id, member.id)
        row = await self.bot.db.fetchone(
            "SELECT inviter_id FROM invite_joins WHERE guild_id=? AND member_id=?", (guild.id, member.id)
        )
        invited_by = f"<@{row['inviter_id']}>" if row and row["inviter_id"] else "Unknown"
        embed = make_embed(f"Invites for {member.display_name}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Effective invites", value=f"**{effective}**")
        embed.add_field(name="Total joins", value=str(total))
        embed.add_field(name="Left", value=str(gone))
        embed.add_field(name="Invited by", value=invited_by)
        embed.add_field(name="On Discord for", value=age_text(member.created_at))
        embed.add_field(name="In this server for", value=age_text(member.joined_at) if member.joined_at else "?")
        nxt = await self.bot.db.fetchone(
            "SELECT invites, role_id FROM invite_rewards WHERE guild_id=? AND invites>? ORDER BY invites LIMIT 1", (guild.id, effective)
        )
        if nxt:
            embed.set_footer(text=f"Next reward at {nxt['invites']} invites ({nxt['invites'] - effective} to go)")
        return embed

    async def leaderboard_embed(self, guild: discord.Guild) -> discord.Embed:
        rows = await self.bot.db.fetchall(
            """SELECT inviter_id, COUNT(*) AS total, COALESCE(SUM(left), 0) AS gone FROM invite_joins
               WHERE guild_id=? AND inviter_id IS NOT NULL GROUP BY inviter_id
               ORDER BY (COUNT(*) - COALESCE(SUM(left), 0)) DESC LIMIT 10""",
            (guild.id,),
        )
        if not rows:
            return make_embed("Invite leaderboard", "No invites tracked yet.")
        lines = []
        for i, r in enumerate(rows):
            medal = MEDALS[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{medal} <@{r['inviter_id']}> · **{r['total'] - r['gone']}** invites ({r['total']} joins, {r['gone']} left)")
        return make_embed("Invite leaderboard", "\n".join(lines), discord.Color.gold())

    async def send_personal_invite(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        await interaction.response.defer(ephemeral=True)
        try:
            live = {i.code for i in await guild.invites()}
        except discord.HTTPException:
            return await interaction.followup.send("I need the **Manage Server** permission to track invites.", ephemeral=True)
        row = await self.bot.db.fetchone("SELECT code FROM invite_links WHERE guild_id=? AND user_id=?", (guild.id, user.id))
        if row and row["code"] in live:
            return await interaction.followup.send(f"Your personal invite: https://discord.gg/{row['code']}", ephemeral=True)
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else guild.system_channel
        if channel is None:
            return await interaction.followup.send("Run this in a normal text channel so I can make the invite.", ephemeral=True)
        try:
            invite = await channel.create_invite(max_age=0, max_uses=0, unique=True, reason=f"Personal invite for {user}")
        except discord.HTTPException:
            return await interaction.followup.send("I couldn't create an invite here (missing Create Invite permission?).", ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO invite_links(guild_id, code, user_id) VALUES(?,?,?)", (guild.id, invite.code, user.id)
        )
        self.cache.setdefault(guild.id, {})[invite.code] = (0, user.id, 0)
        await interaction.followup.send(f"Your personal invite (never expires): {invite.url}", ephemeral=True)

    # ---------- commands ----------
    @app_commands.command(description="Check your invite stats")
    @app_commands.describe(member="Member to look up (defaults to you)")
    async def invites(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.send_message(embed=await self.stats_embed(interaction.guild, member or interaction.user))

    @app_commands.command(description="Generate a personal invite link you can share")
    async def invitecreate(self, interaction: discord.Interaction):
        await self.send_personal_invite(interaction)

    @app_commands.command(description="View the invite leaderboard")
    async def inviteleaderboard(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=await self.leaderboard_embed(interaction.guild))

    @app_commands.command(description="Post the invite tracking panel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def invitepanel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        title = await self.bot.db.get_config(interaction.guild.id, "invite_panel_title", "Invite Tracking")
        embed = make_embed(title, "Check your invites, grab a personal invite link, or see who's on top of the leaderboard.")
        await channel.send(embed=embed, view=InvitePanel(self))
        await interaction.response.send_message(f"Invite panel posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Customize the invite panel title")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setinvitepanel(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 100]):
        await self.bot.db.set_config(interaction.guild.id, "invite_panel_title", title)
        await interaction.response.send_message(f"Invite panel title set to **{title}** (applies to new panels).", ephemeral=True)

    @app_commands.command(description="Toggle: if an invited member leaves, revoke reward roles the inviter no longer qualifies for")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def inviterevoke(self, interaction: discord.Interaction):
        on = await self.bot.db.get_config(interaction.guild.id, "invite_revoke", "0") == "1"
        await self.bot.db.set_config(interaction.guild.id, "invite_revoke", "0" if on else "1")
        await interaction.response.send_message(f"Reward revoking is now **{'OFF' if on else 'ON'}**.", ephemeral=True)


class InviteRewards(commands.GroupCog, group_name="inviterewards", group_description="Invite milestone rewards"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="set", description="Give a role automatically once a member hits X effective invites")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reward_set(self, interaction: discord.Interaction, invites: app_commands.Range[int, 1, 10000], role: discord.Role):
        if role.is_default() or role.managed or role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("I can't hand out that role. Move my role above it first.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO invite_rewards(guild_id, invites, role_id) VALUES(?,?,?)", (interaction.guild.id, invites, role.id)
        )
        await interaction.response.send_message(f"Members get {role.mention} at **{invites}** effective invites.", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="remove", description="Remove an invite milestone reward")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reward_remove(self, interaction: discord.Interaction, invites: int):
        row = await self.bot.db.fetchone("SELECT 1 FROM invite_rewards WHERE guild_id=? AND invites=?", (interaction.guild.id, invites))
        if not row:
            return await interaction.response.send_message(f"No reward is set at {invites} invites.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM invite_rewards WHERE guild_id=? AND invites=?", (interaction.guild.id, invites))
        await interaction.response.send_message(f"Removed the reward at **{invites}** invites.", ephemeral=True)

    @app_commands.command(name="list", description="View all configured invite milestone rewards")
    async def reward_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT invites, role_id FROM invite_rewards WHERE guild_id=? ORDER BY invites", (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("No invite rewards set.", ephemeral=True)
        text = "\n".join(f"**{r['invites']}** invites → <@&{r['role_id']}>" for r in rows)
        await interaction.response.send_message(embed=make_embed("Invite rewards", text), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Invites(bot))
    await bot.add_cog(InviteRewards(bot))
