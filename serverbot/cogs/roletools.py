import asyncio
import hashlib
import re
import unicodedata

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils import Confirm, make_embed

NO_MENTIONS = discord.AllowedMentions.none()
# Roles with any of these can never be handed out by panels / bulk tools, so a misconfigured panel can't hand out admin
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "ban_members",
             "kick_members", "moderate_members", "manage_messages", "mention_everyone")
EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d+)>")
FIXCOLOR_RE = re.compile(r"^(?P<name>.*?)\s*\|\s*#?(?P<hex>[0-9a-fA-F]{6})\s*$")


def normalize_name(name: str) -> str:
    """'Jäger' -> 'jager', 'Pulse ' -> 'pulse', so role names line up with emoji names."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9_]", "_", plain.lower()).strip("_")


def result_embed(title: str, lines: list[str], footer: str, ok: bool) -> discord.Embed:
    shown = lines[:40]
    text = "\n".join(shown) + (f"\n...and {len(lines) - len(shown)} more" if len(lines) > len(shown) else "")
    embed = make_embed(title, text[:4000] or None, discord.Color.green() if ok else discord.Color.orange())
    embed.set_footer(text=footer)
    return embed


def is_dangerous(role: discord.Role) -> bool:
    return any(getattr(role.permissions, name) for name in DANGEROUS)


def role_problem(guild: discord.Guild, actor: discord.Member | None, role: discord.Role) -> str | None:
    if role.is_default() or role.managed:
        return "it's @everyone or managed by an integration"
    if role >= guild.me.top_role:
        return "it's higher than my top role"
    if actor and actor.id != guild.owner_id and role >= actor.top_role:
        return "it's higher than your top role"
    return None


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolepanel:(?P<id>[0-9]+)"):
    """Persistent role-panel button. The role id is stored in the custom_id, so panels survive restarts."""

    def __init__(self, role_id: int, label: str = "Role"):
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"rolepanel:{role_id}"))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["id"]), item.label or "Role")

    async def callback(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        role = guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("That role no longer exists.", ephemeral=True)
        if is_dangerous(role) or role_problem(guild, None, role):
            return await interaction.response.send_message("I can't hand out that role right now.", ephemeral=True)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role panel")
                text = f"Removed **{role.name}**."
            else:
                await member.add_roles(role, reason="Role panel")
                text = f"You now have **{role.name}**."
        except discord.HTTPException:
            text = "I couldn't change your roles. Ask an admin to check my permissions."
        await interaction.response.send_message(text, ephemeral=True)


class NamesModal(discord.ui.Modal):
    """Generic 'paste a list' popup used by the bulk tools."""

    def __init__(self, title: str, label: str, handler, placeholder: str = ""):
        super().__init__(title=title)
        self.handler = handler
        self.field = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph, max_length=4000, placeholder=placeholder[:100])
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        await self.handler(interaction, str(self.field))


class RoleTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(RoleButton)

    # ---------- role panel / autorole / onlinerole ----------
    @app_commands.command(description="Post a role panel where members click buttons to get roles")
    @app_commands.describe(title="Panel title", role1="First role", description="Text under the title", channel="Where to post (default: here)")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rolepanel(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 200], role1: discord.Role,
                        role2: discord.Role | None = None, role3: discord.Role | None = None, role4: discord.Role | None = None,
                        role5: discord.Role | None = None, role6: discord.Role | None = None, role7: discord.Role | None = None,
                        role8: discord.Role | None = None, role9: discord.Role | None = None, role10: discord.Role | None = None,
                        description: app_commands.Range[str, 1, 2000] | None = None, channel: discord.TextChannel | None = None):
        roles = list(dict.fromkeys(r for r in (role1, role2, role3, role4, role5, role6, role7, role8, role9, role10) if r))
        problems = []
        for r in roles:
            if is_dangerous(r):
                problems.append(f"**{r.name}**: it has moderation permissions")
            elif (p := role_problem(interaction.guild, interaction.user, r)):
                problems.append(f"**{r.name}**: {p}")
        if problems:
            return await interaction.response.send_message("I can't put these on a panel:\n" + "\n".join(problems), ephemeral=True)
        channel = channel or interaction.channel
        view = discord.ui.View(timeout=None)
        for r in roles:
            view.add_item(RoleButton(r.id, r.name[:80]))
        text = (description or "Click a button to add or remove a role.").replace("\\n", "\n")
        try:
            await channel.send(embed=make_embed(title, text, discord.Color.blurple()), view=view)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"I couldn't post there: {e}", ephemeral=True)
        await interaction.response.send_message(f"Role panel posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Set a role to be given to new members automatically")
    @app_commands.describe(role="Leave empty to turn autorole off")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def autorole(self, interaction: discord.Interaction, role: discord.Role | None = None):
        if role is None:
            await self.bot.db.del_config(interaction.guild.id, "auto_role")
            return await interaction.response.send_message("Autorole turned off.", ephemeral=True)
        problem = "it has moderation permissions" if is_dangerous(role) else role_problem(interaction.guild, interaction.user, role)
        if problem:
            return await interaction.response.send_message(f"I can't use that role: {problem}.", ephemeral=True)
        await self.bot.db.set_config(interaction.guild.id, "auto_role", role.id)
        await interaction.response.send_message(f"New members will get {role.mention}.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        rid = await self.bot.db.get_config(member.guild.id, "auto_role")
        role = member.guild.get_role(int(rid)) if rid else None
        if role and not role_problem(member.guild, None, role) and not is_dangerous(role):
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.HTTPException:
                pass

    @app_commands.command(description="Give (or remove) a role for everyone currently online")
    @app_commands.choices(action=[app_commands.Choice(name="give", value="give"), app_commands.Choice(name="remove", value="remove")])
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def onlinerole(self, interaction: discord.Interaction, role: discord.Role, action: app_commands.Choice[str] | None = None):
        problem = "it has moderation permissions" if is_dangerous(role) else role_problem(interaction.guild, interaction.user, role)
        if problem:
            return await interaction.response.send_message(f"I can't use that role: {problem}.", ephemeral=True)
        giving = action is None or action.value == "give"
        await interaction.response.defer()
        done = 0
        for m in interaction.guild.members:
            if m.bot or m.status == discord.Status.offline or (role in m.roles) == giving:
                continue  # skip bots, offline members, and anyone already in the right state
            try:
                await (m.add_roles(role, reason="onlinerole") if giving else m.remove_roles(role, reason="onlinerole"))
                done += 1
                await asyncio.sleep(0.4)
            except discord.HTTPException:
                continue
        await interaction.followup.send(f"{'Gave' if giving else 'Removed'} {role.mention} {'to' if giving else 'from'} **{done}** online members.", allowed_mentions=NO_MENTIONS)

    # ---------- bulk roles ----------
    @app_commands.command(description="Delete many roles at once by pasting their names")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def bulkroleremove(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NamesModal("Delete roles", "Role names (one per line)", self._bulk_remove_prepare))

    async def _bulk_remove_prepare(self, interaction: discord.Interaction, text: str):
        names = [n.strip() for n in (text.splitlines() if "\n" in text else text.split(",")) if n.strip()]
        by_name = {}
        for r in interaction.guild.roles:
            by_name.setdefault(r.name.lower(), r)
        found, missing, blocked = [], [], []
        for n in dict.fromkeys(names):
            role = by_name.get(n.lower())
            if not role:
                missing.append(n)
            elif role_problem(interaction.guild, interaction.user, role):
                blocked.append(role.name)
            else:
                found.append(role)
        found = found[:100]
        if not found:
            detail = (f"\nNot found: {', '.join(missing)[:500]}" if missing else "") + (f"\nCan't delete: {', '.join(blocked)[:500]}" if blocked else "")
            return await interaction.response.send_message("No deletable roles matched." + detail, ephemeral=True)

        async def action(i: discord.Interaction):
            deleted = 0
            for role in found:
                try:
                    await role.delete(reason=f"bulkroleremove by {interaction.user}")
                    deleted += 1
                    await asyncio.sleep(0.6)
                except discord.HTTPException:
                    continue
            await i.followup.send(f"Deleted **{deleted}** of {len(found)} roles.", ephemeral=True)

        lines = [f"Will delete **{len(found)}** roles:", ", ".join(r.name for r in found)[:1500]]
        if missing:
            lines.append(f"Not found: {', '.join(missing)[:400]}")
        if blocked:
            lines.append(f"Skipped (can't delete): {', '.join(blocked)[:400]}")
        await interaction.response.send_message(
            embed=make_embed("Delete these roles?", "\n".join(lines), discord.Color.red()),
            view=Confirm(interaction.user.id, action, f"Delete {len(found)} roles"), ephemeral=True,
        )

    @app_commands.command(description="Create many roles at once (one per line)")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def createroles(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            NamesModal("Create roles", "One role per line", self._create_roles, "Name  or  Name | #ff0000  to set a color")
        )

    async def _create_roles(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lines = [l.strip() for l in text.splitlines() if l.strip()][:50]
        room = max(250 - len(guild.roles), 0)
        created, failed = [], 0
        for line in lines[:room]:
            name, color = line, discord.Colour.default()
            if "|" in line:
                left, right = line.rsplit("|", 1)
                if re.fullmatch(r"\s*#?[0-9a-fA-F]{6}\s*", right):
                    name, color = left.strip(), discord.Colour(int(right.strip().lstrip("#"), 16))
            try:
                role = await guild.create_role(name=name[:100] or "new role", colour=color, reason=f"createroles by {interaction.user}")
                created.append(role.name)
                await asyncio.sleep(0.6)
            except discord.HTTPException:
                failed += 1
        extra = f" {len(lines) - room} skipped (role limit)." if len(lines) > room else ""
        await interaction.followup.send(f"Created **{len(created)}** roles." + (f" {failed} failed." if failed else "") + extra, ephemeral=True)

    @app_commands.command(description="Fix roles with '| #hex' stuck in their name: rename and apply the colours")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def fixrolecolors(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        fixed = skipped = 0
        for role in list(interaction.guild.roles):
            m = FIXCOLOR_RE.match(role.name)
            if not m:
                continue
            if role_problem(interaction.guild, interaction.user, role) or not m["name"].strip():
                skipped += 1
                continue
            try:
                await role.edit(name=m["name"].strip()[:100], colour=discord.Colour(int(m["hex"], 16)), reason=f"fixrolecolors by {interaction.user}")
                fixed += 1
                await asyncio.sleep(0.6)
            except discord.HTTPException:
                skipped += 1
        await interaction.followup.send(f"Fixed **{fixed}** roles." + (f" Skipped {skipped} (too high or failed)." if skipped else ""), ephemeral=True)

    # ---------- role icons ----------
    @staticmethod
    def _icons_ok(guild: discord.Guild) -> bool:
        return "ROLE_ICONS" in guild.features

    @app_commands.command(description="Give each role the server emoji that has the same name")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def matchroleicons(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not self._icons_ok(guild):
            return await interaction.response.send_message("Role icons need the server to reach **Boost level 2**.", ephemeral=True)
        await interaction.response.defer()
        emojis = {normalize_name(e.name): e for e in guild.emojis}
        lines, done, failed, skipped = [], 0, 0, 0
        for role in reversed(guild.roles):  # highest role first
            emoji = emojis.get(normalize_name(role.name))
            if not emoji or role.is_default():
                continue
            if role_problem(guild, interaction.user, role) or role.display_icon is not None:
                skipped += 1
                continue
            try:
                await role.edit(display_icon=await emoji.read(), reason=f"matchroleicons by {interaction.user}")
                done += 1
                lines.append(f"✅ {emoji} → {role.mention}")
                await asyncio.sleep(1)
            except discord.HTTPException:
                failed += 1
                lines.append(f"❌ {emoji} → {role.mention}")
        footer = f"{done} set • {failed} failed" + (f" • {skipped} skipped" if skipped else "")
        if not lines:
            footer = "Nothing to do" + (f" • {skipped} skipped (already have an icon or too high)" if skipped else "")
        await interaction.edit_original_response(embed=result_embed(f"Role icons: {done} set", lines, footer, failed == 0))

    @app_commands.command(description="Set a role's icon to an emoji")
    @app_commands.describe(emoji="A normal or custom emoji, or 'none' to remove the icon")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def roleicon(self, interaction: discord.Interaction, role: discord.Role, emoji: str):
        if not self._icons_ok(interaction.guild):
            return await interaction.response.send_message("Role icons need the server to reach **Boost level 2**.", ephemeral=True)
        if (problem := role_problem(interaction.guild, interaction.user, role)):
            return await interaction.response.send_message(f"I can't edit that role: {problem}.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        emoji = emoji.strip()
        try:
            if emoji.lower() == "none":
                await role.edit(display_icon=None)
                return await interaction.followup.send(f"Removed the icon from **{role.name}**.", ephemeral=True)
            pe = discord.PartialEmoji.from_str(emoji)
            if pe.id:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"https://cdn.discordapp.com/emojis/{pe.id}.{'gif' if pe.animated else 'png'}") as r:
                        if r.status != 200:
                            return await interaction.followup.send("I couldn't download that emoji.", ephemeral=True)
                        icon = await r.read()
                await role.edit(display_icon=icon)
            else:
                await role.edit(display_icon=emoji)
        except (discord.HTTPException, TypeError, ValueError) as e:
            return await interaction.followup.send(f"Discord rejected that icon: {e}", ephemeral=True)
        await interaction.followup.send(f"Updated the icon for **{role.name}**.", ephemeral=True)

    # ---------- emojis ----------
    @app_commands.command(description="Copy many custom emojis from other servers into this one")
    @app_commands.checks.has_permissions(manage_expressions=True)
    @app_commands.checks.bot_has_permissions(manage_expressions=True)
    async def bulksteal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NamesModal("Bulk steal emojis", "Paste the emojis", self._bulk_steal, "Paste up to 40 custom emojis"))

    async def _bulk_steal(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer()
        found = {m.group(3): (m.group(2), bool(m.group(1))) for m in EMOJI_RE.finditer(text)}
        if not found:
            return await interaction.edit_original_response(content="I couldn't find any custom emojis in that. Paste them as you'd send them in chat.")
        items = list(found.items())[:40]
        added, failed = [], []
        async with aiohttp.ClientSession() as session:
            for idx, (eid, (name, animated)) in enumerate(items):
                try:
                    async with session.get(f"https://cdn.discordapp.com/emojis/{eid}.{'gif' if animated else 'png'}") as r:
                        if r.status != 200:
                            failed.append(name)
                            continue
                        data = await r.read()
                    added.append(await interaction.guild.create_custom_emoji(name=name, image=data, reason=f"bulksteal by {interaction.user}"))
                except discord.HTTPException as e:
                    failed.append(name)
                    if e.code == 30008:  # no emoji slots left, so everything after this would fail too
                        failed.extend(n for _, (n, _a) in items[idx + 1:])
                        break
                await asyncio.sleep(1)
        lines = [f"✅ {e} `{e.name}`" for e in sorted(added, key=lambda e: e.name.lower())] + [f"❌ `{n}`" for n in failed]
        await interaction.edit_original_response(
            embed=result_embed(f"Bulk steal finished: {len(added)} added", lines, f"{len(added)} added • {len(failed)} failed", not failed)
        )

    @app_commands.command(description="Find and delete duplicate emojis in this server")
    @app_commands.checks.has_permissions(manage_expressions=True)
    @app_commands.checks.bot_has_permissions(manage_expressions=True)
    async def dedupeemojis(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        emojis = list(interaction.guild.emojis)
        if not emojis:
            return await interaction.followup.send("This server has no custom emojis.", ephemeral=True)

        async def digest(e: discord.Emoji):
            try:
                return e, hashlib.sha256(await e.read()).hexdigest()
            except discord.HTTPException:
                return e, None

        groups: dict[str, list[discord.Emoji]] = {}
        for i in range(0, len(emojis), 10):
            for e, h in await asyncio.gather(*(digest(e) for e in emojis[i:i + 10])):
                if h:
                    groups.setdefault(h, []).append(e)
        to_delete, lines = [], []
        for group in groups.values():
            if len(group) > 1:
                group.sort(key=lambda e: e.created_at)
                to_delete.extend(group[1:])
                lines.append(f"Keep {group[0]} · delete " + " ".join(f"`:{e.name}:`" for e in group[1:]))
        if not to_delete:
            return await interaction.followup.send("No duplicate emojis found.", ephemeral=True)

        async def action(i: discord.Interaction):
            deleted = 0
            for e in to_delete:
                try:
                    await e.delete(reason=f"dedupeemojis by {interaction.user}")
                    deleted += 1
                    await asyncio.sleep(0.5)
                except discord.HTTPException:
                    continue
            await i.followup.send(f"Deleted **{deleted}** duplicate emojis.", ephemeral=True)

        await interaction.followup.send(
            embed=make_embed(f"{len(to_delete)} duplicate emojis found", "\n".join(lines)[:4000], discord.Color.orange()),
            view=Confirm(interaction.user.id, action, f"Delete {len(to_delete)} duplicates", timeout=120), ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(RoleTools(bot))
