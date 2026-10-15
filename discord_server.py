#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UN5 server - contas via Discord OAuth2 (substitui o antigo sistema de contas com senha). So biblioteca padrao.

Fluxo (o launcher nunca ve o client secret):
    POST /auth/discord/start                 -> {state, url, expires}   o launcher abre `url` no navegador
    GET  /auth/discord/callback?code&state   o Discord redireciona para ca; o servidor troca o code, le /users/@me
    GET  /auth/discord/poll?state=           -> {status: pending} | {status: ok, token, account} | {status: error}
    POST /accounts/logout        (Authorization: Bearer <sessao>)
    GET  /accounts/me            (Authorization: Bearer <sessao>)  -> {id, nick, username, avatar_url, created, admin}

`id` e o ID (snowflake) do usuario no Discord: e a chave que um bot futuro usa para reconhecer o jogador.
`admin` e true so para o dono do servidor (--admin-id): ele e o unico que pode criar (PREPARE) o save state padrao.
O access token do Discord e descartado logo depois de ler o perfil (escopo `identify`); so ficam id, nome e hash do
avatar em <data>/accounts.json. O avatar em si nao passa pelo servidor: `avatar_url` aponta para o CDN do Discord.
`Accounts.profile(id)` devolve so {nick, avatar_url}: e o que a lista de salas mostra de cada jogador.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_API = "https://discord.com/api/v10"
UA = "UN5Netplay (https://github.com/, 1.0)"
LOGIN_TTL = 300                 # seconds the launcher has to finish the login in the browser
MAX_PENDING = 500
MAX_ACCOUNTS = 5000
SESSION_TTL = 30 * 86400
MAX_SESSIONS = 8                # per account (oldest are dropped)

SESSION_RE = re.compile(r"^[0-9a-f]{48}$")
STATE_RE = re.compile(r"^[0-9a-f]{32}$")
DISCORD_ID_RE = re.compile(r"^[0-9]{5,25}$")
AVATAR_RE = re.compile(r"^(a_)?[0-9a-f]{32}$")

PAGE = """<!doctype html><meta charset="utf-8"><title>UN5 Netplay</title>
<body style="font-family:Segoe UI,Arial,sans-serif;background:#0b0f17;color:#e6edf7;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0"><div style="text-align:center;max-width:420px;padding:24px">
<h2 style="margin:0 0 10px">@TITLE@</h2><p style="color:#8fa0b8">@TEXT@</p></div></body>"""


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def avatar_url(uid, avatar_hash):
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=128"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6}.png"


def clean_display(value):
    return "".join(c for c in str(value or "") if c.isprintable()).strip()[:32]


