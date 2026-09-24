# Automatic recovery from known, safely-recoverable printer faults
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# Handles two faults:
#
# 1. Klipper is in "shutdown" after an MCU communication fault. Runs
#    printer_services.trigger_restart_all() (the same sequence as Mainsail's
#    "Restart All" button). Only acts while the printer is idle.
#
# 2. The SLA projector's serial link drops while video keeps working.
#    Recycles the adapter's USB port once, then optionally power-cycles its
#    hub (projector_usb_hub_path, which also resets anything else on that
#    hub). Acts while idle or paused, never while printing.
#
# Deliberate shutdowns (M112, emergency stop) are never undone. Each path
# caps its retries per incident, then gives up and leaves it to a human.
# Every action is announced through the "auto_recovery_action" event.
# Attempt counts are stored in Moonraker's database because the MCU path
# restarts Moonraker itself.

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from typing import TYPE_CHECKING, Any, Dict

from ..common import KlippyState

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .database import MoonrakerDatabase
    from .klippy_apis import KlippyAPI
    from .klippy_connection import KlippyConnection
    from .machine import Machine
    from .printer_services import PrinterServicesComponent

DB_NAMESPACE = "auto_recovery"
# Klipper words every host-initiated shutdown (M112, the webhooks emergency
# stop, gcode_macro's emergency_stop) as "Shutdown due to <reason>", unlike an
# MCU fault such as "Lost communication with MCU".
DELIBERATE_SHUTDOWN_PREFIX = "Shutdown due to "


