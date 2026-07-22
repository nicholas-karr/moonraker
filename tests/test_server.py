from __future__ import annotations
import pytest
import pytest_asyncio
import asyncio
import socket
import sys
import pathlib

from moonraker.server import CORE_COMPONENTS, Server, API_VERSION
from moonraker.server import main as servermain
from moonraker.eventloop import EventLoop
from moonraker.loghelper import LogManager
from moonraker.utils import ServerError
from moonraker.confighelper import ConfigError
from moonraker.components.klippy_apis import KlippyAPI
from mocks import MockComponent, MockWebsocket
from conftest import build_app_args

from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Dict,
    Optional
)

if TYPE_CHECKING:
    from fixtures import HttpClient, WebsocketClient

@pytest.mark.run_paths(moonraker_conf="invalid_config.conf")
@pytest.mark.asyncio
async def test_invalid_config(path_args: Dict[str, pathlib.Path]):
    # EventLoop() requires a running asyncio loop at construction time
    # (it calls asyncio.get_running_loop() internally).
    evtloop = EventLoop()
    args = build_app_args(path_args)
    with pytest.raises(ConfigError):
        Server(args, None, evtloop)

@pytest.mark.asyncio
async def test_config_and_log_warnings(path_args: Dict[str, pathlib.Path]):
    # Unlike test_invalid_config, this config parses successfully, so
    # Server.__init__() proceeds past _parse_config() and into
    # add_log_rollover_item(), which needs a real LogManager (None is only
    # safe when a ConfigError aborts __init__() before that point).
    evtloop = EventLoop()
    expected = ["Log Warning Test", "Config Warning Test"]
    startup_warnings = list(expected)
    args = build_app_args(path_args, startup_warnings=startup_warnings)
    log_manager = LogManager(args, startup_warnings)
    server = Server(args, log_manager, evtloop)
    assert server.get_warnings() == expected

@pytest.mark.run_paths(moonraker_conf="unparsed_server.conf")
@pytest.mark.asyncio
async def test_unparsed_config_items(full_server: Server):
    expected_warnings = [
        "Unparsed config section [machine unparsed] detected.",
        "Unparsed config option 'unknown_option: True' detected "
        "in section [server]."]
    warn_cnt = 0
    for warn in full_server.get_warnings():
        for expected in expected_warnings:
            if warn.startswith(expected):
                warn_cnt += 1
    assert warn_cnt == 2

@pytest.mark.run_paths(moonraker_log="moonraker.log")
@pytest.mark.asyncio
async def test_file_logger(base_server: Server,
                           path_args: Dict[str, pathlib.Path]):
    log_path = path_args.get("moonraker.log", None)
    assert log_path is not None and log_path.exists()

def test_signal_handler(base_server: Server,
                        event_loop: asyncio.AbstractEventLoop):
    # Nothing calls event_loop.stop(), so a plain run_forever() would hang
    # forever; run_until_exit() resolves once _stop_server() (scheduled by
    # _handle_term_signal()) sets app_running_evt, giving the loop a
    # natural stopping point (mirrors how launch_server() drives Server in
    # moonraker/server.py's main()).
    base_server._handle_term_signal()
    event_loop.run_until_complete(base_server.run_until_exit())
    assert base_server.exit_reason == "terminate"

