"""Background Modrinth download, checksum, and manifest workflow."""

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import time
from urllib.parse import urlparse

import aiofiles
import httpx

from mcpanel import db
from mcpanel.core import mod_storage
from mcpanel.core.mod_job_state import JOBS
from mcpanel.core.mod_provider import provider
from mcpanel.core.mod_storage import hash_file, load_manifest, record_installed_mod
from mcpanel.models import ProfileStatus

logger = logging.getLogger(__name__)


def _failed_item(job, name, error):
    job["items"].append({"name": name, "status": "failed", "error": error})


def _safe_filename(raw_name):
    name = Path(raw_name).name
    if raw_name != name or "/" in raw_name or "\\" in raw_name or ":" in name or not name.lower().endswith(".jar") or name in {"", ".", ".."}:
        return None
    return name


async def _record_cached_mod(profile_id, job, version, file, filename, cache_path, sha256, sha1, sha512):
    async with mod_storage.source_lock(profile_id):
        state = db.get_runtime_state(profile_id)
        if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise RuntimeError("Source changes are blocked while the server is starting or stopping.")
        profile = db.get_profile(profile_id)
        if not profile:
            raise RuntimeError("The selected profile was deleted during installation.")
        if profile.mc_version != job.get("mc_version") or profile.build.value != job.get("loader"):
            raise RuntimeError("The profile version or loader changed during installation; retry for the updated profile.")
        await record_installed_mod(profile_id, {
            "sha256": sha256, "sha1": sha1, "sha512": sha512,
            "project_id": version.get("project_id"), "version_id": version.get("id"),
            "name": version.get("name"), "version": version.get("version_number"),
            "loaders": version.get("loaders", []),
            "game_versions": version.get("game_versions", []),
            "dependencies": version.get("dependencies", []),
            "filename": filename, "cache_filename": cache_path.name,
            "side": version.get("server_side", "unknown"),
            "installed_at": datetime.now(timezone.utc).isoformat(),
        })


async def _download_item(client, job, profile_id, cache_dir, version, job_id):
    file = next((item for item in version.get("files", []) if item.get("primary")), None)
    file = file or next(iter(version.get("files", [])), None)
    if not file:
        _failed_item(job, version.get("name", "mod"), "No downloadable JAR was provided.")
        return
    name = _safe_filename(file.get("filename", ""))
    if not name:
        _failed_item(job, file.get("filename", "mod"), "Unsafe mod filename.")
        return
    parsed = urlparse(file.get("url", ""))
    if parsed.scheme != "https" or not parsed.hostname:
        _failed_item(job, name, "Modrinth returned an invalid download URL.")
        return
    try:
        cache_dir.resolve().relative_to(mod_storage.profile_root(profile_id))
    except ValueError:
        _failed_item(job, name, "Mod cache path escapes profile storage.")
        return
    expected_hashes = {key: value.lower() for key, value in file.get("hashes", {}).items() if key in {"sha1", "sha512"} and value}
    expected = expected_hashes.get("sha512") or expected_hashes.get("sha1")
    if not expected:
        _failed_item(job, name, "Modrinth did not provide a checksum for this file.")
        return
    algorithms = ("sha256", "sha1", "sha512")
    primary_algorithm = "sha512" if "sha512" in expected_hashes else "sha1"
    for cached in cache_dir.glob("*.jar"):
        try:
            if cached.is_symlink():
                continue
            # Short-circuit on the strongest expected hash first — most cached
            # files are eliminated in a single read; the others get one more.
            if await hash_file(cached, primary_algorithm) != expected_hashes[primary_algorithm]:
                continue
            confirmed = True
            for key, value in expected_hashes.items():
                if key != primary_algorithm and await hash_file(cached, key) != value:
                    confirmed = False
                    break
            if confirmed:
                hashes = {key: await hash_file(cached, key) for key in algorithms}
                await _record_cached_mod(profile_id, job, version, file, name, cached, hashes["sha256"], hashes["sha1"], hashes["sha512"])
                entry = {"name": name, "status": "done", "progress": 100}
                job["items"].append(entry)
                return
        except OSError:
            continue
    part = cache_dir / f".{job_id}-{uuid.uuid4().hex}.part"
    destination = None
    entry = {"name": name, "status": "downloading", "progress": 0}
    job["items"].append(entry)
    digests = {key: hashlib.new(key) for key in algorithms}
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        async with client.stream("GET", file["url"]) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0) or 0)
            received = 0
            async with aiofiles.open(part, "xb") as output:
                async for chunk in response.aiter_bytes(1024 * 256):
                    await output.write(chunk)
                    for hasher in digests.values():
                        hasher.update(chunk)
                    received += len(chunk)
                    if total:
                        entry["progress"] = min(99, int(received * 100 / total))
        hashes = {key: hasher.hexdigest().lower() for key, hasher in digests.items()}
        if any(hashes[key] != value for key, value in expected_hashes.items()):
            raise ValueError("Modrinth checksum did not match.")
        destination = cache_dir / f"{hashes['sha256']}.jar"
        if destination.is_symlink():
            raise ValueError("The Modrinth cache destination cannot be a symbolic link.")
        if destination.exists() and await hash_file(destination, "sha256") != hashes["sha256"]:
            raise ValueError("A different file already uses this cache key.")
        if not destination.exists():
            os.link(part, destination)
        await _record_cached_mod(profile_id, job, version, file, name, destination, hashes["sha256"], hashes["sha1"], hashes["sha512"])
        entry.update(status="done", progress=100)
    except Exception as exc:
        part.unlink(missing_ok=True)
        if destination and destination.is_file() and not destination.is_symlink():
            try:
                manifest = await load_manifest(profile_id)
                if not any(item.get("cache_filename") == destination.name for item in manifest.get("mods", [])):
                    destination.unlink(missing_ok=True)
            except Exception:
                logger.warning("Could not clean up an unused failed Modrinth cache file %s", destination)
        entry.update(status="failed", error=str(exc))
    finally:
        part.unlink(missing_ok=True)


async def _download_job(job_id, profile_id, root_version, versions):
    job = JOBS[job_id]
    manifest = await load_manifest(profile_id)
    installed = {item.get("project_id") for item in manifest.get("mods", [])}
    additions = [root_version, *[item for item in versions if item.get("project_id") not in installed]]
    seen = set()
    unique_additions = []
    for item in additions:
        version_id = item.get("id")
        if version_id and version_id not in seen:
            seen.add(version_id)
            unique_additions.append(item)
    additions = unique_additions
    mods = mod_storage.modrinth_cache_dir(profile_id)
    job["status"] = "downloading"
    # Bounded read timeout so a stalled CDN connection fails the item instead
    # of hanging the job in "downloading" forever.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers=provider.headers,
        follow_redirects=True,
    ) as client:
        for version in additions:
            if not db.get_profile(profile_id):
                job.update(status="failed", error="The selected profile was deleted during installation.")
                return
            await _download_item(client, job, profile_id, mods, version, job_id)
    job["status"] = "failed" if any(item.get("status") == "failed" for item in job["items"]) else "done"


async def run_download_job(job_id, profile_id, version, versions):
    """Run one queued job and always timestamp its terminal state."""
    try:
        if not db.get_profile(profile_id):
            JOBS[job_id].update(status="failed", error="The selected profile was deleted before the download started.")
            return
        await _download_job(job_id, profile_id, version, versions)
    except Exception as exc:
        logger.exception("Mod install job %s failed", job_id)
        if job_id in JOBS:
            JOBS[job_id].update(status="failed", error=str(exc))
    finally:
        if job_id in JOBS:
            JOBS[job_id]["finished_at"] = time()
