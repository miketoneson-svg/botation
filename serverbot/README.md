# Server Bot

All-in-one Discord server bot (discord.py 2.x + SQLite). 132 slash commands, run `/help` in Discord to see them all.

**Features:** moderation, automod, ping-spam protection, staff notes, tickets (with transcripts and auto-close), invite tracking
and rewards, welcome/leave messages, levels/XP, vouches, legit votes, slot channels, autoresponder, role panels, bulk role and
emoji tools, AFK, polls, suggestions, sticky messages, and a `/setup` wizard.

## 1. Create the bot (Discord Developer Portal)
1. New Application, then **Bot**: copy the token (keep it secret).
2. Turn on all three **Privileged Gateway Intents**: Presence, Server Members, Message Content.
3. **OAuth2 > URL Generator**: scopes `bot` + `applications.commands`, permission **Administrator**. Open the link to invite it.
4. In your server, drag the bot's role to the **top** of the role list (it can only manage roles below its own).

## 2. Deploy on Railway
1. Put this folder in a **private** GitHub repo (`.env` and `bot.db` are already git-ignored).
2. Railway: **New Project > Deploy from GitHub repo** and pick it. `railway.json` already sets the start command.
3. Open the service > **Variables** and add `DISCORD_TOKEN` = your token.
4. **Add a Volume** (important): in the project canvas click **+ New > Volume**, attach it to the bot service, mount path `/data`.
   Railway then sets `RAILWAY_VOLUME_MOUNT_PATH` and the bot stores `bot.db` and uploaded banners there automatically.
   Without a volume, all settings, XP, invites, tickets and vouches are wiped on every deploy (the logs warn you if it's missing).
5. Deploy and open the logs. You should see `Logged in as ...` and `Synced N commands globally`.
6. Updating later: push to GitHub and Railway redeploys. Data stays in the volume.

Notes: this is a worker (no web port or domain needed). Keep it at **one replica** because SQLite is a single-file database.
Slash commands re-sync only when they change, so restarts are cheap. `/utility sync` forces a sync (owner only).

## Run locally instead
`pip install -r requirements.txt`, copy `.env.example` to `.env`, fill in the token (and `DEV_GUILD_ID` for instant command updates), then `python main.py`. Needs Python 3.10+.

## First-time setup in Discord
Run `/setup` for the main channels and roles, then:
- Tickets: `/ticketsupport` (add your staff roles), `/ticketcategory`, `/setuptickets`
- Invites: `/invitepanel`, `/inviterewards set`
- Welcome: `/setwelcome`, `/setwelcomerules`, `/setwelcomebuylink`, `/setwelcomeimage`, then `/testwelcome`
- Levels: `/setlevelmessage`, `/setlevelrole`
- Other: `/rolepanel`, `/rules`, `/legit`, `/autoresponder add`, `/automod enable`

## Behaviour notes
- Staff-only autoresponder triggers only fire for the staff role (or admins) and reply in the staff channel if one is set.
- `/pingspamprotect` protects a member: anyone who pings them 3+ times in 60s gets a 10 minute timeout.
- Role panels, autorole and bulk tools refuse roles with moderation permissions. Destructive tools (`nuke`, `bulkroleremove`,
  `dedupeemojis`, `ticketpurge`, `deleteslot`) ask for confirmation first.
- `/snipe` needs Manage Messages. Role icons need Boost level 2.
- Gift card codes and payment submissions are not part of this bot.
