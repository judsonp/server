"""
Zone control for HEOS-enabled Denon/Marantz AV receivers.

HEOS treats a multi-zone receiver as a single player and exposes nothing about its zones.
The receiver's own HTTP control interface does, so zone power, input, volume and mute are
handled here while the audio itself keeps flowing through the receiver's HEOS player.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

if TYPE_CHECKING:
    from aiohttp import ClientSession

AVR_HTTP_PORTS = (8080, 80)
AVR_REQUEST_TIMEOUT = ClientTimeout(total=5)
AVR_PROBE_TIMEOUT = ClientTimeout(total=3)

# the receiver's absolute volume scale runs 0..98, where 80 is 0 dB (the same scale HEOS uses)
AVR_VOLUME_MAX = 98
AVR_VOLUME_ZERO_DB = 80

AVR_NET_SOURCE = "NET"

STATUS_QUERY = (
    '<?xml version="1.0" encoding="utf-8"?>\n<tx>\n'
    '<cmd id="1">GetAllZonePowerStatus</cmd>\n'
    '<cmd id="1">GetAllZoneVolume</cmd>\n'
    '<cmd id="1">GetAllZoneSource</cmd>\n'
    '<cmd id="1">GetAllZoneMuteStatus</cmd>\n'
    "</tx>\n"
)
ZONE_NAME_QUERY = (
    '<?xml version="1.0" encoding="utf-8"?>\n<tx>\n<cmd id="1">GetZoneName</cmd>\n</tx>\n'
)


class AvrError(Exception):
    """Raised when the receiver's control interface fails or returns something unexpected."""


@dataclass
class ZoneState:
    """Current state of one receiver zone."""

    powered: bool = False
    source: str | None = None
    volume: int | None = None
    volume_limit: int | None = None
    muted: bool = False

    @property
    def plays_heos(self) -> bool:
        """Return if the zone is on and listening to the receiver's HEOS stream."""
        return self.powered and self.source == AVR_NET_SOURCE


def zone_command_prefixes(zone: str) -> tuple[str, str, str, str]:
    """
    Return the (power, source, volume, mute) command prefixes for a zone.

    :param zone: Zone key as reported by the receiver, e.g. "zone1" or "zone2".
    """
    if zone == "zone1":
        return ("ZM", "SI", "MV", "MU")
    number = zone.removeprefix("zone")
    return (f"Z{number}", f"Z{number}", f"Z{number}", f"Z{number}MU")


def parse_volume(volume: str | None, dispvalue: str | None) -> int | None:
    """
    Return the absolute (0..98) volume from a GetAllZoneVolume zone element.

    :param volume: The relative dB value, e.g. "-55.0".
    :param dispvalue: The absolute display value, e.g. "25.0".
    """
    for value, offset in ((dispvalue, 0.0), (volume, AVR_VOLUME_ZERO_DB)):
        if value is None:
            continue
        try:
            return round(float(value.strip()) + offset)
        except ValueError:
            continue
    return None


def parse_volume_limit(limit: str | None) -> int | None:
    """
    Return the absolute volume limit from a zone's dB limit, or None when there is none.

    :param limit: The receiver's limit value, e.g. "-10.0" or "OFF".
    """
    if limit is None:
        return None
    try:
        return round(float(limit.strip()) + AVR_VOLUME_ZERO_DB)
    except ValueError:
        return None


def parse_status(xml_text: str) -> dict[str, ZoneState]:
    """
    Parse the response to STATUS_QUERY into a state per zone.

    :param xml_text: The receiver's AppCommand.xml response body.
    """
    try:
        root = DefusedET.fromstring(xml_text)
    except (DefusedET.ParseError, DefusedXmlException) as err:
        raise AvrError(f"Invalid status response: {err}") from err
    commands = root.findall("cmd")
    if len(commands) != 4:
        raise AvrError(f"Expected 4 status results, got {len(commands)}")
    power, volume, source, mute = commands

    zones: dict[str, ZoneState] = {}
    for element in power:
        zones[element.tag] = ZoneState(powered=(element.text or "").strip().upper() == "ON")
    for element in volume:
        if state := zones.get(element.tag):
            state.volume = parse_volume(element.findtext("volume"), element.findtext("dispvalue"))
            state.volume_limit = parse_volume_limit(element.findtext("limit"))
    for element in source:
        if state := zones.get(element.tag):
            state.source = (element.findtext("source") or "").strip() or None
    for element in mute:
        if state := zones.get(element.tag):
            state.muted = (element.text or "").strip().lower() == "on"
    return zones


def parse_zone_names(xml_text: str) -> dict[str, str]:
    """
    Parse the response to ZONE_NAME_QUERY into a name per zone.

    :param xml_text: The receiver's AppCommand.xml response body.
    """
    try:
        root = DefusedET.fromstring(xml_text)
    except (DefusedET.ParseError, DefusedXmlException) as err:
        raise AvrError(f"Invalid zone name response: {err}") from err
    names: dict[str, str] = {}
    if (command := root.find("cmd")) is not None:
        for element in command:
            if name := (element.text or "").strip():
                names[element.tag] = name
    return names


