# Single shared-password login for Mainsail
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# One shared password for Mainsail instead of per-user accounts. See
# deps/mainsail's src/store/auth/, TheLoginDialog.vue and
# biokalico_extras/README.md.
#
# How it works:
#  - Registers the fixed user USERNAME with the configured password in
#    authorization's own user table. Mainsail logs in through the normal
#    /access/login endpoint.
#  - Sets force_logins and force_login_bypass_trusted on the authorization
#    component. Don't also set force_logins in moonraker.conf.
#  - local_bypass (default False) lets [authorization] trusted_clients skip
#    the password. Requests that came through a Cloudflare Tunnel still need
#    it, because they arrive from localhost (see CF_HEADERS in
#    authorization.py).
#  - Serves the login-screen hint at /server/simple_password_auth/hint, which
#    needs no login.
#  - API key requests (crowsnest, scripts) are unaffected.
#
# A blank password is replaced with a random one on first start and written
# back into the config file.
#
# A password change logs out existing sessions by clearing the user's
# jwt_secret/jwk_id, as Authorization._handle_logout does. Restarts without
# a change keep sessions.
#
# Relies on these Moonraker internals: authorization loading before this
# component (it is a core component), ConfigHelper.set_option() plus
# get_source().save(), the UserInfo fields, Authorization._sync_user(),
# Authorization.force_login_bypass_trusted, and HASH_ITER.

from __future__ import annotations

import hashlib
import secrets
from typing import Awaitable, cast, Dict, Protocol, TYPE_CHECKING

from ..common import RequestType, UserInfo
from .authorization import HASH_ITER

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest

USERNAME = "biokalico"


class _WritableConfigSource(Protocol):
    def save(self) -> Awaitable[bool]:
        ...


def _clean_config_string(value: str) -> str:
    # Moonraker keeps quote characters as part of a value, so strip one
    # layer of matching quotes.
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value.strip()


class SimplePasswordAuth:
    def __init__(self, config: ConfigHelper) -> None:
        self.config = config
        self.server = config.get_server()
        self.enable = config.getboolean("enable", True)
        # TheLoginDialog.vue trims the entered password the same way.
        self.password: str = config.get("password", "").strip()
        self.local_bypass = config.getboolean("local_bypass", False)
        self.hint: str = _clean_config_string(config.get("hint", ""))

        if self.enable:
            self.server.register_endpoint(
                "/server/simple_password_auth/hint", RequestType.GET,
                self._handle_hint_request, auth_required=False,
            )

    async def _handle_hint_request(
        self, web_request: WebRequest
    ) -> Dict[str, str]:
        return {"password_hint": self.hint}

    @staticmethod
    def _password_changed(existing: UserInfo, password: str) -> bool:
        # Hash with the old salt, since the salt changes on every restart.
        if not existing.salt or not existing.password:
            return True
        try:
            old_salt = bytes.fromhex(existing.salt)
        except ValueError:
            return True
        rehashed = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), old_salt, HASH_ITER
        ).hex()
        return not secrets.compare_digest(rehashed, existing.password)

    async def component_init(self) -> None:
        if not self.enable:
            return

        password = self.password
        if not password:
            password = secrets.token_urlsafe(9)
            self.config.set_option("password", password)
            source = cast(_WritableConfigSource, self.config.get_source())
            await source.save()

        auth = self.server.lookup_component("authorization")

        salt = secrets.token_bytes(32)
        hashed_pass = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, HASH_ITER
        ).hex()
        existing = auth.users.get(USERNAME)
        if existing is not None:
            if self._password_changed(existing, password):
                jwk_id = existing.jwk_id
                existing.jwt_secret = None
                existing.jwk_id = None
                if jwk_id is not None:
                    auth.public_jwks.pop(jwk_id, None)
            existing.password = hashed_pass
            existing.salt = salt.hex()
        else:
            auth.users[USERNAME] = UserInfo(
                username=USERNAME,
                password=hashed_pass,
                salt=salt.hex(),
                source="moonraker",
            )
        await auth._sync_user(USERNAME)

        auth.force_logins = True
        auth.force_login_bypass_trusted = self.local_bypass


def load_component(config: ConfigHelper) -> SimplePasswordAuth:
    return SimplePasswordAuth(config)
