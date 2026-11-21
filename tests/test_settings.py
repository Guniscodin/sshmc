from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from mcpanel.api.profiles import (
    SERVER_SETTING_SPECS,
    _validate_server_setting,
    router,
)
from mcpanel.core import server_properties
from mcpanel.models import BuildType, Profile


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

    # Admin & Operator Broadcasts validation
    console_op_spec = SERVER_SETTING_SPECS["broadcast-console-to-ops"]
    assert _validate_server_setting("broadcast-console-to-ops", console_op_spec, "true") == "true"
    assert _validate_server_setting("broadcast-console-to-ops", console_op_spec, "false") == "false"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("broadcast-console-to-ops", console_op_spec, "yes")
    assert exc.value.status_code == 422

    rcon_op_spec = SERVER_SETTING_SPECS["broadcast-rcon-to-ops"]
    assert _validate_server_setting("broadcast-rcon-to-ops", rcon_op_spec, "true") == "true"
    assert _validate_server_setting("broadcast-rcon-to-ops", rcon_op_spec, "false") == "false"
    with pytest.raises(HTTPException) as exc:
        _validate_server_setting("broadcast-rcon-to-ops", rcon_op_spec, "invalid")
    assert exc.value.status_code == 422


def test_categories_assigned_to_all_specs():
    valid_categories = {"world", "gameplay", "spawns", "multiplayer", "performance", "admin"}
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
    assert len(fields) >= 34

    # Check that world, performance, multiplayer, and admin settings exist and have correct categories
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

    assert field_map["broadcast-console-to-ops"]["category"] == "admin"
    assert field_map["broadcast-rcon-to-ops"]["category"] == "admin"

    # 2. PUT server-settings with custom world, performance, player policy & admin settings
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
            "broadcast-console-to-ops": "false",
            "broadcast-rcon-to-ops": "false",
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
    assert props["broadcast-console-to-ops"] == "false"
    assert props["broadcast-rcon-to-ops"] == "false"


