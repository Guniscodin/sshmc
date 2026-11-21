import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mcpanel.api.profiles import (
    router,
    SERVER_SETTING_SPECS,
    _validate_server_setting,
)
from mcpanel.core import server_properties
from mcpanel.models import Profile, BuildType


def test_validate_server_setting_world_options():
    # World folder name validation
    name_spec = SERVER_SETTING_SPECS["level-name"]
    assert _validate_server_setting("level-name", name_spec, "world") == "world"
    assert _validate_server_setting("level-name", name_spec, "custom_world-1") == "custom_world-1"

    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("level-name", name_spec, "")
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("level-name", name_spec, "../traversal")
    assert exc.value.status_code == 422

    # World seed validation
    seed_spec = SERVER_SETTING_SPECS["level-seed"]
    assert _validate_server_setting("level-seed", seed_spec, "") == ""
    assert _validate_server_setting("level-seed", seed_spec, "   ") == ""
    assert _validate_server_setting("level-seed", seed_spec, "-123456789") == "-123456789"
    assert _validate_server_setting("level-seed", seed_spec, "cool_minecraft_seed") == "cool_minecraft_seed"

    # World preset type validation
    type_spec = SERVER_SETTING_SPECS["level-type"]
    assert _validate_server_setting("level-type", type_spec, "minecraft:normal") == "minecraft:normal"
    assert _validate_server_setting("level-type", type_spec, "minecraft:flat") == "minecraft:flat"

    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("level-type", type_spec, "invalid:custom")
    assert exc.value.status_code == 422

    # Generate structures validation
    struct_spec = SERVER_SETTING_SPECS["generate-structures"]
    assert _validate_server_setting("generate-structures", struct_spec, "true") == "true"
    assert _validate_server_setting("generate-structures", struct_spec, "FALSE") == "false"

    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("generate-structures", struct_spec, "invalid")
    assert exc.value.status_code == 422

    # World border max-world-size validation
    border_spec = SERVER_SETTING_SPECS["max-world-size"]
    assert _validate_server_setting("max-world-size", border_spec, "29999984") == "29999984"
    assert _validate_server_setting("max-world-size", border_spec, "1000") == "1000"

    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("max-world-size", border_spec, "0")
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("max-world-size", border_spec, "30000000")
    assert exc.value.status_code == 422

    # Performance & Crash Prevention validation
    tick_spec = SERVER_SETTING_SPECS["max-tick-time"]
    assert _validate_server_setting("max-tick-time", tick_spec, "-1") == "-1"
    assert _validate_server_setting("max-tick-time", tick_spec, "60000") == "60000"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("max-tick-time", tick_spec, "-2")
    assert exc.value.status_code == 422

    entity_spec = SERVER_SETTING_SPECS["entity-broadcast-range-percentage"]
    assert _validate_server_setting("entity-broadcast-range-percentage", entity_spec, "100") == "100"
    assert _validate_server_setting("entity-broadcast-range-percentage", entity_spec, "50") == "50"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("entity-broadcast-range-percentage", entity_spec, "5")
    assert exc.value.status_code == 422

    compress_spec = SERVER_SETTING_SPECS["network-compression-threshold"]
    assert _validate_server_setting("network-compression-threshold", compress_spec, "-1") == "-1"
    assert _validate_server_setting("network-compression-threshold", compress_spec, "256") == "256"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("network-compression-threshold", compress_spec, "-5")
    assert exc.value.status_code == 422

    neighbor_spec = SERVER_SETTING_SPECS["max-chained-neighbor-updates"]
    assert _validate_server_setting("max-chained-neighbor-updates", neighbor_spec, "1000000") == "1000000"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("max-chained-neighbor-updates", neighbor_spec, "0")
    assert exc.value.status_code == 422

    sync_spec = SERVER_SETTING_SPECS["sync-chunk-writes"]
    assert _validate_server_setting("sync-chunk-writes", sync_spec, "true") == "true"
    assert _validate_server_setting("sync-chunk-writes", sync_spec, "false") == "false"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("sync-chunk-writes", sync_spec, "maybe")
    assert exc.value.status_code == 422

    # Player & Network Policies validation
    idle_spec = SERVER_SETTING_SPECS["player-idle-timeout"]
    assert _validate_server_setting("player-idle-timeout", idle_spec, "0") == "0"
    assert _validate_server_setting("player-idle-timeout", idle_spec, "15") == "15"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("player-idle-timeout", idle_spec, "-1")
    assert exc.value.status_code == 422

    hide_spec = SERVER_SETTING_SPECS["hide-online-players"]
    assert _validate_server_setting("hide-online-players", hide_spec, "true") == "true"
    assert _validate_server_setting("hide-online-players", hide_spec, "false") == "false"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("hide-online-players", hide_spec, "invalid")
    assert exc.value.status_code == 422

    proxy_spec = SERVER_SETTING_SPECS["prevent-proxy-connections"]
    assert _validate_server_setting("prevent-proxy-connections", proxy_spec, "true") == "true"
    assert _validate_server_setting("prevent-proxy-connections", proxy_spec, "false") == "false"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("prevent-proxy-connections", proxy_spec, "invalid")
    assert exc.value.status_code == 422

    status_spec = SERVER_SETTING_SPECS["enable-status"]
    assert _validate_server_setting("enable-status", status_spec, "true") == "true"
    assert _validate_server_setting("enable-status", status_spec, "false") == "false"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("enable-status", status_spec, "no")
    assert exc.value.status_code == 422


