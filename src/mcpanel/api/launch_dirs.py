"""API for importing and assigning whole launch directories."""

import asyncio
import logging
import re
import shutil

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from mcpanel import db
from mcpanel.config import (
    MAX_MODPACK_EXTRACTED_BYTES,
    MAX_MODPACK_UPLOAD_BYTES,
    MAX_SINGLE_MOD_UPLOAD_BYTES,
    UPLOADS_DIR,
)
from mcpanel.core import launch_dirs
from mcpanel.models import ProfileStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/launch-dirs", tags=["launch-directories"])


class LaunchDirImportRequest(BaseModel):
    session_id: str
    name: str
    label: str | None = None
    mc_version: str
    build: str
    replace: bool = False

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value or ""):
            raise ValueError("Invalid upload session.")
        return value


class LaunchDirAddFilesRequest(BaseModel):
    session_id: str
    replace_files: bool = False

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value or ""):
            raise ValueError("Invalid upload session.")
        return value


def _ensure_dir_not_running(safe_name: str) -> None:
    assigned = launch_dirs.assigned_profile(safe_name)
    if assigned:
        state = db.get_runtime_state(assigned.id)
        if state and state.status in (ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise HTTPException(
                status_code=409,
                detail=f"'{safe_name}' is assigned to '{assigned.name}', which is running. Stop it before changing the directory.",
            )


def _discard_session(session_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
        return
    session = UPLOADS_DIR / session_id
    if session.is_symlink():
        return
    try:
        session.resolve().relative_to(UPLOADS_DIR.resolve())
    except (OSError, ValueError):
        return
    shutil.rmtree(session, ignore_errors=True)


@router.get("")
def list_launch_directories() -> dict:
    return {
        "directories": launch_dirs.list_launch_dirs(),
        "max_upload_bytes": MAX_MODPACK_UPLOAD_BYTES,
        "max_single_mod_upload_bytes": MAX_SINGLE_MOD_UPLOAD_BYTES,
        "max_extracted_bytes": MAX_MODPACK_EXTRACTED_BYTES,
    }


@router.post("/import")
async def import_launch_directory(payload: LaunchDirImportRequest) -> dict:
    try:
        safe_name = launch_dirs.sanitize_name(payload.name)
    except launch_dirs.LaunchDirError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _ensure_dir_not_running(safe_name)
    assigned = launch_dirs.assigned_profile(safe_name)
    if assigned and payload.replace:
        raise HTTPException(
            status_code=409,
            detail=f"'{safe_name}' is assigned to '{assigned.name}'. Reset that profile before replacing the directory.",
        )

    try:
        manifest = await asyncio.to_thread(
            launch_dirs.import_session,
            payload.session_id,
            payload.name,
            payload.mc_version,
            payload.build,
            payload.replace,
            payload.label,
        )
    except launch_dirs.LaunchDirExistsError as exc:
        raise HTTPException(status_code=409, detail={"code": "launch_dir_exists", "message": str(exc)}) from exc
    except launch_dirs.LaunchDirInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (launch_dirs.LaunchDirError, FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Launch directory import failed")
        raise HTTPException(status_code=500, detail="Could not import this directory.") from exc
    _discard_session(payload.session_id)
    return {"directory": manifest}


@router.post("/{name}/files")
async def add_files_to_directory(name: str, payload: LaunchDirAddFilesRequest) -> dict:
    try:
        safe_name = launch_dirs.sanitize_name(name)
    except launch_dirs.LaunchDirError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _ensure_dir_not_running(safe_name)

    try:
        manifest = await asyncio.to_thread(
            launch_dirs.add_files_to_launch_dir,
            payload.session_id,
            safe_name,
            payload.replace_files,
        )
        return {"directory": manifest}
    except launch_dirs.LaunchDirFileConflicts as exc:
        raise HTTPException(status_code=409, detail={
            "code": "file_conflicts",
            "filenames": exc.filenames,
            "message": str(exc),
        }) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Launch directory not found.") from exc
    except (launch_dirs.LaunchDirError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to add files to launch directory")
        raise HTTPException(status_code=500, detail="Could not add files to this directory.") from exc


@router.delete("/{name}/files/{file_path:path}")
@router.delete("/{name}/files")
async def delete_launch_directory_file(name: str, file_path: str = "", path: str | None = None) -> dict:
    target_path = file_path or path or ""
    if not target_path:
        raise HTTPException(status_code=400, detail="Path is required.")
    try:
        safe_name = launch_dirs.sanitize_name(name)
    except launch_dirs.LaunchDirError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _ensure_dir_not_running(safe_name)

    try:
        manifest = await asyncio.to_thread(
            launch_dirs.delete_launch_dir_file,
            safe_name,
            target_path,
        )
        return {"directory": manifest}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (launch_dirs.LaunchDirError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to delete file from launch directory")
        raise HTTPException(status_code=500, detail="Could not delete file from this directory.") from exc


@router.delete("/{name}", status_code=204)
async def remove_launch_directory(name: str) -> None:
    try:
        safe_name = launch_dirs.sanitize_name(name)
        await asyncio.to_thread(launch_dirs.delete_launch_dir, safe_name)
    except launch_dirs.LaunchDirInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Launch directory not found.") from exc
    except (launch_dirs.LaunchDirError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
