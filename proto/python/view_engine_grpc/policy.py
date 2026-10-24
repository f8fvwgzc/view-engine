"""Freshness of sidecar answers, declared by the sidecar that knows its data.

The API (Rust) caches service-to-service answers in memory and Redis by the `Cache-Control` sent here, so the
rules sit next to the routes they describe instead of in the caller. Only 200 answers carry a policy; errors are
never cached. A route without a rule gets `no-store`, so a new route is uncached until someone decides otherwise.

Replay answers (`as_of` well in the past, or a candle window that ended long ago) are computed only from closed
candles and do not change, so they are marked immutable for a day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

IMMUTABLE = "max-age=86400, immutable"
NO_STORE = "no-store"


@dataclass(frozen=True)
class Rule:
    max_age: int = 0
    # Serve a stale copy this long past max-age while one background call refreshes it.
    stale: int = 0
    # An `as_of` older than an hour (or an `end` older than a day) makes the answer immutable.
    replay: bool = True

    def header(self, params: Mapping[str, str]) -> str:
        if self.max_age <= 0:
            return NO_STORE
        if self.replay and (_older_than(params.get("as_of"), timedelta(hours=1))
                            or _older_than(params.get("end"), timedelta(days=1))):
            return IMMUTABLE
        return f"max-age={self.max_age}, stale-while-revalidate={self.stale}" if self.stale else f"max-age={self.max_age}"


def _older_than(value: Optional[str], age: timedelta) -> bool:
    if not value:
        return False
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment < datetime.now(timezone.utc) - age


def header_for(rules: Mapping[str, Rule], path: str, params: Mapping[str, str]) -> str:
    """Exact path first, then the longest rule ending in "/" that prefixes it ("/v1/train/" covers job ids)."""
    rule = rules.get(path)
    if rule is None:
        prefixes = [key for key in rules if key.endswith("/") and path.startswith(key)]
        rule = rules[max(prefixes, key=len)] if prefixes else None
    return rule.header(params) if rule else NO_STORE


def install_cache_control(app, rules: Mapping[str, Rule]) -> None:
    """Adds `Cache-Control` to every 200 answer of `app` that does not set one itself."""

    @app.middleware("http")
    async def _cache_control(request, call_next):
        response = await call_next(request)
        if response.status_code == 200 and "cache-control" not in response.headers:
            response.headers["cache-control"] = header_for(rules, request.url.path, request.query_params)
        return response
