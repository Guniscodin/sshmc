import os
import signal
import subprocess
import logging
import sys
from pathlib import Path
import psutil
import time
from mcpanel.config import DATA_DIR, SERVER_LOG_BACKUP_COUNT, SERVER_LOG_MAX_BYTES

logger = logging.getLogger(__name__)

class ProcessError(Exception):
    """Raised when an OS-level process operation fails."""
    pass


def _inside_data(path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(DATA_DIR.resolve())
    return resolved


def _rotate_server_log(log_file_path: Path) -> None:
    _inside_data(log_file_path)
    oldest = log_file_path.with_name(log_file_path.name + f".{SERVER_LOG_BACKUP_COUNT}")
    _inside_data(oldest).unlink(missing_ok=True)
    for index in range(SERVER_LOG_BACKUP_COUNT - 1, 0, -1):
        source = log_file_path.with_name(log_file_path.name + f".{index}")
        destination = log_file_path.with_name(log_file_path.name + f".{index + 1}")
        if source.exists():
            os.replace(_inside_data(source), _inside_data(destination))
    if log_file_path.exists():
        os.replace(_inside_data(log_file_path), _inside_data(log_file_path.with_name(log_file_path.name + ".1")))


def prepare_server_log(log_file_path: Path) -> None:
    """Rotate an oversized prior log before startup offsets are captured."""
    if log_file_path.is_symlink():
        raise ProcessError("Minecraft log path cannot be a symbolic link.")
    _inside_data(log_file_path)
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    if log_file_path.exists() and log_file_path.stat().st_size >= SERVER_LOG_MAX_BYTES:
        _rotate_server_log(log_file_path)

def spawn_detached_process(cmd: list[str], cwd: Path, log_file_path: Path) -> tuple[int, float]:
    """
    Spawns a process detached from the parent (panel).
    Returns a tuple of (PID, create_time).
    """
    # The detached relay owns the output file so rotation never races Java's file descriptor.
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        _inside_data(log_file_path)
        writer = Path(__file__).with_name("log_writer.py")
        writer_cmd = [
            sys.executable, str(writer), str(log_file_path), str(DATA_DIR),
            str(SERVER_LOG_MAX_BYTES), str(SERVER_LOG_BACKUP_COUNT), "--", *cmd,
        ]
        process = subprocess.Popen(
            writer_cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
            
        # Get process create_time immediately to prevent race conditions
        proc_info = psutil.Process(process.pid)
        create_time = proc_info.create_time()
        
        logger.info(f"Spawned detached process PID {process.pid} (Created: {create_time})")
        return process.pid, create_time
        
    except (OSError, psutil.NoSuchProcess) as e:
        # Catch OSError (invalid command) and NoSuchProcess (process died instantly)
        logger.error(f"Failed to spawn process: {e}")
        raise ProcessError(f"Could not start server process: {e}") from e

def is_process_alive(pid: int, expected_create_time: float) -> bool:
    """
    Checks if a PID is alive AND matches the expected creation time.
    This prevents the PID Reuse bug.
    """
    try:
        proc = psutil.Process(pid)
        # Check if it matches create_time
        if proc.create_time() == expected_create_time:
            return True
        return False
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        logger.warning(f"Access denied verifying creation time for PID {pid}; refusing to trust it.")
        return False

def send_signal(pid: int, expected_create_time: float, sig: signal.Signals) -> bool:
    """Signals the detached group, even if its shell leader exited before Java."""
    leader_alive = is_process_alive(pid, expected_create_time)
    # A surviving group keeps its PGID reserved after its leader exits, so this
    # also safely reaches Java when `sh run.sh` has already gone away.
    if not leader_alive and not process_group_alive(pid):
        return False
        
    try:
        os.killpg(pid, sig)
        logger.info(f"Sent signal {sig.name} to process group {pid}")
        return True
    except OSError as e:
        logger.error(f"Failed to send signal {sig.name} to {pid}: {e}")
        return False

def process_group_alive(pgid: int) -> bool:
    """Returns whether any non-zombie process remains in the detached group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    for proc in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(proc.info["pid"]) == pgid and proc.info["status"] != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return False

def wait_for_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_group_alive(pgid):
            return True
        time.sleep(0.2)
    return not process_group_alive(pgid)

def wait_for_exit(pid: int, expected_create_time: float, timeout: int) -> bool:
    """Blocks until process exits or timeout. Returns True if exited."""
    try:
        proc = psutil.Process(pid)
        if proc.create_time() != expected_create_time:
            return True # Process already died and PID was reused
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        return False
    except psutil.NoSuchProcess:
        return True
