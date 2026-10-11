"""Tests for HEOS AV receiver zone players and their grouping."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, PlayerFeature
from pyheos import PlayState as HeosPlayState

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.heos.avr import AvrZones, ZoneState
from music_assistant.providers.heos.grouping import group_members_by_leader, plan_member_change
from music_assistant.providers.heos.player import HeosPlayer
from music_assistant.providers.heos.zone_player import HeosAvrZonePlayer, zone_player_id

AVR_ID = "418271511"
KITCHEN_ID = "-420593095"
LIVING_ID = zone_player_id(AVR_ID, "zone1")
DINING_ID = zone_player_id(AVR_ID, "zone2")

ON_HEOS = ZoneState(powered=True, source="NET")


def _heos_player(provider: MagicMock, player_id: str, name: str) -> HeosPlayer:
    """Build a HeosPlayer backed by a mocked device."""
    device = MagicMock()
    device.player_id = int(player_id)
    device.name = name
    device.heos = MagicMock()
    device.heos.set_group = AsyncMock()
    device.heos.remove_group = AsyncMock()
    device.group_id = None
    device.state = HeosPlayState.PLAY
    device.play_url = AsyncMock()
    return HeosPlayer(provider, device)


def _setup() -> tuple[HeosPlayer, HeosPlayer, AvrZones, dict[str, HeosAvrZonePlayer], MagicMock]:
    """Build a receiver with two zone players plus a Kitchen speaker, all backed by mocks."""
    provider = MagicMock()
    provider._heos_queue = MagicMock()
    provider.instance_id = "heos"
    receiver = _heos_player(provider, AVR_ID, "Receiver")
    kitchen = _heos_player(provider, KITCHEN_ID, "Kitchen")

    client = MagicMock()
    client.set_power = AsyncMock()
    client.get_status = AsyncMock(return_value={})
    zones = AvrZones(client, {"zone1": "Living", "zone2": "Dining"})
    zones.available = True
    zone_players = {
        zone: HeosAvrZonePlayer(provider, receiver, zones, zone) for zone in ("zone1", "zone2")
    }
    by_id = {zp.player_id: zp for zp in zone_players.values()}
    provider.get_zone_player.side_effect = by_id.get
    receiver.attach_zones(zones, zone_players)
    return receiver, kitchen, zones, zone_players, client


def _heos_group(leader: HeosPlayer, members: list[HeosPlayer]) -> None:
    """Put players in a HEOS group as the controller would report it."""
    leader._heos_group_members = [m.player_id for m in members]
    for member in members:
        member._device.group_id = int(leader.player_id)


def test_attach_zones_moves_volume_to_zones() -> None:
    """The receiver's HEOS player gives up volume control to its zone players."""
    receiver, _, _, zone_players, _ = _setup()

    assert PlayerFeature.VOLUME_SET not in receiver.supported_features
    assert receiver.volume_level is None
    for feature in (
        PlayerFeature.VOLUME_SET,
        PlayerFeature.POWER,
        PlayerFeature.PLAY_MEDIA,
        PlayerFeature.SET_MEMBERS,
    ):
        assert feature in zone_players["zone2"].supported_features


def test_no_group_without_listening_zones() -> None:
    """A receiver with at most one zone on HEOS forms no group by itself."""
    receiver, kitchen, zones, _, _ = _setup()
    assert group_members_by_leader([receiver, kitchen]) == {}

    zones.states["zone1"] = ON_HEOS
    zones.states["zone2"] = ZoneState(powered=True, source="TV")
    assert group_members_by_leader([receiver, kitchen]) == {}


def test_listening_zones_group_lead_zone_first() -> None:
    """Zones on the HEOS input group with each other; the lead zone leads."""
    receiver, kitchen, zones, _, _ = _setup()
    zones.states["zone1"] = ON_HEOS
    zones.states["zone2"] = ON_HEOS

    assert group_members_by_leader([receiver, kitchen]) == {LIVING_ID: [LIVING_ID, DINING_ID]}

    receiver.lead_zone = "zone2"
    assert group_members_by_leader([receiver, kitchen]) == {DINING_ID: [DINING_ID, LIVING_ID]}


def test_zones_stand_in_for_receiver_in_heos_group() -> None:
    """In a HEOS group, the receiver's listening zones replace the receiver itself."""
    receiver, kitchen, zones, _, _ = _setup()
    _heos_group(kitchen, [kitchen, receiver])
    zones.states["zone2"] = ON_HEOS

    assert group_members_by_leader([receiver, kitchen]) == {KITCHEN_ID: [KITCHEN_ID, DINING_ID]}


