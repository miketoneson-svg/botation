import asyncio
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils import format_duration, make_embed

NO_MENTIONS = discord.AllowedMentions.none()
SAFE_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)
DEFAULT_RULES = "1. Be respectful to everyone.\n2. No spam, scams or advertising.\n3. Follow Discord's Terms of Service."


def nl(text: str) -> str:
    return text.replace("\\n", "\n")


# ---------------------------------------------------------------- modals
class EmbedModal(discord.ui.Modal, title="Create embed"):
    embed_title = discord.ui.TextInput(label="Title", required=False, max_length=256)
    body = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=4000)
    color = discord.ui.TextInput(label="Color (hex, e.g. #5865F2)", required=False, max_length=7)
    image = discord.ui.TextInput(label="Image URL", required=False, max_length=500)
    footer = discord.ui.TextInput(label="Footer", required=False, max_length=200)

    def __init__(self, channel: discord.abc.Messageable):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        title, body = str(self.embed_title).strip(), str(self.body).strip()
        if not title and not body:
            return await interaction.response.send_message("Give the embed a title or a description.", ephemeral=True)
        color = discord.Color.blurple()
        raw = str(self.color).strip().lstrip("#")
        if raw:
            try:
                if len(raw) != 6:
                    raise ValueError
                color = discord.Color(int(raw, 16))
            except ValueError:
                return await interaction.response.send_message("The color must be a 6-digit hex code like `#5865F2`.", ephemeral=True)
        embed = discord.Embed(title=title or None, description=body or None, color=color)
        image = str(self.image).strip()
        if image:
            if not image.startswith(("http://", "https://")):
                return await interaction.response.send_message("The image must be a direct link starting with http.", ephemeral=True)
            embed.set_image(url=image)
        if str(self.footer).strip():
            embed.set_footer(text=str(self.footer).strip())
        try:
            await self.channel.send(embed=embed)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"I couldn't send that: {e}", ephemeral=True)
        await interaction.response.send_message("Embed sent.", ephemeral=True)


class RulesModal(discord.ui.Modal, title="Server rules"):
    def __init__(self, cog, default: str):
        super().__init__()
        self.cog = cog
        self.rules = discord.ui.TextInput(label="Rules", style=discord.TextStyle.paragraph, default=default[:4000], max_length=4000)
        self.add_item(self.rules)

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.rules).strip()
        await self.cog.bot.db.set_config(interaction.guild.id, "rules_text", text)
        embed = make_embed("📜 Server Rules", text, discord.Color.gold())
        embed.set_footer(text="By staying in this server you agree to follow these rules.")
        try:
            await interaction.channel.send(embed=embed)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"I couldn't post the rules here: {e}", ephemeral=True)
        await interaction.response.send_message("Rules posted. Run `/rules` again any time to edit and repost them.", ephemeral=True)


