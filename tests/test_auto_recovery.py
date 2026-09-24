from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Dict, List, Optional

import pytest
from moonraker.common import KlippyState
from moonraker.components.auto_recovery import AutoRecoveryComponent
from moonraker.server import Server
from moonraker.utils import ServerError


class _JobState:
    def __init__(self, state: str = "standby") -> None:
        self.last_print_stats = {"state": state}


class _Database:
    """In-memory stand-in for the two MoonrakerDatabase calls the component
    makes, keyed by (namespace, key)."""

    def __init__(self) -> None:
        self.records: Dict[Any, Any] = {}

    async def get_item(self, namespace: str, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self.records.get((namespace, key), default))

    async def insert_item(self, namespace: str, key: str, value: Any) -> None:
        self.records[(namespace, key)] = copy.deepcopy(value)


class _PrinterServices:
    def __init__(self, started: bool = True) -> None:
        self.calls = 0
        self.started = started

    async def trigger_restart_all(self) -> bool:
        self.calls += 1
        return self.started


class _Machine:
    def __init__(self) -> None:
        self.commands: List[str] = []

    async def exec_sudo_command(self, command: str, timeout: float = 2.0) -> str:
        self.commands.append(command)
        return ""


class _KlippyAPI:
    def __init__(self, link_error: Optional[str]) -> None:
        self.link_error = link_error

    async def query_objects(self, objects: Dict[str, Any]) -> Dict[str, Any]:
        return {"image_display": {"projector_link_error": self.link_error}}


class _KlippyConnection:
    def __init__(
        self,
        state: KlippyState,
        state_message: str = "Lost communication with MCU 'mcu'",
    ) -> None:
        self.state = state
        self.state_message = state_message


class _RecordingLoop:
    """Records delay_callback() instead of scheduling it, so a test can run
    the recheck itself."""

    def __init__(self) -> None:
        self.delayed: List[tuple] = []

    def delay_callback(self, delay: float, callback: Any) -> None:
        self.delayed.append((delay, callback))


class _FakeServer:
    """Minimal Server stand-in, same pattern test_firmware_build.py uses for
    _check_not_printing(): enough lookup_component()/error() to exercise the
    component's logic without spinning up a full async Server."""

    def __init__(
        self,
        job_state: Any = None,
        printer_services: Any = None,
        machine: Any = None,
        klippy_apis: Any = None,
        klippy_state: KlippyState = KlippyState.SHUTDOWN,
        state_message: str = "Lost communication with MCU 'mcu'",
    ) -> None:
        self.database = _Database()
        self.loop = _RecordingLoop()
        self._components = {
            "job_state": job_state,
            "printer_services": printer_services,
            "machine": machine,
            "klippy_apis": klippy_apis,
            "klippy_connection": _KlippyConnection(
                klippy_state, state_message
            ),
            "database": self.database,
        }
        self.events: List[tuple] = []

    def lookup_component(self, name: str, default: Any = None) -> Any:
        if name in self._components:
            comp = self._components[name]
            return comp if comp is not None else default
        return default

    def error(self, msg: str) -> ServerError:
        return ServerError(msg)

    def send_event(self, name: str, *args: Any) -> None:
        self.events.append((name, args))

    def register_notification(self, name: str) -> None:
        pass

    def register_event_handler(self, name: str, handler: Any) -> None:
        pass

    def get_event_loop(self) -> Any:
        return self.loop


def _component(server: _FakeServer, **overrides: Any) -> AutoRecoveryComponent:
    component = AutoRecoveryComponent.__new__(AutoRecoveryComponent)
    component.server = server
    component.mcu_recovery_enabled = True
    component.mcu_max_attempts = 2
    component.mcu_cooldown = 120.0
    component.projector_recovery_enabled = False
    component.projector_port_path = None
    component.projector_hub_path = None
    component.projector_check_interval = 300.0
    component._projector_probe_started = False
    component._mcu_lock = asyncio.Lock()
    component._mcu_recheck_pending = False
    for k, v in overrides.items():
        setattr(component, k, v)
    return component


