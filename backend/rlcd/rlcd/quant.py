"""Client of the quant sidecar (features and labelled datasets). Base URL: env RLCD_QUANT_URL."""
from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

DEFAULT_URL = "http://127.0.0.1:8090"

# Default training universe: gold plus the FX majors and the main JPY crosses, limited to what /symbols offers.
PREFERRED = ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
             "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY")
MAJORS = PREFERRED[:8]   # gold plus the seven USD majors: the default universe of /v1/scan
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class QuantUnavailable(Exception):
    """The sidecar cannot be reached or failed internally (HTTP 503 to our caller)."""


class QuantRejected(Exception):
    """The sidecar answered 4xx: the request itself is wrong (bad symbol, no data). HTTP 422 to our caller."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def base_url() -> str:
    return (os.environ.get("RLCD_QUANT_URL") or DEFAULT_URL).rstrip("/")


def check_override(url: str) -> str:
    """A per-request sidecar URL must be a local one unless RLCD_ALLOW_REMOTE_QUANT=1: the service fetches
    from it, so an arbitrary URL would turn /v1/train into a request forwarder."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"quant_url must be an http(s) URL, got {url!r}")
    if u.hostname not in LOOPBACK and os.environ.get("RLCD_ALLOW_REMOTE_QUANT") != "1":
        raise ValueError(f"quant_url host {u.hostname!r} is not local; set RLCD_ALLOW_REMOTE_QUANT=1 to allow it")
    return url.rstrip("/")


class QuantClient:
    def __init__(self, url: Optional[str] = None, transport: Optional[httpx.BaseTransport] = None):
        self.url = (url or base_url()).rstrip("/")
        self._transport = transport
        self._http = httpx.Client(base_url=self.url, transport=transport)

    def with_url(self, url: str) -> "QuantClient":
        return QuantClient(url, self._transport)

    def _check(self, r: httpx.Response, path: str) -> None:
        if r.status_code < 400:
            return
        r.read()
        if r.status_code >= 500:
            raise QuantUnavailable(f"quant sidecar {path} failed with {r.status_code}: {r.text[:300]}")
        try:
            msg = r.json().get("error") or r.text
        except Exception:
            msg = r.text
        if r.status_code == 404 and "Not Found" in str(msg):
            raise QuantUnavailable(f"quant sidecar at {self.url} has no {path} endpoint yet")
        raise QuantRejected(f"quant {path}: {str(msg)[:400]}", r.status_code)

    def _get(self, path: str, params: dict, timeout: float) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            r = self._http.get(path, params=params, timeout=timeout)
        except httpx.HTTPError as e:
            raise QuantUnavailable(f"quant sidecar unreachable at {self.url}{path}: {type(e).__name__}: "
                                   f"{str(e)[:200]}") from e
        self._check(r, path)
        try:
            return r.json()
        except ValueError as e:
            raise QuantUnavailable(f"quant sidecar {path} returned non-JSON") from e

    def reachable(self) -> bool:
        try:
            self._get("/health", {}, 2.0)
            return True
        except (QuantUnavailable, QuantRejected):
            return False

    def symbols(self) -> list[dict]:
        return list(self._get("/symbols", {}, 10.0).get("symbols", []))

    def default_symbols(self) -> list[str]:
        offered = {str(s.get("id")) for s in self.symbols()}
        return [s for s in PREFERRED if s in offered]

    def features(self, symbol: str, interval: str, *, as_of: Optional[str] = None, pip: Optional[float] = None,
                 sl_pips: Optional[float] = None, tp_pips: Optional[float] = None,
                 tp2_pips: Optional[float] = None, horizon: Optional[int] = None) -> dict:
        return self._get("/features", {"symbol": symbol, "interval": interval, "as_of": as_of, "pip": pip,
                                       "sl_pips": sl_pips, "tp_pips": tp_pips, "tp2_pips": tp2_pips,
                                       "horizon": horizon}, 60.0)

    def dataset(self, symbol: str, interval: str, *, sl_pips: float, tp_pips: float, tp2_pips: float,
                horizon: int, max_rows: Optional[int] = None, pip: Optional[float] = None,
                start: Optional[str] = None, end: Optional[str] = None) -> dict:
        """JSON export: small pulls and tests (the sidecar caps it at max_rows)."""
        return self._get("/dataset", {"symbol": symbol, "interval": interval, "sl_pips": sl_pips,
                                      "tp_pips": tp_pips, "tp2_pips": tp2_pips, "horizon": horizon,
                                      "max_rows": max_rows, "pip": pip, "start": start, "end": end}, 900.0)

    def dataset_npz(self, dest: Path, symbol: str, interval: str, *, sl_pips: float, tp_pips: float,
                    tp2_pips: float, horizon: int, pip: Optional[float] = None, start: Optional[str] = None,
                    end: Optional[str] = None, attempts: int = 3) -> Optional[Path]:
        """Bulk export (`format=npz`, no row cap), streamed to `dest`. Returns None when the sidecar answered
        with JSON instead, i.e. it does not know the binary format: fall back to `dataset`.

        The download is verified (zip CRCs) before it is used and fetched again if it is damaged: a file that
        is being rebuilt on the sidecar while it is served arrives with the right size and wrong bytes."""
        params = {"symbol": symbol, "interval": interval, "sl_pips": sl_pips, "tp_pips": tp_pips,
                  "tp2_pips": tp2_pips, "horizon": horizon, "pip": pip, "start": start, "end": end,
                  "format": "npz"}
        params = {k: v for k, v in params.items() if v is not None}
        tmp = dest.with_suffix(dest.suffix + ".part")
        problem = ""
        for attempt in range(attempts):
            if attempt:
                time.sleep(2.0 * attempt)
            try:
                with self._http.stream("GET", "/dataset", params=params, timeout=1800.0) as r:
                    if r.status_code >= 500 and attempt + 1 < attempts:
                        r.read()   # two identical exports built at once can collide on the sidecar: ask again
                        problem = f"HTTP {r.status_code}"
                        continue
                    self._check(r, "/dataset")
                    if "json" in r.headers.get("content-type", ""):
                        return None
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
            except httpx.HTTPError as e:
                tmp.unlink(missing_ok=True)
                raise QuantUnavailable(f"quant sidecar unreachable at {self.url}/dataset: {type(e).__name__}: "
                                       f"{str(e)[:200]}") from e
            try:
                with zipfile.ZipFile(tmp) as z:
                    bad = z.testzip()
                problem = f"member {bad} fails its checksum" if bad else ""
            except Exception as e:   # zlib.error, BadZipFile: the bytes are not a readable archive
                problem = f"{type(e).__name__}: {e}"
            if not problem:
                tmp.replace(dest)
                return dest
            tmp.unlink(missing_ok=True)
        raise QuantUnavailable(f"quant sidecar /dataset export of {symbol} {interval} arrived damaged "
                               f"{attempts} times ({problem})")
