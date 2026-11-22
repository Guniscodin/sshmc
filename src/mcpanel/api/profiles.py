import asyncio
from contextlib import AsyncExitStack
import logging
import os
import re
import shutil
import json
import struct
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from mcpanel.models import Profile, BuildType, ProfileStatus, ModDownload
from mcpanel.config import PROFILES_DIR
from mcpanel.core import (
    runtime, loader, ports, process, modpacks, backups, rcon, mod_storage,
    server_properties, launch_dirs, workspace,
)
from mcpanel.core.mod_downloads import source_key, legacy_cache_key
from mcpanel import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/profiles", tags=["profiles"])

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


class BackupRequest(BaseModel):
    worlds: bool = True
    mods: bool = False
    config: bool = False
    datapacks: bool = False
    server_settings: bool = True
    label: str | None = Field(default=None, max_length=200)


def _parse_mod_downloads(raw: str) -> list[dict]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Mod downloads must be valid JSON.") from exc
    if not isinstance(parsed, list):
        raise HTTPException(status_code=422, detail="Mod downloads must be a list of download links.")
    return parsed


# Editable server.properties keys. The panel always manages the keys in
# MANAGED_SERVER_PROPERTIES itself; they cannot be changed here.
SERVER_SETTING_SPECS: dict[str, dict] = {
    # World & Generation
    "level-name": {
        "type": "text", "default": "world", "category": "world", "label": "World Folder Name",
        "description": "Name of the world save directory. Change this to switch between different worlds on disk.",
        "pattern": r"^[A-Za-z0-9_.-]+$",
        "pattern_error": "World name may only contain letters, numbers, '.', '_' and '-'.",
        "max_length": 64,
        "placeholder": "world",
    },
    "level-seed": {
        "type": "text", "default": "", "category": "world", "allow_empty": True, "label": "World Seed",
        "description": "Seed used to generate a new world. Leave empty for a random seed.",
        "max_length": 128,
        "placeholder": "e.g. -123456789 or custom text",
    },
    "level-type": {
        "type": "choice", "default": "minecraft:normal", "category": "world", "label": "World Preset Type",
        "options": ["minecraft:normal", "minecraft:flat", "minecraft:large_biomes", "minecraft:amplified", "minecraft:single_biome_surface", "DEFAULT", "FLAT", "LARGEBIOMES", "AMPLIFIED", "CUSTOMIZED", "default", "buffet"],
        "description": "World generator preset. Only affects newly generated worlds.",
        "legacy_map": {
            "DEFAULT": "minecraft:normal", "FLAT": "minecraft:flat", "LARGEBIOMES": "minecraft:large_biomes",
            "AMPLIFIED": "minecraft:amplified", "CUSTOMIZED": "minecraft:single_biome_surface",
            "default": "minecraft:normal", "buffet": "minecraft:single_biome_surface",
        },
    },
    "generate-structures": {
        "type": "bool", "default": "true", "category": "world", "label": "Generate Structures",
        "description": "Generate structures like villages, dungeons, temples, and strongholds in new chunks.",
    },
    "max-world-size": {
        "type": "int", "default": "29999984", "min": 1, "max": 29999984, "category": "world", "label": "World Border Max Radius",
        "description": "Caps the maximum radius the world border can expand to. Does not set the border itself — use /worldborder set in-game to set the current border (1 to 29,999,984). Default is 29,999,984.",
    },
    "allow-nether": {
        "type": "bool", "default": "true", "category": "world", "label": "Allow Nether",
        "description": "Let players travel to the Nether dimension through portals.",
    },

    # Gameplay & Rules
    "difficulty": {
        "type": "choice", "default": "easy", "category": "gameplay", "label": "Difficulty",
        "options": ["peaceful", "easy", "normal", "hard"],
        "description": "Server difficulty applied to loaded worlds.",
        "legacy_map": {"0": "peaceful", "1": "easy", "2": "normal", "3": "hard"},
    },
    "gamemode": {
        "type": "choice", "default": "survival", "category": "gameplay", "label": "Default Game Mode",
        "options": ["survival", "creative", "adventure", "spectator"],
        "description": "Game mode for new players joining the server.",
        "legacy_map": {"0": "survival", "1": "creative", "2": "adventure", "3": "spectator"},
    },
    "force-gamemode": {
        "type": "bool", "default": "false", "category": "gameplay", "label": "Force Game Mode",
        "description": "Put joining players back into the default game mode even if they changed it.",
    },
    "hardcore": {
        "type": "bool", "default": "false", "category": "gameplay", "label": "Hardcore",
        "description": "Locks difficulty to hard and puts players into spectator mode on death.",
    },
    "pvp": {
        "type": "bool", "default": "true", "category": "gameplay", "label": "PvP",
        "description": "Allow players to attack and deal damage to each other.",
    },
    "allow-flight": {
        "type": "bool", "default": "false", "category": "gameplay", "label": "Allow Flight",
        "description": "Permit flying in survival without false-positive kicks (useful for flight mods).",
    },
    "enable-command-block": {
        "type": "bool", "default": "false", "category": "gameplay", "label": "Command Blocks",
        "description": "Enable execution of command blocks in loaded worlds.",
    },

    # Mobs & Spawning
    "spawn-monsters": {
        "type": "bool", "default": "true", "category": "spawns", "label": "Spawn Monsters",
        "description": "Spawn hostile mobs (creepers, zombies, skeletons, etc.).",
    },
    "spawn-animals": {
        "type": "bool", "default": "true", "category": "spawns", "label": "Spawn Animals",
        "description": "Spawn passive animals (cows, pigs, sheep, etc.).",
    },
    "spawn-npcs": {
        "type": "bool", "default": "true", "category": "spawns", "label": "Spawn Villagers",
        "description": "Spawn villagers (NPCs) in villages and structures.",
    },
    "spawn-protection": {
        "type": "int", "default": "16", "min": 0, "max": 256, "category": "spawns", "label": "Spawn Protection Radius",
        "description": "Radius in blocks around world spawn only operators may edit. 0 disables protection.",
    },

    # Multiplayer & Player Policies
    "online-mode": {
        "type": "bool", "default": "true", "category": "multiplayer", "label": "Online Mode",
        "description": "Verify players against Mojang's session servers. Turn off to allow offline-mode clients.",
    },
    "enforce-secure-profile": {
        "type": "bool", "default": "true", "category": "multiplayer", "label": "Enforce Secure Profiles",
        "description": "Require signed player chat and profiles. Keep off for offline-mode servers.",
    },
    "max-players": {
        "type": "int", "default": "20", "min": 1, "max": 100000, "category": "multiplayer", "label": "Max Players",
        "description": "Maximum number of players connected at the same time.",
    },
    "op-permission-level": {
        "type": "int", "default": "4", "min": 0, "max": 4, "category": "multiplayer", "label": "OP Permission Level",
        "description": "Default permission level granted to operators (1 = spawn protection, 4 = full admin).",
    },
    "player-idle-timeout": {
        "type": "int", "default": "0", "min": 0, "max": 2147483647, "category": "multiplayer", "label": "Player Idle Timeout (min)",
        "description": "Kick AFK/idle players after this many minutes. Set to 0 to disable automatic kicking.",
    },
    "hide-online-players": {
        "type": "bool", "default": "false", "category": "multiplayer", "label": "Hide Online Players",
        "description": "Hide online player names from the multiplayer server list hover menu for privacy.",
    },
    "prevent-proxy-connections": {
        "type": "bool", "default": "false", "category": "multiplayer", "label": "Prevent Proxy Connections",
        "description": "Block players connecting through commercial VPNs or proxies via Mojang's session check.",
    },
    "enable-status": {
        "type": "bool", "default": "true", "category": "multiplayer", "label": "Enable Status Ping",
        "description": "Allow the server to appear as online and respond to multiplayer list status pings.",
    },

    # Performance & Optimization / Crash Prevention
    "view-distance": {
        "type": "int", "default": "10", "min": 3, "max": 32, "category": "performance", "label": "View Distance",
        "description": "Chunks sent to each player. Lower values significantly reduce network and RAM load.",
    },
    "simulation-distance": {
        "type": "int", "default": "10", "min": 3, "max": 32, "category": "performance", "label": "Simulation Distance",
        "description": "Chunks that tick entities and blocks around players. Lower values reduce CPU load.",
    },
    "max-tick-time": {
        "type": "int", "default": "60000", "min": -1, "max": 2147483647, "category": "performance", "label": "Watchdog Max Tick Time (ms)",
        "description": "Max milliseconds a single tick may take before the watchdog stops the server. Set to -1 to disable watchdog (recommended for heavy modpacks).",
    },
    "entity-broadcast-range-percentage": {
        "type": "int", "default": "100", "min": 10, "max": 1000, "category": "performance", "label": "Entity Broadcast Range (%)",
        "description": "Percentage of entity tracking distance sent to clients (10% to 1000%). Lowering to 50%–75% reduces server & client lag with high mob counts.",
    },
    "network-compression-threshold": {
        "type": "int", "default": "256", "min": -1, "max": 65535, "category": "performance", "label": "Network Compression Threshold",
        "description": "Packet size in bytes before compressing data. Set to -1 to disable compression (reduces CPU usage on fast networks). 0 compresses everything.",
    },
    "max-chained-neighbor-updates": {
        "type": "int", "default": "1000000", "min": 1, "max": 10000000, "category": "performance", "label": "Max Chained Neighbor Updates",
        "description": "Caps consecutive block updates (e.g. redstone, gravity blocks) before skipping. Prevents lag machines and freeze cascades.",
    },
    "sync-chunk-writes": {
        "type": "bool", "default": "true", "category": "performance", "label": "Sync Chunk Writes",
        "description": "Save chunk data synchronously to disk. Disabling can reduce I/O stutter on slower hard drives or heavily loaded disks.",
    },

    # Admin & Operator Broadcasts
    "broadcast-console-to-ops": {
        "type": "bool", "default": "true", "category": "admin", "label": "Broadcast Console to Ops",
        "description": "Send automated console command outputs and server alerts to online operators in-game.",
    },
    "broadcast-rcon-to-ops": {
        "type": "bool", "default": "true", "category": "admin", "label": "Broadcast RCON to Ops",
        "description": "Broadcast RCON command executions (such as panel actions) in game chat to online operators.",
    },
}

