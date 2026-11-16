"""Download configured mod JARs into a profile's mods folder, with local caching."""
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from mcpanel.models import ModDownload, Profile

logger = logging.getLogger(__name__)
MAX_MOD_BYTES = 1024 * 1024 * 1024
MAX_REDIRECTS = 8


class ModDownloadFailure(Exception):
    def __init__(self, index: int, filename: str, message: str):
        super().__init__(message)
        self.index = index
        self.filename = filename


def _filename(download: ModDownload, index: int) -> str:
    name = download.filename
    if not name:
        name = Path(unquote(urlsplit(download.url).path)).name
    if not name:
        name = f"external-mod-{index + 1}.jar"
    if not name.lower().endswith(".jar"):
        raise ModDownloadFailure(index, name, "The link does not identify a .jar file. Set its filename to a .jar name.")
    if name in {".", ".."} or "/" in name or "\\" in name or any(ord(char) < 32 for char in name):
        raise ModDownloadFailure(index, name, "The mod filename is not a safe plain filename.")
    return name


async def _validate_public_https_url(url: str, index: int, filename: str) -> None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only public HTTPS download URLs are allowed.")
        if parsed.port not in (None, 443):
            raise ValueError("Mod download links must use HTTPS port 443.")
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError("Local network download hosts are not allowed.")
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            loop = asyncio.get_running_loop()
            results = await loop.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(result[4][0]) for result in results]
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("Mod downloads must resolve to a public internet address.")
    except Exception as exc:
        raise ModDownloadFailure(index, filename, f"Unsafe or unreachable download URL: {exc}") from exc


async def _download_to_cache(
    url: str,
    destination: Path,
    index: int,
    filename: str,
    expected_sha256: str | None,
) -> None:
    temp_path: Path | None = None
    current_url = url
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        timeout = httpx.Timeout(connect=15, read=60, write=15, pool=15)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                await _validate_public_https_url(current_url, index, filename)
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirect_count == MAX_REDIRECTS:
                            raise ValueError("The download redirected too many times or omitted its redirect target.")
                        current_url = urljoin(str(response.url), location)
                        continue
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_MOD_BYTES:
                        raise ValueError("The mod file is larger than the 1 GiB download limit.")
                    with tempfile.NamedTemporaryFile(
                        dir=destination.parent,
                        prefix=".mod-download-",
                        suffix=".part",
                        delete=False,
                    ) as output:
                        temp_path = Path(output.name)
                        size = 0
                        digest = hashlib.sha256()
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_MOD_BYTES:
                                raise ValueError("The mod file is larger than the 1 GiB download limit.")
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if not zipfile.is_zipfile(temp_path):
                        raise ValueError("The downloaded file is not a valid JAR/ZIP archive.")
                    if expected_sha256 and digest.hexdigest() != expected_sha256:
                        raise ValueError("The downloaded file did not match the supplied SHA-256 checksum.")
                    os.replace(temp_path, destination)
                    return
        raise ValueError("The download did not complete.")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise ModDownloadFailure(index, filename, str(exc) or type(exc).__name__) from exc
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_manifest(path: Path, names: set[str]) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".mod-manifest-", suffix=".tmp", delete=False,
        ) as output:
            temp_path = Path(output.name)
            json.dump(sorted(names), output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


async def install_configured_mods(
    profile: Profile,
    server_dir: Path,
    skip_indexes: set[int] | None = None,
    repo_managed_names: set[str] | None = None,
) -> None:
    """Install cached or freshly downloaded mods sequentially; stop on first failure."""
    downloads = profile.mod_downloads
    skip_indexes = skip_indexes or set()
    repo_managed_names = repo_managed_names or set()
    mods_dir = server_dir / "mods"
    if downloads:
        mods_dir.mkdir(parents=True, exist_ok=True)
    cache_root = server_dir.parent / "downloaded_mods"
    manifest_path = server_dir.parent / "installed_mod_downloads.json"
    planned_names: dict[str, int] = {}
    installed_names: set[str] = set()
    try:
        stored_names = json.loads(manifest_path.read_text(encoding="utf-8"))
        previous_names = {
            name for name in stored_names
            if isinstance(name, str) and name not in {".", ".."}
            and Path(name).name == name and name.lower().endswith(".jar")
        } if isinstance(stored_names, list) else set()
    except (OSError, TypeError, json.JSONDecodeError):
        previous_names = set()

    for index, download in enumerate(downloads):
        filename = _filename(download, index)
        key = hashlib.sha256(download.url.encode("utf-8")).hexdigest()
        cached_file = cache_root / key / filename
        target = mods_dir / filename
        if index in skip_indexes or download.skip_on_start:
            logger.warning("Skipping configured mod download %s for this start, as requested.", filename)
            # Continuing without a failed link also removes its previously
            # installed direct-link JAR, but leaves repo and user-managed files.
            if (
                target.is_file()
                and filename in previous_names
                and filename not in repo_managed_names
                and filename not in installed_names
            ):
                try:
                    target.unlink(missing_ok=True)
                except OSError as exc:
                    raise ModDownloadFailure(index, filename, f"Could not skip the previously installed file: {exc}") from exc
            elif target.is_file() and filename not in repo_managed_names:
                installed_names.add(filename)
                _save_manifest(manifest_path, previous_names | installed_names)
            continue
        if filename in planned_names and planned_names[filename] != index:
            raise ModDownloadFailure(index, filename, "Another configured link uses the same filename.")
        planned_names[filename] = index
        if filename in repo_managed_names:
            raise ModDownloadFailure(index, filename, "This filename is already provided by the connected repository.")

        if cached_file.exists() and (
            not zipfile.is_zipfile(cached_file)
            or (download.sha256 and _sha256(cached_file) != download.sha256)
        ):
            try:
                cached_file.unlink()
            except OSError as exc:
                raise ModDownloadFailure(index, filename, f"Could not replace the cached file: {exc}") from exc
        if not cached_file.exists():
            try:
                logger.info("Downloading configured mod %s (%s/%s).", filename, index + 1, len(downloads))
                await _download_to_cache(download.url, cached_file, index, filename, download.sha256)
            except ModDownloadFailure:
                raise
            except Exception as exc:
                raise ModDownloadFailure(index, filename, f"Could not download mod: {exc}") from exc

        try:
            if target.exists() and _sha256(target) != _sha256(cached_file):
                raise ModDownloadFailure(
                    index,
                    filename,
                    "A different mod file with this name already exists in the server mods folder.",
                )
            if not target.exists():
                await asyncio.to_thread(shutil.copy2, cached_file, target)
            installed_names.add(filename)
        except ModDownloadFailure:
            raise
        except OSError as exc:
            raise ModDownloadFailure(index, filename, f"Could not install the downloaded mod: {exc}") from exc

        logger.info("Installed configured mod %s for profile %s.", filename, profile.name)
        _save_manifest(manifest_path, previous_names | installed_names)

    # Retire only old direct-link JARs after all links were resolved. If a link
    # fails, this point is never reached, so Stop never cleans up mod files.
    retained_names = set(installed_names)
    for stale_name in previous_names - installed_names - repo_managed_names:
        stale_path = mods_dir / stale_name
        try:
            if stale_path.is_file() and stale_path.suffix.lower() == ".jar":
                stale_path.unlink()
                logger.info("Removed no-longer-configured external mod %s.", stale_name)
        except OSError as exc:
            logger.warning("Could not remove stale external mod %s: %s", stale_name, exc)
            retained_names.add(stale_name)
    _save_manifest(manifest_path, retained_names)
