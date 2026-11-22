"""Imported, profile-assignable server directories.

A launch directory is a complete server pack folder (for example a CurseForge
server pack) that a profile can launch directly instead of its own managed
``profiles/<id>/server`` folder.  Imports preserve the pack tree, but runtime
artifacts and world saves are deliberately excluded so the profile keeps
ownership of them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from mcpanel import db
from mcpanel.config import DATA_DIR, LAUNCH_DIRS_DIR, MAX_MODPACK_EXTRACTED_BYTES, UPLOADS_DIR
from mcpanel.models import BuildType, Profile

MANIFEST_NAME = "launch.json"
MAX_ENTRIES = 100_000
_IMPORT_LOCK = threading.Lock()
_KNOWN_ROOTS = {
    "mods", "config", "datapacks", "defaultconfigs", "kubejs", "scripts",
    "resourcepacks", "shaderpacks", "structures",
}
_EXCLUDED_DIRS = {
    "world", "world_nether", "world_the_end", "logs", "crash-reports",
    "libraries", "versions", ".fabric", "downloaded_mods",
}
_EXCLUDED_FILES = {
    "server.jar", "run.sh", "run.bat", "user_jvm_args.txt", "eula.txt",
    "server.properties", "profile_meta.json", "installer.jar", "installer.jar.log",
    "banned-ips.json", "banned-players.json", "ops.json", "whitelist.json",
    "usercache.json",
}


class LaunchDirError(Exception):
    """A user-correctable launch-directory problem."""


class LaunchDirExistsError(LaunchDirError):
    """Raised when an import would replace an existing directory."""


class LaunchDirInUseError(LaunchDirError):
    """Raised when a directory is assigned to a profile and cannot be changed."""


class LaunchDirFileConflicts(LaunchDirError):
    """Raised when adding files to an existing directory would replace existing files."""

    def __init__(self, filenames: list[str]) -> None:
        super().__init__(f"Some files already exist: {', '.join(filenames)}")
        self.filenames = filenames


def sanitize_name(raw_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw_name or "").strip()).strip("-_")[:64]
    if not value:
        raise LaunchDirError("Enter a directory name using letters, numbers, dashes, or underscores.")
    return value


def _root() -> Path:
    root = LAUNCH_DIRS_DIR.resolve()
    try:
        root.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise LaunchDirError("Launch directory storage must stay inside the data directory.") from exc
    root.mkdir(parents=True, exist_ok=True)
    return root


def _dir(name: str) -> Path:
    safe_name = sanitize_name(name)
    root = _root()
    target = (root / safe_name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise LaunchDirError("Invalid launch directory name.") from exc
    return target


def _manifest(pack_dir: Path) -> dict | None:
    if pack_dir.is_symlink() or not pack_dir.is_dir():
        return None
    try:
        value = json.loads((pack_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _actual_launch_dir_stats(launch_dir: Path) -> tuple[int, int, list[str], list[dict]]:
    files: list[dict] = []
    total_bytes = 0
    top_folders: set[str] = set()

    for current, directories, filenames in os.walk(launch_dir, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            d for d in directories
            if not d.startswith(".") and not (current_path / d).is_symlink()
        ]
        for filename in filenames:
            if filename.startswith(".") or filename == MANIFEST_NAME:
                continue
            file_path = current_path / filename
            if file_path.is_symlink() or not file_path.is_file():
                continue
            try:
                rel = file_path.relative_to(launch_dir)
                size = file_path.stat().st_size
                posix_str = str(rel).replace("\\", "/")
                files.append({"path": posix_str, "size_bytes": size})
                total_bytes += size
                if len(rel.parts) > 1:
                    top_folders.add(rel.parts[0])
            except OSError:
                continue

    for entry in launch_dir.iterdir():
        if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith("."):
            top_folders.add(entry.name)

    files.sort(key=lambda item: item["path"].casefold())
    folders = sorted(top_folders, key=str.casefold)
    return len(files), total_bytes, folders, files


def list_launch_dirs() -> list[dict]:
    root = _root()
    items: list[dict] = []
    assignments = {
        profile.launch_dir.casefold(): profile
        for profile in db.list_profiles()
        if profile.launch_dir
    }
    for folder in root.iterdir():
        if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir():
            continue
        manifest = _manifest(folder)
        if not manifest:
            continue
        if manifest.get("files") is None:
            file_count, total_bytes, folders, files = _actual_launch_dir_stats(folder)
            manifest["file_count"] = file_count
            manifest["bytes"] = total_bytes
            manifest["folders"] = folders
            manifest["files"] = files
        assigned = assignments.get(folder.name.casefold())
        manifest = {
            **manifest,
            "assigned_profile_id": assigned.id if assigned else None,
            "assigned_profile_name": assigned.name if assigned else None,
        }
        items.append(manifest)
    return sorted(items, key=lambda item: str(item.get("name", "")).casefold())


def get_launch_dir(name: str | None) -> dict | None:
    if not name:
        return None
    try:
        folder = _dir(name)
        manifest = _manifest(folder)
        if manifest and manifest.get("files") is None:
            file_count, total_bytes, folders, files = _actual_launch_dir_stats(folder)
            manifest["file_count"] = file_count
            manifest["bytes"] = total_bytes
            manifest["folders"] = folders
            manifest["files"] = files
        return manifest
    except LaunchDirError:
        return None


def get_launch_dir_path(name: str) -> Path:
    path = _dir(name)
    if path.is_symlink() or not path.is_dir() or not _manifest(path):
        raise LaunchDirError(f"Launch directory '{name}' is missing or invalid.")
    return path


def assigned_profile(name: str, exclude_profile_id: str | None = None) -> Profile | None:
    safe_name = name.casefold()
    for profile in db.list_profiles():
        if profile.launch_dir and profile.launch_dir.casefold() == safe_name and profile.id != exclude_profile_id:
            return profile
    return None


def is_compatible(
    manifest: dict | None,
    profile: Profile | BuildType | str,
    mc_version: str | None = None,
) -> bool:
    if not manifest:
        return False
    if isinstance(profile, Profile):
        build = profile.build.value
        version = profile.mc_version
    elif isinstance(profile, BuildType):
        build = profile.value
        version = mc_version or ""
    else:
        build = str(profile)
        version = mc_version or ""
    return (
        str(manifest.get("build", "")).casefold() == build.casefold()
        and str(manifest.get("mc_version", "")).strip() == str(version).strip()
    )


def _session_files(session_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
        raise LaunchDirError("Invalid upload session.")
    uploads = UPLOADS_DIR.resolve()
    try:
        uploads.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise LaunchDirError("Upload storage must stay inside the data directory.") from exc
    session = (uploads / session_id).resolve()
    try:
        session.relative_to(uploads)
    except ValueError as exc:
        raise LaunchDirError("Invalid upload session.") from exc
    if session.is_symlink() or not session.is_dir():
        raise LaunchDirError("The upload session expired or was cancelled.")
    files = session / "files"
    if files.is_symlink() or not files.is_dir():
        raise LaunchDirError("The upload session has no staged files.")
    return session, files


def _safe_relative(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    if not parts:
        return None
    for part in parts:
        if part in ("", ".", "..") or ".." in part or "\x00" in part:
            return None
    return parts


def _strip_wrapper(paths: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    if len(paths) < 1 or any(len(parts) < 2 for parts in paths):
        return paths
    firsts = {parts[0].casefold() for parts in paths}
    if len(firsts) != 1:
        return paths
    wrapper = next(iter(firsts))
    if wrapper in _KNOWN_ROOTS:
        return paths
    return [parts[1:] for parts in paths]


def _excluded(parts: tuple[str, ...]) -> bool:
    if not parts:
        return True
    top = parts[0].casefold()
    if top in _EXCLUDED_DIRS or (top.startswith("world_") and len(parts) > 1) or top.startswith("."):
        return True
    if len(parts) == 1 and (parts[0].casefold() in _EXCLUDED_FILES or parts[0].casefold() == MANIFEST_NAME.casefold()):
        return True
    return False


def import_session(
    session_id: str,
    raw_name: str,
    mc_version: str,
    build: str,
    replace: bool = False,
    label: str | None = None,
) -> dict:
    """Copy a staged browser folder upload into a managed launch directory."""
    with _IMPORT_LOCK:
        return _import_session(session_id, raw_name, mc_version, build, replace, label)


def _import_session(
    session_id: str,
    raw_name: str,
    mc_version: str,
    build: str,
    replace: bool,
    label: str | None = None,
) -> dict:
    if build not in {item.value for item in BuildType}:
        raise LaunchDirError("Choose Vanilla, Fabric, Forge, or NeoForge as the loader.")
    version = (mc_version or "").strip()
    if not version or len(version) > 32 or any(char in version for char in "\x00\r\n"):
        raise LaunchDirError("Enter a valid Minecraft version.")
    name = sanitize_name(raw_name)
    session, files_dir = _session_files(session_id)
    root = _root()
    existing = next(
        (folder for folder in root.iterdir()
         if folder.name.casefold() == name.casefold() and folder.is_dir() and not folder.is_symlink()),
        None,
    )
    if existing and not replace:
        raise LaunchDirExistsError(
            f"A launch directory named '{existing.name}' already exists. Remove it first or choose another name."
        )
    if existing:
        name = existing.name
    stage = root / f".staging-{uuid4().hex}"
    stage.mkdir()
    warnings: list[str] = []
    try:
        candidates: list[tuple[tuple[str, ...], Path]] = []
        for current, directories, filenames in os.walk(files_dir, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                item for item in directories
                if not (current_path / item).is_symlink()
            ]
            for filename in filenames:
                source = current_path / filename
                if source.is_symlink() or not source.is_file():
                    continue
                relative = source.relative_to(files_dir)
                posix = PurePosixPath(*relative.parts)
                windows = PureWindowsPath(str(relative))
                if posix.is_absolute() or windows.is_absolute() or windows.drive:
                    warnings.append(f"Skipped unsafe path: {relative}")
                    continue
                parts = _safe_relative(tuple(relative.parts))
                if not parts:
                    warnings.append(f"Skipped unsafe path: {relative}")
                    continue
                candidates.append((parts, source))
                if len(candidates) > MAX_ENTRIES:
                    raise LaunchDirError(f"This folder contains more than {MAX_ENTRIES} files.")

        stripped = _strip_wrapper([parts for parts, _ in candidates])
        written: dict[tuple[str, ...], tuple[tuple[str, ...], Path]] = {}
        for parts, (_, source) in zip(stripped, candidates, strict=True):
            key = tuple(part.casefold() for part in parts)
            if key in written:
                warnings.append(f"Skipped duplicate path: {'/'.join(parts)}")
                continue
            written[key] = (parts, source)
        copied = 0
        total_bytes = 0
        top_folders: set[str] = set()
        for parts, source in written.values():
            if _excluded(parts):
                continue
            target = stage.joinpath(*parts)
            try:
                target.resolve().relative_to(stage.resolve())
            except ValueError:
                warnings.append(f"Skipped unsafe path: {'/'.join(parts)}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                warnings.append(f"Skipped duplicate path: {'/'.join(parts)}")
                continue
            if source.suffix.lower() == ".jar" and not zipfile.is_zipfile(source):
                warnings.append(f"Skipped invalid JAR archive: {source.name}")
                continue
            shutil.copyfile(source, target)
            copied += 1
            total_bytes += source.stat().st_size
            if len(parts) > 1:
                top_folders.add(parts[0])
        if not copied:
            raise LaunchDirError(
                "No importable server files were found. World saves, logs, and server runtime files are excluded."
            )
        file_count, total_bytes, folders, files = _actual_launch_dir_stats(stage)
        manifest = {
            "name": name,
            "label": (label or raw_name or "").strip() or name,
            "mc_version": version,
            "build": build,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": file_count,
            "bytes": total_bytes,
            "folders": folders,
            "files": files,
            "warnings": warnings[:250],
        }
        (stage / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        old = root / f".replaced-{uuid4().hex}"
        moved_old = False
        if existing:
            os.replace(existing, old)
            moved_old = True
        try:
            os.replace(stage, _dir(name))
        except Exception:
            if moved_old and existing is not None:
                os.replace(old, existing)
            raise
        if moved_old:
            shutil.rmtree(old, ignore_errors=True)
        return manifest
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def add_files_to_launch_dir(
    session_id: str,
    raw_name: str,
    replace_files: bool = False,
) -> dict:
    """Add or update files inside an existing launch directory."""
    with _IMPORT_LOCK:
        return _add_files_to_launch_dir(session_id, raw_name, replace_files)


def _add_files_to_launch_dir(
    session_id: str,
    raw_name: str,
    replace_files: bool,
) -> dict:
    name = sanitize_name(raw_name)
    pack_dir = _dir(name)
    manifest = _manifest(pack_dir)
    if not manifest:
        raise FileNotFoundError("Launch directory not found.")

    session, files_dir = _session_files(session_id)
    warnings: list[str] = []
    try:
        candidates: list[tuple[tuple[str, ...], Path]] = []
        for current, directories, filenames in os.walk(files_dir, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                item for item in directories
                if not (current_path / item).is_symlink()
            ]
            for filename in filenames:
                source = current_path / filename
                if source.is_symlink() or not source.is_file():
                    continue
                relative = source.relative_to(files_dir)
                posix = PurePosixPath(*relative.parts)
                windows = PureWindowsPath(str(relative))
                if posix.is_absolute() or windows.is_absolute() or windows.drive:
                    warnings.append(f"Skipped unsafe path: {relative}")
                    continue
                parts = _safe_relative(tuple(relative.parts))
                if not parts:
                    warnings.append(f"Skipped unsafe path: {relative}")
                    continue
                candidates.append((parts, source))
                if len(candidates) > MAX_ENTRIES:
                    raise LaunchDirError(f"This upload contains more than {MAX_ENTRIES} files.")

        stripped = _strip_wrapper([parts for parts, _ in candidates])
        written: dict[tuple[str, ...], tuple[tuple[str, ...], Path]] = {}
        for parts, (_, source) in zip(stripped, candidates, strict=True):
            if len(parts) == 1 and parts[0].lower().endswith(".jar"):
                parts = ("mods", parts[0])
            key = tuple(part.casefold() for part in parts)
            if key in written:
                warnings.append(f"Skipped duplicate path: {'/'.join(parts)}")
                continue
            written[key] = (parts, source)

        conflicts = []
        for parts, (orig_parts, _) in written.items():
            if _excluded(orig_parts):
                continue
            target = pack_dir.joinpath(*orig_parts)
            try:
                target.resolve().relative_to(pack_dir.resolve())
            except ValueError:
                continue
            if target.exists() and not target.is_symlink() and target.is_file():
                conflicts.append("/".join(orig_parts))

        if conflicts and not replace_files:
            raise LaunchDirFileConflicts(sorted(conflicts))

        added_count = 0
        for parts, source in written.values():
            if _excluded(parts):
                continue
            target = pack_dir.joinpath(*parts)
            try:
                target.resolve().relative_to(pack_dir.resolve())
            except ValueError:
                warnings.append(f"Skipped unsafe path: {'/'.join(parts)}")
                continue
            if source.suffix.lower() == ".jar" and not zipfile.is_zipfile(source):
                warnings.append(f"Skipped invalid JAR archive: {source.name}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            added_count += 1

        file_count, total_bytes, folders, files = _actual_launch_dir_stats(pack_dir)
        if total_bytes > MAX_MODPACK_EXTRACTED_BYTES:
            raise LaunchDirError("The directory exceeds the configured expanded-size limit.")

        manifest["file_count"] = file_count
        manifest["bytes"] = total_bytes
        manifest["folders"] = folders
        manifest["files"] = files
        manifest["updated"] = True
        manifest["file_count_added"] = added_count
        if warnings:
            existing_warnings = manifest.get("warnings") or []
            manifest["warnings"] = (existing_warnings + warnings)[:250]

        temporary = pack_dir / f".{MANIFEST_NAME}.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, pack_dir / MANIFEST_NAME)
        shutil.rmtree(session, ignore_errors=True)
        return manifest
    except Exception:
        raise


def delete_launch_dir_file(raw_name: str, file_path: str) -> dict:
    """Delete a file or folder from an existing launch directory."""
    with _IMPORT_LOCK:
        return _delete_launch_dir_file(raw_name, file_path)


def _delete_launch_dir_file(raw_name: str, file_path: str) -> dict:
    name = sanitize_name(raw_name)
    pack_dir = _dir(name)
    manifest = _manifest(pack_dir)
    if not manifest:
        raise FileNotFoundError("Launch directory not found.")

    clean = PurePosixPath(*(p for p in PureWindowsPath(file_path).parts if p and p != "."))
    if not clean.parts or any(part in ("", "..") or ".." in part or "\x00" in part for part in clean.parts):
        raise LaunchDirError("Invalid file or folder path.")
    if len(clean.parts) == 1 and clean.parts[0].casefold() == MANIFEST_NAME.casefold():
        raise LaunchDirError("Cannot delete the directory manifest.")

    target = pack_dir.joinpath(*clean.parts)
    try:
        target.resolve().relative_to(pack_dir.resolve())
    except ValueError as exc:
        raise LaunchDirError("Path must stay inside the launch directory.") from exc

    if target.resolve() == pack_dir.resolve():
        raise LaunchDirError("Cannot delete the root launch directory.")

    if target.is_symlink() or not target.exists():
        raise FileNotFoundError(f"'{file_path}' does not exist.")

    if target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        raise FileNotFoundError(f"'{file_path}' does not exist.")

    parent = target.parent
    while parent != pack_dir and parent.is_dir() and not parent.is_symlink():
        try:
            if any(parent.iterdir()):
                break
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break

    file_count, total_bytes, folders, files = _actual_launch_dir_stats(pack_dir)
    manifest["file_count"] = file_count
    manifest["bytes"] = total_bytes
    manifest["folders"] = folders
    manifest["files"] = files

    temporary = pack_dir / f".{MANIFEST_NAME}.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, pack_dir / MANIFEST_NAME)
    return manifest


def delete_launch_dir(name: str) -> None:
    with _IMPORT_LOCK:
        path = _dir(name)
        if path.is_symlink() or not path.is_dir() or not _manifest(path):
            raise FileNotFoundError("Launch directory not found.")
        if assigned_profile(path.name):
            raise LaunchDirInUseError("This launch directory is assigned to a profile. Reset that profile first.")
        shutil.rmtree(path)
