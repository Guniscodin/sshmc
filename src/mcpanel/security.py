"""Local password hashing and signed, HTTP-only panel sessions."""
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from mcpanel.config import DATA_DIR

SESSION_COOKIE = "mcpanel_session"
SESSION_MAX_AGE = 12 * 60 * 60
AUTH_FILE = DATA_DIR / "panel_auth.json"
PASSWORD_HASH_BYTES = 32
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024
MIN_PASSWORD_LENGTH = 12


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=PASSWORD_HASH_BYTES, maxmem=SCRYPT_MAXMEM,
    )


def _read_auth() -> dict | None:
    if not AUTH_FILE.exists():
        return None
    try:
        with AUTH_FILE.open("r", encoding="utf-8") as auth_file:
            data = json.load(auth_file)
        if not isinstance(data, dict):
            raise ValueError("authentication record must be a JSON object")
        if (
            data.get("version") != 1
            or data.get("kdf") != "scrypt"
            or not isinstance(data.get("n"), int)
            or not isinstance(data.get("r"), int)
            or not isinstance(data.get("p"), int)
            or data["n"] < 2**14 or data["n"] > 2**20
            or data["n"] & (data["n"] - 1)
            or data["r"] < 1 or data["r"] > 32
            or data["p"] < 1 or data["p"] > 16
            or len(bytes.fromhex(data["salt"])) < 16
            or len(bytes.fromhex(data["password_hash"])) != PASSWORD_HASH_BYTES
            or len(bytes.fromhex(data["session_key"])) != 32
        ):
            raise ValueError("unsupported or incomplete authentication record")
        os.chmod(AUTH_FILE, 0o600)
        return data
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read local panel authentication file {AUTH_FILE}: {exc}") from exc


def credentials_configured() -> bool:
    """Whether a local panel password record exists."""
    return AUTH_FILE.is_file()


def _save_auth(password: str) -> None:
    salt = secrets.token_bytes(16)
    record = {
        "version": 1,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": salt.hex(),
        "password_hash": _derive(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).hex(),
        "session_key": secrets.token_bytes(32).hex(),
    }
    fd, temp_name = tempfile.mkstemp(prefix=".panel_auth.", dir=DATA_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(record, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, AUTH_FILE)
        os.chmod(AUTH_FILE, 0o600)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def unlock_panel() -> None:
    """Set a password on first run, then verify it before the server starts."""
    auth = _read_auth()
    if not sys.stdin.isatty():
        if auth is None:
            raise RuntimeError(
                "No panel password is set. Run SSHMC once from an interactive terminal "
                "to create the password, then start it again."
            )
        raise RuntimeError("SSHMC needs an interactive terminal to unlock the panel.")

    if auth is None:
        print("First run: set a local SSHMC panel password (12 characters minimum).")
        password = getpass.getpass("New panel password: ")
        confirmation = getpass.getpass("Confirm panel password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise RuntimeError(f"Panel password must be at least {MIN_PASSWORD_LENGTH} characters.")
        if len(password) > 1024:
            raise RuntimeError("Panel password must be no longer than 1024 characters.")
        if not hmac.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
            raise RuntimeError("Panel passwords did not match.")
        _save_auth(password)
        print("Panel password saved locally.")
        return

    password = getpass.getpass("SSHMC panel password: ")
    if not valid_login(password):
        raise RuntimeError("Incorrect panel password; SSHMC startup cancelled.")


def valid_login(password: str) -> bool:
    if len(password) > 1024:
        return False
    auth = _read_auth()
    if auth is None:
        return False
    try:
        expected = bytes.fromhex(auth["password_hash"])
        actual = _derive(password, bytes.fromhex(auth["salt"]), n=auth["n"], r=auth["r"], p=auth["p"])
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _signature(expiry: int) -> str:
    auth = _read_auth()
    if auth is None:
        return ""
    key = bytes.fromhex(auth["session_key"])
    message = f"sshmc-session:{expiry}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def create_session_token() -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE
    return f"{expiry}.{_signature(expiry)}"


def valid_session_token(token: str | None) -> bool:
    if not token or not credentials_configured():
        return False
    try:
        expiry_text, signature = token.split(".", 1)
        expiry = int(expiry_text)
    except (ValueError, TypeError):
        return False
    return time.time() < expiry <= time.time() + SESSION_MAX_AGE + 60 and hmac.compare_digest(signature, _signature(expiry))


def same_origin(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host.lower()
    except ValueError:
        return False


class PanelAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/login", "/auth/login"} or request.url.path.startswith("/static/"):
            return await call_next(request)
        if not credentials_configured():
            return JSONResponse(
                {"detail": "Panel password is not configured. Start SSHMC from an interactive terminal to set one."},
                status_code=503,
            )
        if not valid_session_token(request.cookies.get(SESSION_COOKIE)):
            if "text/html" in request.headers.get("accept", "") and request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and not same_origin(origin, request.headers.get("host")):
                return JSONResponse({"detail": "Cross-origin request denied."}, status_code=403)
        return await call_next(request)
