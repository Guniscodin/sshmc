import asyncio
from contextlib import AsyncExitStack
import logging
import os
import shutil
import json
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, status, Form, Request
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
    all_profiles = db.list_profiles()
    profile_data = []
    for p in all_profiles:
        state = db.get_runtime_state(p.id)
        profile_data.append({
            "profile": p, "state": state, "backups": backups.list_backups(p.id),
            "backup_state": backups.backup_state(p.id),
        })
        
    return templates.TemplateResponse(
        request=request, 
        name="partials/profile_list.html", 
        context={"data": profile_data}
    )

@router.get("/stats")
def get_dashboard_stats(request: Request):
    """Returns the HTML for the top stats bar."""
    profiles = db.list_profiles()
    states = [db.get_runtime_state(p.id) for p in profiles]
    
    total = len(profiles)
    running = sum(1 for s in states if s and s.status == ProfileStatus.RUNNING)
    ram = sum(p.ram_mb for p, s in zip(profiles, states) if s and s.status == ProfileStatus.RUNNING)
    
    html = f"""
    <div class="stat-card"><span class="stat-label">Total Servers</span><span class="stat-value" style="color: white;">{total}</span></div>
    <div class="stat-card"><span class="stat-label">Running</span><span class="stat-value" style="color: var(--success);">{running}</span></div>
    <div class="stat-card"><span class="stat-label">Allocated RAM</span><span class="stat-value" style="color: var(--primary);">{ram} MB</span></div>
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
