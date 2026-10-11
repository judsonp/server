"""Tests for HEOS AV receiver zone players and their grouping."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlayerFeature
from pyheos import PlayState as HeosPlayState

from music_assistant.providers.heos.avr import AvrZones, ZoneState
from music_assistant.providers.heos.player import HeosPlayer
from music_assistant.providers.heos.zone_player import HeosAvrZonePlayer, zone_player_id

AVR_ID = "418271511"
KITCHEN_ID = "-420593095"


def _setup() -> tuple[HeosPlayer, AvrZones, dict[str, HeosAvrZonePlayer], MagicMock]:
    """Build a receiver HeosPlayer with two zone players, all backed by mocks."""
    provider = MagicMock()
    provider._heos_queue = MagicMock()
    provider.instance_id = "heos"
    device = MagicMock()
    device.player_id = int(AVR_ID)
    device.name = "Receiver"
    device.heos = MagicMock()
    device.heos.set_group = AsyncMock()
    device.heos.remove_group = AsyncMock()
    device.group_id = None
    device.state = HeosPlayState.PLAY
    heos_player = HeosPlayer(provider, device)

    client = MagicMock()
    client.set_power = AsyncMock()
    client.get_status = AsyncMock(return_value={})
    zones = AvrZones(client, {"zone1": "Living", "zone2": "Dining"})
    zones.available = True
    zone_players = {
        zone: HeosAvrZonePlayer(provider, heos_player, zones, zone) for zone in ("zone1", "zone2")
    }

    by_id: dict[str, object] = {
        AVR_ID: heos_player,
        **{zp.player_id: zp for zp in zone_players.values()},
    }
    provider.get_zone_player.side_effect = lambda pid: (
        by_id[pid] if isinstance(by_id.get(pid), HeosAvrZonePlayer) else None
    )
    provider.active_zone_player_ids.side_effect = lambda pid: (
        heos_player.active_zone_player_ids() if pid == AVR_ID else []
    )
    heos_player.attach_zones(zones, zone_players)
    return heos_player, zones, zone_players, client


def test_attach_zones_moves_volume_to_zones() -> None:
    """The receiver's HEOS player gives up volume control to its zone players."""
    heos_player, _, zone_players, _ = _setup()

    assert PlayerFeature.VOLUME_SET not in heos_player.supported_features
    assert PlayerFeature.VOLUME_MUTE not in heos_player.supported_features
    assert heos_player.volume_level is None
    assert PlayerFeature.VOLUME_SET in zone_players["zone2"].supported_features
    assert PlayerFeature.POWER in zone_players["zone2"].supported_features


def test_zones_playing_heos_join_receiver_group() -> None:
    """Zones that are on the HEOS input appear as members of the receiver's group."""
    heos_player, zones, _, _ = _setup()
    assert heos_player.group_members == []

    zones.states["zone1"] = ZoneState(powered=True, source="NET")
    zones.states["zone2"] = ZoneState(powered=True, source="TV")
    heos_player.update_group_members()
    assert heos_player.group_members == [AVR_ID, zone_player_id(AVR_ID, "zone1")]

    zones.states["zone2"] = ZoneState(powered=True, source="NET")
    heos_player.update_group_members()
    assert heos_player.group_members == [
        AVR_ID,
        zone_player_id(AVR_ID, "zone1"),
        zone_player_id(AVR_ID, "zone2"),
    ]


def test_zones_follow_heos_group_leader() -> None:
    """When the receiver is grouped in HEOS, its zones join the group under the leader."""
    heos_player, zones, _, _ = _setup()
    zones.states["zone2"] = ZoneState(powered=True, source="NET")
    heos_player._device.group_id = int(AVR_ID)
    heos_player._heos_group_members = [AVR_ID, KITCHEN_ID]

    heos_player.update_group_members()

    assert heos_player.group_members == [AVR_ID, KITCHEN_ID, zone_player_id(AVR_ID, "zone2")]


async def test_add_zone_to_group_powers_it_on() -> None:
    """Adding a zone to the receiver's group turns the zone on without regrouping HEOS."""
    heos_player, _, _, client = _setup()

    await heos_player.set_members(player_ids_to_add=[zone_player_id(AVR_ID, "zone2")])

    client.set_power.assert_awaited_once_with("zone2", True)
    cast("AsyncMock", heos_player._heos.set_group).assert_not_awaited()
    cast("AsyncMock", heos_player._heos.remove_group).assert_not_awaited()


async def test_remove_zone_from_group_powers_it_off() -> None:
    """Removing a zone from the group turns that zone off."""
    heos_player, _, _, client = _setup()

    await heos_player.set_members(player_ids_to_remove=[zone_player_id(AVR_ID, "zone1")])

    client.set_power.assert_awaited_once_with("zone1", False)
    cast("AsyncMock", heos_player._heos.set_group).assert_not_awaited()


async def test_add_zone_to_other_leader_groups_receiver() -> None:
    """Adding a receiver zone to another speaker's group brings the receiver into HEOS."""
    heos_player, _, zone_players, client = _setup()
    leader_device = MagicMock()
    leader_device.player_id = int(KITCHEN_ID)
    leader_device.name = "Kitchen"
    leader_device.heos = MagicMock()
    leader_device.heos.set_group = AsyncMock()
    leader_device.group_id = None
    kitchen = HeosPlayer(heos_player.provider, leader_device)  # type: ignore[arg-type]

    await kitchen.set_members(player_ids_to_add=[zone_players["zone2"].player_id])

    leader_device.heos.set_group.assert_awaited_once_with([int(KITCHEN_ID), int(AVR_ID)])
    client.set_power.assert_awaited_once_with("zone2", True)
