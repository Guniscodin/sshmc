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
from mcpanel.config import DATA_DIR, LAUNCH_DIRS_DIR, UPLOADS_DIR
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


class LaunchDirExists(LaunchDirError):
    """Raised when an import would replace an existing directory."""


class LaunchDirInUse(LaunchDirError):
    """Raised when a directory is assigned to a profile and cannot be changed."""


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
        return _manifest(_dir(name))
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
    if len(parts) == 1 and parts[0].casefold() in _EXCLUDED_FILES:
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
        raise LaunchDirExists(
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
        written = {
            tuple(part.casefold() for part in parts): (parts, source)
            for parts, (_, source) in zip(stripped, candidates)
        }
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
        manifest = {
            "name": name,
            "label": (label or raw_name or "").strip() or name,
            "mc_version": version,
            "build": build,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": copied,
            "bytes": total_bytes,
            "folders": sorted(top_folders, key=str.casefold),
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
            if moved_old:
                os.replace(old, existing)
            raise
        if moved_old:
            shutil.rmtree(old, ignore_errors=True)
        return manifest
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def delete_launch_dir(name: str) -> None:
    with _IMPORT_LOCK:
        path = _dir(name)
        if path.is_symlink() or not path.is_dir() or not _manifest(path):
            raise FileNotFoundError("Launch directory not found.")
        if assigned_profile(path.name):
            raise LaunchDirInUse("This launch directory is assigned to a profile. Reset that profile first.")
        shutil.rmtree(path)
