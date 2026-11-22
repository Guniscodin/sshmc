"""Authenticated inventory and cleanup controls for panel-managed storage."""

import asyncio
import re
import shutil
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException

from mcpanel import db
from mcpanel.config import PROFILES_DIR
from mcpanel.core import backups, launch_dirs, modpacks, storage, mod_storage, workspace
from mcpanel.core.panel_logging import clear_panel_log_files
from mcpanel.core import process
from mcpanel.models import ProfileStatus

router = APIRouter(prefix="/api/storage", tags=["storage"])
_WORLD_FOLDERS = {"world", "world_nether", "world_the_end"}


def _profile(profile_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
        raise HTTPException(404, "Profile not found.")
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    return profile


def _profile_is_running(profile_id: str) -> bool:
    state = db.get_runtime_state(profile_id)
    if not state:
        return False
    if state.status in (ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING):
        return True
    return bool(state.pid and state.pid_create_time and process.is_process_alive(state.pid, state.pid_create_time))


def _claim_stopped_profile(profile_id: str):
    """Block lifecycle starts while stopped-profile files are being removed."""
    state = db.get_runtime_state(profile_id)
    if _profile_is_running(profile_id):
        raise HTTPException(409, "Stop this server before changing its stored files.")
    if not state:
        return None
    if state.status not in (ProfileStatus.STOPPED, ProfileStatus.CRASHED):
        raise HTTPException(409, "Wait until the server lifecycle operation finishes.")
    if not db.claim_runtime_state(
        profile_id, [ProfileStatus.STOPPED, ProfileStatus.CRASHED], ProfileStatus.STOPPING
    ):
        raise HTTPException(409, "The profile state changed. Refresh and try again.")
    return state.status


def _release_profile_claim(profile_id: str, previous_status) -> None:
    if previous_status is not None:
        db.claim_runtime_state(profile_id, [ProfileStatus.STOPPING], previous_status)


async def _finish_filesystem_action(function, *args):
    """Keep maintenance locks until worker-thread file changes have actually finished."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        finally:
            raise


def _safe_profile_server(profile_id: str) -> tuple[Path, Path]:
    profile_path = PROFILES_DIR / profile_id
    if profile_path.is_symlink():
        raise HTTPException(400, "Profile folder cannot be a symbolic link.")
    root = profile_path.resolve()
    try:
        root.relative_to(PROFILES_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(400, "Profile storage path is outside the data directory.") from exc
    server = root / "server"
    if server.is_symlink():
        raise HTTPException(400, "Profile server folder cannot be a symbolic link.")
    try:
        server.resolve().relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "Profile server folder is outside the profile directory.") from exc
    return root, server


@router.get("")
def get_storage():
    return storage.get_storage_snapshot()


@router.post("/refresh", status_code=202)
def refresh_storage():
    storage.request_storage_scan()
    return {"status": "scanning"}


@router.delete("/panel-logs")
def clear_panel_logs():
    try:
        removed = clear_panel_log_files()
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(500, f"Could not clear panel logs: {exc}") from exc
    storage.request_storage_scan()
    return {"removed": removed, "deleted_bytes": sum(item["size_bytes"] for item in removed)}


@router.delete("/profiles/{profile_id}/modrinth-cache")
async def clear_modrinth_cache(profile_id: str):
    _profile(profile_id)
    async with mod_storage.source_lock(profile_id):
        state = db.get_runtime_state(profile_id)
        if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise HTTPException(409, "Wait until the server lifecycle operation finishes.")
        root = mod_storage.profile_root(profile_id)
        cache = root / "modrinth_mods"
        if cache.is_symlink():
            cache.unlink(missing_ok=True)
        elif cache.exists():
            await _finish_filesystem_action(shutil.rmtree, cache)
        mod_storage.write_json_atomic(mod_storage.manifest_path(profile_id), {"mods": []})
    storage.request_storage_scan()
    return {"cleared": True, "restart_required": bool(state and state.status == ProfileStatus.RUNNING)}


@router.delete("/profiles/{profile_id}/logs")
async def clear_profile_logs(profile_id: str):
    profile = _profile(profile_id)
    root, server = _safe_profile_server(profile_id)
    # An assigned launch directory is where the server actually writes its logs.
    directories = [
        (server / "logs", server),
        (server / "crash-reports", server),
        (root / "logs", root),
    ]
    try:
        content_dir = workspace.server_dir(profile)
    except launch_dirs.LaunchDirError:
        content_dir = None
    if content_dir is not None and content_dir.resolve() != server.resolve():
        directories.extend([
            (content_dir / "logs", content_dir),
            (content_dir / "crash-reports", content_dir),
        ])

    def remove_logs():
        removed, failed = [], []
        for directory, allowed_root in directories:
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                directory.resolve().relative_to(allowed_root.resolve())
                entries = list(directory.iterdir())
            except (OSError, ValueError):
                continue
            for path in entries:
                name = str(path.relative_to(allowed_root))
                try:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode):
                        removed.append({"filename": name, "size_bytes": info.st_size})
                        path.unlink()
                except OSError:
                    failed.append(name)
        return removed, failed

    previous_status = _claim_stopped_profile(profile_id)
    try:
        removed, failed = await _finish_filesystem_action(remove_logs)
    finally:
        _release_profile_claim(profile_id, previous_status)
    storage.request_storage_scan()
    return {
        "removed": removed,
        "failed": failed,
        "deleted_bytes": sum(item["size_bytes"] for item in removed),
    }


@router.delete("/profiles/{profile_id}/worlds/{world_name}")
async def delete_profile_world(profile_id: str, world_name: str):
    _profile(profile_id)
    if world_name not in _WORLD_FOLDERS:
        raise HTTPException(400, "Unknown world folder.")
    _, server = _safe_profile_server(profile_id)
    world = server / world_name
    if world.is_symlink():
        raise HTTPException(400, "World folders cannot be symbolic links.")
    try:
        world.resolve().relative_to(server.resolve())
    except ValueError as exc:
        raise HTTPException(400, "World folder is outside the server directory.") from exc
    if not world.is_dir():
        raise HTTPException(404, "World folder not found.")
    previous_status = _claim_stopped_profile(profile_id)
    try:
        if backups.is_backup_running(profile_id):
            raise HTTPException(409, "Wait for the active backup before deleting a world folder.")
        await _finish_filesystem_action(shutil.rmtree, world)
    except OSError as exc:
        raise HTTPException(500, f"Could not delete world folder: {exc}") from exc
    finally:
        _release_profile_claim(profile_id, previous_status)
    storage.request_storage_scan()
    return {"deleted": world_name}


@router.delete("/profiles/{profile_id}/orphaned")
async def delete_orphaned_profile_files(profile_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
        raise HTTPException(404, "Unlinked profile data not found.")
    if db.get_profile(profile_id):
        raise HTTPException(409, "This storage belongs to an existing profile.")
    if backups.is_backup_running(profile_id):
        raise HTTPException(409, "Cannot remove files while a backup is running.")
    state = db.get_runtime_state(profile_id)
    if state and (
        state.status in (ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING)
        or (state.pid and state.pid_create_time and process.is_process_alive(state.pid, state.pid_create_time))
    ):
        raise HTTPException(409, "Cannot remove files while the server process is running.")
    target = PROFILES_DIR / profile_id
    if target.is_symlink():
        raise HTTPException(400, "Unlinked profile folders cannot be symbolic links.")
    try:
        target.resolve().relative_to(PROFILES_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(400, "Profile storage path is outside the data directory.") from exc
    if not target.is_dir():
        raise HTTPException(404, "Unlinked profile files not found.")
    try:
        await _finish_filesystem_action(backups.delete_backup_storage, profile_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"Could not remove orphaned backups: {exc}") from exc
    await _finish_filesystem_action(shutil.rmtree, target)
    storage.request_storage_scan()
    return {"deleted": profile_id}


@router.post("/uploads/cleanup", status_code=202)
async def cleanup_expired_uploads():
    await _finish_filesystem_action(modpacks.cleanup_stale_upload_sessions)
    storage.request_storage_scan()
    return {"status": "expired uploads removed"}
