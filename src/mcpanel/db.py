import sqlite3
import logging
import json
from contextlib import contextmanager
from typing import List, Optional, Generator

from mcpanel.config import DB_PATH
from mcpanel.models import Profile, RuntimeState, ProfileStatus
from mcpanel.core.ports import find_available_port, is_port_available

# Set up module-level logger
logger = logging.getLogger(__name__)

class DatabaseError(Exception):
    """Custom exception for clean database error handling."""
    pass

@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager for SQLite connections.
    Ensures WAL mode, proper commit/rollback, and closing.
    """
    try:
        # check_same_thread=False is required if FastAPI runs sync DB calls in a threadpool
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON;")
        
        # Performance & Concurrency: WAL mode allows reads while writing
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row  # Allows accessing columns by name
        
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database connection error: {e}")
        if 'conn' in locals():
            conn.rollback()
        raise DatabaseError(f"Database operation failed: {e}") from e
    finally:
        if 'conn' in locals():
            conn.close()

def init_db() -> None:
    """Initializes the database schema. Safe to call multiple times."""
    logger.info(f"Initializing database at {DB_PATH}")
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Profiles Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                build TEXT NOT NULL,
                mc_version TEXT NOT NULL,
                loader_version TEXT NOT NULL,
                git_url TEXT,
                modpack_name TEXT,
                mod_downloads TEXT NOT NULL DEFAULT '[]',
                java_path TEXT NOT NULL,
                ram_mb INTEGER NOT NULL,
                port INTEGER NOT NULL,
                rcon_port INTEGER NOT NULL,
                rcon_password TEXT NOT NULL,
                backup_worlds_on_change INTEGER NOT NULL DEFAULT 1,
                backup_on_restart INTEGER NOT NULL DEFAULT 1,
                exclude_regenerable_backup_folders INTEGER NOT NULL DEFAULT 1,
                backup_mods_on_change INTEGER NOT NULL DEFAULT 0,
                backup_config_on_change INTEGER NOT NULL DEFAULT 0,
                backup_datapacks_on_change INTEGER NOT NULL DEFAULT 0,
                backup_server_settings_on_change INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        
        # Runtime State Table
        # We use profile_id as the primary key (1-to-1 relationship with profile)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runtime_states (
                profile_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                pid INTEGER,
                pid_create_time REAL,
                started_at TEXT,
                current_commit TEXT,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            )
        """)
        profile_columns = {row[1] for row in cursor.execute("PRAGMA table_info(profiles)")}
        if "backup_worlds_on_change" not in profile_columns:
            cursor.execute(
                "ALTER TABLE profiles ADD COLUMN backup_worlds_on_change INTEGER NOT NULL DEFAULT 0"
            )
        if "mod_downloads" not in profile_columns:
            cursor.execute("ALTER TABLE profiles ADD COLUMN mod_downloads TEXT NOT NULL DEFAULT '[]'")
        if "modpack_name" not in profile_columns:
            cursor.execute("ALTER TABLE profiles ADD COLUMN modpack_name TEXT")
        if "backup_on_restart" not in profile_columns:
            cursor.execute("ALTER TABLE profiles ADD COLUMN backup_on_restart INTEGER NOT NULL DEFAULT 1")
        if "exclude_regenerable_backup_folders" not in profile_columns:
            cursor.execute("ALTER TABLE profiles ADD COLUMN exclude_regenerable_backup_folders INTEGER NOT NULL DEFAULT 1")
        for column in (
            "backup_mods_on_change", "backup_config_on_change",
            "backup_datapacks_on_change", "backup_server_settings_on_change",
        ):
            if column not in profile_columns:
                cursor.execute(f"ALTER TABLE profiles ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        cursor.execute("DELETE FROM runtime_states WHERE profile_id NOT IN (SELECT id FROM profiles)")

        # Resolve port collisions for stopped profiles created before automatic
        # port assignment existed. Keep active profiles' configured ports fixed.
        rows = cursor.execute("""
            SELECT p.id, p.port, p.rcon_port, p.created_at, r.status, r.pid, r.pid_create_time
            FROM profiles p JOIN runtime_states r ON r.profile_id = p.id
            ORDER BY p.created_at, p.id
        """).fetchall()
        reserved: set[int] = set()
        inactive = []
        for row in rows:
            active = row["status"] in ("running", "starting", "stopping") or row["pid"] is not None
            if active:
                reserved.update((row["port"], row["rcon_port"]))
            else:
                inactive.append(row)
        for row in inactive:
            game_port, rcon_port = row["port"], row["rcon_port"]
            invalid = (
                game_port == rcon_port or game_port in reserved or rcon_port in reserved
                or not is_port_available(game_port) or not is_port_available(rcon_port)
            )
            if invalid:
                game_port = find_available_port(25565, reserved)
                reserved.add(game_port)
                rcon_port = find_available_port(25575, reserved)
                reserved.add(rcon_port)
                cursor.execute(
                    "UPDATE profiles SET port = ?, rcon_port = ? WHERE id = ?",
                    (game_port, rcon_port, row["id"]),
                )
                logger.warning(
                    "Reassigned conflicting ports for stopped profile %s to game=%s rcon=%s",
                    row["id"], game_port, rcon_port,
                )
            else:
                reserved.update((game_port, rcon_port))
        logger.debug("Database schema initialized successfully.")

# ==========================================
# PROFILE CRUD OPERATIONS
# ==========================================

def _profile_from_row(row: sqlite3.Row) -> Profile:
    data = dict(row)
    try:
        data["mod_downloads"] = json.loads(data.get("mod_downloads") or "[]")
    except (TypeError, json.JSONDecodeError):
        logger.warning("Invalid mod download settings for profile %s; ignoring them.", data.get("id"))
        data["mod_downloads"] = []
    return Profile(**data)

def create_profile(profile: Profile) -> None:
    """Inserts a new profile into the database."""
    logger.info(f"Creating profile '{profile.name}' (ID: {profile.id})")
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        collision = cursor.execute(
            "SELECT id FROM profiles WHERE port IN (?, ?) OR rcon_port IN (?, ?) LIMIT 1",
            (profile.port, profile.rcon_port, profile.port, profile.rcon_port),
        ).fetchone()
        if collision:
            raise DatabaseError("The requested Minecraft or RCON port is already assigned.")
        
        cursor.execute("""
            INSERT INTO profiles (
                id, name, build, mc_version, loader_version, git_url, modpack_name, mod_downloads, java_path, 
                ram_mb, port, rcon_port, rcon_password, backup_worlds_on_change,
                backup_mods_on_change, backup_config_on_change,
                backup_datapacks_on_change, backup_server_settings_on_change, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            profile.id, 
            profile.name, 
            profile.build.value, 
            profile.mc_version,
            profile.loader_version, 
            profile.git_url, 
            profile.modpack_name,
            json.dumps([item.model_dump(mode="json") for item in profile.mod_downloads]),
            profile.java_path, 
            profile.ram_mb,
            profile.port, 
            profile.rcon_port, 
            profile.rcon_password,
            int(profile.backup_worlds_on_change),
            int(profile.backup_mods_on_change),
            int(profile.backup_config_on_change),
            int(profile.backup_datapacks_on_change),
            int(profile.backup_server_settings_on_change),
            profile.created_at.isoformat()
        ))
        
        # Initialize an empty runtime state for this profile
        cursor.execute("""
            INSERT INTO runtime_states (profile_id, status) VALUES (?, ?)
        """, (profile.id, ProfileStatus.STOPPED.value))

def get_profile(profile_id: str) -> Optional[Profile]:
    """Fetches a single profile by ID. Returns None if not found."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            return None
        return _profile_from_row(row)

def list_profiles() -> List[Profile]:
    """Returns a list of all profiles."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
        return [_profile_from_row(row) for row in rows]

def delete_profile(profile_id: str) -> None:
    """Deletes a profile and its runtime state (via CASCADE)."""
    logger.warning(f"Deleting profile ID: {profile_id}")
    with get_db() as conn:
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

def unlink_modpack(pack_name: str) -> None:
    """Clear profile references before removing a shared mod pack."""
    with get_db() as conn:
        conn.execute("UPDATE profiles SET modpack_name = NULL WHERE modpack_name = ?", (pack_name,))

# ==========================================
# RUNTIME STATE CRUD OPERATIONS
# ==========================================

def get_runtime_state(profile_id: str) -> Optional[RuntimeState]:
    """Fetches the current runtime state of a profile."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM runtime_states WHERE profile_id = ?", (profile_id,)).fetchone()
        if not row:
            return None
        return RuntimeState(**dict(row))

def update_runtime_state(state: RuntimeState) -> None:
    """Updates the runtime state (e.g., from stopped to running with a PID)."""
    logger.debug(f"Updating runtime state for {state.profile_id} -> {state.status.value}")
    with get_db() as conn:
        conn.execute("""
            UPDATE runtime_states 
            SET status = ?, pid = ?, pid_create_time = ?, started_at = ?, current_commit = ?
            WHERE profile_id = ?
        """, (
            state.status.value, state.pid, state.pid_create_time,
            state.started_at.isoformat() if state.started_at else None,
            state.current_commit, state.profile_id
        ))

def claim_runtime_state(profile_id: str, expected: list[ProfileStatus], new_status: ProfileStatus) -> bool:
    """Atomically claim a lifecycle transition if the current status is expected."""
    if not expected:
        return False
    placeholders = ",".join("?" for _ in expected)
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE runtime_states SET status = ? WHERE profile_id = ? AND status IN ({placeholders})",
            (new_status.value, profile_id, *(status.value for status in expected)),
        )
        return cursor.rowcount == 1

