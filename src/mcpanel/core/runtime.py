import asyncio
import logging
import re
import signal
from datetime import datetime, timezone
from pathlib import Path

from mcpanel.models import Profile, RuntimeState, ProfileStatus
from mcpanel.config import PROFILES_DIR
from mcpanel.core import process, rcon, housekeeping
from mcpanel import db

logger = logging.getLogger(__name__)

_startup_tasks: dict[str, asyncio.Task] = {}
_startup_events: list[dict[str, str]] = []
_DEPENDENCY_FAILURE_MARKERS = (
    "Mod resolution failed",
    "Some of your mods are incompatible",
    "Incompatible mods found",
    "Missing or unsupported mandatory dependenc",
    "requires any version of ",
)

def drain_startup_events() -> list[dict[str, str]]:
    events = list(_startup_events)
    _startup_events.clear()
    return events

def _record_startup_event(profile: Profile, message: str) -> None:
    _startup_events.append({"profile_id": profile.id, "profile_name": profile.name, "message": message})
    del _startup_events[:-50]


def record_startup_warning(profile: Profile, message: str) -> None:
    _record_startup_event(profile, message)

async def _terminate_starting_process(pid: int, create_time: float) -> bool:
    """Stop the whole detached launch group, including run.sh's Java child."""
    if not process.is_process_alive(pid, create_time) and not process.process_group_alive(pid):
        return True
    process.send_signal(pid, create_time, signal.SIGTERM)
    exited = await asyncio.to_thread(process.wait_for_group_exit, pid, 8)
    if not exited:
        process.send_signal(pid, create_time, signal.SIGKILL)
        exited = await asyncio.to_thread(process.wait_for_group_exit, pid, 5)
    return exited