class Accounts:
    """Contas, sessoes e logins pendentes. `discord` = dict(client_id, secret, redirect) (None = login desligado)."""

    def __init__(self, data_dir, discord=None):
        base = Path(data_dir)
        base.mkdir(parents=True, exist_ok=True)
        self.file = base / "accounts.json"
        self.discord = discord or {}
        self.admin_id = ""                             # preenchido pelo Store (ID do Discord do dono)
        self.lock = threading.RLock()
        self.accounts = {}                             # discord id -> record
        self.sessions = {}                             # sha256(token) -> {id, created, seen}
        self.pending = {}                              # state -> {created, result}
        self.hits = {}                                 # rate-limit key -> [timestamps]
        self._load()

    # ---- persistence
    def _load(self):
        try:
            data = json.loads(self.file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for acc in data.get("accounts", []):
            try:
                if acc.get("discord") is True and DISCORD_ID_RE.match(acc["id"]):   # ignores old password accounts
                    self.accounts[acc["id"]] = acc
            except (KeyError, TypeError, AttributeError):
                continue
        sessions = data.get("sessions", {})
        if isinstance(sessions, dict):
            self.sessions = {k: v for k, v in sessions.items() if isinstance(v, dict) and v.get("id") in self.accounts}

    def _save(self):
        """Call with the lock held. Atomic: write a temp file, then replace."""
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(accounts=list(self.accounts.values()), sessions=self.sessions)), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.file)

    # ---- helpers
    @property
    def configured(self):
        return bool(self.discord.get("client_id") and self.discord.get("secret") and self.discord.get("redirect"))

    def _check_rate(self, key, limit, window):
        now = time.time()
        recent = [t for t in self.hits.get(key, ()) if now - t < window]
        self.hits[key] = recent
        if len(recent) >= limit:
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")
        recent.append(now)

    def view(self, acc):
        return dict(id=acc["id"], nick=acc["nick"], username=acc.get("username", ""),
                    avatar_url=avatar_url(acc["id"], acc.get("avatar")), created=acc.get("created", 0),
                    admin=bool(self.admin_id) and acc["id"] == self.admin_id)

    def profile(self, aid):
        """Parte publica de uma conta (o que a lista de salas mostra): {nick, avatar_url}, ou None se nao existir."""
        if not aid:
            return None
        with self.lock:
            acc = self.accounts.get(aid)
            if acc is None:
                return None
            return dict(nick=acc["nick"], avatar_url=avatar_url(aid, acc.get("avatar")))

    def _new_session(self, aid):
        token = secrets.token_hex(24)
        now = time.time()
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = dict(id=aid, created=now, seen=now)
        mine = sorted((k for k, s in self.sessions.items() if s["id"] == aid), key=lambda k: self.sessions[k]["seen"])
        for k in mine[:-MAX_SESSIONS]:
            del self.sessions[k]
        return token

    # ---- sessions
    def auth(self, token, max_age=None):
        if not SESSION_RE.match(str(token or "")):
            raise ApiError(401, "Not logged in.")
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            s = self.sessions.get(key)
            if s is None or s["id"] not in self.accounts or time.time() - s["seen"] > SESSION_TTL:
                raise ApiError(401, "Not logged in.")
            if max_age and time.time() - s["created"] > max_age:
                raise ApiError(401, "Your login is too old for this action. Log in with Discord again.")
            s["seen"] = time.time()
            return s["id"]

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            self._save()

    def get(self, aid):
        with self.lock:
            return self.view(self.accounts[aid])

    def gc(self):
        now = time.time()
        with self.lock:
            stale = [k for k, s in self.sessions.items() if now - s["seen"] > SESSION_TTL]
            for k in stale:
                del self.sessions[k]
            if stale:
                self._save()
            for k in [k for k, v in self.pending.items() if now - v["created"] > LOGIN_TTL * 2]:
                del self.pending[k]
            for key in [k for k, v in self.hits.items() if not v or now - v[-1] > 3600]:
                del self.hits[key]

    # ---- Discord login
    def start(self, ip):
        if not self.configured:
            raise ApiError(503, "Discord login is not configured on this server.")
        with self.lock:
            self._check_rate(("start", ip), 20, 600)
            now = time.time()
            for k in [k for k, v in self.pending.items() if now - v["created"] > LOGIN_TTL]:
                del self.pending[k]
            if len(self.pending) >= MAX_PENDING:
                raise ApiError(503, "Too many logins in progress. Try again in a minute.")
            state = secrets.token_hex(16)
            self.pending[state] = dict(created=now, result=None, ip=ip)
        url = DISCORD_AUTHORIZE + "?" + urllib.parse.urlencode(dict(
            client_id=self.discord["client_id"], redirect_uri=self.discord["redirect"], response_type="code",
            scope="identify", state=state, prompt="none"))
        return dict(state=state, url=url, expires=LOGIN_TTL)

    def poll(self, state, ip=""):
        state = str(state or "")
        with self.lock:
            p = self.pending.get(state) if STATE_RE.match(state) else None
            if p is None or time.time() - p["created"] > LOGIN_TTL:
                self.pending.pop(state, None)
                raise ApiError(404, "The login expired. Start again.")
            if ip and p.get("ip") not in (None, ip):
                raise ApiError(403, "This login was started from another network.")
            if p["result"] is None:
                return dict(status="pending")
            del self.pending[state]                    # delivered once
            return p["result"]

    def callback(self, code, state, error, ip=""):
        """Called by the browser redirect. Stores the outcome for poll(); raises ApiError (shown on the page)."""
        state = str(state or "")
        with self.lock:
            p = self.pending.get(state) if STATE_RE.match(state) else None
            if p is None or p["result"] is not None or time.time() - p["created"] > LOGIN_TTL:
                raise ApiError(400, "This login link expired or was already used. Start again from the launcher.")
            if ip and p.get("ip") not in (None, ip):
                raise ApiError(403, "Open the login link on the same PC (same internet connection) that runs the launcher.")
        try:
            if error or not code or len(str(code)) > 256:
                raise ApiError(400, "The Discord login was cancelled.")
            result = self._finish(self._discord_user(str(code)))
        except ApiError as exc:
            with self.lock:
                p["result"] = dict(status="error", error=exc.message)
            raise
        with self.lock:
            p["result"] = result

    def _discord_user(self, code):
        """code -> access token -> /users/@me. The token is not kept."""
        form = urllib.parse.urlencode(dict(
            client_id=self.discord["client_id"], client_secret=self.discord["secret"], grant_type="authorization_code",
            code=code, redirect_uri=self.discord["redirect"])).encode()
        try:
            req = urllib.request.Request(DISCORD_API + "/oauth2/token", data=form, headers={
                "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                access = json.loads(resp.read().decode("utf-8"))["access_token"]
            req = urllib.request.Request(DISCORD_API + "/users/@me", headers={
                "Authorization": "Bearer " + access, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                user = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ApiError(502, f"Discord refused the login ({exc.code}).") from None
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            raise ApiError(502, "Could not talk to Discord. Try again.") from None
        if not isinstance(user, dict):
            raise ApiError(502, "Discord sent an unexpected answer.")
        return user

    def _finish(self, user):
        uid = str(user.get("id", ""))
        if not DISCORD_ID_RE.match(uid):
            raise ApiError(502, "Discord sent an unexpected answer.")
        username = clean_display(user.get("username")) or "player"
        nick = clean_display(user.get("global_name")) or username          # Discord's display name, else @username
        avatar = user.get("avatar") if AVATAR_RE.match(str(user.get("avatar") or "")) else None
        with self.lock:
            acc = self.accounts.get(uid)
            if acc is None:
                if len(self.accounts) >= MAX_ACCOUNTS:
                    raise ApiError(503, "The server cannot take more accounts.")
                acc = dict(id=uid, discord=True, created=int(time.time()))
                self.accounts[uid] = acc
            acc.update(nick=nick, username=username, avatar=avatar)          # refreshed on every login
            token = self._new_session(uid)
            self._save()
            return dict(status="ok", token=token, account=self.view(acc))


# ======================================================================================================================
def route(h, method, parts, q):
    """HTTP routes for /auth/discord/* and /accounts/*. `h` is the relay_server Handler."""
    accounts = h.store.accounts
    if parts == ["auth", "discord", "start"] and method == "POST":
        return h._send(200, accounts.start(h._ip()))
    if parts == ["auth", "discord", "poll"] and method == "GET":
        return h._send(200, accounts.poll(q.get("state"), h._ip()))
    if parts == ["auth", "discord", "callback"] and method == "GET":
        try:
            accounts.callback(q.get("code"), q.get("state"), q.get("error"), h._ip())
            code, title, text = 200, "You are logged in", "You can close this tab and go back to the launcher."
        except ApiError as exc:
            code, title, text = exc.code, "Login failed", exc.message
        page = PAGE.replace("@TITLE@", html.escape(title)).replace("@TEXT@", html.escape(text))
        return h._send(code, page.encode("utf-8"), "text/html; charset=utf-8")
    if parts and parts[0] == "accounts":
        token = h._session_token()
        aid = accounts.auth(token)                     # everything below needs a logged-in session
        if parts == ["accounts", "logout"] and method == "POST":
            accounts.logout(token)
            return h._send(200, dict(ok=True))
        if parts == ["accounts", "me"] and method == "GET":
            return h._send(200, accounts.get(aid))
    raise ApiError(404, "not found")