def update_profile(profile: Profile) -> None:
    """Updates an existing profile in the database."""
    logger.info(f"Updating profile '{profile.name}' (ID: {profile.id})")
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        old = cursor.execute("SELECT port FROM profiles WHERE id = ?", (profile.id,)).fetchone()
        if old and old["port"] != profile.port:
            collision = cursor.execute(
                "SELECT id FROM profiles WHERE id != ? AND (port IN (?, ?) OR rcon_port IN (?, ?)) LIMIT 1",
                (profile.id, profile.port, profile.rcon_port, profile.port, profile.rcon_port),
            ).fetchone()
            if collision:
                raise DatabaseError("The requested Minecraft or RCON port is already assigned.")
        cursor.execute("""
            UPDATE profiles 
            SET name = ?, build = ?, mc_version = ?, loader_version = ?, 
                git_url = ?, modpack_name = ?, mod_downloads = ?, java_path = ?, ram_mb = ?, port = ?,
                backup_worlds_on_change = ?, backup_mods_on_change = ?, backup_config_on_change = ?,
                backup_datapacks_on_change = ?, backup_server_settings_on_change = ?
            WHERE id = ?
        """, (
            profile.name, profile.build.value, profile.mc_version,
            profile.loader_version, profile.git_url,
            profile.modpack_name,
            json.dumps([item.model_dump(mode="json") for item in profile.mod_downloads]),
            profile.java_path,
            profile.ram_mb, profile.port, int(profile.backup_worlds_on_change), int(profile.backup_mods_on_change),
            int(profile.backup_config_on_change), int(profile.backup_datapacks_on_change),
            int(profile.backup_server_settings_on_change), profile.id
        ))