async def shutdown_starting_servers() -> None:
    """Stop startup processes before the panel exits, preventing Ctrl+C hangs."""
    tasks = list(_startup_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

def _generate_server_properties(profile: Profile, server_dir: Path) -> None:
    """Generates or updates server.properties with our custom profile settings."""
    props_path = server_dir / "server.properties"
    logger.info(f"Updating server.properties (Port: {profile.port}, RCON Port: {profile.rcon_port})")
    
    properties = {}
    if props_path.exists():
        with open(props_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    properties[k.strip()] = v.strip()

    properties.update({
        "server-port": str(profile.port),
        "enable-rcon": "true",
        "rcon.port": str(profile.rcon_port),
        "rcon.password": profile.rcon_password,
        "motd": f"SSHMC - {profile.name}"
    })

    with open(props_path, "w") as f:
        for k, v in properties.items():
            f.write(f"{k}={v}\n")

def _build_java_command(profile: Profile, server_dir: Path) -> list[str]:
    """Constructs the startup command. Handles modern Forge/NeoForge run.sh."""
    run_sh = server_dir / "run.sh"
    
    if run_sh.exists():
        # Modern Forge/NeoForge: write RAM to user_jvm_args.txt and run run.sh
        jvm_args_path = server_dir / "user_jvm_args.txt"
        with open(jvm_args_path, "w") as f:
            f.write(f"-Xmx{profile.ram_mb}M\n")
            f.write(f"-Xms{profile.ram_mb}M\n")
        logger.info("Using run.sh for server execution (Forge/NeoForge)")
        return ["sh", "run.sh", "nogui"]
    else:
        # Vanilla/Fabric: use standard java -jar
        return [
            profile.java_path,
            f"-Xmx{profile.ram_mb}M",
            f"-Xms{profile.ram_mb}M",
            "-jar", "server.jar",
            "nogui"
        ]

async def start_server(profile: Profile) -> RuntimeState:
    """Starts a server and returns its new RuntimeState."""
    server_dir = PROFILES_DIR / profile.id / "server"
    log_path = PROFILES_DIR / profile.id / "logs" / "latest.log"
    
    if not server_dir.exists():
        raise FileNotFoundError(f"Server directory missing for {profile.name}. Run sync first.")

    await asyncio.to_thread(housekeeping.prune_server_artifacts, profile.id)

    # 1. Auto-accept the Minecraft EULA
    eula_path = server_dir / "eula.txt"
    if not eula_path.exists():
        logger.info("Generating eula.txt (eula=true)")
        with open(eula_path, "w") as f:
            f.write("eula=true\n")

    # 2. Generate server.properties (Fixes port and RCON issues)
    _generate_server_properties(profile, server_dir)

    # 3. Spawn the process
    logger.info(f"Starting server for profile '{profile.name}'...")
    cmd = _build_java_command(profile, server_dir)
    
    process.prepare_server_log(log_path)
    start_offset = log_path.stat().st_size if log_path.exists() else 0
    pid, create_time = await asyncio.to_thread(
        process.spawn_detached_process, cmd, server_dir, log_path
    )
    state = RuntimeState(
        profile_id=profile.id,
        status=ProfileStatus.STARTING,
        pid=pid,
        pid_create_time=create_time,
        started_at=datetime.now(timezone.utc)
    )
    db.update_runtime_state(state)

    # Readiness is watched in the background so an HTTP request (and Uvicorn's
    # graceful shutdown) never waits five minutes for a broken mod set.
    task = asyncio.create_task(_watch_startup(profile, state, log_path, start_offset))
    _startup_tasks[profile.id] = task
    task.add_done_callback(lambda done, profile_id=profile.id: _startup_tasks.pop(profile_id, None) if _startup_tasks.get(profile_id) is done else None)
    return state

async def _watch_startup(profile: Profile, state: RuntimeState, log_path: Path, start_offset: int) -> None:
    pid, create_time = state.pid, state.pid_create_time
    if not pid or not create_time:
        return
    deadline = asyncio.get_running_loop().time() + 300
    log_position = start_offset
    try:
        log_identity = (log_path.stat().st_dev, log_path.stat().st_ino)
    except OSError:
        log_identity = None
    recent_output = ""
    try:
        while asyncio.get_running_loop().time() < deadline:
            if log_path.exists():
                try:
                    current_stat = log_path.stat()
                    current_identity = (current_stat.st_dev, current_stat.st_ino)
                    if log_identity is not None and current_identity != log_identity:
                        log_position = 0
                    log_identity = current_identity
                except OSError:
                    current_identity = log_identity
                with open(log_path, "rb") as log_file:
                    log_file.seek(log_position)
                    new_bytes = log_file.read()
                    log_position += len(new_bytes)
                if new_bytes:
                    recent_output = (recent_output + new_bytes.decode("utf-8", errors="replace"))[-8000:]
                    dependency_error = next((marker for marker in _DEPENDENCY_FAILURE_MARKERS if marker.lower() in recent_output.lower()), None)
                    if dependency_error:
                        excerpt = recent_output[-1500:].strip()
                        await _terminate_starting_process(pid, create_time)
                        state.status = ProfileStatus.CRASHED
                        state.pid = None
                        state.pid_create_time = None
                        db.update_runtime_state(state)
                        preparing = "Preparing level" in recent_output or "Preparing spawn area" in recent_output
                        missing_match = re.search(r"requires any version of ([\w.-]+), which is missing", recent_output, re.IGNORECASE)
                        missing = f" Required dependency '{missing_match.group(1)}' was not found." if missing_match else " A required mod dependency was not found."
                        message = (
                            "Minecraft stopped during mod dependency resolution."
                            + missing
                            + (" Startup ended before world creation." if not preparing else "")
                        )
                        _record_startup_event(profile, message)
                        logger.error("Startup dependency failure for %s: %s\n%s", profile.name, message, excerpt)
                        return
                    if "Done (" in recent_output:
                        state.status = ProfileStatus.RUNNING
                        db.update_runtime_state(state)
                        return
            if not process.is_process_alive(pid, create_time):
                state.status = ProfileStatus.CRASHED
                state.pid = None
                state.pid_create_time = None
                db.update_runtime_state(state)
                tail = recent_output[-1200:].strip()
                message = "Minecraft exited before becoming ready. Check the server console for the startup error."
                _record_startup_event(profile, message)
                logger.error("Minecraft exited before readiness for %s: %s", profile.name, tail or "No startup log output.")
                return
            await asyncio.sleep(0.5)

        await _terminate_starting_process(pid, create_time)
        state.status = ProfileStatus.CRASHED
        state.pid = None
        state.pid_create_time = None
        db.update_runtime_state(state)
        _record_startup_event(profile, "Minecraft did not become ready within five minutes; startup was stopped. Check the server console.")
        logger.error("Minecraft readiness timed out for %s: %s", profile.name, recent_output[-1500:])
    except asyncio.CancelledError:
        # Manual cancellation and panel shutdown both stop the complete Java
        # process group. Server/world files are deliberately left untouched.
        stopped = await asyncio.shield(_terminate_starting_process(pid, create_time))
        state.status = ProfileStatus.STOPPED if stopped else ProfileStatus.CRASHED
        if stopped:
            state.pid = None
            state.pid_create_time = None
        db.update_runtime_state(state)
        raise

async def cancel_startup(profile_id: str) -> RuntimeState | None:
    state = db.get_runtime_state(profile_id)
    if not state or state.status != ProfileStatus.STARTING:
        return None
    task = _startup_tasks.get(profile_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    else:
        if state.pid and state.pid_create_time:
            stopped = await _terminate_starting_process(state.pid, state.pid_create_time)
            state.status = ProfileStatus.STOPPED if stopped else ProfileStatus.CRASHED
            if stopped:
                state.pid = None
                state.pid_create_time = None
            db.update_runtime_state(state)
    return db.get_runtime_state(profile_id)

async def stop_server(profile: Profile, state: RuntimeState) -> RuntimeState:
    """
    Graceful shutdown sequence:
    1. Try RCON 'stop' command.
    2. Wait for exit (30s).
    3. Fallback: SIGTERM.
    4. Wait for exit (15s).
    5. Fallback: SIGKILL.
    """
    if not state.pid or not state.pid_create_time:
        logger.warning("Stop called but no PID tracked.")
        state.status = ProfileStatus.STOPPED
        return state

    # FAST-FAIL: If the process is already dead, skip timeouts
    if not process.is_process_alive(state.pid, state.pid_create_time):
        logger.info(f"Process {state.pid} is already dead. Marking as stopped.")
        state.status = ProfileStatus.STOPPED
        state.pid = None
        state.pid_create_time = None
        return state

    logger.info(f"Stopping server '{profile.name}' (PID: {state.pid})...")
    state.status = ProfileStatus.STOPPING
    
    # 1. RCON Attempt
    try:
        await rcon.send_command(
            host="127.0.0.1", 
            port=profile.rcon_port, 
            password=profile.rcon_password, 
            command="stop"
        )
        logger.info("RCON 'stop' sent. Waiting for process to exit...")
        exited = await asyncio.to_thread(process.wait_for_group_exit, state.pid, 30)
        if exited:
            state.status = ProfileStatus.STOPPED
            state.pid = None
            state.pid_create_time = None
            return state
            
    except rcon.RCONError as e:
        logger.warning(f"RCON stop failed: {e}. Falling back to SIGTERM.")

    # 2. SIGTERM Fallback
    if process.send_signal(state.pid, state.pid_create_time, signal.SIGTERM):
        logger.info("SIGTERM sent. Waiting for exit...")
        exited = await asyncio.to_thread(process.wait_for_group_exit, state.pid, 15)
        if exited:
            state.status = ProfileStatus.STOPPED
            state.pid = None
            state.pid_create_time = None
            return state

    # 3. SIGKILL Last Resort
    logger.error("Server did not exit gracefully. Sending SIGKILL.")
    process.send_signal(state.pid, state.pid_create_time, signal.SIGKILL)
    exited = await asyncio.to_thread(process.wait_for_group_exit, state.pid, 5)
    if not exited:
        state.status = ProfileStatus.CRASHED
        return state

    state.status = ProfileStatus.CRASHED
    state.pid = None
    state.pid_create_time = None
    return state
