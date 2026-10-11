"""Player for one zone of a HEOS-enabled AV receiver."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player

from .avr import AvrError
from .constants import AVR_ZONE_SPLITTER

if TYPE_CHECKING:
    from .avr import AvrZones
    from .player import HeosPlayer
    from .provider import HeosPlayerProvider


ZONE_PLAYER_FEATURES = {
    PlayerFeature.POWER,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
}


def zone_player_id(heos_player_id: str, zone: str) -> str:
    """
    Return the player_id for a receiver zone.

    :param heos_player_id: player_id of the receiver's HEOS player.
    :param zone: Zone key, e.g. "zone2".
    """
    return f"{heos_player_id}{AVR_ZONE_SPLITTER}{zone}"


class HeosAvrZonePlayer(Player):
    """
    One zone of a HEOS-enabled AV receiver.

    A receiver has a single HEOS stream, played by its HEOS player, that every zone set to
    the HEOS input hears. A zone player therefore plays nothing of its own: it controls the
    zone's power, volume and mute, and takes part in the HEOS player's group while it is
    listening to that stream.
    """

    def __init__(
        self,
        provider: HeosPlayerProvider,
        heos_player: HeosPlayer,
        zones: AvrZones,
        zone: str,
    ) -> None:
        """
        Initialize the zone player.

        :param provider: The HEOS provider.
        :param heos_player: The receiver's HEOS player, which plays the stream for all zones.
        :param zones: Zone tracker of the receiver.
        :param zone: Zone key, e.g. "zone2".
        """
        super().__init__(provider, zone_player_id(heos_player.player_id, zone))
        self.heos_player = heos_player
        self.zones = zones
        self.zone = zone

        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = ZONE_PLAYER_FEATURES
        self._attr_can_group_with = {provider.instance_id}

    async def setup(self) -> None:
        """Set up the player."""
        self.update_attributes()
        await self.mass.players.register_or_update(self)

    def update_attributes(self) -> None:
        """Update all attributes from the receiver's HEOS player and zone state."""
        heos_device_info = self.heos_player.device_info
        device_info = DeviceInfo(
            model=f"{heos_device_info.model} ({self.zone})",
            software_version=heos_device_info.software_version,
            manufacturer=heos_device_info.manufacturer,
        )
        # no ip/mac on zone players: they would be mistaken for (and merged with) the
        # receiver's own AirPlay/DLNA players, which share that identity
        self._attr_device_info = device_info
        zone_name = self.zones.zone_names.get(self.zone, self.zone)
        self._attr_name = f"{self.heos_player.name} {zone_name}"
        self._attr_available = self.zones.available and self.heos_player.available

        state = self.zones.states[self.zone]
        self._attr_powered = state.powered
        self._attr_volume_level = state.volume
        self._attr_volume_muted = state.muted
        if state.plays_heos:
            self._attr_playback_state = self.heos_player.playback_state
            self._attr_current_media = self.heos_player.current_media
            self._attr_elapsed_time = self.heos_player.elapsed_time
            self._attr_elapsed_time_last_updated = self.heos_player.elapsed_time_last_updated
        else:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command on the player.

        :param powered: bool if the zone should be powered on (on the HEOS input) or off.
        """
        await self._run_zone_command(self.zones.set_power(self.zone, powered))

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        :param volume_level: volume level (0..100) to set on the zone.
        """
        await self._run_zone_command(self.zones.set_volume(self.zone, volume_level))

    async def volume_mute(self, muted: bool) -> None:
        """
        Handle VOLUME MUTE command on the player.

        :param muted: bool if the zone should be muted.
        """
        await self._run_zone_command(self.zones.set_mute(self.zone, muted))

    async def _run_zone_command(self, command: Coroutine[Any, Any, None]) -> None:
        try:
            await command
        except AvrError as err:
            raise PlayerCommandFailed(f"Receiver command failed: {err}") from err
