"""Safe profile mod paths, hashes, and manifests."""

import asyncio
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

import aiofiles
from fastapi import HTTPException

from mcpanel import db
from mcpanel.config import PROFILES_DIR

_manifest_locks: dict[str, asyncio.Lock] = {}
_source_locks: dict[str, asyncio.Lock] = {}
_pack_locks: dict[str, asyncio.Lock] = {}


def profile_root(profile_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
        raise HTTPException(404, "Profile not found.")
    original = PROFILES_DIR / profile_id
    if original.is_symlink():
        raise HTTPException(400, "Profile data path cannot be a symbolic link.")
    root = original.resolve()
    try:
        root.relative_to(PROFILES_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(400, "Profile data path is invalid.") from exc
    return root


def get_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    return profile


def profile_mods_dir(profile_id: str) -> Path:
    root = profile_root(profile_id)
    server = root / "server"
    if server.is_symlink():
        raise HTTPException(400, "Profile server path cannot be a symbolic link.")
    server.mkdir(parents=True, exist_ok=True)
    mods = server / "mods"
    if mods.is_symlink():
        raise HTTPException(400, "Mods folder cannot be a symbolic link.")
    mods.mkdir(exist_ok=True)
    return mods


def modrinth_cache_dir(profile_id: str) -> Path:
    root = profile_root(profile_id)
    path = root / "modrinth_mods"
    if path.is_symlink():
        raise HTTPException(400, "Modrinth cache cannot be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(profile_id: str) -> Path:
    return profile_root(profile_id) / "mods.json"


def source_manifest_path(profile_id: str) -> Path:
    return profile_root(profile_id) / "owned_files.json"


def source_lock(profile_id: str) -> asyncio.Lock:
    return _source_locks.setdefault(profile_id, asyncio.Lock())


def pack_lock(pack_name: str) -> asyncio.Lock:
    return _pack_locks.setdefault(pack_name, asyncio.Lock())


async def hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    async with aiofiles.open(path, "rb") as source:
        while chunk := await source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def hash_file_sync(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_source_manifest(profile_id: str) -> dict:
    path = source_manifest_path(profile_id)
    if path.is_symlink():
        raise HTTPException(500, "The ownership manifest cannot be a symbolic link.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise ValueError("ownership manifest has an invalid shape")
        return data
    except FileNotFoundError:
        return {"files": []}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(500, "The ownership manifest is invalid; repair it before syncing sources.") from exc


def write_json_atomic(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temp.open("x", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_manifest(profile_id: str) -> dict:
    path = manifest_path(profile_id)
    if path.is_symlink():
        raise HTTPException(500, "The profile mods.json manifest cannot be a symbolic link.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("mods", []), list):
            raise ValueError("manifest root or mods list has the wrong shape")
        if any(not isinstance(item, dict) for item in data.get("mods", [])):
            raise ValueError("manifest contains an invalid mod record")
        return data
    except FileNotFoundError:
        return {"mods": []}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(500, "The profile mods.json manifest is invalid; repair it before managing mods.") from exc


async def load_manifest(profile_id: str) -> dict:
    return await asyncio.to_thread(_read_manifest, profile_id)


def _write_manifest(profile_id: str, manifest: dict) -> None:
    write_json_atomic(manifest_path(profile_id), manifest)


async def update_manifest(profile_id: str, update):
    lock = _manifest_locks.setdefault(profile_id, asyncio.Lock())
    async with lock:
        manifest = await load_manifest(profile_id)
        update(manifest)
        await asyncio.to_thread(_write_manifest, profile_id, manifest)


async def record_installed_mod(profile_id: str, item: dict):
    digest = item.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Modrinth manifest entries require a SHA-256 key.")

    def record(manifest):
        records = [entry for entry in manifest.get("mods", []) if entry.get("sha256") != digest]
        records.append({**item, "sha256": digest})
        manifest["mods"] = records

    await update_manifest(profile_id, record)


async def remove_manifest_mod(profile_id: str, digest: str) -> dict:
    removed = {}

    def remove(manifest):
        nonlocal removed
        removed = next((item for item in manifest.get("mods", []) if item.get("sha256") == digest), {})
        manifest["mods"] = [item for item in manifest.get("mods", []) if item.get("sha256") != digest]

    await update_manifest(profile_id, remove)
    return removed
