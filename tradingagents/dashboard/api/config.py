"""Config API endpoint.

Returns the current daemon configuration as read-only JSON.
Sensitive values (API keys) are redacted.
"""

from __future__ import annotations

import os
from fastapi import APIRouter

router = APIRouter()

# Keys that should never be exposed in the config response
_SENSITIVE_PATTERNS = {"key", "secret", "password", "token"}


def _redact(d: dict) -> dict:
    """Recursively redact sensitive values."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _redact(v)
        elif any(pat in k.lower() for pat in _SENSITIVE_PATTERNS):
            out[k] = "***" if v else "(not set)"
        else:
            out[k] = v
    return out


@router.get("/config")
async def get_config():
    """Return the current daemon configuration (read-only, redacted)."""
    from tradingagents.default_config import DEFAULT_CONFIG

    config = _redact(dict(DEFAULT_CONFIG))

    # Add runtime env info
    config["_runtime"] = {
        "pipeline_mode": os.getenv("PIPELINE_MODE", "full"),
        "llm_provider": os.getenv("LLM_PROVIDER", "openai"),
        "deep_think_llm": os.getenv("DEEP_THINK_LLM", ""),
        "quick_think_llm": os.getenv("QUICK_THINK_LLM", ""),
        "dashboard_port": os.getenv("DASHBOARD_PORT", "8050"),
        "instance_label": os.getenv("INSTANCE_LABEL", ""),
        "peer_url": os.getenv("PEER_DASHBOARD_URL", ""),
        "db_path": os.getenv("TRADINGAGENTS_DB_PATH", ""),
        "alpaca_configured": bool(os.getenv("ALPACA_API_KEY")),
        "finnhub_configured": bool(os.getenv("FINNHUB_API_KEY")),
    }

    return config
