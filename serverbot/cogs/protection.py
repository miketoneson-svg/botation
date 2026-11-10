import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils import Confirm, make_embed, send_log

PING_LIMIT = 3          # pings of a protected member...
PING_WINDOW = 60        # ...within this many seconds...
PING_TIMEOUT = timedelta(minutes=10)  # ...earns this timeout


class Protection(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.protected: set[tuple[int, int]] = set()
        self.pings: dict[tuple[int, int, int], deque] = defaultdict(deque)

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS ping_protect (guild_id INTEGER, user_id INTEGER, PRIMARY KEY (guild_id, user_id))")
        for r in await self.bot.db.fetchall("SELECT * FROM ping_protect"):
            self.protected.add((r["guild_id"], r["user_id"]))

    @app_commands.command(description="Toggle ping-spam protection for a member (auto-timeout if they mass-mention)")
    @app_commands.describe(member="The member to protect from being ping-spammed")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pingspamprotect(self, interaction: discord.Interaction, member: discord.Member):
        key = (interaction.guild.id, member.id)
        if key in self.protected:
            self.protected.discard(key)
            await self.bot.db.execute("DELETE FROM ping_protect WHERE guild_id=? AND user_id=?", key)
            text = f"Ping protection is now **OFF** for {member.mention}."
        else:
            self.protected.add(key)
            await self.bot.db.execute("INSERT OR IGNORE INTO ping_protect(guild_id, user_id) VALUES(?,?)", key)
            text = (f"Ping protection is now **ON** for {member.mention}. Anyone who pings them {PING_LIMIT}+ times "
                    f"within {PING_WINDOW}s gets a {int(PING_TIMEOUT.total_seconds() // 60)} minute timeout.")
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.protected or message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        author, gid = message.author, message.guild.id
        if author.guild_permissions.manage_messages:
            return
        now = time.monotonic()
        for target in message.mentions:
            if (gid, target.id) not in self.protected or target.id == author.id:
                continue
            dq = self.pings[(gid, author.id, target.id)]
            dq.append(now)
            while dq and now - dq[0] > PING_WINDOW:
                dq.popleft()
            if len(dq) < PING_LIMIT:
                continue
            dq.clear()
            try:
                await author.timeout(PING_TIMEOUT, reason=f"Ping spam against {target}")
            except discord.HTTPException:
                pass
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                await message.channel.send(f"{author.mention} was timed out for spam-pinging {target.display_name}.", delete_after=10,
                                           allowed_mentions=discord.AllowedMentions(users=[author]))
            except discord.HTTPException:
                pass
            embed = make_embed("Ping spam", color=discord.Color.red(), timestamp=discord.utils.utcnow())
            embed.add_field(name="User", value=f"{author} ({author.id})")
            embed.add_field(name="Pinged", value=target.mention)
            embed.add_field(name="Channel", value=message.channel.mention)
            await send_log(self.bot, message.guild, embed)
            break

    @app_commands.command(description="Instantly clear a channel by cloning it and deleting the original")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def nuke(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        user = interaction.user

        async def action(i: discord.Interaction):
            try:
                new = await channel.clone(reason=f"Nuked by {user}")
                await new.edit(position=channel.position)
                await channel.delete(reason=f"Nuked by {user}")
            except discord.HTTPException as e:
                return await i.followup.send(f"Nuke failed: {e}", ephemeral=True)
            await new.send(embed=make_embed("💥 Channel nuked", f"All messages were cleared by {user.mention}.", discord.Color.red()))

        await interaction.response.send_message(
            f"Delete every message in {channel.mention}? The channel is recreated with the same settings, but **message history can't be recovered**.",
            view=Confirm(user.id, action, "Nuke it"), ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Protection(bot))
