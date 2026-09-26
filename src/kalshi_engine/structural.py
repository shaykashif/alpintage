"""Deterministic relation rules for families of markets whose logic can be
read straight off their tickers, titles and rules -- instead of trusting Jev.

Why: 1,505 hand-checked pairs (data/relation_labels.jsonl, 2026-09-26)
showed Jev missing 705 true relations and confirming 40 false ones, both
concentrated in a few highly structured families. No threshold fixes that
(Jev is confidently wrong on some and quietly unsure on others), but the
families themselves are regular enough to judge exactly:

  - crypto   Polymarket Binance SOL/XRP/... markets: noon close in a range /
             above / below X on a date; 1-minute high reaches X or low dips
             to X within a day, date range or month.
  - ytviews  Kalshi YouTube global daily views above X: on a date, at any
             point in a week, at any point in a month.
  - album    Kalshi Luminate pure album sales vs album-equivalent units
             (pure sales are a component of equivalent units).
  - econ     Kalshi BLS releases (CPI, core, YoY, U-3): "above X" vs "exactly
             Y" for the same number and month; ISM PMIs across venues
             (Kalshi at-least/above vs Polymarket one-decimal brackets).
  - centralbank  policy-rate decisions at one bank's meeting, across venues:
             Kalshi buckets vs Polymarket brackets (which round up to 25 bp).
  - treasury Kalshi daily par yield "above X on D" vs "above/below X on any
             business day" in a window containing D, same tenor.
  - rank     chart positions (#1, #2, top N) -- Netflix, Billboard, YouTube,
             App Store: same title at two ranks of one chart can't both
             happen; #N implies top M >= N; DIFFERENT titles at different
             ranks are unrelated (Jev called those "not both" at 0.86).

judge(a, b) returns a Verdict when both markets belong to one family and
their parameters were parsed with certainty -- its relations (possibly
none) then replace Jev's. restrict(a, b, rels) is a safety filter applied
to Jev's own relations when no family rule applies. Anything the rules
can't read with certainty returns None and stays Jev's call.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .relations import Contract

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MON3 = {m[:3]: i for m, i in MONTHS.items()}
INF = float("inf")


@dataclass
class Verdict:
    relations: list[str]
    family: str
    note: str


# ---- small parsers -------------------------------------------------------------

def _year(c: Contract) -> int | None:
    m = re.search(r"(20\d\d)", c.ticker) or re.search(r"(20\d\d)-\d\d-\d\d", c.context or "")
    return int(m.group(1)) if m else None


def _date(month: str, day: str, year: int | None) -> date | None:
    mo = MONTHS.get(month.lower()) or MON3.get(month.lower()[:3])
    try:
        return date(year, mo, int(day)) if mo and year else None
    except ValueError:
        return None


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _month_window(month: str, year: int | None) -> tuple[date, date] | None:
    mo = MONTHS.get(month.lower())
    if not mo or not year:
        return None
    nxt = date(year + (mo == 12), 1 if mo == 12 else mo + 1, 1)
    return date(year, mo, 1), date.fromordinal(nxt.toordinal() - 1)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---- crypto (Polymarket, Binance 1-minute candles) -----------------------------

@dataclass
class Crypto:
    symbol: str  # e.g. "SOL/USDT"
    kind: str  # "close" | "high" | "low"
    # close: the noon-ET close lies in [lo, hi] with the given inclusivity, on `start`
    lo: float = -INF
    lo_in: bool = False
    hi: float = INF
    hi_in: bool = False
    strike: float = 0.0  # high >= strike / low <= strike
    start: date | None = None  # earliest day the window can cover
    end: date | None = None
    # Latest day from which the window surely covers whole days: equals `start`
    # for fixed windows; for "from the creation of this market" windows it's
    # the day after creation (creation can be mid-day).
    start_full: date | None = None
    opened: str | None = None  # exact creation time when the window starts at creation

    def __post_init__(self) -> None:
        if self.start_full is None:
            self.start_full = self.start


def parse_crypto(c: Contract) -> Crypto | None:
    if c.venue != "polymarket":
        return None
    rules = c.context or ""
    sym = re.search(r"\b([A-Z]{2,6})/USDT\b", rules)
    if not sym or "Binance" not in rules:
        return None
    q, year = c.title, _year(c)
    symbol = sym.group(1) + "/USDT"
    day = r"(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})"

    if re.search(r'"Close" price|final "Close"|Close" price|"Close"', rules) and "12:00 in the ET timezone (noon)" in rules:
        m = re.search(rf"be between \$([\d,.]+) and \$([\d,.]+) on {day}", q)
        if m:  # ties go to the higher bracket: [L, H)
            d = _date(m.group(3), m.group(4), year)
            return Crypto(symbol, "close", _num(m.group(1)), True, _num(m.group(2)), False, start=d, end=d) if d else None
        m = re.search(rf"be above \$([\d,.]+) on {day}", q)
        if m and "higher than the price specified" in rules:  # strictly above
            d = _date(m.group(2), m.group(3), year)
            return Crypto(symbol, "close", _num(m.group(1)), False, start=d, end=d) if d else None
        m = re.search(rf"be greater than \$([\d,.]+) on {day}", q)
        if m:  # top bracket, ties go up: [X, inf)
            d = _date(m.group(2), m.group(3), year)
            return Crypto(symbol, "close", _num(m.group(1)), True, start=d, end=d) if d else None
        m = re.search(rf"be less than \$([\d,.]+) on {day}", q)
        if m:  # bottom bracket: (-inf, X)
            d = _date(m.group(2), m.group(3), year)
            return Crypto(symbol, "close", hi=_num(m.group(1)), hi_in=False, start=d, end=d) if d else None
        return None

    m = re.search(r"\b(reach|hit|dip to) \$([\d,.]+) ", q + " ")
    if not m:
        return None
    kind = "low" if m.group(1) == "dip to" else "high"
    if kind == "high" and "High" not in rules or kind == "low" and "Low" not in rules:
        return None
    strike = _num(m.group(2))
    if "creation of this market" in rules:
        # "... from the creation of this market until 11:59 PM ET on the date
        # specified in the title": starts whenever the market was created.
        w = re.search(rf"\bby {day}, (\d{{4}})\?", q)
        opened = _created_days(c.opened_at)
        if not w or not opened or "until 11:59 PM ET on the date specified" not in rules:
            return None
        e = _date(w.group(1), w.group(2), int(w.group(3)))
        return Crypto(symbol, kind, strike=strike, start=opened[0], end=e,
                      start_full=date.fromordinal(opened[1].toordinal() + 1), opened=c.opened_at) if e else None
    w = re.search(rf"\bon {day}\?", q)
    if w and "on the date specified" in rules:
        d = _date(w.group(1), w.group(2), year)
        return Crypto(symbol, kind, strike=strike, start=d, end=d) if d else None
    w = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})-(\d{1,2})\?", q)
    if w and "date range specified" in rules:
        s, e = _date(w.group(1), w.group(2), year), _date(w.group(1), w.group(3), year)
        return Crypto(symbol, kind, strike=strike, start=s, end=e) if s and e else None
    w = re.search(r"\bin (January|February|March|April|May|June|July|August|September|October|November|December)\?", q)
    if w and "month specified" in rules:
        mw = _month_window(w.group(1), year)
        return Crypto(symbol, kind, strike=strike, start=mw[0], end=mw[1]) if mw else None
    return None


def _created_days(opened_at: str | None) -> tuple[date, date] | None:
    """Earliest and latest ET calendar day of a UTC creation time (checked
    at both EDT and EST, so no time-zone database is needed)."""
    if not opened_at:
        return None
    try:
        t = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    days = sorted({(t - timedelta(hours=h)).date() for h in (4, 5)})
    return days[0], days[-1]


def _subset(a: Crypto, b: Crypto) -> bool:
    """Close interval a within close interval b."""
    lo_ok = a.lo > b.lo or (a.lo == b.lo and (b.lo_in or not a.lo_in))
    hi_ok = a.hi < b.hi or (a.hi == b.hi and (b.hi_in or not a.hi_in))
    return lo_ok and hi_ok


def _disjoint(a: Crypto, b: Crypto) -> bool:
    def below(x: Crypto, y: Crypto) -> bool:  # x entirely below y
        return x.hi < y.lo or (x.hi == y.lo and not (x.hi_in and y.lo_in))
    return below(a, b) or below(b, a)


def _covers_line(a: Crypto, b: Crypto) -> bool:
    """The two close intervals together cover every price (-> exhaustive)."""
    lo, hi = (a, b) if (a.lo, not a.lo_in) <= (b.lo, not b.lo_in) else (b, a)
    if lo.lo != -INF or max(a.hi, b.hi) != INF:
        return False
    return lo.hi > hi.lo or (lo.hi == hi.lo and (lo.hi_in or hi.lo_in))


def _close_within(c: Crypto, strike: float, kind: str) -> bool:
    """Every close in c's interval forces the path event (high >= strike, or low <= strike)."""
    if kind == "high":  # every close >= strike (a close just above it counts too)
        return c.lo >= strike
    return c.hi <= strike


