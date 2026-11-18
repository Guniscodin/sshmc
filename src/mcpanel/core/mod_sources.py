"""Resolve, validate, stage, and atomically apply profile mod sources."""

import asyncio
import logging
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path

import httpx

from mcpanel.core import git_sync, mod_downloads, mod_legacy, modpacks
from mcpanel.core.mod_provider import provider
from mcpanel.core.mod_source_files import (
    copy_candidate, copy_source_tree, old_unowned, recover_staging, safe_file,
    safe_target, install_transaction,
)
from mcpanel.core.mod_storage import (
    hash_file_sync, load_source_manifest, manifest_path, modrinth_cache_dir,
    profile_root, source_lock, source_manifest_path, load_manifest,
)

logger = logging.getLogger(__name__)
GIT_DATA_FOLDERS = ("config", "defaultconfigs", "datapacks", "kubejs", "scripts")
OUTPUT_FOLDERS = ("mods", *GIT_DATA_FOLDERS)


class SourcePreflightError(Exception):
    def __init__(self, conflicts: list[str], warnings: list[str], jar_names: list[str] | None = None):
        self.conflicts = conflicts
        self.warnings = warnings
        self.issues_by_jar = {}
        for conflict in conflicts:
            jar_name = next((name for name in sorted(set(jar_names or []), key=len, reverse=True) if conflict.startswith(name)), "Source checks")
            message = conflict[len(jar_name):].lstrip(": ") if jar_name != "Source checks" else conflict
            self.issues_by_jar.setdefault(jar_name, []).append(message)
        message = "Mod source preflight failed: " + "; ".join(conflicts)
        super().__init__(message)


async def _manifest_mod_candidates(profile_id: str, server_dir: Path, profile_root_path: Path, warnings: list[str]) -> tuple[list[dict], dict | None, dict]:
    path = manifest_path(profile_id)
    if path.is_symlink():
        raise ValueError("The Modrinth manifest cannot be a symbolic link.")
    try:
        manifest = await load_manifest(profile_id)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Modrinth manifest cannot be read: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("mods", []), list):
        raise ValueError("Modrinth manifest has an invalid shape.")
    cache = modrinth_cache_dir(profile_id)
    existing_mods = server_dir / "mods"
    candidates, reconciled = [], []
    changed = False
    for item in manifest.get("mods", []):
        if not isinstance(item, dict):
            changed = True
            continue
        if item.get("pending_removal"):
            # Mod was removed while the server was running; skip it during staging
            # and clean up its cached file if not used by any other mod
            cache_name = item.get("cache_filename")
            cache_file = cache / cache_name if isinstance(cache_name, str) and Path(cache_name).name == cache_name else (cache / f"{item.get('sha256')}.jar" if item.get("sha256") else None)
            if cache_file and cache_file.is_file() and not cache_file.is_symlink():
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
            changed = True
            continue
        digest = item.get("sha256")
        cache_name = item.get("cache_filename")
        cached = cache / cache_name if isinstance(cache_name, str) and Path(cache_name).name == cache_name else None
        if not digest and isinstance(item.get("filename"), str):
            legacy = existing_mods / item["filename"]
            if not existing_mods.is_symlink() and safe_file(legacy, existing_mods):
                digest = await asyncio.to_thread(hash_file_sync, legacy)
                cached = cache / f"{digest}.jar"
                if cached.is_symlink():
                    raise ValueError("The Modrinth cache destination cannot be a symbolic link.")
                temporary = cache / f".{digest}.{uuid.uuid4().hex}.part"
                try:
                    await asyncio.to_thread(shutil.copy2, legacy, temporary)
                    if await asyncio.to_thread(hash_file_sync, temporary) != digest:
                        raise ValueError(f"The legacy Modrinth file changed while being copied: {item['filename']}.")
                    os.replace(temporary, cached)
                finally:
                    temporary.unlink(missing_ok=True)
                item = {**item, "sha256": digest, "sha1": await asyncio.to_thread(hash_file_sync, cached, "sha1"), "cache_filename": cached.name}
                changed = True
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            changed = True
            continue
        if cached is None:
            cached = cache / f"{digest}.jar"
        cached_valid = safe_file(cached, profile_root_path) and await asyncio.to_thread(hash_file_sync, cached) == digest
        if not cached_valid:
            warnings.append(f"{item.get('filename', digest)}: stale Modrinth cache entry was removed from the manifest.")
            changed = True
            continue
        try:
            filename = item.get("filename")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith(".jar"):
                raise ValueError("invalid filename")
            safe_target(filename)
        except ValueError:
            warnings.append(f"{digest}: unsafe filename in Modrinth manifest; entry was skipped.")
            changed = True
            continue
        item = {**item, "sha256": digest, "cache_filename": cached.name}
        reconciled.append(item)
        candidates.append({
            "path": cached, "relative": item["filename"], "owner": "modrinth", "sha256": digest,
            "sha1": item.get("sha1"), "sha512": item.get("sha512"),
            "project_id": item.get("project_id"), "version_id": item.get("version_id"),
            "modrinth": item,
        })
    if changed or len(reconciled) != len(manifest.get("mods", [])):
        manifest["mods"] = reconciled
        return candidates, manifest, manifest
    return candidates, None, manifest


