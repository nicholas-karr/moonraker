# Per-print timelapse: per-layer frame capture, rendered to H.264 after
# each print
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# Implements the `machine.timelapse.*` endpoints and the
# `timelapse:timelapse_event` notification that Mainsail's Timelapse page
# expects.
#
# Each print's frames are kept in their own directory under tmp/ and deleted
# only after a successful render. On klippy_ready, any directory left behind
# by an interrupted print is rendered as "<job>-recovered.mp4".
#
# Limitations: head parking only supports rectangular-bed cartesian/corexy
# kinematics, and only "layermacro" mode is implemented (`mode` is accepted so
# Mainsail's Settings tab keeps working).

from __future__ import annotations

import asyncio
import glob
import json
import logging
import math
import os
import re
import shlex
import shutil
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..common import RequestType

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .klippy_apis import KlippyAPI
    from .shell_command import ShellCommandFactory
    from .database import MoonrakerDatabase
    from .webcam import WebcamManager

DB_NAMESPACE = "timelapse"
JOB_META_FILENAME = "job.json"
FRAME_GLOB_PATTERN = "frame_*.jpg"
FRAME_NAME_FMT = "frame_%06d.jpg"

# Settings read and written by Mainsail through
# machine.timelapse.{get,post}_settings. Names and types must match
# deps/mainsail/src/store/server/timelapse/types.ts.
DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "mode": "layermacro",
    "camera": "",
    "autorender": True,
    "stream_delay_compensation": 0.05,
    "gcode_verbose": False,
    "parkhead": False,
    "parkpos": "back_left",
    "park_custom_pos_x": 10.0,
    "park_custom_pos_y": 10.0,
    "park_custom_pos_dz": 0.0,
    "park_travel_speed": 100,
    "park_retract_speed": 15,
    "park_extrude_speed": 15,
    "park_retract_distance": 1.0,
    "park_extrude_distance": 1.0,
    "park_time": 0.1,
    "fw_retract": False,
    "hyperlapse_cycle": 30,
    "constant_rate_factor": 23,
    "output_framerate": 30,
    "pixelformat": "yuv420p",
    "extraoutputparams": "",
    "variable_fps": False,
    "targetlength": 10,
    "variable_fps_min": 5,
    "variable_fps_max": 60,
    "rotation": 0,
    "duplicatelastframe": 5,
    "previewimage": True,
    "time_format_code": "%Y%m%d_%H%M",
}

# Settings copied into the TIMELAPSE_TAKE_FRAME macro's variables. The macro
# parks in X/Y only, so park_custom_pos_dz is stored but not used.
GCODE_SETTINGS = (
    "parkhead", "parkpos", "park_custom_pos_x", "park_custom_pos_y",
    "park_travel_speed", "park_retract_speed", "park_extrude_speed",
    "park_retract_distance", "park_extrude_distance", "park_time",
    "fw_retract", "gcode_verbose",
)


def _check_extra_params(value: str) -> None:
    # extraoutputparams is split with shlex when a render starts. Reject a
    # value that cannot be split (an unbalanced quote) up front, instead of
    # letting it fail every later render.
    try:
        shlex.split(value)
    except ValueError as err:
        raise ValueError(f"extraoutputparams is not valid: {err}") from err


def _sanitize_job_stem(gcode_filename: str) -> str:
    stem = os.path.splitext(os.path.basename(gcode_filename or "print"))[0]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "print"
    return stem[:40]


