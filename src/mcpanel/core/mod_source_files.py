"""Safe file collection and rollback-capable folder swaps for mod sources."""

import json
import logging
import os
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath

from mcpanel.core.mod_storage import hash_file_sync, write_json_atomic

logger = logging.getLogger(__name__)


def safe_file(path: Path, root: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def tree_files(root: Path):
    if root.is_symlink() or not root.is_dir():
        return
    safe_root = root.resolve()
    for current, directories, filenames in os.walk(root, followlinks=False):
        folder = Path(current)
        directories[:] = [name for name in directories if not (folder / name).is_symlink()]
        for filename in filenames:
            path = folder / filename
            if safe_file(path, safe_root):
                yield path


def safe_target(relative: str) -> Path:
    posix, windows = PurePosixPath(relative), PureWindowsPath(relative)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive or "\\" in relative
            or not posix.parts or any(part in {"", ".", ".."} for part in posix.parts)):
        raise ValueError(f"Unsafe managed path: {relative}")
    return Path(*posix.parts)


def candidate(path: Path, relative: str, owner: str, folder: str | None = None) -> dict:
    value = {"path": path, "relative": safe_target(relative).as_posix(), "owner": owner,
             "sha256": hash_file_sync(path)}
    if folder:
        value["folder"] = folder
    return value


def old_unowned(server_dir: Path, ownership: dict, output_folders: tuple[str, ...]) -> list[dict]:
    previous = {
        item["path"]: item for item in ownership.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    kept = []
    for folder in output_folders:
        root = server_dir / folder
        for path in tree_files(root) or ():
            relative = path.relative_to(server_dir).as_posix()
            digest = hash_file_sync(path)
            managed = previous.get(relative)
            if managed and managed.get("sha256") == digest:
                continue
            if folder != "mods":
                kept.append(candidate(path, relative, "manual"))
    return kept


def copy_source_tree(source: Path, folder: str, owner: str, candidates: list[dict]) -> None:
    if source.is_symlink() or not source.is_dir():
        return
    root = source.resolve()
    for path in tree_files(source) or ():
        relative = path.relative_to(root).as_posix()
        if folder == "mods":
            if path.suffix.lower() != ".jar":
                continue
            relative = path.name
        candidates.append(candidate(path, relative, owner, folder=folder))


def copy_candidate(item: dict, destination: Path, profile_root_path: Path) -> dict:
    relative = safe_target(item["relative"])
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.resolve().relative_to(profile_root_path)
    except ValueError as exc:
        raise ValueError(f"Source path escapes profile storage: {relative}") from exc
    shutil.copy2(item["path"], target)
    record = {"path": f"{destination.name}/{relative.as_posix()}", "owner": item["owner"], "sha256": item["sha256"]}
    if item.get("source_key"):
        record["source_key"] = item["source_key"]
    return record


def _remove_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _write_marker(path: Path) -> None:
    with path.open("x", encoding="utf-8") as marker:
        marker.write("ready\n")
        marker.flush()
        os.fsync(marker.fileno())


def _marker_ready(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8") == "ready\n"
    except OSError:
        return False


def recover_staging(profile_root: Path, server_dir: Path, output_folders: tuple[str, ...]) -> None:
    for stage in server_dir.glob(".sshmc-source-stage-*"):
        if stage.is_symlink() or not stage.is_dir():
            continue
        if (stage / "RECOVERY_FAILED").exists():
            raise ValueError(f"An earlier source-folder swap could not be recovered: {stage.name}")
        if _marker_ready(stage / "COMMITTED"):
            shutil.rmtree(stage, ignore_errors=True)
            continue
        if not _marker_ready(stage / "APPLYING"):
            shutil.rmtree(stage, ignore_errors=True)
            continue
        old_root, new_root = stage / "old", stage / "new"
        for name in reversed(output_folders):
            live, old, staged = server_dir / name, old_root / name, new_root / name
            if old.exists() or old.is_symlink():
                _remove_path(live)
                os.replace(old, live)
            elif not staged.exists():
                _remove_path(live)
        try:
            targets = json.loads((stage / "manifest-targets.json").read_text(encoding="utf-8"))
            if not isinstance(targets, list):
                raise ValueError("invalid recovery manifest")
            for index in reversed(range(len(targets))):
                target = profile_root / safe_target(targets[index])
                backup = stage / "manifests-old" / f"{index}.json"
                staged = stage / "manifests-new" / f"{index}.json"
                if backup.exists():
                    target.unlink(missing_ok=True)
                    os.replace(backup, target)
                elif not staged.exists():
                    target.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.exception("Could not fully recover source manifests from %s", stage)
            try:
                _write_marker(stage / "RECOVERY_FAILED")
            except OSError:
                pass
            raise ValueError(f"Could not safely recover source manifests from {stage.name}.") from exc
        shutil.rmtree(stage, ignore_errors=True)


def install_transaction(
    server_dir: Path,
    stage_root: Path,
    ownership_path: Path,
    ownership: dict,
    output_folders: tuple[str, ...],
    extra_manifests: dict[Path, dict] | None = None,
) -> None:
    new_root, old_root = stage_root / "new", stage_root / "old"
    old_root.mkdir()
    manifest_new, manifest_old = stage_root / "manifests-new", stage_root / "manifests-old"
    manifest_new.mkdir()
    manifest_old.mkdir()
    targets = {ownership_path: ownership, **(extra_manifests or {})}
    profile_root = server_dir.parent.resolve()
    relatives, staged_manifests = [], []
    for index, (target, contents) in enumerate(targets.items()):
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"Managed manifest {target.name} must be a regular file.")
        try:
            relative = target.resolve().relative_to(profile_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Managed manifest {target.name} is outside profile storage.") from exc
        relatives.append(relative)
        staged, backup = manifest_new / f"{index}.json", manifest_old / f"{index}.json"
        write_json_atomic(staged, contents)
        staged_manifests.append((target, staged, backup))
    write_json_atomic(stage_root / "manifest-targets.json", relatives)
    folders_moved, folders_promoted = [], []
    manifests_moved, manifests_promoted = [], []
    try:
        _write_marker(stage_root / "APPLYING")
        for target, _, backup in staged_manifests:
            if target.exists():
                os.replace(target, backup)
                manifests_moved.append(target)
        for name in output_folders:
            live, old, staged = server_dir / name, old_root / name, new_root / name
            if live.is_symlink():
                raise ValueError(f"Server {name}/ cannot be a symbolic link.")
            if live.exists():
                os.replace(live, old)
                folders_moved.append(name)
            os.replace(staged, live)
            folders_promoted.append(name)
        for target, staged, _ in staged_manifests:
            os.replace(staged, target)
            manifests_promoted.append(target)
        _write_marker(stage_root / "COMMITTED")
    except Exception as exc:
        try:
            for target in reversed(manifests_promoted):
                target.unlink(missing_ok=True)
            for name in reversed(folders_promoted):
                _remove_path(server_dir / name)
            for name in reversed(folders_moved):
                old = old_root / name
                if old.exists() or old.is_symlink():
                    os.replace(old, server_dir / name)
            for target, _, backup in reversed(staged_manifests):
                if target in manifests_moved and backup.exists():
                    os.replace(backup, target)
        except Exception as rollback_error:
            try:
                _write_marker(stage_root / "RECOVERY_FAILED")
            except OSError:
                pass
            raise ValueError(f"Source swap failed and rollback needs recovery: {rollback_error}") from exc
        raise
    for name in folders_moved:
        shutil.rmtree(old_root / name, ignore_errors=True)
    for _, _, backup in staged_manifests:
        backup.unlink(missing_ok=True)
