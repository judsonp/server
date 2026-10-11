"""Player for one zone of a HEOS-enabled AV receiver."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player, PlayerMedia

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
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.PLAY_MEDIA,
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
    the HEOS input hears. A zone player has its own power, volume and mute; playback and
    grouping go through the receiver's HEOS player, and every zone that is listening hears
    what any of them plays.
    """

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

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
        self._attr_supported_sample_rates = self.heos_player.supported_sample_rates

        state = self.zones.states[self.zone]
        self._attr_powered = state.powered
        self._attr_volume_level = state.volume
        self._attr_volume_muted = state.muted
        if state.plays_heos and self.zone in self.heos_player.session_zones:
            self._attr_playback_state = self.heos_player.playback_state
            self._attr_current_media = self.heos_player.current_media
            self._attr_active_source = self.heos_player.active_source
            self._attr_elapsed_time = self.heos_player.elapsed_time
            self._attr_elapsed_time_last_updated = self.heos_player.elapsed_time_last_updated
        else:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self._attr_active_source = None
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None

    def set_group_members(self, members: list[str]) -> None:
        """
        Set the group members, as computed by the provider for all HEOS players.

        :param members: Group members (this player first), or an empty list.
        """
        if members != self._attr_group_members:
            self._attr_group_members = members
            self.update_state()

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command on the player.

        :param powered: bool if the zone should be powered on (on the HEOS input) or off.
        """
        session = [zone for zone in self.heos_player.session_zones if zone != self.zone]
        if powered:
            # the only thing a zone on the HEOS input can play is the receiver's stream
            session.append(self.zone)
        await self._run_zone_command(self.heos_player.set_session_zones(session))

    async def set_zone_power(self, powered: bool) -> None:
        """
        Switch the zone on (on the HEOS input) or off, without changing the session.

        :param powered: bool if the zone should be powered on or off.
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

    async def play_media(self, media: PlayerMedia) -> None:
        """
        Handle PLAY MEDIA command on the player.

        :param media: The media to play on the receiver's stream.
        """
        if len(self.group_members) > 1 and self.group_members[0] == self.player_id:
            # playing to a group's leader plays to the whole group
            session = [self.zone, *(z for z in self.heos_player.session_zones if z != self.zone)]
        else:
            # playing to a zone on its own takes the stream away from the other zones
            session = [self.zone]
        await self._run_zone_command(self.heos_player.set_session_zones(session))
        await self.heos_player.play_media_for(self.player_id, media)

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self.heos_player.play()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self.heos_player.pause()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        await self.heos_player.stop()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on the player."""
        await self.heos_player.next_track()

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on the player."""
        await self.heos_player.previous_track()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle SET MEMBERS command on the player.

        :param player_ids_to_add: Player ids (HEOS players or zones) to add to the group.
        :param player_ids_to_remove: Player ids (HEOS players or zones) to remove.
        """
        if player_ids_to_add is None and player_ids_to_remove is None:
            return
        # the zone leading a group is in the session and listening, ahead of the others
        session = [self.zone, *(z for z in self.heos_player.session_zones if z != self.zone)]
        await self._run_zone_command(self.heos_player.set_session_zones(session))
        await self.heos_player.change_members(player_ids_to_add or [], player_ids_to_remove or [])

    async def _run_zone_command(self, command: Coroutine[Any, Any, None]) -> None:
        try:
            await command
        except AvrError as err:
            raise PlayerCommandFailed(f"Receiver command failed: {err}") from err
