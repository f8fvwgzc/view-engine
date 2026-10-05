"""Local M1 store (deep intraday history) + importers.

Store:    quant/.cache/m1/<SYMBOL>/<YYYY>.npz   arrays t (int64 epoch seconds UTC, candle start), o h l c
          (float64), v (float32); sorted, de-duplicated.  meta.json per symbol records the source of each year.
Importer A  HistData.com free M1 "ASCII" zips (yearly for past years, monthly for the current year).
            Documented as "EST without daylight saving"; the files we got are New York wall time, so the
            zone is detected per file (`detect_histdata_tz`). Polite client: one request at a time, >= 3 s apart, honest User-Agent, every zip cached.
            A CAPTCHA / block / unexpected page stops the source — we never try to get around it.
Importer B  drop folder quant/data/import/ : MetaTrader 5 history CSV, old MetaTrader CSV, HistData files.
Fallback    the existing Dukascopy day files (throttled by the feed; resumable).
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import pandas as pd

log = logging.getLogger("quant.m1store")

ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = ROOT / ".cache" / "m1"
ZIP_DIR = ROOT / ".cache" / "histdata_zips"
IMPORT_DIR = ROOT / "data" / "import"
UA = "view-engine-quant/0.1 (personal research; python-httpx)"
HD_PAGE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/{path}"
HD_POST = "https://www.histdata.com/get.php"
HD_SPACING_S = 3.0
HD_MAX_BYTES = 60 * 1024 * 1024
COLS = ["o", "h", "l", "c", "v"]


class SourceBlocked(Exception):
    """The source answered with a CAPTCHA / block / unexpected page: stop using it."""


# ------------------------------------------------------------------ store

def _sym_dir(symbol: str) -> Path:
    return STORE_DIR / symbol.upper()


def _read_meta(symbol: str) -> dict:
    p = _sym_dir(symbol) / "meta.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"symbol": symbol.upper(), "years": {}}


def _write_meta(symbol: str, meta: dict) -> None:
    d = _sym_dir(symbol)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(d / "meta.json")


def clean_m1(df: pd.DataFrame) -> pd.DataFrame:
    """UTC index, sorted, de-duplicated, sane OHLC."""
    if df.empty:
        return df
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df = df.copy()
    df.index = idx.as_unit("ns")
    if "v" not in df:
        df["v"] = 0.0
    df = df[COLS].astype(float)
    df = df[np.isfinite(df["c"]) & (df["c"] > 0)]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["h"] = df[["h", "o", "c"]].max(axis=1)
    df["l"] = df[["l", "o", "c"]].min(axis=1)
    return df


def write_m1(symbol: str, df: pd.DataFrame, source: str, replace_years: bool = False) -> dict:
    """Merge candles into the store (per calendar year). Existing minutes are kept unless the new source is
    given `replace_years` (then the year's file is replaced for the minutes the new data covers)."""
    df = clean_m1(df)
    if df.empty:
        return {"rows": 0}
    d = _sym_dir(symbol)
    d.mkdir(parents=True, exist_ok=True)
    meta = _read_meta(symbol)
    added = 0
    for year, part in df.groupby(df.index.year):
        p = d / f"{year}.npz"
        old = _load_year(p)
        if old is not None and len(old):
            merged = pd.concat([old, part]) if replace_years else pd.concat([part, old])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            merged = part
        added += len(merged) - (len(old) if old is not None else 0)
        tmp = d / f"{year}.tmp.npz"
        np.savez_compressed(tmp, t=(merged.index.asi8 // 10 ** 9).astype(np.int64), o=merged["o"].to_numpy(),
                            h=merged["h"].to_numpy(), l=merged["l"].to_numpy(), c=merged["c"].to_numpy(),
                            v=merged["v"].to_numpy(np.float32))
        tmp.replace(p)
        y = meta["years"].setdefault(str(year), {"sources": []})
        if source not in y["sources"]:
            y["sources"].append(source)
        y.update(rows=int(len(merged)), first=_iso(merged.index[0]), last=_iso(merged.index[-1]))
    meta["updated"] = _iso(pd.Timestamp.now(tz="UTC"))
    _write_meta(symbol, meta)
    _mem.pop(symbol.upper(), None)
    return {"rows": int(len(df)), "new_rows": int(added)}


def _load_year(p: Path) -> Optional[pd.DataFrame]:
    if not p.exists():
        return None
    with np.load(p) as z:
        idx = pd.DatetimeIndex(pd.to_datetime(z["t"], unit="s", utc=True)).as_unit("ns")
        return pd.DataFrame({"o": z["o"], "h": z["h"], "l": z["l"], "c": z["c"], "v": z["v"].astype(float)}, index=idx)


def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


_mem: dict[str, tuple] = {}  # symbol -> (fingerprint, frame)
_mem_lock = threading.Lock()


def fingerprint(symbol: str) -> Optional[tuple]:
    d = _sym_dir(symbol)
    if not d.exists():
        return None
    files = sorted(d.glob("[0-9][0-9][0-9][0-9].npz"))
    if not files:
        return None
    return tuple((f.name, f.stat().st_size, int(f.stat().st_mtime)) for f in files)


def has(symbol: str) -> bool:
    return fingerprint(symbol) is not None


def load_m1(symbol: str) -> Optional[pd.DataFrame]:
    """All stored M1 candles of a symbol (cached in memory until the files change)."""
    symbol = symbol.upper()
    fp = fingerprint(symbol)
    if fp is None:
        return None
    with _mem_lock:
        hit = _mem.get(symbol)
        if hit and hit[0] == fp:
            return hit[1]
    frames = [f for f in (_load_year(_sym_dir(symbol) / name) for name, _, _ in fp) if f is not None and len(f)]
    if not frames:
        return None
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    with _mem_lock:
        _mem[symbol] = (fp, df)
    return df


def coverage(symbol: Optional[str] = None) -> list[dict]:
    """Per symbol: sources, first/last minute, rows, gaps longer than a weekend."""
    out = []
    syms = [symbol.upper()] if symbol else sorted(p.name for p in STORE_DIR.iterdir() if p.is_dir()) \
        if STORE_DIR.exists() else []
    for s in syms:
        df = load_m1(s)
        if df is None or df.empty:
            continue
        meta = _read_meta(s)
        ts = df.index.asi8 // 10 ** 9
        gap = np.diff(ts)
        big = np.flatnonzero(gap > 3 * 86400)  # longer than a weekend (incl. a Monday holiday)
        gaps = [{"from": _iso(df.index[i]), "to": _iso(df.index[i + 1]), "hours": round(float(gap[i]) / 3600, 1)}
                for i in big]
        out.append({"symbol": s, "sources": sorted({x for y in meta["years"].values() for x in y["sources"]}),
                    "first": _iso(df.index[0]), "last": _iso(df.index[-1]), "rows": int(len(df)),
                    "years": {k: {"rows": v.get("rows"), "sources": v.get("sources")}
                              for k, v in sorted(meta["years"].items())},
                    "gaps_longer_than_weekend": gaps, "n_gaps": len(gaps)})
    return out


# ------------------------------------------------------------------ parsers

def detect_histdata_tz(naive: pd.DatetimeIndex) -> tuple[str, str]:
    """HistData documents its ASCII files as "EST without daylight saving" (fixed UTC-5). Checked against our
    feed, the files we downloaded are in New York WALL time (UTC-4 in summer): the Friday close and the daily
    metals break sit at 16:59 all year. Decide per file from the data itself: in the summer months the last
    minute before a session break is 16:5x for New-York-time files and 15:5x for fixed-EST files."""
    if len(naive) < 100:
        return "America/New_York", "too few rows to check; assumed New York time"
    s = pd.Series(naive)
    gap = s.diff().dt.total_seconds().to_numpy()
    brk = np.flatnonzero(gap > 45 * 60)  # daily break (metals) or weekend
    before = s.iloc[brk - 1]
    summer = before[before.dt.month.isin([4, 5, 6, 7, 8, 9, 10])]
    summer = summer[(summer.dt.hour >= 15) & (summer.dt.hour <= 17)]
    if len(summer) < 3:
        return "America/New_York", "no summer session breaks in this file; EST and New York time coincide"
    h = int(summer.dt.hour.mode().iloc[0])
    if h == 16:
        return "America/New_York", f"session breaks at 16:5x in summer ({len(summer)} checked): New York wall time"
    if h == 15:
        return "UTC-5", f"session breaks at 15:5x in summer ({len(summer)} checked): fixed EST (UTC-5)"
    return "America/New_York", f"unexpected break hour {h}; assumed New York time"


def parse_histdata_csv(text: str, tz: str = "auto") -> pd.DataFrame:
    """HistData ASCII M1: `YYYYMMDD HHMMSS;open;high;low;close;volume`. `tz`: 'auto' (detect New York wall time
    vs fixed EST from the session breaks), an IANA name, or a fixed offset like 'UTC-5'. The decision is stored in
    `df.attrs['tz']` / `df.attrs['tz_reason']`."""
    df = pd.read_csv(io.StringIO(text), sep=";", header=None, names=["t", "o", "h", "l", "c", "v"],
                     dtype={"t": str})
    naive = pd.DatetimeIndex(pd.to_datetime(df["t"], format="%Y%m%d %H%M%S"))
    reason = "given"
    if tz == "auto":
        tz, reason = detect_histdata_tz(naive)
    df.index = to_utc(naive, tz)
    df = df[~df.index.isna()]
    out = clean_m1(df[COLS])
    out.attrs.update(tz=tz, tz_reason=reason)
    return out


def parse_metatrader_csv(text: str, tz: str = "UTC") -> pd.DataFrame:
    """MetaTrader history export. Two layouts:
       MT5: header `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> ...` (tab or comma separated)
       old: `YYYY.MM.DD,HH:MM,o,h,l,c,v` (no header)"""
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    sep = "\t" if "\t" in first else ("," if "," in first else ";")
    if "<DATE>" in first.upper():
        df = pd.read_csv(io.StringIO(text), sep=sep)
        df.columns = [c.strip("<> ").lower() for c in df.columns]
        vol = "tickvol" if "tickvol" in df.columns else ("vol" if "vol" in df.columns else None)
        t = df["date"].astype(str) + " " + (df["time"].astype(str) if "time" in df.columns else "00:00:00")
        out = pd.DataFrame({"o": df["open"], "h": df["high"], "l": df["low"], "c": df["close"],
                            "v": df[vol] if vol else 0.0})
    else:
        df = pd.read_csv(io.StringIO(text), sep=sep, header=None)
        t = df[0].astype(str) + " " + df[1].astype(str)
        out = pd.DataFrame({"o": df[2], "h": df[3], "l": df[4], "c": df[5], "v": df[6] if df.shape[1] > 6 else 0.0})
    idx = pd.to_datetime(t.str.replace(".", "-", regex=False))
    out.index = to_utc(pd.DatetimeIndex(idx), tz)
    return clean_m1(out)


def to_utc(idx: pd.DatetimeIndex, tz: str) -> pd.DatetimeIndex:
    """`tz` = IANA name (e.g. Europe/Athens), 'UTC', or a fixed offset like 'UTC+2', '+03:00', 'EST'."""
    tz = (tz or "UTC").strip()
    if tz.upper() == "EST":
        return (idx + pd.Timedelta(hours=5)).tz_localize("UTC")
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", tz, flags=re.I)
    if m:
        off = (int(m.group(2)) + int(m.group(3) or 0) / 60) * (1 if m.group(1) == "+" else -1)
        return (idx - pd.Timedelta(hours=off)).tz_localize("UTC")
    if tz.upper() in ("UTC", "GMT", "Z"):
        return idx.tz_localize("UTC")
    return idx.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward").tz_convert("UTC")


def read_zip_csv(blob: bytes, max_bytes: int = 400 * 1024 * 1024) -> str:
    """The single .csv/.txt data member of a zip, read in memory (nothing is extracted to disk)."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        data = [m for m in members if re.fullmatch(r"[A-Za-z0-9_.\-]+\.csv", m.filename)]
        other = [m for m in members if m not in data and not re.fullmatch(r"[A-Za-z0-9_.\-]+\.txt", m.filename)]
        if other:
            raise ValueError(f"unexpected zip member(s): {[m.filename for m in other][:3]}")
        if len(data) != 1:
            raise ValueError(f"expected exactly one .csv in the zip, found {len(data)}")
        if data[0].file_size > max_bytes:
            raise ValueError("csv inside the zip is too large")
        return z.read(data[0]).decode("ascii", errors="replace")


def symbol_from_filename(name: str) -> Optional[str]:
    m = re.search(r"([A-Za-z]{6})", Path(name).stem.replace("_", " ").replace("-", " "))
    if "DAT_ASCII" in name.upper():
        m = re.search(r"DAT_ASCII_([A-Z]{6})_", name.upper())
    return m.group(1).upper() if m else None


# ------------------------------------------------------------------ importer A: HistData

_hd_lock = threading.Lock()
_hd_last = [0.0]
stats = {"requests": 0, "bytes": 0, "files": 0, "cached_files": 0}


def _polite_wait() -> None:
    wait = HD_SPACING_S - (time.time() - _hd_last[0])
    if wait > 0:
        time.sleep(wait)
    _hd_last[0] = time.time()


def histdata_zip(pair: str, year: int, month: Optional[int] = None) -> Optional[bytes]:
    """Download (or read the cached) HistData zip. None = that period is not published. Raises SourceBlocked."""
    pair = pair.upper()
    name = f"HISTDATA_COM_ASCII_{pair}_M1{year}{month:02d}.zip" if month else f"HISTDATA_COM_ASCII_{pair}_M1{year}.zip"
    cache_file = ZIP_DIR / pair / name
    if cache_file.exists() and cache_file.stat().st_size > 0:
        stats["cached_files"] += 1
        return cache_file.read_bytes()
    path = f"{year}/{month}" if month else f"{year}"
    page_url = HD_PAGE.format(pair=pair.lower(), path=path)
    with _hd_lock:
        _polite_wait()
        stats["requests"] += 1
        r = httpx.get(page_url, headers={"User-Agent": UA}, timeout=40, follow_redirects=True)
        if r.status_code == 404:
            return None
        if r.status_code in (403, 429, 503) or re.search(r"captcha|cf-challenge|are you a robot", r.text, re.I):
            raise SourceBlocked(f"histdata page HTTP {r.status_code} (block / CAPTCHA)")
        if r.status_code != 200:
            raise SourceBlocked(f"histdata page HTTP {r.status_code}")
        form = re.search(r'<form[^>]*id="file_down".*?</form>', r.text, re.S | re.I)
        if not form:
            return None  # no download form: nothing published for this period
        fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form.group(0)))
        if not {"tk", "date", "datemonth", "platform", "timeframe", "fxpair"} <= set(fields):
            raise SourceBlocked("histdata download form changed (missing hidden fields)")
        if fields["fxpair"].upper() != pair:
            return None
        _polite_wait()
        stats["requests"] += 1
        with httpx.stream("POST", HD_POST, data=fields, headers={"User-Agent": UA, "Referer": page_url},
                          timeout=120, follow_redirects=True) as resp:
            if resp.status_code in (403, 429, 503):
                raise SourceBlocked(f"histdata get.php HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise SourceBlocked(f"histdata get.php HTTP {resp.status_code}")
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > HD_MAX_BYTES:
                    raise ValueError("download exceeds the 60 MB cap")
        blob = bytes(buf)
    if not blob.startswith(b"PK"):
        if re.search(rb"captcha|robot|denied|blocked", blob[:4000], re.I):
            raise SourceBlocked("histdata returned a block page instead of a zip")
        return None  # an HTML/empty answer: not available
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".part")
    tmp.write_bytes(blob)
    tmp.replace(cache_file)
    stats["bytes"] += len(blob)
    stats["files"] += 1
    return blob


def import_histdata(symbol: str, from_year: int, progress: Optional[dict] = None,
                    today: Optional[date] = None) -> dict:
    """Yearly zips for past years, monthly zips for the current year."""
    today = today or datetime.now(timezone.utc).date()
    rep = {"symbol": symbol, "source": "histdata", "periods": [], "rows": 0, "missing": []}
    periods = [(y, None) for y in range(int(from_year), today.year)] + [(today.year, m) for m in range(1, today.month + 1)]
    for year, month in periods:
        label = f"{year}-{month:02d}" if month else str(year)
        if progress is not None:
            progress["current"] = f"{symbol} {label}"
        blob = histdata_zip(symbol, year, month)
        if blob is None:
            rep["missing"].append(label)
            continue
        df = parse_histdata_csv(read_zip_csv(blob))
        w = write_m1(symbol, df, "histdata")
        rep["periods"].append({"period": label, "rows": len(df), "zip_mb": round(len(blob) / 1e6, 2),
                               "tz": df.attrs.get("tz"), "tz_reason": df.attrs.get("tz_reason")})
        rep["rows"] += w["rows"]
        if progress is not None:
            progress["rows"] = progress.get("rows", 0) + w["rows"]
    return rep


# ------------------------------------------------------------------ importer B: drop folder

def import_folder(default_tz: str = "UTC") -> list[dict]:
    """Import every .csv / .txt / .zip in quant/data/import/. Symbol from the file name; time zone from a
    `<file>.tz` note next to it (IANA name or offset), else `default_tz` (HistData files are always UTC-5)."""
    out = []
    if not IMPORT_DIR.exists():
        return out
    for f in sorted(IMPORT_DIR.iterdir()):
        if f.suffix.lower() not in (".csv", ".txt", ".zip") or f.name.endswith(".tz"):
            continue
        rep = {"file": f.name}
        try:
            sym = symbol_from_filename(f.name)
            if not sym:
                raise ValueError("cannot read a 6-letter symbol from the file name")
            text = read_zip_csv(f.read_bytes()) if f.suffix.lower() == ".zip" else f.read_text(errors="replace")
            note = f.with_name(f.name + ".tz")
            if "DAT_ASCII" in f.name.upper() or re.match(r"\d{8} \d{6};", text.lstrip()[:20]):
                df = parse_histdata_csv(text)
                tz, kind = f"{df.attrs.get('tz')} ({df.attrs.get('tz_reason')})", "histdata-file"
            else:
                tz = note.read_text().strip() if note.exists() else default_tz
                df, kind = parse_metatrader_csv(text, tz), "metatrader-file"
                if not note.exists():
                    rep["tz_warning"] = (f"no {note.name} note: timestamps were read as {default_tz}. MetaTrader "
                                         "exports are in broker server time (often UTC+2/+3) — add the note and "
                                         "re-import if candles look shifted.")
            w = write_m1(sym, df, f"import:{kind}", replace_years=True)
            rep.update(symbol=sym, kind=kind, tz=tz, rows=w["rows"], first=_iso(df.index[0]), last=_iso(df.index[-1]))
        except Exception as e:
            rep["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out.append(rep)
    return out


# ------------------------------------------------------------------ fallback: Dukascopy day files

def import_dukascopy(symbol: str, from_year: int, progress: Optional[dict] = None, max_days: int = 4000) -> dict:
    """Copy every cached Dukascopy day into the store and try to fetch missing days (the feed throttles hard:
    this stops at the first rate limit and reports how far it got; calling it again resumes)."""
    from . import dukascopy as dk
    from .symbols import normalize
    inst = dk.instrument(normalize(symbol))
    rep = {"symbol": symbol, "source": "dukascopy", "days_imported": 0, "days_fetched": 0, "stopped": None}
    if not inst:
        rep["stopped"] = "no Dukascopy instrument for this symbol"
        return rep
    today = datetime.now(timezone.utc).date()
    frames = []
    for d in dk.wanted_days(today - pd.Timedelta(days=1), min(max_days, (today - date(int(from_year), 1, 1)).days)):
        if progress is not None:
            progress["current"] = f"{symbol} {d}"
        if not dk.cached(inst, d):
            if dk.blocked():
                rep["stopped"] = f"rate-limited by the feed at {d}: {dk.status()['last_error']}"
                break
            if not dk.fetch_day(inst, d):
                rep["stopped"] = f"stopped at {d}: {dk.status()['last_error']}"
                break
            rep["days_fetched"] += 1
        try:
            frames.append(dk.decode_bi5(dk._path(inst, d).read_bytes(), d, dk.point_divisor(inst)))
            rep["days_imported"] += 1
        except Exception:
            continue
    if frames:
        w = write_m1(symbol, pd.concat(frames), "dukascopy")
        rep["rows"] = w["rows"]
    return rep


# ------------------------------------------------------------------ jobs

jobs: dict[str, dict] = {}


def start_import(symbols: list[str], from_year: int, source: str = "histdata", default_tz: str = "UTC") -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "source": source, "symbols": symbols, "from_year": from_year,
           "started": _iso(pd.Timestamp.now(tz="UTC")), "current": None, "rows": 0, "reports": [], "error": None}
    jobs[job_id] = job

    def run():
        try:
            if source == "folder":
                job["reports"] = import_folder(default_tz)
            else:
                for s in symbols:
                    try:
                        fn = import_histdata if source == "histdata" else import_dukascopy
                        job["reports"].append(fn(s.upper(), from_year, job))
                    except SourceBlocked as e:
                        job["reports"].append({"symbol": s, "error": f"source blocked: {e}"})
                        job["error"] = f"{source} blocked us at {s}: {e} — stopped, nothing was bypassed"
                        break
                    except Exception as e:
                        job["reports"].append({"symbol": s, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            job["status"] = "failed" if job["error"] else "done"
        except Exception as e:  # pragma: no cover
            job.update(status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        job["finished"] = _iso(pd.Timestamp.now(tz="UTC"))
        job["download"] = dict(stats)
    threading.Thread(target=run, name=f"m1-import-{job_id}", daemon=True).start()
    return job_id
