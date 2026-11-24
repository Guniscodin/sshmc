"""Modrinth browsing and safe profile mod installation routes."""
import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from time import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mcpanel import db
from mcpanel.core.mod_dependencies import resolve_required_dependencies
from mcpanel.core import mod_downloads, mod_legacy, modpacks, mod_storage
from mcpanel.core.mod_provider import provider
from mcpanel.core.mod_downloader import run_download_job
from mcpanel.core.mod_job_state import JOBS as _jobs, prune_jobs
from mcpanel.core.mod_storage import (
    get_profile as _profile,
    hash_file as _hash_file_async,
    load_manifest as _load_manifest,
    profile_mods_dir as _mods_dir,
    load_source_manifest,
    source_lock,
    remove_manifest_mod,
    modrinth_cache_dir,
)
from mcpanel.models import ProfileStatus

router = APIRouter(prefix="/api", tags=["mods"])
logger = logging.getLogger(__name__)


@router.get("/mods/search")
async def search_mods(q: str = "", profile_id: str = "", sort: str = "relevance", category: str = "", offset: int = 0, server_only: bool = True):
    profile = _profile(profile_id) if profile_id else None
    if profile and profile.build.value == "vanilla":
        return {"hits": [], "total_hits": 0, "unsupported": True}
    try:
        result = await provider.search(
            q[:100],
            profile.mc_version if profile else "",
            profile.build.value if profile else "",
            sort,
            category,
            max(0, min(offset, 10000)),
            server_only=server_only,
        )
        return result
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Modrinth search failed: %s", exc)
        raise HTTPException(502, "Modrinth search is temporarily unavailable.") from exc


@router.get("/mods/categories")
async def mod_categories():
    try:
        return await provider.categories()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not load Modrinth categories.") from exc