class TestInstantiation:
    def test_running(self, base_server: Server):
        assert base_server.is_running() is False

    def test_app_args(self,
                      path_args: Dict[str, pathlib.Path],
                      base_server: Server):
        # get_app_args() returns the full app_args dict (data_path,
        # instance_uuid, launch_args, etc - see build_app_args() in
        # conftest.py), so only the values sourced from path_args/this
        # fixture are checked here rather than the whole dict.
        app_args = base_server.get_app_args()
        assert app_args['config_file'] == str(path_args['moonraker.conf'])
        assert app_args['log_file'] == str(path_args.get("moonraker.log", ""))
        assert app_args['software_version'] == "moonraker-pytest"

    def test_api_version(self, base_server: Server):
        ver = base_server.get_api_version()
        assert ver == API_VERSION

    def test_pending_tasks(self, base_server: Server):
        loop = base_server.get_event_loop().aioloop
        assert len(asyncio.all_tasks(loop)) == 0

    def test_klippy_info(self, base_server: Server):
        assert base_server.get_klippy_info() == {}

    def test_klippy_state(self, base_server: Server):
        assert str(base_server.klippy_connection.state) == "disconnected"

    def test_host_info(self, base_server: Server):
        hinfo = {
            'hostname': socket.gethostname(),
            'address': "0.0.0.0",
            'port': 7010,
            'ssl_port': 7011
        }
        assert base_server.get_host_info() == hinfo

    def test_klippy_connection(self, base_server: Server):
        assert base_server.klippy_connection.is_connected() is False

    def test_components(self, base_server: Server):
        # "secrets" and "template" are loaded as a side effect of parsing
        # the config (see confighelper.py's gettemplate(), which lazily
        # loads the "template" component - itself dependent on "secrets" -
        # the first time any option value needs template substitution).
        key_list = sorted(list(base_server.components.keys()))
        assert key_list == [
            "application",
            "internal_transport",
            "jsonrpc",
            "klippy_connection",
            "secrets",
            "template",
            "websockets",
        ]

    def test_endpoint_registered(self, base_server: Server):
        app = base_server.moonraker_app
        assert "/server/info" in app.registered_base_handlers

    @pytest.mark.asyncio
    async def test_notification(self, base_server: Server):
        base_server.register_notification("test:test_event")
        fut = base_server.event_loop.create_future()
        wsm = base_server.lookup_component("websockets")
        wsm.clients[1] = MockWebsocket(fut)
        base_server.send_event("test:test_event", "test")
        ret = await fut
        # notify_clients() (moonraker/components/websockets.py) assigns the
        # raw *args tuple straight to msg['params'] - it stays a tuple here
        # because this test captures the in-memory message via MockWebsocket
        # rather than round-tripping it through real JSON serialization
        # (which would turn it into a list on the wire).
        expected = {
            'jsonrpc': "2.0",
            'method': "notify_test_event",
            'params': ("test",)
        }
        assert expected == ret

class TestLoadComponent:
    def test_load_component_fail(self, base_server: Server):
        # load_component() re-raises whatever exception importing the
        # component module produced (see the bare `raise` in its except
        # block) rather than wrapping it in a ServerError.
        with pytest.raises(ModuleNotFoundError):
            base_server.load_component(
                base_server.config, "invalid_component")

    def test_failed_component_set(self, base_server: Server):
        assert "invalid_component" in base_server.failed_components

    def test_load_component_fail_with_default(self, base_server: Server):
        # Once a component name is in failed_components, load_component()
        # always raises ServerError rather than falling back to `default`
        # (see its "previously failed to load" check), so the
        # default-swallowing path only applies to a component that hasn't
        # already failed.
        comp = base_server.load_component(
            base_server.config, "another_invalid_component", None)
        assert comp is None

    def test_lookup_failed(self, base_server: Server):
        with pytest.raises(ServerError):
            base_server.lookup_component("invalid_component")

    def test_lookup_failed_with_default(self, base_server: Server):
        comp = base_server.lookup_component("invalid_component", None)
        assert comp is None

    def test_load_component(self, base_server: Server):
        comp = base_server.load_component(base_server.config, "klippy_apis")
        assert isinstance(comp, KlippyAPI)

    def test_lookup_component(self, base_server: Server):
        comp = base_server.lookup_component('klippy_apis')
        assert isinstance(comp, KlippyAPI)

    def test_component_attr(self, base_server: Server):
        key_list = sorted(list(base_server.components.keys()))
        assert key_list == [
            "application",
            "internal_transport",
            "jsonrpc",
            "klippy_apis",
            "klippy_connection",
            "secrets",
            "template",
            "websockets",
        ]

