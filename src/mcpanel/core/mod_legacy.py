"""Backfill Modrinth metadata for older profile JARs without source records."""

import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from mcpanel.core.mod_provider import provider
from mcpanel.core.mod_source_files import candidate
from mcpanel.core.mod_storage import (
    hash_file,
    hash_file_sync,
    load_manifest,
    load_source_manifest,
    manifest_path,
    modrinth_cache_dir,
    source_manifest_path,
    write_json_atomic,
)

logger = logging.getLogger(__name__)


def legacy_candidates(server_dir: Path, ownership: dict, manifest: dict) -> list[dict]:
    """Collect unmanaged live JARs for identification, excluding current source-owned files."""
    owned = {
        item.get("path"): item
        for item in ownership.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    manifest_hashes = {
        item.get("sha256") for item in manifest.get("mods", [])
        if isinstance(item, dict) and isinstance(item.get("sha256"), str)
    }
    mods_dir = server_dir / "mods"
    if mods_dir.is_symlink() or not mods_dir.is_dir():
        return []
    result = []
    for path in mods_dir.glob("*.jar"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            digest = hash_file_sync(path)
        except OSError:
            continue
        relative = f"mods/{path.name}"
        current = owned.get(relative, {}) or owned.get(path.name, {})
        owner = current.get("owner") if current.get("sha256") == digest else "manual"
        if digest in manifest_hashes or owner in {"git", "pack", "link", "modrinth"}:
            continue
        item = candidate(path, path.name, "manual", folder="mods")
        item["legacy"] = True
        result.append(item)
    return result


async def persist_identified(
    profile_id: str,
    candidates: list[dict],
    manifest_data: dict | None = None,
    write_ownership: bool = False,
) -> tuple[dict | None, list[str]]:
    """Cache identified legacy JARs and return an updated Modrinth manifest when changed."""
    identified = [item for item in candidates if item.get("legacy") and item.get("owner") == "modrinth" and item.get("metadata")]
    if not identified:
        return None, []
    warnings = []
    manifest = manifest_data if manifest_data is not None else await load_manifest(profile_id)
    entries = {}
    unkeyed = []
    for item in manifest.get("mods", []):
        if not isinstance(item, dict):
            continue
        if item.get("sha256") and isinstance(item.get("sha256"), str):
            entries[item["sha256"]] = item
        else:
            unkeyed.append(item)
    cache = modrinth_cache_dir(profile_id)
    ownership = load_source_manifest(profile_id) if write_ownership else None
    for source in identified:
        digest = source["sha256"]
        metadata = source["metadata"]
        cache_file = cache / f"{digest}.jar"
        if cache_file.is_symlink():
            source["owner"] = "manual"
            warnings.append(f"{source['relative']}: Modrinth identified it, but its cache path is unsafe.")
            continue
        try:
            if not cache_file.is_file() or hash_file_sync(cache_file) != digest:
                temporary = cache / f".{digest}.{uuid.uuid4().hex}.part"
                try:
                    shutil.copy2(source["path"], temporary)
                    if hash_file_sync(temporary) != digest:
                        raise OSError("the source file changed while it was copied")
                    os.replace(temporary, cache_file)
                finally:
                    temporary.unlink(missing_ok=True)
            sha1 = source.get("sha1") or await hash_file(source["path"], "sha1")
            sha512 = source.get("sha512") or await hash_file(source["path"], "sha512")
            project_id = metadata.get("project_id")
            filename = source["relative"].rsplit("/", 1)[-1]
            previous = entries.get(digest)
            if previous is None:
                for candidate_item in unkeyed:
                    if candidate_item.get("sha512") == sha512 or candidate_item.get("filename") == filename:
                        previous = candidate_item
                        unkeyed.remove(candidate_item)
                        break
            if previous is None:
                previous = {}
            entries[digest] = {
                **previous,
                "sha256": digest,
                "sha1": sha1,
                "sha512": sha512,
                "project_id": project_id,
                "version_id": metadata.get("id"),
                "name": metadata.get("project_title") or previous.get("name") or metadata.get("name") or source["relative"],
                "version": metadata.get("version_number"),
                "filename": filename,
                "cache_filename": cache_file.name,
                "side": previous.get("side", "unknown"),
                "loaders": metadata.get("loaders", []),
                "game_versions": metadata.get("game_versions", []),
                "dependencies": metadata.get("dependencies", []),
                "installed_at": previous.get("installed_at") or datetime.now(timezone.utc).isoformat(),
            }
            if ownership is not None:
                rel_path = f"mods/{source['relative']}" if not source["relative"].startswith("mods/") else source["relative"]
                ownership["files"] = [
                    item for item in ownership.get("files", [])
                    if not (isinstance(item, dict) and item.get("path") in (source["relative"], rel_path))
                ]
                ownership["files"].append({"path": rel_path, "owner": "modrinth", "sha256": digest})
        except (OSError, ValueError) as exc:
            source["owner"] = "manual"
            logger.warning("Could not backfill legacy mod %s: %s", source["relative"], exc)
            warnings.append(f"{source['relative']}: Modrinth identified it, but metadata could not be saved ({exc}).")

    manifest["mods"] = [*entries.values(), *unkeyed]
    if write_ownership:
        try:
            write_json_atomic(manifest_path(profile_id), manifest)
            if ownership is not None:
                write_json_atomic(source_manifest_path(profile_id), ownership)
        except OSError as exc:
            logger.warning("Could not save legacy mod manifests for %s: %s", profile_id, exc)
            warnings.append(f"Legacy mod metadata could not be saved ({exc}).")
    return manifest, warnings


async def identify_legacy(profile_id: str, server_dir: Path) -> list[str]:
    """Identify and persist live legacy mods for the Installed tab, best-effort."""
    manifest = await load_manifest(profile_id)
    ownership = load_source_manifest(profile_id)
    candidates = legacy_candidates(server_dir, ownership, manifest)
    warnings = []
    if not candidates:
        return warnings
    try:
        for item in candidates:
            item["sha512"] = await hash_file(item["path"], "sha512")
        matches = await provider.identify_files([item["sha512"] for item in candidates], "sha512")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.warning("Legacy Modrinth lookup unavailable for %s: %s", profile_id, exc)
        matches = provider.cached_file_identifications([item["sha512"] for item in candidates], "sha512")
        if not matches:
            return [f"Legacy mod lookup is unavailable; existing JARs remain manually managed ({exc})."]
        warnings.append(f"Modrinth hash lookup was unavailable; used cached identifications where available ({exc}).")
    project_ids = list({value.get("project_id") for value in matches.values() if value.get("project_id")})
    titles = {}
    if project_ids:
        try:
            for item in await provider.projects(project_ids):
                if isinstance(item, dict) and item.get("title"):
                    if item.get("id"):
                        titles[item["id"]] = item["title"]
                    if item.get("slug"):
                        titles[item["slug"]] = item["title"]
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Could not resolve Modrinth project names: %s", exc)
    for item in candidates:
        version = matches.get(item["sha512"])
        if not version:
            warnings.append(f"{item['relative']}: Modrinth could not identify this JAR; it remains manually managed.")
            continue
        item["metadata"] = {**version, "project_title": titles.get(version.get("project_id"))}
        item["owner"] = "modrinth"
    _, save_warnings = await persist_identified(profile_id, candidates, write_ownership=True)
    warnings.extend(save_warnings)
    return list(dict.fromkeys(warnings))
