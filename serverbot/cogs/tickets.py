import asyncio
import io
import json
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import make_embed

# key: (button label, emoji, style, intro text shown inside the ticket)
TICKET_TYPES = {
    "owner": ("Speak to Owner", "👑", discord.ButtonStyle.primary, "Tell us what you need to speak to the owner about."),
    "support": ("Support", "🛠️", discord.ButtonStyle.primary, "Describe the problem you're having and include any screenshots."),
    "purchase": ("Purchase", "🛒", discord.ButtonStyle.success, "Tell us what you'd like to buy and how you plan to pay."),
    "shop": ("Shop Questions", "❓", discord.ButtonStyle.secondary, "Ask your question about the shop or our products."),
    "middleman": ("Middle Man", "🤝", discord.ButtonStyle.secondary, "Tell us who you're trading with, what is being traded and the amount."),
    "partnership": ("Partnership", "📨", discord.ButtonStyle.secondary, "Share your server or brand link and what you have in mind."),
    "report": ("Report User", "🚨", discord.ButtonStyle.danger, "Include the user's @ or ID, what happened, and any evidence."),
}
USER_PERMS = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
NO_ROLE_PINGS = discord.AllowedMentions(users=True, roles=False, everyone=False)


# ---------------------------------------------------------------- views
class TicketPanel(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        for key, (label, emoji, style, _) in TICKET_TYPES.items():
            button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"ticket:open:{key}")
            button.callback = self._make_callback(key)
            self.add_item(button)

    def _make_callback(self, key):
        async def callback(interaction: discord.Interaction):
            await self.cog.create_ticket(interaction, key)

        return callback


class CloseModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Reason for closing", style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Closing ticket...", ephemeral=True)
        await self.cog.close_ticket(interaction.channel, interaction.user, str(self.reason) or "No reason provided")


