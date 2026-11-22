import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpanel.api.launch_dirs import router as launch_dirs_router
from mcpanel.api.profiles import router as profiles_router
from mcpanel.core import backups, launch_dirs, mod_storage, workspace
from mcpanel.models import BuildType, Profile, ProfileStatus, RuntimeState
from mcpanel import db


def _create_minimal_jar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fabric.mod.json", '{"id": "testmod", "version": "1.0.0"}')


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    profiles_dir = data_dir / "profiles"
    uploads_dir = data_dir / "uploads"
    launch_dir_root = data_dir / "launch_dirs"

    for directory in (data_dir, profiles_dir, uploads_dir, launch_dir_root):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("mcpanel.config.DATA_DIR", data_dir)
    monkeypatch.setattr("mcpanel.config.PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("mcpanel.config.UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr("mcpanel.config.LAUNCH_DIRS_DIR", launch_dir_root)
    monkeypatch.setattr("mcpanel.core.storage.DATA_DIR", data_dir)
    monkeypatch.setattr("mcpanel.core.storage.PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("mcpanel.core.launch_dirs.DATA_DIR", data_dir)
    monkeypatch.setattr("mcpanel.core.launch_dirs.LAUNCH_DIRS_DIR", launch_dir_root)
    monkeypatch.setattr("mcpanel.core.launch_dirs.UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr("mcpanel.core.backups.PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("mcpanel.core.backups.LAUNCH_DIRS_DIR", launch_dir_root)
    monkeypatch.setattr("mcpanel.core.mod_storage.PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("mcpanel.api.profiles.PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("mcpanel.api.launch_dirs.UPLOADS_DIR", uploads_dir)

    return {
        "data_dir": data_dir,
        "profiles_dir": profiles_dir,
        "uploads_dir": uploads_dir,
        "launch_dir_root": launch_dir_root,
    }


def test_sanitize_name():
    assert launch_dirs.sanitize_name("Better MC 1.21") == "Better-MC-1-21"
    assert launch_dirs.sanitize_name("___pack---name___") == "pack---name"
    assert launch_dirs.sanitize_name("My_Pack-v1.0") == "My_Pack-v1-0"

    with pytest.raises(launch_dirs.LaunchDirError):
        launch_dirs.sanitize_name("   ")
    with pytest.raises(launch_dirs.LaunchDirError):
        launch_dirs.sanitize_name("???***///")


def test_is_compatible():
    manifest = {
        "name": "test-pack",
        "mc_version": "1.21.1",
        "build": "fabric",
    }
    assert launch_dirs.is_compatible(manifest, "fabric", "1.21.1")
    assert launch_dirs.is_compatible(manifest, BuildType.FABRIC, "1.21.1")
    assert launch_dirs.is_compatible(manifest, "Fabric", "1.21.1")

    profile = Profile(
        name="Test Server",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )
    assert launch_dirs.is_compatible(manifest, profile)

    # Incompatible loader or version
    assert not launch_dirs.is_compatible(manifest, "forge", "1.21.1")
    assert not launch_dirs.is_compatible(manifest, "fabric", "1.20.4")
    assert not launch_dirs.is_compatible(None, profile)


def test_import_session_with_wrapper_and_exclusions(isolated_storage):
    uploads_dir = isolated_storage["uploads_dir"]
    session_id = "0123456789abcdef0123456789abcdef"
    session_files = uploads_dir / session_id / "files"
    session_files.mkdir(parents=True)

    # Staged pack files under wrapper "CurseForgePack"
    wrapper = session_files / "CurseForgePack"
    _create_minimal_jar(wrapper / "mods" / "jei-fabric.jar")
    (wrapper / "config" / "jei.json").parent.mkdir(parents=True, exist_ok=True)
    (wrapper / "config" / "jei.json").write_text('{"showCheats": false}', encoding="utf-8")
    (wrapper / "datapacks" / "advancements.zip").parent.mkdir(parents=True, exist_ok=True)
    (wrapper / "datapacks" / "advancements.zip").write_text("dummy-datapack", encoding="utf-8")
    (wrapper / "kubejs" / "startup.js").parent.mkdir(parents=True, exist_ok=True)
    (wrapper / "kubejs" / "startup.js").write_text("console.log('startup');", encoding="utf-8")

    # Excluded files and directories
    (wrapper / "world" / "level.dat").parent.mkdir(parents=True, exist_ok=True)
    (wrapper / "world" / "level.dat").write_text("world-data", encoding="utf-8")
    (wrapper / "logs" / "latest.log").parent.mkdir(parents=True, exist_ok=True)
    (wrapper / "logs" / "latest.log").write_text("log-data", encoding="utf-8")
    (wrapper / "server.properties").write_text("motd=old", encoding="utf-8")
    (wrapper / "server.jar").write_text("fake-jar", encoding="utf-8")
    (wrapper / "ops.json").write_text("[]", encoding="utf-8")

    manifest = launch_dirs.import_session(
        session_id=session_id,
        raw_name="CurseForgePack",
        mc_version="1.21.1",
        build="fabric",
        label="CurseForge Fabric Pack",
    )

    assert manifest["name"] == "CurseForgePack"
    assert manifest["label"] == "CurseForge Fabric Pack"
    assert manifest["mc_version"] == "1.21.1"
    assert manifest["build"] == "fabric"
    assert "mods" in manifest["folders"]
    assert "config" in manifest["folders"]
    assert "datapacks" in manifest["folders"]
    assert "kubejs" in manifest["folders"]

    imported_dir = isolated_storage["launch_dir_root"] / "CurseForgePack"
    assert imported_dir.is_dir()
    assert (imported_dir / "mods" / "jei-fabric.jar").is_file()
    assert (imported_dir / "config" / "jei.json").is_file()
    assert (imported_dir / "datapacks" / "advancements.zip").is_file()
    assert (imported_dir / "kubejs" / "startup.js").is_file()

    # Verify excluded contents were NOT copied
    assert not (imported_dir / "world").exists()
    assert not (imported_dir / "logs").exists()
    assert not (imported_dir / "server.properties").exists()
    assert not (imported_dir / "server.jar").exists()
    assert not (imported_dir / "ops.json").exists()

    # Re-importing with same name without replace raises LaunchDirExists
    with pytest.raises(launch_dirs.LaunchDirExistsError):
        launch_dirs.import_session(
            session_id=session_id,
            raw_name="CurseForgePack",
            mc_version="1.21.1",
            build="fabric",
            replace=False,
        )

    # Re-importing with replace=True succeeds
    manifest2 = launch_dirs.import_session(
        session_id=session_id,
        raw_name="CurseForgePack",
        mc_version="1.21.1",
        build="fabric",
        replace=True,
    )
    assert manifest2["name"] == "CurseForgePack"


def test_launch_dirs_api_endpoints(isolated_storage, monkeypatch):
    app = FastAPI()
    app.include_router(launch_dirs_router)
    client = TestClient(app)

    # Initially empty list
    resp = client.get("/api/launch-dirs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["directories"] == []

    # Stage an upload
    uploads_dir = isolated_storage["uploads_dir"]
    session_id = "aabbccddeeff00112233445566778899"
    session_files = uploads_dir / session_id / "files"
    _create_minimal_jar(session_files / "mods" / "mod.jar")
    (session_files / "config" / "test.json").parent.mkdir(parents=True, exist_ok=True)
    (session_files / "config" / "test.json").write_text("{}", encoding="utf-8")

    # Import via API
    resp = client.post(
        "/api/launch-dirs/import",
        json={
            "session_id": session_id,
            "name": "My-Directory",
            "label": "My Cool Directory",
            "mc_version": "1.21.1",
            "build": "fabric",
            "replace": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["directory"]["name"] == "My-Directory"
    assert resp.json()["directory"]["label"] == "My Cool Directory"

    # List now shows the imported directory
    resp = client.get("/api/launch-dirs")
    assert resp.status_code == 200
    assert len(resp.json()["directories"]) == 1
    assert resp.json()["directories"][0]["name"] == "My-Directory"

    # Deleting the directory
    resp = client.delete("/api/launch-dirs/My-Directory")
    assert resp.status_code == 204

    # List is empty again
    resp = client.get("/api/launch-dirs")
    assert len(resp.json()["directories"]) == 0

    # Deleting nonexistent returns 404
    resp = client.delete("/api/launch-dirs/nonexistent")
    assert resp.status_code == 404


def test_profile_launch_dir_assignment_and_reset(isolated_storage, monkeypatch):
    # Setup database with profiles
    profiles_dir = isolated_storage["profiles_dir"]
    profile_id = "112233445566778899aabbccddeeff00"
    profile_dir = profiles_dir / profile_id
    server_dir = profile_dir / "server"
    server_dir.mkdir(parents=True)
    (server_dir / "server.properties").write_text("level-name=world\n", encoding="utf-8")

    # Real world in profile server directory
    world_dir = server_dir / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "level.dat").write_text("original-world-data", encoding="utf-8")

    profile = Profile(
        id=profile_id,
        name="Test Server",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )

    # Create a launch directory
    launch_root = isolated_storage["launch_dir_root"]
    pack_dir = launch_root / "Better-MC"
    _create_minimal_jar(pack_dir / "mods" / "mod.jar")
    (pack_dir / "config" / "cfg.json").parent.mkdir(parents=True, exist_ok=True)
    (pack_dir / "config" / "cfg.json").write_text("{}", encoding="utf-8")
    manifest = {
        "name": "Better-MC",
        "label": "Better MC",
        "mc_version": "1.21.1",
        "build": "fabric",
        "file_count": 2,
        "bytes": 500,
        "folders": ["config", "mods"],
    }
    (pack_dir / "launch.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Setup DB mocks
    stored_profile = profile
    monkeypatch.setattr(db, "get_profile", lambda pid: stored_profile if pid == profile_id else None)
    monkeypatch.setattr(db, "list_profiles", lambda: [stored_profile])
    monkeypatch.setattr(db, "get_runtime_state", lambda pid: None)

    def mock_update(p):
        nonlocal stored_profile
        stored_profile = p

    monkeypatch.setattr(db, "update_profile", mock_update)

    app = FastAPI()
    app.include_router(profiles_router)
    client = TestClient(app)

    # 1. Update profile to assign launch directory "Better-MC"
    resp = client.put(
        f"/api/profiles/{profile_id}",
        data={
            "name": "Test Server",
            "build": "fabric",
            "mc_version": "1.21.1",
            "loader_version": "latest",
            "launch_dir": "Better-MC",
            "java_path": "java",
            "ram_mb": "2048",
            "port": "25565",
        },
    )
    assert resp.status_code == 200
    assert stored_profile.launch_dir == "Better-MC"

    # Omitting launch_dir entirely keeps the current assignment.
    resp = client.put(
        f"/api/profiles/{profile_id}",
        data={
            "name": "Test Server",
            "build": "fabric",
            "mc_version": "1.21.1",
            "loader_version": "latest",
            "java_path": "java",
            "ram_mb": "2048",
            "port": "25565",
        },
    )
    assert resp.status_code == 200
    assert stored_profile.launch_dir == "Better-MC"

    # Workspace server_dir now points to launch directory
    assert workspace.server_dir(stored_profile) == pack_dir

    # Ensure world links connects launch_dir/world -> profile_server/world
    workspace.ensure_world_links(stored_profile)
    assert (pack_dir / "world").is_symlink()
    assert (pack_dir / "world" / "level.dat").read_text("utf-8") == "original-world-data"

    # 2. Assigning incompatible version/loader should fail with 422
    resp = client.put(
        f"/api/profiles/{profile_id}",
        data={
            "name": "Test Server",
            "build": "forge",  # Incompatible with fabric pack
            "mc_version": "1.21.1",
            "loader_version": "latest",
            "launch_dir": "Better-MC",
            "java_path": "java",
            "ram_mb": "2048",
            "port": "25565",
        },
    )
    assert resp.status_code == 422

    # 3. Cannot delete launch directory while assigned to profile
    with pytest.raises(launch_dirs.LaunchDirInUseError):
        launch_dirs.delete_launch_dir("Better-MC")

    # 4. Reset to default profile directory
    resp = client.put(
        f"/api/profiles/{profile_id}",
        data={
            "name": "Test Server",
            "build": "fabric",
            "mc_version": "1.21.1",
            "loader_version": "latest",
            "launch_dir": "",  # Empty string resets to default
            "java_path": "java",
            "ram_mb": "2048",
            "port": "25565",
        },
    )
    assert resp.status_code == 200
    assert stored_profile.launch_dir is None

    # Server dir is back to default profile directory
    assert workspace.server_dir(stored_profile) == server_dir

    # Symlink in pack_dir was unlinked by release_world_links
    assert not (pack_dir / "world").exists()

    # Profile's default world directory is completely preserved!
    assert (world_dir / "level.dat").is_file()
    assert (world_dir / "level.dat").read_text("utf-8") == "original-world-data"

    # Now that it's reset, launch directory can be safely deleted
    launch_dirs.delete_launch_dir("Better-MC")
    assert not pack_dir.exists()


def test_backup_with_launch_directory(isolated_storage, monkeypatch):
    profiles_dir = isolated_storage["profiles_dir"]
    profile_id = "33445566778899001122334455667788"
    profile_dir = profiles_dir / profile_id
    server_dir = profile_dir / "server"
    server_dir.mkdir(parents=True)
    world_dir = server_dir / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "level.dat").write_text("world-save", encoding="utf-8")

    # Launch dir with custom mods and config
    launch_root = isolated_storage["launch_dir_root"]
    pack_dir = launch_root / "Pack-A"
    _create_minimal_jar(pack_dir / "mods" / "mymod.jar")
    (pack_dir / "config" / "cfg.json").parent.mkdir(parents=True, exist_ok=True)
    (pack_dir / "config" / "cfg.json").write_text('{"val": 123}', encoding="utf-8")
    manifest = {
        "name": "Pack-A",
        "mc_version": "1.20.4",
        "build": "fabric",
    }
    (pack_dir / "launch.json").write_text(json.dumps(manifest), encoding="utf-8")

    profile = Profile(
        id=profile_id,
        name="Test Server",
        build=BuildType.FABRIC,
        mc_version="1.20.4",
        loader_version="latest",
        launch_dir="Pack-A",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )

    workspace.ensure_world_links(profile)

    sources = backups._selected_sources(
        profile,
        {"worlds": True, "mods": True, "config": True, "datapacks": False, "server_settings": False},
    )
    source_names = [name for name, _ in sources]
    assert "world" in source_names
    assert "mods" in source_names
    assert "config" in source_names

    # World source is from profile's server directory
    world_path = next(path for name, path in sources if name == "world")
    assert world_path == world_dir

    # Mods and config sources are from launch directory
    mods_path = next(path for name, path in sources if name == "mods")
    assert mods_path == pack_dir / "mods"
    config_path = next(path for name, path in sources if name == "config")
    assert config_path == pack_dir / "config"


def test_launch_dir_conflict_and_live_state(isolated_storage, monkeypatch):
    launch_root = isolated_storage["launch_dir_root"]
    pack_dir = launch_root / "Pack-B"
    _create_minimal_jar(pack_dir / "mods" / "mod.jar")
    manifest = {
        "name": "Pack-B",
        "mc_version": "1.21.1",
        "build": "fabric",
    }
    (pack_dir / "launch.json").write_text(json.dumps(manifest), encoding="utf-8")

    profile1_id = "11111111111111111111111111111111"
    profile2_id = "22222222222222222222222222222222"

    p1 = Profile(
        id=profile1_id,
        name="Server 1",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        launch_dir="Pack-B",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )
    p2 = Profile(
        id=profile2_id,
        name="Server 2",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        launch_dir=None,
        java_path="java",
        ram_mb=2048,
        port=25566,
        rcon_port=25576,
        rcon_password="secretpassword",
    )

    profiles = {profile1_id: p1, profile2_id: p2}
    monkeypatch.setattr(db, "get_profile", lambda pid: profiles.get(pid))
    monkeypatch.setattr(db, "list_profiles", lambda: list(profiles.values()))

    # Assigning Pack-B to profile 2 should fail with 409 because Pack-B is assigned to profile 1
    app = FastAPI()
    app.include_router(profiles_router)
    client = TestClient(app)

    resp = client.put(
        f"/api/profiles/{profile2_id}",
        data={
            "name": "Server 2",
            "build": "fabric",
            "mc_version": "1.21.1",
            "loader_version": "latest",
            "launch_dir": "Pack-B",
            "java_path": "java",
            "ram_mb": "2048",
            "port": "25566",
        },
    )
    assert resp.status_code == 409
    assert "already used by profile" in resp.json()["detail"]

    # Changing launch directory while server is running should fail with 409
    monkeypatch.setattr(
        db,
        "get_runtime_state",
        lambda pid: RuntimeState(
            profile_id=pid,
            status=ProfileStatus.RUNNING,
            pid=9999,
            started_at=None,
            stopped_at=None,
            crash_count=0,
            last_exit_code=None,
        ) if pid == profile1_id else None,
    )

    resp = client.put(
        f"/api/profiles/{profile1_id}",
        data={
            "name": "Server 1",
            "build": "fabric",
            "mc_version": "1.21.1",
            "loader_version": "latest",
            "launch_dir": "",
            "java_path": "java",
            "ram_mb": "2048",
            "port": "25565",
        },
    )
    assert resp.status_code == 409
    assert "Stop the server before changing its launch directory" in resp.json()["detail"]


def test_real_world_folder_in_launch_dir_rejected(isolated_storage):
    launch_root = isolated_storage["launch_dir_root"]
    pack_dir = launch_root / "Pack-C"
    pack_dir.mkdir(parents=True)
    manifest = {
        "name": "Pack-C",
        "mc_version": "1.21.1",
        "build": "fabric",
    }
    (pack_dir / "launch.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Real folder named 'world' in pack_dir
    (pack_dir / "world").mkdir()
    (pack_dir / "world" / "dont_overwrite.txt").write_text("precious", encoding="utf-8")

    profile = Profile(
        id="44444444444444444444444444444444",
        name="Server Real World",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        launch_dir="Pack-C",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )

    with pytest.raises(launch_dirs.LaunchDirError) as exc_info:
        workspace.ensure_world_links(profile)
    assert "contains a real 'world' folder" in str(exc_info.value)



def test_profile_mods_dir_follows_launch_directory(isolated_storage, monkeypatch):
    launch_root = isolated_storage["launch_dir_root"]
    pack_dir = launch_root / "Pack-Mods"
    pack_dir.mkdir(parents=True)
    (pack_dir / "launch.json").write_text(
        json.dumps({"name": "Pack-Mods", "mc_version": "1.21.1", "build": "fabric"}),
        encoding="utf-8",
    )

    profile = Profile(
        id="77777777777777777777777777777777",
        name="Launch Mods",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        launch_dir="Pack-Mods",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )
    monkeypatch.setattr(db, "get_profile", lambda pid: profile if pid == profile.id else None)

    mods_dir = mod_storage.profile_mods_dir(profile.id)
    assert mods_dir == (pack_dir / "mods").resolve()
    assert mods_dir.is_dir()


def test_start_and_restart_missing_or_incompatible_launch_dir(isolated_storage, monkeypatch):
    from mcpanel.api.lifecycle import router as lifecycle_router
    app = FastAPI()
    app.include_router(lifecycle_router)
    client = TestClient(app)

    profile_id = "88888888888888888888888888888888"
    profile = Profile(
        id=profile_id,
        name="Missing Launch Server",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        launch_dir="MissingPack",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )
    state = RuntimeState(profile_id=profile_id, status=ProfileStatus.STOPPED)

    def mock_update_state(s):
        nonlocal state
        state = s

    monkeypatch.setattr(db, "get_profile", lambda pid: profile if pid == profile_id else None)
    monkeypatch.setattr(db, "get_runtime_state", lambda pid: state if pid == profile_id else None)
    monkeypatch.setattr(db, "update_runtime_state", mock_update_state)
    monkeypatch.setattr(db, "claim_runtime_state", lambda pid, exp, new: True)

    # 1. Starting profile with missing launch directory returns 409 and marks status STOPPED
    resp = client.post(f"/api/lifecycle/{profile_id}/start")
    assert resp.status_code == 409
    assert "missing" in resp.json()["detail"].lower()
    assert state.status == ProfileStatus.STOPPED

    # 2. Restarting profile with missing launch directory returns 409 and marks status STOPPED
    resp = client.post(f"/api/lifecycle/{profile_id}/restart")
    assert resp.status_code == 409
    assert "missing" in resp.json()["detail"].lower()
    assert state.status == ProfileStatus.STOPPED


def test_storage_scan_and_clear_logs_for_launch_directory(isolated_storage, monkeypatch):
    from mcpanel.api.storage import router as storage_router
    from mcpanel.core import storage

    launch_root = isolated_storage["launch_dir_root"]
    pack_dir = launch_root / "StoragePack"
    pack_dir.mkdir(parents=True)
    _create_minimal_jar(pack_dir / "mods" / "mod.jar")
    (pack_dir / "logs").mkdir(parents=True)
    (pack_dir / "logs" / "latest.log").write_text("launch-log-line\n", encoding="utf-8")
    (pack_dir / "crash-reports").mkdir(parents=True)
    (pack_dir / "crash-reports" / "crash.txt").write_text("crash\n", encoding="utf-8")
    manifest = {
        "name": "StoragePack",
        "mc_version": "1.21.1",
        "build": "fabric",
    }
    (pack_dir / "launch.json").write_text(json.dumps(manifest), encoding="utf-8")

    profile_id = "99999999999999999999999999999999"
    profile = Profile(
        id=profile_id,
        name="Storage Server",
        build=BuildType.FABRIC,
        mc_version="1.21.1",
        loader_version="latest",
        launch_dir="StoragePack",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )
    profiles_dir = isolated_storage["profiles_dir"]
    profile_server = profiles_dir / profile_id / "server"
    profile_server.mkdir(parents=True)
    (profile_server / "world").mkdir()
    (profile_server / "world" / "level.dat").write_text("world", encoding="utf-8")

    monkeypatch.setattr(db, "get_profile", lambda pid: profile if pid == profile_id else None)
    monkeypatch.setattr(db, "list_profiles_with_states", lambda: [(profile, RuntimeState(profile_id=profile_id, status=ProfileStatus.STOPPED))])
    monkeypatch.setattr(db, "claim_runtime_state", lambda pid, exp, new: True)
    monkeypatch.setattr(db, "get_runtime_state", lambda pid: RuntimeState(profile_id=profile_id, status=ProfileStatus.STOPPED))

    # Storage scan attributes launch directory bytes to the profile
    scan = storage._scan_storage()
    p_entry = next(p for p in scan["profiles"] if p["id"] == profile_id)
    assert p_entry["launch_dir_bytes"] > 0
    assert p_entry["server_bytes"] >= p_entry["launch_dir_bytes"]

    # Clear logs deletes from launch directory as well
    app = FastAPI()
    app.include_router(storage_router)
    client = TestClient(app)

    resp = client.delete(f"/api/storage/profiles/{profile_id}/logs")
    assert resp.status_code == 200
    assert not (pack_dir / "logs" / "latest.log").exists()
    assert not (pack_dir / "crash-reports" / "crash.txt").exists()