def _complement_within(c: Crypto, strike: float, kind: str) -> bool:
    """Every close OUTSIDE c's interval forces the path event (-> exhaustive)."""
    if kind == "high":  # outside c = closes from h up: all >= strike when h >= strike
        return c.lo == -INF and c.hi >= strike
    return c.hi == INF and c.lo <= strike


def _crypto(a: Crypto, b: Crypto) -> list[str]:
    if a.symbol != b.symbol:
        return []
    rels: set[str] = set()
    if a.kind == "close" and b.kind == "close":
        if a.start != b.start:
            return []
        if _subset(a, b):
            rels.add("a_implies_b")
        if _subset(b, a):
            rels.add("b_implies_a")
        if _disjoint(a, b):
            rels.add("mutually_exclusive")
        if _covers_line(a, b):
            rels.add("exhaustive")
        return sorted(rels)
    if a.kind == "close" or b.kind == "close":
        close, path, close_is_a = (a, b, True) if a.kind == "close" else (b, a, False)
        if not (path.start_full <= close.start <= path.end):
            return []
        if _close_within(close, path.strike, path.kind):
            rels.add("a_implies_b" if close_is_a else "b_implies_a")
        if _complement_within(close, path.strike, path.kind):
            rels.add("exhaustive")
        return sorted(rels)
    if a.kind != b.kind:
        return []
    def implies(x: Crypto, y: Crypto) -> bool:
        if x.opened and y.opened:  # both start at creation: compare the exact moments
            within = y.opened.replace("Z", "+00:00") <= x.opened.replace("Z", "+00:00") and x.end <= y.end
        else:
            within = y.start_full <= x.start and x.end <= y.end
        return within and (x.strike >= y.strike if x.kind == "high" else x.strike <= y.strike)
    if implies(a, b):
        rels.add("a_implies_b")
    if implies(b, a):
        rels.add("b_implies_a")
    return sorted(rels)


