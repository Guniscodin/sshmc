from enum import Enum
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from uuid import uuid4
from urllib.parse import urlsplit
import re


class ModDownload(BaseModel):
    """A direct HTTPS download for an additional server mod JAR."""
    url: str = Field(..., min_length=8, max_length=2048)
    filename: Optional[str] = Field(default=None, max_length=180)
    sha256: Optional[str] = Field(default=None, min_length=64, max_length=64)
    skip_on_start: bool = False

    @field_validator("url")
    @classmethod
    def validate_download_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Mod download links must be absolute HTTPS URLs.")
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("Mod download links cannot contain whitespace or control characters.")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("SHA-256 must contain exactly 64 hexadecimal characters.")
        return value

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if value in {".", ".."} or "/" in value or "\\" in value or any(ord(char) < 32 for char in value):
            raise ValueError("Mod filenames must be a plain filename.")
        if not value.lower().endswith(".jar"):
            raise ValueError("Mod filenames must end in .jar.")
        return value

class BuildType(str, Enum):
    """Strict enum for supported server types."""
    VANILLA = "vanilla"
    FABRIC = "fabric"
    FORGE = "forge"
    NEOFORGE = "neoforge"

class ProfileStatus(str, Enum):
    """Strict enum for the runtime state of a server."""
    STOPPED = "stopped"
    STARTING = "starting"  # Locks the API during the restart pipeline
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"

class ProfileBase(BaseModel):
    """Base fields shared between creation and reading from DB."""
    name: str = Field(..., min_length=1, max_length=50)
    build: BuildType
    mc_version: str = Field(..., min_length=1) 
    loader_version: str = Field(default="latest") 
    git_url: Optional[str] = Field(default=None, description="Optional URL to mods/configs repo")
    modpack_name: Optional[str] = Field(default=None, description="Optional shared mod pack name")
    description: str = Field(default="", max_length=100, description="Short server description shown on the card and used as the MOTD")
    mod_downloads: list[ModDownload] = Field(default_factory=list, max_length=200)
    backup_worlds_on_change: bool = Field(
        default=True,
        description="Back up world folders before replacing an incompatible server install",
    )
    backup_mods_on_change: bool = False
    backup_config_on_change: bool = False
    backup_datapacks_on_change: bool = False
    backup_server_settings_on_change: bool = False
    backup_on_restart: bool = Field(
        default=False,
        description="Automatically back up the world folders on every server stop and restart",
    )
    max_backups: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Maximum number of backups kept per profile (oldest are pruned automatically)",
    )
    
    # Server settings
    ram_mb: int = Field(default=2048, ge=512, le=32768, description="RAM limit in MB")
    port: int = Field(default=25565, ge=1024, le=65535)
    java_path: str = Field(default="java", description="Path to the Java executable")

    @field_validator("name", "mc_version", "loader_version", "java_path")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This value cannot be blank.")
        if any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("Newlines and null characters are not allowed.")
        return value
    
    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("git_url")
    @classmethod
    def validate_git_url(cls, v: Optional[str]) -> Optional[str]:
        """Basic validation to ensure it looks like a git URL if provided."""
        if v is not None and v != "":
            v = v.strip()
            if not (v.startswith("http://") or v.startswith("https://") or v.startswith("git@")):
                raise ValueError("git_url must be a valid HTTP(S) or SSH git URL")
            return v
        return None

class Profile(ProfileBase):
    """Full profile model as stored in the database."""
    id: str = Field(default_factory=lambda: uuid4().hex)
    rcon_port: int = Field(default=25575, ge=1024, le=65535)
    rcon_password: str = Field(default_factory=lambda: uuid4().hex[:12])
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class RuntimeState(BaseModel):
    """
    Tracks the live state of a profile. 
    Used to reconstruct state if the panel restarts.
    """
    profile_id: str
    status: ProfileStatus = ProfileStatus.STOPPED
    
    # Process tracking
    pid: Optional[int] = None
    pid_create_time: Optional[float] = None 
    
    # Metadata
    started_at: Optional[datetime] = None
    current_commit: Optional[str] = None
