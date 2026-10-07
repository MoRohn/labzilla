"""Earn on System: the earning system (namespace earn, ~/arbies) as Services entries and Logs events.

Read-only. The poller fetches earn's GET /api/status with the read key (settings.earn_read_key); this module only
translates that payload (and kube-state-metrics for the earn Deployments) into console contracts. The console
has no Earn control key: stops, resumes, trip clears and rule acks stay with earn's own CLI (arbies RUNBOOK §3).
"""
from __future__ import annotations

import time
from typing import Any

from lif.console import humanize as hz
from lif.console.contracts import ActivityEvent, Health

STOPPED = ("stopped", "paused", "close_only")
BACKUP_STALE_SEC = 26 * 3600            # nightly CronJob (03:50) plus slack
TRIGGERS = {
    "fees_unknown": "fee schedule unknown",
    "lost_leadership": "another instance holds the venue",
    "position_mismatch": "positions don't match the venue",
    "balance_mismatch": "balance doesn't match the venue",
    "missing_fills": "fills missing",
    "fill_id_collision": "duplicate fill id",
}
STATE_WORDS = {"stopped": "stopped", "paused": "paused", "close_only": "closing positions only", "running": "resumed"}


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def trigger_label(trigger: str) -> str:
    if trigger.startswith("rule_change:"):
        return f"watched document changed ({trigger.split(':', 1)[1]})"
    return TRIGGERS.get(trigger, trigger.replace("_", " "))


def scope_label(scope: str) -> str:
    if scope == "global":
        return "all trading"
    kind, _, rest = scope.partition(":")
    return {"venue": f"venue {rest}", "engine": f"{rest} engine", "market": f"market {rest.split(':')[-1]}",
            "instance": f"instance {rest}"}.get(kind, scope)


def _global_state(st: dict[str, Any]) -> dict[str, Any]:
    return next((s for s in _l(st.get("states")) if isinstance(s, dict) and s.get("scope") == "global"), {})


def service(st: dict[str, Any]) -> tuple[Health, str, str | None, list[Any]]:
    """Earn's /api/status → (health, summary, impact, tech). An operator stop is 'paused', not a fault."""
    trips = [t for t in _l(st.get("trips")) if isinstance(t, dict)]
    safe = _d(st.get("trading_safe"))
    boot = _d(st.get("boot"))
    loops = _d(_d(st.get("loops")).get("errors"))
    g = _global_state(st)
    book = str(st.get("book") or "paper")
    failed = [str(s.get("step")) for s in _l(boot.get("steps")) if isinstance(s, dict) and not s.get("ok")]
    net = _d(_d(st.get("pnl")).get("all")).get("net_incremental")
    tech = hz.tech(book=book, instance=st.get("instance"), modes=st.get("mode"), open_safety_stops=len(trips),
                   failed_start_checks=", ".join(failed), loop_errors=", ".join(sorted(loops)),
                   realized_net_usd=net, global_state=g.get("state"))
    stops = f"{len(trips)} safety stop{'s' if len(trips) != 1 else ''} open" if trips else ""
    if boot.get("booted_at") is None:
        why = f" (check failed: {failed[0].replace('_', ' ')})" if failed else ""
        return "attention", f"Not started{why}", "Earn isn't trading until it finishes starting.", tech
    if str(g.get("state")) in STOPPED:
        word = STATE_WORDS.get(str(g.get("state")), str(g.get("state")))
        impact = "; ".join(x for x in (f"Reason: {g.get('reason')}" if g.get("reason") else "", stops) if x)
        return "paused", f"{word.capitalize()} by an operator ({book} book)", (impact + ".") if impact else None, tech
    if loops:
        return "degraded", f"Running, but {len(loops)} loop{'s' if len(loops) != 1 else ''} failing", \
            f"Failing: {', '.join(sorted(loops))}.", tech
    if not safe.get("safe", False):
        reasons = [str(r) for r in _l(safe.get("reasons"))]
        return "attention", f"Running, not trading: {reasons[0] if reasons else 'a safety check failed'}", \
            (stops + ".") if stops else None, tech
    return "healthy", f"Trading ({book} book)", None, tech


