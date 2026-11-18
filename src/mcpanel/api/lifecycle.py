import logging
from fastapi import APIRouter, HTTPException, Query
from mcpanel import db
from mcpanel.config import PROFILES_DIR
from mcpanel.models import ProfileStatus
from mcpanel.core import lifecycle, runtime, mod_downloads, backups, mod_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])

@router.get("/startup-events")
async def startup_events():
    return {"events": runtime.drain_startup_events()}

@router.post("/{profile_id}/cancel-start")
async def cancel_start_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    state = await runtime.cancel_startup(profile_id)
    if state is None:
        raise HTTPException(status_code=409, detail="This profile is not starting.")
    return {"status": state.status.value, "state": state}


def _remember_skipped_downloads(profile, indexes: list[int]) -> None:
    changed = False
    for index in set(indexes):
        if 0 <= index < len(profile.mod_downloads) and not profile.mod_downloads[index].skip_on_start:
            profile.mod_downloads[index].skip_on_start = True
            changed = True
    if changed:
        db.update_profile(profile)

@router.post("/{profile_id}/start")
async def start_profile(profile_id: str, skip_mod_download_indexes: list[int] = Query(default=[])):
    """Starts a stopped server."""
    profile = db.get_profile(profile_id)
    state = db.get_runtime_state(profile_id)
    
    if not profile or not state:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="This profile is backing up. Wait for the backup to finish.")
        
    if state.pid and state.pid_create_time and runtime.process.is_process_alive(state.pid, state.pid_create_time):
        raise HTTPException(status_code=409, detail="A server process is already running for this profile.")
    state.pid = None
    state.pid_create_time = None
    if not db.claim_runtime_state(
        profile_id, [ProfileStatus.STOPPED, ProfileStatus.CRASHED], ProfileStatus.STARTING
    ):
        raise HTTPException(status_code=409, detail="Server is already starting, running, or stopping.")
    if backups.is_backup_running(profile_id):
        db.claim_runtime_state(profile_id, [ProfileStatus.STARTING], ProfileStatus.STOPPED)
        raise HTTPException(status_code=409, detail="This profile is backing up. Wait for the backup to finish.")
    state.status = ProfileStatus.STARTING
    db.update_runtime_state(state)
    async with mod_storage.source_lock(profile_id):
        profile = db.get_profile(profile_id)
        if not profile:
            db.claim_runtime_state(profile_id, [ProfileStatus.STARTING], ProfileStatus.STOPPED)
            raise HTTPException(status_code=404, detail="Profile not found")

    try:
        # Run the pipeline
        _remember_skipped_downloads(profile, skip_mod_download_indexes)
        new_state = await lifecycle.restart_profile(
            profile, state, skip_mod_download_indexes=set(skip_mod_download_indexes)
        )
        new_state = db.get_runtime_state(profile_id) or new_state
        return {"status": new_state.status.value, "state": new_state}
        
    except mod_downloads.ModDownloadFailure as e:
        state = db.get_runtime_state(profile_id) or state
        state.status = ProfileStatus.STOPPED
        state.pid = None
        state.pid_create_time = None
        db.update_runtime_state(state)
        raise HTTPException(status_code=409, detail={
            "code": "mod_download_failed",
            "profile_id": profile_id,
            "index": e.index,
            "filename": e.filename,
            "message": str(e),
        }) from e
    except lifecycle.LifecycleError as e:
        logger.error(f"Start failed for {profile.name}: {e}")
        # Unlock the state and mark it as crashed so the UI updates
        state = db.get_runtime_state(profile_id) or state
        state.status = ProfileStatus.STOPPED if e.preflight or "replacement aborted because" in str(e).lower() else ProfileStatus.CRASHED
        if state.status == ProfileStatus.STOPPED:
            state.pid = None
            state.pid_create_time = None
        db.update_runtime_state(state)
        if e.preflight:
            try:
                relay_log = PROFILES_DIR / profile_id / "logs" / "latest.log"
                relay_log.parent.mkdir(parents=True, exist_ok=True)
                with open(relay_log, "a", encoding="utf-8") as f:
                    f.write(f"\n[SSHMC] Mod source preflight failed:\n")
                    for conflict in e.preflight.conflicts:
                        f.write(f"  - {conflict}\n")
                    for warning in e.preflight.warnings:
                        f.write(f"  [warning] {warning}\n")
            except Exception:
                pass
            raise HTTPException(status_code=409, detail={
                "code": "source_preflight_failed",
                "profile_id": profile_id,
                "message": str(e),
                "conflicts": e.preflight.conflicts,
                "issues_by_jar": e.preflight.issues_by_jar,
                "warnings": e.preflight.warnings,
            }) from e
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error starting {profile.name}: {e}")
        state = db.get_runtime_state(profile_id) or state
        state.status = ProfileStatus.CRASHED
        db.update_runtime_state(state)
        raise HTTPException(status_code=500, detail="Internal server error during startup.")