MANAGED_SERVER_PROPERTIES = {"server-port", "enable-rcon", "rcon.port", "rcon.password", "motd"}

# Keys that change who can reach or administer the server. Blocked from the
# settings PUT endpoint unless they have a dedicated validated field.
# server-port and enable-rcon are already covered by MANAGED_SERVER_PROPERTIES;
# they are listed here for clarity but the blocked-key check unions both sets.
SECURITY_SENSITIVE_KEYS = {
    "server-ip", "query.port", "enable-query",
}


def _is_rcon_key(key: str) -> bool:
    return key.startswith("rcon.")


def _is_blocked_setting_key(key: str) -> bool:
    return key in MANAGED_SERVER_PROPERTIES or key in SECURITY_SENSITIVE_KEYS or _is_rcon_key(key)


# Control characters (0x00-0x1F) that must never appear in setting keys or values.
_CONTROL_CHARS = frozenset(chr(i) for i in range(32))


def _has_control_chars(text: str) -> bool:
    return any(c in _CONTROL_CHARS for c in text)


# Canonical gamerule name -> spec with legacy/modern key mapping.
# Minecraft snapshot 25w44a (1.21.11) renamed gamerules from camelCase to
# snake_case (optionally namespaced as minecraft:<name>). The renames are NOT
# mechanical — doFireTick has no modern equivalent, disableRaids→raids inverts
# the value, and others change in non-obvious ways.
#
# Each spec includes:
#   legacy_key:     the camelCase name used pre-1.21.11
#   modern_keys:    list of possible snake_case names for 1.21.11+ (empty if none)
#   invert:         whether the value is inverted between legacy and modern
#   legacy_first:   for unconfirmed renames, try legacy first regardless of version
#   unavailable_on_modern: no modern equivalent, hide on 1.21.11+
GAMERULE_SPECS: dict[str, dict] = {
    # Player & Death
    "keepInventory": {
        "category": "player", "type": "bool", "default": False,
        "label": "Keep Inventory on Death",
        "description": "Players retain items, weapons, armor, and XP in their inventory when they die.",
        "legacy_key": "keepInventory",
        "modern_keys": ["keep_inventory"],
        "invert": False,
    },
    "naturalRegeneration": {
        "category": "player", "type": "bool", "default": True,
        "label": "Natural Health Regeneration",
        "description": "Players naturally regenerate health when their hunger bar is sufficiently full.",
        "legacy_key": "naturalRegeneration",
        "modern_keys": ["natural_health_regeneration"],
        "invert": False,
    },
    "playersSleepingPercentage": {
        "category": "player", "type": "int", "default": 100, "min": 0, "max": 100,
        "label": "Sleeping Percentage to Skip Night",
        "description": "Percentage of online players in bed needed to skip the night (0 or 1 for single-player sleep).",
        "legacy_key": "playersSleepingPercentage",
        "modern_keys": ["players_sleeping_percentage"],
        "invert": False,
    },
    "spawnRadius": {
        "category": "player", "type": "int", "default": 10, "min": 0, "max": 100,
        "label": "Spawn Radius around World Spawn",
        "description": "Radius in blocks within which respawning players appear around world spawn (0 = exact block).",
        "legacy_key": "spawnRadius",
        "modern_keys": ["respawn_radius"],
        "invert": False,
    },
    "showDeathMessages": {
        "category": "player", "type": "bool", "default": True,
        "label": "Show Death Messages in Chat",
        "description": "Display death notifications in chat when a player dies or is slain.",
        "legacy_key": "showDeathMessages",
        "modern_keys": ["show_death_messages"],  # NOT CONFIRMED - try legacy first
        "invert": False,
        "legacy_first": True,
    },
    # World & Environment
    "mobGriefing": {
        "category": "world", "type": "bool", "default": True,
        "label": "Mob Griefing (Creeper/Enderman Damage)",
        "description": "Allow creepers, withers, ghasts, and endermen to destroy, explode, or pick up blocks in the world.",
        "legacy_key": "mobGriefing",
        "modern_keys": ["mob_griefing"],
        "invert": False,
    },
    "doFireTick": {
        "category": "world", "type": "bool", "default": True,
        "label": "Fire Spread & Extinguish",
        "description": "Allow fire to spread to nearby burnable blocks and extinguish over time. No modern equivalent — unavailable on 1.21.11+.",
        "legacy_key": "doFireTick",
        "modern_keys": [],  # No modern equivalent
        "invert": False,
        "unavailable_on_modern": True,
    },
    "doDaylightCycle": {
        "category": "world", "type": "bool", "default": True,
        "label": "Daylight Cycle (Sun & Moon Advance)",
        "description": "Advance the time of day and cycle between day and night continuously.",
        "legacy_key": "doDaylightCycle",
        "modern_keys": ["advance_time"],
        "invert": False,
    },
    "doWeatherCycle": {
        "category": "world", "type": "bool", "default": True,
        "label": "Weather Cycle (Rain & Thunder)",
        "description": "Allow rain, snow, and thunderstorms to occur dynamically.",
        "legacy_key": "doWeatherCycle",
        "modern_keys": ["advance_weather"],  # NOT CONFIRMED - try legacy first
        "invert": False,
        "legacy_first": True,
    },
    "doPatrolSpawning": {
        "category": "world", "type": "bool", "default": True,
        "label": "Illager Patrol Spawning",
        "description": "Spawn wandering illager raid patrols periodically in the world.",
        "legacy_key": "doPatrolSpawning",
        "modern_keys": ["spawn_patrols"],
        "invert": False,
    },
    "doTraderSpawning": {
        "category": "world", "type": "bool", "default": True,
        "label": "Wandering Trader Spawning",
        "description": "Spawn wandering traders with llamas near players.",
        "legacy_key": "doTraderSpawning",
        "modern_keys": ["spawn_wandering_traders"],
        "invert": False,
    },
    "doInsomnia": {
        "category": "world", "type": "bool", "default": True,
        "label": "Phantom Spawning (Insomnia)",
        "description": "Spawn flying phantoms who attack players that haven't slept in several days.",
        "legacy_key": "doInsomnia",
        "modern_keys": ["spawn_phantoms"],
        "invert": False,
    },
    # Loot & Drops
    "doMobLoot": {
        "category": "drops", "type": "bool", "default": True,
        "label": "Mob Item Drops",
        "description": "Mobs drop loot items and equipment when killed.",
        "legacy_key": "doMobLoot",
        "modern_keys": ["mob_drops"],
        "invert": False,
    },
    "doTileDrops": {
        "category": "drops", "type": "bool", "default": True,
        "label": "Block Item Drops",
        "description": "Blocks drop items when broken or mined by players.",
        "legacy_key": "doTileDrops",
        "modern_keys": ["block_drops"],
        "invert": False,
    },
    "doEntityDrops": {
        "category": "drops", "type": "bool", "default": True,
        "label": "Entity Item Drops",
        "description": "Entities such as minecarts, boats, paintings, and item frames drop items when destroyed.",
        "legacy_key": "doEntityDrops",
        "modern_keys": ["entity_drops"],
        "invert": False,
    },
    "doMobSpawning": {
        "category": "world", "type": "bool", "default": True,
        "label": "Mob Spawning",
        "description": "Controls whether mobs can spawn naturally.",
        "legacy_key": "doMobSpawning",
        "modern_keys": ["spawn_mobs"],
        "invert": False,
    },
    "disableRaids": {
        "category": "world", "type": "bool", "default": True,
        "label": "Disable Raids",
        "description": "Controls whether illager raid patrols spawn. Value is inverted on modern versions (raids=true means raids enabled).",
        "legacy_key": "disableRaids",
        "modern_keys": ["raids"],
        "invert": True,
    },
}


