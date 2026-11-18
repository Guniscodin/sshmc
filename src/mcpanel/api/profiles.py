import asyncio
from contextlib import AsyncExitStack
import logging
import os
import shutil
import json
import struct
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, status, Form, Request, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from mcpanel.models import Profile, BuildType, ProfileStatus, ModDownload
from mcpanel.config import PROFILES_DIR
from mcpanel.core import runtime, loader, ports, process, modpacks, backups, rcon, mod_storage, mod_downloads
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
    "online-mode": {
        "type": "bool", "default": "true", "label": "Online Mode",
        "description": "Verify players against Mojang's session servers. Turn off to allow offline-mode clients.",
    },
    "enforce-secure-profile": {
        "type": "bool", "default": "true", "label": "Enforce Secure Profiles",
        "description": "Require signed player chat and profiles. Keep off for offline-mode servers.",
    },
    "difficulty": {
        "type": "choice", "default": "easy", "label": "Difficulty",
        "options": ["peaceful", "easy", "normal", "hard"],
        "description": "Server difficulty applied to loaded worlds.",
    },
    "gamemode": {
        "type": "choice", "default": "survival", "label": "Default Game Mode",
        "options": ["survival", "creative", "adventure", "spectator"],
        "description": "Game mode for new players.",
    },
    "force-gamemode": {
        "type": "bool", "default": "false", "label": "Force Game Mode",
        "description": "Put joining players back into the default game mode.",
    },
    "hardcore": {
        "type": "bool", "default": "false", "label": "Hardcore",
        "description": "Spectator mode on death instead of respawn. Only affects new worlds.",
    },
    "pvp": {
        "type": "bool", "default": "true", "label": "PvP",
        "description": "Allow players to fight each other.",
    },
    "allow-flight": {
        "type": "bool", "default": "false", "label": "Allow Flight",
        "description": "Permit flying without false movement kicks (useful for some modded clients).",
    },
    "allow-nether": {
        "type": "bool", "default": "true", "label": "Allow Nether",
        "description": "Let players travel to the Nether.",
    },
    "enable-command-block": {
        "type": "bool", "default": "false", "label": "Command Blocks",
        "description": "Enable command blocks in loaded worlds.",
    },
    "spawn-monsters": {
        "type": "bool", "default": "true", "label": "Spawn Monsters",
        "description": "Spawn hostile mobs.",
    },
    "spawn-animals": {
        "type": "bool", "default": "true", "label": "Spawn Animals",
        "description": "Spawn passive animals.",
    },
    "spawn-npcs": {
        "type": "bool", "default": "true", "label": "Spawn Villagers",
        "description": "Spawn villagers (NPCs).",
    },
    "max-players": {
        "type": "int", "default": "20", "min": 1, "max": 100000, "label": "Max Players",
        "description": "Maximum number of players connected at the same time.",
    },
    "view-distance": {
        "type": "int", "default": "10", "min": 3, "max": 32, "label": "View Distance",
        "description": "Chunks sent to each player. Lower values reduce load.",
    },
    "simulation-distance": {
        "type": "int", "default": "10", "min": 5, "max": 32, "label": "Simulation Distance",
        "description": "Chunks that tick entities and blocks. Lower values reduce load.",
    },
    "spawn-protection": {
        "type": "int", "default": "16", "min": 0, "max": 256, "label": "Spawn Protection Radius",
        "description": "Blocks around world spawn only operators may edit. 0 disables protection.",
    },
    "op-permission-level": {
        "type": "int", "default": "4", "min": 0, "max": 4, "label": "OP Permission Level",
        "description": "Default permission level granted to operators.",
    },
}

MANAGED_SERVER_PROPERTIES = {"server-port", "enable-rcon", "rcon.port", "rcon.password", "motd"}


def _read_properties_file(path: Path) -> dict[str, str]:
    properties: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as props_file:
            for line in props_file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    properties[key.strip()] = value.strip()
    except OSError:
        return {}
    return properties


