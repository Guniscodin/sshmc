import asyncio
import shutil
import logging
import json
from pathlib import Path

from mcpanel.models import Profile, RuntimeState, ProfileStatus
from mcpanel.config import PROFILES_DIR
from mcpanel.core import runtime, git_sync, loader, mod_downloads, modpacks, backups, mod_sources

logger = logging.getLogger(__name__)

class LifecycleError(Exception):
    """Raised when a lifecycle operation (start/stop/restart) fails."""
    def __init__(self, message: str, preflight: mod_sources.SourcePreflightError | None = None):
        super().__init__(message)
        self.preflight = preflight

def _needs_wipe(profile: Profile, server_dir: Path) -> bool:
    """Checks if the current server installation matches the profile settings."""
    meta_file = server_dir / "profile_meta.json"
    if not meta_file.exists():
        return False # Fresh install, no need to wipe
    
    try:
        with open(meta_file, "r") as f:
            old_data = json.load(f)
        
        # If any of these changed, the world/mods are likely incompatible
        return (
            old_data.get("build") != profile.build.value or
            old_data.get("mc_version") != profile.mc_version or
            old_data.get("loader_version") != profile.loader_version
        )
    except Exception as e:
        logger.error(f"Failed to read profile_meta.json: {e}")
        return False

def _wipe_server_directory(profile: Profile) -> None:
    """Removes the world folders, loader libraries and caches; mods/configs are owned by the source staging layer."""
    server_dir = PROFILES_DIR / profile.id / "server"
    logger.warning(f"Wiping server directory for profile '{profile.name}' due to build changes.")

    # Clear old world/mod content and loader-specific caches before reinstalling.
    for folder in ["world", "world_nether", "world_the_end", "libraries", ".fabric", "versions"]:
        target = server_dir / folder
        if target.exists():
            shutil.rmtree(target)
            logger.debug(f"Deleted {target}")

    # Remove specific files
    files_to_remove = ["server.jar", "run.sh", "user_jvm_args.txt", "profile_meta.json"]
    for file_name in files_to_remove:
        target = server_dir / file_name
        if target.exists():
            target.unlink()
            logger.debug(f"Deleted {target}")

async def restart_profile(
    profile: Profile,
    current_state: RuntimeState,
    force_wipe: bool = False,
    skip_mod_download_indexes: set[int] | None = None,
) -> RuntimeState:
    """The main restart pipeline."""
    server_dir = PROFILES_DIR / profile.id / "server"
    repo_dir = PROFILES_DIR / profile.id / "repo"
    server_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initiating restart pipeline for '{profile.name}'...")

    # 1. Stop if running. Callers (the API) claim RUNNING -> STOPPING before
    # invoking the pipeline, so reaching this branch means an unsynchronized
    # caller — fail loudly instead of stopping the process untracked.
    if current_state.status == ProfileStatus.RUNNING:
        raise LifecycleError(
            "restart_profile was called with a RUNNING server without claiming it first."
        )

    # 2. Back up only the categories explicitly selected for this pending replacement.
    version_change_pending = _needs_wipe(profile, server_dir)
    should_wipe = force_wipe or version_change_pending
    if version_change_pending:
        requested_backup = any((
            profile.backup_worlds_on_change,
            profile.backup_mods_on_change,
            profile.backup_config_on_change,
            profile.backup_datapacks_on_change,
            profile.backup_server_settings_on_change,
        ))
        try:
            if requested_backup:
                active_backup = backups.begin_backup(profile.id)
                try:
                    await asyncio.to_thread(backups.create_change_backup, profile, active_backup["cancel"])
                finally:
                    backups.finish_backup(profile.id)
        except backups.BackupError as exc:
            raise LifecycleError(f"Replacement aborted because the selected backup could not be created: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected backup error for %s", profile.name)
            if requested_backup:
                raise LifecycleError(f"Replacement aborted because the selected backup failed: {exc}") from exc
    # Resolve every mod/data source and complete preflight before changing the
    # live source folders or intentionally replacing the existing world.
    try:
        result = await mod_sources.build_profile_sources(
            profile, repo_dir, server_dir, skip_link_indexes=skip_mod_download_indexes
        )
        for warning in result["warnings"]:
            runtime.record_startup_warning(profile, warning)
    except mod_sources.SourcePreflightError as exc:
        raise LifecycleError(str(exc), preflight=exc) from exc
    except (git_sync.GitSyncError, modpacks.ModPackError) as exc:
        raise LifecycleError(f"Could not resolve mod sources: {exc}") from exc
    except mod_downloads.ModDownloadFailure:
        current_state.status = ProfileStatus.STOPPED
        raise
    except Exception as exc:
        logger.exception("Could not stage mod sources for %s", profile.name)
        raise LifecycleError(f"Could not stage profile mod sources: {exc}") from exc

    if should_wipe:
        await asyncio.to_thread(_wipe_server_directory, profile)
    if version_change_pending:
        # The backup_*_on_change flags are a one-shot selection consumed by the
        # backup above, not persistent preferences. Resetting them to the
        # worlds-only default here mirrors the edit modal, which also resets
        # them every time it opens without a pending replacement. Without this
        # reset, stale selections would silently apply to the next change.
        profile.backup_worlds_on_change = True
        profile.backup_mods_on_change = False
        profile.backup_config_on_change = False
        profile.backup_datapacks_on_change = False
        profile.backup_server_settings_on_change = False
        from mcpanel import db
        db.update_profile(profile)

    # 5. Install Loader (only if jar is missing or we just wiped)
    server_jar = server_dir / "server.jar"
    run_sh = server_dir / "run.sh"
    
    if not server_jar.exists() and not run_sh.exists():
        try:
            await loader.install_server(
                build=profile.build,
                mc_version=profile.mc_version,
                loader_version=profile.loader_version,
                dest_dir=server_dir,
                java_path=profile.java_path
            )
            # Save the metadata so we know what version is installed
            with open(server_dir / "profile_meta.json", "w") as f:
                json.dump({
                    "build": profile.build.value,
                    "mc_version": profile.mc_version,
                    "loader_version": profile.loader_version
                }, f)
                
        except loader.LoaderInstallError as e:
            logger.error(f"Restart aborted: Loader install failed. {e}")
            current_state.status = ProfileStatus.CRASHED
            raise LifecycleError(f"Failed to install server loader: {e}") from e

    # 6. Start the server
    try:
        new_state = await runtime.start_server(profile)
        return new_state
    except Exception as e:
        logger.error(f"Restart failed during server startup: {e}")
        current_state.status = ProfileStatus.CRASHED
        message = str(e).strip() or type(e).__name__
        raise LifecycleError(f"Failed to start server process: {message[-2000:]}") from e
