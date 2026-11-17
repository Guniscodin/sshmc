import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import logging
import re
import threading
import uuid
import zipfile
import zlib
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil

import aiofiles
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from mcpanel import db
from mcpanel.config import (
    DATA_DIR,
    MAX_MODPACK_EXTRACTED_BYTES,
    MAX_MODPACK_UPLOAD_BYTES,
    MAX_SINGLE_MOD_UPLOAD_BYTES,
    UPLOADS_DIR,
)
from mcpanel.core import modpacks, mod_storage
from mcpanel.models import ProfileStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/modpacks", tags=["modpacks"])
_SESSION_UPLOAD_LOCK = asyncio.Lock()
_SESSION_RESERVED_BYTES: dict[str, int] = {}


def _ensure_pack_not_transitioning(name: str) -> str:
    try:
        safe_name = modpacks.sanitize_pack_name(name)
    except modpacks.ModPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for profile in db.list_profiles():
        if profile.modpack_name != safe_name:
            continue
        state = db.get_runtime_state(profile.id)
        if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise HTTPException(409, detail=f"Mod pack '{safe_name}' is linked to a profile that is starting or stopping. Wait for it to finish.")
    return safe_name


@asynccontextmanager
async def _pack_source_guard(name: str):
    safe_name = _ensure_pack_not_transitioning(name)
    async with mod_storage.pack_lock(safe_name):
        linked = sorted((profile for profile in db.list_profiles() if profile.modpack_name == safe_name), key=lambda item: item.id)
        async with AsyncExitStack() as stack:
            for profile in linked:
                await stack.enter_async_context(mod_storage.source_lock(profile.id))
            _ensure_pack_not_transitioning(safe_name)
            yield safe_name


