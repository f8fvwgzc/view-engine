import pytest

from app.symbols import SymbolError, normalize


@pytest.mark.parametrize("raw,expected", [
    ("usdjpy", "USDJPY"), ("USD/JPY", "USDJPY"), ("usd jpy", "USDJPY"), ("USDJPY=X", "USDJPY"),
    ("usd-jpy", "USDJPY"), ("eur/usd", "EURUSD"), ("gold", "XAUUSD"), ("XAU/USD", "XAUUSD"),
    ("Dollar Index", "DXY"), ("dxy", "DXY"), ("DX-Y.NYB", "DXY"), ("US 10Y", "US10Y"), ("^TNX", "US10Y"),
    ("us2y", "US2Y"), ("S&P 500", "SPX"), ("s&p500", "SPX"), ("nasdaq", "NDX"), ("nikkei", "NIKKEI"),
    ("vix", "VIX"), ("oil", "WTI"), ("silver", "XAGUSD"), ("spy", "SPY"),
])
def test_aliases(raw, expected):
    assert normalize(raw).id == expected


def test_passthrough_equity():
    s = normalize("aapl")
    assert s.id == "AAPL" and s.asset_class == "equity" and s.yahoo == "AAPL" and not s.known
    assert s.options_ticker == "AAPL"
    assert normalize("brk.b").yahoo == "BRK-B"


def test_dynamic_fx_pair():
    s = normalize("eur/aud")
    assert s.id == "EURAUD" and s.yahoo == "EURAUD=X" and s.currencies == ("EUR", "AUD")


def test_metadata():
    u = normalize("USDJPY")
    assert u.options_proxy.ticker == "FXY" and u.options_proxy.relation == "inverse"
    assert "DXY" in u.related and u.currencies == ("USD", "JPY")
    g = normalize("gold")
    assert g.yahoo == "GC=F" and g.options_ticker == "GLD"
    assert normalize("SPY").currencies == ("USD",)


@pytest.mark.parametrize("bad", ["", "   ", "!!!", "x" * 50, "this is not a ticker"])
def test_bad(bad):
    with pytest.raises(SymbolError):
        normalize(bad)
