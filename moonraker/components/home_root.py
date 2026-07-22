# Register $HOME as a selectable file_manager root
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper

# Mainsail's Configure page recursively enumerates every registered root to
# build its file tree - one server.files.get_directory call per directory,
# every time the page loads. For a root as broad as $HOME that's ruinous
# (observed directly: 11,000+ calls in 11 seconds, each one running
# file_manager's directory listing synchronously on Moonraker's event loop,
# long enough to trip its "EVENT LOOP BLOCKED" watchdog and drop Mainsail's
# websocket connection). Fixed client-side instead of here - see
# biokalico_extras/mainsail/home-root-throttle.js - since the fix needs to
# generalize to whatever ends up under $HOME, not just what's there today,
# and a rate limit on the request source is more robust than trying to
# enumerate "big" directories by name on the server.


class HomeRoot:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        root_name = config.get("root_name", "home")
        path = config.get("path", "~")
        full_access = config.getboolean("full_access", False)
        file_manager = self.server.lookup_component("file_manager")
        file_manager.register_directory(root_name, path, full_access)


def load_component(config: ConfigHelper) -> HomeRoot:
    return HomeRoot(config)
