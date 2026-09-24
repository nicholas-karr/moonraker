import asyncio
import json
import os

import pytest

from moonraker.common import RequestType
from moonraker.components.timelapse import DEFAULT_SETTINGS, Timelapse


def test_unique_output_stem_avoids_existing_video_and_preview(tmp_path):
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.out_dir = str(tmp_path)
    (tmp_path / "part_20260724_1010.mp4").touch()
    (tmp_path / "part_20260724_1010_2.jpg").touch()

    assert (
        timelapse._unique_output_stem("part_20260724_1010")
        == "part_20260724_1010_3"
    )


def test_recovery_jobs_render_serially():
    timelapse = Timelapse.__new__(Timelapse)
    calls = []

    async def record_render(job_dir, suffix):
        calls.append((job_dir, suffix))

    timelapse._render_job = record_render
    asyncio.run(timelapse._recover_jobs(["job-a", "job-b"]))

    assert calls == [
        ("job-a", "-recovered"),
        ("job-b", "-recovered"),
    ]


def test_recovery_jobs_continue_after_unexpected_error():
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.render_running = False
    calls = []

    async def render(job_dir, suffix):
        calls.append((job_dir, suffix))
        if job_dir == "broken-job":
            timelapse.render_running = True
            raise RuntimeError("bad metadata")

    timelapse._render_job = render
    asyncio.run(
        timelapse._recover_jobs(["broken-job", "healthy-job"])
    )

    assert calls == [
        ("broken-job", "-recovered"),
        ("healthy-job", "-recovered"),
    ]
    assert timelapse.render_running is False


class _RecordingTaskEventLoop:
    """Stands in for Server.get_event_loop() for _reconcile_tmp_dir: records
    the coroutine passed to create_task() instead of actually scheduling it,
    so the test can drive it manually after patching _render_job."""

    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro)
        return coro


def test_end_job_renders_only_when_autorender_enabled(tmp_path):
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.server = type("S", (), {})()
    eventloop = _RecordingTaskEventLoop()
    timelapse.server.get_event_loop = lambda: eventloop
    rendered = []

    async def render(job_dir, suffix):
        rendered.append((job_dir, suffix))

    timelapse._render_job = render
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(json.dumps({"gcode_file": "part.gcode"}))

    timelapse.config = {"autorender": True}
    timelapse.current_job = {"dir": str(job_dir)}
    asyncio.run(timelapse._end_job())
    asyncio.run(eventloop.tasks.pop())
    assert rendered == [(str(job_dir), "")]
    assert timelapse.current_job is None

    rendered.clear()
    timelapse.config = {"autorender": False}
    timelapse.current_job = {"dir": str(job_dir)}
    asyncio.run(timelapse._end_job())
    assert eventloop.tasks == []
    assert rendered == []
    assert json.loads((job_dir / "job.json").read_text())["completed"] is True


class _SlowShellCommand:
    def __init__(self, cmd):
        self.dest = cmd.split()[-1]

    async def run(self, **kwargs):
        # Yield so a second capture can start before this one finishes,
        # like a real curl subprocess.
        await asyncio.sleep(0.01)
        with open(self.dest, "wb") as f:
            f.write(b"jpg")
        return True


class _RecordingShellServer:
    def lookup_component(self, name):
        assert name == "shell_command"
        return self

    def build_shell_command(self, cmd, **kwargs):
        return _SlowShellCommand(cmd)


def test_overlapping_captures_get_distinct_frame_numbers(tmp_path):
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.capture_lock = asyncio.Lock()
    timelapse.current_job = {
        "dir": str(tmp_path), "framecount": 0, "lastframefile": "",
    }
    timelapse.server = _RecordingShellServer()

    async def snapshot_url():
        return "http://snapshot"

    timelapse._get_snapshot_url = snapshot_url
    timelapse._notify = lambda result: None

    async def capture_twice():
        await asyncio.gather(timelapse._newframe(), timelapse._newframe())

    asyncio.run(capture_twice())

    assert timelapse.current_job["framecount"] == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "frame_000001.jpg", "frame_000002.jpg",
    ]


class _KlippyApisStub:
    def __init__(self, print_stats):
        self._print_stats = print_stats

    async def query_objects(self, objs):
        return {"print_stats": self._print_stats}


