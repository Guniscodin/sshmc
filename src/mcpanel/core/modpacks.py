"""Safe storage, inspection, and application helpers for reusable mod packs."""
import asyncio
import json
import logging
import os
import re
import shutil
import stat
import threading
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from mcpanel.config import (
    DATA_DIR,
    MAX_MODPACK_EXTRACTED_BYTES,
    MODPACKS_DIR,
    UPLOADS_DIR,
    UPLOAD_SESSION_MAX_AGE_SECONDS,
)
from mcpanel.models import BuildType, Profile

MAX_ZIP_ENTRIES = 100_000
COPY_CHUNK_SIZE = 1024 * 1024
_LOADER_NAMES = {item.value for item in BuildType}
_IMPORT_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class ModPackError(Exception):
    """A user-correctable mod pack operation failure."""


class ModPackExists(ModPackError):
    """Raised when an import would replace a pack without consent."""


class ModPackJarConflicts(ModPackError):
    """Raised when adding JARs would replace same-named pack entries."""

    def __init__(self, filenames: list[str]):
        self.filenames = filenames
        super().__init__("These JAR filenames already exist: " + ", ".join(filenames))


def sanitize_pack_name(raw_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", raw_name.strip()).strip("-_")[:64]
    if not value:
        raise ModPackError("Enter a pack name using letters, numbers, dashes, or underscores.")
    return value


def _pack_dir(name: str) -> Path:
    safe_name = sanitize_pack_name(name)
    target = (MODPACKS_DIR / safe_name).resolve()
    try:
        MODPACKS_DIR.resolve().relative_to(DATA_DIR.resolve())
        target.relative_to(MODPACKS_DIR.resolve())
    except ValueError as exc:
        raise ModPackError("Invalid mod pack name.") from exc
    return target


def _ensure_in_data(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise ModPackError("Mod pack files must stay inside the configured data directory.") from exc
    return resolved


def _ensure_safe_child(base: Path, target: Path) -> None:
    relative = target.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ModPackError("Mod pack destinations cannot pass through symbolic links.")
    _ensure_in_data(target)


def _manifest(pack_dir: Path) -> dict | None:
    if pack_dir.is_symlink() or not pack_dir.is_dir():
        return None
    try:
        value = json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def list_packs() -> list[dict]:
    MODPACKS_DIR.resolve().relative_to(DATA_DIR.resolve())
    MODPACKS_DIR.mkdir(parents=True, exist_ok=True)
    packs = []
    for folder in MODPACKS_DIR.iterdir():
        value = _manifest(folder)
        if not value:
            continue
        # jar_count/loader_counts/jars are maintained in pack.json on every
        # create/delete, so only legacy manifests need the slow rescan.
        if isinstance(value.get("jar_count"), int) and isinstance(value.get("loader_counts"), dict) and isinstance(value.get("jars"), list):
            packs.append(value)
            continue
        jars, loader_counts = _actual_pack_jar_stats(folder)
        value["jar_count"] = len(jars)
        value["loader_counts"] = loader_counts
        value["jars"] = jars
        packs.append(value)
    return sorted(packs, key=lambda pack: pack["name"].casefold())


def cleanup_temporary_data() -> None:
    """Remove abandoned uploads/staging folders and recover interrupted swaps."""
    root = MODPACKS_DIR.resolve()
    root.relative_to(DATA_DIR.resolve())
    root.mkdir(parents=True, exist_ok=True)
    uploads = UPLOADS_DIR.resolve()
    uploads.relative_to(DATA_DIR.resolve())
    uploads.mkdir(parents=True, exist_ok=True)
    for folder in root.iterdir():
        if folder.is_symlink():
            continue
        if folder.name.startswith(".staging-"):
            shutil.rmtree(_ensure_in_data(folder), ignore_errors=True)
        elif folder.name.startswith(".replaced-"):
            value = _manifest(folder)
            try:
                target = _pack_dir(value["name"]) if value and value.get("name") else None
            except ModPackError:
                target = None
            if target and not target.exists():
                os.replace(folder, target)
            else:
                shutil.rmtree(_ensure_in_data(folder), ignore_errors=True)
    for upload in uploads.iterdir():
        if re.fullmatch(r"[0-9a-f]{32}", upload.name) and upload.is_dir() and not upload.is_symlink():
            try:
                age = datetime.now(timezone.utc).timestamp() - upload.stat().st_mtime
                if age > UPLOAD_SESSION_MAX_AGE_SECONDS:
                    shutil.rmtree(_ensure_in_data(upload), ignore_errors=True)
            except (OSError, ValueError):
                continue
        elif upload.is_file() or upload.is_symlink():
            # Only reap files old enough that no upload can still own them;
            # in-flight ZIP/.part files are written before this function ever
            # runs during normal operation, but stay defensive.
            try:
                age = datetime.now(timezone.utc).timestamp() - upload.stat().st_mtime
                if age > UPLOAD_SESSION_MAX_AGE_SECONDS:
                    _ensure_in_data(upload).unlink(missing_ok=True)
            except (OSError, ValueError):
                logger.warning("Skipping unsafe temporary upload path %s", upload)


def cleanup_stale_upload_sessions() -> None:
    uploads = _ensure_in_data(UPLOADS_DIR)
    if not uploads.is_dir():
        return
    now = datetime.now(timezone.utc).timestamp()
    for session in uploads.iterdir():
        try:
            age = now - session.stat().st_mtime
        except (OSError, ValueError):
            continue
        try:
            if session.is_symlink() or session.is_file():
                # Loose files: interrupted legacy ZIP uploads and .part files.
                if age > UPLOAD_SESSION_MAX_AGE_SECONDS:
                    _ensure_in_data(session).unlink(missing_ok=True)
            elif re.fullmatch(r"[0-9a-f]{32}", session.name) and session.is_dir():
                if age > UPLOAD_SESSION_MAX_AGE_SECONDS:
                    shutil.rmtree(_ensure_in_data(session))
        except (OSError, ValueError):
            logger.warning("Could not clean stale upload %s", session.name)


async def upload_session_cleanup_loop() -> None:
    while True:
        await asyncio.to_thread(cleanup_stale_upload_sessions)
        await asyncio.sleep(60 * 60)


def get_pack(name: str | None) -> dict | None:
    if not name:
        return None
    return _manifest(_pack_dir(name))


def get_pack_directory(name: str) -> Path:
    """Return a validated pack folder for the source resolver."""
    path = _pack_dir(name)
    if path.is_symlink() or not path.is_dir() or not _manifest(path):
        raise ModPackError(f"Mod pack '{name}' is missing or invalid.")
    return path


def loader_warnings(pack: dict | None, loader: str) -> list[str]:
    if not pack:
        return []
    return [
        f"{jar['filename']}: detected {jar['loader']} metadata; profile uses {loader}."
        for jar in pack.get("jars", [])
        if jar.get("loader") not in (loader, "unknown")
    ] + [
        f"{jar['filename']}: no Fabric, Forge, or NeoForge metadata was found."
        for jar in pack.get("jars", [])
        if jar.get("loader") == "unknown"
    ]


def _entry_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw = info.filename
    if "\x00" in raw:
        raise ModPackError("The ZIP contains a path with a null byte.")
    normalized = raw.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ModPackError(f"The ZIP contains an absolute path: {raw}")
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if ".." in parts:
        raise ModPackError(f"The ZIP contains a parent-directory path: {raw}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ModPackError(f"The ZIP contains a symbolic link: {raw}")
    return parts


def _wrapper_prefix(entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]]) -> str | None:
    files = [parts for info, parts in entries if parts and not info.is_dir()]
    if not files:
        return None
    prefixes = {parts[0] for parts in files}
    if len(prefixes) != 1 or any(len(parts) == 1 for parts in files):
        return None
    prefix = next(iter(prefixes))
    if prefix.casefold() in {"mods", "config", "datapacks"}:
        return None
    return prefix


def _jar_loader(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, zlib.error):
        return "unknown"
    if "META-INF/neoforge.mods.toml" in names:
        return "neoforge"
    if "META-INF/mods.toml" in names:
        return "forge"
    if "fabric.mod.json" in names:
        return "fabric"
    return "unknown"


def _actual_pack_jar_stats(pack_dir: Path) -> tuple[list[dict], dict[str, int]]:
    jars: list[dict] = []
    loader_counts = {loader: 0 for loader in ("fabric", "forge", "neoforge", "unknown")}
    mods_dir = pack_dir / "mods"
    if mods_dir.is_symlink() or not mods_dir.is_dir():
        return jars, loader_counts
    try:
        with os.scandir(mods_dir) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".jar"):
                    continue
                try:
                    if not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                        continue
                except OSError:
                    continue
                loader = _jar_loader(Path(entry.path))
                jars.append({"filename": entry.name, "loader": loader, "size_bytes": entry.stat(follow_symlinks=False).st_size})
                loader_counts[loader] += 1
    except OSError:
        pass
    jars.sort(key=lambda item: str(item["filename"]).casefold())
    return jars, loader_counts


