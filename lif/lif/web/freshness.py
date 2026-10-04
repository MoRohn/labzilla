"""Does this question need information newer than the model's training, and what do we search for?

`assess(text)` is the high-recall rule set behind the `needs-live-data` decision: a question about a
score, the weather, a price, the news, "who is the current …", or anything anchored to a relative date
("last night", "this weekend") or to a year past the models' training needs live data.

`build_query(text, history, now)` turns the question into the one short string that may leave the box:
relative dates become absolute dates in the user's timezone ("last night" at 07:00 on 3 Oct 2026 →
"October 2, 2026"), filler is dropped, and a short follow-up ("what about Clemson?") borrows the subject
of the previous user turn. The conversation itself is never sent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from lif.common import config

# ── signals ────────────────────────────────────────────────────────────────────────────────────

_REL_TIME = re.compile(
    r"(?i)\b(?:today'?s?|tonight'?s?|yesterday'?s?|tomorrow'?s?|last\s+(?:night|evening|week(?:end)?|month|year|season|game|match|race)"
    r"|this\s+(?:morning|afternoon|evening|week(?:end)?|month|year|season|past\s+\w+)|right now|at the moment|these days"
    r"|so far(?: this \w+)?|as of (?:now|today)|latest|most recent|recent(?:ly)?|newest|upcoming|breaking|live (?:score|scores|updates?|now|coverage|results?)"
    r"|what'?s new|what is new|new features?|next\s+(?:game|match|race|election|week(?:end)?|season|fight)|just (?:announced|released|happened|came out)"
    r"|(?:on |last |this )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:'s)?(?: night)?)\b")
_CURRENT = re.compile(r"(?i)\b(?:current(?:ly)?|now|still|anymore|nowadays|presently)\b")
_SPORTS = re.compile(
    r"(?i)\b(?:who won|did (?:the )?\w+(?: \w+)? (?:win|lose)|won the|final score|scores?|beat|lost to|standings|playoffs?"
    r"|kick-?off|tip-?off|box score|rankings?|ap poll|top 25|super bowl|world series|stanley cup|nba finals"
    r"|march madness|bowl game|game|match|race|fight|tournament|grand prix|schedule|record|best team|top team|team"
    r"|ranked|rankings?|poll|standings|leading|first place|number one|#1|mvp|favou?rite to win|odds|playoff picture)\b")
_SPORT_WORDS = re.compile(
    r"(?i)\b(?:football|basketball|baseball|hockey|soccer|nfl|nba|mlb|nhl|wnba|mls|ncaa|cfb|college football"
    r"|f1|formula 1|ufc|tennis|golf|pga|premier league|champions league|quarterback|touchdown)\b")
_WEATHER = re.compile(r"(?i)\b(?:weather|forecast|temperature|raining|snowing|rain|snow|hurricane|tornado|storm|heat wave"
                      r"|humidity|air quality|wildfire)\b")
_FINANCE = re.compile(r"(?i)\b(?:stock price|share price|stock|shares|trading at|market cap|exchange rate|bitcoin|btc|ethereum"
                      r"|crypto|price of|how much (?:is|does|are)|dow jones|\bdow\b|nasdaq|s&p 500|interest rates?"
                      r"|mortgage rates?|inflation|gas prices?|earnings)\b")
_NEWS = re.compile(r"(?i)\b(?:news|headlines?|happened|announced|announcement|election|elected|polls?|polling|vote[ds]?"
                   r"|died|passed away|released|release date|launch(?:ed|es)?|verdict|lawsuit|resign(?:ed|s)?|appointed"
                   r"|fired|hired|merger|acquired|acquisition|outage|recall|ceasefire|war in|update on|status of"
                   r"|box office|trending|version of|came out|come out|coming out|decid(?:e|ed|es)|ruled|ruling|meeting|summit|deal"
                   r"|fed|federal reserve|fomc|central bank|ecb|congress|senate|parliament|supreme court|white house"
                   r"|tariffs?|sanctions?|strike|indicted|charged|sentenced)\b")
# Proper news subjects: these stay "live" even in a first-person sentence ("what did my senator vote for?").
_NEWS_STRONG = re.compile(r"(?i)\b(?:news|headlines?|election|elected|polls?|polling|fed|federal reserve|fomc|central bank"
                          r"|ecb|congress|senate|parliament|supreme court|white house|tariffs?|sanctions?|indicted"
                          r"|ceasefire|war in|box office|earnings)\b")
_TIME_WORDS = {"night", "morning", "afternoon", "evening", "today", "tonight", "tomorrow", "yesterday", "week", "weekend",
               "month", "year", "season", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
               "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december", "last", "this", "next"}
_LEAGUE = re.compile(r"(?i)\b(?:nfl|nba|mlb|nhl|wnba|mls|ncaa|sec|acc|big ten|big 12|pac-12|premier league|la liga"
                     r"|serie a|bundesliga|champions league|f1|formula 1|nascar|pga|ufc|super bowl|world series"
                     r"|stanley cup|world cup|olympics)\b")
_WEATHER_STRONG = re.compile(r"(?i)\b(?:weather|forecast|temperature|rain(?:ing)?|snow(?:ing)?|hurricane|tornado|storm"
                             r"|humidity|air quality|heat wave|wildfire)\b")
_MARKET = re.compile(r"(?i)\b(?:stock|shares|share price|ticker|market cap|price of|exchange rate|bitcoin|btc|ethereum"
                     r"|crypto|nasdaq|dow jones|s&p 500|interest rates?|mortgage rates?|inflation|gas prices?)\b")
# Concept questions: the answer doesn't change week to week, so no lookup unless something dates them.
_CONCEPT = re.compile(r"(?i)^\s*(?:please\s+)?(?:explain|describe|define|teach me|what (?:is|are) (?:a|an)\b|how (?:does|do)"
                      r"|why (?:does|do|is|are)|what(?:'s| is) the difference)")
# A product named with a version number ("iOS 27", "Python 3.15", "RTX 6090") is usually about a release.
_PRODUCT_VERSION = re.compile(r"(?i)\b(?:ios|ipados|macos|watchos|android|windows|iphone|pixel|galaxy|python|node(?:\.js)?"
                              r"|java|rust|go|gpt|claude|gemini|llama|qwen|chrome|firefox|ubuntu|fedora|debian|kubernetes"
                              r"|k3s|rtx|playstation|xbox|switch)[ -]?v?\d+(?:\.\d+)?\b")
_MONTH_REF = re.compile(r"(?i)\b(?:in|at|on|this|last|its|their|the|since|from|during)\s+(?:its\s+|their\s+)?"
                        r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\b(?!\s+\d{4})")
_OFFICE = re.compile(r"(?i)\bwho(?:'s| is| are)\s+(?:the\s+)?(?:current\s+|new\s+)?(?:president|vice president|ceo|prime minister"
                     r"|governor|mayor|senator|speaker|chancellor|pope|king|queen|head coach|coach|champion|leader"
                     r"|owner|chair(?:man|woman)?|secretary)\b")
_CLOCK = re.compile(r"(?i)^\s*(?:what(?:'s| is)\s+)?(?:the\s+)?(?:today'?s\s+date|(?:what\s+)?(?:day|date|time|year)\s+is\s+it"
                    r"|what\s+(?:day|date|year)\s+(?:is\s+)?(?:it\s+)?today)\b")
_YEAR = re.compile(r"\b(20[2-9]\d)\b")
_OLD_YEAR = re.compile(r"\b(1[89]\d\d|20[01]\d)\b")
# Work the model does from the text in front of it: no lookup can help.
_TASK = re.compile(r"(?i)^\s*(?:please\s+)?(?:write|draft|compose|rewrite|translate|summari[sz]e|proofread|fix|refactor"
                   r"|implement|convert|format|explain this|explain the following|generate|create a (?:poem|story|list)"
                   r"|imagine|pretend|role-?play)\b")
# About the person asking, not the world: never turned into a search query unless it also names a topic
# that is public by nature (a score, the weather, a price, the news, an office holder).
_PERSONAL = re.compile(r"(?i)\b(?:i|i'm|i've|i'd|i'll|me|my|mine|myself|we|we're|our|us)\b")
_CODE = re.compile(r"(?i)```|\bdef |\bclass |function\s*\(|#include|traceback|\bregex\b|\bSELECT\b.+\bFROM\b")


@dataclass
class Freshness:
    live: bool
    confidence: float
    kind: str = "none"                     # sports | weather | finance | news | office | general | clock | none
    signals: list[str] = field(default_factory=list)


def _cutoff_year() -> int:
    return int(config.get("web.model_knowledge_year", 2025))


def assess(text: str) -> Freshness:
    """High recall by design: a needless search costs ~1 s, a missed one is a wrong or refused answer."""
    t = (text or "").strip()
    if not t:
        return Freshness(False, 0.99)
    if len(t) > 2000 or _CODE.search(t):
        return Freshness(False, 0.9, signals=["long_or_code"])
    if _CLOCK.search(t) and len(t) < 60:
        return Freshness(False, 0.9, "clock", ["clock"])      # the injected date answers it
    sig: list[str] = []
    rel, cur = bool(_REL_TIME.search(t) or _MONTH_REF.search(t)), bool(_CURRENT.search(t))
    years = [int(y) for y in _YEAR.findall(t)]
    recent_year = any(y >= _cutoff_year() for y in years)
    old_only = bool(_OLD_YEAR.search(t)) and not recent_year and not rel
    if rel:
        sig.append("relative_time")
    if cur:
        sig.append("current")
    if recent_year:
        sig.append("recent_year")
    product = bool(_PRODUCT_VERSION.search(t))
    if product:
        sig.append("product_version")
    topic = ("weather" if _WEATHER.search(t) else "finance" if _FINANCE.search(t) else
             "sports" if _SPORTS.search(t) and (_SPORT_WORDS.search(t) or rel or re.search(r"(?i)who won|score", t))
             else "office" if _OFFICE.search(t) else "news" if _NEWS.search(t) else None)
    if topic:
        sig.append(topic)
    task = bool(_TASK.search(t))
    if task:
        sig.append("task")

    # Generic topic words ("schedule", "game", "how much is") are everyday words in a personal sentence;
    # only a clearly public anchor lets a first-person question be looked up.
    words = [w.strip(".,!?;:'\"()") for w in t.split()[1:]]
    proper = any(w[:1].isupper() and w not in ("I", "I'm", "I've", "I'd", "I'll")
                 and not _SPORTS.fullmatch(w.lower()) and w.lower() not in _TIME_WORDS for w in words)
    public_topic = bool(_SPORT_WORDS.search(t) or _LEAGUE.search(t) or _WEATHER_STRONG.search(t)
                        or _MARKET.search(t) or _OFFICE.search(t) or _NEWS_STRONG.search(t)
                        or topic == "sports" and proper)          # "did my Steelers win?" names a public team
    if _PERSONAL.search(t) and not public_topic:
        return Freshness(False, 0.75, "personal", [*sig, "personal"])
    if _CONCEPT.search(t) and not (rel or cur or recent_year or product):
        return Freshness(False, 0.75, topic or "none", [*sig, "concept"])
    if old_only and topic != "weather":
        return Freshness(False, 0.8, topic or "none", [*sig, "historical"])
    if topic == "weather":
        conf = 0.95
    elif topic in ("sports", "news", "finance") and (rel or cur or recent_year):
        conf = 0.95
    elif topic == "office":
        conf = 0.9
    elif topic == "finance":
        conf = 0.85
    elif topic == "sports":
        conf = 0.8                         # "who won the super bowl" means the latest one
    elif rel and not task:
        conf = 0.8
    elif product and not task:
        conf = 0.8
    elif recent_year:
        conf = 0.75
    elif topic == "news":
        conf = 0.65
    elif cur and re.search(r"(?i)\?\s*$|^(?:who|what|when|where|which|is|are|does|did|how)\b", t):
        conf = 0.6
    else:
        return Freshness(False, 0.8, "none", sig)
    if task and conf < 0.95:
        return Freshness(False, 0.7, topic or "general", sig)
    return Freshness(True, conf, topic or "general", sig)


def assess_turn(text: str, history: list[str]) -> Freshness:
    """A short follow-up ("what about Clemson?", "and the score?") inherits the previous question's need."""
    f = assess(text)
    if f.live or not history or len(_strip(text).split()) > 8 or not _FOLLOWUP.search(text):
        return f
    prev = assess(history[-1])
    if prev.live:
        return Freshness(True, min(prev.confidence, 0.9), prev.kind, [*f.signals, "follow_up"])
    return f