# ---- economic releases ------------------------------------------------------------

@dataclass
class Measure:
    """A condition on one published number: its value lies in [lo, hi]
    (with inclusivity), for `metric` in `period`. Reuses the interval logic."""
    metric: str
    period: str
    lo: float = -INF
    lo_in: bool = False
    hi: float = INF
    hi_in: bool = False


# Kalshi series -> (metric, "above" strictly | "exactly"). Same venue, same
# BLS release, so a delayed release delays both (unlike Polymarket, whose
# BLS markets fall back to prior data -- those stay Jev's call).
_KALSHI_ECON = {
    "KXCPI": ("cpi_mom", "above"), "KXECONSTATCPI": ("cpi_mom", "exactly"),
    "KXCPICORE": ("core_mom", "above"), "KXECONSTATCPICORE": ("core_mom", "exactly"),
    "KXCPIYOY": ("cpi_yoy", "above"), "KXECONSTATCPIYOY": ("cpi_yoy", "exactly"),
    "KXCPICOREYOY": ("core_yoy", "above"), "KXECONSTATCORECPIYOY": ("core_yoy", "exactly"),
    "KXU3": ("u3", "above"), "KXECONSTATU3": ("u3", "exactly"),
}
_MONTH_YEAR = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* (20\d\d)"


def _period(text: str) -> str | None:
    m = re.search(_MONTH_YEAR, text)
    if not m:
        return None
    mo = MONTHS.get(m.group(1).lower()) or MON3.get(m.group(1).lower()[:3])
    return f"{m.group(2)}-{mo:02d}" if mo else None


