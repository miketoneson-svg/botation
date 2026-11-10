import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from utils import DATA_DIR, age_text, fill, make_embed

IMAGE_DIR = DATA_DIR / "welcome_images"
PLACEHOLDERS = "`{user}` `{username}` `{server}` `{count}` `{inviter}` `{account_age}`"
DEFAULTS = {
    "welcome": ("Welcome to {server}!", "Hey {user}, glad you're here! You're member **#{count}**."),
    "leave": ("{username} left", "**{username}** has left {server}. We're now at **{count}** members."),
}


class MessageModal(discord.ui.Modal):
    def __init__(self, cog, kind: str, title: str, description: str):
        super().__init__(title=f"Customize {kind} message")
        self.cog, self.kind = cog, kind
        self.title_input = discord.ui.TextInput(label="Embed title", default=title, max_length=256)
        self.desc_input = discord.ui.TextInput(label="Embed description", style=discord.TextStyle.paragraph, default=description, max_length=2000)
        self.add_item(self.title_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        db = self.cog.bot.db
        await db.set_config(interaction.guild.id, f"{self.kind}_title", str(self.title_input))
        await db.set_config(interaction.guild.id, f"{self.kind}_description", str(self.desc_input))
        await interaction.response.send_message(
            f"Saved the {self.kind} message. Placeholders: {PLACEHOLDERS}. Use `/test{self.kind}` to preview it." if self.kind == "welcome"
            else f"Saved the {self.kind} message. Placeholders: {PLACEHOLDERS}.",
            ephemeral=True,
        )


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cfg(self, guild_id: int, key: str, default=None):
        return await self.bot.db.get_config(guild_id, key, default)

    # ---------- building the welcome message ----------
    async def build_welcome(self, member: discord.Member, inviter_id: int | None):
        g = member.guild
        title_t = await self.cfg(g.id, "welcome_title", DEFAULTS["welcome"][0])
        desc_t = await self.cfg(g.id, "welcome_description", DEFAULTS["welcome"][1])
        values = dict(
            user=member.mention, username=member.display_name, server=g.name, count=g.member_count,
            inviter=f"<@{inviter_id}>" if inviter_id else "Unknown", account_age=age_text(member.created_at),
        )
        embed = make_embed(fill(title_t, **values), fill(desc_t, **values), discord.Color.green())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="On Discord for", value=f"{age_text(member.created_at)}\n(created {discord.utils.format_dt(member.created_at, 'D')})")
        embed.add_field(name="Invited by", value=values["inviter"])

        files = []
        image = await self.cfg(g.id, "welcome_image")
        if image and image.startswith("file:") and os.path.exists(image[5:]):
            name = "welcome" + Path(image[5:]).suffix
            files.append(discord.File(image[5:], filename=name))
            embed.set_image(url=f"attachment://{name}")
        elif image and image.startswith("http"):
            embed.set_image(url=image)

        view = discord.ui.View(timeout=None)
        buy = await self.cfg(g.id, "welcome_buylink")
        if buy:
            view.add_item(discord.ui.Button(label="Buy Now", emoji="🛒", style=discord.ButtonStyle.link, url=buy))
        rules = await self.cfg(g.id, "welcome_rules_channel")
        if rules:
            view.add_item(discord.ui.Button(label="Rules", emoji="📜", style=discord.ButtonStyle.link, url=f"https://discord.com/channels/{g.id}/{rules}"))
        return embed, files, view

    # ---------- listeners ----------
    @commands.Cog.listener()
    async def on_tracked_join(self, member: discord.Member, inviter_id, code):
        channel_id = await self.cfg(member.guild.id, "welcome_channel")
        channel = member.guild.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            return
        embed, files, view = await self.build_welcome(member, inviter_id)
        try:
            await channel.send(content=member.mention, embed=embed, files=files, view=view)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        g = member.guild
        channel_id = await self.cfg(g.id, "leave_channel")
        channel = g.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            return
        title_t = await self.cfg(g.id, "leave_title", DEFAULTS["leave"][0])
        desc_t = await self.cfg(g.id, "leave_description", DEFAULTS["leave"][1])
        values = dict(user=str(member), username=member.display_name, server=g.name, count=g.member_count,
                      inviter="", account_age=age_text(member.created_at))
        embed = make_embed(fill(title_t, **values), fill(desc_t, **values), discord.Color.red())
        embed.set_thumbnail(url=member.display_avatar.url)
        if member.joined_at:
            embed.add_field(name="Was here for", value=age_text(member.joined_at))
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ---------- commands ----------
    @app_commands.command(description="Customize the welcome embed message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwelcome(self, interaction: discord.Interaction):
        t = await self.cfg(interaction.guild.id, "welcome_title", DEFAULTS["welcome"][0])
        d = await self.cfg(interaction.guild.id, "welcome_description", DEFAULTS["welcome"][1])
        await interaction.response.send_modal(MessageModal(self, "welcome", t, d))

    @app_commands.command(description="Customize the leave embed message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setleave(self, interaction: discord.Interaction):
        t = await self.cfg(interaction.guild.id, "leave_title", DEFAULTS["leave"][0])
        d = await self.cfg(interaction.guild.id, "leave_description", DEFAULTS["leave"][1])
        await interaction.response.send_modal(MessageModal(self, "leave", t, d))

    @app_commands.command(description="Set the welcome message channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwelchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "welcome_channel", channel.id)
        await interaction.response.send_message(f"Welcome messages will go to {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Set the leave message channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setleavechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "leave_channel", channel.id)
        await interaction.response.send_message(f"Leave messages will go to {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Set which channel the Rules button on the welcome message links to")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwelcomerules(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "welcome_rules_channel", channel.id)
        await interaction.response.send_message(f"The Rules button now links to {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Set the URL the Buy Now button on the welcome message opens")
    @app_commands.describe(url="Leave empty to remove the button")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwelcomebuylink(self, interaction: discord.Interaction, url: str | None = None):
        if not url:
            await self.bot.db.del_config(interaction.guild.id, "welcome_buylink")
            return await interaction.response.send_message("Buy Now button removed.", ephemeral=True)
        if not url.startswith(("http://", "https://")):
            return await interaction.response.send_message("The link must start with http:// or https://", ephemeral=True)
        await self.bot.db.set_config(interaction.guild.id, "welcome_buylink", url)
        await interaction.response.send_message("Buy Now link saved.", ephemeral=True)

    @app_commands.command(description="Set the banner image shown on the welcome embed")
    @app_commands.describe(image="Upload an image", url="...or give a direct image link (leave both empty to remove)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setwelcomeimage(self, interaction: discord.Interaction, image: discord.Attachment | None = None, url: str | None = None):
        gid = interaction.guild.id
        if image:
            if not (image.content_type or "").startswith("image/"):
                return await interaction.response.send_message("That file isn't an image.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            path = IMAGE_DIR / f"{gid}{Path(image.filename).suffix.lower() or '.png'}"
            path.write_bytes(await image.read())  # saved locally because Discord attachment links expire
            await self.bot.db.set_config(gid, "welcome_image", f"file:{path}")
            return await interaction.followup.send("Welcome banner saved.", ephemeral=True)
        if url:
            if not url.startswith(("http://", "https://")):
                return await interaction.response.send_message("The link must start with http:// or https://", ephemeral=True)
            await self.bot.db.set_config(gid, "welcome_image", url)
            return await interaction.response.send_message("Welcome banner saved.", ephemeral=True)
        await self.bot.db.del_config(gid, "welcome_image")
        await interaction.response.send_message("Welcome banner removed.", ephemeral=True)

    @app_commands.command(description="Preview the welcome message as it would appear for you")
    async def testwelcome(self, interaction: discord.Interaction):
        row = await self.bot.db.fetchone(
            "SELECT inviter_id FROM invite_joins WHERE guild_id=? AND member_id=?", (interaction.guild.id, interaction.user.id)
        )
        embed, files, view = await self.build_welcome(interaction.user, row["inviter_id"] if row else None)
        await interaction.response.send_message(content=interaction.user.mention, embed=embed, files=files, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
