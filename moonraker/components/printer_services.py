# Trigger a full host-service restart from Mainsail's "Restart All" button
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# Runs scripts/printer-services.sh --restart (see that script for exactly
# what it restarts, and in what order: klipper, moonraker, crowsnest, nginx,
# bioslicer-image-display, then an MCU firmware_restart).
#
# The script can't run as a normal child process of this component, because
# moonraker is one of the services it restarts partway through. systemd's
# default KillMode=control-group means the "restart moonraker" step SIGTERMs
# every process still in moonraker.service's cgroup - including a bash
# script we spawned ourselves - before it ever reaches the services later in
# the restart order. `sudo systemd-run` launches the script as its own
# transient unit (its own cgroup) instead, so it keeps running when
# moonraker.service is torn down and restarted out from under it.
#
# The sudo call goes through the `machine` component's exec_sudo_command,
# the same path Reboot/Shutdown/per-service restart already use, so this gets
# the same passwordless-sudo-or-cached-password handling as those instead of
# just failing outright on installs where sudo isn't passwordless.

from __future__ import annotations

import os
import shlex
import subprocess
from typing import TYPE_CHECKING, Any, Dict

from ..common import RequestType

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .machine import Machine

UNIT_NAME = "biokalico-restart-all"


class PrinterServicesComponent:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.klipper_repo = os.path.expanduser(
            config.get("klipper_repo", "~/klipper")
        )
        self.script = os.path.join(
            self.klipper_repo, "scripts", "printer-services.sh"
        )

        self.server.register_endpoint(
            "/server/printer_services/restart_all",
            RequestType.POST,
            self._handle_restart_all,
        )
        self.server.register_endpoint(
            "/server/printer_services/status", RequestType.GET, self._handle_status
        )

    def _log_path(self) -> str:
        file_manager = self.server.lookup_component("file_manager")
        return os.path.join(file_manager.get_directory("logs"), "restart_all.log")

    def _is_running_sync(self) -> bool:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", UNIT_NAME],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return out.stdout.strip() == "active"
        except Exception:
            return False

    async def _is_running(self) -> bool:
        eventloop = self.server.get_event_loop()
        return await eventloop.run_in_thread(self._is_running_sync)

    async def _start(self, log_path: str) -> None:
        # Truncate up front so a stale log from a previous run can't be
        # mistaken for output from this one while systemd-run is still
        # starting the unit.
        with open(log_path, "w") as f:
            f.write("==> starting scripts/printer-services.sh --restart\n")
        machine: Machine = self.server.lookup_component("machine")
        cmd = shlex.join(
            [
                "systemd-run",
                "--unit=%s" % UNIT_NAME,
                "--collect",
                # sudo's env_reset means bare --setenv=HOME would inherit root's home, not ours.
                "--setenv=HOME=%s" % os.path.expanduser("~"),
                "--property=StandardOutput=append:%s" % log_path,
                "--property=StandardError=append:%s" % log_path,
                "--",
                "/bin/bash",
                self.script,
                "--restart",
            ]
        )
        await machine.exec_sudo_command(cmd, timeout=10.0)

    async def _handle_restart_all(self, web_request: WebRequest) -> Dict[str, Any]:
        if await self._is_running():
            raise self.server.error("A restart-all job is already in progress")
        await self._start(self._log_path())
        return {"started": True}

    async def _handle_status(self, web_request: WebRequest) -> Dict[str, Any]:
        return {"running": await self._is_running(), "log_path": self._log_path()}


def load_component(config: ConfigHelper) -> PrinterServicesComponent:
    return PrinterServicesComponent(config)
