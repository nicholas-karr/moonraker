# Periodically expire stale entries from the presence heartbeat namespace
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# Mainsail's multi-user presence warning (see
# deps/mainsail/src/store/presence) heartbeats one key per browser profile
# into Moonraker's generic database under the `biokalico_presence`
# namespace, keyed by a UUID that persists in that browser's localStorage
# forever. Nothing ever deletes those keys client-side, so left alone the
# namespace only grows - every incognito window or cleared-profile visit
# mints a new UUID that would otherwise live in the database permanently.
# This component wakes up periodically and drops any entry whose heartbeat
# is older than max_age_days.

from __future__ import annotations

import time
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .database import MoonrakerDatabase

CHECK_INTERVAL = 3600.


class PresencePrune:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.namespace = config.get("namespace", "biokalico_presence")
        max_age_days = config.getfloat("max_age_days", 3., above=0.)
        self.max_age_secs = max_age_days * 86400.

        eventloop = self.server.get_event_loop()
        self.prune_timer = eventloop.register_timer(self._prune_handler)

    async def component_init(self) -> None:
        self.prune_timer.start(delay=CHECK_INTERVAL)

    async def _prune_handler(self, eventtime: float) -> float:
        db: MoonrakerDatabase = self.server.lookup_component("database")
        try:
            items = await db.ns_items(self.namespace)
        except self.server.error:
            # Namespace doesn't exist until the first heartbeat is posted
            return eventtime + CHECK_INTERVAL
        cutoff = time.time() - self.max_age_secs
        stale: List[str] = [
            key for key, last_active in items
            if not isinstance(last_active, (int, float)) or last_active < cutoff
        ]
        if stale:
            await db.delete_batch(self.namespace, stale)
        return eventtime + CHECK_INTERVAL


def load_component(config: ConfigHelper) -> PresencePrune:
    return PresencePrune(config)
