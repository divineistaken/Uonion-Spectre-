import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import re
import aiohttp
import hashlib

RESULTS_URL = "https://territorial.io/clan-results"
GIST_FILENAME = "winlogger-config.json"
GIST_DESCRIPTION = "WinLogger bot config - do not edit manually"
GIST_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gist_id")

# Gist storage
_gist_id = None
_gist_headers = None


def _get_headers():
    token = os.getenv("GITHUB_PAT")
    if not token:
        return None
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def _save_gist_id():
    if _gist_id:
        try:
            with open(GIST_ID_FILE, "w") as f:
                f.write(_gist_id)
        except Exception:
            pass


def _load_gist_id():
    try:
        with open(GIST_ID_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return None


async def _find_or_create_gist():
    global _gist_id, _gist_headers
    _gist_headers = _get_headers()
    if not _gist_headers:
        print("WARN: No GITHUB_PAT - config will NOT survive redeploys")
        return

    # 1) Try cached gist id from disk
    saved_id = _load_gist_id()
    if saved_id:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.github.com/gists/{saved_id}", headers=_gist_headers) as resp:
                    if resp.status == 200:
                        _gist_id = saved_id
                        print(f"Found gist from cache: {_gist_id}")
                        return
        except Exception as e:
            print(f"Gist id check error: {e}")

    # 2) Search all pages of gists (handles >30 gists)
    page = 1
    try:
        async with aiohttp.ClientSession() as session:
            while True:
                async with session.get(
                    f"https://api.github.com/gists?per_page=100&page={page}",
                    headers=_gist_headers,
                ) as resp:
                    if resp.status != 200:
                        break
                    gists = await resp.json()
                    if not gists:
                        break
                    for g in gists:
                        if GIST_FILENAME in g.get("files", {}):
                            _gist_id = g["id"]
                            _save_gist_id()
                            print(f"Found existing gist: {_gist_id}")
                            return
                    if len(gists) < 100:
                        break
                    page += 1
    except Exception as e:
        print(f"Gist search error: {e}")

    # 3) Create new gist
    try:
        payload = {
            "description": GIST_DESCRIPTION,
            "public": False,
            "files": {GIST_FILENAME: {"content": json.dumps({"guilds": {}}, indent=2)}},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.github.com/gists", headers=_gist_headers, json=payload) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    _gist_id = data["id"]
                    _save_gist_id()
                    print(f"Created new gist: {_gist_id}")
                else:
                    print(f"Gist create failed: {resp.status}")
    except Exception as e:
        print(f"Gist create error: {e}")


async def _load_from_gist():
    if not _gist_id or not _gist_headers:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.github.com/gists/{_gist_id}"
            async with session.get(url, headers=_gist_headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data["files"][GIST_FILENAME]["content"]
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "guilds" in parsed:
                        return parsed
    except Exception as e:
        print(f"Gist load error: {e}")
    return None


async def _save_to_gist(data):
    if not _gist_id or not _gist_headers:
        return
    try:
        payload = {"files": {GIST_FILENAME: {"content": json.dumps(data, indent=2)}}}
        async with aiohttp.ClientSession() as session:
            url = f"https://api.github.com/gists/{_gist_id}"
            async with session.patch(url, headers=_gist_headers, json=payload) as resp:
                if resp.status != 200:
                    print(f"Gist save failed: {resp.status}")
    except Exception as e:
        print(f"Gist save error: {e}")


# --- Config ---
bot_config = {"guilds": {}}
seen_wins = set()

runtime_stats = {
    "last_poll": None,
    "last_entries": 0,
    "last_match": None,
    "last_send_error": None,
    "last_fetch_error": None,
    "last_fetch_source": None,
    "poll_count": 0,
}

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


def get_guild(guild_id):
    gid = str(guild_id)
    if gid not in bot_config["guilds"]:
        bot_config["guilds"][gid] = {"log_channel_id": None, "tags": [], "admin_role_id": None}
    return bot_config["guilds"][gid]


def is_admin(interaction):
    """Allow if user has configured admin role, or is server owner / has manage perms."""
    if not interaction.guild_id:
        return False
    guild_data = get_guild(interaction.guild_id)
    role_id = guild_data.get("admin_role_id")
    member = interaction.user
    if member is None:
        return False
    try:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        if member.id == interaction.guild.owner_id:
            return True
        if role_id and any(r.id == role_id for r in member.roles):
            return True
    except Exception:
        pass
    return False


def normalize_tag(tag):
    tag = tag.strip().upper()
    if not tag.startswith("["):
        tag = f"[{tag}"
    if not tag.endswith("]"):
        tag = f"{tag}]"
    return tag


def find_tag(guild_data, tag_name):
    target = normalize_tag(tag_name)
    for entry in guild_data["tags"]:
        if entry["tag"].upper() == target:
            return entry
    return None


def build_tag_list_embed(guild_data):
    tags = guild_data.get("tags", [])
    embed = discord.Embed(title="Tracked Tags", color=0x00FF88)
    if not tags:
        embed.description = "No tags tracked yet. Click **Add Tag** to start."
    else:
        lines = []
        for e in tags:
            logo_status = "image" if e.get("logo") else "-"
            lines.append(f"- **{e['tag']}**  {logo_status}")
        embed.description = "\n".join(lines)
    embed.set_footer(text=f"{len(tags)} tag(s)")
    return embed


# --- MODALS ---
class AddTagModal(discord.ui.Modal, title="Add Tag"):
    tag_input = discord.ui.TextInput(label="Tag name (e.g. gi)", required=True)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You don't have permission to manage tags.", ephemeral=True)
            return
        guild_data = get_guild(self.guild_id)
        tag = normalize_tag(self.tag_input.value)
        for entry in guild_data["tags"]:
            if entry["tag"].upper() == tag:
                await interaction.response.send_message(f"Already tracking **{tag}**.", ephemeral=True)
                return
        guild_data["tags"].append({"tag": tag, "logo": None})
        await _save_to_gist(bot_config)
        embed = build_tag_list_embed(guild_data)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await interaction.followup.send(f"Added **{tag}**", ephemeral=True)


class AddLogoModal(discord.ui.Modal, title="Set Tag Logo"):
    tag_input = discord.ui.TextInput(label="Tag name (e.g. gi)", required=True)
    logo_input = discord.ui.TextInput(label="Logo URL", required=True, placeholder="https://example.com/logo.png")

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You don't have permission to manage tags.", ephemeral=True)
            return
        guild_data = get_guild(self.guild_id)
        tag = normalize_tag(self.tag_input.value)
        entry = find_tag(guild_data, tag)
        if not entry:
            await interaction.response.send_message(f"**{tag}** not tracked. Add it first.", ephemeral=True)
            return
        entry["logo"] = self.logo_input.value.strip()
        await _save_to_gist(bot_config)
        embed = build_tag_list_embed(guild_data)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await interaction.followup.send(f"Logo set for **{tag}**", ephemeral=True)


class RemoveTagModal(discord.ui.Modal, title="Remove Tag"):
    tag_input = discord.ui.TextInput(label="Tag name to remove", required=True)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You don't have permission to manage tags.", ephemeral=True)
            return
        guild_data = get_guild(self.guild_id)
        tag = normalize_tag(self.tag_input.value)
        entry = find_tag(guild_data, tag)
        if not entry:
            await interaction.response.send_message(f"**{tag}** is not tracked.", ephemeral=True)
            return
        guild_data["tags"].remove(entry)
        await _save_to_gist(bot_config)
        embed = build_tag_list_embed(guild_data)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await interaction.followup.send(f"Removed **{tag}**", ephemeral=True)


# --- TAG VIEW ---
class TagView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    @discord.ui.button(label="Add Tag", style=discord.ButtonStyle.success, custom_id="wl_add_tag")
    async def add_tag_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("You don't have permission to manage tags.", ephemeral=True)
            return
        await interaction.response.send_modal(AddTagModal(self.guild_id))

    @discord.ui.button(label="Set Logo", style=discord.ButtonStyle.primary, custom_id="wl_set_logo")
    async def set_logo_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("You don't have permission to manage tags.", ephemeral=True)
            return
        await interaction.response.send_modal(AddLogoModal(self.guild_id))

    @discord.ui.button(label="Remove Tag", style=discord.ButtonStyle.danger, custom_id="wl_remove_tag")
    async def remove_tag_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("You don't have permission to manage tags.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveTagModal(self.guild_id))


# --- WIN PARSER + POLLER ---
def parse_win_entries(text):
    entries = []
    blocks = re.split(r'\n\n+', text.strip())
    for block in blocks:
        def field(name):
            m = re.search(rf"^\s*{re.escape(name)}:\s*(.+?)\s*$", block, re.MULTILINE)
            return m.group(1) if m else None

        winning_clan = field("Winning Clan")
        if not winning_clan:
            continue

        time_val = field("Time")
        win_hash = hashlib.md5(f"{time_val}:{winning_clan}".encode()).hexdigest()

        if win_hash in seen_wins:
            continue

        entries.append({
            "time": time_val,
            "contest": field("Contest"),
            "map": field("Map"),
            "player_count": field("Player Count"),
            "winning_clan": winning_clan,
            "prev_points": field("Prev. Points"),
            "gain": field("Gain"),
            "curr_points": field("Curr. Points"),
            "payout": field("Payout"),
            "clan_winners": field("Clan Winners"),
            "hash": win_hash,
        })
    return entries


async def _fetch_results_text():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=20)

    # 1) Try direct fetch first
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RESULTS_URL, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.text(), None
                print(f"Direct fetch failed: HTTP {resp.status}, trying relay...")
    except Exception as e:
        print(f"Direct fetch error: {e}, trying relay...")

    # 2) Fall back to relay (different IP) to bypass territorial.io IP blocks
    relay_url = "https://r.jina.ai/" + RESULTS_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(relay_url, timeout=timeout) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # strip the r.jina.ai markdown header wrapper, keep only the win data
                    idx = text.find("Time:")
                    if idx != -1:
                        text = text[idx:]
                    return text, "relay"
                return None, f"relay HTTP {resp.status}"
    except Exception as e:
        return None, f"relay error {e}"


async def fetch_and_log():
    text, err = await _fetch_results_text()
    if text is None:
        runtime_stats["last_fetch_error"] = err
        print(f"Fetch failed: {err}")
        return
    if err:
        runtime_stats["last_fetch_source"] = "relay (r.jina.ai)"
    else:
        runtime_stats["last_fetch_source"] = "direct"
    runtime_stats["last_fetch_error"] = None

    entries = parse_win_entries(text)
    runtime_stats["last_poll"] = __import__("datetime").datetime.now().strftime("%H:%M:%S")
    runtime_stats["last_entries"] = len(entries)
    runtime_stats["poll_count"] += 1
    runtime_stats["last_fetch_error"] = None
    print(f"[poll] parsed {len(entries)} win entries, seen={len(seen_wins)}")

    for guild_id, guild_data in bot_config["guilds"].items():
        log_channel_id = guild_data.get("log_channel_id")
        if not log_channel_id:
            print(f"[poll] guild {guild_id}: no log channel set")
            continue

        log_channel = bot.get_channel(log_channel_id)
        if not log_channel:
            print(f"[poll] guild {guild_id}: channel {log_channel_id} NOT FOUND")
            runtime_stats["last_send_error"] = f"channel {log_channel_id} not found"
            continue

        for win in entries:
            entry = next((e for e in guild_data["tags"] if e["tag"].upper() in win["winning_clan"].upper()), None)
            seen_wins.add(win["hash"])
            if not entry:
                if win["winning_clan"].upper() in [t["tag"].upper() for t in guild_data["tags"]]:
                    print(f"[poll] skipped (already seen): {win['winning_clan']}")
                continue

            desc_lines = [f"**{win['winning_clan']}** won on **{win['map']}**"]
            if win.get("contest") == "Yes":
                desc_lines.append("**Contest**")
            desc_lines.append(f"**{win['player_count']}** Players!")
            desc_lines.append(f"{win['prev_points']} -> **{win['curr_points']}**")

            embed = discord.Embed(
                title="Win Logged",
                color=0x00FF88,
                description="\n".join(desc_lines),
            )
            if entry.get("logo"):
                embed.set_thumbnail(url=entry["logo"])

            runtime_stats["last_match"] = win["winning_clan"]
            try:
                await log_channel.send(embed=embed)
                print(f"Logged win by {win['winning_clan']} (guild {guild_id})")
            except Exception as e:
                runtime_stats["last_send_error"] = str(e)
                print(f"Send error for {win['winning_clan']}: {e}")

@tasks.loop(seconds=10)
async def poll_results():
    await fetch_and_log()


# --- SLASH COMMANDS ---
@bot.tree.command(name="setlogchannel", description="Set the channel where win embeds are posted")
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild_id:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to configure the bot.", ephemeral=True)
        return
    guild_data = get_guild(interaction.guild_id)
    guild_data["log_channel_id"] = channel.id
    await _save_to_gist(bot_config)
    await interaction.response.send_message(f"Log channel set to {channel.mention}", ephemeral=True)


@bot.tree.command(name="setadminrole", description="Set the role allowed to manage the bot")
async def setadminrole(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild_id:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to configure the bot.", ephemeral=True)
        return
    guild_data = get_guild(interaction.guild_id)
    guild_data["admin_role_id"] = role.id
    await _save_to_gist(bot_config)
    await interaction.response.send_message(f"Admin role set to {role.mention}", ephemeral=True)


@bot.tree.command(name="tag", description="Manage tracked clan tags for this server")
async def tag_cmd(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to manage tags.", ephemeral=True)
        return
    guild_data = get_guild(interaction.guild_id)
    embed = build_tag_list_embed(guild_data)
    await interaction.response.send_message(embed=embed, view=TagView(interaction.guild_id), ephemeral=True)


@bot.tree.command(name="setlogo", description="Set a logo for a tag by uploading an image")
async def setlogo_cmd(interaction: discord.Interaction, tag: str, image: discord.Attachment):
    if not interaction.guild_id:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to configure the bot.", ephemeral=True)
        return
    guild_data = get_guild(interaction.guild_id)
    tag = normalize_tag(tag)
    entry = find_tag(guild_data, tag)
    if not entry:
        await interaction.response.send_message(f"**{tag}** not tracked. Add it with `/tag` first.", ephemeral=True)
        return
    entry["logo"] = image.url
    await _save_to_gist(bot_config)
    await interaction.response.send_message(f"Logo set for **{tag}**", ephemeral=True)


@bot.tree.command(name="status", description="Show current config and polling status")
async def status_cmd(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    guild_data = get_guild(interaction.guild_id)
    lines = []
    chan_id = guild_data.get("log_channel_id")
    chan = bot.get_channel(chan_id) if chan_id else None
    lines.append(f"**Log channel:** {chan.mention if chan else (f'ID {chan_id} (NOT FOUND)' if chan_id else 'NOT SET')}")
    tags = guild_data.get("tags", [])
    lines.append(f"**Tracked tags ({len(tags)}):** " + (", ".join(t['tag'] for t in tags) if tags else "NONE"))
    role_id = guild_data.get("admin_role_id")
    lines.append(f"**Admin role:** {interaction.guild.get_role(role_id).mention if role_id and interaction.guild.get_role(role_id) else ('role ID ' + str(role_id) if role_id else 'NOT SET (server admins only)')}")
    lines.append(f"**Polling:** {'running' if poll_results.is_running() else 'STOPPED'}")
    lines.append(f"**Config storage:** {'gist ' + str(_gist_id) if _gist_headers else 'NO GITHUB_PAT (ephemeral)'}")
    lines.append(f"**Last poll:** {runtime_stats['last_poll']} ({runtime_stats['poll_count']} polls)")
    lines.append(f"**Last entries parsed:** {runtime_stats['last_entries']}")
    lines.append(f"**Last match:** {runtime_stats['last_match'] or 'none yet'}")
    lines.append(f"**Last send error:** {runtime_stats['last_send_error'] or 'none'}")
    lines.append(f"**Last fetch error:** {runtime_stats.get('last_fetch_error') or 'none'}")
    lines.append(f"**Fetch source:** {runtime_stats.get('last_fetch_source') or 'n/a'}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

    # Load config from gist (never reset to empty unless gist is missing)
    await _find_or_create_gist()
    global bot_config
    loaded = await _load_from_gist()
    if loaded is not None:
        bot_config = loaded
        print(f"Loaded config: {len(bot_config.get('guilds', {}))} guild(s)")
    else:
        # Fresh config (new gist was just created)
        bot_config = {"guilds": {}}
        print("No existing config - starting fresh")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

    if not poll_results.is_running():
        poll_results.start()
        print(f"Polling {RESULTS_URL} every 10s")
        await fetch_and_log()


_token = os.getenv("WINLOGGER_TOKEN")
if not _token:
    _token = os.getenv("DISCORD_TOKEN")
print(f"[env] WINLOGGER_TOKEN = {'<set>' if _token else 'MISSING (None)'} (len={len(_token) if _token else 0})")
if not _token:
    print("[env] FATAL: no WINLOGGER_TOKEN or DISCORD_TOKEN env var found - check Wispbyte Startup > Environment Variables")
bot.run(_token)