def _mod_base(profile, repo_dir: Path, pack: dict | None) -> tuple[str | None, Path | None]:
    if pack:
        return "pack", modpacks.get_pack_directory(pack["name"]) / "mods"
    if profile.git_url:
        return "git", repo_dir / "mods"
    return None, None


async def _preflight(candidates: list[dict], profile, warnings: list[str]) -> list[dict]:
    unique: dict[str, dict] = {}
    conflicts = []
    for item in candidates:
        name = item["relative"].casefold()
        old = unique.get(name)
        if not old:
            unique[name] = item
        elif old["sha256"] == item["sha256"]:
            logger.info("Deduplicated identical %s and %s file %s", old["owner"], item["owner"], item["relative"])
        else:
            conflicts.append(f"{item['relative']} from {old['owner']} conflicts with {item['relative']} from {item['owner']}.")
    mods = list(unique.values())
    zip_checks = await asyncio.gather(*(asyncio.to_thread(zipfile.is_zipfile, item["path"]) for item in mods))
    invalid = [item["relative"] for item, ok in zip(mods, zip_checks) if not ok]
    conflicts.extend(f"{name}: file is not a valid JAR/ZIP archive." for name in invalid)
    mods = [item for item in mods if item["relative"] not in invalid]
    hashes: dict[str, list[dict]] = {}
    for item in mods:
        item["sha512"] = item.get("sha512") or await asyncio.to_thread(hash_file_sync, item["path"], "sha512")
        hashes.setdefault(item["sha512"], []).append(item)
    try:
        identified = await provider.identify_files(list(hashes), "sha512")
    except (httpx.HTTPError, ValueError, OSError) as exc:
        logger.warning("Modrinth exact hash lookup failed during preflight: %s", exc)
        identified = provider.cached_file_identifications(list(hashes), "sha512")
        warnings.append(f"Modrinth hash lookup was unavailable; compatibility checks were skipped for unidentified JARs ({exc}).")
    projects: dict[str, dict] = {}
    versions = {}
    missing_dependencies = []
    legacy_items = []
    for digest, items in hashes.items():
        exact_metadata = identified.get(digest)
        for item in items:
            recorded = item.get("modrinth") or {}
            metadata = exact_metadata
            if not metadata:
                metadata = recorded.get("metadata")
                if not isinstance(metadata, dict) and recorded.get("project_id"):
                    metadata = {
                        "project_id": recorded.get("project_id"), "id": recorded.get("version_id"),
                        "name": recorded.get("name"), "version_number": recorded.get("version"),
                        "game_versions": recorded.get("game_versions", []),
                        "loaders": recorded.get("loaders", []),
                        "dependencies": recorded.get("dependencies", []),
                    }
                if metadata:
                    warnings.append(f"{item['relative']}: using saved exact-version metadata; Modrinth could not refresh the hash lookup.")
                else:
                    warnings.append(f"{item['relative']}: Modrinth could not identify this JAR; compatibility was not verified.")
                    continue
            item["metadata"] = metadata
            if item.get("legacy"):
                item["owner"] = "modrinth"
                legacy_items.append(item)
            game_versions = metadata.get("game_versions")
            loaders = metadata.get("loaders")
            if not game_versions or not loaders:
                warnings.append(f"{item['relative']}: saved metadata lacks loader or Minecraft-version details; those checks were skipped.")
            if "dependencies" not in metadata:
                warnings.append(f"{item['relative']}: saved metadata lacks dependency details; dependency checks were skipped.")
            if game_versions and profile.mc_version not in game_versions:
                conflicts.append(f"{item['relative']}: identified for different Minecraft versions, not {profile.mc_version}.")
            if loaders and profile.build.value not in loaders:
                conflicts.append(f"{item['relative']}: identified for a different loader, not {profile.build.value}.")
            project_id = metadata.get("project_id") or recorded.get("project_id")
            version_id = metadata.get("id") or recorded.get("version_id")
            if project_id:
                prior = projects.get(project_id)
                if prior and version_id and prior.get("version_id") and prior["version_id"] != version_id:
                    conflicts.append(f"{item['relative']}: {project_id} is present in two versions ({prior['relative']} and {item['relative']}).")
                else:
                    projects[project_id] = {"version_id": version_id, "relative": item["relative"]}
            if version_id:
                versions[version_id] = item
    for item in mods:
        metadata = item.get("metadata") or {}
        for dependency in metadata.get("dependencies") or []:
            if dependency.get("dependency_type") != "required":
                continue
            project_id, version_id = dependency.get("project_id"), dependency.get("version_id")
            if project_id and project_id not in projects:
                missing_dependencies.append((item["relative"], project_id))
            elif not project_id and version_id and version_id not in versions:
                try:
                    dependency_version = await provider.version(version_id)
                    dependency_project = dependency_version.get("project_id")
                    if dependency_project and dependency_project not in projects:
                        missing_dependencies.append((item["relative"], dependency_project))
                except (httpx.HTTPError, ValueError) as exc:
                    warnings.append(f"{item['relative']}: could not resolve dependency {version_id} ({exc}).")
    missing_ids = list(dict.fromkeys(project_id for _, project_id in missing_dependencies))
    legacy_ids = [item["metadata"].get("project_id") for item in legacy_items if item["metadata"].get("project_id")]
    lookup_ids = list(dict.fromkeys([*missing_ids, *legacy_ids]))
    project_names = {}
    if lookup_ids:
        try:
            for item in await provider.projects(lookup_ids):
                if isinstance(item, dict) and item.get("title"):
                    if item.get("id"):
                        project_names[item["id"]] = item["title"]
                    if item.get("slug"):
                        project_names[item["slug"]] = item["title"]
        except (httpx.HTTPError, ValueError) as exc:
            if missing_ids:
                warnings.append(f"Modrinth dependency names could not be loaded ({exc}); project IDs are shown instead.")
    for item in legacy_items:
        item["metadata"]["project_title"] = project_names.get(item["metadata"].get("project_id"))
    for jar_name, project_id in missing_dependencies:
        dependency_name = project_names.get(project_id)
        label = f"{dependency_name} ({project_id})" if dependency_name else project_id
        conflicts.append(f"{jar_name}: missing required dependency {label}.")
    if conflicts:
        raise SourcePreflightError(
            list(dict.fromkeys(conflicts)), list(dict.fromkeys(warnings)),
            [item["relative"] for item in candidates],
        )
    return mods


