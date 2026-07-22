from __future__ import annotations
import datetime
import hashlib
import re
import pytest
import pytest_asyncio
from tornado.web import HTTPError
from moonraker.server import Server
from moonraker.components.authorization import Authorization, HASH_ITER
from moonraker.components.simple_password_auth import SimplePasswordAuth, USERNAME
from fixtures import HttpClient

PASSWORD = "testpass123"


class FakeRequest:
    # Just enough of tornado.httputil.HTTPServerRequest for
    # authenticate_request's force_login_bypass_trusted branch: an
    # empty-dict Authorization/query-token lookup so it falls straight
    # through to the force_logins check, then remote_ip/headers for the
    # bypass/Cloudflare check itself.
    def __init__(self, remote_ip: str, headers: dict | None = None):
        self.method = "GET"
        self.remote_ip = remote_ip
        self.headers = headers or {}
        self.query_arguments: dict = {}
        self.arguments: dict = {}


@pytest.mark.run_paths(moonraker_conf="biokalico_components.conf")
@pytest.mark.asyncio
class TestSimplePasswordAuth:
    async def test_force_logins_enabled(self, full_server: Server):
        auth: Authorization = full_server.lookup_component("authorization")
        assert auth.force_logins is True

    async def test_user_registered_with_working_password(self, full_server: Server):
        auth: Authorization = full_server.lookup_component("authorization")
        user = auth.users.get(USERNAME)
        assert user is not None
        expected_hash = hashlib.pbkdf2_hmac(
            "sha256", PASSWORD.encode(), bytes.fromhex(user.salt), HASH_ITER
        ).hex()
        assert user.password == expected_hash

    async def test_decode_jwt_ignores_expiry(self, full_server: Server):
        auth: Authorization = full_server.lookup_component("authorization")
        user = auth.users[USERNAME]
        # Force this user's jwt_secret/jwk_id to exist so a token can be
        # signed, the same way a real /access/login would on first login.
        if user.jwt_secret is None:
            from libnacl.sign import Signer
            private_key = Signer()
            jwk_id = "test-jwk-id"
            user.jwt_secret = private_key.hex_seed().decode()
            user.jwk_id = jwk_id
            auth.public_jwks[jwk_id] = auth._generate_public_jwk(private_key)
        else:
            private_key = auth._load_private_key(user.jwt_secret)
            jwk_id = user.jwk_id

        # A negative exp_time produces a token that is already expired the
        # instant it's minted.
        expired_token = auth._generate_jwt(
            USERNAME, jwk_id, private_key,
            exp_time=datetime.timedelta(seconds=-3600)
        )

        # Without the patch, decode_jwt(check_exp=True) would raise "JWT
        # Expired" here - the whole point of this component is that it
        # never does, for any user, since login-gate.js has no
        # refresh-token flow to fall back on.
        decoded = auth.decode_jwt(expired_token)
        assert decoded.username == USERNAME

    async def test_local_bypass_allows_trusted_ip_without_login(
        self, full_server: Server
    ):
        auth: Authorization = full_server.lookup_component("authorization")
        # 127.0.0.1 is in base_server.conf's [authorization] trusted_clients
        # and carries no Cloudflare headers - local_bypass (default True in
        # biokalico_components.conf) should grant access with no token.
        user = await auth.authenticate_request(FakeRequest("127.0.0.1"))
        assert user is not None

    async def test_cloudflare_header_blocks_bypass_for_same_trusted_ip(
        self, full_server: Server
    ):
        auth: Authorization = full_server.lookup_component("authorization")
        # Same IP as the passing case above - only the Cf-Ray header
        # differs. This is the exact scenario a cloudflared tunnel produces
        # (tunnel connects over loopback, so the IP alone can't tell a
        # remote visitor apart from someone actually on the LAN).
        request = FakeRequest("127.0.0.1", headers={"Cf-Ray": "test-ray-id"})
        with pytest.raises(HTTPError):
            await auth.authenticate_request(request)

    async def test_local_bypass_still_rejects_untrusted_ip(
        self, full_server: Server
    ):
        auth: Authorization = full_server.lookup_component("authorization")
        # Not in trusted_clients and no Cloudflare header either - confirms
        # the bypass only ever widens access for IPs already trusted by
        # [authorization], not for arbitrary remote requests.
        request = FakeRequest("203.0.113.5")
        with pytest.raises(HTTPError):
            await auth.authenticate_request(request)