def test_send_profile_broadcast(mock_profile, monkeypatch):
    from fastapi import FastAPI

    from mcpanel.api import profiles
    from mcpanel.models import ProfileStatus, RuntimeState

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    profile, server_dir = mock_profile

    # 1. Server stopped -> 409
    resp = client.post(
        f"/api/profiles/{profile.id}/broadcast",
        json={"message": "Server stopped broadcast"},
    )
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"].lower()

    # 2. Server running -> simulate RCON
    monkeypatch.setattr(
        profiles.db,
        "get_runtime_state",
        lambda pid: RuntimeState(
            profile_id=pid,
            status=ProfileStatus.RUNNING,
            pid=99999,
            started_at=None,
            stopped_at=None,
            crash_count=0,
            last_exit_code=None,
        ),
    )

    sent_rcon_commands = []

    async def mock_send_command(host, port, password, command):
        sent_rcon_commands.append(command)
        return "Delivered"

    monkeypatch.setattr(profiles.rcon, "send_command", mock_send_command)

    # 2a. Chat broadcast with prefix, color, bold
    resp = client.post(
        f"/api/profiles/{profile.id}/broadcast",
        json={
            "message": "Welcome everyone!",
            "prefix": "[Alert]",
            "display_type": "chat",
            "color": "red",
            "bold": True,
            "italic": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert len(sent_rcon_commands) == 1
    assert sent_rcon_commands[0].startswith("tellraw @a ")
    assert "[Alert]" in sent_rcon_commands[0]
    assert "Welcome everyone!" in sent_rcon_commands[0]
    assert "red" in sent_rcon_commands[0]

    # 2b. Screen Title broadcast
    resp = client.post(
        f"/api/profiles/{profile.id}/broadcast",
        json={
            "message": "Event Starting!",
            "prefix": "",
            "display_type": "title",
            "color": "gold",
            "bold": True,
            "italic": True,
        },
    )
    assert resp.status_code == 200
    assert len(sent_rcon_commands) == 2
    assert sent_rcon_commands[1].startswith("title @a title ")
    assert "Event Starting!" in sent_rcon_commands[1]

    # 2c. Action Bar broadcast
    resp = client.post(
        f"/api/profiles/{profile.id}/broadcast",
        json={
            "message": "Action bar info",
            "prefix": "[Info]",
            "display_type": "actionbar",
            "color": "aqua",
        },
    )
    assert resp.status_code == 200
    assert len(sent_rcon_commands) == 3
    assert sent_rcon_commands[2].startswith("title @a actionbar ")
    assert "Action bar info" in sent_rcon_commands[2]

    # 3. Invalid payload / empty message -> 422
    resp = client.post(
        f"/api/profiles/{profile.id}/broadcast",
        json={"message": "   "},
    )
    assert resp.status_code == 422


def test_gamerules_api(mock_profile, monkeypatch):
    from fastapi import FastAPI

    from mcpanel.api import profiles
    from mcpanel.models import ProfileStatus, RuntimeState

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    profile, _ = mock_profile

    # 1. GET gamerules when server stopped
    resp = client.get(f"/api/profiles/{profile.id}/gamerules")
    assert resp.status_code == 200
    data = resp.json()
    assert data["server_running"] is False
    assert len(data["rules"]) == 15
    rule_map = {r["key"]: r for r in data["rules"]}
    assert rule_map["keepInventory"]["category"] == "player"
    assert rule_map["mobGriefing"]["category"] == "world"
    assert rule_map["doMobLoot"]["category"] == "drops"

    # 2. PUT gamerules when server stopped -> 409
    resp = client.put(
        f"/api/profiles/{profile.id}/gamerules/keepInventory",
        json={"value": "true"},
    )
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"].lower()

    # 3. Simulate running server
    monkeypatch.setattr(
        profiles.db,
        "get_runtime_state",
        lambda pid: RuntimeState(
            profile_id=pid,
            status=ProfileStatus.RUNNING,
            pid=99999,
            started_at=None,
            stopped_at=None,
            crash_count=0,
            last_exit_code=None,
        ),
    )

    # 3a. GET gamerules live via mock rcon.send_commands
    async def mock_send_commands(host, port, password, commands, timeout=5.0):
        responses = []
        for cmd in commands:
            if "keepInventory" in cmd:
                responses.append("Gamerule keepInventory is currently set to: true")
            elif "playersSleepingPercentage" in cmd:
                responses.append("Gamerule playersSleepingPercentage is currently set to: 50")
            elif "mobGriefing" in cmd:
                responses.append("Gamerule mobGriefing is currently set to: false")
            else:
                responses.append("Gamerule is currently set to: true")
        return responses

    monkeypatch.setattr(profiles.rcon, "send_commands", mock_send_commands)

    resp = client.get(f"/api/profiles/{profile.id}/gamerules")
    assert resp.status_code == 200
    live_data = resp.json()
    assert live_data["server_running"] is True
    live_map = {r["key"]: r for r in live_data["rules"]}
    assert live_map["keepInventory"]["value"] is True
    assert live_map["playersSleepingPercentage"]["value"] == 50
    assert live_map["mobGriefing"]["value"] is False

    # 3b. PUT gamerules live via mock rcon.send_command
    sent_rcon = []

    async def mock_send_command(host, port, password, command):
        sent_rcon.append(command)
        return "Gamerule updated"

    monkeypatch.setattr(profiles.rcon, "send_command", mock_send_command)

    # Valid bool update
    resp = client.put(
        f"/api/profiles/{profile.id}/gamerules/keepInventory",
        json={"value": "false"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["value"] is False
    assert "gamerule keepInventory false" in sent_rcon

    # Valid int update
    resp = client.put(
        f"/api/profiles/{profile.id}/gamerules/playersSleepingPercentage",
        json={"value": "25"},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == 25
    assert "gamerule playersSleepingPercentage 25" in sent_rcon

    # 4. Unknown gamerule -> 404
    resp = client.put(
        f"/api/profiles/{profile.id}/gamerules/unknownGamerule123",
        json={"value": "true"},
    )
    assert resp.status_code == 404

    # 5. Invalid value -> 422
    resp = client.put(
        f"/api/profiles/{profile.id}/gamerules/keepInventory",
        json={"value": "not_a_bool"},
    )
    assert resp.status_code == 422

    # Integer out of range -> 422
    resp = client.put(
        f"/api/profiles/{profile.id}/gamerules/playersSleepingPercentage",
        json={"value": "200"},
    )
    assert resp.status_code == 422


# ========================================================================
# _parse_gamerule_value unit tests — realistic Minecraft RCON responses
# ========================================================================
def test_parse_gamerule_value_int_formats():
    from mcpanel.api.profiles import _parse_gamerule_value

    spec_int = {"type": "int", "min": 0, "max": 100}

    # Vanilla "name is set to: value"
    assert _parse_gamerule_value("playersSleepingPercentage is set to: 1", "int") == 1
    assert _parse_gamerule_value("playersSleepingPercentage is set to: 100", "int") == 100
    assert _parse_gamerule_value("playersSleepingPercentage is set to: 0", "int") == 0

    # Vanilla "name = value"
    assert _parse_gamerule_value("playersSleepingPercentage = 1", "int") == 1
    assert _parse_gamerule_value("playersSleepingPercentage = 50", "int") == 50

    # Bare value only
    assert _parse_gamerule_value("1", "int") == 1
    assert _parse_gamerule_value("100", "int") == 100

    # "Gamerule name is currently set to: value" (used in existing tests)
    assert _parse_gamerule_value("Gamerule playersSleepingPercentage is currently set to: 25", "int") == 25

    # Response with extra text after value
    assert _parse_gamerule_value("playersSleepingPercentage is set to: 1 (players in bed)", "int") == 1

    # Zero is valid
    assert _parse_gamerule_value("playersSleepingPercentage is set to: 0", "int") == 0


def test_parse_gamerule_value_bool_formats():
    from mcpanel.api.profiles import _parse_gamerule_value

    # Bool responses
    assert _parse_gamerule_value("keepInventory is set to: true", "bool") is True
    assert _parse_gamerule_value("keepInventory is set to: false", "bool") is False
    assert _parse_gamerule_value("keepInventory = true", "bool") is True
    assert _parse_gamerule_value("true", "bool") is True
    assert _parse_gamerule_value("false", "bool") is False
    assert _parse_gamerule_value("Gamerule keepInventory is currently set to: false", "bool") is False


def test_parse_gamerule_value_fallback_to_default():
    from mcpanel.api.profiles import _parse_gamerule_value

    # Unparseable responses return None (caller falls back to spec default)
    assert _parse_gamerule_value("", "int") is None
    assert _parse_gamerule_value("Unknown gamerule", "int") is None
    assert _parse_gamerule_value("Command failed", "int") is None
    assert _parse_gamerule_value("", "bool") is None


# ========================================================================
# Integration: full gamerule set + get pipeline (simulates refresh)
# ========================================================================
def test_gamerule_set_then_get_returns_updated_value(mock_profile, monkeypatch):
    """Simulates: set gamerule, then refresh page (GET gamerules) — value must persist."""
    from fastapi import FastAPI

    from mcpanel.api import profiles
    from mcpanel.core import rcon
    from mcpanel.models import ProfileStatus, RuntimeState

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    profile, _ = mock_profile

    # Simulate running server
    monkeypatch.setattr(
        profiles.db,
        "get_runtime_state",
        lambda pid: RuntimeState(
            profile_id=pid,
            status=ProfileStatus.RUNNING,
            pid=99999,
            started_at=None,
            stopped_at=None,
            crash_count=0,
            last_exit_code=None,
        ),
    )

    # Track the "in-memory" gamerule state on the mock server
    server_state = {"playersSleepingPercentage": 100, "keepInventory": True}

    # Mock send_command (SET) — updates server_state
    async def mock_send_command(host, port, password, command, timeout=5.0):
        parts = command.split()
        if parts[0] == "gamerule" and len(parts) == 3:
            rule_name = parts[1]
            rule_val = parts[2]
            if rule_name in server_state:
                if isinstance(server_state[rule_name], bool):
                    server_state[rule_name] = rule_val.lower() == "true"
                else:
                    server_state[rule_name] = int(rule_val)
            return f"Gamerule {rule_name} is now {rule_val}"
        raise rcon.RCONError(f"Unknown command: {command}")

    # Mock send_commands (GET) — queries server_state
    async def mock_send_commands(host, port, password, commands, timeout=5.0):
        responses = []
        for cmd in commands:
            parts = cmd.split()
            if parts[0] == "gamerule" and len(parts) == 2:
                rule_name = parts[1]
                if rule_name in server_state:
                    val = server_state[rule_name]
                    responses.append(f"{rule_name} is set to: {val}")
                else:
                    responses.append("Unknown gamerule")
            else:
                responses.append("Unknown command")
        return responses

    monkeypatch.setattr(profiles.rcon, "send_command", mock_send_command)
    monkeypatch.setattr(profiles.rcon, "send_commands", mock_send_commands)

    # Initial GET — should show default 100
    resp = client.get(f"/api/profiles/{profile.id}/gamerules")
    assert resp.status_code == 200
    rules = {r["key"]: r for r in resp.json()["rules"]}
    assert rules["playersSleepingPercentage"]["value"] == 100

    # PUT — set sleeping percentage to 1
    resp = client.put(
        f"/api/profiles/{profile.id}/gamerules/playersSleepingPercentage",
        json={"value": "1"},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == 1

    # GET again (simulates page refresh) — value must still be 1
    resp = client.get(f"/api/profiles/{profile.id}/gamerules")
    assert resp.status_code == 200
    rules = {r["key"]: r for r in resp.json()["rules"]}
    assert rules["playersSleepingPercentage"]["value"] == 1, (
        f"Expected playersSleepingPercentage=1 after refresh, got {rules['playersSleepingPercentage']['value']}"
    )


# ========================================================================
# send_commands resilience: one failing query must not invalidate the batch
# ========================================================================
def test_send_commands_per_command_failure_resilience(mock_profile, monkeypatch):
    """One slow/failed RCON query in a batch must not invalidate other commands."""
    from fastapi import FastAPI

    from mcpanel.api import profiles
    from mcpanel.models import ProfileStatus, RuntimeState

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    profile, _ = mock_profile

    monkeypatch.setattr(
        profiles.db,
        "get_runtime_state",
        lambda pid: RuntimeState(
            profile_id=pid,
            status=ProfileStatus.RUNNING,
            pid=99999,
            started_at=None,
            stopped_at=None,
            crash_count=0,
            last_exit_code=None,
        ),
    )

    # One command returns empty (simulating timeout caught by send_commands), others succeed
    async def mock_send_commands_partial(host, port, password, commands, timeout=5.0):
        responses = []
        for cmd in commands:
            if "doMobLoot" in cmd:
                responses.append("")  # simulated timeout -> empty response
            elif "playersSleepingPercentage" in cmd:
                responses.append("playersSleepingPercentage is set to: 1")
            else:
                responses.append("OK")
        return responses

    monkeypatch.setattr(profiles.rcon, "send_commands", mock_send_commands_partial)

    resp = client.get(f"/api/profiles/{profile.id}/gamerules")
    assert resp.status_code == 200
    rules = {r["key"]: r for r in resp.json()["rules"]}

    # doMobLoot fell back to default (True) because its query timed out
    assert rules["doMobLoot"]["value"] is True
    # playersSleepingPercentage was NOT affected by the sibling failure
    assert rules["playersSleepingPercentage"]["value"] == 1



