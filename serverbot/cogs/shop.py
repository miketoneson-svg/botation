import time

import discord
from discord import app_commands
from discord.ext import commands

from utils import make_embed

NO_MENTIONS = discord.AllowedMentions.none()


# ================================================================= vouch / legit
class VouchModal(discord.ui.Modal, title="Leave a vouch"):
    product = discord.ui.TextInput(label="What did you buy?", max_length=100)
    rating = discord.ui.TextInput(label="Rating (1-5)", min_length=1, max_length=1, placeholder="5")
    feedback = discord.ui.TextInput(label="Feedback", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.post_vouch(interaction, str(self.product), str(self.rating), str(self.feedback))


class LegitView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Legit", emoji="✅", style=discord.ButtonStyle.success, custom_id="legit:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.cast_vote(interaction, 1)

    @discord.ui.button(label="Not legit", emoji="❌", style=discord.ButtonStyle.danger, custom_id="legit:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.cast_vote(interaction, 0)


class ConfirmDeleteSlot(discord.ui.View):
    def __init__(self, cog, user_id: int, target: discord.User, row):
        super().__init__(timeout=30)
        self.cog, self.user_id, self.target, self.row = cog, user_id, target, row

    @discord.ui.button(label="Delete permanently", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
        channel = interaction.guild.get_channel(self.row["channel_id"])
        if channel:
            try:
                await channel.delete(reason=f"Slot deleted by {interaction.user}")
            except discord.HTTPException:
                pass
        await self.cog.bot.db.execute("DELETE FROM slots WHERE guild_id=? AND user_id=?", (interaction.guild.id, self.target.id))
        await interaction.response.edit_message(content=f"Deleted {self.target.mention}'s slot.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


# ================================================================= main cog
class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS vouches (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, product TEXT, rating INTEGER, feedback TEXT, created_at REAL)")
        await db.execute("CREATE TABLE IF NOT EXISTS legit_votes (guild_id INTEGER, message_id INTEGER, user_id INTEGER, vote INTEGER, PRIMARY KEY (message_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS slots (guild_id INTEGER, user_id INTEGER, channel_id INTEGER, revoked INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        self.bot.add_view(LegitView(self))

    # ---------- restock ----------
    @app_commands.command(description="Post a restock announcement")
    @app_commands.describe(product="What was restocked", quantity="How many are in stock", price="e.g. $10", note="Extra info",
                           channel="Where to post (default: here)", ping="Role to ping (optional)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def restock(self, interaction: discord.Interaction, product: app_commands.Range[str, 1, 200], quantity: app_commands.Range[int, 1, 100000] | None = None,
                      price: app_commands.Range[str, 1, 50] | None = None, note: app_commands.Range[str, 1, 500] | None = None,
                      channel: discord.TextChannel | None = None, ping: discord.Role | None = None):
        channel = channel or interaction.channel
        embed = make_embed(f"🔄 Restocked: {product}", note.replace("\\n", "\n") if note else None, discord.Color.green(), timestamp=discord.utils.utcnow())
        if quantity:
            embed.add_field(name="In stock", value=str(quantity))
        if price:
            embed.add_field(name="Price", value=price)
        view = discord.ui.View(timeout=None)
        buy = await self.bot.db.get_config(interaction.guild.id, "welcome_buylink")
        if buy:
            view.add_item(discord.ui.Button(label="Buy Now", emoji="🛒", style=discord.ButtonStyle.link, url=buy))
        await channel.send(content=ping.mention if ping else None, embed=embed, view=view, allowed_mentions=discord.AllowedMentions(roles=[ping]) if ping else NO_MENTIONS)
        await interaction.response.send_message(f"Restock posted in {channel.mention}.", ephemeral=True)

    # ---------- vouch ----------
    @app_commands.command(description="Leave a vouch / feedback for your purchase")
    async def vouch(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VouchModal(self))

    @app_commands.command(description="Set the channel where vouches are posted")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setvouchchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "vouch_channel", channel.id)
        await interaction.response.send_message(f"Vouches will be posted in {channel.mention}.", ephemeral=True)

    async def post_vouch(self, interaction: discord.Interaction, product: str, rating_text: str, feedback: str):
        guild, user = interaction.guild, interaction.user
        if rating_text not in ("1", "2", "3", "4", "5"):
            return await interaction.response.send_message("The rating must be a number from 1 to 5. Try `/vouch` again.", ephemeral=True)
        rating = int(rating_text)
        last = await self.bot.db.fetchone("SELECT created_at FROM vouches WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (guild.id, user.id))
        if last and time.time() - last["created_at"] < 600:
            return await interaction.response.send_message("You vouched a moment ago. Please wait a few minutes before leaving another.", ephemeral=True)
        cid = await self.bot.db.get_config(guild.id, "vouch_channel")
        channel = guild.get_channel(int(cid)) if cid else interaction.channel
        vid = await self.bot.db.execute(
            "INSERT INTO vouches(guild_id, user_id, product, rating, feedback, created_at) VALUES(?,?,?,?,?,?)",
            (guild.id, user.id, product, rating, feedback, time.time()),
        )
        embed = make_embed("New Vouch", feedback, discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.add_field(name="Rating", value=f"{'⭐' * rating} ({rating}/5)")
        embed.add_field(name="Product", value=product)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)  # server emblem, top right
        embed.set_footer(text=f"Vouch #{vid}")
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Thanks for vouching! It was posted in {channel.mention}.", ephemeral=True)

    # ---------- legit vote ----------
    def legit_embed(self, guild: discord.Guild, yes: int, no: int) -> discord.Embed:
        embed = make_embed(f"Is {guild.name} legit?", "Vote below. You can change your vote at any time.", discord.Color.blurple())
        embed.add_field(name="✅ Legit", value=f"**{yes}**")
        embed.add_field(name="❌ Not legit", value=f"**{no}**")
        return embed

    @app_commands.command(description="Post a vote embed where members can vote the server is legit")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def legit(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        await channel.send(embed=self.legit_embed(interaction.guild, 0, 0), view=LegitView(self))
        await interaction.response.send_message(f"Legit vote posted in {channel.mention}.", ephemeral=True)

    async def cast_vote(self, interaction: discord.Interaction, vote: int):
        await self.bot.db.execute(
            "INSERT INTO legit_votes(guild_id, message_id, user_id, vote) VALUES(?,?,?,?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET vote=excluded.vote",
            (interaction.guild.id, interaction.message.id, interaction.user.id, vote),
        )
        counts = await self.bot.db.fetchone(
            "SELECT COALESCE(SUM(vote), 0) AS yes, COUNT(*) AS total FROM legit_votes WHERE message_id=?", (interaction.message.id,)
        )
        await interaction.response.edit_message(embed=self.legit_embed(interaction.guild, counts["yes"], counts["total"] - counts["yes"]))

    # ---------- slots ----------
    @staticmethod
    def _slot_owner_perms() -> discord.PermissionOverwrite:
        return discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, add_reactions=True)

    async def _slot_row(self, interaction: discord.Interaction, user_id: int):
        row = await self.bot.db.fetchone("SELECT * FROM slots WHERE guild_id=? AND user_id=?", (interaction.guild.id, user_id))
        if not row:
            await interaction.response.send_message("That member doesn't have a slot.", ephemeral=True)
        return row

    @app_commands.command(description="Create a personal slot channel for a member")
    @app_commands.describe(member="Who the slot is for", name="Channel name (default: their username)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def createslot(self, interaction: discord.Interaction, member: discord.Member, name: app_commands.Range[str, 1, 80] | None = None):
        guild = interaction.guild
        row = await self.bot.db.fetchone("SELECT channel_id FROM slots WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        if row and guild.get_channel(row["channel_id"]):
            return await interaction.response.send_message(f"{member.mention} already has a slot: <#{row['channel_id']}>", ephemeral=True, allowed_mentions=NO_MENTIONS)
        await interaction.response.defer(ephemeral=True)
        cat_name = await self.bot.db.get_config(guild.id, "slot_category", "Slots")
        category = discord.utils.get(guild.categories, name=cat_name) or await guild.create_category(cat_name)
        overwrites = {
            # everyone can read the slot but only its owner can post
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False,
                                                            create_public_threads=False, create_private_threads=False, send_messages_in_threads=False),
            member: self._slot_owner_perms(),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        channel = await guild.create_text_channel(name or f"{member.name}-slot", category=category, overwrites=overwrites, reason=f"Slot for {member} by {interaction.user}")
        await self.bot.db.execute("INSERT OR REPLACE INTO slots(guild_id, user_id, channel_id, revoked) VALUES(?,?,?,0)", (guild.id, member.id, channel.id))
        await channel.send(f"{member.mention} this is your slot. Only you can post here.", allowed_mentions=discord.AllowedMentions(users=[member]))
        await interaction.followup.send(f"Created {channel.mention} for {member.mention}.", allowed_mentions=NO_MENTIONS)

    @app_commands.command(description="Revoke a member's slot channel (removes their access)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def revokeslot(self, interaction: discord.Interaction, member: discord.Member):
        row = await self._slot_row(interaction, member.id)
        if not row:
            return
        channel = interaction.guild.get_channel(row["channel_id"])
        if channel:
            await channel.set_permissions(member, overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=False, attach_files=False, embed_links=False, add_reactions=False))
        await self.bot.db.execute("UPDATE slots SET revoked=1 WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await interaction.response.send_message(f"Revoked {member.mention}'s slot. They can no longer post.", allowed_mentions=NO_MENTIONS)

    @app_commands.command(description="Restore a member's revoked slot access")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unrevokeslot(self, interaction: discord.Interaction, member: discord.Member):
        row = await self._slot_row(interaction, member.id)
        if not row:
            return
        channel = interaction.guild.get_channel(row["channel_id"])
        if channel:
            await channel.set_permissions(member, overwrite=self._slot_owner_perms())
        await self.bot.db.execute("UPDATE slots SET revoked=0 WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await interaction.response.send_message(f"Restored {member.mention}'s slot access.", allowed_mentions=NO_MENTIONS)

    @app_commands.command(description="Permanently delete a member's slot channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def deleteslot(self, interaction: discord.Interaction, member: discord.User):
        row = await self._slot_row(interaction, member.id)
        if not row:
            return
        await interaction.response.send_message(
            f"Permanently delete {member.mention}'s slot channel? This can't be undone.",
            view=ConfirmDeleteSlot(self, interaction.user.id, member, row), ephemeral=True, allowed_mentions=NO_MENTIONS,
        )


async def setup(bot):
    await bot.add_cog(Shop(bot))