def parse_measure(c: Contract) -> Measure | None:
    rules = c.context or ""
    if c.venue == "kalshi":
        series = c.ticker.split("-")[0]
        if series in _KALSHI_ECON:
            metric, how = _KALSHI_ECON[series]
            m = re.search(r"-(\d{2})([A-Z]{3})-T(-?\d+(?:\.\d+)?)$", c.ticker)
            if not m or m.group(2).lower() not in MON3 or "Bureau of Labor Statistics" not in rules and how == "above":
                return None
            period = f"20{m.group(1)}-{MON3[m.group(2).lower()]:02d}"
            x = float(m.group(3))
            return Measure(metric, period, x, False) if how == "above" else Measure(metric, period, x, True, x, True)
        if series in ("KXISMPMI", "KXUSISMSERV"):
            metric = "ism_mfg" if series == "KXISMPMI" else "ism_serv"
            period = _period(c.title) or _period(rules)
            m = re.search(r"-T?(\d+(?:\.\d+)?)$", c.ticker)
            if not period or not m:
                return None
            if "is at least" in rules:
                return Measure(metric, period, float(m.group(1)), True)
            if re.search(r"\bis above\b", rules):
                return Measure(metric, period, float(m.group(1)), False)
        return None
    if c.venue == "polymarket" and "ismworld.org" in rules:
        metric = "ism_mfg" if "Manufacturing" in c.title else "ism_serv" if "Services" in c.title else None
        period = _period(rules)
        if not metric or not period or "bracket containing" not in rules:
            return None
        m = re.search(r"between (\d+(?:\.\d+)?) and (\d+(?:\.\d+)?)", c.title)
        if m:  # one-decimal brackets, both ends included
            return Measure(metric, period, float(m.group(1)), True, float(m.group(2)), True)
    return None


def _measure(a: Measure, b: Measure) -> list[str] | None:
    if a.metric != b.metric or a.period != b.period:
        return None
    rels: set[str] = set()
    if _subset(a, b):
        rels.add("a_implies_b")
    if _subset(b, a):
        rels.add("b_implies_a")
    if _disjoint(a, b):
        rels.add("mutually_exclusive")
    if _covers_line(a, b):
        rels.add("exhaustive")
    return sorted(rels)


# ---- central-bank decisions -------------------------------------------------------
#
# Each market becomes a range of the policy-rate change x (bps, cuts negative)
# at one bank's meeting, and the interval logic above decides the relation.
# Kalshi buckets are inclusive ("Cut 1-25bps" = [-25, 0)); the Fed series is
# exact multiples of 25. Polymarket rounds a change UP to the nearest 25, so
# "decreases by 50 bps" is a cut in (25, 50] and "50 bps or more" a cut > 25.

_CB_KALSHI = {"INDIA": "india", "EU": "ecb", "ENGLAND": "england", "JAPAN": "japan", "KOREA": "korea",
              "MEXICO": "mexico", "NZ": "newzealand", "RUSSIA": "russia", "CANADA": "canada",
              "BRAZIL": "brazil", "AUSTRALIA": "australia"}
_CB_POLY = [("Reserve Bank of India", "india"), ("European Central Bank", "ecb"), ("ECB", "ecb"),
            ("Bank of England", "england"), ("Bank of Japan", "japan"), ("Bank of Korea", "korea"),
            ("Bank of Mexico", "mexico"), ("Reserve Bank of New Zealand", "newzealand"),
            ("Bank of Russia", "russia"), ("Bank of Canada", "canada"), ("Bank of Brazil", "brazil"),
            ("Reserve Bank of Australia", "australia"), ("Federal Reserve", "fed"), ("Fed ", "fed")]


