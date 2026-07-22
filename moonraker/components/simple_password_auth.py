# Single shared-password login for Mainsail, with sessions that never expire
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# Gates Mainsail behind one shared password (not per-user accounts), with a
# session that never expires on the browser side, EXCEPT for requests
# already trusted by IP (see "local_bypass" below), which need no password
# at all. See deps/mainsail's src/store/auth/ + TheLoginDialog.vue (the
# matching Mainsail-side login UI) and biokalico_extras/README.md.
#
# Why a whole component instead of just flipping [authorization]
# force_logins: True in moonraker.conf - Mainsail's login dialog only ever
# collects a password (see FIXED_USERNAME below), and stock Moonraker's
# multi-user system can't do that out of the box: a single shared password
# with no separate account to create by hand, a JWT that never forces a
# re-login, and a password requirement that depends on WHERE the request is
# coming from, not just whether it's logged in.
#
# Mechanism:
#  - Registers one fixed, non-configurable username (USERNAME below) with
#    the configured (or auto-generated, see below) password, through
#    authorization's own user table and password hashing (pbkdf2_hmac /
#    HASH_ITER) - not a parallel auth system. The login dialog POSTs this
#    same fixed username to Moonraker's normal, unmodified /access/login, so
#    the login endpoint itself needs no patch at all.
#  - Sets `force_logins = True`, `sessions_never_expire = True`, and
#    `force_login_bypass_trusted = local_bypass` directly on the
#    authorization instance - the same plain-attribute pattern authorization.py
#    already uses for force_logins itself (see Authorization.__init__), not a
#    parallel/patched code path. authorization.py reads these three
#    attributes natively in decode_jwt()/authenticate_request(); this
#    component is simply the one place in the moonraker.conf-facing surface
#    that flips all three together, so they can't drift out of sync the way
#    two independent [authorization]/[simple_password_auth] config options
#    could.
#  - Combined with the registered user (bringing the user count to 2 - the
#    built-in _API_KEY_USER_ is always user 1), setting force_logins makes
#    /access/info report login_required without needing [authorization]
#    force_logins set in moonraker.conf too - keep that unset there; this
#    component is the only on/off switch.
#  - `local_bypass` (config, default True): with force_logins on, stock
#    Moonraker drops its own trusted_clients IP bypass entirely - there is
#    no "logged in OR locally trusted" middle ground built in by default.
#    Setting force_login_bypass_trusted=True (see above) makes
#    authenticate_request try _check_trusted_connection (reusing
#    [authorization]'s own trusted_clients ranges, not a second IP-range
#    config to keep in sync) as a fallback specifically when force_logins
#    would otherwise reject the request, treating a match the same as if
#    force_logins were off. Set local_bypass: False to require the password
#    from everywhere, including the local network.
#  - Why authorization.py's Cloudflare check matters: this repo's own
#    QUICKSTART.md has people run `cloudflared tunnel --url
#    http://localhost:80` on the same host that runs nginx/Moonraker. That
#    means a tunneled remote request reaches Moonraker over loopback,
#    indistinguishable BY IP ALONE from someone sitting on the actual LAN -
#    trusted_clients' 127.0.0.0/8 range would wrongly bypass the password
#    for every internet visitor. See CF_HEADERS in authorization.py.
#  - Registers its own `/server/simple_password_auth/hint` endpoint (GET,
#    auth_required=False) returning `{"password_hint": <hint>}`, so
#    TheLoginDialog.vue can show an optional reminder (from the `hint`
#    config option, blank by default) before anyone has logged in. NOT
#    added as a field on authorization's own `/access/info` - that endpoint
#    is handed to the router as a plain captured callable reference at
#    Authorization.__init__ time, so reassigning it afterward would need
#    touching authorization.py directly anyway, which is exactly what a
#    dedicated endpoint avoids needing to do.
#
# Default password: if `password` is left blank in
# moonraker_simple_password_auth.conf, a random one is generated once and
# written directly back into that same config file via ConfigHelper's
# official set_option()/source.save() mechanism (the same surgical raw-text
# rewrite Moonraker itself uses for any config-writeback feature - it
# preserves formatting and only touches this one option) - not stored
# anywhere else, so retrieving it is just reading a regular config file,
# same as every other setting here. Regenerating it on every restart would
# strand anyone who wrote it down, so it's only generated the first time;
# once written, `password` is no longer blank, so every later restart reads
# that same value straight from the file like a normal option.
#
# On restart, `password`/`salt` are always overwritten on the existing user
# entry, but jwt_secret/jwk_id (which sign that user's tokens) are only
# invalidated when `password` actually changed since the last restart (see
# _password_changed below). Unconditionally regenerating jwt_secret/jwk_id
# on every restart would log out every open browser each time Moonraker
# restarts, which is the opposite of "never expires" - but leaving them
# alone unconditionally would mean a password change (e.g. after a
# suspected compromise, when re-login is exactly the point) never revokes
# already-issued tokens either. Invalidation reuses the same mechanism
# Authorization._handle_logout uses to log out a user: clear
# jwt_secret/jwk_id on the UserInfo entry and drop the old jwk_id from
# auth.public_jwks, so decode_jwt() rejects any token signed with the old
# key (see "kid not in self.public_jwks" in authorization.py) and the next
# request gets a fresh key pair on next login.
#
# Existing API-key-based integrations (crowsnest, other API clients) are
# unaffected: authenticate_request checks X-Api-Key before the force_logins
# check, so only plain browser access is what force_logins/local_bypass
# newly gates.
#
# Depends on the following authorization.py/common.py/confighelper.py
# internals - recheck these after touching any of those files:
#   - `authorization` is listed in CORE_COMPONENTS (moonraker/server.py), so
#     its component_init (which loads users from the database) always
#     finishes before this component's component_init starts.
#   - ConfigHelper.set_option(option, value) + ConfigHelper.get_source().save():
#     the officially-supported way for a component to persist a generated
#     value back into its own config file, used here for the auto-generated
#     password. set_option() rewrites the in-memory raw text for the file
#     this section actually came from and marks it pending; save() (async,
#     on the source object, not the section helper) flushes pending files
#     to disk.
#   - UserInfo (moonraker/common.py): a plain, non-frozen dataclass with
#     username/password/salt/source/jwt_secret/jwk_id fields.
#   - Authorization._sync_user(username): persists a self.users[username]
#     entry to the database.
#   - Authorization.sessions_never_expire / force_login_bypass_trusted
#     (authorization.py): plain instance attributes, default False, read
#     natively by decode_jwt()/authenticate_request(). Not config options -
#     this component is the only thing that sets them.
#   - HASH_ITER (authorization.py): the PBKDF2 iteration count used for
#     password hashing.
#   - Server.register_endpoint's auth_required=False, used the same way
#     /access/login and /access/info are, so the hint endpoint is reachable
#     before anyone has logged in.