class TestCoreServer:
    @pytest_asyncio.fixture(scope="class")
    async def core_server(self, base_server: Server) -> AsyncIterator[Server]:
        base_server.load_components()
        yield base_server
        await base_server._stop_server("terminate")

    def test_running(self, core_server: Server):
        assert core_server.is_running() is False

    def test_http_servers(self, core_server: Server):
        app = core_server.lookup_component("application")
        assert (
            app.http_server is None and
            app.secure_server is None
        )

    def test_warnings(self, core_server: Server):
        assert len(core_server.warnings) == 0

    def test_failed_components(self, core_server: Server):
        assert len(core_server.failed_components) == 0

    def test_lookup_components(self, core_server: Server):
        comps = []
        for comp_name in CORE_COMPONENTS:
            comps.append(core_server.lookup_component(comp_name, None))
        assert None not in comps

    def test_pending_tasks(self, core_server: Server):
        loop = core_server.get_event_loop().aioloop
        assert len(asyncio.all_tasks(loop)) == 0

    def test_register_component_fail(self, core_server: Server):
        with pytest.raises(ServerError):
            core_server.register_component("machine", object())

    def test_register_remote_method(self, core_server: Server):
        core_server.register_remote_method("moonraker_test", lambda: None)
        kconn = core_server.klippy_connection
        assert "moonraker_test" in kconn.remote_methods

    def test_register_method_exists(self, core_server: Server):
        with pytest.raises(ServerError):
            core_server.register_remote_method(
                "shutdown_machine", lambda: None)

class TestServerInit:
    def test_running(self, full_server: Server):
        assert full_server.is_running() is False

    def test_http_servers(self, full_server: Server):
        app = full_server.lookup_component("application")
        assert (
            app.http_server is None and
            app.secure_server is None
        )

    def test_warnings(self, full_server: Server):
        assert len(full_server.warnings) == 0

    def test_failed_components(self, full_server: Server):
        assert len(full_server.failed_components) == 0

    def test_lookup_components(self, full_server: Server):
        comps = []
        for comp_name in CORE_COMPONENTS:
            comps.append(full_server.lookup_component(comp_name, None))
        assert None not in comps

    def test_config_backup(self,
                           full_server: Server,
                           path_args: Dict[str, pathlib.Path]):
        cfg = path_args["config_path"].joinpath(".moonraker.conf.bkp")
        assert cfg.is_file()

class TestServerStart:
    @pytest_asyncio.fixture(scope="class")
    async def server(self, full_server: Server) -> Server:
        await full_server.start_server(connect_to_klippy=False)
        return full_server

    def test_running(self, server: Server):
        assert server.is_running() is True

    def test_http_servers(self, server: Server):
        app = server.lookup_component("application")
        assert (
            app.http_server is not None and
            app.secure_server is None
        )

@pytest.mark.run_paths(moonraker_conf="base_server_ssl.conf")
class TestSecureServerStart:
    @pytest_asyncio.fixture(scope="class")
    async def server(self, full_server: Server) -> Server:
        await full_server.start_server(connect_to_klippy=False)
        return full_server

    def test_running(self, server: Server):
        assert server.is_running() is True

    def test_http_servers(self, server: Server):
        app = server.lookup_component("application")
        assert (
            app.http_server is not None and
            app.secure_server is not None
        )

@pytest.mark.asyncio
async def test_component_init_error(base_server: Server):
    # server_init() unconditionally looks up "file_manager" to start its
    # file observer, so the core/optional components must be loaded first.
    base_server.load_components()
    base_server.register_component("testcomp", MockComponent(err_init=True))
    await base_server.server_init(False)
    assert "testcomp" in base_server.failed_components

@pytest.mark.asyncio
async def test_component_exit_error(base_server: Server,
                                    caplog: pytest.LogCaptureFixture):
    base_server.register_component("testcomp", MockComponent(err_exit=True))
    await base_server._stop_server("terminate")
    expected = "Error executing 'on_exit()' for component: testcomp"
    assert expected in caplog.messages