def parse_cb(c: Contract) -> Measure | None:
    rules = c.context or ""
    if c.venue == "kalshi":
        m = re.match(r"^KX(?:CBDECISION([A-Z]+)|(FED)DECISION)-(\d{2})([A-Z]{3})\d*-([A-Z0-9]+)$", c.ticker)
        if not m or m.group(4).lower() not in MON3:
            return None
        bank = "fed" if m.group(2) else _CB_KALSHI.get(m.group(1))
        if not bank or "meeting" not in rules.lower() and bank != "fed":
            return None
        metric = f"cb|{bank}|20{m.group(3)}-{MON3[m.group(4).lower()]:02d}"
        code, fed = m.group(5), bank == "fed"
        ranges = {"HOLD": (0, True, 0, True), "H0": (0, True, 0, True),
                  "C25": (-25, True, -25, True) if fed else (-25, True, 0, False),
                  "H25": (25, True, 25, True) if fed else (0, False, 25, True),
                  "C25P": (-INF, False, -25, False), "C26": (-INF, False, -25, False),
                  "H25P": (25, False, INF, False), "H26": (25, False, INF, False)}
        if code not in ranges:
            return None
        return Measure(metric, "", *ranges[code])
    if c.venue != "polymarket":
        return None
    q = c.title
    bank = next((key for name, key in _CB_POLY if name in q), None)
    mo = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b", q)
    year = re.search(rf"{mo.group(1)} (20\d\d)", rules) if mo else None
    if not bank or not mo or not year:
        return None
    metric = f"cb|{bank}|{year.group(1)}-{MONTHS[mo.group(1).lower()]:02d}"
    if re.search(r"\bno change\b", q, re.I):
        return Measure(metric, "", 0, True, 0, True)
    m = re.search(r"\b(decrease|decreases|cut|increase|increases|hike)\b.*?\bby (\d+)(\+)?\s?(?:bps|basis points)( or more)?", q, re.I)
    if m:
        if "rounded up to the nearest 25" not in rules:
            return None  # the brackets mean something else (e.g. Bank of Canada rounds to the NEAREST 25)
        n, more, cut = int(m.group(2)), bool(m.group(3) or m.group(4)), m.group(1).lower() in ("decrease", "decreases", "cut")
        if n % 25 or n <= 0:
            return None
        if cut:
            return Measure(metric, "", -INF, False, -(n - 25), False) if more else Measure(metric, "", -n, True, -(n - 25), False)
        return Measure(metric, "", n - 25, False, INF, False) if more else Measure(metric, "", n - 25, False, n, True)
    m = re.search(r"\b(decrease|increase)\b the [\w\s]+? rate\b(?! by)", q, re.I)
    if m:  # any cut / any hike
        return Measure(metric, "", -INF, False, 0, False) if m.group(1).lower() == "decrease" else Measure(metric, "", 0, False, INF, False)
    return None


# ---- Treasury par yields (Kalshi) ---------------------------------------------------

@dataclass
class Yield:
    tenor: str  # "10", "30", ...
    kind: str  # "above" | "below"
    strike: float
    start: date
    end: date


def parse_yield(c: Contract) -> Yield | None:
    if c.venue != "kalshi" or "Treasury" not in c.title:
        return None
    rules = c.context or ""
    m = re.match(r"^KX(?:UST(\d+)A[DM]?|(\d+)YRDIR[HL]M)-", c.ticker)
    s = re.search(r"-T(\d+(?:\.\d+)?)$", c.ticker)
    if not m or not s:
        return None
    tenor = m.group(1) or m.group(2)
    if f"{tenor}Y U.S. Treasury" not in rules:
        return None
    w = re.search(r"is (above|below) [\d.]+% on any business day between (\w{3}) (\d{1,2}), (\d{4}) and (\w{3}) (\d{1,2}), (\d{4})", rules)
    if w and "Daily Treasury Par Yield Curve Rate" in rules:
        start = _date(w.group(2), w.group(3), int(w.group(4)))
        end = _date(w.group(5), w.group(6), int(w.group(7)))
        return Yield(tenor, w.group(1), float(s.group(1)), start, end) if start and end else None
    d = re.search(r"par yield for the \d+Y U.S. Treasury is (above|below) [\d.]+% on (\w{3}) (\d{1,2}), (\d{4})", rules)
    if d:
        day = _date(d.group(2), d.group(3), int(d.group(4)))
        return Yield(tenor, d.group(1), float(s.group(1)), day, day) if day else None
    return None


