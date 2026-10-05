"""Scheduled high-impact events (2019 -> now + what is already scheduled), from official public pages.

Sources (fetched politely: one request at a time, >= 3 s apart, cached under quant/.cache/events/):
  US releases   FRED release calendar (St. Louis Fed): Employment Situation (payrolls), CPI, weekly jobless
                claims, Personal Income & Outlays (PCE), GDP, advance retail sales — real release dates with
                the release time, including holiday / shutdown shifts.
  FOMC          federalreserve.gov meeting calendars (statement 14:00 New York on the last meeting day);
                unscheduled meetings are kept in the table but flagged `scheduled: false` and never used as
                a feature (they were not known in advance).
  ECB           ecb.europa.eu monetary-policy decisions (13:45 Frankfurt, 14:15 since 21 July 2022) and the
                Governing Council calendar for meetings still to come.
  BoJ           boj.or.jp schedule of Monetary Policy Meetings (announcement time varies: 12:00 Tokyo assumed,
                flagged `approximate`).
  BoE           bankofengland.co.uk answered 403 to our client: no BoE rows (we do not work around it).
Fallback rule (rows flagged `approximate`): payrolls first Friday 08:30 New York, claims Thursday 08:30 New York.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import pandas as pd

log = logging.getLogger("quant.events")

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "events"
UA = "view-engine-quant/0.1 (personal research; python-httpx)"
SPACING_S = 3.0
TYPES = ("nfp", "cpi", "fomc", "claims", "central_bank", "other")
FRED_RELEASES = {50: ("nfp", "US Employment Situation (non-farm payrolls)"), 10: ("cpi", "US CPI"),
                 180: ("claims", "US weekly jobless claims"), 54: ("other", "US Personal Income and Outlays (PCE)"),
                 53: ("other", "US GDP"), 9: ("other", "US advance retail sales")}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
_lock = threading.Lock()
_last = [0.0]
status: dict = {"fetched": 0, "cached": 0, "errors": []}


def _get(name: str, url: str, params: Optional[dict] = None, ua: Optional[str] = UA, refresh: bool = False) -> Optional[str]:
    """Cached polite GET. None on any failure (recorded in `status['errors']`)."""
    p = CACHE_DIR / "raw" / f"{name}.html"
    if p.exists() and p.stat().st_size > 500 and not refresh:
        status["cached"] += 1
        return p.read_text(errors="replace")
    with _lock:
        wait = SPACING_S - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = httpx.get(url, params=params, headers={"User-Agent": ua} if ua else None, timeout=40,
                          follow_redirects=True)
        except Exception as e:
            status["errors"].append(f"{name}: {type(e).__name__}")
            return None
    if r.status_code != 200 or len(r.text) < 500:
        status["errors"].append(f"{name}: HTTP {r.status_code}")
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(r.text)
    status["fetched"] += 1
    return r.text


def _utc(d: date, hh: int, mm: int, tz: str) -> pd.Timestamp:
    return pd.Timestamp(datetime(d.year, d.month, d.day, hh, mm), tz=tz).tz_convert("UTC")


def _row(t: pd.Timestamp, currency: str, typ: str, title: str, source: str, approximate: bool = False,
         scheduled: bool = True) -> dict:
    return {"time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "currency": currency, "type": typ, "title": title,
            "source": source, "approximate": bool(approximate), "scheduled": bool(scheduled)}


# ------------------------------------------------------------------ parsers (pure)

def parse_fred_calendar(html: str, typ: str, title: str) -> list[dict]:
    """FRED release calendar (year view): '<span ...>Friday January 10, 2025</span>' then the time '7:30 am'
    (US Central)."""
    out = []
    for m in re.finditer(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})"
                         r"(.{0,600}?)(\d{1,2}):(\d{2})\s*([ap]m)", html, re.S):
        mon = MONTHS.get(m.group(1).lower())
        if not mon:
            continue
        hh = int(m.group(5)) % 12 + (12 if m.group(7) == "pm" else 0)
        t = _utc(date(int(m.group(3)), mon, int(m.group(2))), hh, int(m.group(6)), "America/Chicago")
        out.append(_row(t, "USD", typ, title, "FRED release calendar"))
    return out


def parse_alfred_dates(text: str, typ: str, title: str, year_from: int = 2019) -> list[dict]:
    """ALFRED 'release dates' download (one YYYY-MM-DD per line). These US releases come out at 08:30 New York."""
    out = []
    for y, m, d in re.findall(r"^(\d{4})-(\d{2})-(\d{2})\s*$", text, re.M):
        if int(y) >= year_from:
            out.append(_row(_utc(date(int(y), int(m), int(d)), 8, 30, "America/New_York"), "USD", typ, title,
                            "ALFRED release dates (08:30 New York release time)"))
    return out


def parse_fomc_calendar(html: str) -> list[dict]:
    """federalreserve.gov/monetarypolicy/fomccalendars.htm: panels '<year> FOMC Meetings' with month + dates."""
    out = []
    parts = re.split(r"(\d{4}) FOMC Meetings", html)
    for i in range(1, len(parts) - 1, 2):
        year = int(parts[i])
        for m in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<',
                             parts[i + 1], re.S):
            row = _fomc_row(year, m.group(1), m.group(2))
            if row:
                out.append(row)
    return out


def parse_fomc_historical(html: str) -> list[dict]:
    """federalreserve.gov/monetarypolicy/fomchistorical<year>.htm: '<h5>January 29-30 Meeting - 2019</h5>'."""
    out = []
    for m in re.finditer(r"<h5[^>]*>\s*([A-Za-z/]+)\s+([\d\-–]+)\s*([^<]*?)-\s*(\d{4})\s*</h5>", html):
        row = _fomc_row(int(m.group(4)), m.group(1), m.group(2) + " " + m.group(3))
        if row:
            out.append(row)
    return out


def _fomc_row(year: int, month_txt: str, date_txt: str) -> Optional[dict]:
    low = date_txt.lower()
    if "notation" in low:
        return None
    months = [MONTHS.get(x.strip().lower()[:3] and next((k for k in MONTHS if k.startswith(x.strip().lower()[:3])), ""))
              for x in re.split(r"[/\-]", month_txt)]
    months = [x for x in months if x]
    days = [int(x) for x in re.findall(r"\d{1,2}", date_txt.split("(")[0])]
    if not months or not days:
        return None
    mon, day = months[-1], days[-1]  # decision = last day (a meeting can span two months: 'April/May 30-1')
    try:
        d = date(year, mon, day)
    except ValueError:
        return None
    sched = "unscheduled" not in low and "conference call" not in low
    return _row(_utc(d, 14, 0, "America/New_York"), "USD", "fomc", "FOMC statement", "federalreserve.gov",
                scheduled=sched)


def parse_ecb_decisions(html: str) -> list[dict]:
    """ECB monetary policy decisions list snippet: press-release links 'ecb.mpYYMMDD~...'."""
    out, seen = [], set()
    for yy, mm, dd in re.findall(r"ecb\.mp(\d{2})(\d{2})(\d{2})", html):
        d = date(2000 + int(yy), int(mm), int(dd))
        if d in seen:
            continue
        seen.add(d)
        out.append(_ecb_row(d))
    return out


def parse_ecb_calendar(html: str) -> list[dict]:
    """Governing Council calendar: 'DD/MM/YYYY ... monetary policy meeting ... (Day 2), followed by press conference'."""
    out = []
    for dd, mm, yy, txt in re.findall(r"<dt>\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*</dt>\s*<dd>\s*([^<]+)", html):
        t = txt.lower()
        if "monetary policy meeting" in t and "non-monetary" not in t and "press conference" in t:
            out.append(_ecb_row(date(int(yy), int(mm), int(dd))))
    return out


def _ecb_row(d: date) -> dict:
    hh, mm = (14, 15) if d >= date(2022, 7, 21) else (13, 45)
    return _row(_utc(d, hh, mm, "Europe/Berlin"), "EUR", "central_bank", "ECB rate decision", "ecb.europa.eu")


def parse_boj_schedule(html: str) -> list[dict]:
    """BoJ MPM schedule tables: caption 'Table : 2026', first column 'Jan. 22 (Thurs.), 23 (Fri.)'."""
    out = []
    for year, body in re.findall(r"<caption[^>]*>\s*Table\s*:\s*(\d{4})\s*</caption>(.*?)</table>", html, re.S):
        for row in re.findall(r"<tr>\s*<td[^>]*>(.*?)</td>", body, re.S):
            txt = re.sub(r"<[^>]+>", " ", row)
            m = re.match(r"\s*([A-Z][a-z]{2,8})\.?\s+(\d{1,2})(?:\s*\([^)]*\))?(?:\s*,\s*(?:([A-Z][a-z]{2,8})\.?\s+)?(\d{1,2}))?", txt)
            if not m:
                continue
            mon_txt = (m.group(3) or m.group(1)).lower()[:3]
            mon = next((v for k, v in MONTHS.items() if k.startswith(mon_txt)), None)
            day = int(m.group(4) or m.group(2))
            if not mon:
                continue
            try:
                d = date(int(year), mon, day)
            except ValueError:
                continue
            out.append(_row(_utc(d, 12, 0, "Asia/Tokyo"), "JPY", "central_bank", "BoJ policy decision",
                            "boj.or.jp (time of day assumed 12:00 Tokyo)", approximate=True))
    return out


def parse_boj_statements(html: str, unscheduled: frozenset = frozenset()) -> list[dict]:
    """BoJ 'Statements on Monetary Policy' year page: decision-day statements are linked as kYYMMDDa.pdf."""
    out, seen = [], set()
    for yy, mm, dd in re.findall(r"/k(\d{2})(\d{2})(\d{2})a\.(?:pdf|htm)", html):
        try:
            d = date(2000 + int(yy), int(mm), int(dd))
        except ValueError:
            continue
        if d in seen:
            continue
        seen.add(d)
        out.append(_row(_utc(d, 12, 0, "Asia/Tokyo"), "JPY", "central_bank", "BoJ policy decision",
                        "boj.or.jp (time of day assumed 12:00 Tokyo)", approximate=True,
                        scheduled=d.isoformat() not in unscheduled))
    return out


def rule_events(year_from: int, year_to: int, kinds=("nfp", "claims")) -> list[dict]:
    """Fallback when the official dates cannot be fetched (flagged approximate)."""
    from pandas.tseries.holiday import USFederalHolidayCalendar
    hol = set(USFederalHolidayCalendar().holidays(f"{year_from}-01-01", f"{year_to}-12-31").date)
    out = []
    d = date(year_from, 1, 1)
    while d <= date(year_to, 12, 31):
        if "nfp" in kinds and d.weekday() == 4 and d.day <= 7:
            dd = d - timedelta(days=1) if d in hol else d
            out.append(_row(_utc(dd, 8, 30, "America/New_York"), "USD", "nfp", "US non-farm payrolls (rule: first "
                            "Friday 08:30 New York)", "rule", approximate=True))
        if "claims" in kinds and d.weekday() == 3:
            dd = d - timedelta(days=1) if d in hol else d
            out.append(_row(_utc(dd, 8, 30, "America/New_York"), "USD", "claims", "US weekly jobless claims (rule: "
                            "Thursday 08:30 New York)", "rule", approximate=True))
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ build + table

def build_table(year_from: int = 2019, refresh: bool = False) -> dict:
    """Fetch (or read cached) official pages and write quant/.cache/events/events.json."""
    this_year = datetime.now(timezone.utc).year
    rows: list[dict] = []
    report: dict = {"sources": {}, "blocked": []}
    # US releases from the FRED release calendar
    for rid, (typ, title) in FRED_RELEASES.items():
        # past release dates: ALFRED download; dates still to come this year: FRED release calendar
        txt = _get(f"alfred_{rid}", "https://alfred.stlouisfed.org/release/downloaddates", {"rid": rid, "ff": "txt"},
                   ua=None, refresh=refresh)
        part = parse_alfred_dates(txt, typ, title, year_from) if txt else []
        html = _get(f"fred_{rid}_{this_year}", "https://fred.stlouisfed.org/releases/calendar",
                    {"rid": rid, "vs": f"{this_year}-01-01", "ve": f"{this_year}-12-31", "view": "year"}, ua=None,
                    refresh=refresh)
        have_days = {r["time_utc"][:10] for r in part}
        part += [r for r in (parse_fred_calendar(html, typ, title) if html else [])
                 if r["time_utc"][:10] not in have_days and r["time_utc"][:4] == str(this_year)]
        rows += part
        got = len(part)
        report["sources"][title] = {"rows": got, "source": "ALFRED release dates + FRED release calendar" if got
                                    else "unavailable"}
        if got == 0 and typ in ("nfp", "claims"):
            rows += rule_events(year_from, this_year, (typ,))
            report["sources"][title] = {"rows": "rule", "source": "rule (approximate)"}
    # FOMC
    fomc = []
    html = _get("fomc_calendars", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", refresh=refresh)
    if html:
        fomc += parse_fomc_calendar(html)
    have = {r["time_utc"][:4] for r in fomc}
    for y in range(year_from, this_year + 1):
        if str(y) not in have:
            h = _get(f"fomc_hist{y}", f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{y}.htm")
            if h:
                fomc += parse_fomc_historical(h)
    rows += fomc
    report["sources"]["FOMC statement"] = {"rows": len(fomc), "source": "federalreserve.gov"}
    # ECB
    ecb = []
    for y in range(year_from, this_year + 1):
        h = _get(f"ecb_mopo_{y}", f"https://www.ecb.europa.eu/press/govcdec/mopo/{y}/html/index_include.en.html",
                 refresh=refresh and y == this_year)
        if h:
            ecb += parse_ecb_decisions(h)
    h = _get("ecb_cal", "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html", refresh=refresh)
    if h:
        ecb += parse_ecb_calendar(h)
    rows += ecb
    report["sources"]["ECB rate decision"] = {"rows": len(ecb), "source": "ecb.europa.eu"}
    # BoJ (current page + archive pages per year)
    boj = []
    h = _get("boj_sched", "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm", refresh=refresh)
    if h:
        boj += parse_boj_schedule(h)
    h = _get("boj_past", "https://www.boj.or.jp/en/mopo/mpmsche_minu/m_ref/index.htm")
    called = frozenset(f"20{a}-{b}-{c}" for a, b, c in re.findall(r"m_ref/k(\d{2})(\d{2})(\d{2})a\.pdf", h or ""))
    have_boj = {r["time_utc"][:4] for r in boj}
    for y in range(year_from, this_year + 1):
        if str(y) in have_boj:
            continue
        hy = _get(f"boj_mpr_{y}", f"https://www.boj.or.jp/en/mopo/mpmdeci/mpr_{y}/index.htm")
        if hy:
            boj += parse_boj_statements(hy, called)
    rows += boj
    report["sources"]["BoJ policy decision"] = {"rows": len(boj), "source": "boj.or.jp"}
    report["blocked"].append("bankofengland.co.uk (HTTP 403 to our client): no BoE rate-decision rows")
    report["blocked"].append("bls.gov schedule pages (HTTP 403): US release dates taken from the FRED release "
                             "calendar instead")
    # de-duplicate and store
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda x: (x["time_utc"], x["type"])):
        k = (r["time_utc"], r["currency"], r["type"], r["title"])
        if k not in seen and r["time_utc"] >= f"{year_from}-01-01":
            seen.add(k)
            uniq.append(r)
    table = {"built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "year_from": year_from,
             "report": report, "fetch": dict(status), "events": uniq}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / "events.json.tmp"
    tmp.write_text(json.dumps(table))
    tmp.replace(CACHE_DIR / "events.json")
    _mem.clear()
    return table


_mem: dict = {}


def load_table() -> dict:
    p = CACHE_DIR / "events.json"
    if not p.exists():
        return {"events": [], "report": {"sources": {}, "blocked": ["event table not built yet"]}, "built": None}
    key = (str(p), p.stat().st_mtime_ns)
    if _mem.get("key") != key:
        _mem.update(key=key, table=json.loads(p.read_text()))
    return _mem["table"]


def history(currency: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None,
            scheduled_only: bool = False) -> list[dict]:
    cur = {c.strip().upper() for c in currency.split(",")} if currency else None
    out = []
    for e in load_table()["events"]:
        if cur and e["currency"] not in cur:
            continue
        if start and e["time_utc"] < start:
            continue
        if end and e["time_utc"] > end:
            continue
        if scheduled_only and not e["scheduled"]:
            continue
        out.append(e)
    return out


def coverage() -> dict:
    """Rows per type: official vs approximate, first/last."""
    out: dict = {}
    for e in load_table()["events"]:
        k = f"{e['currency']} {e['title'].split(' (')[0]}"
        c = out.setdefault(k, {"type": e["type"], "rows": 0, "official": 0, "approximate": 0, "unscheduled": 0,
                               "first": e["time_utc"], "last": e["time_utc"], "source": e["source"]})
        c["rows"] += 1
        c["approximate" if e["approximate"] else "official"] += 1
        c["unscheduled"] += 0 if e["scheduled"] else 1
        c["last"] = max(c["last"], e["time_utc"])
    return out


# ------------------------------------------------------------------ features

NEWS_CAP_MIN = 1440.0
NEWS_FEATURES = (["news_mins_to_next", "news_mins_since_last", "news_today", "news_within_60m"]
                 + [f"news_next_{t}" for t in TYPES] + [f"news_last_{t}" for t in TYPES])


def news_block(times: pd.DatetimeIndex, currencies: tuple[str, ...]) -> np.ndarray:
    """Per row (row time = candle close): timing of the scheduled high-impact events of the pair's currencies.
    The schedule is known in advance, so 'minutes to the next event' is legitimate at that time."""
    n = len(times)
    out = np.full((n, len(NEWS_FEATURES)), np.nan)
    evs = [e for e in load_table()["events"] if e["scheduled"] and e["currency"] in currencies]
    if not evs or n == 0:
        return out
    et = pd.DatetimeIndex(pd.to_datetime([e["time_utc"] for e in evs], utc=True)).as_unit("ns")
    order = np.argsort(et.asi8)
    e_ns = et.asi8[order]
    e_type = np.array([TYPES.index(evs[i]["type"]) for i in order])
    t_ns = pd.DatetimeIndex(times).as_unit("ns").asi8
    idx = np.searchsorted(e_ns, t_ns, side="right")  # events at or before the row time are "last"
    has_next, has_last = idx < len(e_ns), idx > 0
    nxt = np.where(has_next, e_ns[np.minimum(idx, len(e_ns) - 1)], 0)
    lst = np.where(has_last, e_ns[np.maximum(idx - 1, 0)], 0)
    to_next = np.where(has_next, (nxt - t_ns) / 6e10, np.inf)
    since = np.where(has_last, (t_ns - lst) / 6e10, np.inf)
    out[:, 0] = np.minimum(to_next, NEWS_CAP_MIN)
    out[:, 1] = np.minimum(since, NEWS_CAP_MIN)
    ny = pd.DatetimeIndex(times).as_unit("ns").tz_convert("America/New_York")
    day = ny.normalize().asi8
    def nyday(ns):
        return pd.DatetimeIndex(pd.to_datetime(ns, utc=True)).tz_convert("America/New_York").normalize().asi8
    out[:, 2] = (has_next & (nyday(nxt) == day)) | (has_last & (nyday(lst) == day))
    out[:, 3] = (to_next <= 60) | (since <= 60)
    out[:, 4:] = 0.0
    tn = e_type[np.minimum(idx, len(e_ns) - 1)]
    tl = e_type[np.maximum(idx - 1, 0)]
    near_next, near_last = has_next & (to_next <= NEWS_CAP_MIN), has_last & (since <= NEWS_CAP_MIN)
    rows = np.arange(n)
    out[rows[near_next], 4 + tn[near_next]] = 1.0
    out[rows[near_last], 4 + len(TYPES) + tl[near_last]] = 1.0
    return out
