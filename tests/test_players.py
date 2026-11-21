import json
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mcpanel.api.players import router
from mcpanel.core import players
from mcpanel.models import Profile


def test_parse_name():
    assert players.parse_name("Steve") == "Steve"
    assert players.parse_name("  alex_123  ") == "alex_123"

    with pytest.raises(HTTPException) as exc:
        players.parse_name("")
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_name("invalid name with spaces")
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_name("a" * 33)
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_name("ab")
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_name("Notch*")
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_name("+Player")
    assert exc.value.status_code == 422


def test_parse_level():
    assert players.parse_level(1) == 1
    assert players.parse_level(2) == 2
    assert players.parse_level(3) == 3
    assert players.parse_level(4) == 4
    assert players.parse_level("3") == 3

    with pytest.raises(HTTPException) as exc:
        players.parse_level(None)
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_level(0)
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_level(5)
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        players.parse_level("not_a_number")
    assert exc.value.status_code == 422


def test_dashed_and_offline_uuid():
    raw_hex = "069a79f444e94726a5befca90e38aaf5"
    dashed = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    assert players._dashed(raw_hex) == dashed
    assert players._dashed(dashed) == dashed
    assert players._dashed("invalid") == ""
    assert players._dashed(None) == ""

    uuid1 = players.offline_uuid("Steve")
    uuid2 = players.offline_uuid("Steve")
    uuid3 = players.offline_uuid("Alex")
    assert uuid1 == uuid2
    assert uuid1 != uuid3
    assert len(uuid1) == 36


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mock_profile(tmp_path, monkeypatch):
    profile_id = "0123456789abcdef0123456789abcdef"
    profile_dir = tmp_path / profile_id
    server_dir = profile_dir / "server"
    server_dir.mkdir(parents=True)

    profile = Profile(
        id=profile_id,
        name="Test Server",
        build="vanilla",
        mc_version="1.20.4",
        loader_version="latest",
        java_path="java",
        ram_mb=2048,
        port=25565,
        rcon_port=25575,
        rcon_password="secretpassword",
    )

    monkeypatch.setattr(players.mod_storage, "profile_root", lambda pid: profile_dir)
    monkeypatch.setattr(players.db, "get_profile", lambda pid: profile if pid == profile_id else None)
    monkeypatch.setattr(players.db, "get_runtime_state", lambda pid: None)
    monkeypatch.setattr(players.server_properties, "properties_path", lambda pid: server_dir / "server.properties")
    monkeypatch.setattr(players.server_properties, "saved_settings_path", lambda pid: profile_dir / "server_settings.json")

    # Default offline mode for testing
    props_file = server_dir / "server.properties"
    props_file.write_text("online-mode=false\nwhite-list=false\n", encoding="utf-8")

    return profile, server_dir


@pytest.mark.anyio
async def test_whitelist_operations(mock_profile):
    profile, server_dir = mock_profile

    # Initial state
    state = players.read_state(profile)
    assert state["whitelist"]["enabled"] is False
    assert state["whitelist"]["players"] == []
    assert state["operators"] == []

    # Toggle whitelist
    res = await players.set_whitelist_enabled(profile, True)
    assert res["enabled"] is True
    state = players.read_state(profile)
    assert state["whitelist"]["enabled"] is True

    # Add player to whitelist
    added = await players.add_whitelist(profile, "Steve")
    assert added["name"] == "Steve"
    assert added["uuid"] == players.offline_uuid("Steve")

    # Re-adding returns existing
    readded = await players.add_whitelist(profile, "steve")
    assert readded["name"] == "Steve"

    # Rename player
    renamed = await players.rename_whitelist(profile, "Steve", "Alex")
    assert renamed["name"] == "Alex"
    assert renamed["uuid"] == players.offline_uuid("Alex")

    # Rename to same name is no-op
    noop = await players.rename_whitelist(profile, "Alex", "Alex")
    assert noop["name"] == "Alex"

    # Rename non-existent player raises 404
    with pytest.raises(HTTPException) as exc:
        await players.rename_whitelist(profile, "NotFound", "Other")
    assert exc.value.status_code == 404

    # Remove player
    await players.remove_whitelist(profile, "Alex")
    state = players.read_state(profile)
    assert state["whitelist"]["players"] == []

    # Removing non-existent player raises 404
    with pytest.raises(HTTPException) as exc:
        await players.remove_whitelist(profile, "Alex")
    assert exc.value.status_code == 404