def _yield(a: Yield, b: Yield) -> list[str] | None:
    if a.tenor != b.tenor:
        return []  # different tenors: separate curves
    rels: set[str] = set()

    def implies(x: Yield, y: Yield) -> bool:  # x YES => y YES
        if x.kind != y.kind or not (y.start <= x.start and x.end <= y.end):
            return False
        return x.strike >= y.strike if x.kind == "above" else x.strike <= y.strike
    if implies(a, b):
        rels.add("a_implies_b")
    if implies(b, a):
        rels.add("b_implies_a")
    # "above Y on day D" NO means the day's yield <= Y; if Y < X that day is
    # "below X", so a window containing D that asks "below X" must be YES.
    for x, y in ((a, b), (b, a)):
        if x.kind == "above" and y.kind == "below" and x.start == x.end and y.start <= x.start <= y.end and x.strike < y.strike:
            rels.add("exhaustive")
    return sorted(rels)


# ---- YouTube views (Kalshi) ------------------------------------------------------

@dataclass
class YTViews:
    artist: str  # event-ticker artist code, e.g. "YOU"
    name: str
    strike: float
    start: date
    end: date


def _strike_suffix(ticker: str) -> float | None:
    m = re.search(r"-(\d+(?:\.\d+)?)([KM])$", ticker)
    return float(m.group(1)) * (1e6 if m.group(2) == "M" else 1e3) if m else None


def parse_ytviews(c: Contract) -> YTViews | None:
    if c.venue != "kalshi" or not c.ticker.startswith(("KXYTVIEWSD-", "KXYTVIEWSW-", "KXYTVIEWSHIGH-")):
        return None
    rules = c.context or ""
    if "Global daily views on YouTube" not in rules:
        return None
    strike = _strike_suffix(c.ticker)
    code = re.match(r"^[A-Z]+-([A-Z]+)\d", c.ticker)
    name = re.search(r"^Will (.+?) have above", c.title)
    if strike is None or not code or not name:
        return None
    m = re.search(r"on YouTube on (\w{3}) (\d{1,2}), (\d{4})", rules)
    if m:
        d = _date(m.group(1), m.group(2), int(m.group(3)))
        return YTViews(code.group(1), name.group(1), strike, d, d) if d else None
    m = re.search(r"at any point during (\w+) (\d{1,2}), (\d{4}) - (\w+) (\d{1,2}), (\d{4})", rules)
    if m:
        s = _date(m.group(1), m.group(2), int(m.group(3)))
        e = _date(m.group(4), m.group(5), int(m.group(6)))
        return YTViews(code.group(1), name.group(1), strike, s, e) if s and e else None
    m = re.search(r"at any point during (\w+) (\d{4})", rules)
    if m:
        mw = _month_window(m.group(1), int(m.group(2)))
        return YTViews(code.group(1), name.group(1), strike, *mw) if mw else None
    return None


def _same_artist(x: YTViews, y: YTViews) -> bool:
    """Same ticker code AND a shared name word ("NBA YoungBoy" / "YoungBoy Never Broke Again")."""
    words = lambda n: {w for w in re.findall(r"[a-z0-9]+", n.lower()) if len(w) > 2}
    return x.artist == y.artist and bool(words(x.name) & words(y.name))


def _ytviews(a: YTViews, b: YTViews) -> list[str] | None:
    if not _same_artist(a, b):
        return None  # different artist codes: not this family's call
    rels = []
    # "above X on day D" / "above X at any point in W": a narrower window at a
    # strike at least as high implies the wider one.
    if b.start <= a.start and a.end <= b.end and a.strike >= b.strike:
        rels.append("a_implies_b")
    if a.start <= b.start and b.end <= a.end and b.strike >= a.strike:
        rels.append("b_implies_a")
    return sorted(rels)


# ---- album sales (Kalshi, Luminate) -----------------------------------------------

def _album(a: Contract, b: Contract) -> Verdict | None:
    def parse(c: Contract):
        m = re.match(r"^(KXPUREALBUMS|KXALBUMEQUIV)-([A-Z0-9]+)-", c.ticker)
        if c.venue != "kalshi" or not m or "Luminate" not in (c.context or ""):
            return None
        s = _strike_suffix(c.ticker)
        return (m.group(1), m.group(2), s) if s is not None else None
    pa, pb = parse(a), parse(b)
    if not pa or not pb or pa[1] != pb[1]:
        return None  # different album or tracking week: not this family's call
    rels = []
    # pure > X  =>  equivalent > X (pure is a component); same metric: higher strike implies lower.
    def implies(x, y):
        return (x[0] == "KXPUREALBUMS" or x[0] == y[0]) and x[2] >= y[2] and not (x[0] == "KXALBUMEQUIV" and y[0] == "KXPUREALBUMS")
    if implies(pa, pb):
        rels.append("a_implies_b")
    if implies(pb, pa):
        rels.append("b_implies_a")
    return Verdict(sorted(rels), "album", "pure album sales are part of album-equivalent units")


