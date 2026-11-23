"""Whitelist and operator management for a profile's Minecraft server.

A running server owns ``whitelist.json`` and ``ops.json``: the only safe way to
change them is through RCON, because the server rewrites the files from memory.
A stopped server is edited directly on disk, which also works before the first
start. This module keeps that split (and the UUID resolution behind it) in one
place so the API layer stays a thin translator.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid as uuid_module
from pathlib import Path

import httpx
from fastapi import HTTPException

from mcpanel import db
from mcpanel.core import launch_dirs, mod_storage, rcon, server_properties, workspace
from mcpanel.models import Profile, ProfileStatus

logger = logging.getLogger(__name__)

# Java usernames are 3-16 chars: letters, digits, underscore only.
# Spaces are excluded because the name is interpolated into an RCON command.
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}\Z")
OP_LEVEL_MIN, OP_LEVEL_MAX = 1, 4
MAX_PLAYER_FILE_BYTES = 2 * 1024 * 1024
WHITELIST_FILE = "whitelist.json"
OPS_FILE = "ops.json"

_MOJANG_ENDPOINTS = (
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}",
    "https://api.mojang.com/users/profiles/minecraft/{name}",
)
_uuid_cache: dict[str, tuple[str, str]] = {}
_locks: dict[str, asyncio.Lock] = {}


def parse_name(name: str) -> str:
    """Validate and trim a player name before it reaches RCON or a JSON file."""
    text = (name or "").strip()
    if not PLAYER_NAME_RE.fullmatch(text):
        raise HTTPException(
            status_code=422,
            detail="Player names may only contain letters, numbers and underscore (3-16 characters).",
        )
    return text


def parse_level(level: int | None) -> int:
    if level is None:
        raise HTTPException(status_code=422, detail="Operator level must be a whole number from 1 to 4.")
    try:
        value = int(level)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Operator level must be a whole number from 1 to 4.") from exc
    if not OP_LEVEL_MIN <= value <= OP_LEVEL_MAX:
        raise HTTPException(status_code=422, detail="Operator level must be between 1 and 4.")
    return value


def _dashed(value) -> str:
    """Normalise a UUID (dashed or undashed) to lowercase dashed form."""
    try:
        return str(uuid_module.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return ""


def _server_dir(profile_id: str) -> Path:
    try:
        server = workspace.server_dir_for_id(profile_id)
    except (FileNotFoundError, launch_dirs.LaunchDirError, OSError):
        server = mod_storage.profile_root(profile_id) / "server"
    if server.is_symlink():
        raise HTTPException(status_code=400, detail="The server directory cannot be a symbolic link.")
    return server


def _player_file(profile_id: str, filename: str) -> Path:
    path = _server_dir(profile_id) / filename
    if path.is_symlink():
        raise HTTPException(status_code=400, detail=f"{filename} cannot be a symbolic link.")
    return path


def _read_entries(profile_id: str, filename: str) -> list[dict]:
    path = _player_file(profile_id, filename)
    try:
        if path.stat().st_size > MAX_PLAYER_FILE_BYTES:
            raise HTTPException(status_code=500, detail=f"{filename} is unexpectedly large; refusing to edit it.")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read {filename}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"{filename} is not valid JSON. Fix it (or restore a backup) before managing players.",
        ) from exc
    if not isinstance(data, list) or any(not isinstance(entry, dict) for entry in data):
        raise HTTPException(
            status_code=500,
            detail=f"{filename} does not contain a list of player records. Fix it before managing players.",
        )
    return data


def _write_entries(profile_id: str, filename: str, entries: list[dict]) -> None:
    path = _player_file(profile_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid_module.uuid4().hex}.part")
    try:
        with temp.open("x", encoding="utf-8") as output:
            json.dump(entries, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _online_mode(profile_id: str) -> bool:
    props_path = server_properties.properties_path(profile_id)
    properties = server_properties.read_properties(props_path)
    if "online-mode" not in properties:
        properties.update(server_properties.read_saved_settings(profile_id))
    return (properties.get("online-mode", "true") or "true").strip().lower() != "false"


def _default_op_level(profile_id: str) -> int:
    """Return the server's configured op-permission-level (default 4)."""
    props_path = server_properties.properties_path(profile_id)
    properties = server_properties.read_properties(props_path)
    if "op-permission-level" not in properties:
        properties.update(server_properties.read_saved_settings(profile_id))
    try:
        return int(properties.get("op-permission-level", "4") or "4")
    except (TypeError, ValueError):
        return 4


