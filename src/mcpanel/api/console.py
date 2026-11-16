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
    log_path = PROFILES_DIR / profile_id / "logs" / "latest.log"
    
    attempts = 0
    while not log_path.exists() and attempts < 30:
        await asyncio.sleep(1)
        attempts += 1
    
    if not log_path.exists():
        try:
            await websocket.send_text("--- Log file not found. Has the server started yet? ---")
            await websocket.close()
        except WebSocketDisconnect:
            pass # Client disconnected before we could send
        return

    try:
        async with aiofiles.open(log_path, mode='rb') as f:
            file_size = os.path.getsize(log_path)
            initial_offset = max(0, file_size - 64 * 1024)
            await f.seek(initial_offset)
            if initial_offset:
                await f.readline()  # Drop a partial first line from the bounded tail.
            while True:
                line = await f.readline()
                if line:
                    try:
                        await websocket.send_text(line.decode("utf-8", errors="replace").rstrip())
                    except WebSocketDisconnect:
                        break # Client disconnected, stop sending
                else:
                    try:
                        path_stat = os.stat(log_path)
                        # aiofiles exposes fileno() as a synchronous descriptor lookup.
                        handle_stat = os.fstat(f.fileno())
                        if (path_stat.st_dev, path_stat.st_ino) != (handle_stat.st_dev, handle_stat.st_ino):
                            await f.close()
                            f = await aiofiles.open(log_path, mode="rb")
                            continue
                    except OSError:
                        pass
                    await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Console WebSocket error: {e}")