def _parse_mc_version(version_str: str | None) -> tuple[int, ...]:
    """Parse a Minecraft version string into a comparable tuple of ints."""
    if not version_str:
        return (0,)
    parts: list[int] = []
    for part in version_str.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _is_modern_minecraft(mc_version: str | None) -> bool:
    """Check if the Minecraft version is 1.21.11 or newer (snapshot 25w44a+),
    when gamerules were renamed from camelCase to snake_case."""
    return _parse_mc_version(mc_version) >= (1, 21, 11)


def _gamerule_aliases(spec: dict, mc_version: str | None) -> list[str]:
    """Return the ordered list of alias keys to try for a gamerule.

    On modern versions (>= 1.21.11), try modern keys first.
    On older versions, try legacy key first.
    For rules with legacy_first=True, always try legacy first.
    """
    legacy_key = spec.get("legacy_key", "")
    modern_keys = spec.get("modern_keys", [])
    legacy_first = spec.get("legacy_first", False)

    is_modern = _is_modern_minecraft(mc_version)

    if legacy_first:
        return [legacy_key] + [k for k in modern_keys if k != legacy_key]
    if is_modern:
        return [k for k in modern_keys if k != legacy_key] + [legacy_key]
    return [legacy_key] + [k for k in modern_keys if k != legacy_key]


def _validate_server_setting(key: str, spec: dict, value: str) -> str:
    text = (value or "").strip()
    if spec["type"] == "text":
        if not text and not spec.get("allow_empty", False):
            raise HTTPException(status_code=422, detail=f"'{key}' cannot be empty.")
        if spec.get("pattern") and text:
            if not re.fullmatch(spec["pattern"], text):
                raise HTTPException(status_code=422, detail=spec.get("pattern_error", f"'{key}' format is invalid."))
        if spec.get("max_length") and len(text) > spec["max_length"]:
            raise HTTPException(status_code=422, detail=f"'{key}' cannot be longer than {spec['max_length']} characters.")
        if text.endswith("\\"):
            raise HTTPException(status_code=422, detail=f"'{key}' cannot end with a backslash (line continuation character in .properties files).")
        return text
    if not text:
        raise HTTPException(status_code=422, detail=f"'{key}' cannot be empty.")
    if spec["type"] == "bool":
        if text.lower() not in ("true", "false"):
            raise HTTPException(status_code=422, detail=f"'{key}' must be true or false.")
        return text.lower()
    if spec["type"] == "int":
        try:
            number = int(text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"'{key}' must be a whole number.") from exc
        if not (spec["min"] <= number <= spec["max"]):
            raise HTTPException(
                status_code=422,
                detail=f"'{key}' must be between {spec['min']} and {spec['max']}.",
            )
        return str(number)
    options = spec["options"]
    if text not in options:
        if text in spec.get("legacy_map", {}):
            return str(spec["legacy_map"][text])
        raise HTTPException(status_code=422, detail=f"'{key}' must be one of: {', '.join(options)}.")
    return text


def _profile_server_running(state) -> bool:
    return bool(state and state.status in (
        ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING
    ))


# Minecraft requires server-icon.png to be exactly 64x64 pixels.
SERVER_ICON_SIZE = 64
SERVER_ICON_MAX_BYTES = 256 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _profile_icon_paths(profile_id: str) -> tuple[Path, Path]:
    """(stored source icon, live server icon) for a profile."""
    try:
        live_dir = workspace.server_dir_for_id(profile_id)
    except (FileNotFoundError, launch_dirs.LaunchDirError, OSError):
        live_dir = PROFILES_DIR / profile_id / "server"
    return (
        PROFILES_DIR / profile_id / "server_icon.png",
        live_dir / "server-icon.png",
    )


def _profile_has_icon(profile_id: str) -> bool:
    source, live = _profile_icon_paths(profile_id)
    return (source.is_file() and not source.is_symlink()) or (live.is_file() and not live.is_symlink())


def _validate_server_icon(data: bytes) -> None:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE):
        raise HTTPException(status_code=422, detail="The server icon must be a PNG image.")
    if data[12:16] != b"IHDR":
        raise HTTPException(status_code=422, detail="The server icon must be a valid PNG image.")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (SERVER_ICON_SIZE, SERVER_ICON_SIZE):
        raise HTTPException(
            status_code=422,
            detail=f"Minecraft server icons must be exactly {SERVER_ICON_SIZE}x{SERVER_ICON_SIZE} pixels.",
        )


def _replacement_pending(profile: Profile) -> bool:
    try:
        metadata = workspace.server_dir(profile) / "profile_meta.json"
    except launch_dirs.LaunchDirError:
        metadata = PROFILES_DIR / profile.id / "server" / "profile_meta.json"
    if not metadata.is_file():
        return False
    try:
        old = json.loads(metadata.read_text(encoding="utf-8"))
        return any((
            old.get("build") != profile.build.value,
            old.get("mc_version") != profile.mc_version,
            old.get("loader_version") != profile.loader_version,
        ))
    except (OSError, json.JSONDecodeError):
        return False

@router.get("")
def list_all_profiles():
    """Returns a list of all profiles."""
    return [p.model_dump(mode="json", exclude={"rcon_password"}) for p in db.list_profiles()]