def offline_uuid(name: str) -> str:
    """Java's UUID.nameUUIDFromBytes("OfflinePlayer:<name>") equivalent."""
    digest = hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest()
    return str(uuid_module.UUID(bytes=digest, version=3))


def _usercache_uuid(profile_id: str, name: str) -> tuple[str, str] | None:
    path = _server_dir(profile_id) / "usercache.json"
    try:
        if path.is_symlink():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
            uid = _dashed(entry.get("uuid"))
            if uid:
                return uid, str(entry.get("name"))
    return None


def _mojang_lookup(name: str) -> tuple[str, str] | None:
    """Resolve an online-mode username. Returns (uuid, canonical_name)."""
    cached = _uuid_cache.get(name.lower())
    if cached:
        return cached
    with httpx.Client(timeout=6.0, follow_redirects=True) as client:
        for template in _MOJANG_ENDPOINTS:
            try:
                response = client.get(template.format(name=name))
            except httpx.HTTPError as exc:
                logger.warning("Mojang lookup failed for %s: %s", name, exc)
                continue
            if response.status_code == 204 or response.status_code == 404:
                return None
            if response.status_code == 429:
                raise HTTPException(
                    status_code=503,
                    detail="Mojang is rate-limiting profile lookups. Wait a minute and try again.",
                )
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            uid = _dashed(payload.get("id"))
            canonical = str(payload.get("name") or name)
            if uid:
                _uuid_cache[name.lower()] = (uid, canonical)
                return uid, canonical
    raise HTTPException(
        status_code=502,
        detail="Could not look this player up with Mojang. Check the spelling, or start the server and add them from there.",
    )


def _resolve_uuid(profile_id: str, name: str, online_mode: bool) -> tuple[str, str]:
    """Return (uuid, canonical_name) for a player that is not in a player file yet."""
    if not online_mode:
        return offline_uuid(name), name
    known = _usercache_uuid(profile_id, name)
    if known:
        return known
    found = _mojang_lookup(name)
    if found:
        return found
    raise HTTPException(
        status_code=404,
        detail=f"Mojang has no account named '{name}'. Check the spelling, or add them after they have joined.",
    )


def _match(entries: list[dict], name: str) -> dict | None:
    """Match a stored display name exactly.

    Display names are case-sensitive in the panel: "Gunornot" and "gunornot"
    are different entries, and changing only the capitalization of an entry is
    a rename, not a duplicate.
    """
    for entry in entries:
        if str(entry.get("name", "")) == name:
            return entry
    return None


def _match_ci(entries: list[dict], name: str) -> dict | None:
    """Case-insensitive name match, used only when reading what the live server wrote.

    Minecraft resolves online accounts case-insensitively and may write the
    account's canonical spelling, so command output cannot be matched exactly.
    """
    folded = name.lower()
    for entry in entries:
        if str(entry.get("name", "")).lower() == folded:
            return entry
    return None


def _match_uuid(entries: list[dict], uid: str) -> dict | None:
    dashed = _dashed(uid)
    if not dashed:
        return None
    for entry in entries:
        if _dashed(entry.get("uuid")) == dashed:
            return entry
    return None


def _operator_level(entry: dict) -> int:
    try:
        level = int(entry.get("level", 4))
    except (TypeError, ValueError):
        return 4
    return level if OP_LEVEL_MIN <= level <= OP_LEVEL_MAX else 4


def _phase(profile_id: str) -> str:
    state = db.get_runtime_state(profile_id)
    if not state:
        return "stopped"
    return state.status.value if isinstance(state.status, ProfileStatus) else str(state.status)


async def _rcon(profile: Profile, command: str) -> str:
    try:
        return await rcon.send_command(
            "127.0.0.1", profile.rcon_port, profile.rcon_password, command
        )
    except rcon.RCONError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The server is running but its console is unreachable ({exc}). Try again in a moment.",
        ) from exc


