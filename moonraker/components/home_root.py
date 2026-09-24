# Register $HOME as a selectable file_manager root
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper

# Listing all of $HOME at once blocks Moonraker long enough to drop
# Mainsail's connection. Mainsail only loads the roots in
# eagerlyExpandedRoots (deps/mainsail/src/store/variables.ts) up front and
# loads this one a directory at a time as the user browses.


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