def backup(last_ok: float | None, now: float) -> tuple[Health, str, list[Any]]:
    """Earn's nightly earn-backup CronJob from kube_cronjob_status_last_successful_time."""
    if last_ok is None:
        return "unknown", "No successful backup recorded yet", []
    age = now - last_ok
    tech = hz.tech(last_success=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_ok)), age_hours=round(age / 3600, 1))
    if age > BACKUP_STALE_SEC:
        return "attention", f"Last good backup {age / 3600:.0f} h ago (overdue)", tech
    return "healthy", f"Last good backup {age / 3600:.0f} h ago", tech


def events(st: dict[str, Any]) -> list[ActivityEvent]:
    """Earn activity for System → Logs, derived from current state so old stops and trips stay visible
    (earn's audit tail is almost all routine order cancels)."""
    out: list[ActivityEvent] = []
    now = hz.num(st.get("ts")) or time.time()
    boot = _d(st.get("boot"))
    for s in _l(st.get("states")):
        if not isinstance(s, dict) or not s.get("scope"):
            continue
        state, scope = str(s.get("state") or ""), str(s.get("scope"))
        word = STATE_WORDS.get(state, state)
        out.append(ActivityEvent(
            id=f"earn:state:{scope}:{int(hz.num(s.get('updated_at')) or 0)}", ts=hz.num(s.get("updated_at")) or 0.0,
            category="system", severity="warning" if state in STOPPED else "info",
            title=f"Earn: {scope_label(scope)} {word}", detail=str(s.get("reason") or "")[:300] or None,
            href="/system/services", tech=hz.tech(source="earn", scope=scope, state=state, actor=s.get("actor"))))
    for t in _l(st.get("trips")):
        if not isinstance(t, dict):
            continue
        trig, scope = str(t.get("trigger") or ""), str(t.get("scope") or "")
        out.append(ActivityEvent(
            id=f"earn:trip:{t.get('id')}", ts=hz.num(t.get("ts")) or 0.0, category="system", severity="warning",
            title=f"Earn safety stop: {trigger_label(trig)}", detail=f"{scope_label(scope)}: {t.get('detail') or ''}"[:300],
            href="/system/services", tech=hz.tech(source="earn", trip_id=t.get("id"), scope=scope, trigger=trig,
                                                  effect=t.get("effect"))))
    for s in _l(boot.get("steps")):
        if isinstance(s, dict) and not s.get("ok"):
            out.append(ActivityEvent(
                id=f"earn:boot:{s.get('step')}:{int(hz.num(s.get('ts')) or 0)}", ts=hz.num(s.get("ts")) or 0.0,
                category="system", severity="error" if s.get("step") == "acquire_leadership" else "warning",
                title=f"Earn start-up check failed: {str(s.get('step')).replace('_', ' ')}",
                detail=str(s.get("detail") or "")[:300] or None, href="/system/services",
                tech=hz.tech(source="earn", step=s.get("step"))))
    if hz.num(boot.get("booted_at")):
        out.append(ActivityEvent(id=f"earn:started:{int(hz.num(boot.get('booted_at')) or 0)}",
                                 ts=hz.num(boot.get("booted_at")) or 0.0, category="system", severity="info",
                                 title=f"Earn started ({st.get('book') or 'paper'} book)", href="/system/services",
                                 tech=hz.tech(source="earn", instance=st.get("instance"))))
    for name, err in sorted(_d(_d(st.get("loops")).get("errors")).items()):
        out.append(ActivityEvent(id=f"earn:loop:{name}", ts=now, category="system", severity="error",
                                 title=f"Earn loop failing: {name}", detail=str(err)[:300] or None,
                                 href="/system/services", tech=hz.tech(source="earn", loop=name)))
    for a in _l(st.get("audit")):
        if not isinstance(a, dict):
            continue
        action = str(a.get("action") or "")
        if action == "trip.clear":
            title, sev = "Earn safety stop cleared", "success"
        elif action == "sources.ack":
            title, sev = "Earn rule change acknowledged", "info"
        else:
            continue
        out.append(ActivityEvent(id=f"earn:audit:{a.get('id')}", ts=hz.num(a.get("ts")) or 0.0, category="system",
                                 severity=sev, title=title,  # type: ignore[arg-type]
                                 detail=f"{a.get('target') or ''} by {a.get('actor') or 'operator'}"[:300],
                                 href="/system/services", tech=hz.tech(source="earn", action=action)))
    return sorted(out, key=lambda e: e.ts, reverse=True)