from __future__ import annotations

import hashlib
import secrets
from typing import Dict, TYPE_CHECKING

from ..common import RequestType, UserInfo
from .authorization import HASH_ITER

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest

USERNAME = "biokalico"


def _clean_config_string(value: str) -> str:
    # Moonraker's config parser has no quoting syntax of its own - unlike a
    # shell or systemd unit file, `key: "value"` keeps the quote characters
    # as a literal part of the string. People naturally quote free-text
    # config values anyway (hint is the field this actually bit), so strip
    # one layer of surrounding matching quotes before using it, the same
    # way those other formats would.
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value.strip()


class SimplePasswordAuth:
    def __init__(self, config: ConfigHelper) -> None:
        self.config = config
        self.server = config.get_server()
        self.enable = config.getboolean("enable", True)
        # Stripped so a stray trailing space/newline left in the config
        # file (easy to introduce editing by hand) doesn't silently become
        # part of the real password; TheLoginDialog.vue trims the submitted
        # password the same way, so both sides agree on what "the password"
        # actually is.
        self.password: str = config.get("password", "").strip()
        self.local_bypass = config.getboolean("local_bypass", True)
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
        # Re-hashes the new password with the OLD salt and compares against
        # the stored hash - the same check authenticate_request() does for
        # a login attempt - rather than comparing the two salted hashes
        # directly, since salt is regenerated on every restart and would
        # make every restart look like a password change.
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
            await self.config.get_source().save()

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
        auth.sessions_never_expire = True
        auth.force_login_bypass_trusted = self.local_bypass


def load_component(config: ConfigHelper) -> SimplePasswordAuth:
    return SimplePasswordAuth(config)
