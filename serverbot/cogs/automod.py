import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils import make_embed, send_log

INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.I)
LINK_RE = re.compile(r"https?://\S+|www\.\S+", re.I)

SPAM_COUNT = 5      # messages...
SPAM_WINDOW = 5     # ...within this many seconds triggers anti-spam
SPAM_TIMEOUT = timedelta(minutes=5)


@app_commands.guild_only()
class AutoMod(commands.GroupCog, group_name="automod", group_description="Automatic moderation"):
    def __init__(self, bot):
        self.bot = bot
        self._settings: dict[int, dict[str, bool]] = {}
        self._words: dict[int, list[re.Pattern]] = {}
        self._spam: dict[tuple[int, int], deque] = defaultdict(deque)
        super().__init__()

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS automod_words (guild_id INTEGER, word TEXT, PRIMARY KEY (guild_id, word))"
        )

    # ---------- cached settings ----------
    async def settings(self, guild_id: int) -> dict[str, bool]:
        if guild_id not in self._settings:
            get = self.bot.db.get_config
            self._settings[guild_id] = {
                "enabled": await get(guild_id, "automod_enabled", "0") == "1",
                "invites": await get(guild_id, "automod_invites", "0") == "1",
                "links": await get(guild_id, "automod_links", "0") == "1",
                "spam": await get(guild_id, "automod_spam", "1") == "1",
            }
        return self._settings[guild_id]

    async def patterns(self, guild_id: int) -> list[re.Pattern]:
        if guild_id not in self._words:
            rows = await self.bot.db.fetchall("SELECT word FROM automod_words WHERE guild_id=?", (guild_id,))
            self._words[guild_id] = [re.compile(rf"(?<!\w){re.escape(r['word'])}(?!\w)", re.I) for r in rows]
        return self._words[guild_id]

    async def _set(self, guild_id: int, key: str, on: bool):
        await self.bot.db.set_config(guild_id, f"automod_{key}", "1" if on else "0")
        self._settings.pop(guild_id, None)

    async def _toggle(self, interaction: discord.Interaction, key: str, label: str):
        current = (await self.settings(interaction.guild.id))[key]
        await self._set(interaction.guild.id, key, not current)
        await interaction.response.send_message(f"{label} is now **{'ON' if not current else 'OFF'}**.", ephemeral=True)

    # ---------- commands ----------
    @app_commands.command(description="Enable automod for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable(self, interaction: discord.Interaction):
        await self._set(interaction.guild.id, "enabled", True)
        await interaction.response.send_message("AutoMod is **enabled**.", ephemeral=True)

    @app_commands.command(description="Disable automod for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction):
        await self._set(interaction.guild.id, "enabled", False)
        await interaction.response.send_message("AutoMod is **disabled**.", ephemeral=True)

    @app_commands.command(description="Add a word to the banned words list")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def addword(self, interaction: discord.Interaction, word: app_commands.Range[str, 1, 50]):
        word = word.strip().lower()
        await self.bot.db.execute("INSERT OR IGNORE INTO automod_words(guild_id, word) VALUES(?,?)", (interaction.guild.id, word))
        self._words.pop(interaction.guild.id, None)
        await interaction.response.send_message("Word added to the banned list.", ephemeral=True)

    @app_commands.command(description="Remove a word from the banned words list")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def removeword(self, interaction: discord.Interaction, word: app_commands.Range[str, 1, 50]):
        await self.bot.db.execute("DELETE FROM automod_words WHERE guild_id=? AND word=?", (interaction.guild.id, word.strip().lower()))
        self._words.pop(interaction.guild.id, None)
        await interaction.response.send_message("Word removed (if it was on the list).", ephemeral=True)

    @app_commands.command(name="settings", description="View current automod settings")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def view_settings(self, interaction: discord.Interaction):
        s = await self.settings(interaction.guild.id)
        words = len(await self.patterns(interaction.guild.id))
        on = lambda v: "ON" if v else "OFF"
        embed = make_embed("AutoMod settings")
        embed.add_field(name="AutoMod", value=on(s["enabled"]))
        embed.add_field(name="Block invites", value=on(s["invites"]))
        embed.add_field(name="Block all links", value=on(s["links"]))
        embed.add_field(name="Anti-spam", value=on(s["spam"]))
        embed.add_field(name="Banned words", value=str(words))
        embed.set_footer(text="Admins and members with Manage Messages are exempt.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Toggle blocking Discord invite links")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggleinvites(self, interaction: discord.Interaction):
        await self._toggle(interaction, "invites", "Invite blocking")

    @app_commands.command(description="Toggle blocking all links")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglelinks(self, interaction: discord.Interaction):
        await self._toggle(interaction, "links", "Link blocking")

    @app_commands.command(description="Toggle basic anti-spam protection")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglespam(self, interaction: discord.Interaction):
        await self._toggle(interaction, "spam", "Anti-spam")

    # ---------- enforcement ----------
    async def _punish(self, message: discord.Message, reason: str):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(f"{message.author.mention} {reason}", delete_after=6)
        except discord.HTTPException:
            pass
        embed = make_embed("AutoMod", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{message.author} ({message.author.id})")
        embed.add_field(name="Channel", value=message.channel.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        if message.content:
            embed.add_field(name="Message", value=message.content[:1000], inline=False)
        await send_log(self.bot, message.guild, embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        s = await self.settings(message.guild.id)
        if not s["enabled"]:
            return
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

        content = message.content
        if s["invites"] and INVITE_RE.search(content):
            return await self._punish(message, "Discord invites aren't allowed here.")
        if s["links"] and LINK_RE.search(content):
            return await self._punish(message, "Links aren't allowed here.")
        pats = await self.patterns(message.guild.id)
        if pats and any(p.search(content) for p in pats):
            return await self._punish(message, "That language isn't allowed here.")

        if s["spam"]:
            key = (message.guild.id, message.author.id)
            now = time.monotonic()
            dq = self._spam[key]
            dq.append(now)
            while dq and now - dq[0] > SPAM_WINDOW:
                dq.popleft()
            if len(dq) >= SPAM_COUNT:
                dq.clear()
                try:
                    await message.author.timeout(SPAM_TIMEOUT, reason="AutoMod: spam")
                except discord.HTTPException:
                    pass
                await self._punish(message, "You're sending messages too fast. You've been timed out for 5 minutes.")


async def setup(bot):
    cog = AutoMod(bot)
    await bot.add_cog(cog)