async def _wait_for_entry(profile_id: str, filename: str, name: str) -> dict | None:
    """The server writes player files synchronously with the command, but give
    slow disks a short window before calling the command failed."""
    for _ in range(6):
        entry = _match_ci(await asyncio.to_thread(_read_entries, profile_id, filename), name)
        if entry is not None:
            return entry
        await asyncio.sleep(0.1)
    return None


async def _wait_for_rename(
    profile_id: str, filename: str, new_name: str, old_name: str, old_uuid
) -> dict | None:
    """Wait for the renamed record.

    Name matching alone is not enough: names are case-insensitive, and an
    offline-mode rename changes the UUID. Only accept a record that either
    belongs to a different player name, carries the requested spelling, or
    has a different UUID from the old one.
    """
    old_dashed = _dashed(old_uuid)
    for _ in range(8):
        for entry in await asyncio.to_thread(_read_entries, profile_id, filename):
            entry_name = str(entry.get("name", ""))
            if entry_name.lower() != new_name.lower():
                continue
            uid = _dashed(entry.get("uuid"))
            if (
                entry_name.lower() != old_name.lower()
                or (entry_name == new_name and entry_name != old_name)
                or (uid and old_dashed and uid != old_dashed)
            ):
                return entry
        await asyncio.sleep(0.1)
    return None


async def _wait_for_uuid_absent(profile_id: str, filename: str, uid) -> bool:
    dashed = _dashed(uid)
    if not dashed:
        return True
    for _ in range(6):
        entries = await asyncio.to_thread(_read_entries, profile_id, filename)
        if not any(_dashed(entry.get("uuid")) == dashed for entry in entries):
            return True
        await asyncio.sleep(0.1)
    return False


async def _rollback(profile: Profile, command: str) -> None:
    """Best-effort undo for a half-finished live rename."""
    try:
        await _rcon(profile, command)
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        logger.warning("Players: could not roll back '%s' on '%s': %s", command, profile.name, detail)


def _command_error(response: str, name: str, action: str) -> HTTPException:
    message = (response or "").strip() or f"The server did not {action} '{name}'."
    if "does not exist" in message.lower() or "not found" in message.lower():
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=502, detail=message)


def _whitelist_view(entry: dict) -> dict:
    return {"uuid": _dashed(entry.get("uuid")), "name": str(entry.get("name") or "")}


def _operator_view(entry: dict) -> dict:
    return {
        "uuid": _dashed(entry.get("uuid")),
        "name": str(entry.get("name") or ""),
        "level": _operator_level(entry),
        "bypasses_player_limit": bool(entry.get("bypassesPlayerLimit", False)),
    }


def read_state(profile: Profile) -> dict:
    """Everything the Players tab needs, read from the files the server owns."""
    props_path = server_properties.properties_path(profile.id)
    server_installed = props_path.is_file() and not props_path.is_symlink()
    properties = server_properties.read_properties(props_path)
    if not server_installed:
        properties.update(server_properties.read_saved_settings(profile.id))
    enabled = (properties.get("white-list", "false") or "false").strip().lower() == "true"
    phase = _phase(profile.id)

    whitelist = [
        _whitelist_view(entry) for entry in _read_entries(profile.id, WHITELIST_FILE)
        if _dashed(entry.get("uuid")) and entry.get("name")
    ]
    operators = [
        _operator_view(entry) for entry in _read_entries(profile.id, OPS_FILE)
        if _dashed(entry.get("uuid")) and entry.get("name")
    ]
    whitelist.sort(key=lambda item: item["name"].lower())
    operators.sort(key=lambda item: (-item["level"], item["name"].lower()))

    warnings = []
    if enabled and not whitelist:
        warnings.append("The whitelist is enabled but empty, so nobody can join. Add a player or turn it off.")
    if not server_installed:
        warnings.append("This server has not been installed yet. Player changes are saved and applied on first start.")

    return {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "server_installed": server_installed,
        "online_mode": (properties.get("online-mode", "true") or "true").strip().lower() != "false",
        "phase": phase,
        "server_running": phase == "running",
        "busy": phase in ("starting", "stopping"),
        "can_edit_bypass": phase in ("stopped", "crashed"),
        "whitelist": {"enabled": enabled, "players": whitelist},
        "operators": operators,
        "warnings": warnings,
    }