@pytest.mark.asyncio
async def test_component_close_error(base_server: Server,
                                     caplog: pytest.LogCaptureFixture):
    base_server.register_component("testcomp", MockComponent(err_close=True))
    await base_server._stop_server("terminate")
    expected = "Error executing 'close()' for component: testcomp"
    assert expected in caplog.messages

def test_register_event(base_server: Server):
    def test_func():
        pass
    base_server.register_event_handler("test:my_test", test_func)
    assert base_server.events["test:my_test"] == [test_func]

def test_register_async_event(base_server: Server):
    async def test_func():
        pass
    base_server.register_event_handler("test:my_test", test_func)
    assert base_server.events["test:my_test"] == [test_func]

@pytest.mark.asyncio
async def test_send_event(full_server: Server):
    evtloop = full_server.get_event_loop()
    fut = evtloop.create_future()

    def test_func(arg):
        fut.set_result(arg)
    full_server.register_event_handler("test:my_test", test_func)
    full_server.send_event("test:my_test", "test")
    result = await fut
    assert result == "test"

@pytest.mark.asyncio
async def test_send_async_event(full_server: Server):
    evtloop = full_server.get_event_loop()
    fut = evtloop.create_future()

    async def test_func(arg):
        fut.set_result(arg)
    full_server.register_event_handler("test:my_test", test_func)
    full_server.send_event("test:my_test", "test")
    result = await fut
    assert result == "test"

@pytest.mark.asyncio
async def test_register_remote_method_running(full_server: Server):
    await full_server.start_server(connect_to_klippy=False)
    with pytest.raises(ServerError):
        full_server.register_remote_method(
            "moonraker_test", lambda: None)

def _set_main_argv(monkeypatch: pytest.MonkeyPatch,
                   path_args: Dict[str, pathlib.Path]) -> None:
    # main() parses real argv via argparse (it no longer accepts an
    # injected args namespace), so tests drive it through sys.argv.
    # -d pins data_path to the test's tmp dir (the default "~/printer_data"
    # would otherwise touch the real home directory), -n disables the
    # file logger, and -c points at the config under test.
    cfg_path = path_args["moonraker.conf"]
    monkeypatch.setattr(sys, "argv", [
        "moonraker",
        "-d", str(path_args["temp_path"]),
        "-c", str(cfg_path),
        "-n",
    ])

@pytest.mark.usefixtures("event_loop")
def test_main(path_args: Dict[str, pathlib.Path],
              monkeypatch: pytest.MonkeyPatch,
              capsys: pytest.CaptureFixture):
    # main() constructs its own LogManager, which unconditionally strips
    # every existing handler off the root logger (see LogManager.__init__
    # in moonraker/loghelper.py) - including pytest's caplog handler - and
    # replaces it with a queue listener that writes formatted records to
    # stdout. So anything logged after that point (which is everything
    # this test cares about) only shows up in captured stdout, not caplog.
    tries = [1]

    async def mock_init(self: Server):
        reason = "terminate"
        if tries:
            reason = "restart"
            tries.pop(0)
        self.event_loop.delay_callback(.01, self._stop_server, reason)
    _set_main_argv(monkeypatch, path_args)
    monkeypatch.setattr(Server, "server_init", mock_init)
    code: Optional[int] = None
    try:
        servermain()
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    assert (
        code == 0 and
        "Attempting Server Restart..." in out and
        out.strip().splitlines()[-1].endswith("Server Shutdown")
    )

@pytest.mark.run_paths(moonraker_conf="invalid_config.conf")
def test_main_config_error(path_args: Dict[str, pathlib.Path],
                           monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture):
    _set_main_argv(monkeypatch, path_args)
    try:
        servermain()
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    assert code == 1 and "Server Config Error" in out

@pytest.mark.run_paths(moonraker_conf="invalid_config.conf",
                       moonraker_bkp=".moonraker.conf.bkp")
