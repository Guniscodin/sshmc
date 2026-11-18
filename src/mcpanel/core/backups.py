"""Manual, bounded-path profile backups with streamed archives."""
import io
import json
import logging
import os
import re
import shutil
import stat
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from mcpanel.config import BACKUP_MIN_FREE_BYTES, BACKUP_WARNING_BYTES, BACKUPS_ROOT, PROFILES_DIR
from mcpanel.models import Profile

logger = logging.getLogger(__name__)
_BACKUP_NAME = re.compile(r"backup_[0-9_]+(?:_[A-Za-z0-9_-]+)?\.tar\.gz\Z")
_SETTINGS = (
    "server.properties", "ops.json", "whitelist.json", "banned-players.json",
    "banned-ips.json", "usercache.json",
)
_active_lock = threading.Lock()
_active: dict[str, dict] = {}


class BackupError(Exception):
    """A backup could not be completed safely."""


class BackupCancelled(BackupError):
    """A user cancelled an active backup."""


class BackupInProgress(BackupError):
    """A backup is already running for this profile."""


class _CancelableReader:
    def __init__(self, source, cancel: threading.Event | None):
        self.source = source
        self.cancel = cancel

    def read(self, size: int = -1):
        if self.cancel and self.cancel.is_set():
            raise BackupCancelled("Backup cancelled.")
        return self.source.read(size)


def _valid_profile_id(profile_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
        raise ValueError("Invalid profile identifier.")


def _backup_dir(profile_id: str) -> Path:
    _valid_profile_id(profile_id)
    if os.getenv("MCPANEL_BACKUPS_DIR"):
        base = BACKUPS_ROOT
        candidate = base / profile_id
    else:
        base = PROFILES_DIR.resolve()
        profile_directory = base / profile_id
        if profile_directory.is_symlink():
            raise ValueError("Profile backup directories cannot be symbolic links.")
        candidate = profile_directory / "backups"
    base_resolved = base.resolve()
    try:
        candidate.resolve().relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError("Backup storage must remain inside its configured root.") from exc
    if candidate.is_symlink():
        raise ValueError("Backup directories cannot be symbolic links.")
    return candidate


def backup_directory(profile_id: str) -> Path:
    """Returns the validated storage location used by backup listing and monitoring."""
    return _backup_dir(profile_id)


def begin_backup(profile_id: str) -> dict:
    _valid_profile_id(profile_id)
    with _active_lock:
        if profile_id in _active:
            raise BackupInProgress("A backup is already running for this profile.")
        value = {"started_monotonic": time.monotonic(), "cancel": threading.Event()}
        _active[profile_id] = value
        return value


def finish_backup(profile_id: str) -> None:
    with _active_lock:
        _active.pop(profile_id, None)


def cancel_backup(profile_id: str) -> bool:
    with _active_lock:
        current = _active.get(profile_id)
        if not current:
            return False
        current["cancel"].set()
        return True


def is_backup_running(profile_id: str) -> bool:
    with _active_lock:
        return profile_id in _active


def backup_state(profile_id: str) -> dict | None:
    with _active_lock:
        current = _active.get(profile_id)
        if not current:
            return None
        return {"elapsed_seconds": max(0, int(time.monotonic() - current["started_monotonic"]))}


def sanitize_label(raw_label: str | None) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw_label or "").strip()).strip("-_")[:40]
    return label