@pytest.mark.run_paths(moonraker_conf="biokalico_components.conf")
class TestAutoRecoveryLoads:
    def test_component_loads(self, full_server: Server):
        comp = full_server.lookup_component("auto_recovery")
        assert isinstance(comp, AutoRecoveryComponent)
        assert comp.mcu_recovery_enabled is True
        assert comp.projector_recovery_enabled is False

    @pytest.mark.asyncio
    async def test_state_round_trips_through_the_real_database(
        self, full_server: Server
    ):
        comp = full_server.lookup_component("auto_recovery")
        assert await comp._load_state("mcu_recovery") == {}

        await comp._save_state("mcu_recovery", {"attempt": 1, "gave_up": False})

        assert await comp._load_state("mcu_recovery") == {
            "attempt": 1,
            "gave_up": False,
        }
        # A different key is a different record, not a shared blob.
        assert await comp._load_state("projector_recovery") == {}


# _not_printing has the same fail-closed contract as firmware_build's version.
@pytest.mark.asyncio
async def test_not_printing_fails_closed_when_job_state_unavailable():
    component = _component(_FakeServer(job_state=None))
    assert await component._not_printing() is False


@pytest.mark.asyncio
async def test_not_printing_true_when_idle():
    component = _component(
        _FakeServer(job_state=_JobState("standby"))
    )
    assert await component._not_printing() is True


@pytest.mark.asyncio
async def test_not_printing_false_when_printing():
    component = _component(
        _FakeServer(job_state=_JobState("printing"))
    )
    assert await component._not_printing() is False


@pytest.mark.asyncio
async def test_not_printing_false_when_paused():
    component = _component(_FakeServer(job_state=_JobState("paused")))
    assert await component._not_printing() is False


@pytest.mark.asyncio
async def test_klippy_shutdown_does_nothing_while_printing():
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("printing"), printer_services=services
    )
    component = _component(server)

    await component._on_klippy_shutdown()

    assert services.calls == 0
    assert any("not attempting automatic recovery" in e[1][0] for e in server.events)


@pytest.mark.asyncio
async def test_klippy_shutdown_triggers_restart_all_when_idle():
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("standby"), printer_services=services
    )
    component = _component(server)

    await component._on_klippy_shutdown()

    assert services.calls == 1
    assert server.database.records[("auto_recovery", "mcu_recovery")]["attempt"] == 1


@pytest.mark.asyncio
async def test_klippy_shutdown_respects_cooldown():
    """A second shutdown event landing right after the first must not
    re-trigger a restart that's already in flight."""
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("standby"), printer_services=services
    )
    component = _component(server, mcu_cooldown=120.0)

    await component._on_klippy_shutdown()
    await component._on_klippy_shutdown()
    await component._on_klippy_shutdown()

    assert services.calls == 1
    # The suppressed events are not lost: one recheck is scheduled for when
    # the cooldown ends, however many events landed inside it.
    assert len(server.loop.delayed) == 1


@pytest.mark.asyncio
async def test_klippy_shutdown_gives_up_after_max_attempts():
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("standby"), printer_services=services
    )
    component = _component(server, mcu_max_attempts=2, mcu_cooldown=0.0)

    await component._on_klippy_shutdown()
    await component._on_klippy_shutdown()
    # A third incident, once the attempt count is already at the cap.
    await component._on_klippy_shutdown()

    assert services.calls == 2
    assert any("giving up" in e[1][0] for e in server.events)

    # It must say so exactly once, not on every subsequent shutdown.
    events_before = len(server.events)
    await component._on_klippy_shutdown()
    new_give_up_messages = [
        e for e in server.events[events_before:] if "giving up" in e[1][0]
    ]
    assert new_give_up_messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "Shutdown due to M112 command\nOnce the underlying issue is corrected",
    "Shutdown due to webhooks request",
    "Shutdown due to filament runout",
])
async def test_deliberate_shutdown_is_not_recovered(message):
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("standby"),
        printer_services=services,
        state_message=message,
    )
    component = _component(server)

    await component._on_klippy_shutdown()
    await component._on_klippy_started(KlippyState.SHUTDOWN)

    assert services.calls == 0
    assert server.events == []
    assert server.loop.delayed == []