@pytest.mark.usefixtures("event_loop")
def test_main_restore_config(path_args: Dict[str, pathlib.Path],
                             monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture):
    async def mock_init(self: Server):
        reason = "terminate"
        self.event_loop.delay_callback(.01, self._stop_server, reason)

    _set_main_argv(monkeypatch, path_args)
    monkeypatch.setattr(Server, "server_init", mock_init)
    code: Optional[int] = None
    try:
        servermain()
    except SystemExit as e:
        code = e.code
    # launch_server() logs the config error, then on a successful retry
    # against the backup config appends this warning describing the
    # fallback (see the ConfigError branch in moonraker/server.py's
    # launch_server()).
    out = capsys.readouterr().out
    assert (
        code == 0 and
        "Loading most recent working configuration:" in out
    )

class TestEndpoints:
    @pytest_asyncio.fixture(scope="class")
    async def server(self, full_server: Server):
        await full_server.start_server()
        yield full_server

    @pytest.mark.asyncio
    async def test_http_server_info(self,
                                    server: Server,
                                    http_client: HttpClient):
        ret = await http_client.get("/server/info")
        comps = list(server.components.keys())
        expected = {
            'klippy_connected': False,
            'klippy_state': "disconnected",
            'components': comps,
            'failed_components': [],
            'registered_directories': ["config", "logs", "gcodes"],
            'warnings': [],
            'websocket_count': 0,
            'moonraker_version': "moonraker-pytest",
            'missing_klippy_requirements': [],
            'api_version': list(API_VERSION),
            'api_version_string': ".".join(str(v) for v in API_VERSION)
        }
        assert ret["result"] == expected

    @pytest.mark.asyncio
    async def test_http_server_config(self,
                                      server: Server,
                                      http_client: HttpClient):
        cfg = server.config.get_parsed_config()
        ret = await http_client.get("/server/config")
        assert ret["result"]["config"] == cfg

    @pytest.mark.asyncio
    async def test_websocket_server_info(self,
                                         server: Server,
                                         websocket_client: WebsocketClient):
        ret = await websocket_client.request("server.info")
        comps = list(server.components.keys())
        expected = {
            'klippy_connected': False,
            'klippy_state': "disconnected",
            'components': comps,
            'failed_components': [],
            'registered_directories': ["config", "logs", "gcodes"],
            'warnings': [],
            'websocket_count': 1,
            'moonraker_version': "moonraker-pytest",
            'missing_klippy_requirements': [],
            'api_version': list(API_VERSION),
            'api_version_string': ".".join(str(v) for v in API_VERSION)
        }
        assert ret == expected

    @pytest.mark.asyncio
    async def test_websocket_server_config(self,
                                           server: Server,
                                           websocket_client: WebsocketClient):
        cfg = server.config.get_parsed_config()
        ret = await websocket_client.request("server.config")
        assert ret["config"] == cfg

def test_server_restart(base_server: Server,
                        http_client: HttpClient,
                        event_loop: asyncio.AbstractEventLoop):
    result = {}

    async def do_restart():
        base_server.load_components()
        await base_server.start_server()
        ret = await http_client.post("/server/restart")
        result.update(ret)
    event_loop.create_task(do_restart())
    event_loop.run_until_complete(base_server.run_until_exit())
    assert result["result"] == "ok" and base_server.exit_reason == "restart"

@pytest.mark.no_ws_connect
def test_websocket_restart(base_server: Server,
                           websocket_client: WebsocketClient,
                           event_loop: asyncio.AbstractEventLoop):
    result = {}

    async def do_restart():
        base_server.load_components()
        await base_server.start_server()
        await websocket_client.connect()
        ret = await websocket_client.request("server.restart")
        result["result"] = ret
    event_loop.create_task(do_restart())
    event_loop.run_until_complete(base_server.run_until_exit())
    assert result["result"] == "ok" and base_server.exit_reason == "restart"


# TODO:
# test invalid cert, key (probably should do that in test_app.py)