@router.post("", status_code=status.HTTP_201_CREATED)
def create_new_profile(
    name: str = Form(...),
    build: BuildType = Form(...),
    mc_version: str = Form(...),
    loader_version: str = Form("latest"),
    git_url: Optional[str] = Form(None),
    description: str = Form(""),
    mod_downloads: str = Form("[]"),
    java_path: str = Form("java"),
    ram_mb: int = Form(2048),
    port: Optional[int] = Form(None)
):
    """Creates a new profile in the database from form data."""
    try:
        # Clean up the git_url if the user left it blank (empty string -> None)
        clean_git_url = git_url.strip() if git_url and git_url.strip() else None
        parsed_mod_downloads = _parse_mod_downloads(mod_downloads)
        
        # Construct the strictly-typed Pydantic model
        current_profiles = db.list_profiles()
        used_game_ports = {p.port for p in current_profiles}
        used_rcon_ports = {p.rcon_port for p in current_profiles}
        if port is None:
            assigned_port = ports.find_available_port(25565, used_game_ports | used_rcon_ports)
        elif port in used_game_ports or port in used_rcon_ports or not ports.is_port_available(port):
            raise HTTPException(status_code=409, detail=f"Port {port} is already assigned or in use.")
        else:
            assigned_port = port
        assigned_rcon_port = ports.find_available_port(
            25575, used_game_ports | used_rcon_ports | {assigned_port}
        )

        profile = Profile(
            name=name,
            build=build,
            mc_version=mc_version,
            loader_version=loader_version,
            git_url=clean_git_url,
            description=(description or "").strip(),
            mod_downloads=[ModDownload.model_validate(item) for item in parsed_mod_downloads],
            java_path=java_path,
            ram_mb=ram_mb,
            port=assigned_port,
            rcon_port=assigned_rcon_port,
        )
        
        db.create_profile(profile)
        logger.info(f"API: Created profile '{profile.name}' (ID: {profile.id})")
        return profile.model_dump(mode="json", exclude={"rcon_password"})
        
    except HTTPException:
        raise
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except db.DatabaseError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.error(f"API Error creating profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{profile_id}")
async def update_existing_profile(
    profile_id: str,
    name: str = Form(...),
    build: BuildType = Form(...),
    mc_version: str = Form(...),
    loader_version: str = Form("latest"),
    git_url: Optional[str] = Form(None),
    description: str = Form(""),
    mod_downloads: str = Form("[]"),
    modpack_name: Optional[str] = Form(None),
    launch_dir: Optional[str] = Form(None),
    java_path: str = Form("java"),
    ram_mb: int = Form(2048),
    port: Optional[int] = Form(None),
    backup_worlds_on_change: bool = Form(True),
    backup_mods_on_change: bool = Form(False),
    backup_config_on_change: bool = Form(False),
    backup_datapacks_on_change: bool = Form(False),
    backup_server_settings_on_change: bool = Form(False),
    backup_on_restart: bool = Form(False),
    max_backups: int = Form(5),
):
    """Updates an existing profile."""
    existing = db.get_profile(profile_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Profile settings cannot be changed while a backup is running.")
    mc_version = mc_version.strip()
    loader_version = loader_version.strip()
        
    incompatible_change = any((
        existing.build != build,
        existing.mc_version != mc_version,
        existing.loader_version != loader_version,
    ))
    state = db.get_runtime_state(profile_id)
    if incompatible_change and state and state.status in (
        ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING
    ):
        raise HTTPException(status_code=409, detail="Stop the server before changing its build or version.")
    if incompatible_change and state and state.pid and state.pid_create_time and process.is_process_alive(state.pid, state.pid_create_time):
        raise HTTPException(status_code=409, detail="Stop the server before changing its build or version.")

    assigned_port = existing.port if port is None else port
    if assigned_port != existing.port and state and state.status in (
        ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING
    ):
        raise HTTPException(status_code=409, detail="Stop the server before changing its game port.")
    if assigned_port != existing.port:
        others = [p for p in db.list_profiles() if p.id != profile_id]
        occupied = {p.port for p in others} | {p.rcon_port for p in others}
        if assigned_port in occupied or not ports.is_port_available(assigned_port):
            raise HTTPException(status_code=409, detail=f"Port {assigned_port} is already assigned or in use.")
    if assigned_port == existing.rcon_port:
        raise HTTPException(status_code=409, detail="Game and RCON ports must be different.")

    try:
        clean_git_url = git_url.strip() if git_url and git_url.strip() else None
        parsed_mod_downloads = _parse_mod_downloads(mod_downloads)
        existing_downloads = [item.model_dump(exclude_none=True) for item in existing.mod_downloads]
        requested_pack = modpacks.sanitize_pack_name(modpack_name) if modpack_name and modpack_name.strip() else None
        if requested_pack and not modpacks.get_pack(requested_pack):
            raise HTTPException(status_code=422, detail="The selected mod pack no longer exists.")
        requested_launch = launch_dirs.sanitize_name(launch_dir) if launch_dir and launch_dir.strip() else None
        if requested_launch:
            manifest = launch_dirs.get_launch_dir(requested_launch)
            if not manifest:
                raise HTTPException(status_code=422, detail="The selected launch directory no longer exists.")
            if not launch_dirs.is_compatible(manifest, build.value, mc_version):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Launch directory '{requested_launch}' targets "
                        f"{manifest.get('mc_version')} {manifest.get('build')}, not {mc_version} {build.value}."
                    ),
                )
            other_profile = launch_dirs.assigned_profile(requested_launch, exclude_profile_id=profile_id)
            if other_profile:
                raise HTTPException(
                    status_code=409,
                    detail=f"Launch directory '{requested_launch}' is already used by profile '{other_profile.name}'.",
                )
        version_changed = existing.build != build or existing.mc_version != mc_version
        effective_launch = requested_launch
        if version_changed and not requested_launch:
            # An assigned directory cannot follow a version/loader switch.
            effective_launch = None
        if effective_launch != existing.launch_dir and state and state.status in (
            ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING
        ):
            raise HTTPException(status_code=409, detail="Stop the server before changing its launch directory.")
        effective_git_url = None if incompatible_change else clean_git_url
        effective_pack = existing.modpack_name if incompatible_change else requested_pack
        effective_downloads = (
            [{**item, "skip_on_start": False} for item in parsed_mod_downloads]
            if incompatible_change else parsed_mod_downloads
        )
        effective_download_models = [ModDownload.model_validate(item) for item in effective_downloads]
        normalized_effective_downloads = [item.model_dump(exclude_none=True) for item in effective_download_models]
        sources_changed = any((
            effective_git_url != existing.git_url,
            effective_pack != existing.modpack_name,
            normalized_effective_downloads != existing_downloads,
        ))
        if sources_changed and state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise HTTPException(status_code=409, detail="Mod sources cannot be changed while the profile is starting or stopping.")
        
        updated_profile = Profile(
            id=profile_id,
            name=name,
            build=build,
            mc_version=mc_version,
            loader_version=loader_version,
            git_url=effective_git_url,
            description=(description or "").strip(),
            modpack_name=effective_pack,
            launch_dir=effective_launch,
            mod_downloads=effective_download_models,
            java_path=java_path,
            ram_mb=ram_mb,
            port=assigned_port,
            rcon_port=existing.rcon_port,
            rcon_password=existing.rcon_password,
            backup_worlds_on_change=backup_worlds_on_change,
            backup_mods_on_change=backup_mods_on_change,
            backup_config_on_change=backup_config_on_change,
            backup_datapacks_on_change=backup_datapacks_on_change,
            backup_server_settings_on_change=backup_server_settings_on_change,
            backup_on_restart=backup_on_restart,
            max_backups=max_backups,
            created_at=existing.created_at
        )
        
        async with AsyncExitStack() as source_stack:
            for pack_name in sorted({item for item in (existing.modpack_name, effective_pack) if item}):
                await source_stack.enter_async_context(mod_storage.pack_lock(pack_name))
            await source_stack.enter_async_context(mod_storage.source_lock(profile_id))
            current_state = db.get_runtime_state(profile_id)
            if (sources_changed or incompatible_change) and current_state and current_state.status in (
                ProfileStatus.STARTING, ProfileStatus.STOPPING
            ):
                message = (
                    "Stop the profile lifecycle operation before changing its build or version."
                    if incompatible_change
                    else "Mod sources cannot be changed while the profile is starting or stopping."
                )
                raise HTTPException(status_code=409, detail=message)
            if effective_pack and not modpacks.get_pack(effective_pack):
                raise HTTPException(status_code=422, detail="The selected mod pack no longer exists.")
            if effective_launch:
                other_profile = launch_dirs.assigned_profile(effective_launch, exclude_profile_id=profile_id)
                if other_profile:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Launch directory '{effective_launch}' is already used by profile '{other_profile.name}'.",
                    )
            db.update_profile(updated_profile)
            if existing.launch_dir and existing.launch_dir != effective_launch:
                await asyncio.to_thread(workspace.release_world_links, existing)
            root = mod_storage.profile_root(profile_id)
            if not incompatible_change:
                retained = {source_key(item, index) for index, item in enumerate(effective_download_models)}
                cache_root = root / "downloaded_mods"
                if cache_root.is_dir() and not cache_root.is_symlink():
                    for index, old_item in enumerate(existing.mod_downloads):
                        key = source_key(old_item, index)
                        if key not in retained:
                            for cache_key in (key, legacy_cache_key(old_item)):
                                folder = cache_root / cache_key
                                if folder.is_dir() and not folder.is_symlink():
                                    await asyncio.to_thread(shutil.rmtree, folder, True)
            else:
                repo_dir = root / "repo"
                if repo_dir.is_symlink():
                    repo_dir.unlink(missing_ok=True)
                elif repo_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, repo_dir, True)
                download_cache = root / "downloaded_mods"
                if download_cache.is_symlink():
                    download_cache.unlink(missing_ok=True)
                elif download_cache.exists():
                    await asyncio.to_thread(shutil.rmtree, download_cache, True)
                manifest = mod_storage.manifest_path(profile_id)
                if manifest.is_symlink():
                    manifest.unlink(missing_ok=True)
                elif manifest.exists():
                    await asyncio.to_thread(manifest.unlink)
                modrinth_cache = root / "modrinth_mods"
                if modrinth_cache.is_symlink():
                    modrinth_cache.unlink(missing_ok=True)
                elif modrinth_cache.exists():
                    await asyncio.to_thread(shutil.rmtree, modrinth_cache, True)
        logger.info(f"API: Updated profile '{updated_profile.name}'")
        result = updated_profile.model_dump(mode="json", exclude={"rcon_password"})
        result["applies_on_next_restart"] = bool(sources_changed and state and state.status == ProfileStatus.RUNNING)
        return result
        
    except HTTPException:
        raise
    except modpacks.ModPackError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except db.DatabaseError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.error(f"API Error updating profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{profile_id}/backups")
