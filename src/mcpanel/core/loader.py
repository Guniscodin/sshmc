import asyncio
import logging
import os
import re
import httpx
import subprocess
import shlex
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
import zipfile

import aiofiles

from mcpanel.models import BuildType

logger = logging.getLogger(__name__)


class LoaderInstallError(Exception):
    pass


# Generous ceiling for server installers/jars (~300 MB in practice).
MAX_SERVER_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


async def _download_file(url: str, dest: Path) -> None:
    """Download and validate a JAR, replacing the destination only when complete."""
    logger.info(f"DOWNLOADING URL: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > MAX_SERVER_DOWNLOAD_BYTES:
                    raise LoaderInstallError(f"Download from {url} exceeds the {MAX_SERVER_DOWNLOAD_BYTES} byte limit.")
                with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part", delete=False) as f:
                    temp_path = Path(f.name)
                downloaded = 0
                async with aiofiles.open(temp_path, "wb") as f:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        downloaded += len(chunk)
                        if downloaded > MAX_SERVER_DOWNLOAD_BYTES:
                            raise LoaderInstallError(f"Download from {url} exceeds the {MAX_SERVER_DOWNLOAD_BYTES} byte limit.")
                        await f.write(chunk)
        if not temp_path or not await asyncio.to_thread(zipfile.is_zipfile, temp_path):
            raise LoaderInstallError(f"Downloaded file from {url} is not a valid JAR archive.")
        os.replace(temp_path, dest)
    except httpx.HTTPStatusError as e:
        raise LoaderInstallError(f"Failed to download {url} (HTTP {e.response.status_code})") from e
    except httpx.RequestError as e:
        raise LoaderInstallError(f"Network error downloading {url}: {e}") from e
    except OSError as e:
        raise LoaderInstallError(f"Could not save download from {url}: {e}") from e
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


async def install_vanilla(mc_version: str, dest_dir: Path) -> None:
    """Fetches the Mojang manifest and downloads the vanilla server jar."""
    logger.info(f"Installing Vanilla {mc_version}")
    manifest_url = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            resp = await client.get(manifest_url)
            resp.raise_for_status()
            manifest = resp.json()
            version_url = next((v["url"] for v in manifest["versions"] if v["id"] == mc_version), None)
            if not version_url:
                raise LoaderInstallError(f"Minecraft version {mc_version} not found in manifest.")
            version_resp = await client.get(version_url)
            version_resp.raise_for_status()
            version_data = version_resp.json()
            server_jar_url = version_data["downloads"]["server"]["url"]
    except httpx.HTTPStatusError as e:
        raise LoaderInstallError(f"Failed to fetch Mojang manifest for {mc_version} (HTTP {e.response.status_code})") from e
    except httpx.RequestError as e:
        raise LoaderInstallError(f"Network error fetching Mojang manifest for {mc_version}: {e}") from e
    except (KeyError, TypeError, ValueError) as e:
        raise LoaderInstallError(f"Mojang manifest did not provide a server JAR for {mc_version}.") from e
    await _download_file(server_jar_url, dest_dir / "server.jar")


async def install_fabric(mc_version: str, loader_version: str, dest_dir: Path) -> None:
    """Fetches the Fabric server launcher."""
    logger.info(f"Installing Fabric {mc_version} (Loader: {loader_version})")
    # Fabric's server bootstrap endpoint requires an installer version between
    # the loader version and ``server/jar``. ``stable`` is resolved by Meta to
    # the current stable installer, so profiles only need to select a loader.
    url = f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}/{loader_version}/stable/server/jar"
    await _download_file(url, dest_dir / "server.jar")


def _run_forge_installer(installer_path: Path, dest_dir: Path, build: BuildType, java_path: str) -> None:
    """Runs the Forge/NeoForge installer and injects the custom Java path into run.sh."""
    logger.info(f"Running {build.value} installer (this may take a minute)...")
    try:
        process = subprocess.run(
            [java_path, "-jar", installer_path.name, "--installServer"],
            cwd=dest_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )

        logger.debug(f"Installer stdout:\n{process.stdout}")

        if not (dest_dir / "run.sh").exists():
            raise LoaderInstallError("Installer finished but run.sh was not generated.")

        run_sh_path = dest_dir / "run.sh"
        content = run_sh_path.read_text(encoding="utf-8")

        # Rewrite the java invocation line for user-supplied Java paths.
        # A best-effort match; anything unrecognized leaves run.sh untouched
        # with a loud warning instead of a silent no-op.
        if re.search(r"^\s*exec\s+[^\n]*java", content, re.MULTILINE):
            logger.debug("run.sh already execs java; leaving it untouched.")
        else:
            java_match = re.search(r"^(\s*)java(\s[^\n]*@[^\n]*)$", content, re.MULTILINE)
            if java_match:
                indent, rest = java_match.groups()
                content = content[:java_match.start()] + f"{indent}exec {shlex.quote(java_path)}{rest}" + content[java_match.end():]
                run_sh_path.write_text(content, encoding="utf-8")
                logger.info("Configured run.sh to use java at: %s", java_path)
            else:
                logger.warning(
                    "run.sh has no recognized 'java @...' line; leaving the default java in place (custom Java path ignored)."
                )

    except subprocess.TimeoutExpired as e:
        logger.error(f"{build.value} installer timed out after 600 seconds.")
        raise LoaderInstallError(f"{build.value} installer timed out.") from e
    except subprocess.CalledProcessError as e:
        logger.error(f"{build.value} installer failed with exit code {e.returncode}.")
        logger.error(f"Stdout: {e.stdout}")
        logger.error(f"Stderr: {e.stderr}")
        raise LoaderInstallError(f"{build.value} installer failed.") from e
    except OSError as e:
        logger.error(f"Could not launch {build.value} installer using '{java_path}': {e}")
        raise LoaderInstallError(f"Could not run {build.value} installer using '{java_path}': {e}") from e