def _tree_bytes(root: Path) -> int:
    total = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink() or not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _valid_jar(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as jar:
            infos = jar.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(info.file_size for info in infos) > MAX_MODPACK_EXTRACTED_BYTES:
                return False
            return jar.testzip() is None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, zlib.error):
        return False


def _copy_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    total: list[int],
    cancelled: threading.Event | None,
) -> None:
    total[0] += info.file_size
    if total[0] > MAX_MODPACK_EXTRACTED_BYTES:
        raise ModPackError("The extracted pack exceeds the configured expanded-size limit.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, destination.open("wb") as output:
        copied = 0
        while True:
            if cancelled and cancelled.is_set():
                raise InterruptedError("Mod pack import was cancelled.")
            chunk = source.read(COPY_CHUNK_SIZE)
            if not chunk:
                break
            copied += len(chunk)
            if copied > info.file_size or total[0] - info.file_size + copied > MAX_MODPACK_EXTRACTED_BYTES:
                raise ModPackError("The ZIP contains invalid or oversized entries.")
            output.write(chunk)
        if copied != info.file_size:
            raise ModPackError("The ZIP contains an entry with an invalid size.")


def _publish_stage(
    stage: Path,
    name: str,
    target_loader: str,
    existing: Path | None,
    warnings: list[str],
    empty_warning: str = "No valid mod JARs, config files, or datapacks were found in the upload.",
) -> dict:
    jars, loader_counts = _actual_pack_jar_stats(stage)
    for jar in jars:
        if jar["loader"] not in (target_loader, "unknown"):
            warnings.append(f"{jar['filename']}: detected {jar['loader']} metadata; pack target is {target_loader}.")
        elif jar["loader"] == "unknown":
            warnings.append(f"{jar['filename']}: no Fabric, Forge, or NeoForge metadata was found.")
    if not jars and not (stage / "config").exists() and not (stage / "datapacks").exists():
        warnings.append(empty_warning)
    if len(warnings) > 250:
        omitted = len(warnings) - 249
        warnings = warnings[:249] + [f"{omitted} additional warnings were omitted."]

    manifest = {
        "name": name,
        "target_loader": target_loader,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "jar_count": len(jars),
        "loader_counts": loader_counts,
        "jars": jars,
        "warnings": warnings,
        "has_config": (stage / "config").is_dir(),
        "has_datapacks": (stage / "datapacks").is_dir(),
    }
    (stage / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    root = _ensure_in_data(MODPACKS_DIR)
    target = existing or _pack_dir(name)
    old_pack = root / f".replaced-{uuid.uuid4().hex}"
    moved_old = False
    if existing:
        os.replace(_ensure_in_data(existing), old_pack)
        moved_old = True
    try:
        os.replace(_ensure_in_data(stage), target)
    except Exception:
        if moved_old:
            os.replace(old_pack, target)
        raise
    if moved_old:
        shutil.rmtree(_ensure_in_data(old_pack), ignore_errors=True)
    return manifest


def import_pack(
    upload_id: str,
    raw_name: str,
    target_loader: str,
    replace: bool = False,
    cancelled: threading.Event | None = None,
) -> dict:
    with _IMPORT_LOCK:
        return _import_pack(upload_id, raw_name, target_loader, replace, cancelled)


def _session_files(session_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise ModPackError("Invalid upload session.")
    uploads = _ensure_in_data(UPLOADS_DIR)
    session = _ensure_in_data(uploads / session_id)
    if session.parent != uploads or session.is_symlink() or not session.is_dir():
        raise ModPackError("The upload session expired or was cancelled.")
    files = _ensure_in_data(session / "files")
    if files.parent != session or not files.is_dir():
        raise ModPackError("The upload session has no staged files.")
    return session, files


def _copy_session_files(
    files_dir: Path,
    stage: Path,
    allowed_top: set[str] | None = None,
    validated_jars: dict[str, bool] | None = None,
) -> list[str]:
    skipped = []
    allowed_top = allowed_top or {"mods", "config", "datapacks"}
    for top in allowed_top:
        source_root = files_dir / top
        if source_root.is_symlink() or not source_root.is_dir():
            continue
        for current, directories, filenames in os.walk(source_root, followlinks=False):
            current_path = Path(current)
            directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
            relative_dir = current_path.relative_to(files_dir)
            _ensure_safe_child(stage, stage / relative_dir)
            (stage / relative_dir).mkdir(parents=True, exist_ok=True)
            for filename in filenames:
                source = current_path / filename
                if source.is_symlink() or not source.is_file():
                    continue
                if top == "mods" and source.suffix.lower() != ".jar":
                    skipped.append(f"Skipped non-JAR file in mods/: {source.relative_to(files_dir)}")
                    continue
                target = stage / source.relative_to(files_dir)
                _ensure_safe_child(stage, target)
                target.parent.mkdir(parents=True, exist_ok=True)
                validation = (validated_jars or {}).get(source.name.casefold()) if top == "mods" else None
                if validation is False:
                    skipped.append(f"Skipped invalid JAR archive: {source.name}")
                    continue
                shutil.copyfile(source, target)
                if top == "mods" and validation is not True and not _valid_jar(target):
                    target.unlink(missing_ok=True)
                    skipped.append(f"Skipped invalid JAR archive: {source.name}")
    return skipped


def _remove_session(session: Path) -> None:
    try:
        shutil.rmtree(_ensure_in_data(session))
    except FileNotFoundError:
        pass


def import_file_session(
    session_id: str,
    raw_name: str,
    target_loader: str,
    replace: bool = False,
) -> dict:
    with _IMPORT_LOCK:
        if target_loader not in _LOADER_NAMES:
            raise ModPackError("Choose Vanilla, Fabric, Forge, or NeoForge as the pack target.")
        name = sanitize_pack_name(raw_name)
        session, files_dir = _session_files(session_id)
        root = _ensure_in_data(MODPACKS_DIR)
        root.mkdir(parents=True, exist_ok=True)
        existing = next((folder for folder in root.iterdir()
                         if folder.name.casefold() == name.casefold() and folder.is_dir()), None)
        if existing and existing.is_symlink():
            raise ModPackError("The existing pack path is not a regular directory.")
        if existing and not replace:
            raise ModPackExists(f"A mod pack named '{existing.name}' already exists.")
        if existing:
            name = existing.name
        stage = root / f".staging-{uuid.uuid4().hex}"
        stage.mkdir()
        try:
            warnings = _copy_session_files(files_dir, stage)
            result = _publish_stage(stage, name, target_loader, existing, warnings)
            _remove_session(session)
            return result
        except Exception:
            if stage.exists():
                shutil.rmtree(_ensure_in_data(stage), ignore_errors=True)
            raise


def _copy_pack_tree(source: Path, destination: Path, stage_root: Path) -> None:
    if source.is_symlink():
        raise ModPackError("Pack contents cannot contain symbolic links.")
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_pack_tree(child, destination / child.name, stage_root)
    elif source.is_file():
        _ensure_safe_child(stage_root, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def add_files_to_pack(
    session_id: str,
    raw_pack_name: str,
    replace_filenames: bool = False,
) -> dict:
    with _IMPORT_LOCK:
        pack_name = sanitize_pack_name(raw_pack_name)
        existing = _pack_dir(pack_name)
        manifest = _manifest(existing)
        if not manifest:
            raise FileNotFoundError("Mod pack not found.")
        session, files_dir = _session_files(session_id)
        incoming_dir = files_dir / "mods"
        incoming = []
        if incoming_dir.is_dir() and not incoming_dir.is_symlink():
            incoming = [
                path for path in incoming_dir.iterdir()
                if path.suffix.lower() == ".jar" and not path.is_symlink() and path.is_file()
            ]
        validated_jars = {path.name.casefold(): _valid_jar(path) for path in incoming}
        incoming = [path for path in incoming if validated_jars[path.name.casefold()]]
        existing_mods = existing / "mods"
        by_name = {
            path.name.casefold(): path.name
            for path in existing_mods.iterdir()
            if path.is_file() and not path.is_symlink()
        } if existing_mods.is_dir() and not existing_mods.is_symlink() else {}
        conflicts = sorted({by_name[path.name.casefold()] for path in incoming if path.name.casefold() in by_name})
        if conflicts and not replace_filenames:
            raise ModPackJarConflicts(conflicts)

        root = _ensure_in_data(MODPACKS_DIR)
        stage = root / f".staging-{uuid.uuid4().hex}"
        stage.mkdir()
        try:
            for folder in ("mods", "config", "datapacks"):
                source = existing / folder
                if source.exists():
                    _copy_pack_tree(source, stage / folder, stage)
            for path in incoming:
                old_name = by_name.get(path.name.casefold())
                if old_name:
                    old = stage / "mods" / old_name
                    if old.is_file() and not old.is_symlink():
                        _ensure_in_data(old).unlink()
            warnings = _copy_session_files(files_dir, stage, validated_jars=validated_jars)
            if _tree_bytes(stage) > MAX_MODPACK_EXTRACTED_BYTES:
                raise ModPackError("The mod pack exceeds the configured expanded-size limit.")
            target_loader = manifest.get("target_loader", "unknown")
            result = _publish_stage(stage, existing.name, target_loader, existing, warnings)
            _remove_session(session)
            return result
        except Exception:
            if stage.exists():
                shutil.rmtree(_ensure_in_data(stage), ignore_errors=True)
            raise


def delete_pack_jar(raw_pack_name: str, filename: str) -> None:
    if (
        filename in {".", ".."} or ".." in filename or "/" in filename or "\\" in filename
        or Path(filename).is_absolute() or PureWindowsPath(filename).drive
    ):
        raise ModPackError("Choose a JAR filename without a path.")
    if not filename.lower().endswith(".jar"):
        raise ModPackError("Only JAR files can be removed from a pack.")
    with _IMPORT_LOCK:
        pack_dir = _pack_dir(raw_pack_name)
        if not _manifest(pack_dir):
            raise FileNotFoundError("Mod pack not found.")
        mods_dir = pack_dir / "mods"
        if mods_dir.is_symlink():
            raise ModPackError("Mods folder cannot be a symbolic link.")
        target = mods_dir / filename
        if target.is_symlink() or not target.is_file():
            raise FileNotFoundError(filename)
        _ensure_in_data(target).unlink()
        manifest = _manifest(pack_dir) or {}
        jars, counts = _actual_pack_jar_stats(pack_dir)
        manifest["jar_count"] = len(jars)
        manifest["loader_counts"] = counts
        manifest["jars"] = jars
        target_loader = manifest.get("target_loader", "unknown")
        manifest["warnings"] = [
            f"{jar['filename']}: detected {jar['loader']} metadata; pack target is {target_loader}."
            for jar in jars if jar["loader"] not in (target_loader, "unknown")
        ] + [
            f"{jar['filename']}: no Fabric, Forge, or NeoForge metadata was found."
            for jar in jars if jar["loader"] == "unknown"
        ]
        temporary = pack_dir / ".pack.json.tmp"
        _ensure_safe_child(pack_dir, temporary)
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(_ensure_in_data(temporary), _ensure_in_data(pack_dir / "pack.json"))


def _import_pack(
    upload_id: str,
    raw_name: str,
    target_loader: str,
    replace: bool = False,
    cancelled: threading.Event | None = None,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ModPackError("Invalid upload identifier.")
    if target_loader not in _LOADER_NAMES:
        raise ModPackError("Choose Vanilla, Fabric, Forge, or NeoForge as the pack target.")
    name = sanitize_pack_name(raw_name)
    upload_path = UPLOADS_DIR / f"{upload_id}.zip"
    if upload_path.is_symlink():
        raise ModPackError("Uploaded ZIP cannot be a symbolic link.")
    upload = upload_path.resolve()
    try:
        upload.relative_to(DATA_DIR.resolve())
        upload.relative_to(UPLOADS_DIR.resolve())
    except ValueError as exc:
        raise ModPackError("Invalid upload path.") from exc
    if not upload.is_file():
        raise ModPackError("The uploaded ZIP has expired or was cancelled. Upload it again.")

    try:
        root = MODPACKS_DIR.resolve()
        root.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise ModPackError("Mod pack storage must remain inside the data directory.") from exc
    root.mkdir(parents=True, exist_ok=True)
    existing = next((folder for folder in MODPACKS_DIR.iterdir()
                     if folder.name.casefold() == name.casefold() and folder.is_dir()), None)
    if existing and not replace:
        raise ModPackExists(f"A mod pack named '{existing.name}' already exists.")
    if existing and existing.is_symlink():
        raise ModPackError("The existing pack path is not a regular directory.")
    if existing:
        name = existing.name

    stage = root / f".staging-{uuid.uuid4().hex}"
    stage.mkdir()
    warnings: list[str] = []
    written_names: set[str] = set()
    total = [0]
    try:
        with zipfile.ZipFile(upload) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ModPackError(f"The ZIP contains more than {MAX_ZIP_ENTRIES} entries.")
            entries = [(info, _entry_parts(info)) for info in infos]
            prefix = _wrapper_prefix(entries)
            for info, parts in entries:
                if cancelled and cancelled.is_set():
                    raise InterruptedError("Mod pack import was cancelled.")
                if not parts or info.is_dir():
                    continue
                if prefix and parts[0] == prefix:
                    parts = parts[1:]
                if not parts:
                    continue
                top = parts[0].casefold()
                if top in {"config", "datapacks"} and len(parts) > 1:
                    _copy_member(archive, info, stage.joinpath(top, *parts[1:]), total, cancelled)
                    continue
                in_mods = top == "mods"
                jar_file = parts[-1].lower().endswith(".jar")
                root_jar = len(parts) == 1 and jar_file
                if in_mods and not jar_file:
                    warnings.append(f"Skipped non-JAR file in mods/: {'/'.join(parts[1:])}")
                    continue
                if not (in_mods and jar_file or root_jar):
                    continue
                filename = parts[-1]
                collision_key = filename.casefold()
                if collision_key in written_names:
                    warnings.append(f"Skipped duplicate JAR filename: {filename}")
                    continue
                written_names.add(collision_key)
                jar_path = stage / "mods" / filename
                try:
                    _copy_member(archive, info, jar_path, total, cancelled)
                except InterruptedError:
                    raise
                except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, zlib.error) as exc:
                    jar_path.unlink(missing_ok=True)
                    warnings.append(f"Skipped unreadable JAR {filename}: {exc}")
                    continue
                if not zipfile.is_zipfile(jar_path) or not _valid_jar(jar_path):
                    jar_path.unlink(missing_ok=True)
                    warnings.append(f"Skipped invalid JAR archive: {filename}")
                    continue
        if cancelled and cancelled.is_set():
            raise InterruptedError("Mod pack import was cancelled.")
        manifest = _publish_stage(
            stage,
            name,
            target_loader,
            existing,
            warnings,
            "No valid mod JARs, config files, or datapacks were found in the ZIP.",
        )
        try:
            upload.unlink(missing_ok=True)
        except OSError:
            pass
        return manifest
    except Exception:
        shutil.rmtree(_ensure_in_data(stage), ignore_errors=True)
        raise


def delete_pack(raw_name: str) -> None:
    with _IMPORT_LOCK:
        pack_dir = _pack_dir(raw_name)
        if pack_dir.is_symlink() or not pack_dir.is_dir() or not _manifest(pack_dir):
            raise FileNotFoundError("Mod pack not found.")
        shutil.rmtree(pack_dir)


def apply_pack(profile: Profile, server_dir: Path, repo_managed_names: set[str] | None = None) -> set[str]:
    """Overlay a linked pack after Git sync and before external mod downloads."""
    if server_dir.is_symlink():
        raise ModPackError("Server directory cannot be a symbolic link.")
    _ensure_in_data(server_dir)
    pack = get_pack(profile.modpack_name)
    repo_managed_names = repo_managed_names or set()
    mods_dir = server_dir / "mods"
    manifest_path = server_dir.parent / "installed_modpack.json"
    if mods_dir.is_symlink():
        raise ModPackError("Server mods directory cannot be a symbolic link.")
    _ensure_in_data(mods_dir)
    _ensure_in_data(manifest_path)
    if manifest_path.is_symlink():
        raise ModPackError("The installed mod pack manifest cannot be a symbolic link.")
    if not pack:
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                for filename in previous.get("jars", []):
                    if isinstance(filename, str) and filename not in repo_managed_names and Path(filename).name == filename:
                        jar_path = mods_dir / filename
                        if not jar_path.is_symlink():
                            _ensure_in_data(jar_path).unlink(missing_ok=True)
                        else:
                            jar_path.unlink(missing_ok=True)
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        manifest_path.unlink(missing_ok=True)
        return set()

    pack_dir = _pack_dir(pack["name"])
    mods_dir.mkdir(parents=True, exist_ok=True)
    for jar in mods_dir.rglob("*"):
        if jar.is_file() and jar.suffix.lower() == ".jar":
            jar.unlink()
    source_mods = pack_dir / "mods"
    installed_jars = []
    if source_mods.is_dir():
        for jar in source_mods.iterdir():
            if jar.is_file() and jar.suffix.lower() == ".jar":
                destination = mods_dir / jar.name
                _ensure_safe_child(server_dir, destination)
                shutil.copy2(jar, destination)
                installed_jars.append(jar.name)
    for folder in ("config", "datapacks"):
        source = pack_dir / folder
        if source.is_dir():
            destination = server_dir / folder
            for source_path in source.rglob("*"):
                relative = source_path.relative_to(source)
                target = destination / relative
                _ensure_safe_child(server_dir, target)
                if source_path.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, target)
    manifest_path.write_text(json.dumps({"name": pack["name"], "jars": installed_jars}) + "\n", encoding="utf-8")
    return set(installed_jars)