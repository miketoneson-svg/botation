import discord
from discord import app_commands
from discord.ext import commands

from utils import make_embed


@app_commands.guild_only()
class Notes(commands.GroupCog, group_name="note", group_description="Private staff notes about members"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, user_id INTEGER, author_id INTEGER,
                note TEXT, created_at TEXT
            )"""
        )

    @app_commands.command(description="Add a private staff note about a member")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def add(self, interaction: discord.Interaction, member: discord.User, note: app_commands.Range[str, 1, 500]):
        note_id = await self.bot.db.execute(
            "INSERT INTO notes(guild_id, user_id, author_id, note, created_at) VALUES(?,?,?,?,?)",
            (interaction.guild.id, member.id, interaction.user.id, note, discord.utils.utcnow().isoformat()),
        )
        await interaction.response.send_message(f"Added note **#{note_id}** for {member.mention}.", ephemeral=True)

    @app_commands.command(description="View all staff notes for a member")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def view(self, interaction: discord.Interaction, member: discord.User):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM notes WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
            (interaction.guild.id, member.id),
        )
        if not rows:
            return await interaction.response.send_message(f"No notes for {member.mention}.", ephemeral=True)
        lines = [
            f"**#{r['id']}** · <@{r['author_id']}> · {discord.utils.format_dt(discord.utils.parse_time(r['created_at']), 'R')}\n{r['note']}"
            for r in rows
        ]
        await interaction.response.send_message(
            embed=make_embed(f"Notes for {member} (latest 15)", "\n\n".join(lines)[:4000]), ephemeral=True
        )

    @app_commands.command(description="Remove a single note by its number (see /note view)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def remove(self, interaction: discord.Interaction, number: int):
        row = await self.bot.db.fetchone("SELECT id FROM notes WHERE id=? AND guild_id=?", (number, interaction.guild.id))
        if not row:
            return await interaction.response.send_message(f"There's no note #{number} in this server.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM notes WHERE id=?", (number,))
        await interaction.response.send_message(f"Removed note **#{number}**.", ephemeral=True)

    @app_commands.command(description="Clear ALL notes for a member")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def clear(self, interaction: discord.Interaction, member: discord.User):
        await self.bot.db.execute("DELETE FROM notes WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await interaction.response.send_message(f"Cleared all notes for {member.mention}.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Notes(bot))