async def set_whitelist_enabled(profile: Profile, enabled: bool) -> dict:
    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")
        if phase == "running":
            await _rcon(profile, f"whitelist {'on' if enabled else 'off'}")

        def _persist() -> None:
            props_path = server_properties.properties_path(profile.id)
            if props_path.is_file() and not props_path.is_symlink():
                current = server_properties.read_properties(props_path).get("white-list")
                if (current or "").strip().lower() != ("true" if enabled else "false"):
                    server_properties.set_property(props_path, "white-list", "true" if enabled else "false")
            server_properties.merge_saved_settings(
                profile.id, {"white-list": "true" if enabled else "false"}
            )

        await asyncio.to_thread(_persist)
        logger.info(
            "Players: %s whitelist for profile '%s' (server %s)",
            "enabled" if enabled else "disabled", profile.name, phase,
        )
        return {"enabled": enabled, "applied": phase == "running"}


async def add_whitelist(profile: Profile, name: str) -> dict:
    name = parse_name(name)
    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")
        if phase == "running":
            response = await _rcon(profile, f"whitelist add {name}")
            entry = await _wait_for_entry(profile.id, WHITELIST_FILE, name)
            if entry is None:
                raise _command_error(response, name, "whitelist")
            # The server may store the account's canonical spelling. Keep the
            # spelling the user typed so the panel never silently changes case.
            if str(entry.get("name", "")) != name:
                def _patch_name() -> None:
                    entries = _read_entries(profile.id, WHITELIST_FILE)
                    target = _match_uuid(entries, entry.get("uuid")) or _match_ci(entries, name)
                    if target is not None:
                        target["name"] = name
                        _write_entries(profile.id, WHITELIST_FILE, entries)
                await asyncio.to_thread(_patch_name)
                entry["name"] = name
            logger.info("Players: added '%s' to the whitelist of '%s' (live)", name, profile.name)
            return _whitelist_view(entry)

        def _add() -> dict:
            entries = _read_entries(profile.id, WHITELIST_FILE)
            online = _online_mode(profile.id)
            existing = _match(entries, name)
            if existing is not None:
                return _whitelist_view(existing)
            uid, _canonical = _resolve_uuid(profile.id, name, online)
            uuid_match = _match_uuid(entries, uid)
            if uuid_match is not None:
                return _whitelist_view(uuid_match)
            # Store the exact spelling that was submitted; the UUID is what
            # Minecraft uses to identify the player.
            entry = {"uuid": uid, "name": name}
            entries.append(entry)
            _write_entries(profile.id, WHITELIST_FILE, entries)
            return _whitelist_view(entry)

        entry = await asyncio.to_thread(_add)
        logger.info("Players: added '%s' to the whitelist of '%s'", entry["name"], profile.name)
        return entry


async def remove_whitelist(profile: Profile, name: str) -> None:
    name = parse_name(name)
    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")
        if phase == "running":
            response = await _rcon(profile, f"whitelist remove {name}")
            if await _wait_for_entry(profile.id, WHITELIST_FILE, name) is not None:
                raise _command_error(response, name, "remove from the whitelist")
            # On older/legacy versions (1.13 and earlier), whitelist remove does
            # not kick online players, so kick them explicitly.
            await _rcon(profile, f"kick {name} Removed from whitelist")
            return

        def _remove() -> None:
            entries = _read_entries(profile.id, WHITELIST_FILE)
            target = _match(entries, name)
            if target is None:
                raise HTTPException(status_code=404, detail=f"'{name}' is not on the whitelist.")
            entries.remove(target)
            _write_entries(profile.id, WHITELIST_FILE, entries)

        await asyncio.to_thread(_remove)
        logger.info("Players: removed '%s' from the whitelist of '%s'", name, profile.name)


