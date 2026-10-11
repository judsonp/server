"""Tests for HEOS AV receiver zone control."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.heos.avr import (
    AvrError,
    AvrZones,
    ZoneState,
    parse_status,
    parse_zone_names,
    zone_command_prefixes,
)

# responses captured from an AVR-S930H (zone2 mute flipped to "on" to cover both states)
STATUS_RESPONSE = """<?xml version="1.0" encoding="utf-8" ?>
<rx>
<cmd>
<zone1>ON</zone1>
<zone2>OFF</zone2>
</cmd>
<cmd>
<zone1>
<volume>-55.0</volume>
<state>variable</state>
<limit>OFF</limit>
<disptype>ABSOLUTE</disptype>
<dispvalue>25.0</dispvalue>
</zone1>
<zone2>
<volume>-50</volume>
<state>variable</state>
<limit>-10.0</limit>
<disptype>ABSOLUTE</disptype>
<dispvalue> 30</dispvalue>
</zone2>
</cmd>
<cmd>
<zone1>
<source>NET</source>
</zone1>
<zone2>
<source>NET</source>
</zone2>
</cmd>
<cmd>
<zone1>off</zone1>
<zone2>on</zone2>
</cmd>
</rx>
"""
ZONE_NAME_RESPONSE = (
    '<?xml version="1.0" encoding="utf-8" ?><rx><cmd>'
    "<zone1>Living    </zone1><zone2>Dining    </zone2></cmd></rx>"
)


def _zones(states: dict[str, ZoneState] | None = None) -> tuple[AvrZones, MagicMock]:
    """Build an AvrZones tracker backed by a mocked client."""
    client = MagicMock()
    client.get_status = AsyncMock(return_value=states or {})
    client.set_power = AsyncMock()
    client.set_volume = AsyncMock()
    client.set_mute = AsyncMock()
    zones = AvrZones(client, {"zone1": "Living", "zone2": "Dining"})
    return zones, client


def test_parse_status() -> None:
    """Parse power, absolute volume, volume limit, source and mute per zone."""
    status = parse_status(STATUS_RESPONSE)

    assert status["zone1"] == ZoneState(
        powered=True, source="NET", volume=25, volume_limit=None, muted=False
    )
    assert status["zone2"] == ZoneState(
        powered=False, source="NET", volume=30, volume_limit=70, muted=True
    )


def test_parse_status_rejects_incomplete_response() -> None:
    """Raise AvrError for a response that does not answer all four queries."""
    with pytest.raises(AvrError):
        parse_status('<?xml version="1.0" encoding="utf-8" ?><rx></rx>')
    with pytest.raises(AvrError):
        parse_status("Error 403: Forbidden")


def test_parse_zone_names_strips_padding() -> None:
    """Zone names come padded with spaces."""
    assert parse_zone_names(ZONE_NAME_RESPONSE) == {"zone1": "Living", "zone2": "Dining"}


def test_zone_command_prefixes() -> None:
    """The main zone uses its own command set, other zones are numbered."""
    assert zone_command_prefixes("zone1") == ("ZM", "SI", "MV", "MU")
    assert zone_command_prefixes("zone2") == ("Z2", "Z2", "Z2", "Z2MU")
    assert zone_command_prefixes("zone3") == ("Z3", "Z3", "Z3", "Z3MU")


def test_plays_heos_requires_power_and_net_input() -> None:
    """A zone only hears the HEOS stream while on and set to the NET input."""
    assert ZoneState(powered=True, source="NET").plays_heos
    assert not ZoneState(powered=False, source="NET").plays_heos
    assert not ZoneState(powered=True, source="TV").plays_heos


async def test_set_volume_respects_zone_limit() -> None:
    """Volume requests above a zone's configured limit are capped at the limit."""
    zones, client = _zones()
    zones.states["zone2"] = ZoneState(powered=True, source="NET", volume=30, volume_limit=70)

    await zones.set_volume("zone2", 90)

    client.set_volume.assert_awaited_once_with("zone2", 70)


async def test_refresh_notifies_only_on_change() -> None:
    """Listeners fire when the state changes, not on every poll."""
    zones, client = _zones()
    client.get_status.return_value = {"zone1": ZoneState(powered=True, source="NET")}
    listener = MagicMock()
    zones.add_listener(listener)

    await zones.refresh()
    await zones.refresh()
    assert listener.call_count == 1
    assert zones.zones_playing_heos() == ["zone1"]

    client.get_status.return_value = {"zone2": ZoneState(powered=True, source="NET")}
    await zones.refresh()
    assert listener.call_count == 2
    assert zones.zones_playing_heos() == ["zone2"]


async def test_refresh_marks_unavailable_on_error() -> None:
    """A failing receiver marks the zones unavailable and notifies once."""
    zones, client = _zones()
    await zones.refresh()
    listener = MagicMock()
    zones.add_listener(listener)
    client.get_status.side_effect = AvrError("unreachable")

    await zones.refresh()
    await zones.refresh()

    assert not zones.available
    assert listener.call_count == 1
