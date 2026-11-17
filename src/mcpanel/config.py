import os
from pathlib import Path

# 1. Base Paths
# __file__ is src/mcpanel/config.py. We go up 3 levels to get the project root (sshmc/).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Allow overriding the data directory via environment variables for production
# Defaults to the 'data/' folder we created in the project root.
DATA_DIR = Path(os.getenv("MCPANEL_DATA_DIR", BASE_DIR / "data")).resolve()
PROFILES_DIR = DATA_DIR / "profiles"
UPLOADS_DIR = DATA_DIR / "uploads"
MODPACKS_DIR = DATA_DIR / "modpacks"
DB_PATH = DATA_DIR / "panel.db"
LOGS_DIR = DATA_DIR / "logs"
PID_FILE = DATA_DIR / "sshmc.pid"
_BACKUPS_DIR_OVERRIDE = os.getenv("MCPANEL_BACKUPS_DIR")
BACKUPS_ROOT = Path(_BACKUPS_DIR_OVERRIDE).expanduser().resolve() if _BACKUPS_DIR_OVERRIDE else PROFILES_DIR.resolve()

def _positive_limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default

MAX_MODPACK_UPLOAD_BYTES = _positive_limit("MCPANEL_MAX_UPLOAD_BYTES", 4 * 1024**3)
MAX_MODPACK_EXTRACTED_BYTES = _positive_limit("MCPANEL_MAX_EXTRACTED_BYTES", 8 * 1024**3)
MAX_SINGLE_MOD_UPLOAD_BYTES = _positive_limit("MCPANEL_MAX_SINGLE_MOD_BYTES", 1024**3)
SERVER_LOG_MAX_BYTES = _positive_limit("MCPANEL_SERVER_LOG_MAX_BYTES", 10 * 1024**2)
SERVER_LOG_BACKUP_COUNT = _positive_limit("MCPANEL_SERVER_LOG_BACKUP_COUNT", 5)
SERVER_ROTATED_LOG_KEEP = _positive_limit("MCPANEL_SERVER_ROTATED_LOG_KEEP", 10)
CRASH_REPORT_RETENTION_DAYS = _positive_limit("MCPANEL_CRASH_REPORT_RETENTION_DAYS", 14)
CRASH_REPORT_KEEP = _positive_limit("MCPANEL_CRASH_REPORT_KEEP", 3)
PANEL_LOG_MAX_BYTES = _positive_limit("MCPANEL_PANEL_LOG_MAX_BYTES", 5 * 1024**2)
PANEL_LOG_BACKUP_COUNT = _positive_limit("MCPANEL_PANEL_LOG_BACKUP_COUNT", 3)
BACKUP_WARNING_BYTES = _positive_limit("MCPANEL_BACKUP_WARNING_BYTES", 10 * 1024**3)
BACKUP_MIN_FREE_BYTES = _positive_limit("MCPANEL_BACKUP_MIN_FREE_BYTES", 1024**3)
UPLOAD_SESSION_MAX_AGE_SECONDS = _positive_limit("MCPANEL_UPLOAD_SESSION_MAX_AGE_SECONDS", 24 * 60 * 60)

# 2. Default Settings
DEFAULT_RAM_MB = 2048
DEFAULT_PORT = 25565
DEFAULT_RCON_PORT = 25575

# 3. Ensure directories exist immediately upon import
# This saves us from having to check/create them in our database or file handling code.
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