async def rename_whitelist(profile: Profile, name: str, new_name: str) -> dict:
    """Change the name on a whitelist entry.

    A Minecraft whitelist record is a UUID plus a display name, so a rename is
    a remove-and-add on the server side. For a live server the new name is
    added first; only after that succeeds is the old record removed, and the
    add is rolled back if the removal fails.
    """
    name = parse_name(name)
    new_name = parse_name(new_name)

    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")

        def _target() -> dict:
            entries = _read_entries(profile.id, WHITELIST_FILE)
            target = _match(entries, name)
            if target is None:
                raise HTTPException(status_code=404, detail=f"'{name}' is not on the whitelist.")
            conflict = _match(entries, new_name)
            if conflict is not None and conflict is not target:
                raise HTTPException(status_code=409, detail=f"'{new_name}' is already on the whitelist.")
            return target

        target = await asyncio.to_thread(_target)

        def _rename_entry() -> dict:
            entries = _read_entries(profile.id, WHITELIST_FILE)
            current = _match(entries, name)
            if current is None:
                raise HTTPException(status_code=404, detail=f"'{name}' is not on the whitelist.")
            conflict = _match(entries, new_name)
            if conflict is not None and conflict is not current:
                raise HTTPException(status_code=409, detail=f"'{new_name}' is already on the whitelist.")
            uid, _canonical = _resolve_uuid(profile.id, new_name, _online_mode(profile.id))
            uid_conflict = _match_uuid(entries, uid)
            if uid_conflict is not None and uid_conflict is not current:
                existing_name = uid_conflict.get("name") or new_name
                raise HTTPException(status_code=409, detail=f"'{existing_name}' is already on the whitelist.")
            current["uuid"] = uid
            current["name"] = new_name
            _write_entries(profile.id, WHITELIST_FILE, entries)
            return _whitelist_view(current)

        if new_name == name:
            return _whitelist_view(target)

        if phase == "running":
            if name.lower() == new_name.lower():
                # Minecraft treats names case-insensitively, so there is no
                # new record to add. Change the stored display name directly;
                # the server reloads it on its next restart.
                entry = await asyncio.to_thread(_rename_entry)
                logger.info("Players: renamed '%s' to '%s' on the whitelist of '%s' (live)", name, entry["name"], profile.name)
                return _whitelist_view(entry)
            response = await _rcon(profile, f"whitelist add {new_name}")
            entry = await _wait_for_rename(profile.id, WHITELIST_FILE, new_name, name, target.get("uuid"))
            if entry is None:
                raise _command_error(response, new_name, "whitelist")
            if str(entry.get("name", "")) != new_name:
                entry = await asyncio.to_thread(_rename_entry)
            elif _dashed(entry.get("uuid")) != _dashed(target.get("uuid")):
                response = await _rcon(profile, f"whitelist remove {name}")
                if not await _wait_for_uuid_absent(profile.id, WHITELIST_FILE, target.get("uuid")):
                    await _rollback(profile, f"whitelist remove {new_name}")
                    raise _command_error(response, name, "remove from the whitelist")
            logger.info("Players: renamed '%s' to '%s' on the whitelist of '%s' (live)", name, entry["name"], profile.name)
            return _whitelist_view(entry)

        entry = await asyncio.to_thread(_rename_entry)
        logger.info("Players: renamed '%s' to '%s' on the whitelist of '%s'", name, entry["name"], profile.name)
        return entry


