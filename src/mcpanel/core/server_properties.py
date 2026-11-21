"""Shared helpers for server.properties and the panel's saved overrides.

Both the Settings tab and the server runtime need to read and rewrite
server.properties, and player management needs to toggle whitelist-related
keys. Keeping the parsing/writing in one module avoids the two copies drifting
apart (the runtime used to inline its own parser).
"""

import json
import logging
import os
import uuid
from pathlib import Path

from mcpanel.config import PROFILES_DIR

logger = logging.getLogger(__name__)

MAX_SAVED_SETTINGS_BYTES = 1024 * 1024


def properties_path(profile_id: str) -> Path:
    return PROFILES_DIR / profile_id / "server" / "server.properties"


def saved_settings_path(profile_id: str) -> Path:
    return PROFILES_DIR / profile_id / "server_settings.json"


def read_properties(path: Path) -> dict[str, str]:
    """Parse a Java .properties file into a dict, ignoring comments."""
    properties: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as props_file:
            for line in props_file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    properties[key.strip()] = value.strip()
    except OSError:
        return {}
    return properties


def _write_atomic(path: Path, payload: str) -> None:
    if path.is_symlink():
        raise OSError(f"{path.name} cannot be a symbolic link.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temp.open("x", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_properties(path: Path, properties: dict[str, str]) -> None:
    """Rewrite a .properties file from a dict, one key per line."""
    payload = "".join(f"{key}={value}\n" for key, value in properties.items())
    _write_atomic(path, payload)


def set_property(path: Path, key: str, value: str) -> None:
    """Read-modify-write a single properties key."""
    properties = read_properties(path)
    properties[key] = value
    write_properties(path, properties)


def read_saved_settings(profile_id: str) -> dict[str, str]:
    """User-managed server.properties values saved from the Settings tab."""
    path = saved_settings_path(profile_id)
    try:
        if path.is_symlink():
            logger.warning("Ignoring symbolic link at %s", path)
            return {}
        if path.stat().st_size > MAX_SAVED_SETTINGS_BYTES:
            logger.warning("Ignoring oversized saved settings at %s", path)
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def write_saved_settings(profile_id: str, settings: dict[str, str]) -> None:
    path = saved_settings_path(profile_id)
    _write_atomic(path, json.dumps(settings, indent=2, sort_keys=True) + "\n")


def merge_saved_settings(profile_id: str, updates: dict[str, str]) -> dict[str, str]:
    """Merge updates into the saved settings and persist them."""
    saved = read_saved_settings(profile_id)
    saved.update(updates)
    write_saved_settings(profile_id, saved)
    return saved
