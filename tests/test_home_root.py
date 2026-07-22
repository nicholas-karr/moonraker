from __future__ import annotations
import os
import pathlib
import pytest
from moonraker.server import Server
from moonraker.components.file_manager.file_manager import FileManager

from typing import Dict


@pytest.mark.run_paths(moonraker_conf="biokalico_components.conf")
class TestHomeRoot:
    def test_root_registered(self,
                             full_server: Server,
                             path_args: Dict[str, pathlib.Path]):
        fm: FileManager = full_server.lookup_component("file_manager")
        expected = os.path.abspath(os.path.expanduser(str(path_args["temp_path"])))
        assert fm.file_paths.get("home") == expected

    def test_root_not_full_access(self, full_server: Server):
        fm: FileManager = full_server.lookup_component("file_manager")
        assert "home" not in fm.full_access_roots
