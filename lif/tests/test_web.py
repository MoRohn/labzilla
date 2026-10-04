"""Live information (lif/web): freshness, dates, feeds, search ranking, fetch safety, grounding, and the
gateway's local/auto path. Everything runs offline against fake upstreams."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from lif.policy import engine as policy
from lif.web import fetch as webfetch
from lif.web import ground as webground
from lif.web.freshness import assess, assess_turn, build_query, resolve_dates
from lif.web.search import Hit, SearchResult, rank
from lif.web.sports import SportsFeed, Team, match_teams
from lif.web.weather import place_of

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 3, 7, 2, tzinfo=ET)          # a Saturday morning


# ── freshness ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,live,kind", [
    ("Who won the Beavers football game last night?", True, "sports"),
    ("what's the weather in Boise tomorrow", True, "weather"),
    ("NVDA stock price", True, "finance"),
    ("who is the current president of the united states", True, "office"),
    ("latest news on the hurricane", True, "weather"),
    ("What's new in iOS 27?", True, None),
    ("did the steelers win on sunday", True, "sports"),
    ("Write a poem about autumn", False, None),
    ("What is the capital of France?", False, None),
    ("Explain how TCP works", False, None),
    ("who won the 1986 world series", False, None),
    ("What's today's date?", False, "clock"),
    ("Refactor this python function: def f(x): return x", False, None),
])
def test_assess(q, live, kind):
    f = assess(q)
    assert f.live is live, (q, f)
    if kind:
        assert f.kind == kind


def test_follow_up_inherits_need():
    prev = ["Who won the Beavers football game last night?"]
    assert assess("what about Clemson?").live is False
    f = assess_turn("what about Clemson?", prev)
    assert f.live and f.kind == "sports"
    assert assess_turn("what about recursion?", ["Explain recursion"]).live is False


@pytest.mark.parametrize("now,phrase,expect", [
    (NOW, "last night", date(2026, 10, 2)),
    (datetime(2026, 10, 3, 0, 30, tzinfo=ET), "last night", date(2026, 10, 2)),
    (NOW, "yesterday", date(2026, 10, 2)),
    (NOW, "tonight", date(2026, 10, 3)),
    (NOW, "tomorrow", date(2026, 10, 4)),
    (NOW, "on Sunday", date(2026, 9, 27)),                 # most recent Sunday
    (NOW, "next Sunday", date(2026, 10, 4)),
    (NOW, "this weekend", date(2026, 10, 3)),             # Saturday: this weekend is today
    (datetime(2026, 9, 30, 12, tzinfo=ET), "this weekend", date(2026, 10, 3)),   # Wednesday → coming Saturday
    (datetime(2026, 9, 30, 12, tzinfo=ET), "last weekend", date(2026, 9, 26)),
    (NOW, "last weekend", date(2026, 9, 26)),
])
def test_resolve_dates(now, phrase, expect):
    r = resolve_dates(f"who won {phrase}", now)
    assert r.target == expect, (phrase, r)
    assert phrase not in r.text


def test_dates_follow_the_users_timezone_not_the_pods():
    utc = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)       # 22:00 on Oct 2 in New York
    assert resolve_dates("last night", utc).target == date(2026, 10, 2)
    assert resolve_dates("last night", utc.astimezone(ET)).target == date(2026, 10, 1)


def test_query_is_short_absolute_and_without_filler():
    q = build_query("Hey, can you tell me who won the Beavers football game last night?", [], NOW)
    assert q.q == "who won the Beavers football game October 2, 2026"
    assert q.target == date(2026, 10, 2)
    assert len(build_query("x " * 400 + "score today", [], NOW).q) <= 200


def test_follow_up_query_borrows_previous_subject():
    q = build_query("what about tomorrow?", ["weather in Boise today"], NOW)
    assert q.borrowed_context and "Boise" in q.q


# ── sports ─────────────────────────────────────────────────────────────────────────────────────

def _team(league, tid, display, *aliases):
    return Team(league, tid, display, tuple(sorted({a.lower() for a in (display, *aliases)}, key=len, reverse=True)))


TEAMS = [_team("college-football", "259", "Oregon State Beavers", "Oregon State", "Beavers"),
         _team("college-football", "258", "Oregon Ducks", "Oregon", "Ducks"),
         _team("college-football", "99", "LSU Tigers", "LSU", "Tigers"),
         _team("college-football", "228", "Clemson Tigers", "Clemson", "Tigers"),
         _team("nba", "13", "Miami Heat", "Miami", "Heat")]


def test_match_teams_longest_alias_wins():
    got = match_teams("Did Oregon State win?", TEAMS)
    assert [t.id for t in got] == ["259"]


def test_shared_nickname_keeps_every_team():
    assert {t.id for t in match_teams("How did the Tigers do?", TEAMS)} == {"99", "228"}


def test_common_word_needs_capitals():
    assert match_teams("is there a heat wave coming", TEAMS) == []
    assert [t.id for t in match_teams("did the Heat win", TEAMS)] == ["13"]


def _espn_event(eid, when, home, away, hs, as_, state="post"):
    def comp(t, ha, score, win):
        return {"homeAway": ha, "winner": win, "score": {"displayValue": score} if score else None,
                "team": {"id": t[0], "displayName": t[1]}}
    return {"id": eid, "date": when, "links": [{"rel": ["summary", "desktop", "event"],
                                                "href": f"https://www.espn.com/college-football/game/_/gameId/{eid}"}],
            "competitions": [{"status": {"type": {"state": state, "completed": state == "post",
                                                  "shortDetail": "Final" if state == "post" else "Sat 3:30 PM"}},
                              "competitors": [comp(home, "home", hs, state == "post" and int(hs or 0) > int(as_ or 0)),
                                              comp(away, "away", as_, state == "post" and int(as_ or 0) > int(hs or 0))]}]}


OSU, PITT, CAL = ("259", "Oregon State Beavers"), ("221", "Pittsburgh Panthers"), ("25", "California Golden Bears")
ESPN_SCHEDULE = {"events": [
    _espn_event("1", "2026-09-26T16:00Z", ("103", "Boston College Eagles"), OSU, "14", "21"),
    _espn_event("401858245", "2026-10-02T23:00Z", OSU, PITT, "33", "35"),
    _espn_event("3", "2026-10-10T19:30Z", CAL, OSU, None, None, state="pre")]}
ESPN_TEAMS = {"sports": [{"leagues": [{"teams": [
    {"team": {"id": "259", "location": "Oregon State", "name": "Beavers", "displayName": "Oregon State Beavers",
              "shortDisplayName": "Oregon State"}},
    {"team": {"id": "221", "location": "Pittsburgh", "name": "Panthers", "displayName": "Pittsburgh Panthers",
              "shortDisplayName": "Pittsburgh"}}]}]}]}


def espn(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p.endswith("/teams") and "college-football" in p:
        return httpx.Response(200, json=ESPN_TEAMS)
    if p.endswith("/teams"):
        return httpx.Response(200, json={"sports": []})
    if p.endswith("/259/schedule"):
        return httpx.Response(200, json=ESPN_SCHEDULE)
    return httpx.Response(404)


async def test_sports_feed_answers_last_night_exactly():
    feed = SportsFeed(httpx.AsyncClient(transport=httpx.MockTransport(espn)))
    q = build_query("Who won the Beavers football game last night?", [], NOW)
    r = await feed.lookup("Who won the Beavers football game last night?", NOW, q.target, q.span)
    assert r is not None and len(r.lines) == 1
    assert r.lines[0].startswith("FINAL (college football, Fri October 2, 2026, 7:00 PM EDT)")
    assert "Pittsburgh Panthers 35, Oregon State Beavers 33" in r.lines[0] and "Pittsburgh Panthers won" in r.lines[0]
    assert r.sources[0]["url"].endswith("/401858245")


async def test_sports_feed_without_a_day_gives_latest_and_next():
    feed = SportsFeed(httpx.AsyncClient(transport=httpx.MockTransport(espn)))
    r = await feed.lookup("when do the Beavers play next?", NOW)
    assert [ln.split(" ")[0] for ln in r.lines] == ["FINAL", "SCHEDULED"]


# ── weather ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,expect", [
    ("what's the weather in Boise tomorrow", ("Boise", None)),
    ("Boise, ID weather", ("Boise", "Idaho")),
    ("Will it rain in Paris, France this weekend?", ("Paris", "France")),
    ("What's the weather?", None),
])
def test_place_of(q, expect):
    assert place_of(q) == expect


# ── search ranking ─────────────────────────────────────────────────────────────────────────────

def test_rank_prefers_recent_and_trusted_and_caps_domains():
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    same = "Oregon State Beavers football game final score"
    hits = [Hit(same, "https://blog.example.com/old", same, now - timedelta(days=60)),
            Hit(same, "https://www.espn.com/a", same, now - timedelta(hours=7)),
            Hit(same, "https://www.espn.com/b", same),
            Hit(same, "https://www.espn.com/c", same),
            Hit(same, "https://www.pinterest.com/x", same, now - timedelta(hours=1)),
            Hit("Recipes", "https://cooking.example.org/r", "soup")]
    out = rank("who won the Beavers football game Oregon State October 2, 2026", hits, now)
    assert out[0].url == "https://www.espn.com/a"                 # trusted and fresh
    assert sum(h.domain == "espn.com" for h in out) == 2           # at most two per domain
    assert [h.domain for h in out].index("pinterest.com") > [h.domain for h in out].index("blog.example.com")
    assert out[-1].domain == "cooking.example.org"


def test_sufficient_by_kind():
    assert webground.sufficient("sports", "Beavers football game", ["Pitt 35-33 Oregon State Beavers football game"])
    assert not webground.sufficient("sports", "Beavers football game", ["Beavers football game preview"])
    assert webground.sufficient("weather", "weather Boise", ["Boise: high of 68°F"])
    assert webground.sufficient("finance", "NVDA price", ["NVDA trades at $233.95"])


# ── fetch safety ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://10.43.0.10/", "http://127.0.0.1/", "http://169.254.169.254/latest",
                                 "http://localhost/", "http://gateway.ai-system.svc:8080/", "http://example.com:8080/",
                                 "file:///etc/passwd", "http://user:pw@example.com/", "http://[::1]/",
                                 "http://192.168.1.1/", "http://100.64.0.1/"])
async def test_check_url_blocks_internal_targets(url):
    with pytest.raises(webfetch.Blocked):
        await webfetch.check_url(url)


async def test_redirect_to_internal_is_blocked(monkeypatch):
    async def fake_check(url):
        if "10.0.0.1" in url:
            raise webfetch.Blocked("private")
    monkeypatch.setattr(webfetch, "check_url", fake_check)

    def handler(req):
        return httpx.Response(302, headers={"location": "http://10.0.0.1/secret"})
    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await webfetch.fetch_text(c, "https://example.com/") == ""


def test_extract_and_passages():
    html = ("<html><head><script>var x=1</script><style>p{}</style></head><body><nav>Home | News | Sports</nav>"
            "<article><h1>Pitt edges Oregon State</h1><p>" + "Filler words about the stadium and the crowd. " * 30 +
            "</p><p>Pittsburgh beat Oregon State 35-33 on Friday night in Boise after a late field goal.</p>"
            "</article><footer>Copyright</footer></body></html>")
    text = webfetch.extract(html)
    assert "var x" not in text and "Copyright" not in text and "Home | News" not in text
    ps = webfetch.passages(text, "Pittsburgh Oregon State score 35-33", k=1)
    assert "35-33" in ps[0]


# ── grounding ──────────────────────────────────────────────────────────────────────────────────

class FakeSearch:
    name = "fake"

    def __init__(self, hits=None, error=None):
        self.hits, self.error, self.queries = hits or [], error, []

    async def search(self, q, *, time_range=None, news=False):
        self.queries.append(q)
        return SearchResult(list(self.hits), provider=self.name, error=self.error)


async def test_grounding_puts_exact_feed_first_and_cites():
    s = FakeSearch([Hit("Pitt 35-33 Oregon State - ESPN", "https://www.espn.com/x", "Pitt won 35-33")])
    g = webground.Grounder(s, SportsFeed(httpx.AsyncClient(transport=httpx.MockTransport(espn))))
    r = await g.ground("Who won the Beavers football game last night?", [], NOW, "sports")
    assert r.status == "ok" and r.providers == ["espn", "fake"]
    assert r.evidence[0].kind == "feed" and s.queries == ["Who won the Beavers football game October 2, 2026"]
    block = webground.evidence_block(r)
    assert block.startswith("<web_results>\n[1] FINAL") and "[2] Pitt 35-33" in block


async def test_query_with_personal_data_never_leaves():
    s = FakeSearch()
    r = await webground.Grounder(s).ground("is jane.doe@example.com still the CEO today?", [], NOW, "office")
    assert r.status == "blocked" and s.queries == [] and "email" in r.note


async def test_search_failure_is_reported_not_hidden():
    r = await webground.Grounder(FakeSearch(error="ConnectError: refused")).ground(
        "latest news on the Fed decision", [], NOW, "news")
    assert r.status == "error" and "web search failed" in r.note
    msgs = webground.inject_unavailable([{"role": "user", "content": "latest Fed news?"}], r)
    assert "couldn't confirm" in msgs[-1]["content"] and "knowledge cutoff" in msgs[-1]["content"]


def test_inject_date_merges_into_existing_system_message():
    msgs = webground.inject_date([{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}],
                                 NOW, grounded=False)
    assert len(msgs) == 2 and msgs[0]["content"].startswith("Today is Saturday, October 3, 2026 (America/New_York).")
    later = webground.inject_date([{"role": "user", "content": "hi"}], NOW.replace(hour=23, minute=59))
    assert later[0] == webground.inject_date([{"role": "user", "content": "hi"}], NOW)[0]   # same all day
    assert msgs[0]["content"].endswith("Be terse.")


def test_evidence_rides_on_the_last_user_turn_only():
    g = webground.Grounding("ok", evidence=[webground.Evidence("t", "https://a", "fact", "snippet")])
    msgs = webground.inject_evidence([{"role": "user", "content": "first"}, {"role": "assistant", "content": "a"},
                                      {"role": "user", "content": "second"}], g)
    assert msgs[0]["content"] == "first" and msgs[2]["content"].startswith("<web_results>")


@pytest.mark.parametrize("text,hit", [
    ("As of my last update in October 2023, I don't have real-time data.", True),
    ("I don't have access to real-time information, but", True),
    ("My knowledge cutoff is 2024.", True),
    ("Pittsburgh won 35-33.", False),
    ("The update to iOS 27 adds real-time translation.", False),
])
def test_disclaimer_detector(text, hit):
    assert webground.is_disclaimer(text) is hit


def test_web_search_privacy():
    assert policy.may_send(policy.DataClass.CONFIDENTIAL, "web_search")
    assert not policy.may_send(policy.DataClass.RESTRICTED, "web_search")
    assert not policy.may_send(policy.DataClass.CONFIDENTIAL, "jev")


# ── gateway ────────────────────────────────────────────────────────────────────────────────────

KEY = "test-key-web"
H = {"Authorization": f"Bearer {KEY}", "X-LIF-Timezone": "America/New_York"}
calls: list[dict] = []
answers: list[str] = []


def model_upstream(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    body = json.loads(req.content)
    calls.append({"host": req.url.host, **body})
    text = answers.pop(0) if answers else "Pittsburgh won 35-33 [1]."
    if body.get("stream"):
        words = [w + " " for w in text.split(" ")]
        lines = [f"data: {json.dumps({'choices': [{'delta': {'content': w}}]})}\n\n" for w in words]
        lines.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 9, 'completion_tokens': 3}})}\n\n")
        lines.append("data: [DONE]\n\n")
        return httpx.Response(200, content="".join(lines).encode(), headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text},
                                                  "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 9, "completion_tokens": 3}})


@pytest.fixture()
def gw_client(monkeypatch):
    monkeypatch.setenv("LIF_GATEWAY_KEYS", f"tester:{KEY}")
    monkeypatch.delenv("LIF_CONTROLLER_URL", raising=False)
    from fastapi.testclient import TestClient
    from lif.gateway import app as gw
    from lif.gpu.state import BlerbzState, Snapshot
    with TestClient(gw.app) as c:
        for t in gw.S.tasks:
            t.get_loop().call_soon_threadsafe(t.cancel)
        __import__("time").sleep(0.2)
        gw.S.http = httpx.AsyncClient(transport=httpx.MockTransport(model_upstream))
        gw.S.router.client = gw.S.http
        for n in gw.S.router.health:
            gw.S.router.health[n].ok = True
        gw.S.gpu._snap = Snapshot(ts=__import__("time").time(), reachable=True, state=BlerbzState.LOW, reason="test")
        search = FakeSearch([Hit("Pitt 35-33 Oregon State - ESPN", "https://www.espn.com/x", "Pitt won 35-33")])
        gw.S.grounder = webground.Grounder(search, SportsFeed(httpx.AsyncClient(transport=httpx.MockTransport(espn))),
                                           weather=None)
        # The gateway resolves "last night" against the clock: pin it to the fixtures' Saturday morning.
        monkeypatch.setattr(webground, "now_in", lambda tz=None: NOW.astimezone(ZoneInfo(tz)) if tz else NOW)
        calls.clear()
        answers.clear()
        yield c, gw, search


def _user(body):
    return [m for m in body["messages"] if m["role"] == "user"][-1]["content"]


def test_auto_live_question_is_grounded_and_never_goes_to_instant(gw_client):
    c, gw, search = gw_client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "temperature": 0, "messages": [
        {"role": "user", "content": "Who won the Beavers football game last night?"}]})
    lif = r.json()["lif"]
    assert lif["route_decision"]["decision"] == "web" and lif["route_decision"]["provider"] == "rules"
    assert lif["alias"] in ("local/web", "local/default")
    assert gw.S.router.profiles[lif["served_by"]].params_b >= 4
    assert lif["web"]["status"] == "ok" and lif["web"]["query"] == "Who won the Beavers football game October 2, 2026"
    assert lif["web"]["sources"][0]["url"].endswith("/401858245")
    sent = calls[-1]
    assert sent["messages"][0]["role"] == "system" and "October 3, 2026" in sent["messages"][0]["content"]
    assert _user(sent).startswith("Current local time:") and "<web_results>\n[1] FINAL" in _user(sent)
    assert "never question today's date" in _user(sent) and "web_results" not in sent["messages"][0]["content"]
    # Live answers are never served from the deterministic cache.
    c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "temperature": 0, "messages": [
        {"role": "user", "content": "Who won the Beavers football game last night?"}]})
    assert len(calls) == 2


def test_auto_ordinary_question_gets_date_but_no_lookup(gw_client):
    c, _, search = gw_client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "messages": [
        {"role": "user", "content": "Write a haiku about rivers"}]})
    assert r.json()["lif"]["web"]["status"] == "not_needed" and search.queries == []
    assert calls[-1]["messages"][0]["content"].startswith("Today is ")


def test_explicit_alias_is_untouched_unless_opted_in(gw_client):
    c, _, search = gw_client
    body = {"model": "local/fast", "messages": [{"role": "user", "content": "Who won the Beavers game last night?"}]}
    r = c.post("/v1/chat/completions", headers=H, json=body)
    assert "web" not in r.json()["lif"] and search.queries == []
    assert calls[-1]["messages"] == body["messages"]
    r = c.post("/v1/chat/completions", headers=H, json={**body, "lif": {"web": "auto"}})
    assert r.json()["lif"]["web"]["status"] == "ok" and r.json()["lif"]["alias"] == "local/fast"


def test_restricted_prompt_is_never_looked_up(gw_client):
    c, _, search = gw_client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "messages": [
        {"role": "user", "content": "Who won the game last night? password: hunter2hunter2"}]})
    assert search.queries == [] and r.json()["lif"]["web"]["status"] == "blocked"


def test_cutoff_disclaimer_is_replaced_nonstreaming(gw_client):
    c, gw, search = gw_client
    answers.extend(["As of my last update in October 2023, I don't have real-time data.", "Pitt won 35-33 [1]."])
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "messages": [
        {"role": "user", "content": "Which team has the best record?"}]})
    d = r.json()
    assert d["choices"][0]["message"]["content"] == "Pitt won 35-33 [1]."
    assert d["lif"]["route_decision"]["provider"] == "guard" or d["lif"]["web"]["decision"]["provider"] == "guard"
    assert len(search.queries) == 1


def test_cutoff_disclaimer_is_replaced_streaming(gw_client):
    c, gw, search = gw_client
    answers.extend(["As of my last update in October 2023, I don't have real-time data. Check ESPN.",
                    "Pitt won 35-33 [1]."])
    with c.stream("POST", "/v1/chat/completions", headers=H, json={"model": "local/auto", "stream": True,
                  "messages": [{"role": "user", "content": "Which team has the best record?"}]}) as r:
        text, meta = "", None
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                ch = json.loads(line[6:])
                for x in ch.get("choices") or []:
                    text += (x.get("delta") or {}).get("content") or ""
                meta = ch.get("lif") or meta
    assert "last update" not in text and text.strip() == "Pitt won 35-33 [1]."
    assert meta["web"]["status"] == "ok" and meta["web"]["decision"]["provider"] == "guard"


def test_normal_streaming_answer_passes_through_whole(gw_client):
    c, *_ = gw_client
    answers.append("Rivers bend. Stones sing. Water remembers everything it touched on the way down.")
    with c.stream("POST", "/v1/chat/completions", headers=H, json={"model": "local/auto", "stream": True,
                  "messages": [{"role": "user", "content": "What is a river?"}]}) as r:
        text = "".join((x.get("delta") or {}).get("content") or ""
                       for line in r.iter_lines() if line.startswith("data: ") and line != "data: [DONE]"
                       for x in json.loads(line[6:]).get("choices") or [])
    assert text.strip() == "Rivers bend. Stones sing. Water remembers everything it touched on the way down."


# ── console receipt ────────────────────────────────────────────────────────────────────────────

def test_console_receipt_shows_query_sources_and_web_step():
    from lif.console.routes import ai
    o = ai._Outcome()
    o.headers = {"x-lif-served-by": "qwen3-4b-instruct-2507-q4km-cpu", "x-lif-web": "ok"}
    o.meta = {"alias": "local/default", "served_by": "qwen3-4b-instruct-2507-q4km-cpu",
              "route_decision": {"decision": "web", "confidence": 0.95, "provider": "rules", "action": "auto"},
              "web": {"status": "ok", "query": "Who won the Beavers football game October 2, 2026",
                      "providers": ["espn", "searxng"], "ms": 650.0,
                      "sources": [{"n": 1, "title": "ESPN: Pittsburgh Panthers at Oregon State Beavers",
                                   "url": "https://www.espn.com/college-football/game/_/gameId/401858245"},
                                  {"n": 2, "title": "bad", "url": "javascript:alert(1)"}],
                      "decision": {"decision": "web", "provider": "rules"}}}
    r = ai.build_receipt(o, "auto", "local/auto", "local_only", 3200.0, False, "m1")
    assert r.web is not None and r.web.status == "ok" and r.web.query.endswith("October 2, 2026")
    assert [s.n for s in r.web.sources] == [1]                    # non-http links are dropped
    assert [s.label for s in r.route] == ["Auto", "Web search", "Local Balanced"]
    assert r.decision.decision.startswith("Look it up live")
    assert any(t.label == "web query sent" for t in r.tech)


def test_console_receipt_marks_guard_regeneration():
    from lif.console.routes import ai
    o = ai._Outcome()
    o.meta = {"alias": "local/default", "served_by": "x",
              "route_decision": {"decision": "instant", "confidence": 0.7, "provider": "rules", "action": "validate"},
              "web": {"status": "ok", "query": "q", "sources": [], "ms": 10.0,
                      "decision": {"decision": "web", "provider": "guard", "action": "regenerate", "confidence": 1.0}}}
    r = ai.build_receipt(o, "auto", "local/auto", "local_only", 100.0, False, "m2")
    assert r.web.by_guard and "Answer check" in [f.value for f in r.decision.why if f.label == "Decided by"][0]


def test_console_sends_web_auto_and_timezone():
    from lif.console.contracts import MessageRequest
    assert MessageRequest(content="x", tz="America/New_York").tz == "America/New_York"
    with pytest.raises(Exception):
        MessageRequest(content="x", tz="../../etc/passwd")


@pytest.mark.parametrize("q", ["What should I do about my doctor's appointment tomorrow?", "where do you live",
                               "Can we meet this weekend?", "How do I prepare for my interview next week?",
                               "Remind me what I said yesterday", "What's on my schedule this weekend?",
                               "How much is my car payment this month?", "Remind me about my team meeting tomorrow",
                               "Can we play a game tonight?", "How did my stocks do today?"])
def test_personal_time_questions_never_become_queries(gw_client, q):
    c, _, search = gw_client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "messages": [
        {"role": "user", "content": q}]})
    assert r.status_code == 200 and search.queries == []


def test_public_prompt_rules_still_force_lookup(gw_client, monkeypatch):
    """Under PUBLIC the fabric may ask Jev; a confident rules 'yes' is never overruled to 'no'."""
    c, gw, search = gw_client
    from lif.decision.types import DecisionResult
    real = gw.S.fabric.evaluate_group

    async def jev_says_no(names, state, data_class=None, thresholds=None):
        out = await real(names, state, data_class, thresholds)
        out["needs-live-data"] = DecisionResult(decision="no", confidence=0.9, probabilities={}, provider="jev",
                                                decision_ref="needs-live-data@v1", action="auto")
        return out
    monkeypatch.setattr(gw.S.fabric, "evaluate_group", jev_says_no)
    r = c.post("/v1/chat/completions", headers={**H, "X-LIF-Data-Class": "PUBLIC"}, json={
        "model": "local/auto", "messages": [{"role": "user", "content": "Who won the Beavers game last night?"}]})
    assert r.json()["lif"]["web"]["status"] == "ok" and len(search.queries) == 1


@pytest.mark.parametrize("q,live", [
    ("What did the Fed decide at its September meeting?", True),
    ("When did iOS 27 come out?", True),
    ("Explain how a Fed rate hike affects bonds", False),
    ("How does the stock market work?", False),
    ("My meeting is in October", False),
    ("Is it going to rain tomorrow where I live?", True),
    ("Did my team win last night?", False),
    ("Did my Steelers win on Sunday?", True),
    ("What's my NFL team's record?", True),
])
def test_assess_news_concepts_and_personal(q, live):
    assert assess(q).live is live


STANDINGS = {"children": [{"name": "American Football Conference", "standings": {"entries": [
    {"team": {"id": "12", "displayName": "Kansas City Chiefs"},
     "stats": [{"name": "winPercent", "value": 1.0}, {"name": "pointDifferential", "value": 38},
               {"name": "overall", "displayValue": "3-0"}]},
    {"team": {"id": "23", "displayName": "Pittsburgh Steelers"},
     "stats": [{"name": "winPercent", "value": 0.667}, {"name": "pointDifferential", "value": 5},
               {"name": "overall", "displayValue": "2-1"}]}]}}]}
RANKINGS = {"rankings": [{"name": "AP Top 25", "headline": "AP Poll Week 5", "ranks": [
    {"current": 1, "team": {"id": "251", "location": "Texas"}, "recordSummary": "4-0"},
    {"current": 2, "team": {"id": "61", "location": "Georgia"}, "recordSummary": "4-0"}]}]}


def espn_tables(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p.endswith("/nfl/standings"):
        return httpx.Response(200, json=STANDINGS)
    if p.endswith("/college-football/rankings"):
        return httpx.Response(200, json=RANKINGS)
    if p.endswith("/nfl/teams"):
        return httpx.Response(200, json={"sports": [{"leagues": [{"teams": [
            {"team": {"id": "12", "location": "Kansas City", "name": "Chiefs", "displayName": "Kansas City Chiefs"}}]}]}]})
    return espn(req)


async def test_best_record_question_reads_the_standings():
    feed = SportsFeed(httpx.AsyncClient(transport=httpx.MockTransport(espn_tables)))
    r = await feed.lookup("Who has the best record in the NFL?", NOW)
    assert r.lines[0].startswith("STANDINGS (NFL") and "Kansas City Chiefs 3-0; Pittsburgh Steelers 2-1" in r.lines[0]
    assert len(r.sources) == len(r.lines) and r.sources[0]["url"].endswith("/nfl/standings")


async def test_best_college_team_reads_the_poll_and_names_unranked_teams():
    feed = SportsFeed(httpx.AsyncClient(transport=httpx.MockTransport(espn_tables)))
    r = await feed.lookup("Which college football team is the best?", NOW)
    assert r.lines == ["RANKING (AP Poll Week 5): 1. Texas (4-0); 2. Georgia (4-0)."]
    r = await feed.lookup("Where are the Beavers ranked in college football?", NOW)
    assert "Oregon State Beavers: not ranked in the top 2 of AP Top 25." in r.lines
    assert len(r.sources) == len(r.lines)


def test_guard_never_looks_up_personal_questions(gw_client):
    c, _, search = gw_client
    answers.append("I don't have access to real-time information about what you said yesterday.")
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "messages": [
        {"role": "user", "content": "Remind me what I said yesterday?"}]})
    assert search.queries == [] and r.json()["choices"][0]["message"]["content"].startswith("I don't have access")


def test_web_path_fails_open(gw_client, monkeypatch):
    c, gw, search = gw_client

    async def boom(*a, **kw):
        raise KeyError("unknown decision 'needs-live-data'")
    monkeypatch.setattr(gw.S.fabric, "evaluate", boom)
    monkeypatch.setattr(gw.S.fabric, "evaluate_group", boom)
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/fast", "lif": {"web": "auto"},
                                                        "messages": [{"role": "user", "content": "Who won last night?"}]})
    assert r.status_code == 200 and "web" not in r.json()["lif"] and search.queries == []


def test_explicit_alias_never_sends_the_prompt_to_jev(gw_client, monkeypatch):
    c, gw, _ = gw_client
    seen_classes = []
    real = gw.S.fabric.evaluate

    async def spy(name, state, data_class=None, thresholds=None):
        seen_classes.append((name, data_class))
        return await real(name, state, data_class, thresholds)
    monkeypatch.setattr(gw.S.fabric, "evaluate", spy)
    c.post("/v1/chat/completions", headers={**H, "X-LIF-Data-Class": "PUBLIC"}, json={
        "model": "local/fast", "lif": {"web": "auto"}, "messages": [{"role": "user", "content": "Write a haiku"}]})
    assert ("needs-live-data", "CONFIDENTIAL") in seen_classes