def test_reconcile_tmp_dir_recovers_older_same_filename_job(tmp_path):
    # Two unfinished jobs for the same gcode file: the newest one belongs
    # to the running print, and the older one is rendered as a crashed job.
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.tmp_dir = str(tmp_path)
    timelapse.current_job = None
    timelapse.klippy_apis = _KlippyApisStub(
        {"state": "printing", "filename": "part.gcode"}
    )
    eventloop = _RecordingTaskEventLoop()

    class FakeServer:
        def get_event_loop(self):
            return eventloop

    timelapse.server = FakeServer()

    older_dir = tmp_path / "part-20260101_000000"
    older_dir.mkdir()
    (older_dir / "job.json").write_text(json.dumps({
        "gcode_file": "part.gcode", "start_time": 1000.0
    }))

    newer_dir = tmp_path / "part-20260102_000000"
    newer_dir.mkdir()
    (newer_dir / "job.json").write_text(json.dumps({
        "gcode_file": "part.gcode", "start_time": 2000.0
    }))

    asyncio.run(timelapse._reconcile_tmp_dir())

    # The newer job is attached to the running print.
    assert timelapse.current_job is not None
    assert timelapse.current_job["dir"] == str(newer_dir)

    # The older job is queued for crash recovery.
    assert len(eventloop.tasks) == 1
    recovered = []

    async def fake_render_job(job_dir, suffix):
        recovered.append((job_dir, suffix))

    timelapse._render_job = fake_render_job
    asyncio.run(eventloop.tasks[0])

    assert recovered == [(str(older_dir), "-recovered")]


def test_render_state_resets_after_malformed_extra_params(tmp_path):
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.render_running = False
    timelapse.render_progress = 0
    timelapse.current_job = None
    timelapse.out_dir = str(tmp_path)
    timelapse.ffmpeg_binary_path = str(tmp_path / "ffmpeg")
    (tmp_path / "ffmpeg").touch()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "frame_000001.jpg").touch()
    timelapse.config = dict(DEFAULT_SETTINGS)
    timelapse.config["duplicatelastframe"] = 0
    timelapse.config["extraoutputparams"] = "'unterminated"

    with pytest.raises(ValueError):
        asyncio.run(timelapse._render_job(str(job_dir), ""))

    assert timelapse.render_running is False


class _SettingsRequest:
    def __init__(self, args):
        self._args = args

    def get_request_type(self):
        return RequestType.POST

    def get_args(self):
        return self._args

    def get_str(self, key, default=None):
        return self._args.get(key, default)


class _ServerError(Exception):
    def __init__(self, message, status_code=500):
        super().__init__(message)
        self.status_code = status_code


def test_settings_post_rejects_unsplittable_extra_params():
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.config = dict(DEFAULT_SETTINGS)
    timelapse.config["blockedsettings"] = []
    timelapse.server = type("S", (), {"error": _ServerError})()

    request = _SettingsRequest({
        "output_framerate": 24, "extraoutputparams": "'unterminated",
    })
    with pytest.raises(_ServerError) as excinfo:
        asyncio.run(timelapse._handle_settings(request))

    assert excinfo.value.status_code == 400
    # Nothing from the rejected request was applied.
    assert timelapse.config["output_framerate"] == DEFAULT_SETTINGS[
        "output_framerate"]
    assert timelapse.config["extraoutputparams"] == ""


def test_render_output_name_cannot_leave_the_output_directory(tmp_path):
    timelapse = Timelapse.__new__(Timelapse)
    timelapse.render_progress = 0
    timelapse.current_job = None
    timelapse.out_dir = str(tmp_path / "out")
    (tmp_path / "out").mkdir()
    timelapse.ffmpeg_binary_path = str(tmp_path / "ffmpeg")
    (tmp_path / "ffmpeg").touch()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "frame_000001.jpg").touch()
    timelapse.config = dict(DEFAULT_SETTINGS)
    timelapse.config["duplicatelastframe"] = 0
    timelapse.config["time_format_code"] = "../../escape/%Y"
    commands = []

    class _Shell:
        def lookup_component(self, name):
            return self

        def build_shell_command(self, cmd, **kwargs):
            commands.append(cmd)
            raise RuntimeError("stop before running ffmpeg")

    timelapse.server = _Shell()
    timelapse._notify = lambda result: None

    with pytest.raises(RuntimeError):
        asyncio.run(timelapse._render_job_impl(str(job_dir), ""))

    assert commands
    output = commands[0].split()[-1]
    assert os.path.dirname(output) == str(job_dir)
