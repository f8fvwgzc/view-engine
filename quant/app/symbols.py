"""Symbol registry + normalization of free-form user input to canonical ids."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional


class SymbolError(ValueError):
    """Raised when user input cannot be resolved to a symbol (HTTP 400)."""


@dataclass(frozen=True)
class OptionsProxy:
    ticker: str
    relation: str  # "direct" | "inverse"
    # how to map proxy price levels onto the underlying:
    #   "ratio"      level_u = level_p * (U/P)          (direct)
    #   "reciprocal" level_u = (U*P) / level_p          (inverse FX ETFs)
    #   "duration"   yield_u = y - (level_p/P - 1)/D*100 (bond ETF -> yield, inverse)
    model: str = "ratio"
    duration: Optional[float] = None


@dataclass(frozen=True)
class Symbol:
    id: str
    name: str
    asset_class: str  # fx | metal | index | rate | etf | equity | commodity
    yahoo: Optional[str]
    stooq: Optional[str] = None
    fred: Optional[str] = None
    currencies: tuple[str, ...] = ()
    options_proxy: Optional[OptionsProxy] = None
    related: tuple[str, ...] = ()
    known: bool = True  # False for pass-through tickers not in the registry
    spot_code: Optional[str] = None  # "XAU"/"XAG": serve futures candles basis-adjusted to live spot
    prefer_fred: bool = False  # use FRED before Yahoo for daily+ bars
    intraday_ok: bool = True  # False when no reliable free intraday source exists
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["currencies"] = list(self.currencies)
        d["related"] = list(self.related)
        return d

    @property
    def options_ticker(self) -> Optional[str]:
        """Ticker whose listed options we read (proxy or the symbol itself)."""
        if self.options_proxy:
            return self.options_proxy.ticker
        if self.asset_class in ("etf", "equity"):
            return self.yahoo
        return None


def _fx(pair: str, name: str, related: tuple[str, ...], proxy: Optional[OptionsProxy] = None) -> Symbol:
    return Symbol(
        id=pair, name=name, asset_class="fx", yahoo=f"{pair}=X", stooq=pair.lower(),
        currencies=(pair[:3], pair[3:]), options_proxy=proxy, related=related,
    )


_REG: list[Symbol] = [
    # ---- FX majors / crosses
    _fx("USDJPY", "US Dollar / Japanese Yen", ("DXY", "US10Y", "NIKKEI", "XAUUSD", "SPX", "EURJPY"),
        OptionsProxy("FXY", "inverse", "reciprocal")),
    _fx("EURUSD", "Euro / US Dollar", ("DXY", "GBPUSD", "US10Y", "XAUUSD", "SPX", "USDCHF"),
        OptionsProxy("FXE", "direct")),
    _fx("GBPUSD", "British Pound / US Dollar", ("DXY", "EURUSD", "EURGBP", "US10Y", "SPX"),
        OptionsProxy("FXB", "direct")),
    _fx("AUDUSD", "Australian Dollar / US Dollar", ("DXY", "NZDUSD", "XAUUSD", "SPX", "AUDJPY"),
        OptionsProxy("FXA", "direct")),
    _fx("NZDUSD", "New Zealand Dollar / US Dollar", ("DXY", "AUDUSD", "SPX", "AUDJPY")),
    _fx("USDCAD", "US Dollar / Canadian Dollar", ("DXY", "WTI", "US10Y", "SPX", "AUDUSD"),
        OptionsProxy("FXC", "inverse", "reciprocal")),
    _fx("USDCHF", "US Dollar / Swiss Franc", ("DXY", "EURUSD", "XAUUSD", "US10Y", "USDJPY"),
        OptionsProxy("FXF", "inverse", "reciprocal")),
    _fx("EURJPY", "Euro / Japanese Yen", ("USDJPY", "EURUSD", "NIKKEI", "SPX")),
    _fx("GBPJPY", "British Pound / Japanese Yen", ("USDJPY", "GBPUSD", "NIKKEI", "SPX")),
    _fx("AUDJPY", "Australian Dollar / Japanese Yen", ("USDJPY", "AUDUSD", "NIKKEI", "SPX", "VIX")),
    _fx("CHFJPY", "Swiss Franc / Japanese Yen", ("USDJPY", "USDCHF", "NIKKEI")),
    _fx("CADJPY", "Canadian Dollar / Japanese Yen", ("USDJPY", "USDCAD", "WTI", "NIKKEI")),
    _fx("EURGBP", "Euro / British Pound", ("EURUSD", "GBPUSD", "DXY")),
    # ---- metals
    Symbol("XAUUSD", "Gold spot (GC=F futures candles shifted by live futures-spot basis)", "metal", "GC=F",
           "xauusd", currencies=("USD",), options_proxy=OptionsProxy("GLD", "direct"),
           related=("DXY", "US10Y", "XAGUSD", "USDJPY", "SPX", "VIX"), spot_code="XAU",
           notes="Candles: COMEX GC=F minus current basis (GC=F last - live spot from gold-api.com / Swissquote)."),
    Symbol("XAGUSD", "Silver spot (SI=F futures candles shifted by live futures-spot basis)", "metal", "SI=F",
           "xagusd", currencies=("USD",), options_proxy=OptionsProxy("SLV", "direct"),
           related=("XAUUSD", "DXY", "US10Y", "SPX"), spot_code="XAG"),
    # ---- dollar index / rates
    Symbol("DXY", "US Dollar Index (ICE)", "index", "DX-Y.NYB", None, currencies=("USD",),
           options_proxy=OptionsProxy("UUP", "direct"),
           related=("EURUSD", "USDJPY", "US10Y", "XAUUSD", "SPX")),
    Symbol("US2Y", "US 2-Year Treasury Yield (%)", "rate", "2YY=F", "2yusy.b", fred="DGS2",
           currencies=("USD",), related=("US10Y", "DXY", "USDJPY", "SPX"), prefer_fred=True, intraday_ok=False,
           notes="Daily: FRED DGS2 (official constant-maturity yield, ~1 business day lag). "
                 "Fallback: Yahoo 2YY=F (CBOT 2Y yield futures, sparse/noisy). No reliable free intraday 2Y."),
    Symbol("US10Y", "US 10-Year Treasury Yield (%)", "rate", "^TNX", "10yusy.b", fred="DGS10",
           currencies=("USD",), options_proxy=OptionsProxy("TLT", "inverse", "duration", duration=16.5),
           related=("US2Y", "DXY", "USDJPY", "XAUUSD", "SPX"),
           notes="Yahoo ^TNX (CBOE 10Y yield index, in %) primary; FRED DGS10 daily fallback."),
    # ---- equity indices / vol / energy
    Symbol("SPX", "S&P 500 Index", "index", "^GSPC", "^spx", currencies=("USD",),
           options_proxy=OptionsProxy("SPY", "direct"), related=("NDX", "VIX", "US10Y", "DXY", "USDJPY")),
    Symbol("NDX", "Nasdaq 100 Index", "index", "^NDX", "^ndx", currencies=("USD",),
           options_proxy=OptionsProxy("QQQ", "direct"), related=("SPX", "VIX", "US10Y", "DXY")),
    Symbol("DJI", "Dow Jones Industrial Average", "index", "^DJI", "^dji", currencies=("USD",),
           options_proxy=OptionsProxy("DIA", "direct"), related=("SPX", "NDX", "US10Y")),
    Symbol("NIKKEI", "Nikkei 225", "index", "^N225", "^nkx", currencies=("JPY",),
           related=("USDJPY", "SPX", "NDX", "VIX")),
    Symbol("VIX", "CBOE Volatility Index", "index", "^VIX", None, currencies=("USD",),
           related=("SPX", "NDX", "USDJPY", "XAUUSD")),
    Symbol("WTI", "WTI Crude Oil (NYMEX front-month CL=F)", "commodity", "CL=F", "cl.f", currencies=("USD",),
           options_proxy=OptionsProxy("USO", "direct"), related=("USDCAD", "DXY", "SPX", "XAUUSD")),
    # ---- common ETFs (optionable directly)
    Symbol("SPY", "SPDR S&P 500 ETF", "etf", "SPY", "spy.us", currencies=("USD",), related=("SPX", "QQQ", "VIX", "US10Y")),
    Symbol("QQQ", "Invesco QQQ (Nasdaq 100) ETF", "etf", "QQQ", "qqq.us", currencies=("USD",), related=("NDX", "SPY", "VIX")),
    Symbol("GLD", "SPDR Gold Shares", "etf", "GLD", "gld.us", currencies=("USD",), related=("XAUUSD", "DXY", "US10Y")),
    Symbol("SLV", "iShares Silver Trust", "etf", "SLV", "slv.us", currencies=("USD",), related=("XAGUSD", "XAUUSD", "DXY")),
    Symbol("TLT", "iShares 20+Y Treasury Bond ETF", "etf", "TLT", "tlt.us", currencies=("USD",), related=("US10Y", "US2Y", "SPX")),
    Symbol("FXY", "Invesco CurrencyShares Japanese Yen", "etf", "FXY", "fxy.us", currencies=("JPY", "USD"), related=("USDJPY", "DXY")),
    Symbol("FXE", "Invesco CurrencyShares Euro", "etf", "FXE", "fxe.us", currencies=("EUR", "USD"), related=("EURUSD", "DXY")),
    Symbol("UUP", "Invesco DB US Dollar Index Bullish", "etf", "UUP", "uup.us", currencies=("USD",), related=("DXY", "EURUSD")),
    Symbol("USO", "United States Oil Fund", "etf", "USO", "uso.us", currencies=("USD",), related=("WTI", "USDCAD")),
]

REGISTRY: dict[str, Symbol] = {s.id: s for s in _REG}

_ALIASES: dict[str, str] = {
    # metals
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "GC": "XAUUSD", "GC=F": "XAUUSD", "SPOTGOLD": "XAUUSD", "XAUUSD=X": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD", "SI=F": "XAGUSD",
    # dollar index
    "DOLLARINDEX": "DXY", "USDX": "DXY", "DX": "DXY", "DXYNYB": "DXY", "DX=F": "DXY", "USDOLLARINDEX": "DXY",
    "DOLLAR": "DXY",
    # rates
    "US10": "US10Y", "UST10Y": "US10Y", "10Y": "US10Y", "TNX": "US10Y", "^TNX": "US10Y", "10YEAR": "US10Y",
    "TNOTE10Y": "US10Y", "US10YEAR": "US10Y", "10YR": "US10Y", "US10YR": "US10Y", "DGS10": "US10Y",
    "US2": "US2Y", "UST2Y": "US2Y", "2Y": "US2Y", "2YEAR": "US2Y", "US2YEAR": "US2Y", "2YR": "US2Y", "US2YR": "US2Y",
    "2YY=F": "US2Y", "DGS2": "US2Y",
    # indices
    "S&P500": "SPX", "S&P": "SPX", "SP500": "SPX", "SANDP500": "SPX", "GSPC": "SPX", "^GSPC": "SPX", "US500": "SPX",
    "ES": "SPX", "ES=F": "SPX",
    "NASDAQ": "NDX", "NASDAQ100": "NDX", "NAS100": "NDX", "US100": "NDX", "^NDX": "NDX", "NQ": "NDX", "NQ=F": "NDX",
    "DOW": "DJI", "DOWJONES": "DJI", "US30": "DJI", "^DJI": "DJI", "DJIA": "DJI",
    "NIKKEI225": "NIKKEI", "N225": "NIKKEI", "^N225": "NIKKEI", "JP225": "NIKKEI", "NKY": "NIKKEI", "JPN225": "NIKKEI",
    "^VIX": "VIX", "VOLATILITY": "VIX",
    "OIL": "WTI", "CRUDE": "WTI", "CRUDEOIL": "WTI", "USOIL": "WTI", "CL": "WTI", "CL=F": "WTI", "WTICRUDE": "WTI",
}
# Yahoo FX tickers like "USDJPY=X" and loose spellings are handled by compact normalization.
for _s in _REG:
    if _s.yahoo and _s.asset_class == "fx":
        _ALIASES[_s.yahoo] = _s.id

_PASSTHROUGH_RE = re.compile(r"^[\^A-Z0-9][A-Z0-9.\-=^]{0,14}$")


def _compact(raw: str) -> str:
    return re.sub(r"[\s/_\-.]", "", raw.strip().upper())


def normalize(raw: Optional[str]) -> Symbol:
    """Resolve free-form input ("usd/jpy", "Gold", "us 10y", "AAPL") to a Symbol."""
    if raw is None or not str(raw).strip():
        raise SymbolError("symbol is required")
    raw = str(raw).strip()
    if len(raw) > 40:
        raise SymbolError("symbol too long")
    key = _compact(raw)
    if key in REGISTRY:
        return REGISTRY[key]
    if key in _ALIASES:
        return REGISTRY[_ALIASES[key]]
    up = raw.upper().replace(" ", "")
    if up in _ALIASES:
        return REGISTRY[_ALIASES[up]]
    # e.g. "USDJPY=X" after compaction stays "USDJPY=X"; strip "=X"
    if key.endswith("=X") and key[:-2] in REGISTRY:
        return REGISTRY[key[:-2]]
    # Unknown 6-letter FX pair (e.g. "EURAUD"): build on the fly.
    if re.fullmatch(r"[A-Z]{6}", key) and key[:3] in _CCYS and key[3:] in _CCYS:
        return Symbol(id=key, name=f"{key[:3]}/{key[3:]}", asset_class="fx", yahoo=f"{key}=X",
                      stooq=key.lower(), currencies=(key[:3], key[3:]),
                      related=tuple(x for x in ("DXY", "SPX", "US10Y") if x != key), known=False)
    # Pass-through stock / ETF ticker (validated later when data is fetched).
    tick = up.replace("/", "-")
    if "." in tick and re.fullmatch(r"[A-Z]{1,5}\.[A-Z]", tick):  # BRK.B -> BRK-B (Yahoo style)
        tick = tick.replace(".", "-")
    if _PASSTHROUGH_RE.match(tick):
        return Symbol(id=tick, name=tick, asset_class="equity", yahoo=tick,
                      stooq=f"{tick.lower()}.us" if re.fullmatch(r"[A-Z\-]{1,6}", tick) else None,
                      currencies=("USD",), related=("SPX", "NDX", "VIX", "US10Y"), known=False)
    raise SymbolError(f"cannot interpret symbol '{raw}'")


_CCYS = {"USD", "EUR", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF", "CNH", "CNY", "SEK", "NOK", "DKK", "SGD",
         "HKD", "MXN", "ZAR", "TRY", "PLN", "HUF", "CZK", "INR", "KRW", "BRL"}


def list_symbols() -> list[dict]:
    return [s.to_dict() for s in _REG]