# ---- rank charts --------------------------------------------------------------------

@dataclass
class Rank:
    subject: str  # normalized title
    pos: int | None  # exact rank (#1, #2), or None for "top N"
    top: int | None  # top-N list membership
    chart: str | None  # normalized chart + period, when parsed with certainty (same venue only)


def parse_rank(c: Contract) -> Rank | None:
    rules = c.context or ""
    if c.venue == "kalshi":
        subject = c.title.split(" -- ", 1)[1] if " -- " in c.title else None
        m = (re.search(r" (?:is|ranks) (#\d+|Top \d+) on (?:the )?(.+?)\s+(?:chart published on|chart for the Week of|chart dated) (\w+ \d{1,2}, \d{4})", rules)
             or re.search(r" (?:is|ranks) (#\d+|Top \d+) on (.+?) chart dated (\w+ \d{1,2}, \d{4})", rules))
        if not subject or not m:
            return None
        rank, chart, when = m.group(1), m.group(2), m.group(3)
    elif c.venue == "polymarket":
        q = re.search(r'^Will "?(.+?)"? be the (#\d+|top|number one) (.+?)(?: this week)?\??$', c.title, re.I)
        if not q:
            return None
        subject, rank, chart = q.group(1), q.group(2), q.group(3)
        d = re.search(r"-(\d{8})$", c.ticker) or re.search(r"(20\d\d-\d\d-\d\d)", rules)
        if not d:
            return None
        when = d.group(1)
    else:
        return None
    r = rank.lower()
    if r in ("top", "number one") or r == "#1":
        pos, top = 1, None
    elif r.startswith("#"):
        pos, top = int(r[1:]), None
    else:
        pos, top = None, int(r.split()[1])
    return Rank(_norm(subject), pos, top, f"{c.venue}|{_norm(chart)}|{_norm(when)}")


def _same_subject(x: Rank, y: Rank) -> bool:
    return x.subject == y.subject or (min(len(x.subject), len(y.subject)) >= 6
                                      and (x.subject in y.subject or y.subject in x.subject))


def _rank(a: Rank, b: Rank) -> Verdict | None:
    same_chart = a.chart == b.chart
    if _same_subject(a, b):
        if not same_chart:
            return None  # e.g. US vs global, or across venues: Jev's call (restrict still applies)
        if a.pos and b.pos:
            if a.pos == b.pos:
                return Verdict(["a_implies_b", "b_implies_a"], "rank", "same title, same rank, same chart")
            return Verdict(["mutually_exclusive"], "rank", "one title can't hold two ranks on one chart")
        if a.pos and b.top:
            return Verdict(["a_implies_b"] if a.pos <= b.top else [], "rank", "#N implies top M when N <= M")
        if b.pos and a.top:
            return Verdict(["b_implies_a"] if b.pos <= a.top else [], "rank", "#N implies top M when N <= M")
        return None
    # Different titles: only "two titles can't share one exact rank" can hold.
    if a.pos and b.pos and a.pos == b.pos and same_chart:
        return Verdict(["mutually_exclusive"], "rank", "two titles can't both be #N on one chart")
    if same_chart:
        return Verdict([], "rank", "different titles at different ranks can both happen")
    return None


def covers_all(contracts: list[Contract]) -> bool:
    """True when the markets are noon-close brackets for one coin and date
    that together cover every price with no gap -- so YES on all of them is
    certain to pay exactly one $1 (see relations.partition_arb)."""
    parsed = [parse_crypto(c) for c in contracts]
    if len(parsed) < 2 or any(p is None or p.kind != "close" for p in parsed):
        return False
    if len({(p.symbol, p.start) for p in parsed}) != 1:
        return False
    parsed.sort(key=lambda p: (p.lo, not p.lo_in))
    if parsed[0].lo != -INF or max(p.hi for p in parsed) != INF:
        return False
    reach, reach_in = parsed[0].hi, parsed[0].hi_in
    for p in parsed[1:]:
        if p.lo > reach or (p.lo == reach and not (reach_in or p.lo_in)):
            return False  # a gap: some price is in no bracket
        if (p.hi, p.hi_in) > (reach, reach_in):
            reach, reach_in = p.hi, p.hi_in
    return True


