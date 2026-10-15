#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UN5 Netplay - onde o app guarda os dados do usuario.

Windows : %APPDATA%\\UN5 Netplay\\
    launcher.json   preferencias (nick, papel, pasta de save do PCSX2 ...)
    session.json    sessao do Discord (token + dados da conta)
    avatar.png      avatar do Discord em cache
    symbols.json    symbols.json baixado do host (quando nao ha build local)
    saves\\UN5-partida.p2s   o estado da partida (feito pelo PREPARE ou baixado do host)
Linux/macOS: ~/.config/UN5 Netplay  /  ~/Library/Application Support/UN5 Netplay

Se a pasta nao puder ser criada, cai para a pasta do launcher (o app continua funcionando).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "UN5 Netplay"
STATE_NAME = "UN5-partida.p2s"
HERE = Path(__file__).resolve().parent


def _base() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _make_data_dir() -> Path:
    path = _base() / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return HERE


DATA_DIR = _make_data_dir()
SAVES_DIR = DATA_DIR / "saves"
try:
    SAVES_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    SAVES_DIR = DATA_DIR

CONFIG_FILE = DATA_DIR / "launcher.json"
SESSION_FILE = DATA_DIR / "session.json"
AVATAR_FILE = DATA_DIR / "avatar.png"
STATE_PATH = SAVES_DIR / STATE_NAME
SYMBOLS_PATH = DATA_DIR / "symbols.json"


def write_json(path: Path, data, private=False):
    """Escrita atomica (arquivo temporario + replace). `private` tenta deixar o arquivo so para o dono."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


# ---- sessao do Discord -------------------------------------------------------------------------------------------
def load_session():
    """(token, conta) salvos, ou ("", None)."""
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        token, account = str(data.get("token", "")), data.get("account")
        if token and isinstance(account, dict) and account.get("nick"):
            return token, account
    except (OSError, ValueError, AttributeError):
        pass
    return "", None


def save_session(token: str, account: dict):
    try:
        write_json(SESSION_FILE, dict(token=token, account=account), private=True)
    except OSError:
        pass


def clear_session():
    for path in (SESSION_FILE, AVATAR_FILE):
        try:
            path.unlink()
        except OSError:
            pass


# ---- migracao dos arquivos antigos (ao lado do launcher) ------------------------------------------------------------
def migrate_legacy():
    """Copia (nao apaga) o json e o estado antigos para o AppData, uma unica vez. A sessao antiga (login por senha) nao
    e migrada: contas agora sao do Discord."""
    for old, new in ((HERE / "un5_launcher.json", CONFIG_FILE), (HERE / STATE_NAME, STATE_PATH)):
        if old.is_file() and not new.exists() and old.resolve() != new.resolve():
            try:
                shutil.copy2(old, new)
            except OSError:
                pass


migrate_legacy()