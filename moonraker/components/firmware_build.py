# Firmware build/flash endpoints for [firmware_build <name>] targets
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# Drives scripts/firmware/build_and_flash.py (in the klipper repo) to build
# and flash [firmware_build <name>] targets declared in printer.cfg. Exposes
# REST endpoints consumed by biokalico_extras/mainsail/firmware-panel.js.
#
# There is deliberately no "flash only" endpoint - build_and_flash is the
# only action that ever writes firmware, so a stale binary can't be flashed
# without also being rebuilt first (see biokalico_extras/firmware_flash.md).
#
# A single job lock spans an entire request (build-only or build_and_flash),
# not just the flash phase - within one job, builds for multiple targets run
# in parallel (build_and_flash.py's own ThreadPoolExecutor) but a second
# concurrent top-level request is rejected outright, since two overlapping
# jobs could otherwise race on the same firmware_builds/<name>/ directory.
# Flashing itself is always sequential across targets regardless.

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..common import RequestType

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .shell_command import ShellCommandFactory, ShellCommand

MAX_LOG_LINES = 200


class FirmwareBuildComponent:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.klipper_repo = os.path.expanduser(
            config.get("klipper_repo", "~/klipper")
        )
        app_args = self.server.get_app_args()
        data_path = app_args.get("data_path")
        default_cfg = (
            os.path.join(data_path, "config", "printer.cfg") if data_path else None
        )
        self.printer_cfg = os.path.expanduser(
            config.get(
                "printer_cfg_path",
                default_cfg or "~/printer_data/config/printer.cfg",
            )
        )
        self.driver = os.path.join(
            self.klipper_repo, "scripts", "firmware", "build_and_flash.py"
        )

        sys.path.insert(
            0, os.path.join(self.klipper_repo, "scripts", "firmware")
        )

        self.job_lock = asyncio.Lock()
        self.job_active = False
        self.job_action: Optional[str] = None
        self.job_targets: List[str] = []
        self.job_status: Dict[str, Dict[str, Any]] = {}
        self.job_error: Optional[str] = None
        self.current_cmd: Optional[ShellCommand] = None
        # Guards the second _check_not_printing() call (see _handle_output)
        # so it only fires once per job, right as the flash phase begins.
        self.job_flash_check_done = False

        self.server.register_endpoint(
            "/server/firmware/targets", RequestType.GET, self._handle_targets
        )
        self.server.register_endpoint(
            "/server/firmware/build", RequestType.POST, self._handle_build
        )
        self.server.register_endpoint(
            "/server/firmware/build_and_flash",
            RequestType.POST,
            self._handle_build_and_flash,
        )
        self.server.register_endpoint(
            "/server/firmware/status", RequestType.GET, self._handle_status
        )
        self.server.register_endpoint(
            "/server/firmware/cancel", RequestType.POST, self._handle_cancel
        )

    # ---- printer.cfg reading ---------------------------------------------
    # Reuses build_and_flash.py's own parser rather than duplicating it -
    # both need to agree on exactly which targets/devices exist.

    def _load_targets(self) -> Dict[str, Dict[str, str]]:
        import build_and_flash as bf  # noqa: PLC0415 (see sys.path above)

        try:
            sections = bf.load_printer_cfg(self.printer_cfg)
            return bf.resolve_targets(sections)
        except bf.ConfigError as e:
            # bf.ConfigError is a plain Exception (not SystemExit) precisely
            # so it can be caught here and turned into a normal API error
            # instead of escaping as a BaseException and crashing Moonraker.
            raise self.server.error(str(e))

    def _mcu_section_name(self, mcu: str) -> str:
        return "mcu" if mcu == "mcu" else "mcu %s" % mcu

    def _read_last_flashed(self, name: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(
            self.klipper_repo, "firmware_builds", name, "last_flashed.json"
        )
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _current_git_describe(self) -> Optional[str]:
        # Same invocation scripts/buildcommands.py's git_version() uses to
        # produce the string firmware embeds as mcu_version, so the two are
        # directly comparable (see _version_matches). Coarse: --dirty is a
        # boolean, not a content hash - see _current_source_fingerprint for
        # the precise check used whenever a last_flashed record exists.
        try:
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    self.klipper_repo,
                    "describe",
                    "--always",
                    "--tags",
                    "--long",
                    "--dirty",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return out.stdout.strip() or None
        except Exception:
            return None

    def _current_source_fingerprint(
        self, preset: str, overrides: str = ""
    ) -> Optional[str]:
        # Reuses scripts/firmware/build_and_flash.py's source_fingerprint()
        # (same module _load_targets already imports) rather than
        # duplicating it, so the two can't drift apart. Deliberately scoped
        # to src/ plus this target's own preset - not a whole-repo or even
        # a whole-git hash - so an edit to something that doesn't affect
        # this target's compiled output (docs, klippy's host-side Python, a
        # *different* target's preset) never produces a false "needs
        # rebuild" flag. And deliberately a plain filesystem content hash
        # rather than a git-based one, so a new *untracked* file under
        # src/ still counts (a git-based hash only sees tracked files -
        # and even a tracked-aware one like `git describe --dirty` can't
        # distinguish one dirty edit from a different one anyway, since
        # "dirty" is a boolean, not a content hash - the more basic problem
        # this replaces).
        import build_and_flash as bf  # noqa: PLC0415 (see sys.path in __init__)

        try:
            return bf.source_fingerprint(preset, overrides)
        except Exception:
            return None

    @staticmethod
    def _version_matches(mcu_version: str, current_describe: str) -> bool:
        # build_version() in scripts/buildcommands.py appends
        # "-<timestamp>-<hostname>" onto git_version()'s output whenever the
        # build isn't a clean tagged checkout (i.e. almost always in
        # practice) - that suffix is never going to match between two
        # different build invocations even of identical source, so compare
        # as a prefix rather than requiring exact equality.
        if mcu_version == "?" or mcu_version.startswith("?-"):
            return False
        return mcu_version == current_describe or mcu_version.startswith(
            current_describe + "-"
        )

    def _gather_target_info_sync(self):
        # Everything here is blocking I/O (printer.cfg parsing, filesystem
        # walks under src/, one file read per target) - bundled into a
        # single call so _handle_targets can run it all in one
        # run_in_thread() hop instead of blocking Moonraker's single event
        # loop thread directly.
        targets = self._load_targets()
        current_describe = self._current_git_describe()
        last_flashed = {name: self._read_last_flashed(name) for name in targets}
        current_fingerprint = {
            name: self._current_source_fingerprint(
                info["preset"], info.get("overrides", "")
            )
            for name, info in targets.items()
        }
        return targets, current_describe, last_flashed, current_fingerprint

    # ---- request handlers --------------------------------------------------

    async def _handle_targets(self, web_request: WebRequest) -> Dict[str, Any]:
        eventloop = self.server.get_event_loop()
        targets, current_describe, last_flashed_map, current_fingerprint_map = (
            await eventloop.run_in_thread(self._gather_target_info_sync)
        )

        status: Dict[str, Any] = {}
        try:
            kapis = self.server.lookup_component("klippy_apis")
            query = {self._mcu_section_name(t["mcu"]): None for t in targets.values()}
            status = await kapis.query_objects(query, default={})
        except Exception:
            status = {}

        out: Dict[str, Any] = {}
        for name, info in targets.items():
            mcu_status = status.get(self._mcu_section_name(info["mcu"]), {})
            mcu_version = mcu_status.get("mcu_version")
            last_flashed = last_flashed_map.get(name)
            mismatch = None
            if last_flashed is not None and last_flashed.get("source_fingerprint"):
                # Primary, precise signal: does src/ + this target's preset,
                # as they currently sit on disk, match what was actually
                # built the last time this panel flashed this target? Works
                # whether or not the MCU is even connected, and (unlike
                # mcu_version below) catches every edit individually rather
                # than just "has anything at all changed since a clean
                # checkout".
                if (
                    current_fingerprint_map.get(name)
                    != last_flashed["source_fingerprint"]
                ):
                    mismatch = (
                        "source has changed since this target was last "
                        "built & flashed - rebuild & reflash"
                    )
            elif mcu_version:
                # Fallback for when we have no flash record from this panel
                # (flashed by hand, or last_flashed.json predates this
                # field): coarser ground truth from the firmware itself.
                # git describe --dirty is a boolean, not a content hash, so
                # this only distinguishes "matches a clean/tagged build" vs
                # "doesn't" - it can't tell two different dirty edits apart.
                if current_describe and not self._version_matches(
                    mcu_version, current_describe
                ):
                    mismatch = (
                        "on-device firmware (%s) does not match the current "
                        "build (%s) - rebuild & reflash"
                        % (mcu_version, current_describe)
                    )
            else:
                mismatch = (
                    "mcu not connected and never flashed by this panel - "
                    "build & flash to confirm"
                )
            out[name] = {
                "preset": info["preset"],
                "mcu": info["mcu"],
                "device": info["device"],
                "mcu_version": mcu_version,
                "last_flashed": last_flashed,
                "mismatch": mismatch,
            }
        return {"targets": out}

    def _get_target_names(
        self, web_request: WebRequest, all_targets: Dict[str, Any]
    ) -> List[str]:
        raw = web_request.get_args().get("targets", "all")
        if raw == "all" or raw == ["all"]:
            names = list(all_targets)
        elif isinstance(raw, str):
            names = [t.strip() for t in raw.split(",") if t.strip()]
        else:
            names = [str(t).strip() for t in raw if str(t).strip()]
        if not names:
            raise self.server.error("No targets specified")
        unknown = [n for n in names if n not in all_targets]
        if unknown:
            raise self.server.error(
                "Unknown firmware_build target(s): %s" % ", ".join(unknown)
            )
        return names

    def _check_not_printing(self) -> None:
        job_state = self.server.lookup_component("job_state", None)
        if job_state is None:
            return
        state = getattr(job_state, "last_print_stats", {}).get("state")
        if state in ("printing", "paused"):
            raise self.server.error(
                "Refusing to flash while a print is %s" % state
            )

    async def _handle_build(self, web_request: WebRequest) -> Dict[str, Any]:
        return await self._start_job(web_request, "build")

    async def _handle_build_and_flash(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        return await self._start_job(web_request, "build_and_flash")

    async def _start_job(
        self, web_request: WebRequest, action: str
    ) -> Dict[str, Any]:
        all_targets = await self.server.get_event_loop().run_in_thread(
            self._load_targets
        )
        names = self._get_target_names(web_request, all_targets)
        if action == "build_and_flash":
            self._check_not_printing()
        if self.job_lock.locked():
            raise self.server.error(
                "A firmware build/flash job is already in progress"
            )
        await self.job_lock.acquire()
        try:
            self.job_active = True
            self.job_action = action
            self.job_targets = names
            self.job_error = None
            self.job_status = {n: {"phase": "queued", "log": []} for n in names}
            self.job_flash_check_done = False

            shell_cmd: ShellCommandFactory = self.server.lookup_component(
                "shell_command"
            )
            # Quoted: printer_cfg_path/klipper_repo are user-configurable
            # and shell_command's ShellCommand tokenizes this whole string
            # with shlex.split() - an unquoted path containing a space or
            # quote character would otherwise either misparse or raise
            # inside build_shell_command, which (without this try/finally)
            # would leak job_lock held forever.
            cmd = "%s %s --action %s --printer-cfg %s --targets %s" % (
                shlex.quote(sys.executable),
                shlex.quote(self.driver),
                action,
                shlex.quote(self.printer_cfg),
                ",".join(shlex.quote(n) for n in names),
            )
            scmd = shell_cmd.build_shell_command(
                cmd, callback=self._handle_output, cwd=self.klipper_repo
            )
            self.current_cmd = scmd
        except BaseException:
            self.job_active = False
            self.current_cmd = None
            if self.job_lock.locked():
                self.job_lock.release()
            raise

        async def _run_job() -> None:
            try:
                success = await scmd.run(timeout=0, verbose=True, log_complete=True)
                if not success and self.job_error is None:
                    self.job_error = "build/flash process exited with an error"
            except Exception as e:
                self.job_error = str(e)
            finally:
                self.job_active = False
                self.current_cmd = None
                if self.job_lock.locked():
                    self.job_lock.release()

        self.server.get_event_loop().create_task(_run_job())
        return {"started": True, "action": action, "targets": names}

    def _handle_output(self, line: bytes) -> None:
        try:
            rec = json.loads(line.decode(errors="replace"))
        except Exception:
            return
        target = rec.get("target")
        phase = rec.get("phase")
        text = rec.get("line", "")
        entry = self.job_status.setdefault(target, {"phase": phase, "log": []})
        entry["phase"] = phase
        if text:
            entry["log"].append(text)
            del entry["log"][:-MAX_LOG_LINES]
        if phase == "error" and self.job_error is None:
            self.job_error = "%s: %s" % (target, text)
        # Second not-printing check (see _check_not_printing / _start_job):
        # the job-submission-time check only catches a print already
        # running before this (potentially multi-minute) build started - it
        # can't see one started via a different Mainsail tab/API call while
        # the build was in flight. The driver's own progress stream is the
        # only signal this component has for "the flash phase is starting
        # now" (build_and_flash.py flashes sequentially, only after every
        # target has finished building - see build_and_flash.py's main()),
        # so fire once, right as the first "flashing" phase line for this
        # job comes in, and abort before the driver stops the klipper
        # service if a print has started in the meantime.
        if (
            phase == "flashing"
            and self.job_action == "build_and_flash"
            and not self.job_flash_check_done
        ):
            self.job_flash_check_done = True
            try:
                self._check_not_printing()
            except Exception as e:
                self.server.get_event_loop().create_task(
                    self._abort_flash_for_active_print(str(e))
                )

    async def _handle_status(self, web_request: WebRequest) -> Dict[str, Any]:
        return {
            "active": self.job_active,
            "action": self.job_action,
            "targets": self.job_targets,
            "error": self.job_error,
            "status": self.job_status,
        }

    async def _handle_cancel(self, web_request: WebRequest) -> Dict[str, Any]:
        if self.current_cmd is None:
            raise self.server.error("No firmware build/flash job is running")
        # SIGTERM first (build_and_flash.py catches it and still restarts
        # klipper if a flash was in progress), escalating to SIGKILL only if
        # unresponsive.
        await self.current_cmd.cancel(sig_idx=self.current_cmd.IDX_SIGTERM)
        return {"cancelled": True}

    async def _abort_flash_for_active_print(self, reason: str) -> None:
        # Called from _handle_output (a sync callback) via create_task,
        # since cancelling the driver process is async. Same SIGTERM path
        # as _handle_cancel - build_and_flash.py's signal handler still
        # restarts klipper in flash_target()'s finally block either way, so
        # this never leaves the service down.
        if self.current_cmd is None:
            return
        self.job_error = "aborting build_and_flash job: %s" % reason
        await self.current_cmd.cancel(sig_idx=self.current_cmd.IDX_SIGTERM)


def load_component(config: ConfigHelper) -> FirmwareBuildComponent:
    return FirmwareBuildComponent(config)