# ---- entry points ---------------------------------------------------------------

def judge(a: Contract, b: Contract) -> Verdict | None:
    """An authoritative verdict for a pair in a family the rules can read, else None."""
    ca, cb = parse_crypto(a), parse_crypto(b)
    if ca and cb:
        return Verdict(_crypto(ca, cb), "crypto", "Binance 1-minute candle logic")
    ya, yb = parse_ytviews(a), parse_ytviews(b)
    if ya and yb:
        rels = _ytviews(ya, yb)
        if rels is not None:
            return Verdict(rels, "ytviews", "narrower window, strike at least as high")
    v = _album(a, b)
    if v:
        return v
    ma, mb = parse_measure(a), parse_measure(b)
    if ma and mb:
        rels = _measure(ma, mb)
        if rels is not None:
            return Verdict(rels, "econ", "same published number: interval logic on its value")
    ca, cb = parse_cb(a), parse_cb(b)
    if ca and cb:
        rels = _measure(ca, cb)
        if rels is not None:
            return Verdict(rels, "centralbank", "policy-rate change ranges at the same meeting")
    ua, ub = parse_yield(a), parse_yield(b)
    if ua and ub:
        return Verdict(_yield(ua, ub), "treasury", "daily par yield inside the window, same tenor")
    ra, rb = parse_rank(a), parse_rank(b)
    if ra and rb:
        return _rank(ra, rb)
    return None


def restrict(a: Contract, b: Contract, rels: list[str]) -> tuple[list[str], str | None]:
    """Drop relations the rules know can't hold, from a verdict judge() didn't
    cover (e.g. Jev's, across venues). Different titles on rank charts can
    only ever be "not both" at the same exact rank; nothing else between them."""
    ra, rb = parse_rank(a), parse_rank(b)
    if ra and rb and not _same_subject(ra, rb):
        allowed = {"mutually_exclusive"} if ra.pos and rb.pos and ra.pos == rb.pos else set()
        kept = [r for r in rels if r in allowed]
        if kept != rels:
            return kept, "different titles on rank charts"
    return rels, None


def family_key(c: Contract) -> str | None:
    """Groups markets the rules can pair with each other, for candidate
    generation beyond shared title words (whose per-event cap misses most
    strike-ladder pairs)."""
    cr = parse_crypto(c)
    if cr:
        return f"crypto|{cr.symbol}"
    yt = parse_ytviews(c)
    if yt:
        return f"yt|{yt.artist}"
    m = re.match(r"^(?:KXPUREALBUMS|KXALBUMEQUIV)-([A-Z0-9]+)-", c.ticker)
    if m and c.venue == "kalshi":
        return f"album|{m.group(1)}"
    me = parse_measure(c)
    if me:
        return f"econ|{me.metric}|{me.period}"
    y = parse_yield(c)
    if y:
        return f"ust|{y.tenor}"
    cbm = parse_cb(c)
    if cbm:
        return cbm.metric
    rk = parse_rank(c)
    if rk and rk.chart:
        return f"rank|{rk.subject}"
    return None


def family_pairs(contracts: list[Contract], max_per_family: int = 400,
                 max_total: int = 20000) -> list[tuple[Contract, Contract]]:
    """Every same-family pair judge() gives a relation to, lower ticker first."""
    groups: dict[str, list[Contract]] = {}
    for c in contracts:
        k = family_key(c)
        if k:
            groups.setdefault(k, []).append(c)
    out: list[tuple[Contract, Contract]] = []
    for members in groups.values():
        n = 0
        for x, y in itertools.combinations(sorted(members, key=lambda c: c.ticker), 2):
            v = judge(x, y)
            if v and v.relations:
                out.append((x, y))
                n += 1
                if n >= max_per_family:
                    break
        if len(out) >= max_total:
            break
    return out