# ── dates ──────────────────────────────────────────────────────────────────────────────────────

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _fmt(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


@dataclass
class Resolved:
    text: str                       # the question with relative dates replaced by absolute ones
    target: date | None = None      # the day the question is about, when it names one
    span: tuple[date, date] | None = None


def resolve_dates(text: str, now: datetime) -> Resolved:
    today = now.date()
    out = Resolved(text)

    def sub(pattern: str, d: date | None, span: tuple[date, date] | None = None, label: str | None = None) -> None:
        nonlocal out
        rx = re.compile(pattern, re.I)
        if rx.search(out.text):
            out.text = rx.sub(label or _fmt(d), out.text, count=1)
            if out.target is None and d is not None:
                out.target = d
            if out.span is None and span is not None:
                out.span = span

    yday = today - timedelta(days=1)
    sub(r"\blast night'?s?\b|\blast evening'?s?\b|\byesterday'?s?\b", yday, (yday, yday))
    sub(r"\btonight'?s?\b|\btoday'?s?\b|\bthis (?:morning|afternoon|evening)'?s?\b", today, (today, today))
    sub(r"\btomorrow'?s?\b", today + timedelta(days=1))
    wd = today.weekday()
    sat_recent = today - timedelta(days=(wd - 5) % 7)              # the latest Saturday, today included
    sat_this = sat_recent if wd >= 5 else today + timedelta(days=5 - wd)
    sat_last = sat_recent - timedelta(days=7) if wd >= 5 else sat_recent
    for phrase, sat in (("this weekend", sat_this), ("last weekend", sat_last)):
        sub(rf"\b{phrase}\b", sat, (sat, sat + timedelta(days=1)), f"weekend of {_fmt(sat)}")
    m = re.search(r"(?i)\b(next |this |last |on )?(" + "|".join(_DAYS) + r")(?:'s)?(?: night)?\b", out.text)
    if m:
        target_wd = _DAYS.index(m.group(2).lower())
        mod = (m.group(1) or "").strip().lower()
        if mod == "next":
            d = today + timedelta(days=(target_wd - wd) % 7 or 7)
        elif mod == "this" and target_wd >= wd:
            d = today + timedelta(days=target_wd - wd)
        else:                            # "on Saturday", "Saturday's game", "last Saturday": the most recent one
            back = (wd - target_wd) % 7
            # Today's own weekday counts only for present/future questions ("who plays on Sunday?"); a past-tense
            # one asked on that day ("did they win on Sunday?" on Sunday morning) means the week before.
            if back == 0 and (mod == "last" or _PAST.search(out.text)):
                back = 7
            d = today - timedelta(days=back)
        out.text = out.text[:m.start()] + _fmt(d) + out.text[m.end():]
        out.target = out.target or d
    if re.search(r"(?i)\b(?:this|last) (?:week|month|year|season)\b", out.text) and out.target is None:
        out.text = re.sub(r"(?i)\bthis (week|month)\b", lambda x: f"{x.group(1)} of {_fmt(today)}", out.text, count=1)
        out.text = re.sub(r"(?i)\bthis year\b", str(today.year), out.text, count=1)
        out.text = re.sub(r"(?i)\bthis season\b", f"{today.year} season", out.text, count=1)
    return out


# ── query ──────────────────────────────────────────────────────────────────────────────────────

_FILLER = re.compile(r"(?i)^\s*(?:hey|hi|ok(?:ay)?|so|please|quick question[:,]?|"
                     r"(?:can|could|would) you (?:please )?(?:tell me|let me know|find out|look up|check|search)(?: for)?|"
                     r"do you know|i(?:'d| would) like to know|i want to know|tell me|look up|search(?: for)?|find out)\b[\s,:]*")
_PAST = re.compile(r"(?i)\b(?:did|was|were|won|lost|beat|happened|went|scored|played|finished|how did)\b")
_FOLLOWUP = re.compile(r"(?i)^\s*(?:and|what about|how about|and what about|what of|also|same for)\b|\b(?:it|they|them|he|she"
                       r"|that|those|this one|their|his|her)\b")
_MAX_QUERY = 200


def _strip(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):
        t2 = _FILLER.sub("", t)
        if t2 == t:
            break
        t = t2
    return t.rstrip(" ?!.") or text.strip()


@dataclass
class Query:
    q: str                          # the only text that leaves the box
    target: date | None
    span: tuple[date, date] | None
    borrowed_context: bool = False


def build_query(text: str, history: list[str], now: datetime, kind: str | None = None) -> Query:
    """`history`: earlier user turns, oldest first. Only the latest one may contribute, and only its subject."""
    t = _strip(text)
    borrowed = False
    if history and len(t.split()) <= 8 and _FOLLOWUP.search(t):
        prev = _strip(history[-1])
        t = re.sub(r"(?i)^\s*(?:and|what about|how about|and what about|what of|also|same for)\b\s*", "", t)
        t = f"{prev} {t}".strip()
        borrowed = True
    r = resolve_dates(t, now)
    q = r.text
    has_date = r.target is not None or bool(re.search(r"\b20\d\d\b", q))
    kind = kind or assess_turn(text, history).kind
    if not has_date and kind in ("sports", "news", "finance", "office", "weather", "general"):
        q = f"{q} {_fmt(now.date())}" if kind in ("weather", "finance") else f"{q} {now.year}"
    return Query(q[:_MAX_QUERY], r.target, r.span, borrowed)