async def build_profile_sources(profile, repo_dir: Path, server_dir: Path, skip_link_indexes: set[int] | None = None) -> dict:
    """Build all source folders in staging, preflight, then swap as one rollback-capable operation."""
    profile_root_path = profile_root(profile.id)
    if server_dir.is_symlink() or repo_dir.is_symlink():
        raise ValueError("Profile server and repository folders cannot be symbolic links.")
    for path in (server_dir, repo_dir):
        try:
            path.resolve().relative_to(profile_root_path)
        except ValueError as exc:
            raise ValueError("Profile source paths must stay inside profile storage.") from exc
    async with source_lock(profile.id):
        await asyncio.to_thread(recover_staging, profile_root_path, server_dir, OUTPUT_FOLDERS)
        warnings = []
        git_empty = bool(profile.git_url and await git_sync.sync_git_repository(profile.git_url, repo_dir))
        if git_empty:
            warnings.append("The configured Git repository is empty; Git files were not applied.")
        pack = modpacks.get_pack(profile.modpack_name)
        if profile.modpack_name and not pack:
            raise SourcePreflightError([f"Linked mod pack '{profile.modpack_name}' no longer exists."], warnings)
        ownership = load_source_manifest(profile.id)
        unowned = await asyncio.to_thread(old_unowned, server_dir, ownership, OUTPUT_FOLDERS)
        base_owner, base_path = _mod_base(profile, repo_dir, pack)
        if base_owner == "git" and git_empty:
            base_candidates = []
        elif base_owner and base_path:
            base_candidates = []
            await asyncio.to_thread(copy_source_tree, base_path, "mods", base_owner, base_candidates)
        else:
            base_candidates = []
        manifest_mods, reconciled_manifest, manifest_data = await _manifest_mod_candidates(
            profile.id, server_dir, profile_root_path, warnings
        )
        legacy_mods = await asyncio.to_thread(mod_legacy.legacy_candidates, server_dir, ownership, manifest_data)
        link_candidates = await mod_downloads.install_configured_mods(
            profile, profile_root_path / "downloaded_mods", skip_link_indexes or set()
        )
        hashed_links = []
        for item in link_candidates:
            hashed_links.append({**item, "relative": item["filename"],
                                 "sha256": await asyncio.to_thread(hash_file_sync, item["path"])})
        link_candidates = hashed_links
        mod_candidates = await _preflight(
            [*base_candidates, *manifest_mods, *legacy_mods, *link_candidates], profile, warnings
        )
        legacy_manifest, backfill_warnings = await mod_legacy.persist_identified(
            profile.id, mod_candidates, manifest_data=manifest_data
        )
        warnings.extend(backfill_warnings)
        if legacy_manifest is not None:
            reconciled_manifest = legacy_manifest
        data_candidates = [item for item in unowned if item["owner"] == "manual"]
        if pack:
            pack_dir = modpacks.get_pack_directory(pack["name"])
            for folder in ("config", "datapacks"):
                await asyncio.to_thread(copy_source_tree, pack_dir / folder, folder, "pack", data_candidates)
        if profile.git_url and not git_empty:
            for folder in GIT_DATA_FOLDERS:
                await asyncio.to_thread(copy_source_tree, repo_dir / folder, folder, "git", data_candidates)
        stage_root = server_dir / f".sshmc-source-stage-{uuid.uuid4().hex}"
        new_root = stage_root / "new"
        try:
            new_root.mkdir(parents=True)
            ownership_files = []
            for folder in OUTPUT_FOLDERS:
                (new_root / folder).mkdir(parents=True)
            for item in mod_candidates:
                copied = await asyncio.to_thread(copy_candidate, item, new_root / "mods", profile_root_path)
                ownership_files.append(copied)
            winners: dict[str, dict] = {}
            for item in data_candidates:
                target = f"{item['folder']}/{item['relative']}" if item.get("folder") else item["relative"]
                item = {**item, "relative": target}
                prior = winners.get(target)
                if prior:
                    logger.info("Git/pack config overlay: %s overrides %s at %s", item["owner"], prior["owner"], target)
                winners[target] = item
            for item in winners.values():
                folder, relative = item["relative"].split("/", 1)
                copied = await asyncio.to_thread(copy_candidate, {**item, "relative": relative}, new_root / folder, profile_root_path)
                ownership_files.append(copied)
                   # After staging succeeds, purge pending_removal entries so they are gone
            # from the manifest on the next read (they were already excluded from staging).
            if reconciled_manifest is not None:
                reconciled_manifest["mods"] = [
                    m for m in reconciled_manifest.get("mods", [])
                    if not (isinstance(m, dict) and m.get("pending_removal"))
                ]
            else:
                # Build a cleaned copy of the current manifest if there are pending removals.
                current_pending = [m for m in manifest_data.get("mods", []) if isinstance(m, dict) and m.get("pending_removal")]
                if current_pending:
                    reconciled_manifest = {**manifest_data, "mods": [m for m in manifest_data.get("mods", []) if not (isinstance(m, dict) and m.get("pending_removal"))]}
            extra_manifests = {manifest_path(profile.id): reconciled_manifest} if reconciled_manifest is not None else None
            await asyncio.to_thread(
                install_transaction, server_dir, stage_root, source_manifest_path(profile.id),
                {"files": ownership_files}, OUTPUT_FOLDERS, extra_manifests,
            )
        except Exception:
            if not (stage_root / "RECOVERY_FAILED").exists():
                await asyncio.to_thread(shutil.rmtree, stage_root, True)
            raise
        await asyncio.to_thread(shutil.rmtree, stage_root, True)
        return {"warnings": list(dict.fromkeys(warnings)), "mods": mod_candidates}