class AvrClient:
    """Minimal client for the HTTP control interface of a Denon/Marantz receiver."""

    def __init__(self, session: ClientSession, host: str, port: int) -> None:
        """
        Initialize the client.

        :param session: Shared aiohttp session.
        :param host: Hostname or IP address of the receiver.
        :param port: HTTP port of the control interface (8080 on 2016+ models, else 80).
        """
        self._session = session
        self.host = host
        self.port = port

    @classmethod
    async def probe(cls, session: ClientSession, host: str) -> AvrClient | None:
        """
        Return a client for the receiver at host, or None if it has no zone control interface.

        :param session: Shared aiohttp session.
        :param host: Hostname or IP address of the HEOS device.
        """
        for port in AVR_HTTP_PORTS:
            client = cls(session, host, port)
            try:
                names = await client.get_zone_names(timeout=AVR_PROBE_TIMEOUT)
            except AvrError:
                continue
            if names:
                return client
        return None

    async def get_status(self) -> dict[str, ZoneState]:
        """Return the current state of every zone."""
        return parse_status(await self._app_command(STATUS_QUERY))

    async def get_zone_names(self, timeout: ClientTimeout = AVR_REQUEST_TIMEOUT) -> dict[str, str]:
        """
        Return the user-assigned name of every zone.

        :param timeout: Request timeout.
        """
        return parse_zone_names(await self._app_command(ZONE_NAME_QUERY, timeout))

    async def set_power(self, zone: str, powered: bool) -> None:
        """
        Turn a zone on or off. Turning it on also selects the HEOS (NET) input.

        :param zone: Zone key, e.g. "zone2".
        :param powered: True to turn the zone on.
        """
        power, source, _, _ = zone_command_prefixes(zone)
        if not powered:
            await self._direct_command(f"{power}OFF")
            return
        await self._direct_command(f"{power}ON")
        await self._direct_command(f"{source}{AVR_NET_SOURCE}")

    async def set_volume(self, zone: str, volume: int) -> None:
        """
        Set a zone's absolute volume (0..98).

        :param zone: Zone key, e.g. "zone2".
        :param volume: Absolute volume.
        """
        _, _, prefix, _ = zone_command_prefixes(zone)
        volume = max(0, min(AVR_VOLUME_MAX, volume))
        await self._direct_command(f"{prefix}{volume:02d}")

    async def set_mute(self, zone: str, muted: bool) -> None:
        """
        Mute or unmute a zone.

        :param zone: Zone key, e.g. "zone2".
        :param muted: True to mute.
        """
        _, _, _, prefix = zone_command_prefixes(zone)
        await self._direct_command(f"{prefix}{'ON' if muted else 'OFF'}")

    async def _app_command(self, body: str, timeout: ClientTimeout = AVR_REQUEST_TIMEOUT) -> str:
        url = f"http://{self.host}:{self.port}/goform/AppCommand.xml"
        try:
            async with self._session.post(
                url, data=body, headers={"Content-Type": "text/xml"}, timeout=timeout
            ) as response:
                if response.status != 200:
                    raise AvrError(f"{url} returned HTTP {response.status}")
                return await response.text()
        except (ClientError, TimeoutError) as err:
            raise AvrError(f"Request to {url} failed: {err}") from err

    async def _direct_command(self, command: str) -> None:
        url = f"http://{self.host}:{self.port}/goform/formiPhoneAppDirect.xml?{command}"
        try:
            async with self._session.get(url, timeout=AVR_REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise AvrError(f"Command {command} returned HTTP {response.status}")
        except (ClientError, TimeoutError) as err:
            raise AvrError(f"Command {command} failed: {err}") from err


class AvrZones:
    """Tracks the zones of one receiver and keeps their state current by polling."""

    def __init__(self, client: AvrClient, zone_names: dict[str, str]) -> None:
        """
        Initialize the zone tracker.

        :param client: Client for the receiver's control interface.
        :param zone_names: Name per zone key, which also defines the zones to track.
        """
        self.client = client
        self.zone_names = zone_names
        self.states: dict[str, ZoneState] = {zone: ZoneState() for zone in zone_names}
        self.available = False
        self._listeners: list[Callable[[], None]] = []
        self._refresh_lock = asyncio.Lock()

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """
        Register a callback for state changes and return a function that removes it.

        :param callback: Called after every refresh that changed something.
        """
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def zones_playing_heos(self) -> list[str]:
        """Return the zones that are on and listening to the HEOS stream."""
        return [zone for zone, state in self.states.items() if state.plays_heos]

    async def refresh(self) -> None:
        """Fetch the current state of all zones and notify listeners about changes."""
        async with self._refresh_lock:
            try:
                status = await self.client.get_status()
            except AvrError:
                changed = self.available
                self.available = False
            else:
                new_states = {zone: status.get(zone, ZoneState()) for zone in self.zone_names}
                changed = not self.available or new_states != self.states
                self.states = new_states
                self.available = True
        if changed:
            for callback in list(self._listeners):
                callback()

    async def set_power(self, zone: str, powered: bool) -> None:
        """
        Turn a zone on (on the HEOS input) or off.

        :param zone: Zone key, e.g. "zone2".
        :param powered: True to turn the zone on.
        """
        await self.client.set_power(zone, powered)
        await self.refresh()

    async def set_volume(self, zone: str, volume: int) -> None:
        """
        Set a zone's absolute volume, capped at the zone's configured volume limit.

        :param zone: Zone key, e.g. "zone2".
        :param volume: Absolute volume (0..98).
        """
        if (limit := self.states[zone].volume_limit) is not None:
            volume = min(volume, limit)
        await self.client.set_volume(zone, volume)
        await self.refresh()

    async def set_mute(self, zone: str, muted: bool) -> None:
        """
        Mute or unmute a zone.

        :param zone: Zone key, e.g. "zone2".
        :param muted: True to mute.
        """
        await self.client.set_mute(zone, muted)
        await self.refresh()
