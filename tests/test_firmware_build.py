from __future__ import annotations
import pytest
from moonraker.server import Server
from moonraker.components.firmware_build import FirmwareBuildComponent
from moonraker.common import WebRequest, RequestType
from moonraker.utils import ServerError

from typing import Any, Dict


def _make_request(args: Dict[str, Any]) -> WebRequest:
    return WebRequest("/server/firmware/build", args, RequestType.POST)


@pytest.mark.run_paths(moonraker_conf="biokalico_components.conf")
class TestFirmwareBuildEndpoints:
    def test_endpoints_registered(self, full_server: Server):
        app = full_server.moonraker_app
        expected = [
            "/server/firmware/targets",
            "/server/firmware/build",
            "/server/firmware/build_and_flash",
            "/server/firmware/status",
            "/server/firmware/cancel",
        ]
        for path in expected:
            assert path in app.registered_base_handlers

    def test_no_job_running_status(self, full_server: Server):
        comp: FirmwareBuildComponent = full_server.lookup_component("firmware_build")
        assert comp.job_active is False
        assert comp.current_cmd is None

    @pytest.mark.asyncio
    async def test_cancel_without_job(self, full_server: Server):
        comp: FirmwareBuildComponent = full_server.lookup_component("firmware_build")
        with pytest.raises(ServerError):
            await comp._handle_cancel(_make_request({}))


@pytest.mark.run_paths(moonraker_conf="biokalico_components.conf")
class TestGetTargetNames:
    # NOTE: the run_paths marker must be applied at the class level, not on
    # individual test methods: path_args/base_server/full_server are all
    # class-scoped fixtures, so the FixtureRequest they see is bound to the
    # class collector node. get_closest_marker() only climbs from that node
    # up through the module/session, so a marker placed on a method (a child
    # node) is invisible to it.
    @pytest.fixture
    def comp(self, full_server: Server) -> FirmwareBuildComponent:
        return full_server.lookup_component("firmware_build")

    ALL_TARGETS = {"mcu": {}, "toolboard": {}}

    def test_all_keyword(self, comp: FirmwareBuildComponent):
        req = _make_request({"targets": "all"})
        names = comp._get_target_names(req, self.ALL_TARGETS)
        assert sorted(names) == sorted(self.ALL_TARGETS)

    def test_comma_separated_string(self, comp: FirmwareBuildComponent):
        req = _make_request({"targets": "mcu, toolboard"})
        names = comp._get_target_names(req, self.ALL_TARGETS)
        assert names == ["mcu", "toolboard"]

    def test_unknown_target_rejected(self, comp: FirmwareBuildComponent):
        req = _make_request({"targets": "nonexistent"})
        with pytest.raises(ServerError):
            comp._get_target_names(req, self.ALL_TARGETS)

    def test_empty_targets_rejected(self, comp: FirmwareBuildComponent):
        req = _make_request({"targets": ""})
        with pytest.raises(ServerError):
            comp._get_target_names(req, self.ALL_TARGETS)


class TestVersionMatches:
    def test_exact_match(self):
        assert FirmwareBuildComponent._version_matches(
            "v1.2.3-4-abcdef", "v1.2.3-4-abcdef"
        )

    def test_prefix_match_with_timestamp_suffix(self):
        # build_version() appends "-<timestamp>-<hostname>" onto a build
        # that isn't a clean tagged checkout - that suffix never matches
        # between two separate build invocations, so a prefix match against
        # the current `git describe` output is what actually indicates the
        # same source was built.
        mcu_version = "v1.2.3-4-abcdef-20260101_120000-myhost"
        assert FirmwareBuildComponent._version_matches(
            mcu_version, "v1.2.3-4-abcdef"
        )

    def test_different_describe_does_not_match(self):
        assert not FirmwareBuildComponent._version_matches(
            "v1.2.3-4-abcdef-20260101_120000-myhost", "v1.2.4-1-fedcba"
        )

    def test_unknown_version_never_matches(self):
        assert not FirmwareBuildComponent._version_matches("?", "v1.2.3-4-abcdef")
        assert not FirmwareBuildComponent._version_matches(
            "?-20260101_120000-myhost", "v1.2.3-4-abcdef"
        )


def test_flash_check_writes_abort_before_driver_can_continue(tmp_path):
    component = FirmwareBuildComponent.__new__(FirmwareBuildComponent)
    approval = tmp_path / "approval"
    component.flash_approval_file = str(approval)
    component.job_status = {}
    component.job_error = None

    def reject_active_print():
        raise RuntimeError("print is active")

    component._check_not_printing = reject_active_print
    component._handle_output(
        b'{"target":"main","phase":"flash_check","line":"waiting"}'
    )

    assert approval.read_text() == "abort: print is active"
    assert component.job_error == "abort: print is active"


class _FakeServer:
    """Minimal stand-in for Server.lookup_component/error used to test
    _check_not_printing() without spinning up a full Server."""

    def __init__(self, job_state: Any = None) -> None:
        self._job_state = job_state

    def lookup_component(self, name: str, default: Any = None) -> Any:
        if name == "job_state":
            return self._job_state
        return default

    def error(self, msg: str) -> ServerError:
        return ServerError(msg)


def test_check_not_printing_fails_closed_when_job_state_unavailable():
    # Without job_state the print state is unknown, so refuse to flash.
    component = FirmwareBuildComponent.__new__(FirmwareBuildComponent)
    component.server = _FakeServer(job_state=None)

    with pytest.raises(ServerError):
        component._check_not_printing()


def test_check_not_printing_permits_when_job_state_idle():
    class _JobState:
        last_print_stats = {"state": "standby"}

    component = FirmwareBuildComponent.__new__(FirmwareBuildComponent)
    component.server = _FakeServer(job_state=_JobState())

    # Should not raise.
    component._check_not_printing()


def test_check_not_printing_blocks_when_printing():
    class _JobState:
        last_print_stats = {"state": "printing"}

    component = FirmwareBuildComponent.__new__(FirmwareBuildComponent)
    component.server = _FakeServer(job_state=_JobState())

    with pytest.raises(ServerError):
        component._check_not_printing()


def test_flash_decision_is_written_atomically(tmp_path):
    component = FirmwareBuildComponent.__new__(FirmwareBuildComponent)
    component.job_error = None
    approval = tmp_path / "decision"

    component._write_flash_decision(str(approval), "ok")

    assert approval.read_text() == "ok"
    # The temporary name is renamed away, so the driver never sees it.
    assert [p.name for p in tmp_path.iterdir()] == ["decision"]
    assert component.job_error is None


def test_flash_decision_write_failure_is_recorded_not_raised(tmp_path):
    component = FirmwareBuildComponent.__new__(FirmwareBuildComponent)
    component.job_error = None

    component._write_flash_decision(str(tmp_path / "missing" / "decision"), "ok")

    assert component.job_error == "unable to record flash approval decision"


def test_flash_approval_cleanup_removes_the_private_directory(tmp_path):
    component = FirmwareBuildComponent.__new__(FirmwareBuildComponent)
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    (approval_dir / "decision").write_text("ok")
    component.flash_approval_dir = str(approval_dir)
    component.flash_approval_file = str(approval_dir / "decision")

    component._cleanup_flash_approval()

    assert not approval_dir.exists()
    assert component.flash_approval_dir is None
    assert component.flash_approval_file is None
    # Cleaning up twice, as the error and finally paths both do, is harmless.
    component._cleanup_flash_approval()
