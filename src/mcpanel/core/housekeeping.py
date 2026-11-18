"""Bounded cleanup for regenerable Minecraft server artifacts."""
import logging
import os
import stat
import time
import json
import shutil
from pathlib import Path

from mcpanel.config import (
    CRASH_REPORT_KEEP,
    CRASH_REPORT_RETENTION_DAYS,
    DATA_DIR,
    PROFILES_DIR,
    SERVER_ROTATED_LOG_KEEP,
)

logger = logging.getLogger(__name__)

# Unreferenced mod cache entries younger than this are kept — an install may
# have linked the file before its manifest entry was written.
_CACHE_PRUNE_MIN_AGE_SECONDS = 300


def _inside_data(path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(DATA_DIR.resolve())
    return resolved


def _regular_files(directory: Path, suffix: str | None = None) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        return []
    try:
        _inside_data(directory)
        paths = []
        for path in directory.iterdir():
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                if suffix is None or path.name.endswith(suffix):
                    _inside_data(path)
                    paths.append(path)
            except (OSError, ValueError):
                continue
        return paths
    except (OSError, ValueError):
        return []


def _mtime_or_minus_inf(path: Path) -> float:
    """Sort key that never raises; files deleted mid-scan sort last (and are
    skipped afterward anyway)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def prune_server_artifacts(profile_id: str) -> None:
    server_dir = PROFILES_DIR / profile_id / "server"
    if server_dir.is_symlink():
        logger.warning("Skipping server artifact cleanup for symbolic-link server directory %s", profile_id)
        return
    try:
        server_dir = _inside_data(server_dir)
    except (OSError, ValueError):
        logger.warning("Skipping server artifact cleanup outside the data directory for %s", profile_id)
        return

    log_files = sorted(
        _regular_files(server_dir / "logs", ".log.gz"),
        key=_mtime_or_minus_inf,
        reverse=True,
    )
    for stale in log_files[SERVER_ROTATED_LOG_KEEP:]:
        try:
            _inside_data(stale).unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("Could not remove old server log %s", stale)

    reports_dir = server_dir / "crash-reports"
    reports = sorted(_regular_files(reports_dir), key=_mtime_or_minus_inf, reverse=True)
    protected = {path.name for path in reports[:CRASH_REPORT_KEEP]}
    cutoff = time.time() - CRASH_REPORT_RETENTION_DAYS * 24 * 60 * 60
    for report in reports:
        try:
            if report.name not in protected and report.stat().st_mtime < cutoff:
                _inside_data(report).unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("Could not remove old crash report %s", report)


def prune_unreferenced_mod_caches(profile) -> int:
    """Remove cached mod files nothing references anymore.

    Two re-downloadable stores are scanned:
      - modrinth_mods/*.jar not cited by the profile's Modrinth manifest, and
      - downloaded_mods/* entries not referenced by a configured direct link.

    Anything younger than 5 minutes is kept (an install may be linking it as
    we scan). Returns the number of entries removed.
    """
    profile_id = profile.id
    root = PROFILES_DIR / profile_id
    if root.is_symlink():
        return 0
    try:
        root = _inside_data(root)
    except (OSError, ValueError):
        return 0

    removed = 0
    now = time.time()

    def fresh_enough(path: Path) -> bool:
        try:
            return (now - path.stat().st_mtime) < _CACHE_PRUNE_MIN_AGE_SECONDS
        except OSError:
            return True

    # 1. Modrinth JAR cache: keep jars cited by the manifest (cache_filename
    #    or <sha256>.jar), everything else is dead weight.
    manifest_path = root / "mods.json"
    referenced: set[str] = set()
    try:
        if manifest_path.is_file() and not manifest_path.is_symlink():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest.get("mods", []) if isinstance(manifest, dict) else []:
                if not isinstance(item, dict):
                    continue
                cache_name = item.get("cache_filename")
                if isinstance(cache_name, str) and Path(cache_name).name == cache_name:
                    referenced.add(cache_name)
                sha256 = item.get("sha256")
                if isinstance(sha256, str):
                    referenced.add(f"{sha256}.jar")
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not parse the mod manifest for cache pruning (%s); keeping the Modrinth cache untouched.", manifest_path)
        referenced = None

    modrinth_cache = root / "modrinth_mods"
    if referenced is not None and modrinth_cache.is_dir() and not modrinth_cache.is_symlink():
        for entry in modrinth_cache.iterdir():
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                if entry.name.startswith(".") or entry.name.endswith(".part"):
                    continue
                if entry.name in referenced or fresh_enough(entry):
                    continue
                _inside_data(entry).unlink(missing_ok=True)
                removed += 1
            except (OSError, ValueError) as exc:
                logger.warning("Could not remove unreferenced Modrinth cache file %s: %s", entry, exc)

    # 2. Direct-link download cache: keep keys referenced by configured links.
    from mcpanel.core import mod_downloads

    download_cache = root / "downloaded_mods"
    keys = {mod_downloads.source_key(item, index) for index, item in enumerate(profile.mod_downloads)}
    keys.update(mod_downloads.legacy_cache_key(item) for item in profile.mod_downloads)
    if download_cache.is_dir() and not download_cache.is_symlink():
        for entry in download_cache.iterdir():
            try:
                if entry.is_symlink():
                    continue
                if entry.name in keys or fresh_enough(entry):
                    continue
                target = _inside_data(entry)
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.is_file():
                    target.unlink(missing_ok=True)
                else:
                    continue
                removed += 1
            except (OSError, ValueError) as exc:
                logger.warning("Could not remove unreferenced download cache entry %s: %s", entry, exc)

    if removed:
        logger.info("Pruned %d unreferenced mod cache entr%s for profile '%s'", removed, "y" if removed == 1 else "ies", profile.name)
    return removed
