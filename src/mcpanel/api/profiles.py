import asyncio
import logging
import shutil
import json
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, status, Form, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from mcpanel.models import Profile, BuildType, ProfileStatus
from mcpanel.config import PROFILES_DIR
from mcpanel.core import runtime, loader, ports, process, modpacks, backups
from mcpanel import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/profiles", tags=["profiles"])

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


def _parse_mod_downloads(raw: str) -> list[dict]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Mod downloads must be valid JSON.") from exc
    if not isinstance(parsed, list):
        raise HTTPException(status_code=422, detail="Mod downloads must be a list of download links.")
    return parsed

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
    backup_worlds_on_change: bool = Form(False),
    backup_on_restart: bool = Form(True),
    exclude_regenerable_backup_folders: bool = Form(True)
):
    """Updates an existing profile."""
    existing = db.get_profile(profile_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Profile not found")
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
        requested_pack = modpacks.sanitize_pack_name(modpack_name) if modpack_name and modpack_name.strip() else None
        if requested_pack and not modpacks.get_pack(requested_pack):
            raise HTTPException(status_code=422, detail="The selected mod pack no longer exists.")
        
        updated_profile = Profile(
            id=profile_id,
            name=name,
            build=build,
            mc_version=mc_version,
            loader_version=loader_version,
            git_url=None if incompatible_change else clean_git_url,
            modpack_name=existing.modpack_name if incompatible_change else requested_pack,
            mod_downloads=[] if incompatible_change else parsed_mod_downloads,
            java_path=java_path,
            ram_mb=ram_mb,
            port=assigned_port,
            rcon_port=existing.rcon_port,
            rcon_password=existing.rcon_password,
            backup_worlds_on_change=backup_worlds_on_change,
            backup_on_restart=backup_on_restart,
            exclude_regenerable_backup_folders=exclude_regenerable_backup_folders,
            created_at=existing.created_at
        )
        
        db.update_profile(updated_profile)
        if incompatible_change:
            repo_dir = PROFILES_DIR / profile_id / "repo"
            if repo_dir.exists():
                await asyncio.to_thread(shutil.rmtree, repo_dir, True)
            download_cache = PROFILES_DIR / profile_id / "downloaded_mods"
            if download_cache.exists():
                await asyncio.to_thread(shutil.rmtree, download_cache, True)
        logger.info(f"API: Updated profile '{updated_profile.name}'")
        return updated_profile.model_dump(mode="json", exclude={"rcon_password"})
        
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


@router.delete("/{profile_id}/backups/{filename}", status_code=204)
def delete_profile_backup(profile_id: str, filename: str):
    if not db.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    try:
        backups.delete_backup(profile_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Backup not found") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return None

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

    # Remove files before deleting the DB row so a failed disk cleanup is retryable.
    if delete_files:
        profile_dir = PROFILES_DIR / profile_id
        if profile_dir.exists():
            logger.warning(f"Deleting all files at {profile_dir}")
            try:
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
        profile_data.append({"profile": p, "state": state, "backups": backups.list_backups(p.id)})
        
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
    settings = profile.model_dump(mode="json", exclude={"rcon_password"})
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
    state = db.get_runtime_state(profile_id)
    if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
        raise HTTPException(status_code=409, detail="Wait until the server lifecycle operation finishes.")
    skipped_links = [item for item in profile.mod_downloads if item.skip_on_start]
    if skipped_links:
        for item in skipped_links:
            item.skip_on_start = False
        db.update_profile(profile)
    cache_dir = PROFILES_DIR / profile_id / "downloaded_mods"
    if cache_dir.exists():
        try:
            await asyncio.to_thread(shutil.rmtree, cache_dir)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not clear the mod download cache: {exc}") from exc
    return None
