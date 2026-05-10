"""Logs API endpoint.

Reads systemd journal logs for the trading daemon services.
Only works when the dashboard runs on the same host as the daemon.
"""

from __future__ import annotations

import logging
import os
import subprocess

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_service_name() -> str:
    """Determine the systemd service name based on pipeline mode."""
    mode = os.getenv("PIPELINE_MODE", "full")
    return f"trading-{mode}-daemon"


@router.get("/logs")
async def get_logs(
    lines: int = Query(200, ge=10, le=2000, description="Number of log lines"),
    service: str = Query(None, description="Override service name"),
):
    """Return recent journal logs for the daemon service."""
    svc = service or _get_service_name()

    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u", svc,
                "--no-pager",
                "-n", str(lines),
                "--output", "short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        log_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Also grab dashboard logs
        dashboard_svc = svc.replace("-daemon", "-dashboard")
        dash_result = subprocess.run(
            [
                "journalctl",
                "-u", dashboard_svc,
                "--no-pager",
                "-n", str(min(lines, 50)),
                "--output", "short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        dash_lines = dash_result.stdout.strip().split("\n") if dash_result.stdout.strip() else []

        return {
            "service": svc,
            "dashboard_service": dashboard_svc,
            "daemon_logs": log_lines,
            "dashboard_logs": dash_lines,
            "line_count": len(log_lines),
        }
    except FileNotFoundError:
        return {
            "service": svc,
            "daemon_logs": ["journalctl not available (not running on systemd)"],
            "dashboard_logs": [],
            "line_count": 0,
            "error": "journalctl not found",
        }
    except subprocess.TimeoutExpired:
        return {
            "service": svc,
            "daemon_logs": ["Timed out reading logs"],
            "dashboard_logs": [],
            "line_count": 0,
            "error": "timeout",
        }
    except Exception as exc:
        logger.error("Failed to read logs: %s", exc, exc_info=True)
        return {
            "service": svc,
            "daemon_logs": ["Failed to read logs"],
            "dashboard_logs": [],
            "line_count": 0,
            "error": "internal_error",
        }