def list_profile_backups(profile_id: str):
    if not db.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return backups.list_backups(profile_id)


@router.post("/{profile_id}/backups")
async def create_profile_backup(profile_id: str, payload: BackupRequest):
    profile = db.get_profile(profile_id)
    state = db.get_runtime_state(profile_id)
    if not profile or not state:
        raise HTTPException(status_code=404, detail="Profile not found")
    if state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
        raise HTTPException(status_code=409, detail="Wait for the server lifecycle operation to finish before backing up.")
    selection = {
        "worlds": payload.worlds,
        "mods": payload.mods,
        "config": payload.config,
        "datapacks": payload.datapacks,
        "server_settings": payload.server_settings,
    }
    if not any(selection.values()):
        raise HTTPException(status_code=422, detail="Choose at least one item to include in this backup.")
    tracked_process_alive = bool(
        state.pid and state.pid_create_time
        and process.is_process_alive(state.pid, state.pid_create_time)
    )
    was_running = state.status == ProfileStatus.RUNNING or tracked_process_alive
    try:
        active = backups.begin_backup(profile_id)
    except backups.BackupInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    save_off_attempted = False
    result = None
    failure = None
    worker = None
    request_cancelled = False
    try:
        if was_running and selection["worlds"]:
            save_off_attempted = True
            save_off_response = await rcon.send_command(
                "127.0.0.1", profile.rcon_port, profile.rcon_password, "save-off"
            )
            if not save_off_response.strip() or "unknown command" in save_off_response.lower():
                raise backups.BackupError("Minecraft did not confirm save-off. Stop the server before backing up its world.")
            response = await rcon.send_command(
                "127.0.0.1", profile.rcon_port, profile.rcon_password, "save-all flush", timeout=30
            )
            response_text = response.lower()
            if not response.strip() or any(word in response_text for word in ("unknown command", "failed", "error")):
                raise backups.BackupError("Minecraft did not confirm save-all flush. Stop the server before backing up its world.")
        worker = asyncio.create_task(asyncio.to_thread(
            backups.create_manual_backup, profile, selection, payload.label, was_running, active["cancel"]
        ))
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        request_cancelled = True
        active["cancel"].set()
        if worker:
            try:
                await asyncio.shield(worker)
            except BaseException:
                pass
    except Exception as exc:
        failure = exc
    finally:
        if save_off_attempted:
            try:
                response = await rcon.send_command("127.0.0.1", profile.rcon_port, profile.rcon_password, "save-on")
                if "unknown command" in response.lower():
                    raise backups.BackupError("Minecraft did not restore automatic saving after the backup.")
            except Exception as exc:
                logger.exception("Could not restore autosaving after backup for %s", profile_id)
                if failure is None:
                    if result:
                        failure = backups.BackupError(
                            f"Backup {result['name']} was created ({result['size_bytes']} bytes), "
                            f"but Minecraft autosaving could not be restored: {exc}"
                        )
                    else:
                        failure = backups.BackupError(f"Minecraft autosaving could not be restored: {exc}")
        backups.finish_backup(profile_id)

    if request_cancelled:
        raise asyncio.CancelledError

    if failure:
        if isinstance(failure, backups.BackupCancelled):
            raise HTTPException(status_code=409, detail=str(failure)) from failure
        if was_running and selection["worlds"] and isinstance(failure, (rcon.RCONError, OSError)):
            raise HTTPException(status_code=409, detail="RCON is not reachable. Stop the server before backing up its world.") from failure
        if isinstance(failure, backups.BackupError):
            raise HTTPException(status_code=422, detail=str(failure)) from failure
        logger.error("Manual backup failed for %s: %s", profile_id, failure)
        raise HTTPException(status_code=500, detail="Could not create this backup.") from failure
    return result


@router.post("/{profile_id}/backups/cancel", status_code=202)
def cancel_profile_backup(profile_id: str):
    if not db.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    if not backups.cancel_backup(profile_id):
        raise HTTPException(status_code=409, detail="No backup is currently running for this profile.")
    return {"status": "cancelling"}