@pytest.mark.run_paths(moonraker_conf="biokalico_components_blank_password.conf")
@pytest.mark.asyncio
class TestSimplePasswordAuthBlankPassword:
    async def test_blank_password_generates_and_persists_to_config_file(
        self, full_server: Server, path_args
    ):
        auth: Authorization = full_server.lookup_component("authorization")
        user = auth.users.get(USERNAME)
        assert user is not None
        assert user.password

        # The generated password must land as a plain, readable value in the
        # actual config file on disk - not hidden behind a database/API
        # lookup - so a later restart (with the file now non-blank) reads it
        # like any other option, and an admin can just `cat` the file.
        conf_path = path_args["moonraker.conf"]
        content = conf_path.read_text()
        match = re.search(r"^password:\s*(\S+)\s*$", content, re.MULTILINE)
        assert match is not None, f"no populated password line in {content!r}"
        generated_password = match.group(1)
        assert generated_password

        expected_hash = hashlib.pbkdf2_hmac(
            "sha256", generated_password.encode(), bytes.fromhex(user.salt), HASH_ITER
        ).hex()
        assert user.password == expected_hash


@pytest.mark.run_paths(moonraker_conf="biokalico_components.conf")
@pytest.mark.asyncio
class TestSimplePasswordAuthHintEndpointDefault:
    # Real HTTP requests against the actual registered endpoint, not a
    # direct method call, so a routing mistake (wrong path, wrong request
    # type, auth_required left True) fails the test instead of passing
    # silently.
    @pytest_asyncio.fixture(scope="class")
    async def server(self, full_server: Server):
        await full_server.start_server()
        yield full_server

    async def test_password_hint_blank_by_default(
        self, server: Server, http_client: HttpClient
    ):
        ret = await http_client.get("/server/simple_password_auth/hint")
        assert ret["result"]["password_hint"] == ""


@pytest.mark.run_paths(moonraker_conf="biokalico_components_with_hint.conf")
@pytest.mark.asyncio
class TestSimplePasswordAuthHintEndpointConfigured:
    @pytest_asyncio.fixture(scope="class")
    async def server(self, full_server: Server):
        await full_server.start_server()
        yield full_server

    async def test_password_hint_exposed_via_dedicated_endpoint(
        self, server: Server, http_client: HttpClient
    ):
        ret = await http_client.get("/server/simple_password_auth/hint")
        assert ret["result"]["password_hint"] == "ask nick"


@pytest.mark.run_paths(moonraker_conf="biokalico_components_with_quoted_hint.conf")
@pytest.mark.asyncio
class TestSimplePasswordAuthQuotedHint:
    # Moonraker's config parser has no quoting syntax, so `hint: "ask nick"`
    # would otherwise show up in the login screen with the quote characters
    # still literally in it - covers the same bug end to end, through the
    # real endpoint.
    @pytest_asyncio.fixture(scope="class")
    async def server(self, full_server: Server):
        await full_server.start_server()
        yield full_server

    async def test_quotes_are_stripped_from_hint(
        self, server: Server, http_client: HttpClient
    ):
        ret = await http_client.get("/server/simple_password_auth/hint")
        assert ret["result"]["password_hint"] == "ask nick"


@pytest.mark.run_paths(moonraker_conf="biokalico_components_with_empty_quoted_hint.conf")
@pytest.mark.asyncio
class TestSimplePasswordAuthEmptyQuotedHint:
    # `hint: ""` (the shipped template's default) must behave exactly like
    # `hint:` (bare/absent) - no hint shown, not a literal empty-quotes hint.
    @pytest_asyncio.fixture(scope="class")
    async def server(self, full_server: Server):
        await full_server.start_server()
        yield full_server

    async def test_empty_quoted_hint_is_treated_as_blank(
        self, server: Server, http_client: HttpClient
    ):
        ret = await http_client.get("/server/simple_password_auth/hint")
        assert ret["result"]["password_hint"] == ""


class _FakeStripConfig:
    # Just enough of ConfigHelper for SimplePasswordAuth's __init__ - a pure
    # unit test of the stripping itself, independent of whether Moonraker's
    # own config file parser happens to already strip plain single-line
    # values (it does, for the simple case - this guards the cases it
    # might not, e.g. a value arriving with embedded leading/trailing
    # whitespace some other way, and documents the behavior directly).
    class _FakeServer:
        def register_endpoint(self, *args, **kwargs) -> None:
            pass

    def __init__(self, password: str, hint: str = ""):
        self._values = {"password": password, "hint": hint}

    def get_server(self):
        return self._FakeServer()

    def getboolean(self, option: str, default: bool) -> bool:
        return default

    def get(self, option: str, default: str) -> str:
        return self._values.get(option, default)


def test_password_and_hint_are_stripped():
    comp = SimplePasswordAuth(_FakeStripConfig("  testpass123  \n", " ask nick "))
    assert comp.password == "testpass123"
    assert comp.hint == "ask nick"


def test_hint_quotes_are_stripped():
    comp = SimplePasswordAuth(_FakeStripConfig("testpass123", '"ask nick"'))
    assert comp.hint == "ask nick"

    comp = SimplePasswordAuth(_FakeStripConfig("testpass123", "'ask nick'"))
    assert comp.hint == "ask nick"

    comp = SimplePasswordAuth(_FakeStripConfig("testpass123", '""'))
    assert comp.hint == ""