class TicketControls(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await self.cog.get_ticket(interaction.channel.id)
        if not row:
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        if interaction.user.id != row["user_id"] and not await self.cog.is_staff(interaction.user):
            return await interaction.response.send_message("Only the ticket owner or staff can close this.", ephemeral=True)
        await interaction.response.send_modal(CloseModal(self.cog))

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.claim_ticket(interaction, from_button=True)


class ConfirmPurge(discord.ui.View):
    def __init__(self, cog, user_id: int):
        super().__init__(timeout=30)
        self.cog, self.user_id = cog, user_id

    @discord.ui.button(label="Yes, delete all tickets", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
        await interaction.response.edit_message(content="Deleting all ticket channels...", view=None)
        count = await self.cog.purge_all(interaction.guild)
        await interaction.followup.send(f"Deleted **{count}** ticket channels.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


# ---------------------------------------------------------------- main cog
class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.open_channels: set[int] = set()

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, channel_id INTEGER, user_id INTEGER, type TEXT,
                claimed_by INTEGER, created_at REAL, last_activity REAL,
                warned INTEGER DEFAULT 0, closed INTEGER DEFAULT 0
            )"""
        )
        rows = await self.bot.db.fetchall("SELECT channel_id FROM tickets WHERE closed=0")
        self.open_channels = {r["channel_id"] for r in rows}
        self.bot.add_view(TicketPanel(self))
        self.bot.add_view(TicketControls(self))
        self.autoclose_loop.start()

    async def cog_unload(self):
        self.autoclose_loop.cancel()

    # ---------- helpers ----------
    async def get_ticket(self, channel_id: int):
        return await self.bot.db.fetchone("SELECT * FROM tickets WHERE channel_id=? AND closed=0", (channel_id,))

    async def support_roles(self, guild) -> list[int]:
        return json.loads(await self.bot.db.get_config(guild.id, "ticket_support_roles", "[]"))

    async def is_staff(self, member: discord.Member) -> bool:
        p = member.guild_permissions
        if p.administrator or p.manage_channels:
            return True
        ids = set(await self.support_roles(member.guild))
        return any(r.id in ids for r in member.roles)

    async def get_category(self, guild) -> discord.CategoryChannel:
        name = await self.bot.db.get_config(guild.id, "ticket_category", "Tickets")
        category = discord.utils.get(guild.categories, name=name)
        if category is None:
            category = await guild.create_category(
                name, overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            )
        return category

    async def build_transcript(self, channel) -> bytes:
        lines = [f"Transcript of #{channel.name} ({channel.id})", ""]
        async for m in channel.history(limit=10000, oldest_first=True):
            text = m.content
            for e in m.embeds:
                if e.title or e.description:
                    text += f" [embed: {e.title or ''} {e.description or ''}]"
            for a in m.attachments:
                text += f" [attachment: {a.url}]"
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {m.author} ({m.author.id}): {text}".rstrip())
        return "\n".join(lines).encode("utf-8")

    # ---------- create / claim / close ----------
    async def create_ticket(self, interaction: discord.Interaction, key: str):
        guild, user = interaction.guild, interaction.user
        label, emoji, _, intro = TICKET_TYPES[key]
        existing = await self.bot.db.fetchone(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND type=? AND closed=0", (guild.id, user.id, key)
        )
        if existing:
            channel = guild.get_channel(existing["channel_id"])
            if channel:
                return await interaction.response.send_message(f"You already have an open {label} ticket: {channel.mention}", ephemeral=True)
            await self.bot.db.execute("UPDATE tickets SET closed=1 WHERE channel_id=?", (existing["channel_id"],))

        await interaction.response.defer(ephemeral=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(manage_channels=True, **USER_PERMS),
            user: discord.PermissionOverwrite(**USER_PERMS),
        }
        for rid in await self.support_roles(guild):
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(**USER_PERMS)

        now = time.time()
        ticket_id = await self.bot.db.execute(
            "INSERT INTO tickets(guild_id, channel_id, user_id, type, created_at, last_activity) VALUES(?,?,?,?,?,?)",
            (guild.id, 0, user.id, key, now, now),
        )
        try:
            category = await self.get_category(guild)
            channel = await guild.create_text_channel(
                name=f"{key}-{user.name}"[:90], category=category, overwrites=overwrites,
                topic=f"Ticket #{ticket_id} | {label} | opened by {user} ({user.id})", reason=f"Ticket opened by {user}",
            )
        except discord.HTTPException as e:
            await self.bot.db.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
            return await interaction.followup.send(f"I couldn't create the ticket channel: {e}", ephemeral=True)

        await self.bot.db.execute("UPDATE tickets SET channel_id=? WHERE id=?", (channel.id, ticket_id))
        self.open_channels.add(channel.id)
        embed = make_embed(f"{emoji} {label}", f"{intro}\n\nA staff member will be with you shortly.", discord.Color.blurple())
        embed.set_footer(text=f"Ticket #{ticket_id}")
        # Only the opener is pinged: no support/sales role pings on new tickets
        await channel.send(content=user.mention, embed=embed, view=TicketControls(self), allowed_mentions=NO_ROLE_PINGS)
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)

    async def claim_ticket(self, interaction: discord.Interaction, from_button: bool = False):
        row = await self.get_ticket(interaction.channel.id)
        if not row:
            return await interaction.response.send_message("This isn't an open ticket channel.", ephemeral=True)
        if not await self.is_staff(interaction.user):
            return await interaction.response.send_message("Only support staff can claim tickets.", ephemeral=True)
        if row["claimed_by"]:
            return await interaction.response.send_message(f"Already claimed by <@{row['claimed_by']}>.", ephemeral=True)
        await self.bot.db.execute("UPDATE tickets SET claimed_by=? WHERE id=?", (interaction.user.id, row["id"]))
        await interaction.response.send_message(
            embed=make_embed("Ticket claimed", f"{interaction.user.mention} will be handling this ticket.", discord.Color.green())
        )
        if from_button:
            view = TicketControls(self)
            view.claim.disabled = True
            view.claim.label = "Claimed"
            try:
                await interaction.message.edit(view=view)
            except discord.HTTPException:
                pass

    async def close_ticket(self, channel, closer, reason: str) -> bool:
        row = await self.get_ticket(channel.id)
        if not row:
            return False
        await self.bot.db.execute("UPDATE tickets SET closed=1 WHERE id=?", (row["id"],))
        self.open_channels.discard(channel.id)
        guild = channel.guild
        data = await self.build_transcript(channel)
        filename = f"transcript-{row['id']}.txt"
        label = TICKET_TYPES.get(row["type"], (row["type"],))[0]

        embed = make_embed(f"Ticket #{row['id']} closed", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Server", value=guild.name)
        embed.add_field(name="Type", value=label)
        embed.add_field(name="Closed by", value=str(closer))
        embed.add_field(name="Reason", value=reason[:1000], inline=False)

        log_id = await self.bot.db.get_config(guild.id, "ticket_log_channel")
        log_channel = guild.get_channel(int(log_id)) if log_id else None
        if log_channel:
            log_embed = embed.copy()
            log_embed.add_field(name="Opened by", value=f"<@{row['user_id']}>")
            try:
                await log_channel.send(embed=log_embed, file=discord.File(io.BytesIO(data), filename))
            except discord.HTTPException:
                pass

        # Copy of the close reason + transcript goes to the person who opened the ticket
        try:
            opener = guild.get_member(row["user_id"]) or await self.bot.fetch_user(row["user_id"])
            await opener.send(embed=embed, file=discord.File(io.BytesIO(data), filename))
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            await channel.send("This ticket will be deleted in 5 seconds...")
            await asyncio.sleep(5)
            await channel.delete(reason=f"Ticket closed by {closer}: {reason}"[:500])
        except discord.HTTPException:
            pass
        return True

    async def purge_all(self, guild) -> int:
        rows = await self.bot.db.fetchall("SELECT channel_id FROM tickets WHERE guild_id=? AND closed=0", (guild.id,))
        count = 0
        for r in rows:
            self.open_channels.discard(r["channel_id"])
            channel = guild.get_channel(r["channel_id"])
            if channel:
                try:
                    await channel.delete(reason="Ticket purge")
                    count += 1
                except discord.HTTPException:
                    pass
        await self.bot.db.execute("UPDATE tickets SET closed=1 WHERE guild_id=? AND closed=0", (guild.id,))
        return count

    # ---------- listeners / auto-close ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id not in self.open_channels:
            return
        await self.bot.db.execute(
            "UPDATE tickets SET last_activity=?, warned=0 WHERE channel_id=? AND closed=0", (time.time(), message.channel.id)
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if channel.id in self.open_channels:
            self.open_channels.discard(channel.id)
            await self.bot.db.execute("UPDATE tickets SET closed=1 WHERE channel_id=?", (channel.id,))

    @tasks.loop(minutes=5)
    async def autoclose_loop(self):
        get = self.bot.db.get_config
        for r in await self.bot.db.fetchall("SELECT * FROM tickets WHERE closed=0"):
            try:
                guild = self.bot.get_guild(r["guild_id"])
                if not guild or await get(guild.id, "ticket_autoclose", "0") != "1":
                    continue
                channel = guild.get_channel(r["channel_id"])
                if channel is None:
                    self.open_channels.discard(r["channel_id"])
                    await self.bot.db.execute("UPDATE tickets SET closed=1 WHERE id=?", (r["id"],))
                    continue
                close_h = float(await get(guild.id, "ticket_close_hours", "48"))
                warn_h = float(await get(guild.id, "ticket_warn_hours", "24"))
                idle = (time.time() - r["last_activity"]) / 3600
                if idle >= close_h:
                    await self.close_ticket(channel, self.bot.user, "Closed automatically due to inactivity")
                elif idle >= warn_h and not r["warned"]:
                    await self.bot.db.execute("UPDATE tickets SET warned=1 WHERE id=?", (r["id"],))
                    await channel.send(
                        f"<@{r['user_id']}>",
                        embed=make_embed("Still there?", f"This ticket will close automatically after {close_h:g}h of inactivity. Send a message to keep it open.", discord.Color.orange()),
                        allowed_mentions=NO_ROLE_PINGS,
                    )
            except Exception:
                continue

    @autoclose_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------- commands ----------
    @app_commands.command(description="Set up the ticket panel in a channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setuptickets(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           title: str = "Support Tickets", description: str | None = None):
        description = description or "Need help? Pick the option that fits below and a private ticket channel will be created for you."
        await channel.send(embed=make_embed(title, description, discord.Color.blurple()), view=TicketPanel(self))
        await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Claim the current ticket")
    async def claim(self, interaction: discord.Interaction):
        await self.claim_ticket(interaction)

    @app_commands.command(description="Set the category name for ticket channels")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticketcategory(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100]):
        await self.bot.db.set_config(interaction.guild.id, "ticket_category", name)
        await interaction.response.send_message(f"New tickets will be created under the **{name}** category (made automatically if missing).", ephemeral=True)

    @app_commands.command(description="Set the channel where ticket transcripts are logged when closed")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setticketlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "ticket_log_channel", channel.id)
        await interaction.response.send_message(f"Ticket transcripts will be logged in {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Set the support role for tickets")
    @app_commands.describe(role="A role that can see and handle tickets", action="Add (default) or remove the role")
    @app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove")])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticketsupport(self, interaction: discord.Interaction, role: discord.Role, action: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        roles = await self.support_roles(guild)
        adding = action is None or action.value == "add"
        if adding and role.id not in roles:
            roles.append(role.id)
        elif not adding and role.id in roles:
            roles.remove(role.id)
        await self.bot.db.set_config(guild.id, "ticket_support_roles", json.dumps(roles))
        # apply to tickets that are already open
        for cid in list(self.open_channels):
            channel = guild.get_channel(cid)
            if channel:
                try:
                    await channel.set_permissions(role, overwrite=discord.PermissionOverwrite(**USER_PERMS) if adding else None)
                except discord.HTTPException:
                    pass
        current = ", ".join(f"<@&{r}>" for r in roles) or "none"
        await interaction.followup.send(f"{'Added' if adding else 'Removed'} {role.mention}. Support roles: {current}", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="List all open tickets")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticketlist(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM tickets WHERE guild_id=? AND closed=0 ORDER BY id", (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("No open tickets.", ephemeral=True)
        lines = []
        for r in rows[:25]:
            ch = interaction.guild.get_channel(r["channel_id"])
            claimed = f" · claimed by <@{r['claimed_by']}>" if r["claimed_by"] else ""
            lines.append(f"**#{r['id']}** {ch.mention if ch else '(deleted)'} · {TICKET_TYPES.get(r['type'], (r['type'],))[0]} · <@{r['user_id']}> · <t:{int(r['created_at'])}:R>{claimed}")
        if len(rows) > 25:
            lines.append(f"...and {len(rows) - 25} more")
        await interaction.response.send_message(embed=make_embed(f"Open tickets ({len(rows)})", "\n".join(lines)), ephemeral=True)

    @app_commands.command(description="Delete ALL ticket channels (Owner only)")
    async def ticketpurge(self, interaction: discord.Interaction):
        if interaction.user.id != interaction.guild.owner_id and not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("Only the server owner can do this.", ephemeral=True)
        await interaction.response.send_message(
            "This deletes **every open ticket channel** without saving transcripts. Continue?",
            view=ConfirmPurge(self, interaction.user.id), ephemeral=True,
        )


# ---------------------------------------------------------------- auto-close settings group
class TicketAutoClose(commands.GroupCog, group_name="ticketautoclose", group_description="Auto-close inactive tickets"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def _hours(self, guild_id):
        get = self.bot.db.get_config
        return float(await get(guild_id, "ticket_warn_hours", "24")), float(await get(guild_id, "ticket_close_hours", "48"))

    @app_commands.command(description="Enable auto-closing of inactive tickets")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable(self, interaction: discord.Interaction):
        await self.bot.db.set_config(interaction.guild.id, "ticket_autoclose", "1")
        await interaction.response.send_message("Ticket auto-close is **enabled**.", ephemeral=True)

    @app_commands.command(description="Disable auto-closing of inactive tickets")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction):
        await self.bot.db.set_config(interaction.guild.id, "ticket_autoclose", "0")
        await interaction.response.send_message("Ticket auto-close is **disabled**.", ephemeral=True)

    @app_commands.command(description="Set how many hours of inactivity before a ticket auto-closes")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setclose(self, interaction: discord.Interaction, hours: app_commands.Range[int, 1, 720]):
        warn, _ = await self._hours(interaction.guild.id)
        if hours <= warn:
            return await interaction.response.send_message(f"That must be more than the warning time ({warn:g}h).", ephemeral=True)
        await self.bot.db.set_config(interaction.guild.id, "ticket_close_hours", hours)
        await interaction.response.send_message(f"Tickets auto-close after **{hours}h** of inactivity.", ephemeral=True)

    @app_commands.command(description="Set how many hours of inactivity before a warning is sent")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwarn(self, interaction: discord.Interaction, hours: app_commands.Range[int, 1, 719]):
        _, close = await self._hours(interaction.guild.id)
        if hours >= close:
            return await interaction.response.send_message(f"That must be less than the close time ({close:g}h).", ephemeral=True)
        await self.bot.db.set_config(interaction.guild.id, "ticket_warn_hours", hours)
        await interaction.response.send_message(f"Warning sent after **{hours}h** of inactivity.", ephemeral=True)

    @app_commands.command(description="View current auto-close settings")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(self, interaction: discord.Interaction):
        on = await self.bot.db.get_config(interaction.guild.id, "ticket_autoclose", "0") == "1"
        warn, close = await self._hours(interaction.guild.id)
        embed = make_embed("Ticket auto-close")
        embed.add_field(name="Status", value="ON" if on else "OFF")
        embed.add_field(name="Warn after", value=f"{warn:g}h")
        embed.add_field(name="Close after", value=f"{close:g}h")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
    await bot.add_cog(TicketAutoClose(bot))