# ---------------------------------------------------------------- main community cog
class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.afk_users: dict[tuple[int, int], tuple[str, float]] = {}
        self.deleted: dict[int, dict] = {}
        self.suggest_cooldown: dict[tuple[int, int], float] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS afk (guild_id INTEGER, user_id INTEGER, reason TEXT, since REAL, PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, content TEXT, created_at REAL)")
        for r in await db.fetchall("SELECT * FROM afk"):
            self.afk_users[(r["guild_id"], r["user_id"])] = (r["reason"], r["since"])

    # ---------- afk ----------
    @app_commands.command(description="Set yourself as AFK")
    @app_commands.describe(reason="Why you're away")
    async def afk(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 100] = "AFK"):
        now = time.time()
        self.afk_users[(interaction.guild.id, interaction.user.id)] = (reason, now)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO afk(guild_id, user_id, reason, since) VALUES(?,?,?,?)", (interaction.guild.id, interaction.user.id, reason, now)
        )
        await interaction.response.send_message(f"{interaction.user.mention} is now AFK: {reason}", allowed_mentions=NO_MENTIONS)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        gid = message.guild.id
        key = (gid, message.author.id)
        if key in self.afk_users:
            _, since = self.afk_users.pop(key)
            await self.bot.db.execute("DELETE FROM afk WHERE guild_id=? AND user_id=?", key)
            away = format_duration(timedelta(seconds=int(time.time() - since)))
            try:
                await message.channel.send(f"Welcome back {message.author.mention}, I removed your AFK (away {away}).",
                                           delete_after=8, allowed_mentions=discord.AllowedMentions(users=[message.author]))
            except discord.HTTPException:
                pass
        notices = 0
        for m in message.mentions:
            entry = self.afk_users.get((gid, m.id))
            if entry and m.id != message.author.id and notices < 3:
                notices += 1
                try:
                    await message.channel.send(f"**{m.display_name}** is AFK: {entry[0]} (<t:{int(entry[1])}:R>)", delete_after=10, allowed_mentions=NO_MENTIONS)
                except discord.HTTPException:
                    pass

    # ---------- snipe ----------
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        self.deleted[message.channel.id] = {
            "author": message.author, "content": message.content,
            "files": [a.url for a in message.attachments], "time": discord.utils.utcnow(),
        }

    @app_commands.command(description="Show the last deleted message in this channel")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def snipe(self, interaction: discord.Interaction):
        data = self.deleted.get(interaction.channel.id)
        if not data:
            return await interaction.response.send_message("Nothing to snipe here.", ephemeral=True)
        embed = make_embed(None, data["content"] or "*(no text)*", discord.Color.red(), timestamp=data["time"])
        embed.set_author(name=str(data["author"]), icon_url=data["author"].display_avatar.url)
        if data["files"]:
            embed.add_field(name="Attachments", value="\n".join(data["files"])[:1000], inline=False)
        embed.set_footer(text="Deleted")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- say / embed / announce / testembed ----------
    @app_commands.command(description="Make the bot say something in a channel")
    @app_commands.describe(message="What to say (use \\n for a new line)", channel="Where to send it (default: here)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def say(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 2000], channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        try:
            await channel.send(nl(message), allowed_mentions=SAFE_MENTIONS)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"I couldn't send that: {e}", ephemeral=True)
        await interaction.response.send_message(f"Sent in {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Create and send a custom embed")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await interaction.response.send_modal(EmbedModal(channel or interaction.channel))

    @app_commands.command(description="Send an announcement embed to a channel")
    @app_commands.describe(message="Use \\n for a new line", ping="Role to ping (optional)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, title: app_commands.Range[str, 1, 200],
                       message: app_commands.Range[str, 1, 3500], ping: discord.Role | None = None):
        embed = make_embed(f"📢 {title}", nl(message), discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.set_footer(text=f"Announcement by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        try:
            await channel.send(content=ping.mention if ping else None, embed=embed,
                               allowed_mentions=discord.AllowedMentions(roles=[ping]) if ping else NO_MENTIONS)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"I couldn't post there: {e}", ephemeral=True)
        await interaction.response.send_message(f"Announcement sent in {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Test if embeds work in this channel")
    async def testembed(self, interaction: discord.Interaction):
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not perms.embed_links:
            return await interaction.response.send_message("❌ I'm missing the **Embed Links** permission in this channel.", ephemeral=True)
        await interaction.response.send_message(embed=make_embed("Embeds work here", "✅ I can send embeds in this channel.", discord.Color.green()))

    # ---------- rules / poll / suggestions ----------
    @app_commands.command(description="Post a rules embed in this channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rules(self, interaction: discord.Interaction):
        saved = await self.bot.db.get_config(interaction.guild.id, "rules_text", DEFAULT_RULES)
        await interaction.response.send_modal(RulesModal(self, saved))

    @app_commands.command(description="Create a quick yes/no poll")
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 250]):
        embed = make_embed("📊 Poll", question, discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.set_footer(text=f"Asked by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        try:
            for emoji in ("✅", "❌"):
                await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass

    @app_commands.command(description="Set the channel where /suggest posts suggestions")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setsuggestchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "suggest_channel", channel.id)
        await interaction.response.send_message(f"Suggestions will be posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Submit a suggestion for the server")
    async def suggest(self, interaction: discord.Interaction, suggestion: app_commands.Range[str, 1, 1000]):
        guild, user = interaction.guild, interaction.user
        cid = await self.bot.db.get_config(guild.id, "suggest_channel")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return await interaction.response.send_message("Suggestions aren't set up yet. Ask an admin to run `/setsuggestchannel`.", ephemeral=True)
        if time.time() - self.suggest_cooldown.get((guild.id, user.id), 0) < 60:
            return await interaction.response.send_message("Please wait a minute between suggestions.", ephemeral=True)
        self.suggest_cooldown[(guild.id, user.id)] = time.time()
        sid = await self.bot.db.execute("INSERT INTO suggestions(guild_id, user_id, content, created_at) VALUES(?,?,?,?)", (guild.id, user.id, suggestion, time.time()))
        embed = make_embed("💡 New Suggestion", suggestion, discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.set_footer(text=f"Suggestion #{sid}")
        try:
            msg = await channel.send(embed=embed)
            for emoji in ("👍", "👎"):
                await msg.add_reaction(emoji)
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post in the suggestions channel.", ephemeral=True)
        await interaction.response.send_message(f"Thanks! Your suggestion was posted in {channel.mention}.", ephemeral=True)


# ---------------------------------------------------------------- /status
@app_commands.guild_only()
class Status(commands.GroupCog, group_name="status", group_description="Custom status notes for members"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS member_status (guild_id INTEGER, user_id INTEGER, note TEXT, set_by INTEGER, updated REAL, PRIMARY KEY (guild_id, user_id))"
        )

    @app_commands.command(name="set", description="Set a custom status note for a member")
    @app_commands.describe(note="Type 'clear' to remove it")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def status_set(self, interaction: discord.Interaction, member: discord.Member, note: app_commands.Range[str, 1, 200]):
        gid = interaction.guild.id
        if note.strip().lower() in ("clear", "none", "reset"):
            await self.bot.db.execute("DELETE FROM member_status WHERE guild_id=? AND user_id=?", (gid, member.id))
            return await interaction.response.send_message(f"Cleared {member.mention}'s status note.", ephemeral=True, allowed_mentions=NO_MENTIONS)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO member_status(guild_id, user_id, note, set_by, updated) VALUES(?,?,?,?,?)",
            (gid, member.id, note, interaction.user.id, time.time()),
        )
        await interaction.response.send_message(f"Status note set for {member.mention}.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="view", description="View a member's custom status note")
    async def status_view(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        row = await self.bot.db.fetchone("SELECT * FROM member_status WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        if not row:
            return await interaction.response.send_message(f"{member.display_name} has no status note.", ephemeral=True)
        embed = make_embed(f"Status for {member.display_name}", row["note"], member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Last updated")
        embed.timestamp = datetime.fromtimestamp(row["updated"], tz=timezone.utc)
        await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------- /stick
@app_commands.guild_only()
class Stick(commands.GroupCog, group_name="stick", group_description="Sticky messages that stay at the bottom"):
    def __init__(self, bot):
        self.bot = bot
        self.sticky: dict[int, dict] = {}
        self.pending: dict[int, asyncio.Task] = {}
        super().__init__()

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS sticky (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, content TEXT, last_message_id INTEGER)"
        )
        for r in await self.bot.db.fetchall("SELECT * FROM sticky"):
            self.sticky[r["channel_id"]] = {"content": r["content"], "last": r["last_message_id"]}

    async def cog_unload(self):
        for task in self.pending.values():
            task.cancel()

    async def repost(self, channel):
        data = self.sticky.get(channel.id)
        if not data:
            return
        if data["last"]:
            try:
                await channel.get_partial_message(data["last"]).delete()
            except discord.HTTPException:
                pass
        msg = await channel.send(embed=make_embed("📌 Sticky message", nl(data["content"]), discord.Color.gold()))
        data["last"] = msg.id
        await self.bot.db.execute("UPDATE sticky SET last_message_id=? WHERE channel_id=?", (msg.id, channel.id))

    async def _delayed(self, channel):
        try:
            await asyncio.sleep(2)  # wait for the chat to settle so we don't repost on every message
            await self.repost(channel)
        except discord.HTTPException:
            pass
        finally:
            self.pending.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.channel.id not in self.sticky or message.channel.id in self.pending:
            return
        if message.author.id == self.bot.user.id:
            return
        self.pending[message.channel.id] = asyncio.create_task(self._delayed(message.channel))

    @app_commands.command(name="set", description="Make a message sticky (stays at the bottom of the channel)")
    @app_commands.describe(message="The sticky text (use \\n for a new line)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def stick_set(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 1500]):
        channel = interaction.channel
        await interaction.response.defer(ephemeral=True)
        old = self.sticky.get(channel.id)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO sticky(channel_id, guild_id, content, last_message_id) VALUES(?,?,?,?)",
            (channel.id, interaction.guild.id, message, old["last"] if old else None),
        )
        self.sticky[channel.id] = {"content": message, "last": old["last"] if old else None}
        await self.repost(channel)
        await interaction.followup.send("Sticky message set.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove the sticky message from this channel")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def stick_remove(self, interaction: discord.Interaction):
        data = self.sticky.pop(interaction.channel.id, None)
        if not data:
            return await interaction.response.send_message("This channel has no sticky message.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM sticky WHERE channel_id=?", (interaction.channel.id,))
        if data["last"]:
            try:
                await interaction.channel.get_partial_message(data["last"]).delete()
            except discord.HTTPException:
                pass
        await interaction.response.send_message("Sticky message removed.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Community(bot))
    await bot.add_cog(Status(bot))
    await bot.add_cog(Stick(bot))
