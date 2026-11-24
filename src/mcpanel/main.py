import logging
import asyncio
import os
import signal
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from mcpanel import db
from mcpanel.api import profiles, lifecycle, console, modpacks as modpack_api, monitoring as monitoring_api, mods as mods_api, storage as storage_api, players as players_api, launch_dirs as launch_dirs_api
from mcpanel.models import ProfileStatus
from mcpanel.core import process, runtime, modpacks, monitoring, backups, storage
from mcpanel.config import (
    DATA_DIR,
    LOGS_DIR,
    PID_FILE,
    PANEL_LOG_BACKUP_COUNT,
    PANEL_LOG_MAX_BYTES,
)
from mcpanel.core.panel_logging import PollingAccessFilter, SafePanelRotatingHandler
from mcpanel.core.mod_provider import provider as mod_provider
from mcpanel.security import (
    PanelAuthMiddleware, SESSION_COOKIE, SESSION_MAX_AGE,
    create_session_token, credentials_configured, same_origin, unlock_panel, valid_login,
)

# Keep a terminal log and a bounded file log under the configured data directory.
LOGS_DIR.mkdir(parents=True, exist_ok=True)
panel_log = LOGS_DIR / "panel.log"
panel_log.resolve().relative_to(DATA_DIR.resolve())
panel_file_handler = SafePanelRotatingHandler(
    panel_log, DATA_DIR, PANEL_LOG_MAX_BYTES, PANEL_LOG_BACKUP_COUNT
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), panel_file_handler],
    force=True,
)
logging.getLogger("uvicorn.access").addFilter(PollingAccessFilter())
logger = logging.getLogger(__name__)

# Setup templates
BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = BASE_DIR / "web" / "static"
_static_version_cache: dict[str, tuple[float, str]] = {}


def static_url(path: str) -> str:
    """Generate a versioned URL for static assets based on mtime/sha256."""
    clean_path = path.lstrip("/")
    if clean_path.startswith("static/"):
        rel_path = clean_path[len("static/"):]
    else:
        rel_path = clean_path
    target_file = _STATIC_DIR / rel_path
    try:
        mtime = target_file.stat().st_mtime
        cached_mtime, cached_hash = _static_version_cache.get(rel_path, (0, ""))
        if cached_mtime == mtime and cached_hash:
            v = cached_hash
        else:
            import hashlib
            h = hashlib.sha256(target_file.read_bytes()).hexdigest()[:10]
            _static_version_cache[rel_path] = (mtime, h)
            v = h
        return f"/static/{rel_path}?v={v}"
    except OSError:
        return f"/static/{rel_path}"


templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
templates.env.globals["static_url"] = static_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs before the app starts."""
    logger.info("Starting SSHMC Panel...")
    console.reset_shutdown_state()
    unlock_panel()
    db.init_db()
    modpacks.cleanup_temporary_data()
    for _, state in db.list_profiles_with_states():
        if not state:
            continue
        alive = bool(
            state.pid and state.pid_create_time
            and process.is_server_alive(state.pid, state.pid_create_time)
        )
        if alive:
            state.status = ProfileStatus.RUNNING
        elif state.pid is not None or state.status in (
            ProfileStatus.RUNNING, ProfileStatus.STARTING, ProfileStatus.STOPPING
        ):
            state.status = ProfileStatus.STOPPED
            state.pid = None
            state.pid_create_time = None
        else:
            continue
        db.update_runtime_state(state)

    # Port repair runs after dead processes are reconciled to stopped, because
    # it only ever reassigns stopped profiles. Sockets are probed with no
    # database lock held, and any reassignment is surfaced in the panel.
    repaired = db.repair_profile_ports()
    for item in repaired:
        try:
            profile = db.get_profile(item["id"])
            if profile:
                if item["old_rcon_port"] == item["rcon_port"]:
                    rcon_note = f"RCON kept at {item['rcon_port']}"
                else:
                    rcon_note = f"RCON {item['old_rcon_port']} → {item['rcon_port']}"
                runtime.record_startup_warning(
                    profile,
                    f"Ports were reassigned after a conflict: game {item['old_port']} → {item['port']}, {rcon_note}.",
                )
        except Exception:
            logger.exception("Could not record port-repair warning for %s", item["id"])

    await monitoring.start_monitoring()
    await storage.start_storage_monitoring()
    await mod_provider.start()
    monitor = asyncio.create_task(_monitor_servers())
    upload_cleanup = asyncio.create_task(modpacks.upload_session_cleanup_loop())
    try:
        yield
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        try:
            await console.close_all_consoles()
            await monitoring.stop_monitoring()
            await storage.stop_storage_monitoring()
            await mod_provider.close()
            await runtime.shutdown_starting_servers()
            monitor.cancel()
            upload_cleanup.cancel()
            try:
                await asyncio.gather(monitor, upload_cleanup, return_exceptions=True)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            logger.info("Shutting down SSHMC Panel...")
            _remove_cli_pid_file()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass


def _remove_cli_pid_file() -> None:
    try:
        if not PID_FILE.is_symlink() and PID_FILE.read_text(encoding="ascii").strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
    except (OSError, UnicodeDecodeError):
        logger.warning("Could not clean the SSHMC CLI PID file at %s", PID_FILE)


async def _signal_panel_shutdown() -> None:
    await console.close_all_consoles()
    await asyncio.sleep(0.25)
    os.kill(os.getpid(), signal.SIGTERM)


def _reconcile_servers_once() -> None:
    for _, state in db.list_profiles_with_states():
        if not state:
            continue
        if state.status == ProfileStatus.CRASHED and state.pid and state.pid_create_time:
            if process.is_server_alive(state.pid, state.pid_create_time):
                state.status = ProfileStatus.RUNNING
            else:
                state.pid = None
                state.pid_create_time = None
            db.update_runtime_state(state)
            continue
        if state.status != ProfileStatus.RUNNING:
            continue
        if not state.pid or not state.pid_create_time or not process.is_server_alive(state.pid, state.pid_create_time):
            state.status = ProfileStatus.CRASHED
            state.pid = None
            state.pid_create_time = None
            db.update_runtime_state(state)


async def _monitor_servers() -> None:
    """Keep persisted RUNNING states aligned with the tracked process."""
    while True:
        try:
            await asyncio.to_thread(_reconcile_servers_once)
        except Exception:
            logger.exception("Error while reconciling server runtime states")
        await asyncio.sleep(2)


app = FastAPI(title="SSHMC Panel", lifespan=lifespan)
app.add_middleware(PanelAuthMiddleware)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not credentials_configured():
        return HTMLResponse(
            "<h1>SSHMC password is not configured</h1><p>Stop the panel and start it from an interactive terminal to set a local password.</p>",
            status_code=503,
        )
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={},
    )


@app.post("/auth/login")
async def login(request: Request, password: str = Form(...)):
    if not credentials_configured():
        return HTMLResponse("Panel password is not configured.", status_code=503)
    origin = request.headers.get("origin")
    if origin and not same_origin(origin, request.headers.get("host")):
        return HTMLResponse("Cross-origin login denied.", status_code=403)
    if not valid_login(password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Incorrect password. Try again."},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https" or forwarded_proto == "https",
        samesite="strict",
        path="/",
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Unauthenticated health check for load balancers and monitoring."""
    return {"status": "ok"}


@app.post("/api/panel/shutdown")
async def shutdown_panel(background_tasks: BackgroundTasks):
    """Shut down only the panel process; detached Minecraft servers keep running."""
    background_tasks.add_task(_signal_panel_shutdown)
    return {"status": "shutting_down", "minecraft_servers": "left_running"}


@app.get("/api/heartbeat")
async def heartbeat():
    """Lightweight single heartbeat for panel runtime state, startup events, and counts."""
    profile_states = db.list_profiles_with_states()
    statuses: dict[str, str] = {}
    running = 0
    for p, st in profile_states:
        val = st.status.value if st else "stopped"
        statuses[p.id] = val
        if val == "running":
            running += 1
    events = runtime.drain_startup_events()
    return {
        "statuses": statuses,
        "running_count": running,
        "total_count": len(profile_states),
        "events": events,
    }


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Mount static files (CSS/JS) with revalidating headers
app.mount("/static", RevalidatingStaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.middleware("http")
async def add_no_cache_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Include API Routers
app.include_router(profiles.router)
app.include_router(lifecycle.router)
app.include_router(console.router)
app.include_router(modpack_api.router)
app.include_router(launch_dirs_api.router)
app.include_router(monitoring_api.router)
app.include_router(mods_api.router)
app.include_router(storage_api.router)
app.include_router(players_api.router)


# src/mcpanel/main.py (at the bottom)

@app.get("/")
async def serve_dashboard(request: Request):
    """Serves the main HTMX dashboard."""
    all_profiles = db.list_profiles_with_states()
    profile_data = []
    for p, state in all_profiles:
        profile_data.append({
            "profile": p, "state": state, "backups": backups.list_backups(p.id),
            "backup_state": backups.backup_state(p.id), "has_icon": profiles._profile_has_icon(p.id),
        })

    # FIX: Pass request as the first argument (or explicitly as a kwarg)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"data": profile_data}
    )