@pytest.mark.asyncio
async def test_klippy_started_shut_down_triggers_restart_all():
    """Moonraker sends no klippy_shutdown for a Klipper that connects while
    already shut down, so klippy_started has to cover that case."""
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("standby"), printer_services=services
    )
    component = _component(server)

    await component._on_klippy_started(KlippyState.READY)
    assert services.calls == 0

    await component._on_klippy_started(KlippyState.SHUTDOWN)
    assert services.calls == 1


@pytest.mark.asyncio
async def test_recheck_after_cooldown_retries_a_failed_attempt():
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("standby"), printer_services=services
    )
    component = _component(server, mcu_max_attempts=2, mcu_cooldown=120.0)

    await component._on_klippy_shutdown()
    # Klipper comes back shut down while the first attempt is cooling down.
    await component._on_klippy_started(KlippyState.SHUTDOWN)
    assert services.calls == 1
    delay, recheck = server.loop.delayed[0]
    assert 0 < delay <= 121.0

    # The cooldown ends. Klipper is still shut down, so try again.
    key = ("auto_recovery", "mcu_recovery")
    server.database.records[key]["triggered_at"] -= 200.0
    await recheck()
    assert services.calls == 2
    assert server.database.records[key]["attempt"] == 2

    # Out of attempts: the next recheck must hand off to a human.
    server.database.records[key]["triggered_at"] -= 200.0
    await component._on_klippy_started(KlippyState.SHUTDOWN)
    assert services.calls == 2
    assert any("giving up" in e[1][0] for e in server.events)


@pytest.mark.asyncio
async def test_recheck_does_nothing_once_klippy_is_no_longer_shut_down():
    services = _PrinterServices()
    server = _FakeServer(
        job_state=_JobState("standby"),
        printer_services=services,
        klippy_state=KlippyState.READY,
    )
    component = _component(server)
    component._mcu_recheck_pending = True

    await component._recheck_mcu_shutdown()

    assert component._mcu_recheck_pending is False
    assert services.calls == 0


@pytest.mark.asyncio
async def test_restart_already_running_is_not_counted_as_an_attempt():
    services = _PrinterServices(started=False)
    server = _FakeServer(
        job_state=_JobState("standby"), printer_services=services
    )
    component = _component(server)

    await component._on_klippy_shutdown()

    assert services.calls == 1
    key = ("auto_recovery", "mcu_recovery")
    assert server.database.records[key] == {}
    assert len(server.loop.delayed) == 1


@pytest.mark.asyncio
async def test_klippy_ready_after_giving_up_does_not_claim_a_recovery():
    server = _FakeServer(job_state=_JobState("standby"))
    component = _component(server)
    server.database.records[("auto_recovery", "mcu_recovery")] = {
        "attempt": 2,
        "gave_up": True,
    }

    await component._on_klippy_ready()

    assert not any("reconnected successfully" in e[1][0] for e in server.events)
    assert any("given up" in e[1][0] for e in server.events)
    assert server.database.records[("auto_recovery", "mcu_recovery")] == {}


@pytest.mark.asyncio
async def test_klippy_ready_announces_recovery_after_an_attempt():
    server = _FakeServer(job_state=_JobState("standby"))
    component = _component(server)
    server.database.records[("auto_recovery", "mcu_recovery")] = {
        "attempt": 1,
        "triggered_at": time.time(),
    }

    await component._on_klippy_ready()

    assert any("reconnected successfully" in e[1][0] for e in server.events)
    assert server.database.records[("auto_recovery", "mcu_recovery")] == {}


