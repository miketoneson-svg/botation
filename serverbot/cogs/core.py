import platform

import discord
from discord import app_commands
from discord.ext import commands

from utils import Confirm, make_embed, owner_only


def _chunk(items: list[str], limit: int = 1000) -> list[str]:
    chunks, current = [], ""
    for item in items:
        if len(current) + len(item) + 2 > limit:
            chunks.append(current)
            current = ""
        current += item + "  "
    if current:
        chunks.append(current)
    return chunks


class Core(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="View all available commands")
    async def help(self, interaction: discord.Interaction):
        groups: dict[str, list[str]] = {}
        for cmd in self.bot.tree.walk_commands():
            if isinstance(cmd, app_commands.Group):
                continue
            root = cmd.root_parent.name if cmd.parent else "general"
            groups.setdefault(root, []).append(f"`/{cmd.qualified_name}`")

        embed = make_embed("Commands", "Here's everything I can do.")
        order = sorted(groups, key=lambda g: (g == "general", g))
        for name in order:
            for i, chunk in enumerate(_chunk(sorted(groups[name]))):
                if len(embed.fields) >= 25:
                    break
                embed.add_field(name=name.title() if i == 0 else "\u200b", value=chunk, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="View information about the bot")
    async def botinfo(self, interaction: discord.Interaction):
        embed = make_embed(self.bot.user.name, color=discord.Color.blurple())
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="Servers", value=len(self.bot.guilds))
        embed.add_field(name="Users", value=sum(g.member_count or 0 for g in self.bot.guilds))
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms")
        embed.add_field(name="Online since", value=discord.utils.format_dt(self.bot.start_time, "R"))
        embed.add_field(name="Python", value=platform.python_version())
        embed.add_field(name="discord.py", value=discord.__version__)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave-server", description="Make the bot leave this server (owner only)")
    @owner_only()
    async def leave_server(self, interaction: discord.Interaction):
        guild = interaction.guild

        async def action(i: discord.Interaction):
            await i.followup.send(f"Leaving **{guild.name}**. Goodbye.", ephemeral=True)
            await guild.leave()

        await interaction.response.send_message(
            "Are you sure? I'll leave this server and will need a new invite to come back. "
            "My saved settings for this server stay in the database.",
            view=Confirm(interaction.user.id, action, "Leave server"), ephemeral=True,
        )

    @app_commands.command(name="list-servers", description="List every server the bot is in (owner only)")
    @owner_only()
    async def list_servers(self, interaction: discord.Interaction):
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        lines = [f"**{g.name}** - `{g.id}` - {g.member_count or 0} members" for g in guilds]
        text, shown = "", 0
        for line in lines:
            if len(text) + len(line) + 1 > 3900:
                break
            text += line + "\n"
            shown += 1
        if shown < len(lines):
            text += f"...and {len(lines) - shown} more"
        embed = make_embed(f"Servers ({len(guilds)})", text or "I'm not in any servers.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.guild_only()
class Config(commands.GroupCog, group_name="config", group_description="Bot configuration"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="admin-role", description="Set the role treated as your admin role for reference (informational)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_role(self, interaction: discord.Interaction, role: discord.Role):
        await self.bot.db.set_config(interaction.guild.id, "admin_role", role.id)
        await interaction.response.send_message(f"Admin role set to {role.mention}.", ephemeral=True)

    @app_commands.command(name="log-channel", description="Set the channel for moderation and automod logs")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "log_channel", channel.id)
        await interaction.response.send_message(f"Logs will be sent to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="bot-avatar", description="Change the bot's avatar (owner only)")
    @owner_only()
    async def set_avatar(self, interaction: discord.Interaction, image: discord.Attachment):
        if not (image.content_type or "").startswith("image/"):
            return await interaction.response.send_message("That file isn't an image.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.user.edit(avatar=await image.read())
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that: {e}", ephemeral=True)
        await interaction.followup.send("Avatar updated.", ephemeral=True)

    @app_commands.command(name="bot-name", description="Change the bot's display username (owner only)")
    @owner_only()
    async def set_username(self, interaction: discord.Interaction, name: app_commands.Range[str, 2, 32]):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.user.edit(username=name)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that (name changes are rate limited): {e}", ephemeral=True)
        await interaction.followup.send(f"Bot renamed to **{name}**.", ephemeral=True)

    @app_commands.command(name="view", description="View this server's current bot configuration")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def view(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT key, value FROM guild_config WHERE guild_id=? AND key NOT LIKE '\\_%' ESCAPE '\\' ORDER BY key",
            (interaction.guild.id,),
        )
        if not rows:
            return await interaction.response.send_message("Nothing configured yet.", ephemeral=True)
        lines = []
        for r in rows:
            key, value = r["key"], r["value"]
            if key.endswith("channel") and value.isdigit():
                value = f"<#{value}>"
            elif key.endswith("role") and value.isdigit():
                value = f"<@&{value}>"
            lines.append(f"**{key}**: {value}")
        await interaction.response.send_message(
            embed=make_embed("Server configuration", "\n".join(lines)[:4000]), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Core(bot))
    await bot.add_cog(Config(bot))