@router.get("/mods/project/{project_id}")
async def get_mod_project(project_id: str):
    try:
        return await provider.project(project_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(404 if exc.response.status_code == 404 else 502, "Modrinth project not found or unavailable.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not load this Modrinth project.") from exc


@router.get("/mods/project/{project_id}/versions")
async def get_project_versions(project_id: str, profile_id: str):
    profile = _profile(profile_id)
    if profile.build.value == "vanilla":
        return []
    try:
        return await provider.versions(project_id, profile.mc_version, profile.build.value)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not load compatible versions.") from exc


@router.get("/profiles/{profile_id}/mods")
async def list_profile_mods(profile_id: str):
    profile = _profile(profile_id)
    backfill_warnings = []
    state = db.get_runtime_state(profile_id)
    if not state or state.status not in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
        async with source_lock(profile_id):
            state = db.get_runtime_state(profile_id)
            if not state or state.status not in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
                if not profile.launch_dir:
                    # Imported launch directories own their JARs; adopting them as
                    # profile mods would rewrite pack content after a later reset.
                    backfill_warnings = await mod_legacy.identify_legacy(profile_id, _mods_dir(profile_id).parent)
                profile = _profile(profile_id)
    directory = _mods_dir(profile_id)
    manifest = await _load_manifest(profile_id)
    by_hash = {item.get("sha256"): item for item in manifest.get("mods", []) if isinstance(item, dict)}
    configured_link_keys = {mod_downloads.source_key(item, index) for index, item in enumerate(profile.mod_downloads)}
    owned = load_source_manifest(profile_id).get("files", [])
    owners = {item.get("path"): item for item in owned if isinstance(item, dict) and isinstance(item.get("path"), str)}
    result = []
    seen_hashes = set()
    unidentified = []
    for path in directory.glob("*.jar"):
        try:
            if path.is_symlink():
                continue
            sha256 = await _hash_file_async(path, "sha256")
            sha512 = await _hash_file_async(path, "sha512")
            item = by_hash.get(sha256, {})
            seen_hashes.add(sha256)
            owner = owners.get(f"mods/{path.name}", {}) or owners.get(path.name, {})
            source = owner.get("owner", "manual") if owner.get("sha256") == sha256 else "manual"
            if source == "manual" and sha256 in by_hash:
                source = "modrinth"
            manifest_pending = isinstance(item, dict) and bool(item.get("pending_removal"))
            pending_removal = manifest_pending or (source == "modrinth" and sha256 not in by_hash) or (source == "link" and owner.get("source_key") not in configured_link_keys)
            if not item and source != "modrinth":
                unidentified.append((sha512, path, path.stat().st_size, sha256, source, owner))
            else:
                result.append({"filename": path.name, "size": path.stat().st_size, "source": source, "sha256": sha256,
                               "pending_removal": pending_removal,
                               "remove_allowed": not manifest_pending and ((source == "modrinth" and sha256 in by_hash) or (source == "link" and owner.get("source_key") in configured_link_keys)), **item})
        except OSError:
            continue
    try:
        identified = await provider.identify_files(
            [digest for digest, *_ in unidentified], "sha512"
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not batch-identify manual mods for profile %s: %s", profile_id, exc)
        identified = provider.cached_file_identifications([digest for digest, *_ in unidentified], "sha512")
        if not identified:
            backfill_warnings.append(f"Modrinth hash lookup is unavailable; unidentified JARs remain manually managed ({exc}).")
    for digest, path, size, sha256, source, owner in unidentified:
        version = identified.get(digest)
        if not version:
            backfill_warnings.append(f"{path.name}: Modrinth could not identify this JAR; it remains manually managed.")
            item = {"side": "unknown", "manual": True}
        else:
            source = "modrinth"
            item = {
                "project_id": version.get("project_id"),
                "version_id": version.get("id"),
                "name": version.get("name"),
                "version": version.get("version_number"),
                "loaders": version.get("loaders", []),
                "dependencies": version.get("dependencies", []),
                "side": "unknown",
                "identified_by_hash": True,
            }
        result.append({"filename": path.name, "size": size, "sha256": sha256, "source": source,
                       "source_key": owner.get("source_key"),
                       "pending_removal": False,
                       "remove_allowed": (source == "modrinth" and sha256 in by_hash) or (source == "link" and owner.get("source_key") in configured_link_keys) or bool(version), **item})
    for item in manifest.get("mods", []):
        digest = item.get("sha256")
        if not digest or digest in seen_hashes:
            continue
        # Skip entries pending removal — they will be purged on next Start
        if item.get("pending_removal"):
            continue
        cache_name = item.get("cache_filename")
        cache_file = modrinth_cache_dir(profile_id) / cache_name if isinstance(cache_name, str) and Path(cache_name).name == cache_name else None
        if not cache_file or cache_file.is_symlink() or not cache_file.is_file():
            # Cache file is gone — flag as missing rather than silently hiding the entry
            result.append({**item, "filename": item.get("filename", digest), "size": 0,
                           "source": "modrinth", "remove_allowed": True, "missing_file": True})
            continue
        try:
            if await _hash_file_async(cache_file, "sha256") != digest:
                continue
            result.append({**item, "filename": item.get("filename", cache_file.name), "size": cache_file.stat().st_size,
                           "source": "modrinth", "remove_allowed": True, "pending_install": True})
        except OSError:
            continue
    state = db.get_runtime_state(profile_id)
    return {
        "mods": result,
        "running": bool(state and state.status == ProfileStatus.RUNNING),
        "transitioning": bool(state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING)),
        "build": profile.build.value,
        "mc_version": profile.mc_version,
        "warnings": list(dict.fromkeys(backfill_warnings)),
    }


@router.get("/profiles/{profile_id}/mods/{project_id}/plan")
async def install_plan(profile_id: str, project_id: str):
    """Lightweight install preview for hovers: required dependency count and titles.

    Does NOT resolve the dependency graph, hash files, or queue downloads —
    the confirm dialog remains the authoritative plan before anything is installed.
    """
    prune_jobs()
    profile = _profile(profile_id)
    if profile.build.value == "vanilla":
        return {"compatible": False, "reason": "Mods cannot be installed on a Vanilla profile.", "required": []}
    state = db.get_runtime_state(profile_id)
    if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
        return {"compatible": False, "reason": "Mod sources cannot be changed while the server is starting or stopping.", "required": []}
    try:
        candidates = await provider.versions(project_id, profile.mc_version, profile.build.value)
        if not candidates:
            return {"compatible": False, "reason": "No compatible version for this profile.", "required": []}
        version = candidates[0]
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not load compatible versions.") from exc
    required = [
        {"project_id": d.get("project_id"), "version_id": d.get("version_id")}
        for d in version.get("dependencies", []) if d.get("dependency_type") == "required"
    ]
    required_ids = [d["project_id"] for d in required if d["project_id"]]
    titles: dict[str, str | None] = {}
    try:
        if required_ids:
            titles = {item.get("id"): item.get("title") for item in await provider.projects(required_ids)}
    except httpx.HTTPError as exc:
        logger.warning("Could not fetch dependency titles for the preview: %s", exc)
    return {
        "compatible": True,
        "version_name": version.get("name"),
        "version_number": version.get("version_number"),
        "required": [
            {**d, "title": titles.get(d["project_id"]) or d["project_id"]}
            for d in required
        ],
    }


class InstallRequest(BaseModel):
    version_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    confirm: bool = False
    skip_unsupported: bool = False


@router.post("/profiles/{profile_id}/mods/install", status_code=202)
async def install_mod(profile_id: str, request: InstallRequest):
    prune_jobs()
    profile = _profile(profile_id)
    if profile.build.value == "vanilla":
        raise HTTPException(400, "Mods cannot be installed on a Vanilla profile.")
    state = db.get_runtime_state(profile_id)
    if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
        raise HTTPException(409, "Mod sources cannot be changed while the server is starting or stopping.")
    if not request.version_id and not request.project_id:
        raise HTTPException(400, "Provide either a version_id or a project_id.")
    try:
        if request.version_id:
            version = await provider.version(request.version_id)
        else:
            candidates = await provider.versions(request.project_id, profile.mc_version, profile.build.value)
            if not candidates:
                raise HTTPException(409, f"No compatible {profile.build.value} version is available for this mod.")
            version = candidates[0]
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not load that Modrinth version.") from exc
    if profile.mc_version not in version.get("game_versions", []) or profile.build.value not in version.get("loaders", []):
        raise HTTPException(409, "That mod version is incompatible with this profile.")
    resolved_dependencies = []
    manifest = await _load_manifest(profile_id)
    installed_projects = {
        item.get("project_id")
        for item in manifest.get("mods", [])
        if isinstance(item, dict) and not item.get("pending_removal")
    }
    reserved_projects = {
        project_id for job in _jobs.values()
        if job.get("profile_id") == profile_id and job.get("status") in {"queued", "downloading"}
        for project_id in job.get("project_ids", [job.get("project_id")]) if project_id
    }
    mods_dir = _mods_dir(profile_id)
    local_hashes = set()
    if version.get("dependencies"):
        base_mod_dir = None
        if profile.modpack_name:
            try:
                base_mod_dir = modpacks.get_pack_directory(profile.modpack_name) / "mods"
            except modpacks.ModPackError as exc:
                raise HTTPException(409, f"The linked mod pack is unavailable: {exc}") from exc
        elif profile.git_url:
            repo = mod_storage.profile_root(profile_id) / "repo"
            if not repo.is_symlink():
                base_mod_dir = repo / "mods"
        if base_mod_dir and not base_mod_dir.is_symlink() and base_mod_dir.is_dir():
            for jar in base_mod_dir.glob("*.jar"):
                if jar.is_symlink() or not jar.is_file():
                    continue
                try:
                    local_hashes.add(await _hash_file_async(jar, "sha1"))
                except OSError:
                    continue
    try:
        resolved_dependencies, dependency_metadata, skipped_dependencies = await resolve_required_dependencies(
            provider,
            version,
            profile.mc_version,
            profile.build.value,
            installed_projects | {version.get("project_id")},
            reserved_projects,
            local_hashes,
            {
                item.get("project_id"): item.get("version_id")
                for item in manifest.get("mods", [])
                if isinstance(item, dict) and not item.get("pending_removal") and item.get("project_id") and item.get("version_id")
            },
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(409, f"A required dependency is unavailable or incompatible: {exc}. No mods were installed.") from exc

    dependencies_out = [
        {
            "project_id": item.get("project_id"),
            "title": item.get("_project_title") or item.get("name") or item.get("project_id"),
            "version_name": item.get("name"),
            "version_number": item.get("version_number"),
            "size_bytes": next((f.get("size") or 0 for f in item.get("files") or []), 0),
            "icon_url": item.get("_project_icon"),
        }
        for item in resolved_dependencies
    ]
    satisfied_out = [
        {"project_id": s.get("project_id"), "version_id": s.get("version_id"), "reason": s.get("reason")}
        for s in skipped_dependencies
    ]
    # One batch lookup to give the "Already installed" list human-readable titles.
    satisfied_ids = [s["project_id"] for s in satisfied_out if s.get("project_id")]
    if satisfied_ids:
        try:
            titles = {item.get("id"): item.get("title") for item in await provider.projects(satisfied_ids)}
            for s in satisfied_out:
                if s.get("project_id"):
                    s["title"] = titles.get(s["project_id"])
        except httpx.HTTPError as exc:
            logger.warning("Could not fetch satisfied-dependency titles: %s", exc)
    unsupported = version.get("server_side") == "unsupported"
    if not request.confirm:
        return {"needs_confirmation": True, "mod": version, "dependencies": dependencies_out, "satisfied": satisfied_out, "unsupported_server": unsupported, "running": bool(state and state.status == ProfileStatus.RUNNING), "applies_on_next_restart": bool(state and state.status == ProfileStatus.RUNNING)}
    if unsupported and not request.skip_unsupported:
        raise HTTPException(409, "This mod does not support servers. Confirm skip_unsupported to install anyway.")
    for job in _jobs.values():
        if job.get("profile_id") == profile_id and job.get("project_id") == version.get("project_id") and job.get("status") in {"queued", "downloading"}:
            raise HTTPException(409, "This mod is already queued or downloading for this profile.")
    if version.get("project_id") in installed_projects:
        raise HTTPException(409, "This mod is already installed in this profile.")
    wanted_hashes = {file.get("hashes", {}).get("sha1", "").lower() for file in version.get("files", [])}
    wanted_hashes.discard("")
    if wanted_hashes:
        for jar in mods_dir.glob("*.jar"):
            try:
                if await _hash_file_async(jar, "sha1") in wanted_hashes:
                    raise HTTPException(409, "This mod file is already present in the profile's mods folder.")
            except OSError:
                continue
    async with source_lock(profile_id):
        profile = _profile(profile_id)
        state = db.get_runtime_state(profile_id)
        if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise HTTPException(409, "Mod sources cannot be changed while the server is starting or stopping.")
        latest_manifest = await _load_manifest(profile_id)
        latest_projects = {
            item.get("project_id")
            for item in latest_manifest.get("mods", [])
            if isinstance(item, dict) and not item.get("pending_removal")
        }
        installed_projects.update(latest_projects)
        if version.get("project_id") in installed_projects:
            raise HTTPException(409, "This mod is already installed in this profile.")
        reserved_projects = {
            project_id for job in _jobs.values()
            if job.get("profile_id") == profile_id and job.get("status") in {"queued", "downloading"}
            for project_id in job.get("project_ids", [job.get("project_id")]) if project_id
        }
        if version.get("project_id") in reserved_projects:
            raise HTTPException(409, "This mod is already queued or downloading for this profile.")
        resolved_dependencies = [
            item for item in resolved_dependencies
            if item.get("project_id") not in reserved_projects | installed_projects
        ]
        job_id = uuid.uuid4().hex
        project_ids = list(dict.fromkeys([version.get("project_id"), *(item.get("project_id") for item in resolved_dependencies)]))
        _jobs[job_id] = {"id": job_id, "status": "queued", "items": [], "profile_id": profile_id, "project_id": version.get("project_id"), "project_ids": project_ids, "running": bool(state and state.status == ProfileStatus.RUNNING), "mc_version": profile.mc_version, "loader": profile.build.value, "created_at": time()}
        asyncio.create_task(run_download_job(job_id, profile_id, version, resolved_dependencies))
    return {"job_id": job_id, "status": "queued", "running": _jobs[job_id]["running"], "applies_on_next_restart": _jobs[job_id]["running"]}


@router.get("/profiles/{profile_id}/mods/jobs/{job_id}")
async def mod_install_status(profile_id: str, job_id: str):
    prune_jobs()
    _profile(profile_id)
    job = _jobs.get(job_id)
    if not job or job["profile_id"] != profile_id:
        raise HTTPException(404, "Install job not found.")
    return job


@router.delete("/profiles/{profile_id}/mods/{digest}")
async def remove_profile_mod(profile_id: str, digest: str):
    _profile(profile_id)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        raise HTTPException(400, "Invalid mod SHA-256 identifier.")
    async with source_lock(profile_id):
        profile = _profile(profile_id)
        state = db.get_runtime_state(profile_id)
        if state and state.status in (ProfileStatus.STARTING, ProfileStatus.STOPPING):
            raise HTTPException(409, "Mod sources cannot be changed while the server is starting or stopping.")
        owned = load_source_manifest(profile_id).get("files", [])
        record = next((item for item in owned if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].startswith("mods/") and item.get("sha256") == digest), None)
        manifest = await _load_manifest(profile_id)
        modrinth_entry = next((item for item in manifest.get("mods", []) if item.get("sha256") == digest), None)
        owner = record.get("owner") if record else ("modrinth" if modrinth_entry else None)
        if owner not in {"modrinth", "link"}:
            raise HTTPException(409, "Only Modrinth and direct-link mods can be removed here. Git, pack, and manual files are managed by their source.")
        filename = record["path"].split("/", 1)[1] if record else modrinth_entry.get("filename", digest)
        running = bool(state and state.status == ProfileStatus.RUNNING)
        if owner == "modrinth":
            if running:
                if not modrinth_entry:
                    raise HTTPException(404, "Modrinth manifest entry not found.")
                # Server is running: mark pending_removal in the manifest; apply on next Start.
                from mcpanel.core.mod_storage import update_manifest as _update_manifest

                async def _mark_pending(manifest):
                    for item in manifest.get("mods", []):
                        if isinstance(item, dict) and item.get("sha256") == digest:
                            item["pending_removal"] = True
                            break

                await _update_manifest(profile_id, _mark_pending)
            else:
                removed = await remove_manifest_mod(profile_id, digest)
                if not removed:
                    raise HTTPException(404, "Modrinth manifest entry not found.")
                # Delete the live JAR from server/mods/
                from mcpanel.core.mod_storage import profile_mods_dir as _live_mods_dir
                live_dir = _live_mods_dir(profile_id)
                live_jar = live_dir / filename
                if live_jar.is_file() and not live_jar.is_symlink():
                    try:
                        live_jar.unlink()
                    except OSError as exc:
                        logger.warning("remove_profile_mod: could not delete live JAR %s: %s", live_jar, exc)
                # Remove from owned_files.json
                owned_data = load_source_manifest(profile_id)
                owned_data["files"] = [
                    item for item in owned_data.get("files", [])
                    if not (isinstance(item, dict) and item.get("sha256") == digest)
                ]
                from mcpanel.core.mod_storage import write_json_atomic, source_manifest_path
                await asyncio.to_thread(write_json_atomic, source_manifest_path(profile_id), owned_data)
                # Delete the cache file if no longer referenced
                cache_name = removed.get("cache_filename")
                if isinstance(cache_name, str) and Path(cache_name).name == cache_name:
                    cache_path = modrinth_cache_dir(profile_id) / cache_name
                    if cache_path.is_file() and not cache_path.is_symlink():
                        still_used = any(item.get("cache_filename") == cache_name for item in (await _load_manifest(profile_id)).get("mods", []))
                        if not still_used:
                            cache_path.unlink(missing_ok=True)
        else:
            source_key = record.get("source_key")
            if not source_key:
                raise HTTPException(409, "This direct-link mod has no source mapping and cannot be safely removed.")
            removed_links = [
                item for index, item in enumerate(profile.mod_downloads)
                if mod_downloads.source_key(item, index) == source_key
            ]
            profile.mod_downloads = [
                item for index, item in enumerate(profile.mod_downloads)
                if mod_downloads.source_key(item, index) != source_key
            ]
            db.update_profile(profile)
            cache_root = mod_storage.profile_root(profile_id) / "downloaded_mods"
            if cache_root.is_dir() and not cache_root.is_symlink():
                legacy_keys = [mod_downloads.legacy_cache_key(item) for item in removed_links]
                for cache_key in (source_key, *legacy_keys):
                    cache_folder = cache_root / cache_key
                    if cache_folder.is_dir() and not cache_folder.is_symlink():
                        shutil.rmtree(cache_folder, ignore_errors=True)
            if not running:
                live_dir = _mods_dir(profile_id)
                live_jar = live_dir / filename
                if live_jar.is_file() and not live_jar.is_symlink():
                    try:
                        live_jar.unlink()
                    except OSError as exc:
                        logger.warning("remove_profile_mod: could not delete link mod live JAR %s: %s", live_jar, exc)
                owned_data = load_source_manifest(profile_id)
                owned_data["files"] = [
                    item for item in owned_data.get("files", [])
                    if not (isinstance(item, dict) and item.get("sha256") == digest)
                ]
                await asyncio.to_thread(mod_storage.write_json_atomic, mod_storage.source_manifest_path(profile_id), owned_data)
        result_status = "pending_removal" if running else "deleted"
        logger.info("remove_profile_mod: profile=%s file=%s source=%s result=%s", profile_id, filename, owner, result_status)
        return {"removed": filename, "source": owner, "restart_required": running, "applies_on_next_restart": running}