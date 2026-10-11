"""Tests for HEOS AV receiver zone players and their grouping."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature
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


class FakeAvrClient:
    """Receiver control client that remembers what it was told, like the real receiver."""

    def __init__(self) -> None:
        """Start with both zones off."""
        self.states = {"zone1": ZoneState(), "zone2": ZoneState()}
        self.power_calls: list[tuple[str, bool]] = []

    async def get_status(self) -> dict[str, ZoneState]:
        """Return the current zone states."""
        return {zone: replace(state) for zone, state in self.states.items()}

    async def set_power(self, zone: str, powered: bool) -> None:
        """Switch a zone, selecting the HEOS input when turning it on."""
        self.power_calls.append((zone, powered))
        self.states[zone].powered = powered
        if powered:
            self.states[zone].source = "NET"


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
    device.stop = AsyncMock()
    return HeosPlayer(provider, device)


def _setup() -> tuple[
    HeosPlayer, HeosPlayer, AvrZones, dict[str, HeosAvrZonePlayer], FakeAvrClient
]:
    """Build a receiver with two zone players plus a Kitchen speaker, all backed by fakes."""
    provider = MagicMock()
    provider._heos_queue = MagicMock()
    provider.instance_id = "heos"
    receiver = _heos_player(provider, AVR_ID, "Receiver")
    kitchen = _heos_player(provider, KITCHEN_ID, "Kitchen")

    client = FakeAvrClient()
    zones = AvrZones(client, {"zone1": "Living", "zone2": "Dining"})  # type: ignore[arg-type]
    zones.available = True
    zone_players = {
        zone: HeosAvrZonePlayer(provider, receiver, zones, zone) for zone in ("zone1", "zone2")
    }
    by_id = {zp.player_id: zp for zp in zone_players.values()}
    provider.get_zone_player.side_effect = by_id.get

    def update_group_members() -> None:
        # what the provider does whenever the session changes
        groups = group_members_by_leader([receiver, kitchen])
        for player in (receiver, kitchen):
            player.set_group_members(groups.get(player.player_id, []))
        for zone_player in zone_players.values():
            zone_player.set_group_members(groups.get(zone_player.player_id, []))

    provider.update_group_members.side_effect = update_group_members
    receiver.attach_zones(zones, zone_players)
    return receiver, kitchen, zones, zone_players, client


async def _listen(zones: AvrZones, client: FakeAvrClient, *zone_keys: str) -> None:
    """Have zones on the HEOS input, as if switched there with the remote."""
    for zone in zone_keys:
        client.states[zone] = replace(ON_HEOS)
    await zones.refresh()
    client.power_calls.clear()


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


async def test_session_follows_zones_switched_elsewhere() -> None:
    """A zone switched to HEOS outside MA joins the session; one turned off leaves it."""
    receiver, _, zones, _, client = _setup()

    await _listen(zones, client, "zone2")
    receiver.sync_session_zones()
    assert receiver.session_zones == ["zone2"]

    await _listen(zones, client, "zone1")
    receiver.sync_session_zones()
    assert receiver.session_zones == ["zone2", "zone1"]

    client.states["zone2"].powered = False
    await zones.refresh()
    receiver.sync_session_zones()
    assert receiver.session_zones == ["zone1"]


async def test_play_to_zone_alone_turns_other_zone_off() -> None:
    """Playing to one zone takes the stream away from the other zone."""
    receiver, _, zones, zone_players, client = _setup()
    await _listen(zones, client, "zone1")
    receiver.sync_session_zones()
    mass = cast("MagicMock", receiver.mass)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/stream/dining")
    media = PlayerMedia(uri="spotify://track", media_type=MediaType.TRACK, title="Song")

    await zone_players["zone2"].play_media(media)

    assert client.power_calls == [("zone1", False), ("zone2", True)]
    assert receiver.session_zones == ["zone2"]
    mass.streams.resolve_stream_url.assert_awaited_once_with(DINING_ID, media)
    cast("AsyncMock", receiver._device.play_url).assert_awaited_once_with("http://ma/stream/dining")
    assert receiver.active_source == DINING_ID
    assert zone_players["zone1"].playback_state == PlaybackState.IDLE


async def test_play_to_zone_leaves_zone_on_other_input_alone() -> None:
    """A zone watching TV is not switched off when the other zone plays."""
    receiver, _, zones, zone_players, client = _setup()
    client.states["zone1"] = ZoneState(powered=True, source="TV")
    await zones.refresh()
    cast("MagicMock", receiver.mass).streams.resolve_stream_url = AsyncMock(return_value="u")

    await zone_players["zone2"].play_media(PlayerMedia(uri="x", media_type=MediaType.TRACK))

    assert client.power_calls == [("zone2", True)]
    assert client.states["zone1"].powered


async def test_play_to_group_leader_keeps_group() -> None:
    """Playing to the zone leading a zone group plays to both zones."""
    receiver, _, zones, zone_players, client = _setup()
    await _listen(zones, client, "zone1", "zone2")
    receiver.sync_session_zones()
    receiver.update_zone_players()
    cast("MagicMock", receiver.provider).update_group_members()
    assert zone_players["zone1"].group_members == [LIVING_ID, DINING_ID]
    cast("MagicMock", receiver.mass).streams.resolve_stream_url = AsyncMock(return_value="u")

    await zone_players["zone1"].play_media(PlayerMedia(uri="x", media_type=MediaType.TRACK))

    assert client.power_calls == []
    assert receiver.session_zones == ["zone1", "zone2"]


async def test_power_on_zone_joins_session() -> None:
    """Turning a zone on puts it on the HEOS input, hearing (and grouped with) the stream."""
    receiver, _, zones, zone_players, client = _setup()
    await _listen(zones, client, "zone1")
    receiver.sync_session_zones()

    await zone_players["zone2"].power(True)

    assert client.power_calls == [("zone2", True)]
    assert receiver.session_zones == ["zone1", "zone2"]
    assert zone_players["zone1"].group_members == [LIVING_ID, DINING_ID]


async def test_power_off_last_zone_stops_stream() -> None:
    """With no zone left to hear it, the receiver's stream stops."""
    receiver, _, zones, zone_players, client = _setup()
    await _listen(zones, client, "zone2")
    receiver.sync_session_zones()
    receiver._attr_playback_state = PlaybackState.PLAYING

    await zone_players["zone2"].power(False)

    assert client.power_calls == [("zone2", False)]
    assert receiver.session_zones == []
    cast("AsyncMock", receiver._device.stop).assert_awaited_once()