def test_categories_assigned_to_all_specs():
    valid_categories = {"world", "gameplay", "spawns", "multiplayer", "performance"}
    for key, spec in SERVER_SETTING_SPECS.items():
        assert "category" in spec, f"Spec '{key}' is missing a category"
        assert spec["category"] in valid_categories, f"Spec '{key}' has invalid category '{spec['category']}'"


@pytest.fixture
def mock_profile(tmp_path, monkeypatch):
    profile_id = "0123456789abcdef0123456789abcdef"
    profile_dir = tmp_path / profile_id
    server_dir = profile_dir / "server"
    server_dir.mkdir(parents=True)

    profile = Profile(
        id=profile_id,
        name="Test Server",
        build=BuildType.VANILLA,
        mc_version="1.20.4",
        loader_version="latest",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )

    from mcpanel import db
    from mcpanel.api import profiles
    monkeypatch.setattr(profiles.mod_storage, "profile_root", lambda pid: profile_dir)
    monkeypatch.setattr(profiles.db, "get_profile", lambda pid: profile if pid == profile_id else None)
    monkeypatch.setattr(profiles.db, "get_runtime_state", lambda pid: None)
    monkeypatch.setattr(server_properties, "properties_path", lambda pid: server_dir / "server.properties")
    monkeypatch.setattr(server_properties, "saved_settings_path", lambda pid: profile_dir / "server_settings.json")

    props_file = server_dir / "server.properties"
    props_file.write_text("online-mode=true\nlevel-name=world\n", encoding="utf-8")

    return profile, server_dir


def test_get_and_update_server_settings_api(mock_profile):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    profile, server_dir = mock_profile

    # 1. GET server-settings
    resp = client.get(f"/api/profiles/{profile.id}/server-settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["profile_id"] == profile.id
    fields = data["fields"]
    assert len(fields) >= 32

    # Check that world, performance, and multiplayer settings exist and have correct categories
    field_map = {f["key"]: f for f in fields}
    assert field_map["level-name"]["category"] == "world"
    assert field_map["level-name"]["value"] == "world"
    assert field_map["level-seed"]["category"] == "world"
    assert field_map["max-world-size"]["category"] == "world"
    assert field_map["generate-structures"]["category"] == "world"
    assert field_map["level-type"]["category"] == "world"

    assert field_map["max-tick-time"]["category"] == "performance"
    assert field_map["entity-broadcast-range-percentage"]["category"] == "performance"
    assert field_map["network-compression-threshold"]["category"] == "performance"
    assert field_map["max-chained-neighbor-updates"]["category"] == "performance"
    assert field_map["sync-chunk-writes"]["category"] == "performance"

    assert field_map["player-idle-timeout"]["category"] == "multiplayer"
    assert field_map["hide-online-players"]["category"] == "multiplayer"
    assert field_map["prevent-proxy-connections"]["category"] == "multiplayer"
    assert field_map["enable-status"]["category"] == "multiplayer"

    # 2. PUT server-settings with custom world, performance & player policy settings
    payload = {
        "properties": {
            "level-name": "skyblock_world",
            "level-seed": "987654321",
            "level-type": "minecraft:flat",
            "generate-structures": "false",
            "max-world-size": "5000",
            "difficulty": "hard",
            "max-tick-time": "-1",
            "entity-broadcast-range-percentage": "75",
            "network-compression-threshold": "-1",
            "max-chained-neighbor-updates": "500000",
            "sync-chunk-writes": "false",
            "player-idle-timeout": "10",
            "hide-online-players": "true",
            "prevent-proxy-connections": "true",
            "enable-status": "false",
        }
    }
    update_resp = client.put(f"/api/profiles/{profile.id}/server-settings", json=payload)
    assert update_resp.status_code == 200

    # 3. Verify server.properties has updated values
    props = server_properties.read_properties(server_dir / "server.properties")
    assert props["level-name"] == "skyblock_world"
    assert props["level-seed"] == "987654321"
    assert props["level-type"] == "minecraft:flat"
    assert props["generate-structures"] == "false"
    assert props["max-world-size"] == "5000"
    assert props["difficulty"] == "hard"
    assert props["max-tick-time"] == "-1"
    assert props["entity-broadcast-range-percentage"] == "75"
    assert props["network-compression-threshold"] == "-1"
    assert props["max-chained-neighbor-updates"] == "500000"
    assert props["sync-chunk-writes"] == "false"
    assert props["player-idle-timeout"] == "10"
    assert props["hide-online-players"] == "true"
    assert props["prevent-proxy-connections"] == "true"
    assert props["enable-status"] == "false"
