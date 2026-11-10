import re
import time as _time
from datetime import timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import format_duration, make_embed, owner_only, parse_duration


_CDN_EMOJI = re.compile(r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/emojis/(\d+)(?:\.(\w+))?(?:\?(\S*))?", re.I)


@app_commands.guild_only()
class Utility(commands.GroupCog, group_name="utility", group_description="Utility commands"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, remind_at REAL, text TEXT
            )"""
        )
        self.reminder_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()

    # ---------- reminders ----------
    @tasks.loop(seconds=10)
    async def reminder_loop(self):
        due = await self.bot.db.fetchall("SELECT * FROM reminders WHERE remind_at <= ?", (_time.time(),))
        for r in due:
            await self.bot.db.execute("DELETE FROM reminders WHERE id=?", (r["id"],))
            try:
                user = self.bot.get_user(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
                await user.send(embed=make_embed("Reminder", r["text"], discord.Color.gold()))
            except discord.HTTPException:
                pass

    @reminder_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.command(description="Get a DM reminder after a set time")
    @app_commands.describe(time="e.g. 10m, 2h, 1d12h", message="What to remind you about")
    async def remind(self, interaction: discord.Interaction, time: str, message: app_commands.Range[str, 1, 500]):
        delta = parse_duration(time)
        if not delta or delta < timedelta(seconds=10) or delta > timedelta(days=365):
            return await interaction.response.send_message(
                "Use a time like `30m`, `2h` or `1d12h` (10 seconds to 1 year).", ephemeral=True
            )
        await self.bot.db.execute(
            "INSERT INTO reminders(user_id, remind_at, text) VALUES(?,?,?)",
            (interaction.user.id, _time.time() + delta.total_seconds(), message),
        )
        await interaction.response.send_message(
            f"I'll DM you in **{format_duration(delta)}**: {message}", ephemeral=True
        )

    # ---------- user / profile ----------
    @app_commands.command(description="Get a user's avatar")
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None):
        user = user or interaction.user
        embed = make_embed(f"{user.display_name}'s avatar")
        embed.set_image(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Get a user's banner")
    async def banner(self, interaction: discord.Interaction, user: discord.User | None = None):
        user = user or interaction.user
        fetched = await self.bot.fetch_user(user.id)
        if not fetched.banner:
            return await interaction.response.send_message("That user has no banner.", ephemeral=True)
        embed = make_embed(f"{user.display_name}'s banner")
        embed.set_image(url=fetched.banner.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="View information about a user")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        roles = [r.mention for r in reversed(member.roles) if r != interaction.guild.default_role]
        embed = make_embed(str(member), color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=member.id)
        embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"))
        embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "?")
        embed.add_field(name=f"Roles ({len(roles)})", value=(" ".join(roles[:20]) or "None")[:1024], inline=False)
        await interaction.response.send_message(embed=embed)

    # ---------- server / channel ----------
    @app_commands.command(description="Get info about a channel")
    async def channelinfo(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel | None = None):
        channel = channel or interaction.channel
        embed = make_embed(f"#{channel.name}")
        embed.add_field(name="ID", value=channel.id)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title())
        embed.add_field(name="Category", value=getattr(channel.category, "name", "None"))
        embed.add_field(name="Created", value=discord.utils.format_dt(channel.created_at, "R"))
        if getattr(channel, "topic", None):
            embed.add_field(name="Topic", value=channel.topic[:1024], inline=False)
        if getattr(channel, "slowmode_delay", 0):
            embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Get the direct link of an image from a message")
    @app_commands.describe(message_id="Message ID or message link (from this channel)")
    async def imagelink(self, interaction: discord.Interaction, message_id: str):
        nums = re.findall(r"\d+", message_id)
        if not nums:
            return await interaction.response.send_message("Give me a message ID or link.", ephemeral=True)
        try:
            msg = await interaction.channel.fetch_message(int(nums[-1]))
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't find that message in this channel.", ephemeral=True)
        urls = [a.url for a in msg.attachments if (a.content_type or "").startswith("image/")]
        for e in msg.embeds:
            if e.image:
                urls.append(e.image.url)
            if e.thumbnail:
                urls.append(e.thumbnail.url)
        if not urls:
            return await interaction.response.send_message("No images in that message.", ephemeral=True)
        await interaction.response.send_message("\n".join(urls), ephemeral=True)

    @app_commands.command(description="Show the server member count")
    async def membercount(self, interaction: discord.Interaction):
        g = interaction.guild
        bots = sum(1 for m in g.members if m.bot)
        await interaction.response.send_message(
            embed=make_embed("Members", f"**{g.member_count}** total\n{g.member_count - bots} humans, {bots} bots")
        )

    @app_commands.command(description="View information about this server")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        embed = make_embed(g.name)
        embed.add_field(name="Owner", value=f"<@{g.owner_id}>")
        embed.add_field(name="Members", value=g.member_count)
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
        embed.add_field(name="Channels", value=f"{len(g.text_channels)} text, {len(g.voice_channels)} voice")
        embed.add_field(name="Roles", value=len(g.roles))
        embed.add_field(name="Emojis", value=f"{len(g.emojis)}/{g.emoji_limit}")
        embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (tier {g.premium_tier})")
        embed.set_footer(text=f"ID: {g.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="List all roles in this server")
    async def rolelist(self, interaction: discord.Interaction):
        roles = [r.mention for r in reversed(interaction.guild.roles) if r != interaction.guild.default_role]
        text = "\n".join(roles)
        if len(text) > 4000:
            text = text[:4000].rsplit("\n", 1)[0] + "\n..."
        await interaction.response.send_message(embed=make_embed(f"Roles ({len(roles)})", text or "No roles."))

    # ---------- bot ----------
    @app_commands.command(description="Check the bot's latency")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Pong! {round(self.bot.latency * 1000)}ms")

    @app_commands.command(description="Check how long the bot has been online")
    async def uptime(self, interaction: discord.Interaction):
        delta = discord.utils.utcnow() - self.bot.start_time
        await interaction.response.send_message(
            f"Online for **{format_duration(delta)}** (since {discord.utils.format_dt(self.bot.start_time, 'R')})"
        )

    @app_commands.command(description="Sync slash commands (owner only)")
    @owner_only()
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        synced = await self.bot.tree.sync()
        await interaction.followup.send(f"Synced {len(synced)} commands globally (can take a few minutes to show).", ephemeral=True)

    # ---------- emojis ----------
    @app_commands.command(description="Add a custom emoji from another server to this one")
    @app_commands.describe(emoji="The custom emoji, or a cdn.discordapp.com/emojis/... link", name="Optional new name")
    @app_commands.checks.has_permissions(manage_expressions=True)
    @app_commands.checks.bot_has_permissions(manage_expressions=True)
    async def steal(self, interaction: discord.Interaction, emoji: str, name: str | None = None):
        emoji = emoji.strip()
        m = _CDN_EMOJI.fullmatch(emoji)
        if m:
            emoji_id = int(m.group(1))
            animated = (m.group(2) or "").lower() == "gif" or "animated=true" in (m.group(3) or "").lower()
            default_name = f"emoji_{emoji_id % 100000}"
        else:
            pe = discord.PartialEmoji.from_str(emoji)
            if not pe.id:
                return await interaction.response.send_message(
                    "Paste a custom emoji or a cdn.discordapp.com/emojis/... link.", ephemeral=True)
            emoji_id, animated, default_name = pe.id, pe.animated, pe.name
        await interaction.response.defer()
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}?size=128"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return await interaction.followup.send("I couldn't download that emoji.")
                data = await resp.read()
        try:
            new = await interaction.guild.create_custom_emoji(name=name or default_name, image=data, reason=f"Stolen by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Couldn't add it (emoji slots full?): {e}")
        await interaction.followup.send(f"Added {new} as `:{new.name}:`")


async def setup(bot):
    await bot.add_cog(Utility(bot))