@router.delete("/{profile_id}/backups/{filename}", status_code=204)
def delete_profile_backup(profile_id: str, filename: str):
    if not db.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Cannot delete backups while a backup is running for this profile.")
    try:
        backups.delete_backup(profile_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Backup not found") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return None


@router.get("/{profile_id}/backups/{filename}/download")
def download_profile_backup(profile_id: str, filename: str):
    if not db.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    try:
        path = backups.resolve_backup(profile_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Backup not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/gzip", filename=filename)

@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, delete_files: bool = False):
    """
    Deletes a profile. Gracefully stops the server if running.
    Optionally deletes the profile directory from disk.
    """
    profile = db.get_profile(profile_id)
    state = db.get_runtime_state(profile_id)
    
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Profile settings are locked while a backup is running.")

    # Never remove a profile directory until its tracked server process is gone.
    if state and state.status in [ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING]:
        if not state.pid or not state.pid_create_time:
            raise HTTPException(status_code=409, detail="Server state is active but no process is tracked; refusing deletion.")
    if state and state.pid and state.pid_create_time:
        logger.info(f"Deleting profile '{profile.name}': Stopping tracked server process...")
        try:
            state = await runtime.stop_server(profile, state)
        except Exception as e:
            logger.error(f"Failed to gracefully stop server during deletion: {e}")
            raise HTTPException(status_code=409, detail="Could not stop the server; profile was not deleted.") from e
        if state.pid and state.pid_create_time and process.is_process_alive(state.pid, state.pid_create_time):
            raise HTTPException(status_code=409, detail="Server process is still running; profile was not deleted.")

    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Cannot delete this profile while a backup is running.")

    if profile.launch_dir:
        await asyncio.to_thread(workspace.release_world_links, profile)

    # Remove files before deleting the DB row so a failed disk cleanup is retryable.
    if delete_files:
        profile_dir = PROFILES_DIR / profile_id
        if profile_dir.is_symlink():
            raise HTTPException(status_code=400, detail="Profile storage cannot be a symbolic link.")
        try:
            profile_dir.resolve().relative_to(PROFILES_DIR.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Profile storage must remain inside the data directory.") from exc
        if profile_dir.exists():
            logger.warning(f"Deleting all files at {profile_dir}")
            try:
                if os.getenv("MCPANEL_BACKUPS_DIR"):
                    await asyncio.to_thread(backups.delete_backup_storage, profile_id)
                await asyncio.to_thread(shutil.rmtree, profile_dir)
            except OSError as e:
                raise HTTPException(status_code=500, detail="Could not delete profile files; profile was kept.") from e

    db.delete_profile(profile_id)
    logger.info(f"Deleted profile '{profile.name}' from database.")
            
    return None

@router.get("/partials/list")
def get_profile_list_partial(request: Request):
    """Returns just the HTML for the profile list (used by HTMX polling)."""
    all_profiles = db.list_profiles_with_states()
    profile_data = []
    for p, state in all_profiles:
        profile_data.append({
            "profile": p, "state": state, "backups": backups.list_backups(p.id),
            "backup_state": backups.backup_state(p.id), "has_icon": _profile_has_icon(p.id),
        })
        
    return templates.TemplateResponse(
        request=request, 
        name="partials/profile_list.html", 
        context={"data": profile_data}
    )

@router.get("/stats")
def get_dashboard_stats(request: Request):
    """Returns the HTML for the top stats bar."""
    profile_states = db.list_profiles_with_states()

    total = len(profile_states)
    running = 0
    ram = 0
    for p, s in profile_states:
        if s and s.status == ProfileStatus.RUNNING:
            running += 1
            ram += p.ram_mb
    
    html = f"""
    <div class="stat-card"><span class="stat-label">Total Servers</span><span class="stat-value">{total}</span></div>
    <div class="stat-card"><span class="stat-label">Running</span><span class="stat-value text-success">{running}</span></div>
    <div class="stat-card"><span class="stat-label">Allocated RAM</span><span class="stat-value text-primary-color">{ram} MB</span></div>
    """
    return HTMLResponse(content=html)

@router.get("/loaders")
async def get_available_loaders(build: BuildType, mc_version: str):
    """Returns a JSON list of available loader versions for the frontend."""
    try:
        versions = await loader.get_loader_versions(build, mc_version)
    except loader.LoaderInstallError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"versions": versions, "error": None if versions else "No compatible loader versions found."}


@router.get("/{profile_id}/settings")
def get_profile_settings(profile_id: str):
    """Fetch full settings only when the user opens the edit form."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Cannot delete this profile while a backup is running.")
    settings = profile.model_dump(mode="json", exclude={"rcon_password"})
    settings["server_replacement_pending"] = _replacement_pending(profile)
    settings["modpack_warnings"] = modpacks.loader_warnings(
        modpacks.get_pack(profile.modpack_name), profile.build.value
    )
    return settings


class ServerSettingsUpdate(BaseModel):
    properties: dict[str, str]


@router.get("/{profile_id}/server-settings")
def get_profile_server_settings(profile_id: str):
    """Editable server.properties values for the server settings tab."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    props_path = server_properties.properties_path(profile_id)
    server_installed = props_path.is_file() and not props_path.is_symlink()
    if server_installed:
        values = server_properties.read_properties(props_path)
    else:
        # The server has not been installed yet; show settings saved from a
        # previous edit so they are not silently lost before the first Start.
        values = server_properties.read_saved_settings(profile_id)
    state = db.get_runtime_state(profile_id)
    fields = []
    for key, spec in SERVER_SETTING_SPECS.items():
        # A curated setting is only "available" if it actually exists in the
        # running server's server.properties. Otherwise it is greyed out as
        # "not available on this version" instead of failing saves.
        available = (not server_installed) or (key in values)
        fields.append({
            "key": key,
            "category": spec.get("category", "gameplay"),
            "label": spec["label"],
            "description": spec.get("description", ""),
            "type": spec["type"],
            "value": values.get(key, spec["default"]),
            "options": spec.get("options"),
            "min": spec.get("min"),
            "max": spec.get("max"),
            "placeholder": spec.get("placeholder", ""),
            "available": available,
        })
    # Include any dynamic keys from the actual server.properties that aren't
    # in the curated spec, so settings added by mods or other loaders are
    # visible and editable for any version/build.
    known = set(SERVER_SETTING_SPECS) | set(MANAGED_SERVER_PROPERTIES)
    for key in sorted(values):
        if key not in known:
            text = str(values[key])
            field_type = "bool" if text.lower() in ("true", "false") else "int" if text.lstrip("-").isdigit() else "text"
            fields.append({
                "key": key,
                "category": "custom",
                "label": key,
                "description": "Setting read from server.properties (not part of the curated spec).",
                "type": field_type,
                "value": text,
                "options": None,
                "min": None,
                "max": None,
                "placeholder": "",
                "available": True,
            })
    return {
        "profile_id": profile_id,
        "profile_name": profile.name,
        "description": profile.description or "",
        "has_icon": _profile_has_icon(profile_id),
        "server_installed": server_installed,
        "server_running": _profile_server_running(state),
        "managed_keys": sorted(MANAGED_SERVER_PROPERTIES),
        "fields": fields,
    }


@router.put("/{profile_id}/server-settings")
async def update_profile_server_settings(profile_id: str, payload: ServerSettingsUpdate):
    """Saves editable server.properties values. Applies on the next server start."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Server settings cannot be changed while a backup is running.")
    if not isinstance(payload.properties, dict) or not payload.properties:
        raise HTTPException(status_code=422, detail="Provide at least one server setting to save.")

    # Reject blocked keys and malformed keys/values before touching anything.
    for key, value in payload.properties.items():
        clean_key = key.strip()
        raw_value = value or ""
        clean_value = raw_value.strip()
        if clean_key != key:
            raise HTTPException(
                status_code=422,
                detail=f"'{key}' contains leading or trailing whitespace and cannot be used as a setting key.",
            )
        if _is_blocked_setting_key(clean_key):
            raise HTTPException(
                status_code=422,
                detail=f"'{clean_key}' is a reserved or security-sensitive key and cannot be changed via settings.",
            )
        if any(c in clean_key for c in ("=", ":", " ", "\t")):
            raise HTTPException(
                status_code=422,
                detail=f"'{clean_key}' contains invalid character for a setting key.",
            )
        if _has_control_chars(clean_key):
            raise HTTPException(
                status_code=422,
                detail=f"'{clean_key}' contains invalid control characters.",
            )
        # Check control chars in both raw and stripped value (strip removes
        # leading/trailing control chars but they may still exist internally).
        if _has_control_chars(raw_value) or _has_control_chars(clean_value):
            raise HTTPException(
                status_code=422,
                detail=f"'{clean_key}' contains invalid control characters in its value.",
            )

    # Validate each key independently so one invalid key never blocks or
    # reverts the others.
    applied: dict[str, str] = {}
    rejected: dict[str, str] = {}
    for key, value in payload.properties.items():
        clean_value = (value or "").strip()
        spec = SERVER_SETTING_SPECS.get(key)
        if spec is not None:
            try:
                applied[key] = _validate_server_setting(key, spec, clean_value)
            except HTTPException as exc:
                rejected[key] = exc.detail
        else:
            # Dynamic key: not in the curated spec, but accept it so settings
            # added by mods or other loaders work for any version/build.
            if clean_value.lower() in ("true", "false"):
                applied[key] = clean_value.lower()
            elif clean_value.lstrip("-").isdigit():
                applied[key] = str(int(clean_value))
            else:
                applied[key] = clean_value

    if not applied:
        raise HTTPException(
            status_code=422,
            detail=f"No valid settings to save. Rejected: {rejected}",
        )

    # Persist the saved settings so they survive a wiped or not-yet-installed
    # server folder; the panel re-applies them whenever it writes server.properties.
    server_properties.merge_saved_settings(profile_id, applied)

    # Also merge into the existing server.properties so the file on disk matches
    # what the panel will apply on the next start.
    props_path = server_properties.properties_path(profile_id)
    if props_path.is_file() and not props_path.is_symlink():
        existing = server_properties.read_properties(props_path)
        existing.update(applied)
        server_properties.write_properties(props_path, existing)

    state = db.get_runtime_state(profile_id)
    logger.info("API: Saved %d server settings for profile '%s'", len(applied), profile.name)
    return {
        "saved": True,
        "applied": list(applied.keys()),
        "rejected": rejected,
        "applies_after_restart": _profile_server_running(state),
    }


class ServerIdentityUpdate(BaseModel):
    description: str = Field(default="", max_length=100)


@router.put("/{profile_id}/server-identity")
def update_profile_server_identity(profile_id: str, payload: ServerIdentityUpdate):
    """Saves the profile description shown on the card and used as the server MOTD."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Profile settings cannot be changed while a backup is running.")
    profile.description = (payload.description or "").strip()
    try:
        db.update_profile(profile)
    except db.DatabaseError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    logger.info("API: Updated description for profile '%s'", profile.name)
    return {
        "saved": True,
        "description": profile.description,
        "applies_after_restart": _profile_server_running(db.get_runtime_state(profile_id)),
    }