class ImportRequest(BaseModel):
    upload_id: str
    name: str
    target_loader: str
    replace: bool = False

    @field_validator("upload_id")
    @classmethod
    def validate_upload_id(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("Invalid upload identifier.")
        return value


class FileImportRequest(BaseModel):
    session_id: str
    name: str
    target_loader: str
    replace: bool = False

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("Invalid upload session.")
        return value


class AddFilesRequest(BaseModel):
    session_id: str
    replace_filenames: bool = False

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("Invalid upload session.")
        return value


@router.get("")
def list_modpacks():
    return {
        "packs": modpacks.list_packs(),
        "max_upload_bytes": MAX_MODPACK_UPLOAD_BYTES,
        "max_single_mod_upload_bytes": MAX_SINGLE_MOD_UPLOAD_BYTES,
    }


def _safe_session_path(session_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise HTTPException(status_code=400, detail="Invalid upload session.")
    root = UPLOADS_DIR.resolve()
    try:
        root.relative_to(DATA_DIR.resolve())
        candidate = root / session_id
        if candidate.is_symlink():
            raise HTTPException(status_code=400, detail="Invalid upload session path.")
        session = candidate.resolve()
        session.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Upload storage must remain inside the data directory.") from exc
    return root, session


def _session_tree_bytes(session: Path) -> int:
    total = 0
    for current, directories, filenames in os.walk(session, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
        for filename in filenames:
            if filename.endswith(".part"):
                continue
            path = current_path / filename
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except OSError:
                continue
    return total


@router.post("/sessions/{session_id}/files")
async def upload_modpack_file(
    session_id: str,
    request: Request,
    filename: str,
    relative_path: str | None = None,
):
    root, session = _safe_session_path(session_id)
    if (
        filename in {"", ".", ".."} or ".." in filename or "/" in filename or "\\" in filename
        or Path(filename).is_absolute() or PureWindowsPath(filename).drive
    ):
        raise HTTPException(status_code=400, detail="The filename must be a plain name without path characters.")
    is_jar = filename.lower().endswith(".jar")
    normalized_path = ""
    if relative_path:
        posix = PurePosixPath(relative_path)
        windows = PureWindowsPath(relative_path)
        if posix.is_absolute() or windows.is_absolute() or windows.drive or "\\" in relative_path:
            raise HTTPException(status_code=400, detail="The uploaded path must be relative.")
        parts = tuple(part for part in posix.parts if part not in ("", "."))
        if not parts or any(part == ".." or ".." in part for part in parts):
            raise HTTPException(status_code=400, detail="The uploaded path contains an unsafe component.")
        if parts[-1] != filename:
            raise HTTPException(status_code=400, detail="The filename does not match the uploaded path.")
        if not is_jar:
            matching_root = next((index for index, part in enumerate(parts) if part in {"config", "datapacks"}), None)
            if matching_root is None:
                raise HTTPException(status_code=415, detail="Only JARs and files under config/ or datapacks/ are accepted.")
            parts = parts[matching_root:]
            normalized_path = "/".join(parts)
    if not is_jar and not normalized_path:
        raise HTTPException(status_code=415, detail="Only .jar files are accepted outside config/ and datapacks/.")

    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    max_file_bytes = MAX_SINGLE_MOD_UPLOAD_BYTES if is_jar else MAX_MODPACK_EXTRACTED_BYTES
    if content_length > max_file_bytes:
        raise HTTPException(status_code=413, detail="This file exceeds its configured upload limit.")

    relative_destination = Path("files") / (Path("mods") / filename if is_jar else Path(normalized_path))
    destination = session / relative_destination
    partial = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
    try:
        destination.resolve().relative_to(root)
        partial.resolve().relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Upload destination must remain inside data/uploads.") from exc

    async with _SESSION_UPLOAD_LOCK:
        session.mkdir(parents=True, exist_ok=True)
        session.resolve().relative_to(root)
        os.utime(session, None)
        stored_bytes = _session_tree_bytes(session)
        other_reserved_bytes = _SESSION_RESERVED_BYTES.get(session_id, 0)
        reservation = content_length or max_file_bytes
        if stored_bytes + other_reserved_bytes + reservation > MAX_MODPACK_EXTRACTED_BYTES:
            raise HTTPException(status_code=413, detail="This upload session exceeds the expanded pack-size limit.")
        _SESSION_RESERVED_BYTES[session_id] = _SESSION_RESERVED_BYTES.get(session_id, 0) + reservation
    size = 0
    try:
        current = session
        for part in relative_destination.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise HTTPException(status_code=400, detail="Upload staging cannot contain symbolic links.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise HTTPException(status_code=409, detail="A file with this staged path already exists.")
        async with aiofiles.open(partial, "xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_file_bytes:
                    raise HTTPException(status_code=413, detail="This file exceeds its configured upload limit.")
                if size > reservation:
                    async with _SESSION_UPLOAD_LOCK:
                        other_reserved = _SESSION_RESERVED_BYTES.get(session_id, 0) - reservation
                        if _session_tree_bytes(session) + other_reserved + size > MAX_MODPACK_EXTRACTED_BYTES:
                            raise HTTPException(status_code=413, detail="This upload session exceeds the expanded pack-size limit.")
                        _SESSION_RESERVED_BYTES[session_id] = other_reserved + size
                        reservation = size
                elif content_length and size > content_length:
                    async with _SESSION_UPLOAD_LOCK:
                        other_reserved = _SESSION_RESERVED_BYTES.get(session_id, 0) - reservation
                        if _session_tree_bytes(session) + other_reserved + size > MAX_MODPACK_EXTRACTED_BYTES:
                            raise HTTPException(status_code=413, detail="This upload session exceeds the expanded pack-size limit.")
                        _SESSION_RESERVED_BYTES[session_id] = other_reserved + size
                        reservation = size
                await output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        try:
            os.link(partial, destination)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="A file with this staged path already exists.") from exc
        partial.unlink()
        os.utime(session, None)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        async with _SESSION_UPLOAD_LOCK:
            _SESSION_RESERVED_BYTES[session_id] = max(0, _SESSION_RESERVED_BYTES.get(session_id, 0) - reservation)
            if not _SESSION_RESERVED_BYTES[session_id]:
                _SESSION_RESERVED_BYTES.pop(session_id, None)
    return {"session_id": session_id, "filename": filename, "relative_path": str(relative_destination), "size": size}


@router.delete("/sessions/{session_id}", status_code=204)
def cancel_upload_session(session_id: str):
    _, session = _safe_session_path(session_id)
    if session.exists():
        shutil.rmtree(session)
    return None


@router.post("/uploads")
async def upload_modpack(request: Request):
    filename = Path(request.headers.get("x-upload-filename", "mods.zip")).name[:180]
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=415, detail="Choose a .zip file.")
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > MAX_MODPACK_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The ZIP exceeds the configured upload-size limit.")

    uploads_root = UPLOADS_DIR.resolve()
    try:
        uploads_root.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Upload storage must remain inside the data directory.") from exc
    uploads_root.mkdir(parents=True, exist_ok=True)
    upload_id = request.headers.get("x-upload-id", "") or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload identifier.")
    destination = uploads_root / f"{upload_id}.zip"
    partial = uploads_root / f"{upload_id}.part"
    try:
        destination.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Upload storage must remain inside the data directory.") from exc
    if destination.exists() or destination.is_symlink() or partial.exists() or partial.is_symlink():
        raise HTTPException(status_code=409, detail="This upload identifier is already in use.")

    size = 0
    try:
        async with aiofiles.open(partial, "wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_MODPACK_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="The ZIP exceeds the configured upload-size limit.")
                await output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded ZIP is empty.")
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return {"upload_id": upload_id, "filename": filename, "size": size}


@router.delete("/uploads/{upload_id}", status_code=204)
def cancel_or_discard_upload(upload_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(status_code=404, detail="Upload not found.")
    uploads_root = UPLOADS_DIR.resolve()
    try:
        uploads_root.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Upload not found.") from exc
    path = uploads_root / f"{upload_id}.zip"
    path.unlink(missing_ok=True)
    (uploads_root / f"{upload_id}.part").unlink(missing_ok=True)
    return None


@router.post("/import")
async def import_modpack(payload: ImportRequest):
    if payload.replace:
        async with _pack_source_guard(payload.name):
            return await _import_modpack(payload)
    return await _import_modpack(payload)


async def _import_modpack(payload: ImportRequest):
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(
        modpacks.import_pack,
        payload.upload_id,
        payload.name,
        payload.target_loader,
        payload.replace,
        cancelled,
    ))
    try:
        pack = await worker
    except asyncio.CancelledError:
        cancelled.set()
        await asyncio.gather(worker, return_exceptions=True)
        (UPLOADS_DIR / f"{payload.upload_id}.zip").unlink(missing_ok=True)
        raise
    except modpacks.ModPackExists as exc:
        raise HTTPException(status_code=409, detail={"code": "pack_exists", "message": str(exc)}) from exc
    except (modpacks.ModPackError, FileNotFoundError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, NotImplementedError, RuntimeError) as exc:
        upload_path = UPLOADS_DIR / f"{payload.upload_id}.zip"
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (InterruptedError, OSError) as exc:
        (UPLOADS_DIR / f"{payload.upload_id}.zip").unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc) or "Mod pack import failed.") from exc
    except Exception as exc:
        (UPLOADS_DIR / f"{payload.upload_id}.zip").unlink(missing_ok=True)
        logger.exception("Mod pack import failed")
        raise HTTPException(status_code=500, detail="Could not import this mod pack.") from exc
    return {"pack": pack}


@router.post("/import-files")
async def import_modpack_files(payload: FileImportRequest):
    if payload.replace:
        async with _pack_source_guard(payload.name):
            return await _import_modpack_files(payload)
    return await _import_modpack_files(payload)


async def _import_modpack_files(payload: FileImportRequest):
    try:
        pack = await asyncio.to_thread(
            modpacks.import_file_session,
            payload.session_id,
            payload.name,
            payload.target_loader,
            payload.replace,
        )
        return {"pack": pack}
    except modpacks.ModPackExists as exc:
        raise HTTPException(status_code=409, detail={"code": "pack_exists", "message": str(exc)}) from exc
    except (modpacks.ModPackError, FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{pack_name}/files")
async def add_files_to_modpack(pack_name: str, payload: AddFilesRequest):
    async with _pack_source_guard(pack_name) as pack_name:
        return await _add_files_to_modpack(pack_name, payload)


async def _add_files_to_modpack(pack_name: str, payload: AddFilesRequest):
    try:
        pack = await asyncio.to_thread(
            modpacks.add_files_to_pack,
            payload.session_id,
            pack_name,
            payload.replace_filenames,
        )
        return {"pack": pack}
    except modpacks.ModPackJarConflicts as exc:
        raise HTTPException(status_code=409, detail={
            "code": "jar_conflicts",
            "filenames": exc.filenames,
            "message": str(exc),
        }) from exc
    except (modpacks.ModPackError, FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{pack_name}/jars/{filename}", status_code=204)
async def delete_modpack_jar(pack_name: str, filename: str):
    async with _pack_source_guard(pack_name) as pack_name:
        try:
            modpacks.delete_pack_jar(pack_name, filename)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Pack JAR not found.") from exc
        except (modpacks.ModPackError, OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{pack_name}")
async def remove_modpack(pack_name: str):
    async with _pack_source_guard(pack_name) as safe_name:
        linked = [profile for profile in db.list_profiles() if profile.modpack_name == safe_name]
        try:
            modpacks.delete_pack(safe_name)
        except (modpacks.ModPackError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        db.unlink_modpack(safe_name)
        running = [profile.name for profile in linked if (state := db.get_runtime_state(profile.id)) and state.status == ProfileStatus.RUNNING]
        return {"deleted": safe_name, "unlinked_profiles": len(linked), "running_profiles": running}
