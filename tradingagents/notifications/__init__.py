"""Discord webhook notifications for trade events.

Fire-and-forget: notifications never raise exceptions or block the
trading pipeline.  If ``DISCORD_WEBHOOK_URL`` is not set, every call
is a silent no-op.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Discord embed colour codes (decimal)
_COLORS = {
    "entry": 0x10B981,      # green
    "exit": 0xEF4444,       # red
    "blocked": 0xF59E0B,    # orange
    "error": 0xEF4444,      # red
    "daily_summary": 0x3B82F6,  # blue
    "stop_update": 0x6B7280,    # grey
}

_ICONS = {
    "entry": "🟢",
    "exit": "🔴",
    "blocked": "🛑",
    "error": "⚠️",
    "daily_summary": "📊",
    "stop_update": "🔄",
}


def _get_webhook_url() -> Optional[str]:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    return url if url else None


def _get_instance_label() -> str:
    return os.getenv("INSTANCE_LABEL", os.getenv("PIPELINE_MODE", "full")).upper()


def _build_embed(event_type: str, **kwargs) -> Dict[str, Any]:
    """Build a Discord embed payload for the given event type."""
    icon = _ICONS.get(event_type, "📌")
    color = _COLORS.get(event_type, 0x94A3B8)
    label = _get_instance_label()
    now = datetime.now(timezone.utc).isoformat()

    if event_type == "entry":
        title = f"{icon} Entry Submitted — {kwargs.get('symbol', '?')}"
        fields = [
            {"name": "Shares", "value": str(kwargs.get("shares", "?")), "inline": True},
            {"name": "Entry", "value": f"${kwargs.get('entry', 0):.2f}", "inline": True},
            {"name": "Stop", "value": f"${kwargs.get('stop', 0):.2f}", "inline": True},
            {"name": "Position Value", "value": f"${kwargs.get('value', 0):,.0f}", "inline": True},
            {"name": "Risk", "value": f"${kwargs.get('risk', 0):,.0f}", "inline": True},
        ]
        if kwargs.get("order_id"):
            fields.append({"name": "Order ID", "value": kwargs["order_id"][:20], "inline": False})

    elif event_type == "exit":
        title = f"{icon} Exit Executed — {kwargs.get('symbol', '?')}"
        fields = [
            {"name": "Type", "value": kwargs.get("action", "exit_full"), "inline": True},
            {"name": "Reason", "value": kwargs.get("reason", "—")[:200], "inline": False},
        ]

    elif event_type == "blocked":
        title = f"{icon} Entry Blocked — {kwargs.get('symbol', '?')}"
        fields = [
            {"name": "Reason", "value": kwargs.get("reason", "—")[:200], "inline": False},
        ]

    elif event_type == "stop_update":
        title = f"{icon} Stop Updated — {kwargs.get('symbol', '?')}"
        fields = [
            {"name": "New Stop", "value": f"${kwargs.get('new_stop', 0):.2f}", "inline": True},
            {"name": "Reason", "value": kwargs.get("reason", "—")[:200], "inline": False},
        ]

    elif event_type == "daily_summary":
        title = f"{icon} Daily Summary"
        pnl = kwargs.get("pnl", 0)
        pnl_str = f"+${pnl:,.0f}" if pnl >= 0 else f"-${abs(pnl):,.0f}"
        fields = [
            {"name": "Portfolio", "value": f"${kwargs.get('portfolio', 0):,.0f}", "inline": True},
            {"name": "P&L Today", "value": pnl_str, "inline": True},
            {"name": "Entries", "value": str(kwargs.get("entries", 0)), "inline": True},
            {"name": "Exits", "value": str(kwargs.get("exits", 0)), "inline": True},
            {"name": "Positions", "value": str(kwargs.get("positions", 0)), "inline": True},
        ]

    elif event_type == "error":
        title = f"{icon} Error"
        fields = [
            {"name": "Message", "value": kwargs.get("message", "Unknown error")[:1024], "inline": False},
        ]

    else:
        title = f"📌 {event_type}"
        fields = [
            {"name": "Details", "value": str(kwargs)[:1024], "inline": False},
        ]

    return {
        "embeds": [{
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": f"{label} Instance"},
            "timestamp": now,
        }]
    }


def _send(payload: Dict[str, Any], webhook_url: str) -> None:
    """POST the payload to Discord. Runs in a background thread."""
    try:
        import requests
        resp = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code not in (200, 204):
            logger.warning(
                "Discord webhook returned %d: %s",
                resp.status_code, resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Discord notification failed: %s", exc)


def notify(event_type: str, **kwargs) -> None:
    """Send a Discord notification (fire-and-forget).

    Does nothing if ``DISCORD_WEBHOOK_URL`` is not set.

    Parameters
    ----------
    event_type : str
        One of: entry, exit, blocked, error, daily_summary, stop_update.
    **kwargs
        Event-specific fields (symbol, shares, entry, stop, reason, etc.).
    """
    webhook_url = _get_webhook_url()
    if not webhook_url:
        return

    try:
        payload = _build_embed(event_type, **kwargs)
        # Fire in background thread so we never block the pipeline
        thread = threading.Thread(
            target=_send,
            args=(payload, webhook_url),
            daemon=True,
        )
        thread.start()
    except Exception as exc:
        # Absolute last resort — never let notifications crash anything
        logger.warning("Failed to build Discord notification: %s", exc)
