import asyncio
import shutil
import logging
import json
from pathlib import Path

from mcpanel.models import Profile, RuntimeState, ProfileStatus
from mcpanel.config import PROFILES_DIR
from mcpanel.core import runtime, git_sync, loader, mod_downloads, modpacks, backups

logger = logging.getLogger(__name__)

class LifecycleError(Exception):
    """Raised when a lifecycle operation (start/stop/restart) fails."""
    pass

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
    """Removes the world, mods, configs, and old server JAR."""
    server_dir = PROFILES_DIR / profile.id / "server"
    logger.warning(f"Wiping server directory for profile '{profile.name}' due to build changes.")
    
    # Clear old world/mod content and loader-specific caches before reinstalling.
    for folder in ["world", "world_nether", "world_the_end", "mods", "config", "datapacks", "libraries", ".fabric", "versions"]:
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
    create_restart_backup: bool = False,
) -> RuntimeState:
    """The main restart pipeline."""
    server_dir = PROFILES_DIR / profile.id / "server"
    repo_dir = PROFILES_DIR / profile.id / "repo"
    server_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initiating restart pipeline for '{profile.name}'...")

    # 1. Stop if running
    if current_state.status == ProfileStatus.RUNNING:
        current_state = await runtime.stop_server(profile, current_state)

    # 2. Smart Wipe Check. Mods/config/datapacks are always backed up; worlds
    # join the archive only when the user opted in on the profile.
    should_wipe = force_wipe or _needs_wipe(profile, server_dir)
    if should_wipe or (create_restart_backup and profile.backup_on_restart):
        try:
            _, backup_warning = await asyncio.to_thread(
                backups.create_backup,
                profile,
                bool(should_wipe and profile.backup_worlds_on_change),
                profile.exclude_regenerable_backup_folders,
            )
        except Exception as exc:
            logger.exception("Unexpected backup error for %s", profile.name)
            backup_warning = f"Backup could not be created ({exc}). Startup will continue."
        if backup_warning:
            logger.warning("Backup warning for %s: %s", profile.name, backup_warning)
            runtime.record_startup_warning(profile, backup_warning)
    if should_wipe:
        _wipe_server_directory(profile)

    # 4. Sync Git
    if profile.git_url:
        try:
            await git_sync.sync_git_repository(profile.git_url, repo_dir, server_dir)
        except git_sync.GitSyncError as e:
            logger.error(f"Restart aborted: Git sync failed. {e}")
            current_state.status = ProfileStatus.CRASHED
            raise LifecycleError(f"Failed to sync repository: {e}") from e
    else:
        logger.info("No Git URL provided. Skipping repository sync.")

    repo_mods_dir = repo_dir / "mods"
    repo_managed_names = (
        {path.name for path in repo_mods_dir.iterdir() if path.is_file()}
        if profile.git_url and repo_mods_dir.is_dir()
        else set()
    )

    try:
        pack_managed_names = modpacks.apply_pack(profile, server_dir, repo_managed_names)
    except (modpacks.ModPackError, OSError) as e:
        logger.error("Failed to apply mod pack for %s: %s", profile.name, e)
        current_state.status = ProfileStatus.CRASHED
        raise LifecycleError(f"Failed to apply mod pack: {e}") from e
    repo_managed_names.update(pack_managed_names)

    # Apply the selected pack after Git and before direct links. A failed link
    # pauses startup while preserving synced files and earlier downloads.
    try:
        await mod_downloads.install_configured_mods(
            profile,
            server_dir,
            skip_indexes=skip_mod_download_indexes,
            repo_managed_names=repo_managed_names,
        )
    except mod_downloads.ModDownloadFailure:
        current_state.status = ProfileStatus.STOPPED
        raise

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
