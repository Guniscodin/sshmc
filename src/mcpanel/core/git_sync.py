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
    clean_url = repo_url.strip()
    if not clean_url or clean_url.startswith("-"):
        raise GitSyncError("Invalid Git repository URL.")
    if repo_dir.is_symlink():
        raise GitSyncError("The profile repository folder cannot be a symbolic link.")
    repo_dir.mkdir(parents=True, exist_ok=True)

    git_marker = repo_dir / ".git"
    if git_marker.is_symlink():
        raise GitSyncError("The local repository metadata cannot be a symbolic link.")

    # 1. Clone or Open
    if not git_marker.exists():
        logger.info(f"Cloning repository {clean_url} into {repo_dir}")
        try:
            repo = git.Repo.clone_from(
                clean_url,
                repo_dir,
                env={"GIT_TERMINAL_PROMPT": "0"},
            )
        except GitCommandError as e:
            logger.error(f"Git clone failed: {e}")
            raise GitSyncError(f"Failed to clone repo: {e}") from e
    else:
        repo = git.Repo(repo_dir)

    try:
        Path(repo.git_dir).resolve().relative_to(repo_dir.resolve())
    except ValueError as exc:
        raise GitSyncError("The local Git metadata resolves outside the profile repository folder.") from exc

    # 2. Fetch latest changes and Hard Reset
    try:
        with repo.git.custom_environment(GIT_TERMINAL_PROMPT="0"):
            origin = repo.remotes.origin
            url_changed = origin.url != clean_url
            if url_changed:
                origin.set_url(clean_url)
            origin.fetch(prune=True)
            if url_changed:
                try:
                    repo.git.remote("set-head", "origin", "-a")
                except GitCommandError:
                    pass

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
                if not repo.head.is_detached and repo.active_branch.tracking_branch():
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
        # GitPython has no per-operation timeout and threads can't be
        # cancelled; bound the wait so a black-holed remote fails the start
        # instead of leaving the profile stuck in STARTING forever. The stray
        # worker keeps running harmlessly in the background if it ever times out.
        empty = await asyncio.wait_for(
            asyncio.to_thread(_clone_or_fetch_sync, repo_url, repo_dir),
            timeout=600,
        )
        logger.info("Git sync completed successfully.")
        return empty
    except asyncio.TimeoutError as e:
        logger.error(f"Git sync timed out after 600 seconds for {repo_url}")
        raise GitSyncError(
            "Git sync timed out after 10 minutes. Check the repository URL and network connection."
        ) from e
    except GitSyncError:
        # Re-raise our custom error as-is so the lifecycle pipeline can catch it
        raise
    except Exception as e:
        # Catch generic errors (like disk full or permission denied)
        # and wrap them in GitSyncError
        logger.error(f"Unexpected error during git sync: {e}")
        raise GitSyncError(f"Unexpected git sync failure: {e}") from e