@pytest.mark.asyncio
async def test_klippy_ready_stays_silent_on_a_normal_boot():
    """No state file (or an attempt of 0) means nothing was ever triggered -
    a routine startup must not claim credit for a recovery that never
    happened."""
    server = _FakeServer(job_state=_JobState("standby"))
    component = _component(server)

    await component._on_klippy_ready()

    assert server.events == []


@pytest.mark.asyncio
async def test_projector_check_does_nothing_when_link_is_healthy():
    server = _FakeServer(
        job_state=_JobState("standby"),
        klippy_apis=_KlippyAPI(link_error=None),
        machine=_Machine(),
    )
    component = _component(server, projector_port_path="/sys/fake/port4")

    await component._check_projector_link()

    assert server.events == []


@pytest.mark.asyncio
async def test_projector_check_recycles_port_when_link_is_down_and_idle():
    machine = _Machine()
    server = _FakeServer(
        job_state=_JobState("standby"),
        klippy_apis=_KlippyAPI(link_error="cannot open /dev/ttyUSB0"),
        machine=machine,
    )
    component = _component(server, projector_port_path="/sys/fake/port4")

    await component._check_projector_link()

    assert len(machine.commands) == 1
    assert "/sys/fake/port4/disable" in machine.commands[0]
    proj = server.database.records[("auto_recovery", "projector_recovery")]
    assert proj["attempted"] is True


@pytest.mark.asyncio
async def test_projector_check_does_not_touch_hardware_mid_print():
    machine = _Machine()
    server = _FakeServer(
        job_state=_JobState("printing"),
        klippy_apis=_KlippyAPI(link_error="cannot open /dev/ttyUSB0"),
        machine=machine,
    )
    component = _component(server, projector_port_path="/sys/fake/port4")

    await component._check_projector_link()

    assert machine.commands == []


@pytest.mark.asyncio
async def test_projector_check_recovery_is_announced_and_clears_state():
    server = _FakeServer(
        job_state=_JobState("standby"),
        klippy_apis=_KlippyAPI(link_error=None),
    )
    component = _component(server, projector_port_path="/sys/fake/port4")
    server.database.records[("auto_recovery", "projector_recovery")] = {
        "attempted": True,
        "gave_up": True,
    }

    await component._check_projector_link()

    assert any("recovered" in e[1][0] for e in server.events)
    assert server.database.records[("auto_recovery", "projector_recovery")] == {}


@pytest.mark.asyncio
async def test_projector_hub_power_cycle_follows_a_failed_port_reset():
    machine = _Machine()
    server = _FakeServer(
        job_state=_JobState("paused"),
        klippy_apis=_KlippyAPI(link_error="cannot open /dev/ttyUSB0"),
        machine=machine,
    )
    component = _component(
        server,
        projector_port_path="/sys/fake/port4",
        projector_hub_path="/sys/fake/hub",
    )
    key = ("auto_recovery", "projector_recovery")
    server.database.records[key] = {"attempted": True, "gave_up": False}

    await component._check_projector_link()

    assert len(machine.commands) == 1
    assert "/sys/fake/hub/authorized" in machine.commands[0]
    assert server.database.records[key]["hub_attempted"] is True

    # Still down after the hub power-cycle: announce once and stop.
    await component._check_projector_link()
    await component._check_projector_link()
    assert len(machine.commands) == 1
    assert server.database.records[key]["gave_up"] is True
    assert sum("needs physical attention" in e[1][0] for e in server.events) == 1


@pytest.mark.asyncio
async def test_projector_gives_up_after_port_reset_without_a_hub_path():
    machine = _Machine()
    server = _FakeServer(
        job_state=_JobState("standby"),
        klippy_apis=_KlippyAPI(link_error="cannot open /dev/ttyUSB0"),
        machine=machine,
    )
    component = _component(server, projector_port_path="/sys/fake/port4")
    key = ("auto_recovery", "projector_recovery")
    server.database.records[key] = {"attempted": True, "gave_up": False}

    await component._check_projector_link()

    assert machine.commands == []
    assert server.database.records[key]["gave_up"] is True
