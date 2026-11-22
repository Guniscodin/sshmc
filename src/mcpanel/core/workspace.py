"""Resolve the effective server folder and keep profile worlds isolated.

A profile normally runs from ``profiles/<id>/server``.  When the user assigns
an imported launch directory, the server runs directly from that folder
instead.  Worlds always stay in the profile's own server folder; they are
exposed inside the launch directory through symbolic links so the server can
still use its normal relative ``level-name`` lookup.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from mcpanel import db
from mcpanel.core import launch_dirs, mod_storage, server_properties
from mcpanel.models import Profile

logger = logging.getLogger(__name__)

_WORLD_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_RUNTIME_FILES = (
    "server.properties", "eula.txt", "profile_meta.json", "server-icon.png",
    "server.jar", "run.sh", "run.bat", "user_jvm_args.txt",
    "installer.jar", "installer.jar.log",
)
_RUNTIME_DIRS = ("libraries", "versions", ".fabric")


def default_server_dir(profile: Profile) -> Path:
    return mod_storage.profile_root(profile.id) / "server"


def server_dir(profile: Profile) -> Path:
    """The folder the server process should use as its working directory."""
    if profile.launch_dir:
        return launch_dirs.get_launch_dir_path(profile.launch_dir)
    return default_server_dir(profile)


def server_dir_for_id(profile_id: str) -> Path:
    profile = db.get_profile(profile_id)
    if not profile:
        raise FileNotFoundError("Profile not found.")
    return server_dir(profile)


def _world_names(profile: Profile) -> list[str]:
    name = "world"
    try:
        props = server_properties.read_properties(
            server_properties.properties_path(profile.id)
        )
    except OSError:
        props = {}
    saved = server_properties.read_saved_settings(profile.id)
    candidate = (props.get("level-name") or saved.get("level-name") or "world").strip()
    if candidate and _WORLD_NAME.fullmatch(candidate) and candidate not in (".", ".."):
        name = candidate
    names = [name]
    if name == "world":
        names.extend(["world_nether", "world_the_end"])
    return names


def ensure_world_links(profile: Profile) -> None:
    """Expose profile-owned worlds inside the launch directory.

    Only called when a launch directory is assigned.  Existing pack content is
    never replaced: if a real folder with the same name exists, the launch is
    rejected instead of silently hiding it.
    """
    if not profile.launch_dir:
        return
    launch_dir = launch_dirs.get_launch_dir_path(profile.launch_dir)
    profile_server = default_server_dir(profile)
    profile_server.mkdir(parents=True, exist_ok=True)
    for name in _world_names(profile):
        target = (profile_server / name).resolve()
        try:
            target.relative_to(profile_server.resolve())
        except ValueError as exc:
            raise launch_dirs.LaunchDirError("Profile world path is invalid.") from exc
        target.mkdir(parents=True, exist_ok=True)
        link = launch_dir / name
        if link.is_symlink():
            try:
                if link.resolve() == target.resolve():
                    continue
            except (OSError, ValueError):
                pass
            link.unlink(missing_ok=True)
        elif link.exists():
            raise launch_dirs.LaunchDirError(
                f"The launch directory contains a real '{name}' folder. Remove it before using this directory so the profile's own world is not overwritten."
            )
        os.symlink(target, link, target_is_directory=True)
        logger.info("Linked launch directory world %s -> %s", link, target)


def release_world_links(profile: Profile) -> None:
    """Remove links and runtime files created in a launch directory for a profile."""
    if not profile.launch_dir:
        return
    try:
        launch_dir = launch_dirs.get_launch_dir_path(profile.launch_dir)
    except launch_dirs.LaunchDirError:
        return
    profile_server = default_server_dir(profile).resolve()
    try:
        entries = list(launch_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            try:
                entry.resolve().relative_to(profile_server)
            except ValueError:
                continue
            entry.unlink(missing_ok=True)
    for name in _RUNTIME_FILES:
        path = launch_dir / name
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
    for name in _RUNTIME_DIRS:
        path = launch_dir / name
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def launch_dir_compatible(profile: Profile) -> bool:
    manifest = launch_dirs.get_launch_dir(profile.launch_dir) if profile.launch_dir else None
    return bool(manifest and launch_dirs.is_compatible(manifest, profile))


def clean_runtime_artifacts(profile: Profile) -> None:
    """Remove loader/runtime files from the current effective server directory."""
    directory = server_dir(profile)
    for name in _RUNTIME_FILES:
        path = directory / name
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
    for name in _RUNTIME_DIRS:
        path = directory / name
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
