"""Tests for per-world chunk and stats collection on the monitoring tab."""

from pathlib import Path
import struct
from unittest.mock import patch

from mcpanel.core import monitoring, storage


def _write_region_file(path: Path, chunk_offsets: dict[int, int]) -> None:
    """Write a synthetic .mca region file.

    The first 4096 bytes are the chunk offset table: 1024 big-endian uint32
    entries. Non-zero entries mark generated chunks.
    """
    header = bytearray(4096)
    for offset_index, value in chunk_offsets.items():
        struct.pack_into(">I", header, offset_index * 4, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header))


def test_count_chunks_in_region_file(tmp_path: Path) -> None:
    region = tmp_path / "region" / "r.0.0.mca"
    _write_region_file(region, {0: 1234, 5: 5678, 999: 91011})
    assert storage._count_chunks_in_region_file(region) == 3

    # Empty region file (all zero offsets) -> 0 chunks
    empty = tmp_path / "region" / "r.0.1.mca"
    _write_region_file(empty, {})
    assert storage._count_chunks_in_region_file(empty) == 0

    # Truncated file (< 4096 bytes) -> 0 chunks
    truncated = tmp_path / "region" / "r.0.2.mca"
    truncated.parent.mkdir(parents=True, exist_ok=True)
    truncated.write_bytes(b"\x00" * 100)
    assert storage._count_chunks_in_region_file(truncated) == 0

    # Missing file -> 0 chunks
    assert storage._count_chunks_in_region_file(tmp_path / "region" / "r.0.3.mca") == 0


def test_world_folder_stats_counts_chunks_and_bytes(tmp_path: Path) -> None:
    server_dir = tmp_path / "server"

    # Overworld with 2 region files, 5 total chunks, plus a level.dat
    world = server_dir / "world" / "region"
    _write_region_file(world / "r.0.0.mca", {0: 1, 1: 2})
    _write_region_file(world / "r.0.1.mca", {3: 4, 4: 5, 10: 11})
    (server_dir / "world").mkdir(parents=True, exist_ok=True)
    (server_dir / "world" / "level.dat").write_bytes(b"\x00" * 1024)

    # Nether with 1 region file, 1 chunk
    nether = server_dir / "world_nether" / "region"
    _write_region_file(nether / "r.-1.0.mca", {7: 99})

    # The End does not exist
    stats = storage._world_folder_stats(server_dir)

    overworld = stats["world"]
    assert overworld["chunks"] == 5
    assert overworld["region_files"] == 2
    assert overworld["bytes"] >= 4096 * 2 + 1024
    assert overworld["last_modified"] is not None

    nether_stats = stats["world_nether"]
    assert nether_stats["chunks"] == 1
    assert nether_stats["region_files"] == 1

    end_stats = stats["world_the_end"]
    assert end_stats["chunks"] == 0
    assert end_stats["region_files"] == 0
    assert end_stats["bytes"] == 0
    assert end_stats["last_modified"] is None


def test_world_folder_stats_missing_server_dir(tmp_path: Path) -> None:
    stats = storage._world_folder_stats(tmp_path / "no_such_server")
    for world_name in storage.WORLD_FOLDERS:
        assert stats[world_name]["chunks"] == 0
        assert stats[world_name]["bytes"] == 0
        assert stats[world_name]["last_modified"] is None


def test_monitoring_get_stats_merges_world_data() -> None:
    sample = {
        "timestamp": "2026-11-21T10:00:00Z",
        "host": {"cpu_percent": 10.0},
        "servers": [
            {
                "profile_id": "abc123",
                "name": "Survival",
                "ram_allocation_mb": 2048,
                "rss_bytes": 512 * 1024**2,
                "cpu_percent_machine": 25.0,
                "cpu_percent_one_core": 100.0,
                "available": True,
            }
        ],
    }
    profile_sizes = [
        {
            "profile_id": "abc123",
            "server_bytes": 1000,
            "world_bytes": 500,
            "world_folders": {
                "world": {"bytes": 400, "chunks": 42, "region_files": 1, "last_modified": "2026-11-20T12:00:00Z"},
                "world_nether": {"bytes": 100, "chunks": 5, "region_files": 1, "last_modified": None},
                "world_the_end": {"bytes": 0, "chunks": 0, "region_files": 0, "last_modified": None},
            },
            "backups_bytes": 0,
        },
    ]

    with patch.object(monitoring, "_latest", sample), patch.object(
        storage, "profile_sizes_snapshot", return_value=(profile_sizes, "2026-11-21T10:00:00Z")
    ):
        stats = monitoring.get_stats()

    assert len(stats["servers"]) == 1
    server = stats["servers"][0]
    assert server["profile_id"] == "abc123"
    worlds = server["worlds"]
    assert worlds["world"]["chunks"] == 42
    assert worlds["world"]["region_files"] == 1
    assert worlds["world"]["last_modified"] == "2026-11-20T12:00:00Z"
    assert worlds["world_nether"]["chunks"] == 5
    assert worlds["world_the_end"]["chunks"] == 0


def test_monitoring_get_stats_missing_worlds_defaults_empty() -> None:
    sample = {"timestamp": "2026-11-21T10:00:00Z", "host": None, "servers": []}
    with patch.object(monitoring, "_latest", sample), patch.object(
        storage, "profile_sizes_snapshot", return_value=([], None)
    ):
        stats = monitoring.get_stats()
    assert stats["servers"] == []
