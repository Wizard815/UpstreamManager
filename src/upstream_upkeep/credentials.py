"""Credentials for GitHub and Docker Hub, kept out of the app's state file.

Lookup order for each value:
  1. environment variable (UPSTREAM_GITHUB_TOKEN, UPSTREAM_DOCKERHUB_USERNAME,
     UPSTREAM_DOCKERHUB_TOKEN) - for servers and containers (Docker/K8s secrets)
  2. the OS keychain via `keyring` (Windows Credential Manager, macOS Keychain,
     Secret Service on Linux desktops)
  3. a private file next to the state file (secrets.json, owner-only
     permissions) when no keychain is available, e.g. a headless Linux service

Values are never returned to the web page in full: `public()` reports which
are set, where from, and the last four characters.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

SERVICE = "upstream-manager"
FIELDS = {
    "github_token": "UPSTREAM_GITHUB_TOKEN",
    "dockerhub_username": "UPSTREAM_DOCKERHUB_USERNAME",
    "dockerhub_token": "UPSTREAM_DOCKERHUB_TOKEN",
}
SECRET_FIELDS = {"github_token", "dockerhub_token"}  # usernames are not secret


def _file() -> Path:
    return config.CONFIG_PATH.parent / "secrets.json"


def _keyring():
    if os.environ.get("UPSTREAM_SECRETS_BACKEND") == "file":
        return None
    try:
        import keyring
        from keyring.backends import fail, null
        kr = keyring.get_keyring()
        if isinstance(kr, (fail.Keyring, null.Keyring)):
            return None
        return kr
    except Exception:  # no usable keychain on this machine
        return None


def _read_file() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_file(data: dict) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # owner-only (no effect on Windows ACLs)
    except OSError:
        pass
    os.replace(tmp, p)


def get(name: str) -> str:
    if name not in FIELDS:
        raise KeyError(name)
    if os.environ.get(FIELDS[name]):
        return os.environ[FIELDS[name]]
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, name)
            if value:
                return value
        except Exception:
            pass
    return _read_file().get(name, "")


def source(name: str) -> str | None:
    if os.environ.get(FIELDS[name]):
        return "environment"
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(SERVICE, name):
                return "keychain"
        except Exception:
            pass
    return "file" if _read_file().get(name) else None


def set(name: str, value: str) -> str:
    """Store a value; returns where it went. Empty value deletes it."""
    if name not in FIELDS:
        raise KeyError(name)
    if not value:
        delete(name)
        return "deleted"
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, name, value)
            data = _read_file()  # never leave an older copy in the file
            if data.pop(name, None) is not None:
                _write_file(data)
            return "keychain"
        except Exception:
            pass
    data = _read_file()
    data[name] = value
    _write_file(data)
    return "file"


def delete(name: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:
            pass
    data = _read_file()
    if data.pop(name, None) is not None:
        _write_file(data)


def public() -> dict:
    """What the page may see: set or not, where from, and a masked hint."""
    out = {}
    for name in FIELDS:
        value = get(name)
        hint = ("••••" + value[-4:] if len(value) > 8 else "••••") if name in SECRET_FIELDS else value
        out[name] = {"set": bool(value), "source": source(name) if value else None, "hint": hint if value else ""}
    return out