@router.get("/{profile_id}/icon")
def get_profile_icon(profile_id: str):
    """Serves the profile's 64x64 server icon for previews and cards."""
    if not db.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    source, live = _profile_icon_paths(profile_id)
    for path in (live, source):
        if path.is_file() and not path.is_symlink():
            return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="This profile has no server icon.")


@router.post("/{profile_id}/icon")
async def upload_profile_icon(profile_id: str, icon: UploadFile = File(...)):
    """Stores a 64x64 PNG as the profile's server icon."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Profile settings cannot be changed while a backup is running.")
    data = await icon.read(SERVER_ICON_MAX_BYTES + 1)
    if len(data) > SERVER_ICON_MAX_BYTES:
        raise HTTPException(status_code=422, detail="The server icon must be smaller than 256 KB.")
    _validate_server_icon(data)
    source, live = _profile_icon_paths(profile_id)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    if live.parent.is_dir() and not live.parent.is_symlink():
        live.write_bytes(data)
    logger.info("API: Updated server icon for profile '%s'", profile.name)
    return {"saved": True}


@router.delete("/{profile_id}/icon", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_icon(profile_id: str):
    if not db.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="Profile settings cannot be changed while a backup is running.")
    source, live = _profile_icon_paths(profile_id)
    for path in (source, live):
        if path.is_symlink():
            raise HTTPException(status_code=400, detail="Profile storage cannot be a symbolic link.")
        path.unlink(missing_ok=True)
    logger.info("API: Removed server icon for profile '%s'", profile_id)
    return None


@router.post("/{profile_id}/mod-download-cache/clear", status_code=status.HTTP_204_NO_CONTENT)
async def clear_mod_download_cache(profile_id: str):
    """Forget cached downloads so the next manual start fetches them again."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    async with mod_storage.source_lock(profile_id):
        profile = db.get_profile(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        state = db.get_runtime_state(profile_id)
        if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise HTTPException(status_code=409, detail="Wait until the server lifecycle operation finishes.")
        skipped_links = [item for item in profile.mod_downloads if item.skip_on_start]
        if skipped_links:
            for item in skipped_links:
                item.skip_on_start = False
            db.update_profile(profile)
        cache_dir = mod_storage.profile_root(profile_id) / "downloaded_mods"
        if cache_dir.is_symlink():
            cache_dir.unlink(missing_ok=True)
        elif cache_dir.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, cache_dir)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Could not clear the mod download cache: {exc}") from exc
    return None


