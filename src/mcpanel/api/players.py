"""HTTP routes for whitelist and operator management.

The routes only translate requests into calls on ``core.players``; all file and
RCON behaviour lives there.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from mcpanel import db
from mcpanel.core import players

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/profiles/{profile_id}/players", tags=["players"])


class PlayerName(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class WhitelistSettings(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=32)


class OperatorCreate(PlayerName):
    level: int = Field(default=4, ge=1, le=4)


class OperatorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    level: int | None = Field(default=None, ge=1, le=4)
    bypasses_player_limit: bool | None = None


def _profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.get("")
async def get_players(profile_id: str):
    """Current whitelist and operator state for a profile."""
    profile = _profile(profile_id)
    return await asyncio.to_thread(players.read_state, profile)


@router.post("/whitelist", status_code=status.HTTP_201_CREATED)
async def add_whitelisted_player(profile_id: str, payload: PlayerName):
    profile = _profile(profile_id)
    return await players.add_whitelist(profile, payload.name)


@router.delete("/whitelist/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_whitelisted_player(profile_id: str, name: str):
    profile = _profile(profile_id)
    await players.remove_whitelist(profile, name)


@router.put("/whitelist/settings")
async def update_whitelist_settings(profile_id: str, payload: WhitelistSettings):
    profile = _profile(profile_id)
    if payload.enabled is not None:
        return await players.set_whitelist_enabled(profile, payload.enabled)
    if payload.name is not None:
        return await players.rename_whitelist(profile, "settings", payload.name)
    raise HTTPException(status_code=422, detail="Provide an enabled state or a player name.")


# Keep this after /whitelist/settings so "settings" is not treated as a name.
@router.put("/whitelist/{name}")
async def rename_whitelisted_player(profile_id: str, name: str, payload: PlayerName):
    profile = _profile(profile_id)
    return await players.rename_whitelist(profile, name, payload.name)


@router.post("/ops", status_code=status.HTTP_201_CREATED)
async def add_operator(profile_id: str, payload: OperatorCreate):
    profile = _profile(profile_id)
    return await players.add_operator(profile, payload.name, payload.level)


@router.put("/ops/{name}")
async def update_operator(profile_id: str, name: str, payload: OperatorUpdate):
    profile = _profile(profile_id)
    if payload.name is not None:
        if payload.level is not None or payload.bypasses_player_limit is not None:
            raise HTTPException(
                status_code=422,
                detail="Rename an operator separately from level or bypass changes.",
            )
        return await players.rename_operator(profile, name, payload.name)
    return await players.update_operator(
        profile, name, payload.level, payload.bypasses_player_limit
    )


@router.delete("/ops/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_operator(profile_id: str, name: str):
    profile = _profile(profile_id)
    await players.remove_operator(profile, name)