async def install_forge_family(build: BuildType, mc_version: str, loader_version: str, dest_dir: Path, java_path: str) -> None:
    """Handles Forge and NeoForge installation with exact version numbers."""
    logger.info(f"Installing {build.value} {mc_version} (Loader: {loader_version})")

    if build == BuildType.FORGE:
        version_str = f"{mc_version}-{loader_version}"
        url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version_str}/forge-{version_str}-installer.jar"
    else:
        if mc_version == "1.20.1":
            version_str = f"{mc_version}-{loader_version}"
            url = f"https://maven.neoforged.net/releases/net/neoforged/forge/{version_str}/forge-{version_str}-installer.jar"
        else:
            url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{loader_version}/neoforge-{loader_version}-installer.jar"

    installer_path = dest_dir / "installer.jar"
    await _download_file(url, installer_path)

    try:
        await asyncio.to_thread(_run_forge_installer, installer_path, dest_dir, build, java_path)
    finally:
        installer_path.unlink(missing_ok=True)
        (dest_dir / "installer.jar.log").unlink(missing_ok=True)


async def install_server(build: BuildType, mc_version: str, loader_version: str, dest_dir: Path, java_path: str) -> None:
    """Main entry point to install the correct server jar based on build type."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if build == BuildType.VANILLA:
        await install_vanilla(mc_version, dest_dir)
    elif build == BuildType.FABRIC:
        await install_fabric(mc_version, loader_version, dest_dir)
    elif build in (BuildType.FORGE, BuildType.NEOFORGE):
        await install_forge_family(build, mc_version, loader_version, dest_dir, java_path)
    else:
        raise LoaderInstallError(f"Unsupported build type: {build}")
    logger.info(f"Successfully installed {build.value} server in {dest_dir}")


async def get_loader_versions(build: BuildType, mc_version: str) -> list[str]:
    """Fetches available loader versions for the frontend dropdown."""
    if build == BuildType.VANILLA:
        return ["latest"]

    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=15.0)) as client:
        try:
            if build == BuildType.FABRIC:
                url = f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}"
                resp = await client.get(url)
                resp.raise_for_status()
                if resp.status_code == 200:
                    data = resp.json()
                    # FIX: Return ALL versions, not just 'stable' ones
                    all_versions = [v["loader"]["version"] for v in data]
                    logger.debug("Fetched %s Fabric versions for %s", len(all_versions), mc_version)
                    return all_versions

            elif build == BuildType.NEOFORGE:
                if mc_version == "1.20.1":
                    url = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/forge"
                    prefix = f"{mc_version}-"
                else:
                    parts = mc_version.split('.')
                    if len(parts) < 2:
                        raise LoaderInstallError(f"Invalid Minecraft version for NeoForge lookup: {mc_version}")
                    minor = parts[1]
                    patch = parts[2] if len(parts) > 2 else "0"
                    prefix = f"{minor}.{patch}."
                    url = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"

                resp = await client.get(url)
                resp.raise_for_status()
                if resp.status_code == 200:
                    data = resp.json()
                    if mc_version == "1.20.1":
                        return [v.replace(prefix, "") for v in data["versions"] if v.startswith(prefix)][::-1]
                    return [v for v in data["versions"] if v.startswith(prefix)][::-1]

            elif build == BuildType.FORGE:
                url = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
                resp = await client.get(url)
                resp.raise_for_status()
                if resp.status_code == 200:
                    root = ET.fromstring(resp.text)
                    versions = []
                    for version in root.findall(".//version"):
                        if version.text and version.text.startswith(f"{mc_version}-"):
                            versions.append(version.text.replace(f"{mc_version}-", ""))
                    return versions[::-1]
        except Exception as e:
            logger.error(f"Failed to fetch loader versions: {e}")
            raise LoaderInstallError(f"Could not fetch {build.value} loader versions for {mc_version}: {e}") from e

    return []