class AutoRecoveryComponent:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()

        self.mcu_recovery_enabled = config.getboolean(
            "mcu_recovery_enabled", True
        )
        self.mcu_max_attempts = config.getint(
            "mcu_recovery_max_attempts", 2, minval=1
        )
        self.mcu_cooldown = config.getfloat(
            "mcu_recovery_cooldown_seconds", 120.0, above=0.0
        )

        self.projector_recovery_enabled = config.getboolean(
            "projector_recovery_enabled", False
        )
        self.projector_port_path = config.get(
            "projector_usb_port_path", None
        )
        self.projector_check_interval = config.getfloat(
            "projector_recovery_check_interval", 300.0, above=0.0
        )
        self.projector_hub_path = config.get(
            "projector_usb_hub_path", None
        )
        if self.projector_recovery_enabled and not self.projector_port_path:
            raise config.error(
                "[auto_recovery]: projector_usb_port_path is required "
                "when projector_recovery_enabled is true - the sysfs port "
                "directory to recycle, e.g. "
                "/sys/bus/usb/devices/1-3:1.0/1-3-port4 (find it with "
                "`udevadm info -a -n <device>` and match the port number "
                "under the parent hub)."
            )

        self.server.register_notification("auto_recovery_action")
        self.server.register_event_handler(
            "server:klippy_shutdown", self._on_klippy_shutdown
        )
        self.server.register_event_handler(
            "server:klippy_started", self._on_klippy_started
        )
        self.server.register_event_handler(
            "server:klippy_ready", self._on_klippy_ready
        )
        self._projector_probe_started = False
        # Serializes the read-decide-write cycle on the persisted MCU state,
        # since shutdown, started, ready and recheck events can overlap.
        self._mcu_lock = asyncio.Lock()
        self._mcu_recheck_pending = False

    async def _load_state(self, key: str) -> Dict[str, Any]:
        db: MoonrakerDatabase = self.server.lookup_component("database")
        return await db.get_item(DB_NAMESPACE, key, {})

    async def _save_state(self, key: str, value: Dict[str, Any]) -> None:
        db: MoonrakerDatabase = self.server.lookup_component("database")
        await db.insert_item(DB_NAMESPACE, key, value)

    async def _not_printing(self) -> bool:
        """True only if job_state confirms no print is printing or paused."""
        job_state = self.server.lookup_component("job_state", None)
        if job_state is None:
            return False
        state = job_state.last_print_stats.get("state", "")
        return state not in ("printing", "paused")

    async def _safe_for_projector_recovery(self) -> bool:
        """Like _not_printing, but also allows a paused print, which may be
        paused waiting for the projector. Resetting the adapter's port
        doesn't affect motion.
        """
        job_state = self.server.lookup_component("job_state", None)
        if job_state is None:
            return False
        state = job_state.last_print_stats.get("state", "")
        return state != "printing"

    async def _announce(self, message: str) -> None:
        logging.info("AUTO-RECOVERY: %s", message)
        self.server.send_event("auto_recovery_action", message)

    async def _on_klippy_shutdown(self) -> None:
        await self._recover_from_shutdown()

    async def _on_klippy_started(self, startup_state: KlippyState) -> None:
        # Moonraker sends klippy_shutdown only for a transition into
        # shutdown, not for a Klipper that is already shut down when it
        # connects (for example the MCU fault outlived a recovery attempt).
        if startup_state == KlippyState.SHUTDOWN:
            await self._recover_from_shutdown()

    def _schedule_mcu_recheck(self, delay: float) -> None:
        # In-memory only. A Moonraker restart drops it, but Moonraker
        # reconnecting to a Klipper that is still shut down raises
        # klippy_started again, which schedules a new one.
        if self._mcu_recheck_pending:
            return
        self._mcu_recheck_pending = True
        self.server.get_event_loop().delay_callback(
            delay + 1., self._recheck_mcu_shutdown
        )

    async def _recheck_mcu_shutdown(self) -> None:
        self._mcu_recheck_pending = False
        kconn: "KlippyConnection" = self.server.lookup_component(
            "klippy_connection"
        )
        if kconn.state == KlippyState.SHUTDOWN:
            await self._recover_from_shutdown()

    async def _recover_from_shutdown(self) -> None:
        if not self.mcu_recovery_enabled:
            return
        async with self._mcu_lock:
            await self._recover_from_shutdown_locked()

    async def _recover_from_shutdown_locked(self) -> None:
        kconn: "KlippyConnection" = self.server.lookup_component(
            "klippy_connection"
        )
        if DELIBERATE_SHUTDOWN_PREFIX in kconn.state_message:
            # M112, the web UI's emergency stop and a macro's emergency_stop()
            # all shut Klipper down on purpose. That is not a fault to undo.
            logging.info(
                "auto_recovery: Klipper was shut down deliberately, not "
                "attempting automatic recovery"
            )
            return
        if not await self._not_printing():
            await self._announce(
                "Klipper reported a shutdown, but a print is active - not "
                "attempting automatic recovery. Fix the underlying issue "
                "and run FIRMWARE_RESTART once it's safe to."
            )
            return

        mcu = await self._load_state("mcu_recovery")
        now = time.time()

        remaining = mcu.get("triggered_at", 0) + self.mcu_cooldown - now
        if remaining > 0:
            # An attempt is still in flight. Look again once its cooldown
            # ends: if it failed, no further event may arrive to act on.
            self._schedule_mcu_recheck(remaining)
            return

        attempt = mcu.get("attempt", 0)
        if attempt >= self.mcu_max_attempts:
            if not mcu.get("gave_up"):
                await self._announce(
                    "Klipper reported a shutdown again after "
                    f"{self.mcu_max_attempts} automatic recovery "
                    "attempt(s) - giving up. This needs a human: check "
                    "~/printer_data/logs/klippy.log and the MCU's wiring/"
                    "USB connection, then run FIRMWARE_RESTART."
                )
                mcu["gave_up"] = True
                await self._save_state("mcu_recovery", mcu)
            return

        printer_services: "PrinterServicesComponent" = (
            self.server.lookup_component("printer_services", None)
        )
        if printer_services is None:
            await self._announce(
                "Klipper reported a shutdown while idle, but the "
                "printer_services component isn't loaded, so automatic "
                "recovery can't run. Run FIRMWARE_RESTART manually."
            )
            return

        attempt += 1
        await self._announce(
            "Klipper reported a shutdown while idle - attempting "
            f"automatic recovery (attempt {attempt}/{self.mcu_max_attempts}"
            "): restarting services and the MCU firmware. Klipper, "
            "Moonraker and this printer's web UI will be briefly "
            "unreachable."
        )
        # Saved before the restart: the restart takes Moonraker down partway
        # through, so this record is the only thing that survives it.
        await self._save_state("mcu_recovery", {
            "attempt": attempt,
            "triggered_at": now,
            "gave_up": False,
        })
        try:
            started = await printer_services.trigger_restart_all()
        except Exception as e:
            await self._announce(f"Automatic recovery failed to start: {e}")
            return
        if not started:
            # A restart-all job (for example from the UI button) is already
            # running. It is not this attempt, so do not count it.
            await self._save_state("mcu_recovery", mcu)
            await self._announce(
                "A restart of all services was already in progress, so no "
                "additional automatic recovery was started."
            )
            self._schedule_mcu_recheck(self.mcu_cooldown)

    async def _on_klippy_ready(self) -> None:
        # Fires on every normal startup too, not just after a recovery
        # attempt - only announce/reset when the stored state shows one was
        # actually in flight, so a routine boot stays silent.
        async with self._mcu_lock:
            mcu = await self._load_state("mcu_recovery")
            if mcu.get("gave_up"):
                await self._announce(
                    "Klipper is ready again after automatic recovery had "
                    "given up."
                )
                await self._save_state("mcu_recovery", {})
            elif mcu.get("attempt", 0) > 0:
                await self._announce(
                    "Klipper reconnected successfully after automatic "
                    f"recovery (attempt {mcu['attempt']})."
                )
                await self._save_state("mcu_recovery", {})

        if self.projector_recovery_enabled and not self._projector_probe_started:
            self._projector_probe_started = True
            eventloop = self.server.get_event_loop()
            eventloop.register_callback(self._projector_probe_loop)

    async def _projector_probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self.projector_check_interval)
            try:
                await self._check_projector_link()
            except Exception:
                logging.exception(
                    "auto_recovery: projector link check failed"
                )

    async def _check_projector_link(self) -> None:
        kapis: "KlippyAPI" = self.server.lookup_component(
            "klippy_apis", None
        )
        if kapis is None:
            return
        try:
            result = await kapis.query_objects({"image_display": None})
        except self.server.error:
            return
        link_error = result.get("image_display", {}).get(
            "projector_link_error"
        )

        proj = await self._load_state("projector_recovery")

        if not link_error:
            if proj.get("attempted") or proj.get("hub_attempted"):
                await self._announce(
                    "The projector's serial link recovered after an "
                    "automatic USB reset."
                )
                await self._save_state("projector_recovery", {})
            return

        if not await self._safe_for_projector_recovery():
            return  # never touch USB hardware while actively printing

        if proj.get("hub_attempted"):
            if not proj.get("gave_up"):
                await self._announce(
                    "The projector's serial link is still down after an "
                    f"automatic hub power-cycle ({link_error}). This needs "
                    "physical attention: check the adapter and its cable."
                )
                proj["gave_up"] = True
                await self._save_state("projector_recovery", proj)
            return

        if proj.get("attempted"):
            if not proj.get("hub_attempted") and self.projector_hub_path:
                await self._announce(
                    f"The projector's serial link is still down after a "
                    f"soft USB port reset ({link_error}) - attempting a "
                    "hub-level power-cycle."
                )
                proj["hub_attempted"] = True
                proj["triggered_at"] = time.time()
                await self._save_state("projector_recovery", proj)
                await self._recycle_projector_hub()
                return

            if not proj.get("gave_up"):
                await self._announce(
                    "The projector's serial link is still down after an "
                    f"automatic USB reset ({link_error}). This needs "
                    "physical attention: check the adapter and its cable."
                )
                proj["gave_up"] = True
                await self._save_state("projector_recovery", proj)
            return

        await self._announce(
            f"The projector's serial link is down ({link_error}) - "
            "attempting an automatic USB port reset."
        )
        await self._save_state("projector_recovery", {
            "attempted": True,
            "triggered_at": time.time(),
            "gave_up": False,
        })
        await self._recycle_projector_port()

    async def _recycle_projector_port(self) -> None:
        machine: "Machine" = self.server.lookup_component("machine")
        port_path = self.projector_port_path
        if port_path is None:
            raise self.server.error("projector_usb_port_path is not configured")
        disable_path = shlex.quote("%s/disable" % port_path.rstrip("/"))
        cmd = "sh -c %s" % shlex.quote(
            "echo 1 > %s; sleep 2; echo 0 > %s"
            % (disable_path, disable_path)
        )
        await machine.exec_sudo_command(cmd, timeout=10.0)

    async def _recycle_projector_hub(self) -> None:
        """Power-cycle the USB hub the projector's adapter is on, when the
        port reset didn't help. This also resets any Klipper MCU on the hub,
        which ends a paused print; that's why projector_usb_hub_path is
        opt-in.
        """
        if not self.projector_hub_path:
            return
        machine: "Machine" = self.server.lookup_component("machine")
        authorized_path = shlex.quote(
            "%s/authorized" % self.projector_hub_path.rstrip("/")
        )
        cmd = "sh -c %s" % shlex.quote(
            "echo 0 > %s; sleep 2; echo 1 > %s; sleep 5"
            % (authorized_path, authorized_path)
        )
        await machine.exec_sudo_command(cmd, timeout=15.0)


def load_component(config: ConfigHelper) -> AutoRecoveryComponent:
    return AutoRecoveryComponent(config)
