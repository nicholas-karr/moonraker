from __future__ import annotations
import os
import pathlib
import pytest
from moonraker.server import Server
from moonraker.components.printer_services import PrinterServicesComponent

from typing import Dict


@pytest.mark.run_paths(moonraker_conf="biokalico_components.conf")
class TestPrinterServicesEndpoints:
    def test_endpoints_registered(self, full_server: Server):
        app = full_server.moonraker_app
        expected = [
            "/server/printer_services/restart_all",
            "/server/printer_services/status",
        ]
        for path in expected:
            assert path in app.registered_base_handlers

    @pytest.mark.asyncio
    async def test_status_idle(self, full_server: Server, path_args: Dict[str, pathlib.Path]):
        comp: PrinterServicesComponent = full_server.lookup_component("printer_services")
        result = await comp._handle_status(None)
        assert result["running"] is False
        expected_log = os.path.join(str(path_args["temp_path"].joinpath("logs")), "restart_all.log")
        assert result["log_path"] == expected_log