async def add_operator(profile: Profile, name: str, level: int | None) -> dict:
    name = parse_name(name)
    level = parse_level(level)
    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")
        if phase == "running":
            default_level = _default_op_level(profile.id)
            if level != default_level:
                raise HTTPException(
                    status_code=409,
                    detail=f"The server is running. Vanilla /op always assigns the server's default op-permission-level ({default_level}). Stop the server to assign operator level {level}.",
                )
            response = await _rcon(profile, f"op {name}")
            entry = await _wait_for_entry(profile.id, OPS_FILE, name)
            if entry is None:
                raise _command_error(response, name, "op")
            # Patch the desired level directly into ops.json (vanilla /op uses
            # server default) and keep the submitted spelling.
            def _patch_add_level() -> None:
                entries = _read_entries(profile.id, OPS_FILE)
                target = _match_uuid(entries, entry.get("uuid")) or _match_ci(entries, name)
                if target is not None:
                    target["level"] = level
                    target["name"] = name
                    _write_entries(profile.id, OPS_FILE, entries)
            await asyncio.to_thread(_patch_add_level)
            entry["level"] = level
            entry["name"] = name
            logger.info("Players: made '%s' an operator (level %d) on '%s' (live)", name, level, profile.name)
            return _operator_view(entry)

        def _add() -> dict:
            entries = _read_entries(profile.id, OPS_FILE)
            online = _online_mode(profile.id)
            existing = _match(entries, name)
            if existing is not None:
                existing["level"] = level
                _write_entries(profile.id, OPS_FILE, entries)
                return _operator_view(existing)
            uid, _canonical = _resolve_uuid(profile.id, name, online)
            uid_match = _match_uuid(entries, uid)
            if uid_match is not None:
                uid_match["level"] = level
                _write_entries(profile.id, OPS_FILE, entries)
                return _operator_view(uid_match)
            entry = {
                "uuid": uid,
                "name": name,
                "level": level,
                "bypassesPlayerLimit": False,
            }
            entries.append(entry)
            _write_entries(profile.id, OPS_FILE, entries)
            return _operator_view(entry)

        entry = await asyncio.to_thread(_add)
        logger.info("Players: made '%s' an operator (level %d) on '%s'", entry["name"], level, profile.name)
        return entry


async def update_operator(
    profile: Profile,
    name: str,
    level: int | None = None,
    bypasses_player_limit: bool | None = None,
) -> dict:
    name = parse_name(name)
    if level is None and bypasses_player_limit is None:
        raise HTTPException(status_code=422, detail="Provide a level or a bypass setting to change.")
    if level is not None:
        level = parse_level(level)

    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")
        if phase == "running":
            if bypasses_player_limit is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Minecraft has no command for the bypass limit, so stop the server to change it.",
                )
            if level is not None:
                default_level = _default_op_level(profile.id)
                if level != default_level:
                    raise HTTPException(
                        status_code=409,
                        detail=f"The server is running. Vanilla /op always assigns the server's default op-permission-level ({default_level}). Stop the server to assign operator level {level}.",
                    )
            response = await _rcon(profile, f"op {name}")
            entry = await _wait_for_entry(profile.id, OPS_FILE, name)
            if entry is None:
                raise _command_error(response, name, "op")
            # Patch the desired level directly into ops.json (vanilla /op uses server default)
            def _patch_update_level() -> None:
                entries = _read_entries(profile.id, OPS_FILE)
                target = _match_uuid(entries, entry.get("uuid")) or _match_ci(entries, name)
                if target is not None:
                    target["name"] = name
                    if level is not None:
                        target["level"] = level
                    _write_entries(profile.id, OPS_FILE, entries)
            await asyncio.to_thread(_patch_update_level)
            entry["level"] = level
            entry["name"] = name
            return _operator_view(entry)

        def _update() -> dict:
            entries = _read_entries(profile.id, OPS_FILE)
            target = _match(entries, name)
            if target is None:
                raise HTTPException(status_code=404, detail=f"'{name}' is not an operator.")
            if level is not None:
                target["level"] = level
            if bypasses_player_limit is not None:
                target["bypassesPlayerLimit"] = bool(bypasses_player_limit)
            _write_entries(profile.id, OPS_FILE, entries)
            return _operator_view(target)

        entry = await asyncio.to_thread(_update)
        logger.info("Players: updated operator '%s' on '%s'", entry["name"], profile.name)
        return entry