def test_receiver_leading_heos_group_is_led_by_its_zone() -> None:
    """When the receiver leads in HEOS, its lead zone leads in Music Assistant."""
    receiver, kitchen, zones, _, _ = _setup()
    _heos_group(receiver, [receiver, kitchen])
    zones.states["zone2"] = ON_HEOS

    assert group_members_by_leader([receiver, kitchen]) == {DINING_ID: [DINING_ID, KITCHEN_ID]}


def test_receiver_without_listening_zones_groups_as_itself() -> None:
    """A receiver whose zones are all off still shows up as itself in a HEOS group."""
    receiver, kitchen, _, _, _ = _setup()
    _heos_group(kitchen, [kitchen, receiver])

    assert group_members_by_leader([receiver, kitchen]) == {KITCHEN_ID: [KITCHEN_ID, AVR_ID]}


def test_plain_heos_groups_unchanged() -> None:
    """Without zones, group members are exactly the HEOS group."""
    provider = MagicMock()
    provider._heos_queue = MagicMock()
    kitchen = _heos_player(provider, KITCHEN_ID, "Kitchen")
    library = _heos_player(provider, "1598068076", "Library")
    _heos_group(kitchen, [kitchen, library])

    assert group_members_by_leader([kitchen, library]) == {KITCHEN_ID: [KITCHEN_ID, "1598068076"]}


def test_plan_add_sibling_zone_powers_it_on() -> None:
    """Adding a zone of the same receiver only turns it on."""
    receiver, _, _, zone_players, _ = _setup()
    get_zone = cast("MagicMock", receiver.provider).get_zone_player

    change = plan_member_change(receiver, [DINING_ID], [], get_zone)

    assert change.heos_members is None
    assert change.zones_on == [zone_players["zone2"]]


def test_plan_add_zone_to_other_leader_groups_receiver() -> None:
    """Adding a zone to another speaker's group brings the receiver into the HEOS group."""
    receiver, kitchen, _, zone_players, _ = _setup()
    get_zone = cast("MagicMock", receiver.provider).get_zone_player

    change = plan_member_change(kitchen, [DINING_ID], [], get_zone)

    assert change.heos_members == [KITCHEN_ID, AVR_ID]
    assert change.zones_on == [zone_players["zone2"]]


def test_plan_remove_last_zone_drops_receiver_from_heos_group() -> None:
    """Removing a receiver's last listening zone also takes the receiver out of HEOS."""
    receiver, kitchen, zones, zone_players, _ = _setup()
    get_zone = cast("MagicMock", receiver.provider).get_zone_player
    _heos_group(kitchen, [kitchen, receiver])
    zones.states["zone2"] = ON_HEOS

    change = plan_member_change(kitchen, [], [DINING_ID], get_zone)

    assert change.heos_members == [KITCHEN_ID]
    assert change.zones_off == [zone_players["zone2"]]


def test_plan_remove_one_of_two_zones_keeps_receiver() -> None:
    """The receiver stays in the HEOS group while another of its zones still listens."""
    receiver, kitchen, zones, _, _ = _setup()
    get_zone = cast("MagicMock", receiver.provider).get_zone_player
    _heos_group(kitchen, [kitchen, receiver])
    zones.states["zone1"] = ON_HEOS
    zones.states["zone2"] = ON_HEOS

    change = plan_member_change(kitchen, [], [DINING_ID], get_zone)

    assert change.heos_members is None


async def test_zone_set_members_takes_lead_and_groups_receiver() -> None:
    """A zone leading a group drives the receiver's HEOS group and becomes the lead zone."""
    receiver, _, _, zone_players, client = _setup()

    await zone_players["zone2"].set_members(player_ids_to_add=[KITCHEN_ID])

    assert receiver.lead_zone == "zone2"
    cast("AsyncMock", receiver._heos.set_group).assert_awaited_once_with(
        [int(AVR_ID), int(KITCHEN_ID)]
    )
    client.set_power.assert_not_awaited()


async def test_zone_play_media_turns_zone_on_and_plays_on_receiver() -> None:
    """Playing to a zone turns it on and plays the zone's queue on the receiver's stream."""
    receiver, _, _, zone_players, client = _setup()
    mass = cast("MagicMock", receiver.mass)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/stream/dining")
    media = PlayerMedia(uri="spotify://track", media_type=MediaType.TRACK, title="Song")

    await zone_players["zone2"].play_media(media)

    client.set_power.assert_awaited_once_with("zone2", True)
    mass.streams.resolve_stream_url.assert_awaited_once_with(DINING_ID, media)
    cast("AsyncMock", receiver._device.play_url).assert_awaited_once_with("http://ma/stream/dining")
    assert receiver.active_source == DINING_ID
    assert receiver.lead_zone == "zone2"