@router.post("/{profile_id}/stop")
async def stop_profile(profile_id: str):
    """Gracefully stops a running server."""
    profile = db.get_profile(profile_id)
    state = db.get_runtime_state(profile_id)
    
    if not profile or not state:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="This profile is backing up. Wait for the backup to finish.")
    if not db.claim_runtime_state(profile_id, [ProfileStatus.RUNNING], ProfileStatus.STOPPING):
        raise HTTPException(status_code=409, detail="Server is not in a stoppable state.")
    if backups.is_backup_running(profile_id):
        db.claim_runtime_state(profile_id, [ProfileStatus.STOPPING], ProfileStatus.RUNNING)
        raise HTTPException(status_code=409, detail="This profile is backing up. Wait for the backup to finish.")

    # Lock the state so people can't spam Stop
    state.status = ProfileStatus.STOPPING
    db.update_runtime_state(state)

    try:
        new_state = await runtime.stop_server(profile, state)
        db.update_runtime_state(new_state)
        return {"status": new_state.status.value, "state": new_state}
    except Exception as e:
        logger.error(f"Failed to stop server: {e}")
        # If stop fails, it might be crashed or hung. Mark as crashed.
        state.status = ProfileStatus.CRASHED
        db.update_runtime_state(state)
        raise HTTPException(status_code=500, detail="Failed to stop server.")