async def rename_operator(profile: Profile, name: str, new_name: str) -> dict:
    """Change the name on an operator entry, keeping its level and bypass flag."""
    name = parse_name(name)
    new_name = parse_name(new_name)

    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")

        def _target() -> dict:
            entries = _read_entries(profile.id, OPS_FILE)
            target = _match(entries, name)
            if target is None:
                raise HTTPException(status_code=404, detail=f"'{name}' is not an operator.")
            conflict = _match(entries, new_name)
            if conflict is not None and conflict is not target:
                raise HTTPException(status_code=409, detail=f"'{new_name}' is already an operator.")
            return target

        target = await asyncio.to_thread(_target)

        def _rename_entry() -> dict:
            entries = _read_entries(profile.id, OPS_FILE)
            current = _match(entries, name)
            if current is None:
                raise HTTPException(status_code=404, detail=f"'{name}' is not an operator.")
            conflict = _match(entries, new_name)
            if conflict is not None and conflict is not current:
                raise HTTPException(status_code=409, detail=f"'{new_name}' is already an operator.")
            uid, _canonical = _resolve_uuid(profile.id, new_name, _online_mode(profile.id))
            uid_conflict = _match_uuid(entries, uid)
            if uid_conflict is not None and uid_conflict is not current:
                existing_name = uid_conflict.get("name") or new_name
                raise HTTPException(status_code=409, detail=f"'{existing_name}' is already an operator.")
            current["uuid"] = uid
            current["name"] = new_name
            _write_entries(profile.id, OPS_FILE, entries)
            return _operator_view(current)

        if new_name == name:
            return _operator_view(target)

        if phase == "running":
            if target.get("bypassesPlayerLimit"):
                raise HTTPException(
                    status_code=409,
                    detail="Stop the server to rename this operator. The bypass-limit setting cannot be carried over the console.",
                )
            level = _operator_level(target)
            default_level = _default_op_level(profile.id)
            if level != default_level:
                raise HTTPException(
                    status_code=409,
                    detail=f"The server is running. Stop the server to rename an operator with a non-default level ({level}).",
                )
            if name.lower() == new_name.lower():
                # A capitalization change is not a new operator; patch the
                # stored display name and keep the current level.
                entry = await asyncio.to_thread(_rename_entry)
                logger.info("Players: renamed operator '%s' to '%s' on '%s' (live)", name, entry["name"], profile.name)
                return _operator_view(entry)
            response = await _rcon(profile, f"op {new_name}")
            entry = await _wait_for_rename(profile.id, OPS_FILE, new_name, name, target.get("uuid"))
            if entry is None:
                raise _command_error(response, new_name, "op")
            # Patch the preserved level and submitted spelling into ops.json
            def _patch_rename_level() -> None:
                entries = _read_entries(profile.id, OPS_FILE)
                t = _match_uuid(entries, entry.get("uuid")) or _match_ci(entries, new_name)
                if t is not None:
                    t["level"] = level
                    t["name"] = new_name
                    _write_entries(profile.id, OPS_FILE, entries)
            await asyncio.to_thread(_patch_rename_level)
            entry["level"] = level
            entry["name"] = new_name
            if _dashed(entry.get("uuid")) != _dashed(target.get("uuid")):
                response = await _rcon(profile, f"deop {name}")
                if not await _wait_for_uuid_absent(profile.id, OPS_FILE, target.get("uuid")):
                    await _rollback(profile, f"deop {new_name}")
                    raise _command_error(response, name, "deop")
            logger.info("Players: renamed operator '%s' to '%s' on '%s' (live)", name, entry["name"], profile.name)
            return _operator_view(entry)

        entry = await asyncio.to_thread(_rename_entry)
        logger.info("Players: renamed operator '%s' to '%s' on '%s'", name, entry["name"], profile.name)
        return entry


async def remove_operator(profile: Profile, name: str) -> None:
    name = parse_name(name)
    async with _locks.setdefault(profile.id, asyncio.Lock()):
        phase = _phase(profile.id)
        if phase in ("starting", "stopping"):
            raise HTTPException(status_code=409, detail=f"The server is {phase}. Try again when it has finished.")
        if phase == "running":
            response = await _rcon(profile, f"deop {name}")
            if await _wait_for_entry(profile.id, OPS_FILE, name) is not None:
                raise _command_error(response, name, "deop")
            return

        def _remove() -> None:
            entries = _read_entries(profile.id, OPS_FILE)
            target = _match(entries, name)
            if target is None:
                raise HTTPException(status_code=404, detail=f"'{name}' is not an operator.")
            entries.remove(target)
            _write_entries(profile.id, OPS_FILE, entries)

        await asyncio.to_thread(_remove)
        logger.info("Players: removed operator '%s' from '%s'", name, profile.name)
