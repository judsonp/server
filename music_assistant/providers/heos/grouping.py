"""
Group membership for HEOS players and the zones of HEOS-enabled AV receivers.

HEOS groups devices; a receiver is one HEOS device no matter how many of its zones are on.
In Music Assistant the zones stand in for their receiver: whenever any zone is listening
to the receiver's HEOS stream, those zone players take the receiver's place in its group.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .player import HeosPlayer
    from .zone_player import HeosAvrZonePlayer


@dataclass
class MemberChange:
    """What to change to apply a set_members command."""

    # the new HEOS group (leader first), or None if the HEOS group stays as it is
    heos_members: list[str] | None = None
    zones_on: list[HeosAvrZonePlayer] = field(default_factory=list)
    zones_off: list[HeosAvrZonePlayer] = field(default_factory=list)


def representatives(heos_player: HeosPlayer) -> list[str]:
    """
    Return the ids of the players that stand for a HEOS device in a group.

    :param heos_player: The HEOS player of the device.
    """
    if active_zones := heos_player.active_zone_player_ids():
        return active_zones
    return [heos_player.player_id]


def group_members_by_leader(heos_players: Iterable[HeosPlayer]) -> dict[str, list[str]]:
    """
    Return the group members (leader first) of every player that leads a group.

    :param heos_players: All HEOS players of the provider.
    """
    players = {player.player_id: player for player in heos_players}
    groups: dict[str, list[str]] = {}
    for player in players.values():
        if player.heos_group_members:
            members = [
                member_id
                for heos_id in player.heos_group_members
                for member_id in (
                    representatives(players[heos_id]) if heos_id in players else [heos_id]
                )
            ]
        elif not player.in_heos_group and player.avr_zones is not None:
            # a receiver on its own still groups its zones with each other
            members = representatives(player)
        else:
            continue
        if len(members) > 1:
            groups[members[0]] = members
    return groups


def plan_member_change(
    leader: HeosPlayer,
    player_ids_to_add: list[str],
    player_ids_to_remove: list[str],
    get_zone_player: Callable[[str], HeosAvrZonePlayer | None],
) -> MemberChange:
    """
    Translate a set_members command into HEOS group and zone power changes.

    :param leader: HEOS player of the group leader (the receiver when a zone leads).
    :param player_ids_to_add: Player ids (HEOS or zone players) to add to the group.
    :param player_ids_to_remove: Player ids (HEOS or zone players) to remove from the group.
    :param get_zone_player: Returns the zone player for a player id, or None if it isn't one.
    """
    change = MemberChange()
    heos_to_add: list[str] = []
    heos_to_remove: list[str] = []

    for player_id in player_ids_to_add:
        heos_id = player_id
        if zone_player := get_zone_player(player_id):
            change.zones_on.append(zone_player)
            # a zone only hears its own receiver's stream, so the receiver joins too
            heos_id = zone_player.heos_player.player_id
        if heos_id not in heos_to_add:
            heos_to_add.append(heos_id)

    removed_zone_ids: dict[str, set[str]] = {}
    for player_id in player_ids_to_remove:
        if zone_player := get_zone_player(player_id):
            change.zones_off.append(zone_player)
            receiver_id = zone_player.heos_player.player_id
            removed_zone_ids.setdefault(receiver_id, set()).add(player_id)
            continue
        heos_to_remove.append(player_id)
    # another receiver leaves the HEOS group once none of its zones are left in it
    for receiver_id, zone_ids in removed_zone_ids.items():
        receiver = next(z.heos_player for z in change.zones_off if z.player_id in zone_ids)
        if receiver is not leader and set(receiver.active_zone_player_ids()) <= zone_ids:
            heos_to_remove.append(receiver_id)

    current = list(leader.heos_group_members) or [leader.player_id]
    members = list(current)
    for player_id in heos_to_add:
        if player_id not in members:
            members.append(player_id)
    for player_id in heos_to_remove:
        if player_id in members and player_id != leader.player_id:
            members.remove(player_id)
    if members != current:
        change.heos_members = members
    return change