@router.post("/{profile_id}/restart")
async def restart_profile(
    profile_id: str,
    force_wipe: bool = False,
    skip_mod_download_indexes: list[int] = Query(default=[]),
):
    """Restarts a server, syncing git and optionally wiping the world."""
    profile = db.get_profile(profile_id)
    state = db.get_runtime_state(profile_id)
    
    if not profile or not state:
        raise HTTPException(status_code=404, detail="Profile not found")
    if backups.is_backup_running(profile_id):
        raise HTTPException(status_code=409, detail="This profile is backing up. Wait for the backup to finish.")
    if state.status == ProfileStatus.RUNNING:
        if not db.claim_runtime_state(profile_id, [ProfileStatus.RUNNING], ProfileStatus.STOPPING):
            raise HTTPException(status_code=409, detail="Server state changed; refresh and retry.")
        if backups.is_backup_running(profile_id):
            db.claim_runtime_state(profile_id, [ProfileStatus.STOPPING], ProfileStatus.RUNNING)
            raise HTTPException(status_code=409, detail="This profile is backing up. Wait for the backup to finish.")
        state.status = ProfileStatus.STOPPING
        try:
            state = await runtime.stop_server(profile, state)
        except Exception as e:
            logger.error(f"Could not stop {profile.name} before restart: {e}")
            state.status = ProfileStatus.CRASHED
            db.update_runtime_state(state)
            raise HTTPException(status_code=500, detail="Could not stop server before restart.") from e
        db.update_runtime_state(state)
        if state.pid and state.pid_create_time and runtime.process.is_process_alive(state.pid, state.pid_create_time):
            raise HTTPException(status_code=409, detail="Server is still running; restart cancelled.")
    elif state.status in [ProfileStatus.STOPPED, ProfileStatus.CRASHED]:
        if state.pid and state.pid_create_time and runtime.process.is_process_alive(state.pid, state.pid_create_time):
            raise HTTPException(status_code=409, detail="A server process is still running; stop it first.")
    else:
        raise HTTPException(status_code=409, detail="Server is already starting or stopping.")

    if not db.claim_runtime_state(profile_id, [ProfileStatus.STOPPED, ProfileStatus.CRASHED], ProfileStatus.STARTING):
        raise HTTPException(status_code=409, detail="Server state changed; refresh and retry.")
    if backups.is_backup_running(profile_id):
        db.claim_runtime_state(profile_id, [ProfileStatus.STARTING], ProfileStatus.STOPPED)
        raise HTTPException(status_code=409, detail="This profile is backing up. Wait for the backup to finish.")
    state.status = ProfileStatus.STARTING
    db.update_runtime_state(state)
    async with mod_storage.source_lock(profile_id):
        profile = db.get_profile(profile_id)
        if not profile:
            db.claim_runtime_state(profile_id, [ProfileStatus.STARTING], ProfileStatus.STOPPED)
            raise HTTPException(status_code=404, detail="Profile not found")

    try:
        _remember_skipped_downloads(profile, skip_mod_download_indexes)
        new_state = await lifecycle.restart_profile(
            profile,
            state,
            force_wipe=force_wipe,
            skip_mod_download_indexes=set(skip_mod_download_indexes),
        )
        new_state = db.get_runtime_state(profile_id) or new_state
        return {"status": new_state.status.value, "state": new_state}
        
    except mod_downloads.ModDownloadFailure as e:
        state = db.get_runtime_state(profile_id) or state
        state.status = ProfileStatus.STOPPED
        state.pid = None
        state.pid_create_time = None
        db.update_runtime_state(state)
        raise HTTPException(status_code=409, detail={
            "code": "mod_download_failed",
            "profile_id": profile_id,
            "index": e.index,
            "filename": e.filename,
            "message": str(e),
        }) from e
    except lifecycle.LifecycleError as e:
        logger.error(f"Restart failed for {profile.name}: {e}")
        state = db.get_runtime_state(profile_id) or state
        state.status = ProfileStatus.STOPPED if e.preflight or "replacement aborted because" in str(e).lower() else ProfileStatus.CRASHED
        if state.status == ProfileStatus.STOPPED:
            state.pid = None
            state.pid_create_time = None
        db.update_runtime_state(state)
        if e.preflight:
            try:
                relay_log = PROFILES_DIR / profile_id / "logs" / "latest.log"
                relay_log.parent.mkdir(parents=True, exist_ok=True)
                with open(relay_log, "a", encoding="utf-8") as f:
                    f.write(f"\n[SSHMC] Mod source preflight failed:\n")
                    for conflict in e.preflight.conflicts:
                        f.write(f"  - {conflict}\n")
                    for warning in e.preflight.warnings:
                        f.write(f"  [warning] {warning}\n")
            except Exception:
                pass
            raise HTTPException(status_code=409, detail={
                "code": "source_preflight_failed",
                "profile_id": profile_id,
                "message": str(e),
                "conflicts": e.preflight.conflicts,
                "issues_by_jar": e.preflight.issues_by_jar,
                "warnings": e.preflight.warnings,
            }) from e
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error restarting {profile.name}: {e}")
        state = db.get_runtime_state(profile_id) or state
        state.status = ProfileStatus.CRASHED
        db.update_runtime_state(state)
        raise HTTPException(status_code=500, detail="Internal server error during restart.")