class Timelapse:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.klippy_apis: KlippyAPI = self.server.lookup_component("klippy_apis")
        self.database: MoonrakerDatabase = self.server.lookup_component("database")

        self.ffmpeg_binary_path = config.get(
            "ffmpeg_binary_path", "/usr/bin/ffmpeg")
        self.out_dir = os.path.expanduser(
            config.get("output_path", "~/printer_data/timelapse/"))
        self.tmp_dir = os.path.join(self.out_dir, "tmp")
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)

        self.config: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.config["blockedsettings"] = []
        self.current_job: Optional[Dict[str, Any]] = None
        self.render_running = False
        self.render_progress = 0
        self.capture_lock = asyncio.Lock()
        self.last_print_state = "standby"
        self.confighelper = config
        # Read config options in __init__. Moonraker reports options not
        # read by then as unparsed.
        self._apply_static_config_overrides()

        file_manager = self.server.lookup_component("file_manager")
        file_manager.register_directory(
            "timelapse", self.out_dir, full_access=True)
        file_manager.register_directory("timelapse_frames", self.tmp_dir)

        self.server.register_notification("timelapse:timelapse_event")
        self.server.register_event_handler(
            "server:klippy_ready", self._handle_klippy_ready)
        self.server.register_event_handler(
            "server:status_update", self._handle_status_update)
        self.server.register_remote_method(
            "timelapse_newframe", self._call_newframe)
        self.server.register_remote_method(
            "timelapse_render", self._call_render)

        self.server.register_endpoint(
            "/machine/timelapse/settings", RequestType.GET | RequestType.POST,
            self._handle_settings)
        self.server.register_endpoint(
            "/machine/timelapse/lastframeinfo", RequestType.GET,
            self._handle_lastframeinfo)
        self.server.register_endpoint(
            "/machine/timelapse/render", RequestType.POST,
            self._handle_render_request)

    async def component_init(self) -> None:
        stored: Dict[str, Any] = await self.database.get_item(
            DB_NAMESPACE, "settings", {})
        blocked = self.config["blockedsettings"]
        self.config.update({k: v for k, v in stored.items()
                            if k in DEFAULT_SETTINGS and k not in blocked})

    def _apply_static_config_overrides(self) -> None:
        # Settings in moonraker.conf win and are greyed out in Mainsail.
        blocked: List[str] = []
        for key in self.confighelper.get_options():
            if key not in DEFAULT_SETTINGS:
                continue
            default = DEFAULT_SETTINGS[key]
            if isinstance(default, bool):
                value: Any = self.confighelper.getboolean(key)
            elif isinstance(default, int):
                value = self.confighelper.getint(key)
            elif isinstance(default, float):
                value = self.confighelper.getfloat(key)
            else:
                value = self.confighelper.get(key)
                if key == "extraoutputparams":
                    try:
                        _check_extra_params(value)
                    except ValueError as err:
                        raise self.confighelper.error(f"[timelapse] {err}")
            self.config[key] = value
            blocked.append(key)
        self.config["blockedsettings"] = blocked

    async def _handle_klippy_ready(self) -> None:
        result = await self.klippy_apis.subscribe_objects({"print_stats": None})
        # Otherwise a Moonraker restart mid-print looks like a new print
        # starting.
        self.last_print_state = result.get(
            "print_stats", {}).get("state", "standby")
        await self._push_gcode_settings()
        await self._reconcile_tmp_dir()

    async def _handle_status_update(self, status: Dict[str, Any]) -> None:
        if "print_stats" not in status:
            return
        state = status["print_stats"].get("state")
        if state is None or state == self.last_print_state:
            return
        was_active = self.last_print_state in ("printing", "paused")
        is_active = state in ("printing", "paused")
        self.last_print_state = state
        if is_active and not was_active:
            filename = status["print_stats"].get("filename", "")
            if not filename:
                result = await self.klippy_apis.query_objects(
                    {"print_stats": None})
                filename = result.get("print_stats", {}).get("filename", "")
            await self._start_job(filename)
        elif was_active and not is_active:
            await self._end_job()

    async def _reconcile_tmp_dir(self) -> None:
        # At klippy_ready, render any job left in tmp/ by an interrupted
        # print, other than the one still running.
        result = await self.klippy_apis.query_objects({"print_stats": None})
        pstats = result.get("print_stats", {})
        live_filename = pstats.get("filename", "")
        is_live = pstats.get("state") in ("printing", "paused")

        try:
            entries = sorted(os.scandir(self.tmp_dir), key=lambda e: e.name)
        except FileNotFoundError:
            entries = []
        recovery_dirs: List[str] = []
        live_candidates: List[tuple[float, str]] = []
        if is_live:
            for entry in entries:
                if not entry.is_dir():
                    continue
                meta = self._read_job_meta(entry.path)
                if (
                    meta is not None
                    and os.path.basename(live_filename)
                    == meta.get("gcode_file")
                    and not meta.get("completed")
                ):
                    live_candidates.append(
                        (float(meta.get("start_time", 0.)), entry.path)
                    )
        live_job_dir = (
            max(live_candidates, default=(0., ""))[1]
            if live_candidates else ""
        )
        for entry in entries:
            if not entry.is_dir():
                continue
            meta = self._read_job_meta(entry.path)
            if (
                entry.path == live_job_dir
                and meta is not None
                and self.current_job is None
            ):
                framecount = len(self._job_frames(entry.path))
                self.current_job = {
                    "dir": entry.path,
                    "gcode_file": meta.get("gcode_file", ""),
                    "framecount": framecount,
                    "lastframefile": (
                        f"{os.path.basename(entry.path)}/"
                        f"{FRAME_NAME_FMT % framecount}"
                        if framecount else ""
                    ),
                }
                continue
            if meta is not None and meta.get("completed"):
                # Finished with autorender off; kept for a manual render.
                continue
            logging.info(
                f"timelapse: recovering interrupted job directory "
                f"{entry.path}"
            )
            recovery_dirs.append(entry.path)
        if recovery_dirs:
            # One task rendering in order; parallel tasks would skip each
            # other because of render_running.
            self.server.get_event_loop().create_task(
                self._recover_jobs(recovery_dirs))

    async def _recover_jobs(self, job_dirs: List[str]) -> None:
        for job_dir in job_dirs:
            try:
                await self._render_job(job_dir, suffix="-recovered")
            except Exception:
                # Keep going if one job fails.
                self.render_running = False
                logging.exception(
                    "timelapse: unexpected error recovering %s", job_dir
                )

    def _read_job_meta(self, job_dir: str) -> Optional[Dict[str, Any]]:
        try:
            with open(os.path.join(job_dir, JOB_META_FILENAME)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _job_frames(self, job_dir: str) -> List[str]:
        return sorted(glob.glob(os.path.join(job_dir, FRAME_GLOB_PATTERN)))

    async def _start_job(self, gcode_filename: str) -> None:
        if self.current_job is not None:
            # The previous print never finished. Leave its directory for the
            # next klippy_ready to render.
            logging.info(
                "timelapse: new print started while a previous job was "
                "still marked current - abandoning it for later recovery"
            )
            self.current_job = None

        if not self.config["enabled"]:
            return

        start_stamp = time.strftime("%Y%m%d_%H%M%S")
        job_id = f"{_sanitize_job_stem(gcode_filename)}-{start_stamp}"
        job_dir = os.path.join(self.tmp_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, JOB_META_FILENAME), "w") as f:
            json.dump({
                "gcode_file": os.path.basename(gcode_filename),
                "start_time": time.time(),
            }, f)

        self.current_job = {
            "dir": job_dir,
            "gcode_file": os.path.basename(gcode_filename),
            "framecount": 0,
            "lastframefile": "",
        }

    async def _end_job(self) -> None:
        job = self.current_job
        self.current_job = None
        if job is None:
            return
        if self.config["autorender"]:
            self.server.get_event_loop().create_task(
                self._render_job(job["dir"], suffix=""))
        else:
            # Keep the frames for a manual render. Marked completed so a
            # restart doesn't render it as "-recovered".
            self._mark_job_completed(job["dir"])

    def _mark_job_completed(self, job_dir: str) -> None:
        meta = self._read_job_meta(job_dir) or {}
        meta["completed"] = True
        try:
            with open(os.path.join(job_dir, JOB_META_FILENAME), "w") as f:
                json.dump(meta, f)
        except OSError as err:
            logging.info(f"timelapse: could not mark job completed: {err}")

    async def _push_gcode_settings(self) -> None:
        # One SET_GCODE_VARIABLE per setting, to avoid escaping braces.
        c = self.config
        variables = {
            "enable": c["enabled"],
            "verbose": c["gcode_verbose"],
            "park_enable": c["parkhead"],
            # Klipper's shlex parsing removes one layer of quotes before
            # SET_GCODE_VARIABLE's literal_eval, so strings need two.
            "park_pos": json.dumps(f"'{c['parkpos']}'"),
            "park_time": c["park_time"],
            "park_custom_x": c["park_custom_pos_x"],
            "park_custom_y": c["park_custom_pos_y"],
            "park_travel_speed": c["park_travel_speed"],
            "park_retract_speed": c["park_retract_speed"],
            "park_extrude_speed": c["park_extrude_speed"],
            "park_retract_distance": c["park_retract_distance"],
            "park_extrude_distance": c["park_extrude_distance"],
            "fw_retract": c["fw_retract"],
        }
        gcommand = "\n".join(
            f"SET_GCODE_VARIABLE MACRO=TIMELAPSE_TAKE_FRAME "
            f"VARIABLE={name} VALUE={value}"
            for name, value in variables.items()
        )
        try:
            await self.klippy_apis.run_gcode(gcommand)
        except self.server.error as err:
            # Usually the macros aren't included in printer.cfg. Not a bug,
            # so no traceback.
            logging.info("timelapse: could not push gcode settings "
                         "(is TIMELAPSE_TAKE_FRAME installed?): %s", err)

    def _call_newframe(self) -> None:
        # Called by TIMELAPSE_TAKE_FRAME once the toolhead is parked (or
        # right away if parking is off). stream_delay_compensation delays
        # the capture to allow for camera latency; park_time delays telling
        # the macro to resume.
        eventloop = self.server.get_event_loop()
        eventloop.delay_callback(
            self.config["park_time"], self._release_parked_head)
        eventloop.delay_callback(
            self.config["stream_delay_compensation"],
            lambda: eventloop.create_task(self._newframe())
        )

    async def _release_parked_head(self) -> None:
        gcommand = (
            "SET_GCODE_VARIABLE MACRO=TIMELAPSE_TAKE_FRAME "
            "VARIABLE=takingframe VALUE=False"
        )
        try:
            await self.klippy_apis.run_gcode(gcommand)
        except self.server.error:
            logging.exception(f"timelapse: error running {gcommand}")

    async def _get_snapshot_url(self) -> str:
        if self.config["camera"]:
            try:
                wcmgr: WebcamManager = self.server.lookup_component("webcam")
                cams = wcmgr.get_webcams()
                cam = cams.get(self.config["camera"])
                if cam is not None:
                    return await cam.get_snapshot_url()
            except Exception:
                logging.exception("timelapse: error reading webcam config")
        return "http://127.0.0.1:8080/snapshot"

    async def _newframe(self) -> None:
        # Two layers can request a frame before the first capture finishes.
        # Serialize them so each gets its own frame number.
        async with self.capture_lock:
            await self._capture_frame()

    async def _capture_frame(self) -> None:
        job = self.current_job
        if job is None:
            logging.info("timelapse: newframe requested with no active job")
            return

        snapshot_url = await self._get_snapshot_url()
        framefile = FRAME_NAME_FMT % (job["framecount"] + 1)
        dest = os.path.join(job["dir"], framefile)
        cmd = shlex.join([
            "curl", "-sf", "--max-time", "5", snapshot_url, "-o", dest,
        ])

        shell_cmd: ShellCommandFactory = self.server.lookup_component(
            "shell_command")
        scmd = shell_cmd.build_shell_command(cmd)
        try:
            success = await scmd.run(timeout=6., verbose=False)
        except Exception:
            logging.exception(f"timelapse: error running '{cmd}'")
            success = False

        result: Dict[str, Any] = {"action": "newframe"}
        if success and os.path.isfile(dest):
            job["framecount"] += 1
            job["lastframefile"] = f"{os.path.basename(job['dir'])}/{framefile}"
            result.update({
                "frame": str(job["framecount"]),
                "framefile": job["lastframefile"],
                "status": "success",
            })
        else:
            logging.info(f"timelapse: capturing frame failed: {cmd}")
            try:
                os.remove(dest)
            except OSError:
                pass
            result["status"] = "error"
        self._notify(result)

    def _call_render(self) -> None:
        target = self._status_target_dir()
        if target is not None:
            self.server.get_event_loop().create_task(
                self._render_job(target, suffix=""))

    def _status_target_dir(self) -> Optional[str]:
        # Between prints (autorender off) this is the last print's job.
        if self.current_job is not None:
            return self.current_job["dir"]
        try:
            candidates = [
                e.path for e in os.scandir(self.tmp_dir) if e.is_dir()
            ]
        except FileNotFoundError:
            return None
        if not candidates:
            return None
        return max(candidates, key=os.path.getmtime)

    def _rotation_filter(self) -> str:
        rotation = self.config["rotation"]
        if rotation == 90:
            return "transpose=1"
        if rotation == 180:
            return "hflip,vflip"
        if rotation == 270:
            return "transpose=2"
        if rotation:
            return f"rotate={rotation * math.pi / 180}"
        return ""

    def _unique_output_stem(self, base_stem: str) -> str:
        # The default time format has minute precision, so avoid overwriting
        # an earlier video.
        candidate = base_stem
        sequence = 2
        while (
            os.path.exists(os.path.join(self.out_dir, candidate + ".mp4"))
            or os.path.exists(os.path.join(self.out_dir, candidate + ".jpg"))
        ):
            candidate = f"{base_stem}_{sequence}"
            sequence += 1
        return candidate

    async def _render_job(self, job_dir: str, suffix: str) -> None:
        if self.render_running:
            logging.info("timelapse: render already running, skipping")
            return
        self.render_running = True
        try:
            await self._render_job_impl(job_dir, suffix)
        finally:
            # Any failure must still let later renders run.
            self.render_running = False

    async def _render_job_impl(self, job_dir: str, suffix: str) -> None:
        frames = self._job_frames(job_dir)
        if not frames:
            logging.info(f"timelapse: no frames to render in {job_dir}")
            shutil.rmtree(job_dir, ignore_errors=True)
            return
        if not os.path.isfile(self.ffmpeg_binary_path):
            logging.info(
                f"timelapse: {self.ffmpeg_binary_path} not found, "
                "leaving frames in place"
            )
            return

        self.render_progress = 0
        meta = self._read_job_meta(job_dir) or {}
        gcode_stem = _sanitize_job_stem(meta.get("gcode_file", ""))
        # A user-supplied format may contain a path separator, which would
        # move the output outside out_dir.
        date_time = re.sub(
            r"[\\/]+", "_",
            datetime.now().strftime(self.config["time_format_code"]))
        out_stem = self._unique_output_stem(
            f"{gcode_stem}_{date_time}{suffix}")

        last_real_frame = frames[-1]
        duplicates: List[str] = []
        n_dupes = self.config["duplicatelastframe"]
        if n_dupes > 0:
            for i in range(n_dupes):
                dupe = os.path.join(
                    job_dir, FRAME_NAME_FMT % (len(frames) + i + 1))
                try:
                    shutil.copy(last_real_frame, dupe)
                    duplicates.append(dupe)
                except OSError as err:
                    logging.info(f"timelapse: duplicating last frame failed: {err}")
            frames = self._job_frames(job_dir)

        if self.config["variable_fps"]:
            fps = len(frames) // max(self.config["targetlength"], 1)
            fps = max(min(fps, self.config["variable_fps_max"]),
                      self.config["variable_fps_min"])
        else:
            fps = self.config["output_framerate"]

        tmp_output = os.path.join(job_dir, out_stem + ".mp4")
        argv = [
            self.ffmpeg_binary_path, "-y", "-r", str(fps),
            "-i", os.path.join(job_dir, FRAME_NAME_FMT),
        ]
        vf = self._rotation_filter()
        if vf:
            argv.extend(["-vf", vf])
        argv.extend([
            "-threads", "2", "-g", "5",
            "-crf", str(self.config["constant_rate_factor"]),
            "-vcodec", "libx264",
            "-pix_fmt", self.config["pixelformat"],
            "-an",
        ])
        argv.extend(shlex.split(self.config["extraoutputparams"]))
        argv.append(tmp_output)
        cmd = shlex.join(argv)

        logging.info(f"timelapse: rendering {job_dir} -> {out_stem}.mp4")
        self._notify({
            "action": "render", "status": "started",
            "framecount": str(len(frames)),
        })

        shell_cmd: ShellCommandFactory = self.server.lookup_component(
            "shell_command")
        scmd = shell_cmd.build_shell_command(
            cmd, std_err_callback=lambda data: self._render_progress_cb(
                data, len(frames))
        )
        try:
            success = await scmd.run(
                timeout=9999999999., verbose=True, log_complete=False)
        except Exception:
            logging.exception(f"timelapse: error running '{cmd}'")
            success = False

        for dupe in duplicates:
            try:
                os.remove(dupe)
            except OSError:
                pass

        result: Dict[str, Any] = {"action": "render"}
        if success:
            final_path = os.path.join(self.out_dir, out_stem + ".mp4")
            try:
                shutil.move(tmp_output, final_path)
            except OSError as err:
                logging.info(f"timelapse: moving rendered video failed: {err}")
                success = False

        if success:
            if self.config["previewimage"]:
                try:
                    shutil.copy(
                        last_real_frame,
                        os.path.join(self.out_dir, out_stem + ".jpg"),
                    )
                    result["previewimage"] = out_stem + ".jpg"
                except OSError as err:
                    logging.info(f"timelapse: copying preview image failed: {err}")
            shutil.rmtree(job_dir, ignore_errors=True)
            result.update({
                "status": "success",
                "filename": out_stem + ".mp4",
                "printfile": meta.get("gcode_file", ""),
            })
            logging.info(f"timelapse: rendered {out_stem}.mp4")
        else:
            result.update({"status": "error", "cmd": cmd})
            logging.info(
                f"timelapse: render failed, frames kept at {job_dir}")

        if self.current_job is not None and self.current_job["dir"] == job_dir:
            self.current_job = None
        self._notify(result)

    def _render_progress_cb(self, data: bytes, framecount: int) -> None:
        text = data.decode("utf-8", "ignore")
        match = re.search(r"frame=\s*(\d+)", text)
        if not match or framecount <= 0:
            return
        percent = min(int(match.group(1)) / framecount * 100, 100)
        if int(percent) != self.render_progress:
            self.render_progress = int(percent)
            self._notify({
                "action": "render", "status": "running",
                "progress": self.render_progress,
            })

    def _notify(self, result: Dict[str, Any]) -> None:
        self.server.send_event("timelapse:timelapse_event", result)

    async def _handle_settings(self, web_request: WebRequest) -> Dict[str, Any]:
        if web_request.get_request_type() == RequestType.POST:
            args = web_request.get_args()
            gcode_changed = False
            extra_params = web_request.get_str("extraoutputparams", None)
            if (
                extra_params is not None
                and "extraoutputparams" not in self.config["blockedsettings"]
            ):
                try:
                    _check_extra_params(extra_params)
                except ValueError as err:
                    raise self.server.error(str(err), 400)
            for key in args:
                if key not in DEFAULT_SETTINGS or key in self.config["blockedsettings"]:
                    continue
                default = DEFAULT_SETTINGS[key]
                if isinstance(default, bool):
                    self.config[key] = web_request.get_boolean(key)
                elif isinstance(default, int):
                    self.config[key] = web_request.get_int(key)
                elif isinstance(default, float):
                    self.config[key] = web_request.get_float(key)
                else:
                    self.config[key] = web_request.get_str(key)
                if key in GCODE_SETTINGS:
                    gcode_changed = True
            await self.database.insert_item(
                DB_NAMESPACE, "settings",
                {k: v for k, v in self.config.items()
                 if k not in ("blockedsettings",)}
            )
            if gcode_changed:
                await self._push_gcode_settings()
        return self.config

    async def _handle_lastframeinfo(self, web_request: WebRequest) -> Dict[str, Any]:
        target = self._status_target_dir()
        if target is None:
            return {"framecount": 0, "lastframefile": ""}
        frames = self._job_frames(target)
        lastframefile = (
            f"{os.path.basename(target)}/{os.path.basename(frames[-1])}"
            if frames else ""
        )
        return {"framecount": len(frames), "lastframefile": lastframefile}

    async def _handle_render_request(self, web_request: WebRequest) -> Dict[str, Any]:
        target = self._status_target_dir()
        if target is None:
            return {"status": "skipped", "msg": "no frames to render"}
        self.server.get_event_loop().create_task(
            self._render_job(target, suffix=""))
        return {"status": "started"}


def load_component(config: ConfigHelper) -> Timelapse:
    return Timelapse(config)
