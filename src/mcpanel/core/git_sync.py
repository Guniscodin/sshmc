import asyncio
import logging
from pathlib import Path
from typing import Optional
import git
from git.exc import GitCommandError

logger = logging.getLogger(__name__)

class GitSyncError(Exception):
    """Raised when a git operation fails."""
    pass

def _clone_or_fetch_sync(repo_url: str, repo_dir: Path) -> bool:
    """Fetch/reset the profile checkout; return True when the remote is empty."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Clone or Open
    if not (repo_dir / ".git").exists():
        logger.info(f"Cloning repository {repo_url} into {repo_dir}")
        try:
            repo = git.Repo.clone_from(repo_url, repo_dir)
        except GitCommandError as e:
            logger.error(f"Git clone failed: {e}")
            raise GitSyncError(f"Failed to clone repo: {e}") from e
    else:
        git_marker = repo_dir / ".git"
        if git_marker.is_symlink():
            raise GitSyncError("The local repository metadata cannot be a symbolic link.")
        repo = git.Repo(repo_dir)

    try:
        Path(repo.git_dir).resolve().relative_to(repo_dir.resolve())
    except ValueError as exc:
        raise GitSyncError("The local Git metadata resolves outside the profile repository folder.") from exc

    # 2. Fetch latest changes and Hard Reset
    try:
        origin = repo.remotes.origin
        if origin.url != repo_url:
            origin.set_url(repo_url)
        origin.fetch(prune=True)

        # Empty repositories have no remote refs and no commit to reset to. Treat
        # them as a configured repo with no files yet, preserving local folders.
        remote_refs = [ref for ref in origin.refs if ref.name != "origin/HEAD"]
        if not remote_refs:
            logger.warning("Configured Git repository is empty; no Git files will be applied.")
            return True

        # Prefer the remote's default branch, then the active branch's upstream.
        try:
            repo.git.rev_parse("--verify", "origin/HEAD^{commit}")
            remote_branch_ref = "origin/HEAD"
        except GitCommandError:
            if repo.active_branch.tracking_branch():
                remote_branch_ref = repo.active_branch.tracking_branch().name
            else:
                # Some hosts do not advertise origin/HEAD. Pick an available
                # remote ref instead of assuming the default branch is main.
                remote_ref_names = sorted(ref.name for ref in remote_refs)
                remote_branch_ref = next(
                    (name for name in remote_ref_names if name == "origin/main"),
                    remote_ref_names[0],
                )

        try:
            repo.git.rev_parse("--verify", f"{remote_branch_ref}^{{commit}}")
        except GitCommandError as e:
            raise GitSyncError(
                "Repository has remote refs but no usable commit to sync."
            ) from e

        logger.info(f"Hard resetting {repo_dir} to {remote_branch_ref}")
        
        # Hard reset forces the local repo to exactly match the remote
        repo.git.reset('--hard', remote_branch_ref)
        
        # NEVER use 'git clean -fdx' here. It will delete untracked files 
        # (like the world folder if the user made a mistake).
        
    except GitCommandError as e:
        logger.error(f"Git sync failed: {e}")
        raise GitSyncError(f"Failed to sync repo: {e}") from e

    return False

async def sync_git_repository(repo_url: Optional[str], repo_dir: Path, server_dir: Path | None = None) -> bool:
    """Fetch the Git checkout without modifying the live server tree."""
    if not repo_url:
        logger.info("No Git URL provided for this profile. Skipping Git sync.")
        return False
        
    logger.info(f"Starting git sync for {repo_url}")
    try:
        empty = await asyncio.to_thread(_clone_or_fetch_sync, repo_url, repo_dir)
        logger.info("Git sync completed successfully.")
        return empty
    except GitSyncError:
        # Re-raise our custom error as-is so the lifecycle pipeline can catch it
        raise
    except Exception as e:
        # Catch generic errors (like disk full or permission denied) 
        # and wrap them in GitSyncError
        logger.error(f"Unexpected error during git sync: {e}")
        raise GitSyncError(f"Unexpected git sync failure: {e}") from e
