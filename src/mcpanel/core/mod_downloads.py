"""Resolve configured mod links into a profile-owned cache for staged startup."""
import asyncio
import hashlib
import ipaddress
import logging
import os
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
    if name in {".", ".."} or "/" in name or "\\" in name or ":" in name or any(ord(char) < 32 for char in name):
        raise ModDownloadFailure(index, name, "The mod filename is not a safe plain filename.")
    return name


def source_key(download: ModDownload, index: int = 0) -> str:
    filename = _filename(download, index)
    value = f"{download.url}\0{filename}\0{(download.sha256 or '').lower()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def legacy_cache_key(download: ModDownload) -> str:
    return hashlib.sha256(download.url.encode("utf-8")).hexdigest()


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


async def install_configured_mods(
    profile: Profile,
    cache_root: Path,
    skip_indexes: set[int] | None = None,
) -> list[dict]:
    """Resolve configured links into cached source candidates without touching the live server."""
    downloads = profile.mod_downloads
    skip_indexes = skip_indexes or set()
    candidates = []
    if cache_root.is_symlink():
        raise ModDownloadFailure(0, "download cache", "The direct-link cache cannot be a symbolic link.")
    cache_root.mkdir(parents=True, exist_ok=True)

    for index, download in enumerate(downloads):
        filename = _filename(download, index)
        if index in skip_indexes or download.skip_on_start:
            logger.warning("Skipping configured mod download %s for this start, as requested.", filename)
            continue
        key = source_key(download, index)
        cache_folder = cache_root / key
        if cache_folder.is_symlink():
            raise ModDownloadFailure(index, filename, "The cached mod path cannot be a symbolic link.")
        cached_file = cache_folder / filename
        if cached_file.is_symlink():
            raise ModDownloadFailure(index, filename, "The cached mod file cannot be a symbolic link.")
        legacy_folder = cache_root / legacy_cache_key(download)
        legacy_file = legacy_folder / filename
        if key != legacy_folder.name and not cached_file.exists() and not legacy_folder.is_symlink() and legacy_file.is_file() and not legacy_file.is_symlink():
            valid_legacy = zipfile.is_zipfile(legacy_file) and (not download.sha256 or _sha256(legacy_file) == download.sha256.lower())
            if valid_legacy:
                cache_folder.mkdir(parents=True, exist_ok=True)
                os.replace(legacy_file, cached_file)
                try:
                    legacy_folder.rmdir()
                except OSError:
                    pass
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

        candidates.append({
            "filename": filename,
            "owner": "link",
            "path": cached_file,
            "source_key": key,
        })
        logger.info("Resolved configured mod link %s for profile %s.", filename, profile.name)
    return candidates
