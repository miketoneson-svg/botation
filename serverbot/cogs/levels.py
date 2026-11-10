import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils import fill, make_embed

XP_MIN, XP_MAX = 15, 25
COOLDOWN = 60  # seconds between XP gains per member
MAX_LEVEL = 500
DEFAULT_MESSAGE = "GG {user}, you reached level **{level}**!"


def total_xp_for_level(level: int) -> int:
    """Total XP needed to reach `level` (each level n -> n+1 costs 5n^2 + 50n + 100)."""
    if level <= 0:
        return 0
    return 5 * ((level - 1) * level * (2 * level - 1) // 6) + 25 * level * (level - 1) + 100 * level


def level_from_xp(xp: int) -> int:
    level = 0
    while level < MAX_LEVEL and total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cooldowns: dict[tuple[int, int], float] = {}

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS level_roles (guild_id INTEGER, level INTEGER, role_id INTEGER, PRIMARY KEY (guild_id, level))"
        )

    # ---------- core ----------
    async def get_xp(self, guild_id: int, user_id: int) -> int:
        row = await self.bot.db.fetchone("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return row["xp"] if row else 0

    async def save_xp(self, guild_id: int, user_id: int, xp: int) -> int:
        level = level_from_xp(xp)
        await self.bot.db.execute(
            "INSERT INTO levels(guild_id, user_id, xp, level) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, level=excluded.level",
            (guild_id, user_id, xp, level),
        )
        return level

    async def sync_roles(self, member: discord.Member, level: int, remove_higher: bool = False):
        rows = await self.bot.db.fetchall("SELECT level, role_id FROM level_roles WHERE guild_id=?", (member.guild.id,))
        add, remove = [], []
        for r in rows:
            role = member.guild.get_role(r["role_id"])
            if not role or role >= member.guild.me.top_role:
                continue
            if r["level"] <= level and role not in member.roles:
                add.append(role)
            elif remove_higher and r["level"] > level and role in member.roles:
                remove.append(role)
        try:
            if add:
                await member.add_roles(*add, reason=f"Reached level {level}")
            if remove:
                await member.remove_roles(*remove, reason="Level was lowered")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self.cooldowns.get(key, 0) < COOLDOWN:
            return
        self.cooldowns[key] = now

        old_xp = await self.get_xp(*key)
        new_xp = old_xp + random.randint(XP_MIN, XP_MAX)
        old_level = level_from_xp(old_xp)
        new_level = await self.save_xp(*key, new_xp)
        if new_level > old_level:
            await self.sync_roles(message.author, new_level)
            await self.announce(message, message.author, new_level)

    async def announce(self, message: discord.Message, member: discord.Member, level: int):
        get = self.bot.db.get_config
        template = await get(member.guild.id, "level_message", DEFAULT_MESSAGE)
        text = fill(template, user=member.mention, username=member.display_name, level=level, server=member.guild.name)
        channel_id = await get(member.guild.id, "level_channel")
        channel = member.guild.get_channel(int(channel_id)) if channel_id else message.channel
        try:
            await (channel or message.channel).send(text, allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException:
            pass

    # ---------- everyone ----------
    @app_commands.command(description="Check your or someone else's level")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        xp = await self.get_xp(interaction.guild.id, member.id)
        level = level_from_xp(xp)
        base, nxt = total_xp_for_level(level), total_xp_for_level(level + 1)
        done, need = xp - base, nxt - base
        filled = int(done / need * 12) if need else 12
        position = (await self.bot.db.fetchone(
            "SELECT COUNT(*) AS n FROM levels WHERE guild_id=? AND xp>?", (interaction.guild.id, xp)
        ))["n"] + 1
        embed = make_embed(member.display_name, color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="Rank", value=f"#{position}")
        embed.add_field(name="Total XP", value=f"{xp:,}")
        embed.add_field(name=f"Progress to level {level + 1}", value=f"`{'█' * filled}{'░' * (12 - filled)}` {done:,}/{need:,} XP", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="View the server XP leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT user_id, xp FROM levels WHERE guild_id=? AND xp>0 ORDER BY xp DESC LIMIT 10", (interaction.guild.id,)
        )
        if not rows:
            return await interaction.response.send_message("Nobody has any XP yet.")
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> · Level **{level_from_xp(r['xp'])}** · {r['xp']:,} XP"
            for i, r in enumerate(rows)
        ]
        await interaction.response.send_message(embed=make_embed("XP leaderboard", "\n".join(lines), discord.Color.gold()))

    # ---------- admin ----------
    @app_commands.command(description="Manually set a user's XP")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setxp(self, interaction: discord.Interaction, member: discord.Member, xp: app_commands.Range[int, 0, 100_000_000]):
        level = await self.save_xp(interaction.guild.id, member.id, xp)
        await self.sync_roles(member, level, remove_higher=True)
        await interaction.response.send_message(f"Set {member.mention} to **{xp:,} XP** (level {level}).", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Manually set a user's level")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setlevel(self, interaction: discord.Interaction, member: discord.Member, level: app_commands.Range[int, 0, MAX_LEVEL]):
        await self.save_xp(interaction.guild.id, member.id, total_xp_for_level(level))
        await self.sync_roles(member, level, remove_higher=True)
        await interaction.response.send_message(f"Set {member.mention} to **level {level}**.", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Reset a user's XP and level to zero")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resetxp(self, interaction: discord.Interaction, member: discord.Member):
        await self.save_xp(interaction.guild.id, member.id, 0)
        await self.sync_roles(member, 0, remove_higher=True)
        await interaction.response.send_message(f"Reset {member.mention}'s XP and level.", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Set the channel for level-up messages")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setlevelchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "level_channel", channel.id)
        await interaction.response.send_message(f"Level-up messages will go to {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Customize the level-up message")
    @app_commands.describe(message="Placeholders: {user} {username} {level} {server}")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setlevelmessage(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 500]):
        await self.bot.db.set_config(interaction.guild.id, "level_message", message)
        preview = fill(message, user=interaction.user.mention, username=interaction.user.display_name, level=5, server=interaction.guild.name)
        await interaction.response.send_message(f"Saved. Preview:\n{preview}", ephemeral=True)

    @app_commands.command(description="Set a role reward for reaching a certain level")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def setlevelrole(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, MAX_LEVEL], role: discord.Role):
        if role.is_default() or role.managed or role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("I can't hand out that role. Move my role above it first.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO level_roles(guild_id, level, role_id) VALUES(?,?,?)", (interaction.guild.id, level, role.id)
        )
        await interaction.response.send_message(f"Members get {role.mention} at **level {level}**.", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Remove a level role reward")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def removelevelrole(self, interaction: discord.Interaction, level: int):
        row = await self.bot.db.fetchone("SELECT 1 FROM level_roles WHERE guild_id=? AND level=?", (interaction.guild.id, level))
        if not row:
            return await interaction.response.send_message(f"No role reward is set at level {level}.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM level_roles WHERE guild_id=? AND level=?", (interaction.guild.id, level))
        await interaction.response.send_message(f"Removed the role reward at **level {level}**.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Levels(bot))