def _inside_backup_root(path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(BACKUPS_ROOT.resolve() if os.getenv("MCPANEL_BACKUPS_DIR") else PROFILES_DIR.resolve())
    return resolved


def _tree_size(path: Path) -> int:
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
        except OSError:
            continue
    return total


def _selected_sources(profile: Profile, selection: dict[str, bool]) -> list[tuple[str, Path]]:
    profile_directory = PROFILES_DIR / profile.id
    if profile_directory.is_symlink():
        raise BackupError("The profile folder is a symbolic link; refusing to archive it.")
    server = profile_directory / "server"
    if server.is_symlink():
        raise BackupError("The server folder is a symbolic link; refusing to archive it.")
    try:
        server.resolve().relative_to(profile_directory.resolve())
    except ValueError as exc:
        raise BackupError("The server folder is outside the profile data directory.") from exc
    choices = []
    if selection.get("worlds"):
        choices.extend((name, server / name) for name in ("world", "world_nether", "world_the_end"))
    if selection.get("mods"):
        choices.append(("mods", server / "mods"))
    if selection.get("config"):
        choices.append(("config", server / "config"))
    if selection.get("datapacks"):
        choices.append(("datapacks", server / "datapacks"))
    if selection.get("server_settings"):
        choices.extend((name, server / name) for name in _SETTINGS)
    result = []
    for name, path in choices:
        if path.is_symlink():
            continue
        try:
            path.resolve().relative_to(server.resolve())
        except ValueError:
            continue
        if path.is_dir() or path.is_file():
            result.append((name, path))
    return result


def _make_archive(
    profile: Profile,
    selection: dict[str, bool],
    label: str | None,
    was_running: bool,
    cancel: threading.Event | None = None,
) -> dict:
    label = sanitize_label(label)
    sources = _selected_sources(profile, selection)
    selected_categories = [name for name, chosen in selection.items() if chosen]
    if not selected_categories:
        raise BackupError("Choose at least one item to include in this backup.")
    backup_dir = _backup_dir(profile.id)
    backup_dir.mkdir(parents=True, exist_ok=True)
    _inside_backup_root(backup_dir)
    estimated_bytes = sum(_tree_size(path) if path.is_dir() else path.stat().st_size for _, path in sources)
    try:
        free_bytes = shutil.disk_usage(backup_dir).free
    except OSError as exc:
        raise BackupError(f"Could not check free backup space: {exc}") from exc
    if free_bytes < estimated_bytes + BACKUP_MIN_FREE_BYTES:
        raise BackupError(
            f"Not enough free space: selected files are about {estimated_bytes // 1024**2} MiB; "
            f"at least {BACKUP_MIN_FREE_BYTES // 1024**2} MiB must remain free."
        )
    if cancel and cancel.is_set():
        raise BackupCancelled("Backup cancelled.")

    timestamp = datetime.now(timezone.utc)
    stamp = timestamp.strftime("%Y%m%d_%H%M%S_%f")
    suffix = f"_{label}" if label else ""
    archive = backup_dir / f"backup_{stamp}{suffix}.tar.gz"
    partial = backup_dir / f".backup_{stamp}{suffix}.partial"
    if archive.exists() or archive.is_symlink() or partial.exists() or partial.is_symlink():
        raise BackupError("A backup with this timestamp already exists. Try again.")
    _inside_backup_root(partial)
    manifest = {
        "included": [name for name, _ in sources],
        "selected": selected_categories,
        "label": label,
        "created_at": timestamp.isoformat(),
        "profile": {
            "name": profile.name,
            "build": profile.build.value,
            "minecraft_version": profile.mc_version,
            "loader_version": profile.loader_version,
        },
        "server_was_running": bool(was_running),
    }

    def add_path(archive_file: tarfile.TarFile, source: Path, archive_name: str) -> None:
        if cancel and cancel.is_set():
            raise BackupCancelled("Backup cancelled.")
        try:
            info = source.lstat()
        except OSError:
            return
        if stat.S_ISDIR(info.st_mode):
            member = archive_file.gettarinfo(str(source), arcname=archive_name)
            archive_file.addfile(member)
            try:
                children = sorted(source.iterdir(), key=lambda path: path.name.casefold())
            except OSError:
                return
            for child in children:
                add_path(archive_file, child, f"{archive_name}/{child.name}")
        elif stat.S_ISREG(info.st_mode):
            member = archive_file.gettarinfo(str(source), arcname=archive_name)
            with source.open("rb") as file_stream:
                archive_file.addfile(member, _CancelableReader(file_stream, cancel))

    try:
        with tarfile.open(partial, "x:gz") as archive_file:
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            manifest_info = tarfile.TarInfo("backup-manifest.json")
            manifest_info.size = len(manifest_bytes)
            archive_file.addfile(manifest_info, io.BytesIO(manifest_bytes))
            for archive_name, source in sources:
                add_path(archive_file, source, archive_name)
        if cancel and cancel.is_set():
            raise BackupCancelled("Backup cancelled.")
        os.replace(_inside_backup_root(partial), archive)
    except BaseException:
        try:
            _inside_backup_root(partial).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        raise
    # Write a small sidecar manifest so future listings don't have to
    # re-open the gzip stream. Best-effort: listing falls back to the archive.
    try:
        _sidecar_path(archive).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.warning("Could not write backup manifest sidecar for %s", archive.name)
    info = archive.stat()
    result = {"name": archive.name, "size_bytes": info.st_size, "manifest": manifest}
    # Enforce the profile's retention cap: keep the newest max_backups, oldest first out.
    keep = getattr(profile, "max_backups", 5) or 5
    pruned = prune_backups(profile.id, keep)
    if pruned:
        logger.info("Pruned %d old backup(s) for profile '%s' (kept %d): %s", len(pruned), profile.name, keep, ", ".join(pruned))
    result["pruned"] = pruned
    return result


def prune_backups(profile_id: str, keep: int) -> list[str]:
    """Delete the oldest backups beyond `keep` for a profile. Returns removed names."""
    if keep < 1:
        return []
    listing = list_backups(profile_id)["backups"]  # sorted newest first
    removed: list[str] = []
    for item in listing[keep:]:
        try:
            delete_backup(profile_id, item["name"])
            removed.append(item["name"])
        except (OSError, ValueError, FileNotFoundError) as exc:
            logger.warning("Could not prune old backup %s for profile %s: %s", item["name"], profile_id, exc)
    return removed


def create_manual_backup(
    profile: Profile,
    selection: dict[str, bool],
    label: str | None,
    was_running: bool,
    cancel: threading.Event | None = None,
) -> dict:
    try:
        return _make_archive(profile, selection, label, was_running, cancel)
    except BackupError:
        raise
    except Exception as exc:
        logger.exception("Manual backup failed for profile %s", profile.id)
        raise BackupError(f"Backup failed: {exc}") from exc


def create_change_backup(profile: Profile, cancel: threading.Event | None = None) -> dict | None:
    selection = {
        "worlds": profile.backup_worlds_on_change,
        "mods": profile.backup_mods_on_change,
        "config": profile.backup_config_on_change,
        "datapacks": profile.backup_datapacks_on_change,
        "server_settings": profile.backup_server_settings_on_change,
    }
    if not any(selection.values()):
        return None
    return create_manual_backup(profile, selection, "before-version-change", False, cancel)


AUTO_BACKUP_SELECTION = {
    "worlds": True,
    "mods": False,
    "config": False,
    "datapacks": False,
    "server_settings": False,
}


def maybe_auto_backup(profile: Profile, label: str = "auto") -> dict | None:
    """World-only backup taken after a clean server stop, when the profile opts in.

    Returns the created backup (or None); failures are logged and swallowed so
    they never break the stop path.
    """
    if not getattr(profile, "backup_on_restart", False):
        return None
    try:
        result = create_manual_backup(profile, dict(AUTO_BACKUP_SELECTION), label, was_running=False, cancel=None)
        logger.info("Automatic world backup for profile '%s': %s", profile.name, result["name"])
        return result
    except Exception as exc:
        logger.warning("Automatic world backup failed for profile '%s': %s", profile.name, exc)
        return None


def _sidecar_path(archive: Path) -> Path:
    """Path of the JSON sidecar manifest for a backup archive."""
    return archive.with_name(archive.name + ".json")


def _sidecar_manifest(path: Path) -> dict | None:
    try:
        raw = _sidecar_path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > 1024 * 1024:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _manifest_for(path: Path) -> dict | None:
    sidecar = _sidecar_manifest(path)
    if sidecar is not None:
        return sidecar
    manifest = None
    try:
        with tarfile.open(path, "r:gz") as archive:
            first_entry = archive.next()
            if first_entry is None or first_entry.name != "backup-manifest.json":
                return None
            member = archive.extractfile(first_entry)
            if member is None:
                return None
            raw = member.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return None
            value = json.loads(raw.decode("utf-8"))
            manifest = value if isinstance(value, dict) else None
    except (OSError, tarfile.TarError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if manifest is not None:
        # Backfill the sidecar so future listings skip the gzip re-open.
        try:
            _sidecar_path(path).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
    return manifest


def list_backups(profile_id: str) -> dict:
    directory = _backup_dir(profile_id)
    entries = []
    if directory.is_dir() and not directory.is_symlink():
        for path in directory.iterdir():
            try:
                if not _BACKUP_NAME.fullmatch(path.name) or not stat.S_ISREG(path.lstat().st_mode):
                    continue
                _inside_backup_root(path)
                info = path.stat()
                entries.append({
                    "name": path.name,
                    "size_bytes": info.st_size,
                    "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    "manifest": _manifest_for(path),
                })
            except (OSError, ValueError):
                continue
    entries.sort(key=lambda item: item["modified_at"], reverse=True)
    total_bytes = sum(item["size_bytes"] for item in entries)
    return {
        "backups": entries,
        "total_bytes": total_bytes,
        "warning_bytes": BACKUP_WARNING_BYTES,
        "over_limit": total_bytes > BACKUP_WARNING_BYTES,
    }


def resolve_backup(profile_id: str, filename: str) -> Path:
    if (
        not _BACKUP_NAME.fullmatch(filename) or "/" in filename or "\\" in filename
        or ".." in filename or Path(filename).is_absolute()
    ):
        raise ValueError("Invalid backup filename.")
    directory = _backup_dir(profile_id)
    candidate = directory / filename
    if candidate.is_symlink() or not candidate.is_file():
        raise FileNotFoundError(filename)
    resolved = _inside_backup_root(candidate)
    resolved.relative_to(directory.resolve())
    return resolved


def delete_backup(profile_id: str, filename: str) -> None:
    backup = resolve_backup(profile_id, filename)
    backup.unlink()
    _sidecar_path(backup).unlink(missing_ok=True)


def delete_backup_storage(profile_id: str) -> None:
    directory = _backup_dir(profile_id)
    if not directory.exists():
        return
    if directory.is_symlink():
        raise ValueError("Backup directories cannot be symbolic links.")
    resolved = _inside_backup_root(directory)
    resolved.relative_to(directory.resolve())
    shutil.rmtree(resolved)
