"""Read/write the hermes-agent config.yaml so Upstream Management shares the
exact same LLM backend as Studio/hermes-agent.

This module mirrors the model/provider block out of ~/.hermes/config.yaml into
the local app settings, and writes any LLM changes back so both systems stay in
sync.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# default hermes home; override with HERMES_HOME env var
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"


def load_config() -> dict:
    """Load the hermes config.yaml as a dict."""
    if not CONFIG_PATH.exists() or yaml is None:
        return {}
    return yaml.safe_load(CONFIG_PATH.read_text()) or {}


def get_model_block() -> dict:
    """Return the `model` block: {default, provider, ...}."""
    cfg = load_config()
    return cfg.get("model", {})


def get_providers() -> list[dict]:
    """Return the `custom_providers` list."""
    cfg = load_config()
    return cfg.get("custom_providers", [])


def get_provider_by_name(name: str) -> dict | None:
    for p in get_providers():
        if p.get("name") == name:
            return p
    return None


def resolve_llm_config() -> dict:
    """Resolve the active LLM config (provider + base_url + model + api_mode).

    This is what the `llm.py` module uses to talk to the OpenAI-compatible endpoint.
    """
    model = get_model_block()
    default_model = model.get("default") or "ornith-1.5-35b-q6_k"
    provider_name = (model.get("provider") or "custom:llama-swap").split(":")[-1]

    provider = get_provider_by_name(provider_name) or {}
    base_url = provider.get("base_url") or "http://192.168.20.5:8642/v1"
    model_name = model.get("default") or provider.get("model") or "ornith-1.5-35b-q6_k"
    api_mode = provider.get("api_mode") or "chat_completions"

    return {
        "provider": provider_name,
        "model": model_name,
        "base_url": base_url.rstrip("/"),
        "api_mode": api_mode,
        "default_model": default_model,
    }


def set_llm(llm: dict) -> bool:
    """Write an LLM config block back into the hermes config.yaml.

    Updates both `model.default` and the matching `custom_providers` entry.
    """
    if yaml is None:
        return False
    cfg = load_config()
    if "model" not in cfg:
        cfg["model"] = {}
    cfg["model"]["default"] = llm.get("model", cfg["model"].get("default"))
    cfg["model"]["provider"] = llm.get("provider", cfg["model"].get("provider"))

    # update custom_providers base_url if provided
    base_url = llm.get("base_url")
    if base_url:
        for p in cfg.get("custom_providers", []):
            if p.get("name") == llm.get("provider", p.get("name")):
                p["base_url"] = base_url
                break

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return True
