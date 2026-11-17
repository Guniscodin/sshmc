"""Provider interface and Modrinth implementation for mod discovery."""
import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

import httpx


class ModrinthProvider:
    base_url = "https://api.modrinth.com/v2"
    headers = {"User-Agent": "sshmc/0.1 (personal minecraft panel)"}
    _cache: dict[str, tuple[float, Any]] = {}
    _hash_cache: dict[str, tuple[float, dict | None]] = {}
    cache_limit = 256
    hash_cache_limit = 4096
    hash_cache_ttl = 24 * 60 * 60

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def start(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30, headers=self.headers, follow_redirects=True)

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _cache_value(self, key: str, value: Any):
        now = time.monotonic()
        self._cache = {name: item for name, item in self._cache.items() if item[0] > now}
        if len(self._cache) >= self.cache_limit:
            oldest = min(self._cache, key=lambda name: self._cache[name][0])
            self._cache.pop(oldest, None)
        self._cache[key] = (now + 90, value)

    async def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        key = path + "?" + json.dumps(params or {}, sort_keys=True) + json.dumps(body or {}, sort_keys=True)
        if method == "GET":
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        temporary_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30, headers=self.headers, follow_redirects=True)
        try:
            for attempt in range(4):
                response = await client.request(method, self.base_url + path, params=params, json=body)
                if response.status_code == 429:
                    try:
                        retry_after = max(1, int(response.headers.get("Retry-After", "1")))
                    except ValueError:
                        retry_after = 1
                    await asyncio.sleep(min(8, retry_after * (attempt + 1)))
                    continue
                response.raise_for_status()
                payload = response.json()
                if method == "GET":
                    self._cache_value(key, payload)
                return payload
            raise httpx.HTTPStatusError("Modrinth rate limit exceeded.", request=response.request, response=response)
        finally:
            if temporary_client:
                await client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params)

    async def _post(self, path: str, body: dict) -> Any:
        return await self._request("POST", path, body=body)

    async def search(self, query: str, mc_version: str, loader: str, sort: str, category: str, offset: int, server_only: bool = False) -> dict:
        facets = [["project_type:mod"]]
        if mc_version:
            facets.append([f"versions:{mc_version}"])
        if loader and loader != "vanilla":
            facets.append([f"categories:{loader}"])
        if category:
            facets.append([f"categories:{category}"])
        if server_only:
            facets.append(["server_side:required", "server_side:optional"])
        order = {"downloads": "downloads", "updated": "updated", "relevance": "relevance"}.get(sort, "relevance")
        return await self._get("/search", {"query": query, "facets": json.dumps(facets), "index": order, "offset": offset, "limit": 20})

    async def project(self, project_id: str) -> dict:
        return await self._get("/project/" + quote(project_id, safe=""))

    async def versions(self, project_id: str, mc_version: str, loader: str) -> list[dict]:
        params = {"game_versions": json.dumps([mc_version])}
        if loader != "vanilla":
            params["loaders"] = json.dumps([loader])
        return await self._get("/project/" + quote(project_id, safe="") + "/version", params)

    async def version(self, version_id: str) -> dict:
        return await self._get("/version/" + quote(version_id, safe=""))

    async def categories(self) -> list[dict]:
        return await self._get("/tag/category")

    def cached_file_identifications(self, hashes: list[str], algorithm: str = "sha512") -> dict:
        """Return any currently cached identifications for the requested hashes."""
        now = time.monotonic()
        found = {}
        for digest in (value.lower() for value in hashes if isinstance(value, str)):
            cached = self._hash_cache.get(f"{algorithm}:{digest}")
            if cached and cached[0] > now and cached[1] is not None:
                found[digest] = cached[1]
        return found

    async def identify_files(self, hashes: list[str], algorithm: str = "sha512") -> dict:
        """Return the exact Modrinth version owning each file hash, caching per hash."""
        if algorithm not in {"sha1", "sha512"}:
            raise ValueError("Modrinth file hashes must use SHA-1 or SHA-512.")
        normalized = list(dict.fromkeys(value.lower() for value in hashes if isinstance(value, str)))
        if not normalized:
            return {}
        now = time.monotonic()
        self._hash_cache = {key: value for key, value in self._hash_cache.items() if value[0] > now}
        found, missing = {}, []
        for digest in normalized:
            cached = self._hash_cache.get(f"{algorithm}:{digest}")
            if cached:
                if cached[1] is not None:
                    found[digest] = cached[1]
            else:
                missing.append(digest)
        if missing:
            payload = await self._post("/version_files", {"hashes": missing, "algorithm": algorithm})
            if not isinstance(payload, dict):
                raise ValueError("Modrinth returned an invalid file-hash response.")
            payload = {str(key).lower(): value for key, value in payload.items()}
            expires = now + self.hash_cache_ttl
            for digest in missing:
                version = payload.get(digest)
                if isinstance(version, dict):
                    found[digest] = version
                    self._hash_cache[f"{algorithm}:{digest}"] = (expires, version)
                else:
                    self._hash_cache[f"{algorithm}:{digest}"] = (expires, None)
            while len(self._hash_cache) > self.hash_cache_limit:
                self._hash_cache.pop(next(iter(self._hash_cache)))
        return found

    async def projects(self, project_ids: list[str]) -> list[dict]:
        if not project_ids:
            return []
        return await self._get("/projects", {"ids": json.dumps(list(dict.fromkeys(project_ids)))})

    async def latest_versions_from_hashes(self, hashes: list[str], loaders: list[str], game_versions: list[str]) -> dict:
        """For update checks only; Modrinth requires compatibility filters here."""
        if not loaders or not game_versions:
            raise ValueError("Update lookups require loader and Minecraft-version filters.")
        return await self._post("/version_files/update", {
            "hashes": hashes,
            "algorithm": "sha512",
            "loaders": loaders,
            "game_versions": game_versions,
        })


provider = ModrinthProvider()