class BroadcastPayload(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    display_type: str = Field(default="chat", pattern="^(chat|title|actionbar)$")
    color: str = Field(default="gold", pattern="^(gold|yellow|red|green|aqua|light_purple|white|gray)$")
    bold: bool = False
    italic: bool = False
    prefix: str = Field(default="[Server]", max_length=50)


@router.post("/{profile_id}/broadcast")
async def send_profile_broadcast(profile_id: str, payload: BroadcastPayload):
    """Broadcasts a styled in-game announcement, screen title, or action bar notice to all players via RCON."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    state = db.get_runtime_state(profile_id)
    if not state or state.status != ProfileStatus.RUNNING:
        raise HTTPException(status_code=409, detail="The server must be running to send a live broadcast.")

    msg_text = payload.message.strip()
    if not msg_text:
        raise HTTPException(status_code=422, detail="Broadcast message cannot be empty.")

    prefix_text = (payload.prefix or "").strip()
    components = []
    if prefix_text:
        components.append({"text": f"{prefix_text} ", "color": "gold", "bold": True})

    msg_comp: dict[str, object] = {
        "text": msg_text,
        "color": payload.color,
    }
    if payload.bold:
        msg_comp["bold"] = True
    if payload.italic:
        msg_comp["italic"] = True
    components.append(msg_comp)

    raw_json = json.dumps(components)

    # Version fallbacks: /title does not exist before 1.8, and actionbar
    # title subcommand does not exist before 1.11.
    mc_version = _parse_mc_version(profile.mc_version)
    display_type = payload.display_type
    if mc_version < (1, 8):
        display_type = "chat"
    elif mc_version < (1, 11) and display_type == "actionbar":
        display_type = "title"

    if display_type == "chat":
        command = f"tellraw @a {raw_json}"
    elif display_type == "title":
        command = f"title @a title {raw_json}"
    elif display_type == "actionbar":
        command = f"title @a actionbar {raw_json}"
    else:
        command = f"tellraw @a {raw_json}"

    try:
        response = await rcon.send_command(
            "127.0.0.1",
            profile.rcon_port,
            profile.rcon_password,
            command,
        )
    except rcon.RCONError as exc:
        logger.warning("Broadcast failed for profile '%s': %s", profile.name, exc)
        raise HTTPException(status_code=502, detail=f"Could not deliver broadcast to server: {exc}") from exc

    logger.info("Broadcast sent on profile '%s' (%s): %s", profile.name, display_type, msg_text)
    return {
        "success": True,
        "display_type": display_type,
        "message": msg_text,
        "response": response,
    }


def _parse_gamerule_value(response: str, rule_type: str):
    m = re.search(r'(?:is currently set to:?\s*|[:=]\s*)(true|false|\d+)', response, re.IGNORECASE)
    if not m:
        m = re.search(r'\b(true|false|\d+)\b', response, re.IGNORECASE)
    if m:
        raw = m.group(1).lower()
        if rule_type == "bool":
            return raw == "true"
        elif rule_type == "int":
            try:
                return int(raw)
            except ValueError:
                return None
    return None


# Per-profile cache of resolved gamerule alias keys: (profile_id, canonical_key) -> working_alias
_gamerule_alias_cache: dict[tuple[str, str], str] = {}


@router.get("/{profile_id}/gamerules")
async def get_profile_gamerules(profile_id: str):
    """Fetches in-game gamerules via RCON when the server is running."""
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    state = db.get_runtime_state(profile_id)
    server_running = bool(state and state.status == ProfileStatus.RUNNING)

    rule_items = []
    if not server_running:
        for key, spec in GAMERULE_SPECS.items():
            unavailable = spec.get("unavailable_on_modern", False) and _is_modern_minecraft(profile.mc_version)
            rule_items.append({
                "key": key,
                "label": spec["label"],
                "category": spec["category"],
                "description": spec["description"],
                "type": spec["type"],
                "default": spec["default"],
                "value": spec["default"],
                "min": spec.get("min"),
                "max": spec.get("max"),
                "available": not unavailable,
            })
        return {
            "profile_id": profile_id,
            "server_running": False,
            "rules": rule_items,
        }

    # Server is running -> query live gamerules via RCON batch.
    # Resolve aliases per rule based on version. Detect which alias works by
    # querying the server, not by guessing from version.
    alias_commands: list[str] = []
    cmd_to_canonical: dict[str, str] = {}
    for canonical, spec in GAMERULE_SPECS.items():
        # Skip rules unavailable on modern versions
        if spec.get("unavailable_on_modern", False) and _is_modern_minecraft(profile.mc_version):
            continue
        aliases = _gamerule_aliases(spec, profile.mc_version)
        for alias in aliases:
            cmd = f"gamerule {alias}"
            alias_commands.append(cmd)
            cmd_to_canonical[cmd] = canonical

    try:
        responses = await rcon.send_commands(
            "127.0.0.1",
            profile.rcon_port,
            profile.rcon_password,
            alias_commands,
            timeout=5.0,
        )
    except rcon.RCONError as exc:
        logger.warning("Failed to fetch gamerules for %s: %s", profile.name, exc)
        responses = [""] * len(alias_commands)

    # Command string -> index in responses
    cmd_to_index = {cmd: i for i, cmd in enumerate(alias_commands)}

    for canonical, spec in GAMERULE_SPECS.items():
        value = None
        available = False

        # Rules unavailable on modern versions
        if spec.get("unavailable_on_modern", False) and _is_modern_minecraft(profile.mc_version):
            rule_items.append({
                "key": canonical,
                "label": spec["label"],
                "category": spec["category"],
                "description": spec["description"],
                "type": spec["type"],
                "default": spec["default"],
                "value": spec["default"],
                "min": spec.get("min"),
                "max": spec.get("max"),
                "available": False,
            })
            continue

        for alias in _gamerule_aliases(spec, profile.mc_version):
            cmd = f"gamerule {alias}"
            idx = cmd_to_index.get(cmd)
            if idx is None:
                continue
            resp = responses[idx]
            parsed = _parse_gamerule_value(resp, spec["type"])
            if parsed is not None:
                value = parsed
                available = True
                # Cache the working alias for this profile + rule
                _gamerule_alias_cache[(profile_id, canonical)] = alias
                break

        # Apply invert flag when reading
        if available and spec.get("invert", False):
            if spec["type"] == "bool":
                value = not value
            elif spec["type"] == "int":
                int_value = value if isinstance(value, int) else 0
                value = spec["max"] - int_value if spec.get("max") is not None else -int_value

        rule_items.append({
            "key": canonical,
            "label": spec["label"],
            "category": spec["category"],
            "description": spec["description"],
            "type": spec["type"],
            "default": spec["default"],
            "value": value if value is not None else spec["default"],
            "min": spec.get("min"),
            "max": spec.get("max"),
            "available": available,
        })

    return {
        "profile_id": profile_id,
        "server_running": True,
        "rules": rule_items,
    }


class GameruleUpdate(BaseModel):
    value: str = Field(..., max_length=50)


@router.put("/{profile_id}/gamerules/{rule}")
async def update_profile_gamerule(profile_id: str, rule: str, payload: GameruleUpdate):
    """Updates an in-game gamerule in real time over RCON without requiring a restart.

    Resolves the correct alias key for the server's version, applies the
    invert flag when needed, and reads the value back after writing to confirm
    it stuck.
    """
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    state = db.get_runtime_state(profile_id)
    if not state or state.status != ProfileStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail="The server must be running to update gamerules. Changes apply in-game immediately without restart.",
        )

    spec = GAMERULE_SPECS.get(rule)
    if not spec:
        raise HTTPException(status_code=404, detail=f"'{rule}' is not a recognized gamerule.")

    val_str = payload.value.strip()
    typed_val: bool | int | str
    if spec["type"] == "bool":
        if val_str.lower() not in ("true", "false"):
            raise HTTPException(status_code=422, detail=f"Gamerule '{rule}' must be true or false.")
        typed_val = val_str.lower() == "true"
        # Apply invert flag when sending: disableRaids=true means raids=false
        if spec.get("invert", False):
            send_val = "true" if not typed_val else "false"
        else:
            send_val = val_str.lower()
    elif spec["type"] == "int":
        try:
            num = int(val_str)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Gamerule '{rule}' must be a whole number.") from exc
        if not (spec["min"] <= num <= spec["max"]):
            raise HTTPException(
                status_code=422,
                detail=f"Gamerule '{rule}' must be between {spec['min']} and {spec['max']}.",
            )
        typed_val = num
        if spec.get("invert", False) and spec.get("max") is not None:
            send_val = str(spec["max"] - num)
        else:
            send_val = str(num)
    else:
        send_val = val_str
        typed_val = val_str

    # Resolve aliases for this rule based on version
    aliases = _gamerule_aliases(spec, profile.mc_version)
    last_response = ""
    accepted_alias: str | None = None

    for alias in aliases:
        try:
            response = await rcon.send_command(
                "127.0.0.1",
                profile.rcon_port,
                profile.rcon_password,
                f"gamerule {alias} {send_val}",
            )
            last_response = response
            # Minecraft responds with the gamerule name when successful;
            # unknown/unavailable gamerules produce version-specific messages.
            lower = response.lower()
            is_failure = any(
                phrase in lower
                for phrase in (
                    "unknown gamerule",
                    "does not exist",
                    "no game rule called",
                    "is available",
                    "incorrect argument",
                    "unknown or incomplete command",
                )
            )
            if not is_failure:
                accepted_alias = alias
                break
        except rcon.RCONError as exc:
            logger.warning("RCON error setting gamerule %s via alias %s on %s: %s", rule, alias, profile.name, exc)
            last_response = str(exc)

    if accepted_alias is None:
        raise HTTPException(
            status_code=502,
            detail=f"Could not apply gamerule '{rule}' to server. Last response: {last_response}",
        )

    # Read back the value using each alias to confirm it stuck.
    # Try all aliases because the server may have renamed the gamerule
    # internally and only responds to a specific name.
    read_back_value = None
    read_back_alias: str | None = None
    for alias in aliases:
        try:
            read_response = await rcon.send_command(
                "127.0.0.1",
                profile.rcon_port,
                profile.rcon_password,
                f"gamerule {alias}",
                timeout=5.0,
            )
            read_parsed = _parse_gamerule_value(read_response, spec["type"])
            if read_parsed is not None:
                # Apply invert flag when reading back
                if spec.get("invert", False):
                    if spec["type"] == "bool":
                        read_parsed = not read_parsed
                    elif spec["type"] == "int" and spec.get("max") is not None:
                        read_parsed = spec["max"] - read_parsed
                if read_parsed == typed_val:
                    read_back_value = read_parsed
                    read_back_alias = alias
                    break
        except rcon.RCONError as exc:
            logger.warning("RCON error reading back gamerule %s via alias %s on %s: %s", rule, alias, profile.name, exc)

    if read_back_value is None:
        # The command may have been accepted but we couldn't verify it stuck.
        # Still report success if the set command was accepted, but flag it.
        logger.warning(
            "Gamerule '%s' set via alias '%s' but read-back verification failed",
            rule, accepted_alias,
        )
        return {
            "success": True,
            "rule": rule,
            "applied_alias": accepted_alias,
            "value": typed_val,
            "response": last_response,
            "read_back_verified": False,
        }

    # Cache the working alias for this profile + rule
    if read_back_alias is not None:
        _gamerule_alias_cache[(profile_id, rule)] = read_back_alias

    logger.info(
        "Updated live gamerule '%s' (alias '%s', read-back '%s') to '%s' on profile '%s'",
        rule, accepted_alias, read_back_alias, send_val, profile.name,
    )
    return {
        "success": True,
        "rule": rule,
        "applied_alias": accepted_alias,
        "read_back_alias": read_back_alias,
        "value": typed_val,
        "response": last_response,
        "read_back_verified": True,
    }