def _saved_server_settings_path(profile_id: str) -> Path:
    return PROFILES_DIR / profile_id / "server_settings.json"


def _read_saved_server_settings(profile_id: str) -> dict[str, str]:
    path = _saved_server_settings_path(profile_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _validate_server_setting(key: str, spec: dict, value: str) -> str:
    text = (value or "").strip()
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
    if text not in spec["options"]:
        raise HTTPException(status_code=422, detail=f"'{key}' must be one of: {', '.join(spec['options'])}.")
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
    return (
        PROFILES_DIR / profile_id / "server_icon.png",
        PROFILES_DIR / profile_id / "server" / "server-icon.png",
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
            mod_downloads=parsed_mod_downloads,
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
    java_path: str = Form("java"),
    ram_mb: int = Form(2048),
    port: Optional[int] = Form(None),
    backup_worlds_on_change: bool = Form(True),
    backup_mods_on_change: bool = Form(False),
    backup_config_on_change: bool = Form(False),
    backup_datapacks_on_change: bool = Form(False),
    backup_server_settings_on_change: bool = Form(False)
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
            mod_downloads=effective_downloads,
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
            db.update_profile(updated_profile)
            root = mod_storage.profile_root(profile_id)
            if not incompatible_change:
                retained = {mod_downloads.source_key(item, index) for index, item in enumerate(effective_download_models)}
                cache_root = root / "downloaded_mods"
                if cache_root.is_dir() and not cache_root.is_symlink():
                    for index, old_item in enumerate(existing.mod_downloads):
                        key = mod_downloads.source_key(old_item, index)
                        if key not in retained:
                            for cache_key in (key, mod_downloads.legacy_cache_key(old_item)):
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
    running = sum(1 for _, s in profile_states if s and s.status == ProfileStatus.RUNNING)
    ram = sum(p.ram_mb for p, s in profile_states if s and s.status == ProfileStatus.RUNNING)
    
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
    props_path = PROFILES_DIR / profile_id / "server" / "server.properties"
    server_installed = props_path.is_file() and not props_path.is_symlink()
    if server_installed:
        values = _read_properties_file(props_path)
    else:
        # The server has not been installed yet; show settings saved from a
        # previous edit so they are not silently lost before the first Start.
        values = _read_saved_server_settings(profile_id)
    state = db.get_runtime_state(profile_id)
    fields = []
    for key, spec in SERVER_SETTING_SPECS.items():
        fields.append({
            "key": key,
            "label": spec["label"],
            "description": spec.get("description", ""),
            "type": spec["type"],
            "value": values.get(key, spec["default"]),
            "options": spec.get("options"),
            "min": spec.get("min"),
            "max": spec.get("max"),
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
    updates: dict[str, str] = {}
    for key, value in payload.properties.items():
        spec = SERVER_SETTING_SPECS.get(key)
        if spec is None:
            raise HTTPException(status_code=422, detail=f"'{key}' is not an editable server setting.")
        updates[key] = _validate_server_setting(key, spec, value)

    # Persist the saved settings so they survive a wiped or not-yet-installed
    # server folder; the panel re-applies them whenever it writes server.properties.
    saved = _read_saved_server_settings(profile_id)
    saved.update(updates)
    settings_path = _saved_server_settings_path(profile_id)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(saved, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Also merge into the existing server.properties so the file on disk matches
    # what the panel will apply on the next start.
    props_path = PROFILES_DIR / profile_id / "server" / "server.properties"
    if props_path.is_file() and not props_path.is_symlink():
        existing = _read_properties_file(props_path)
        existing.update(updates)
        with open(props_path, "w", encoding="utf-8") as props_file:
            for prop_key, prop_value in existing.items():
                props_file.write(f"{prop_key}={prop_value}\n")

    state = db.get_runtime_state(profile_id)
    logger.info("API: Saved %d server settings for profile '%s'", len(updates), profile.name)
    return {"saved": True, "applies_after_restart": _profile_server_running(state)}


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
