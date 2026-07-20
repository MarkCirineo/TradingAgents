"""Tests for per-instance log service-name resolution.

Two same-pipeline instances (e.g. a 100k and a 4k quant account) must not
resolve to the same journald unit — otherwise the Logs tab shows the wrong
(or an old, stopped) instance's logs.
"""

import pytest

from tradingagents.dashboard.api import logs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("DAEMON_SERVICE", "DASHBOARD_SERVICE", "INSTANCE_LABEL", "PIPELINE_MODE"):
        monkeypatch.delenv(var, raising=False)


def test_explicit_daemon_service_wins(monkeypatch):
    monkeypatch.setenv("DAEMON_SERVICE", "trading-100k-daemon")
    monkeypatch.setenv("INSTANCE_LABEL", "4k")     # ignored — explicit wins
    monkeypatch.setenv("PIPELINE_MODE", "quant")
    assert logs._get_service_name() == "trading-100k-daemon"


def test_instance_label_used_when_no_explicit(monkeypatch):
    monkeypatch.setenv("INSTANCE_LABEL", "100k")
    assert logs._get_service_name() == "trading-100k-daemon"


def test_instance_label_is_lowercased(monkeypatch):
    monkeypatch.setenv("INSTANCE_LABEL", "4K")
    assert logs._get_service_name() == "trading-4k-daemon"


def test_legacy_pipeline_mode_fallback(monkeypatch):
    monkeypatch.setenv("PIPELINE_MODE", "quant")
    assert logs._get_service_name() == "trading-quant-daemon"


def test_default_is_full(monkeypatch):
    assert logs._get_service_name() == "trading-full-daemon"


def test_dashboard_service_explicit_override(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SERVICE", "trading-100k-web")
    assert logs._get_dashboard_service("trading-100k-daemon") == "trading-100k-web"


def test_dashboard_service_derived_from_daemon(monkeypatch):
    assert logs._get_dashboard_service("trading-100k-daemon") == "trading-100k-dashboard"
