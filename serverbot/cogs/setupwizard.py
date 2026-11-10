import discord
from discord import app_commands
from discord.ext import commands

from utils import make_embed

# config key -> (label, kind)
FEATURES = {
    "log_channel": ("Mod and automod log channel", "channel"),
    "welcome_channel": ("Welcome message channel", "channel"),
    "leave_channel": ("Leave message channel", "channel"),
    "level_channel": ("Level-up message channel", "channel"),
    "ticket_log_channel": ("Ticket transcript channel", "channel"),
    "vouch_channel": ("Vouch channel", "channel"),
    "suggest_channel": ("Suggestions channel", "channel"),
    "auto_role": ("Auto role for new members", "role"),
    "admin_role": ("Admin role (for reference)", "role"),
}


class SetupView(discord.ui.View):
    """Main menu: pick what you want to set up."""

    def __init__(self, cog, owner_id: int):
        super().__init__(timeout=300)
        self.cog, self.owner_id = cog, owner_id
        self.select = discord.ui.Select(
            placeholder="Pick something to set up",
            options=[discord.SelectOption(label=label, value=key) for key, (label, _) in FEATURES.items()],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This setup menu belongs to someone else. Run `/setup` yourself.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        key = self.select.values[0]
        label, _ = FEATURES[key]
        await interaction.response.edit_message(
            embed=make_embed(f"Set: {label}", "Choose below, or go back.", discord.Color.blurple()),
            view=PickView(self.cog, self.owner_id, key),
        )


class PickView(discord.ui.View):
    """Second step: pick the channel or role for the chosen feature."""

    def __init__(self, cog, owner_id: int, key: str):
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.key = cog, owner_id, key
        label, kind = FEATURES[key]
        if kind == "channel":
            self.picker = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text, discord.ChannelType.news], placeholder=f"Choose {label.lower()}")
        else:
            self.picker = discord.ui.RoleSelect(placeholder=f"Choose {label.lower()}")
        self.picker.callback = self.on_pick
        self.add_item(self.picker)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This setup menu belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_pick(self, interaction: discord.Interaction):
        picked = self.picker.values[0]
        await self.cog.bot.db.set_config(interaction.guild.id, self.key, picked.id)
        await interaction.response.edit_message(embed=await self.cog.status_embed(interaction.guild), view=SetupView(self.cog, self.owner_id))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=await self.cog.status_embed(interaction.guild), view=SetupView(self.cog, self.owner_id))


class SetupWizard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def status_embed(self, guild: discord.Guild) -> discord.Embed:
        lines = []
        for key, (label, kind) in FEATURES.items():
            value = await self.bot.db.get_config(guild.id, key)
            if value:
                lines.append(f"✅ **{label}**: {'<#' if kind == 'channel' else '<@&'}{value}>")
            else:
                lines.append(f"⬜ **{label}**: not set")
        embed = make_embed("Setup wizard", "\n".join(lines), discord.Color.blurple())
        embed.set_footer(text="Pick an item below to set it. Other features have their own commands: /setuptickets, /invitepanel, /setwelcome, /rolepanel...")
        return embed

    @app_commands.command(description="Interactive setup wizard for the bot's main features")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await self.status_embed(interaction.guild), view=SetupView(self, interaction.user.id), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(SetupWizard(bot))
