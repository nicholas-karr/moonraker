# Health Monitor
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Watch for printer/host health conditions Moonraker doesn't already
surface externally (disk space, CPU temperature) and raise them as events
so a `[notifier ...]` section can push them out (webhook, ntfy, etc).
`klippy_shutdown`/`klippy_disconnect`/`cpu_throttled` already exist as
internal events raised elsewhere; `notifier.py` listens for those directly.
"""

from __future__ import annotations

import os
import shutil

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..eventloop import FlexTimer
    from .file_manager.file_manager import FileManager

DISK_CHECK_TIME = 300.
CPU_TEMP_HYSTERESIS = 5.


class HealthMonitor:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.event_loop = self.server.get_event_loop()

        self.disk_threshold = config.getfloat(
            "disk_threshold", 90., above=0., maxval=100.
        )
        self.disk_check_interval = config.getfloat(
            "disk_check_interval", DISK_CHECK_TIME, above=0.
        )
        self.cpu_temp_threshold: Optional[float] = config.getfloat(
            "cpu_temp_threshold", None
        )

        self.server.register_notification("disk_low")
        self.server.register_notification("disk_recovered")
        self.disk_alerted: Dict[str, bool] = {}
        self.disk_timer: FlexTimer = self.event_loop.register_timer(
            self._check_disk
        )

        self.cpu_temp_alerted = False
        if self.cpu_temp_threshold is not None:
            self.server.register_notification("cpu_temp_high")
            self.server.register_notification("cpu_temp_normal")
            self.server.register_event_handler(
                "proc_stats:proc_stat_update", self._check_cpu_temp
            )

    async def component_init(self) -> None:
        self.disk_timer.start()

    async def _check_disk(self, eventtime: float) -> float:
        fm: FileManager = self.server.lookup_component("file_manager")
        # Dedupe by filesystem so gcodes/config/logs sharing one disk
        # don't each raise their own alert.
        checked_devices: Dict[int, str] = {}
        for root in fm.get_registered_dirs():
            path = fm.get_directory(root)
            if not path or not os.path.isdir(path):
                continue
            try:
                dev = os.stat(path).st_dev
            except OSError:
                continue
            checked_devices.setdefault(dev, path)
        for path in checked_devices.values():
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            pct_used = usage.used / usage.total * 100.
            was_alerted = self.disk_alerted.get(path, False)
            if pct_used >= self.disk_threshold and not was_alerted:
                self.disk_alerted[path] = True
                self.server.send_event(
                    "disk_low",
                    f"Disk usage for '{path}' is {pct_used:.1f}%, at or "
                    f"above the configured threshold of "
                    f"{self.disk_threshold:.1f}%."
                )
            elif pct_used < self.disk_threshold and was_alerted:
                self.disk_alerted[path] = False
                self.server.send_event(
                    "disk_recovered",
                    f"Disk usage for '{path}' has dropped to "
                    f"{pct_used:.1f}%, below the configured threshold of "
                    f"{self.disk_threshold:.1f}%."
                )
        return eventtime + self.disk_check_interval

    async def _check_cpu_temp(self, stats: Dict[str, Any]) -> None:
        cpu_temp = stats.get("cpu_temp")
        if cpu_temp is None or self.cpu_temp_threshold is None:
            return
        if cpu_temp >= self.cpu_temp_threshold and not self.cpu_temp_alerted:
            self.cpu_temp_alerted = True
            self.server.send_event(
                "cpu_temp_high",
                f"CPU temperature is {cpu_temp:.1f}C, at or above the "
                f"configured threshold of {self.cpu_temp_threshold:.1f}C."
            )
        elif (
            cpu_temp < self.cpu_temp_threshold - CPU_TEMP_HYSTERESIS
            and self.cpu_temp_alerted
        ):
            self.cpu_temp_alerted = False
            self.server.send_event(
                "cpu_temp_normal",
                f"CPU temperature has dropped to {cpu_temp:.1f}C, back "
                "below the configured threshold."
            )


def load_component(config: ConfigHelper) -> HealthMonitor:
    return HealthMonitor(config)
