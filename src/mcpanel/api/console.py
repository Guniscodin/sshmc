import asyncio
import logging
import os
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import aiofiles
from mcpanel.config import PROFILES_DIR
from mcpanel import db
from mcpanel.security import SESSION_COOKIE, credentials_configured, same_origin, valid_session_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["console"])

ACTIVE_WEBSOCKETS: set[WebSocket] = set()
_SHUTDOWN_EVENT: asyncio.Event = asyncio.Event()


def reset_shutdown_state() -> None:
    _SHUTDOWN_EVENT.clear()


async def close_all_consoles() -> None:
    """Signal all active console WebSockets to exit immediately during server shutdown."""
    _SHUTDOWN_EVENT.set()
    for ws in list(ACTIVE_WEBSOCKETS):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except Exception:
            pass
    ACTIVE_WEBSOCKETS.clear()


@router.websocket("/ws/{profile_id}/console")
async def websocket_console(websocket: WebSocket, profile_id: str):
    if not credentials_configured():
        await websocket.close(code=1011, reason="Panel password is not configured")
        return
    if not valid_session_token(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=4401, reason="Authentication required")
        return
    if not same_origin(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=4403, reason="Same-origin connection required")
        return
    if not db.get_profile(profile_id):
        await websocket.close(code=4404, reason="Profile not found")
        return
    await websocket.accept()

    ACTIVE_WEBSOCKETS.add(websocket)
    disconnect_event = asyncio.Event()

    async def _watch_disconnect() -> None:
        try:
            while not disconnect_event.is_set() and not _SHUTDOWN_EVENT.is_set():
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            disconnect_event.set()

    watch_task = asyncio.create_task(_watch_disconnect())

    async def _interruptible_sleep(duration: float) -> bool:
        """Sleep for duration, but return immediately if disconnect or shutdown occurs.

        Returns True if disconnected or shutting down, False if normal sleep completed.
        """
        if disconnect_event.is_set() or _SHUTDOWN_EVENT.is_set():
            return True
        try:
            await asyncio.wait_for(disconnect_event.wait(), timeout=duration)
            return True
        except asyncio.TimeoutError:
            return False

    try:
        # Minecraft writes its own structured log in its working directory. The
        # detached relay log remains a fallback for early boot/process errors.
        from mcpanel.core import launch_dirs, workspace
        try:
            server_dir = workspace.server_dir_for_id(profile_id)
        except (FileNotFoundError, launch_dirs.LaunchDirError, OSError):
            server_dir = PROFILES_DIR / profile_id / "server"
        server_log = server_dir / "logs" / "latest.log"
        relay_log = PROFILES_DIR / profile_id / "logs" / "latest.log"
        log_path = server_log if server_log.is_file() else relay_log

        attempts = 0
        while not server_log.exists() and not relay_log.exists() and attempts < 30:
            if await _interruptible_sleep(1.0):
                return
            attempts += 1
            log_path = server_log if server_log.is_file() else relay_log

        if not log_path.exists():
            try:
                await websocket.send_text("--- Log file not found. Has the server started yet? ---")
                await websocket.close()
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            return

        async with aiofiles.open(log_path, mode="rb") as f:
            file_size = os.path.getsize(log_path)
            initial_offset = max(0, file_size - 64 * 1024)
            await f.seek(initial_offset)
            if initial_offset:
                await f.readline()  # Drop a partial first line from the bounded tail.
            while not disconnect_event.is_set() and not _SHUTDOWN_EVENT.is_set():
                line = await f.readline()
                if line:
                    try:
                        await websocket.send_text(line.decode("utf-8", errors="replace").rstrip())
                    except (WebSocketDisconnect, asyncio.CancelledError):
                        break
                else:
                    try:
                        path_stat = os.stat(log_path)
                        handle_stat = os.fstat(f.fileno())
                        if (path_stat.st_dev, path_stat.st_ino) != (handle_stat.st_dev, handle_stat.st_ino):
                            await f.close()
                            f = await aiofiles.open(log_path, mode="rb")
                            continue
                    except OSError:
                        pass
                    if await _interruptible_sleep(0.5):
                        break
    except (asyncio.CancelledError, WebSocketDisconnect):
        pass  # Server is shutting down or client disconnected; close silently.
    except Exception as e:
        logger.error(f"Console WebSocket error: {e}")
    finally:
        disconnect_event.set()
        ACTIVE_WEBSOCKETS.discard(websocket)
        watch_task.cancel()
        try:
            await watch_task
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