def test_no_group_with_one_zone_in_session() -> None:
    """A receiver with a single zone in its session forms no group by itself."""
    receiver, kitchen, zones, _, _ = _setup()
    zones.states["zone1"] = replace(ON_HEOS)
    receiver.session_zones = ["zone1"]

    assert group_members_by_leader([receiver, kitchen]) == {}


def test_zones_in_session_group_leading_zone_first() -> None:
    """Zones in the session group with each other, led by the first one."""
    receiver, kitchen, zones, _, _ = _setup()
    zones.states["zone1"] = replace(ON_HEOS)
    zones.states["zone2"] = replace(ON_HEOS)

    receiver.session_zones = ["zone1", "zone2"]
    assert group_members_by_leader([receiver, kitchen]) == {LIVING_ID: [LIVING_ID, DINING_ID]}

    receiver.session_zones = ["zone2", "zone1"]
    assert group_members_by_leader([receiver, kitchen]) == {DINING_ID: [DINING_ID, LIVING_ID]}


def test_zones_stand_in_for_receiver_in_heos_group() -> None:
    """In a HEOS group, the receiver's session zones replace the receiver itself."""
    receiver, kitchen, zones, _, _ = _setup()
    _heos_group(kitchen, [kitchen, receiver])
    zones.states["zone2"] = replace(ON_HEOS)
    receiver.session_zones = ["zone2"]

    assert group_members_by_leader([receiver, kitchen]) == {KITCHEN_ID: [KITCHEN_ID, DINING_ID]}


def test_receiver_leading_heos_group_is_led_by_its_zone() -> None:
    """When the receiver leads in HEOS, its leading zone leads in Music Assistant."""
    receiver, kitchen, zones, _, _ = _setup()
    _heos_group(receiver, [receiver, kitchen])
    zones.states["zone2"] = replace(ON_HEOS)
    receiver.session_zones = ["zone2"]

    assert group_members_by_leader([receiver, kitchen]) == {DINING_ID: [DINING_ID, KITCHEN_ID]}


def test_receiver_without_session_zones_groups_as_itself() -> None:
    """A receiver with no zone in its session still shows up as itself in a HEOS group."""
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
    """Removing a receiver's last zone also takes the receiver out of HEOS."""
    receiver, kitchen, zones, zone_players, _ = _setup()
    get_zone = cast("MagicMock", receiver.provider).get_zone_player
    _heos_group(kitchen, [kitchen, receiver])
    zones.states["zone2"] = replace(ON_HEOS)
    receiver.session_zones = ["zone2"]

    change = plan_member_change(kitchen, [], [DINING_ID], get_zone)

    assert change.heos_members == [KITCHEN_ID]
    assert change.zones_off == [zone_players["zone2"]]


def test_plan_remove_one_of_two_zones_keeps_receiver() -> None:
    """The receiver stays in the HEOS group while another of its zones is still in it."""
    receiver, kitchen, zones, _, _ = _setup()
    get_zone = cast("MagicMock", receiver.provider).get_zone_player
    _heos_group(kitchen, [kitchen, receiver])
    zones.states["zone1"] = replace(ON_HEOS)
    zones.states["zone2"] = replace(ON_HEOS)
    receiver.session_zones = ["zone1", "zone2"]

    change = plan_member_change(kitchen, [], [DINING_ID], get_zone)

    assert change.heos_members is None


async def test_zone_set_members_leads_and_groups_receiver() -> None:
    """A zone leading a group turns on, leads the session and drives the HEOS group."""
    receiver, _, _, zone_players, client = _setup()

    await zone_players["zone2"].set_members(player_ids_to_add=[KITCHEN_ID])

    assert client.power_calls == [("zone2", True)]
    assert receiver.session_zones == ["zone2"]
    cast("AsyncMock", receiver._heos.set_group).assert_awaited_once_with(
        [int(AVR_ID), int(KITCHEN_ID)]
    )


async def test_zone_set_members_adds_sibling_zone() -> None:
    """Grouping the two zones (e.g. a sync group) has both of them listening."""
    receiver, _, zones, zone_players, client = _setup()
    await _listen(zones, client, "zone1")
    receiver.sync_session_zones()

    await zone_players["zone1"].set_members(player_ids_to_add=[DINING_ID])

    assert client.power_calls == [("zone2", True)]
    assert receiver.session_zones == ["zone1", "zone2"]
    assert zone_players["zone1"].group_members == [LIVING_ID, DINING_ID]
    cast("AsyncMock", receiver._heos.set_group).assert_not_awaited()
