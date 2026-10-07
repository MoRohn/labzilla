"""Earn on System: the earning system (namespace earn) as Services entries, Logs events and alert wording.

The earn upstream is faked next to the SYS fakes; every value (markets, amounts, reasons) is invented for the tests.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from lif.console import earn
from lif.console import humanize as hz
from test_console_sys import ADMIN, NOW, Fake, fake, make_client, published, refresh  # noqa: F401  (fixtures)

READ_KEY = "test-earn-read-key"


def _status(**over: Any) -> dict[str, Any]:
    st: dict[str, Any] = {
        "ts": NOW, "book": "paper", "instance": "earn-abc", "mode": {"liquidity": "paper"},
        "boot": {"steps": [{"step": "load_policy", "ok": True, "detail": "", "ts": NOW - 600},
                           {"step": "verify_rules", "ok": False, "detail": "venue.doc: changed, awaiting review",
                            "ts": NOW - 590}],
                 "booted_at": NOW - 580, "warm": True},
        "trading_safe": {"safe": False, "reasons": ["global stopped: maintenance", "venue:alpha: rule_change:alpha.doc"]},
        "states": [{"scope": "global", "state": "stopped", "reason": "maintenance", "actor": "owner via admin",
                    "updated_at": NOW - 3000}],
        "trips": [{"id": 7, "scope": "venue:alpha", "trigger": "rule_change:alpha.doc", "effect": "paused",
                   "detail": "a watched document changed", "ts": NOW - 2000, "cleared_at": None}],
        "loops": {"last_ok": {}, "errors": {}},
        "pnl": {"all": {"net_incremental": "1.25"}},
        "audit": [{"id": 90, "ts": NOW - 100, "actor": "engine", "action": "order.cancel_request", "target": "o1"},
                  {"id": 91, "ts": NOW - 50, "actor": "reconcile", "action": "trip.clear", "target": "venue:beta"}],
    }
    st.update(over)
    return st


class EarnFake(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.earn: Any = _status()
        self.backup_ok: float | None = NOW - 3600

    def route(self, host: str, method: str, path: str, params: Any, body: Any) -> Any:
        if host == "earn":
            if path != "/api/status" or method != "GET":
                return httpx.Response(404, json={"detail": "not found"})
            return self.earn if isinstance(self.earn, dict) else self.earn
        if host == "monitoring-kube-prometheus-prometheus" and not path.endswith("query_range"):
            q = params.get("query", "")
            if q.startswith("kube_deployment_spec_replicas") or q.startswith("kube_deployment_status_replicas_available"):
                res = [{"metric": {"namespace": "earn", "deployment": d}, "value": [NOW, "1"]} for d in ("earn", "earn-synth")]
                return {"status": "success", "data": {"result": res}}
            if q.startswith("kube_cronjob_status_last_successful_time"):
                res = [] if self.backup_ok is None else [{"metric": {"namespace": "earn"}, "value": [NOW, str(self.backup_ok)]}]
                return {"status": "success", "data": {"result": res}}
        return super().route(host, method, path, params, body)


@pytest.fixture
def efake(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> EarnFake:   # noqa: F811
    from lif.console import poller, upstream
    monkeypatch.setenv("LIF_EARN_URL", "http://earn.earn.svc:8080")
    monkeypatch.setenv("LIF_EARN_READ_KEY", f"  {READ_KEY}\n")
    f = EarnFake()
    upstream.set_transport(httpx.MockTransport(f))
    poller.reset()
    return f


def _svc(snap: Any, key: str) -> Any:
    return next(s for s in snap.services if s.key == key)


def test_operator_stop_is_paused_not_a_fault(efake: EarnFake, published: list[tuple[str, Any]]) -> None:  # noqa: F811
    snap = refresh()
    s = _svc(snap, "earn")
    assert s.name == "Earning system" and s.health == "paused"
    assert s.summary == "Stopped by an operator (paper book)"
    assert "Reason: maintenance" in (s.impact or "") and "1 safety stop open" in (s.impact or "")
    tech = {t.label: t.value for t in s.tech}
    assert tech["open safety stops"] == "1" and tech["failed start checks"] == "verify_rules"
    assert tech["replicas available"] == "1.0"                              # kube-state joined in
    assert _svc(snap, "earn-synth").health == "healthy"
    assert _svc(snap, "earn-backup").health == "healthy"
    # read-only: one GET with the stripped read key, nothing else sent to earn, no control key held
    calls = [c for c in efake.calls if c[0] == "earn"]
    assert calls and all(m == "GET" and p == "/api/status" for _, m, p, _, _ in calls)
    assert all(h.get("x-earn-key") == READ_KEY and "x-earn-admin" not in h for *_, h in calls)
    # an earn operator stop is not a LIF service outage: no notification
    assert not [d for t, d in published if t == "notification" and "Earn" in str(d)]


@pytest.mark.parametrize("over,health,summary", [
    ({"states": [], "trips": [], "trading_safe": {"safe": True, "reasons": []}}, "healthy", "Trading (paper book)"),
    ({"states": [], "trading_safe": {"safe": False, "reasons": ["venue:alpha: rule_change:alpha.doc"]}},
     "attention", "Running, not trading: venue:alpha: rule_change:alpha.doc"),
    ({"states": [], "loops": {"errors": {"feeds": "ConnectError"}}}, "degraded", "Running, but 1 loop failing"),
    ({"boot": {"steps": [{"step": "acquire_leadership", "ok": False, "ts": NOW}], "booted_at": None}},
     "attention", "Not started (check failed: acquire leadership)"),
])
def test_earn_health_mapping(over: dict[str, Any], health: str, summary: str) -> None:
    h, s, _, _ = earn.service(_status(**over))
    assert (h, s) == (health, summary)


def test_unreachable_or_unauthorized_earn(efake: EarnFake, published: list[tuple[str, Any]]) -> None:  # noqa: F811
    efake.down.add("earn")
    assert _svc(refresh(), "earn").health == "offline"
    efake.down.clear()
    efake.earn = httpx.Response(401, json={"detail": "read key required"})
    s = _svc(refresh(), "earn")
    assert s.health == "attention" and "read key" in s.summary


def test_without_earn_url_kube_state_still_shows_it(fake: Fake, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                    published: list[tuple[str, Any]]) -> None:  # noqa: F811
    from lif.console import poller, upstream
    monkeypatch.delenv("LIF_EARN_URL", raising=False)
    f = EarnFake()
    upstream.set_transport(httpx.MockTransport(f))
    poller.reset()
    snap = refresh()
    assert _svc(snap, "earn").health == "healthy" and not [c for c in f.calls if c[0] == "earn"]


def test_backup_overdue() -> None:
    now = time.time()
    assert earn.backup(now - 30 * 3600, now)[0] == "attention"
    assert earn.backup(None, now)[0] == "unknown"


def test_logs_include_earn_activity(efake: EarnFake, published: list[tuple[str, Any]]) -> None:  # noqa: F811
    refresh()
    c = make_client(ADMIN)
    ev = c.get("/api/system/logs?level=all&q=earn").json()["events"]
    ids = [e["id"] for e in ev]
    assert "earn:trip:7" in ids and "earn:audit:91" in ids and "earn:audit:90" not in ids     # cancels are noise
    assert any(i.startswith("earn:state:global:") for i in ids) and any(i.startswith("earn:boot:verify_rules") for i in ids)
    assert [e["ts"] for e in ev] == sorted((e["ts"] for e in ev), reverse=True)
    stop = next(e for e in ev if e["id"].startswith("earn:state:global:"))
    assert stop["title"] == "Earn: all trading stopped" and stop["severity"] == "warning"
    # earn down: LIF events still listed, with a note
    efake.down.add("earn")
    refresh()
    body = c.get("/api/system/logs?level=all").json()
    assert "Earn's activity can't be read" in body["raw_logs_note"]


def test_earn_alerts_have_plain_summaries() -> None:
    for name in ("EarnDown", "EarnNotSafeToTrade", "EarnUnresolvedOrders", "EarnFeedStale", "EarnLoopFailing",
                 "EarnRiskTripOpen"):
        assert "Earn page" not in hz.alert_summary(name) and hz.alert_summary(name) != name