@pytest.mark.anyio
async def test_operator_operations(mock_profile):
    profile, server_dir = mock_profile

    # Add operator
    op = await players.add_operator(profile, "AdminUser", 3)
    assert op["name"] == "AdminUser"
    assert op["level"] == 3
    assert op["bypasses_player_limit"] is False

    # Update operator level and bypass
    updated = await players.update_operator(profile, "AdminUser", level=4, bypasses_player_limit=True)
    assert updated["level"] == 4
    assert updated["bypasses_player_limit"] is True

    # Rename operator preserves level and bypass
    renamed = await players.rename_operator(profile, "AdminUser", "SuperAdmin")
    assert renamed["name"] == "SuperAdmin"
    assert renamed["level"] == 4
    assert renamed["bypasses_player_limit"] is True

    # Conflict on rename raises 409
    await players.add_operator(profile, "SecondAdmin", 2)
    with pytest.raises(HTTPException) as exc:
        await players.rename_operator(profile, "SuperAdmin", "SecondAdmin")
    assert exc.value.status_code == 409

    # Remove operator
    await players.remove_operator(profile, "SuperAdmin")
    state = players.read_state(profile)
    assert len(state["operators"]) == 1
    assert state["operators"][0]["name"] == "SecondAdmin"


def test_api_routes(mock_profile, monkeypatch):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    profile, server_dir = mock_profile

    # GET players
    resp = client.get(f"/api/profiles/{profile.id}/players")
    assert resp.status_code == 200
    data = resp.json()
    assert "whitelist" in data
    assert "operators" in data

    # POST whitelist
    resp = client.post(f"/api/profiles/{profile.id}/players/whitelist", json={"name": "Alice"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "Alice"

    # PUT whitelist rename
    resp = client.put(f"/api/profiles/{profile.id}/players/whitelist/Alice", json={"name": "AliceRenamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "AliceRenamed"

    # PUT whitelist/settings toggle
    resp = client.put(f"/api/profiles/{profile.id}/players/whitelist/settings", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    # DELETE whitelist
    resp = client.delete(f"/api/profiles/{profile.id}/players/whitelist/AliceRenamed")
    assert resp.status_code == 204

    # POST ops
    resp = client.post(f"/api/profiles/{profile.id}/players/ops", json={"name": "OpBoss", "level": 4})
    assert resp.status_code == 201
    assert resp.json()["name"] == "OpBoss"
    assert resp.json()["level"] == 4

    # PUT ops rename
    resp = client.put(f"/api/profiles/{profile.id}/players/ops/OpBoss", json={"name": "OpChief"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "OpChief"

    # PUT ops level
    resp = client.put(f"/api/profiles/{profile.id}/players/ops/OpChief", json={"level": 2})
    assert resp.status_code == 200
    assert resp.json()["level"] == 2

    # PUT ops combining rename and level raises 422
    resp = client.put(f"/api/profiles/{profile.id}/players/ops/OpChief", json={"name": "OpNew", "level": 3})
    assert resp.status_code == 422

    # DELETE ops
    resp = client.delete(f"/api/profiles/{profile.id}/players/ops/OpChief")
    assert resp.status_code == 204


@pytest.mark.anyio
async def test_live_server_rcon_and_rollback(mock_profile, monkeypatch):
    profile, server_dir = mock_profile

    # Mark server as running
    from mcpanel.models import RuntimeState, ProfileStatus
    monkeypatch.setattr(
        players.db,
        "get_runtime_state",
        lambda pid: RuntimeState(
            profile_id=pid,
            status=ProfileStatus.RUNNING,
            pid=12345,
            started_at=None,
            stopped_at=None,
            crash_count=0,
            last_exit_code=None,
        )
    )

    rcon_commands = []

    async def mock_send_command(host, port, password, command):
        rcon_commands.append(command)
        # Simulate server writing to whitelist.json/ops.json upon RCON command
        if command.startswith("whitelist add "):
            name = command.split(" ", 2)[2]
            wfile = server_dir / "whitelist.json"
            current = json.loads(wfile.read_text("utf-8")) if wfile.exists() else []
            current.append({"uuid": players.offline_uuid(name), "name": name})
            wfile.write_text(json.dumps(current), encoding="utf-8")
            return f"Added {name} to the whitelist"
        if command.startswith("whitelist remove "):
            name = command.split(" ", 2)[2]
            wfile = server_dir / "whitelist.json"
            if wfile.exists():
                current = json.loads(wfile.read_text("utf-8"))
                current = [e for e in current if e.get("name", "").lower() != name.lower()]
                wfile.write_text(json.dumps(current), encoding="utf-8")
            return f"Removed {name} from the whitelist"
        if command.startswith("op "):
            parts = command.split(" ")
            name = parts[1]
            level = int(parts[2]) if len(parts) > 2 else 4
            ofile = server_dir / "ops.json"
            current = json.loads(ofile.read_text("utf-8")) if ofile.exists() else []
            current.append({
                "uuid": players.offline_uuid(name),
                "name": name,
                "level": level,
                "bypassesPlayerLimit": False,
            })
            ofile.write_text(json.dumps(current), encoding="utf-8")
            return f"Made {name} a server operator"
        if command.startswith("deop "):
            name = command.split(" ", 1)[1]
            ofile = server_dir / "ops.json"
            if ofile.exists():
                current = json.loads(ofile.read_text("utf-8"))
                current = [e for e in current if e.get("name", "").lower() != name.lower()]
                ofile.write_text(json.dumps(current), encoding="utf-8")
            return f"Made {name} no longer a server operator"
        return "OK"

    monkeypatch.setattr(players.rcon, "send_command", mock_send_command)

    # 1. Add player to whitelist live
    added = await players.add_whitelist(profile, "LivePlayer")
    assert added["name"] == "LivePlayer"
    assert "whitelist add LivePlayer" in rcon_commands

    # 2. Rename player live
    renamed = await players.rename_whitelist(profile, "LivePlayer", "LiveRenamed")
    assert renamed["name"] == "LiveRenamed"
    assert "whitelist add LiveRenamed" in rcon_commands
    assert "whitelist remove LivePlayer" in rcon_commands

    # 3. Add operator live (vanilla /op takes no level arg; level is patched into ops.json after)
    op = await players.add_operator(profile, "LiveOp", 3)
    assert op["name"] == "LiveOp"
    assert op["level"] == 3
    assert "op LiveOp" in rcon_commands  # no level suffix in the RCON command

    # 4. Rename operator live
    op_renamed = await players.rename_operator(profile, "LiveOp", "LiveOpBoss")
    assert op_renamed["name"] == "LiveOpBoss"
    assert "op LiveOpBoss" in rcon_commands  # no level suffix in the RCON command
    assert "deop LiveOp" in rcon_commands

    # 5. Live operator cannot change bypass limit
    with pytest.raises(HTTPException) as exc:
        await players.update_operator(profile, "LiveOpBoss", bypasses_player_limit=True)
    assert exc.value.status_code == 409

    # 6. Server starting / stopping returns 409
    monkeypatch.setattr(
        players.db,
        "get_runtime_state",
        lambda pid: RuntimeState(
            profile_id=pid,
            status=ProfileStatus.STARTING,
            pid=12345,
            started_at=None,
            stopped_at=None,
            crash_count=0,
            last_exit_code=None,
        )
    )
    with pytest.raises(HTTPException) as exc:
        await players.add_whitelist(profile, "AnyPlayer")
    assert exc.value.status_code == 409
