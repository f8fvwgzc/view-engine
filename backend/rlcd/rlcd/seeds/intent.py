"""Deterministic seed set for the `intent` and `trade_style` heads.

These rows are generated from templates, not collected from users. They exist so the heads work on day one;
real corrections posted to /v1/feedback are added on top (up-weighted) at every train. Each row carries the
template it came from (`group`) so the holdout can be split by template: the reported metrics are measured on
phrasings whose template was never seen in training, which is harder than a random split but is still
synthetic text. Treat the numbers as a sanity check, not as accuracy on real traffic.

Labelling rules (the ones the user gave):
  position  asks which way to trade / where to enter, including screenshot-only messages with a few words
            daytrade: M1..H1, scalp, now, today, session; a bare request with no horizon is daytrade because
                      the desk it routes to is the day-trade desk
            swing:    this week / month, fundamentals, hold for days, H4-only, D1, W1
  research  asks to find out, compare or explain something (markets included) without asking for a position
  other     greetings, app commands, coding chores
"""
from __future__ import annotations

import random
from typing import Optional

SYMS = ["xauusd", "gold", "xau", "eurusd", "eur/usd", "eu", "gbpusd", "gu", "cable", "usdjpy", "uj", "gbpjpy",
        "gj", "audusd", "nzdusd", "usdcad", "usdchf", "eurjpy", "audjpy", "silver", "xagusd", "btc", "nas100",
        "us30", "dxy", "oil", "XAUUSD", "EURUSD", "GBPJPY", "USDJPY"]
DAY_TF = ["m1", "m5", "m15", "m30", "h1", "1m", "5m", "15m", "30m", "1h", "M5", "M15", "H1", "5 min", "15min",
          "15 minute", "1 hour"]
SWING_TF = ["d1", "w1", "daily", "weekly", "1d", "1w", "D1", "W1", "h4", "4h", "H4", "daily chart",
            "weekly chart"]
SESS = ["london", "ny", "new york", "asia", "tokyo", "us", "europe"]
EVENTS = ["cpi", "nfp", "fomc", "fed", "the fed decision", "pce", "ecb", "boj", "rate decision", "gdp",
          "inflation data", "jobs report", "powell speech"]
TOPICS = ["vector databases", "reinforcement learning for trading", "probability calibration",
          "the carry trade", "central bank gold buying", "rust async runtimes", "llm agent frameworks",
          "order flow analysis", "market microstructure", "the yen intervention history", "solid state batteries",
          "retrieval augmented generation", "isotonic regression", "dow theory", "options gamma exposure",
          "the mongolian mining sector", "real yields and gold", "prompt caching", "postgres partitioning",
          "seasonality in fx", "the dollar smile theory", "gradient boosting", "quantum computing"]
TECH = ["qdrant", "pgvector", "milvus", "redis", "postgres", "sqlite", "lightgbm", "xgboost", "fastapi", "axum",
        "react", "svelte", "kafka", "nats", "docker", "kubernetes", "ollama", "duckdb", "clickhouse", "tokio"]
CONCEPTS = ["platt scaling", "a contextual bandit", "gamma squeeze", "the swap rate", "quantitative tightening",
            "walk forward validation", "a liquidity sweep", "the kelly criterion", "vector search",
            "expected calibration error", "the yield curve", "an order block", "tree shap"]
COMPANIES = ["nvidia", "anthropic", "tesla", "oanda", "tradingview", "stripe", "rio tinto", "apple", "typesafe"]
THINGS = ["vector database", "broker api", "charting library", "backtesting framework", "laptop for coding",
          "news feed", "embedding model", "vps provider", "data vendor"]
PURPOSES = ["a small team", "low latency", "semantic search", "intraday data", "a side project", "production",
            "a trading dashboard", "long documents"]
FILES = ["main.rs", "app.tsx", "the login page", "api.py", "the sidebar", "docker-compose.yml", "the websocket",
         "the settings panel", "store.ts"]
UI = ["button", "dark mode toggle", "search box", "chart", "table", "tooltip", "tab", "progress bar"]
MODULES = ["api", "frontend", "agent runner", "auth module", "scheduler", "database layer", "router"]
NAMES = ["scout", "atlas", "nova", "trader one", "helper", "research bot"]
LANGS = ["mongolian", "english", "japanese", "german", "russian"]
PEOPLE = ["boss", "landlord", "team", "client", "professor"]
TIMES = ["tomorrow 9am", "monday", "5pm", "next week", "tonight"]
CODETASKS = ["parses a csv", "retries a request", "sorts users by name", "validates an email",
             "merges two lists"]

# (pattern, weight of having a screenshot attached)
DAY = [
    "long or short now", "{sym} {tf} where entry", "what position can I open today", "buy or sell {sym} now",
    "{sym} scalp setup {tf}", "give me entry sl tp for {sym} {tf}", "can i long {sym} now",
    "can I short {sym} now or wait", "is it good to sell {sym} in {sess} session",
    "{sym} {tf} and {tf} where to enter", "should I buy {sym} right now", "entry for {sym} today",
    "{sym} now buy or sell", "{sess} session {sym} trade idea", "where sl and tp for {sym} short now",
    "any setup on {sym} for today session", "quick scalp {sym}",
    "i want open position {sym} today, which direction", "what you think {sym} {tf}, can enter",
    "{sym} intraday signal", "day trade {sym} today long or short", "which pair can I trade now",
    "{sym} {tf} breakout, enter buy", "{sym} is at support on {tf}, long here",
    "open {sym} sell now, where stop loss", "today {sym} direction, i want trade {sess} open",
    "what position for {sym} before {sess} close", "scalp {sym} {tf} entry now please",
    "tell me buy or sell today", "{sym} entry point for today session",
    "next hour {sym} up or down, i want to enter", "is now good time to buy {sym}",
    "{sym} 20 pip stop 50 pip target, long or short today", "trade setup now", "{sym} {tf} analysis, entry",
    "analyze {sym} now for a trade", "{sym} position today", "where can i enter {sym} this session",
    "sell {sym} here or wait for pullback on {tf}", "{sym} {tf} long setup valid", "{sym}", "{sym} now",
    "{sym} buy", "{sym} sell", "{sym} long", "{sym} short or long",
]
SWING = [
    "is {sym} buy this week based on {ev} and {ev}", "{sym} swing trade idea for this week",
    "should I hold {sym} long for few days", "{sym} {htf} outlook, buy or sell for next week",
    "position for {sym} this month based on fundamental", "long term buy {sym}, hold for weeks",
    "swing setup {sym} {htf}", "can I buy {sym} and hold until {ev} next week",
    "weekly bias {sym} long or short", "{sym} fundamental say buy or sell this month",
    "is {sym} a sell this week after {ev}", "i want swing position on {sym}, hold for days, where entry",
    "{sym} {htf} trend is up, should i buy and hold this week", "what position to hold over the week on {sym}",
    "based on {ev} and {ev} is {sym} long this month", "buy {sym} for next few weeks",
    "medium term {sym} position, buy or sell", "{sym} swing long from {htf} support, hold for days",
    "this week {sym} direction for swing trade", "should i keep my {sym} long over the weekend and next week",
    "position trade {sym} based on interest rate outlook", "{sym} {htf} buy or sell, hold 1-2 weeks",
    "which pair to swing this week", "where to buy {sym} for a multi day swing",
    "fundamental and {htf} say what for {sym}, long or short this month",
    "swing short {sym} this month, where stop", "{sym} buy and hold till end of month",
    "is {sym} good long for the coming weeks given {ev}", "{sym} this week long or short, fundamental view",
    "open a swing on {sym} from the {htf}, hold some days",
]
SCREEN_DAY = ["", "?", "this", "check", "now?", "entry?", "{sym}", "{sym} {tf}", "what do you think",
              "long or short", "buy or sell?", "position?", "{tf}", "look", "{sym} now", "can enter?",
              "where entry", "sl tp?", "see chart", "{sym} {tf} ?"]
SCREEN_SWING = ["{sym} {htf}", "{htf}", "swing?", "{sym} weekly", "hold this week?", "{htf} buy or sell",
                "this week?", "{sym} swing", "weekly view, position?", "{sym} {htf} hold?"]
RESEARCH = [
    "research the best {thing}", "research {topic}", "compare {tech} and {tech}", "why did {sym} drop yesterday",
    "what is the impact of {ev} on {sym} historically", "explain how {ev} affect {sym}",
    "find papers about {topic}", "summarize the latest news on {topic}", "deep dive on {topic}",
    "what are the pros and cons of {tech}", "investigate {topic} and write a report",
    "how does {concept} work", "does london breakout strategy work on {sym}, research it",
    "who are the main competitors of {company}", "what is the history of {topic}",
    "find the best {thing} for {purpose}", "look up how {tech} handles {concept}", "survey of {topic}",
    "what do analysts say about {topic}", "study the correlation between {sym} and {sym} over last 5 years",
    "explain what is {concept}", "research report on {company}", "is {tech} better than {tech} for {purpose}",
    "learn about {topic} and give me summary", "what drives {sym} price in general",
    "gather sources on {topic}", "how often {sym} reverses after {ev}, check the data",
    "market research for {thing}", "what happened to {sym} last month and why", "overview of {topic} please",
    "which {thing} is recommended in 2026, do research", "analyse the literature on {topic}",
    "how did {sym} react to past {ev} releases", "tell me about {company} business model",
    "read this paper and explain the method", "what is the difference between {concept} and {concept}",
    "i need a report about {topic} with sources", "search the web for {topic}",
    "benchmark {tech} against {tech}", "who invented {concept} and when", "why is {sym} falling",
    "why {sym} went up last week", "reason for the {sym} move yesterday", "what moved {sym} after {ev}",
]
OTHER = [
    "hi", "hello", "hey there", "good morning", "thanks", "thank you", "ok", "ok thanks", "nice", "bye",
    "how are you", "who are you", "what can you do", "help", "test", "are you there", "lol", "cool",
    "fix the bug in {file}", "add a {ui} to the dashboard", "refactor the {module}", "run the tests",
    "commit the changes", "why is the build failing", "change theme to dark", "stop the agent",
    "rename this agent to {name}", "show my usage", "translate this to {lang}", "write an email to my {person}",
    "set a reminder for {time}", "tell me a joke", "delete the last task", "open settings", "update the readme",
    "deploy the backend", "create a new agent called {name}", "restart the server", "make the font bigger",
    "what time is it", "export this chat", "fix this error", "install {tech}",
    "write a function that {codetask}", "clean up the code in {file}", "how do I change my password",
    "cancel that", "continue", "try again", "what model are you using", "close all agents",
    "merge the pull request", "write unit tests for the {module}", "schedule this task every morning",
    "connect my {tech} account", "the {ui} is broken on mobile", "undo the last change", "clear the history",
    "why is the {module} so slow", "add logging to {file}",
]

# The phrasings the user gave, verbatim. Always in training, never in the holdout.
PINNED = [
    ("long or short now?", "position", "daytrade", 0, [], None),
    ("xauusd m15 where entry", "position", "daytrade", 0, [], "XAUUSD"),
    ("what position can I open today", "position", "daytrade", 0, [], None),
    ("is gold buy this week based on fed and cpi", "position", "swing", 0, [], "XAUUSD"),
    ("research the best vector database", "research", None, 0, [], None),
    ("", "position", "daytrade", 2, ["15m"], "XAUUSD"),
    ("m5 scalp gold now", "position", "daytrade", 1, ["5m"], "XAUUSD"),
    ("hold eurusd long for days, d1 and w1 trend up", "position", "swing", 0, ["1d", "1w"], "EURUSD"),
]

PREFIX = ["", "", "", "", "", "hey ", "hi ", "ok so ", "pls ", "bro ", "sir ", "quick question ", "so "]
SUFFIX = ["", "", "", "", "?", "?", " pls", " please", " ?", " thx", " bro", "...", " ??", " asap"]
SLOTS = {"sym": SYMS, "tf": DAY_TF, "htf": SWING_TF, "sess": SESS, "ev": EVENTS, "topic": TOPICS, "tech": TECH,
         "concept": CONCEPTS, "company": COMPANIES, "thing": THINGS, "purpose": PURPOSES, "file": FILES,
         "ui": UI, "module": MODULES, "name": NAMES, "lang": LANGS, "person": PEOPLE, "time": TIMES,
         "codetask": CODETASKS}
DAY_TF_SETS = [["15m"], ["5m"], ["1h", "15m"], ["15m", "5m"], ["4h", "1h", "15m"], ["1h", "15m", "5m"], ["1h"],
               ["4h", "1h", "15m", "5m"], ["1m"], ["30m"]]
SWING_TF_SETS = [["1d"], ["1w", "1d"], ["4h", "1d"], ["1w"], ["4h"], ["1d", "4h"], ["1w", "1d", "4h"]]
CANON = {"gold": "XAUUSD", "xau": "XAUUSD", "xauusd": "XAUUSD", "eu": "EURUSD", "eur/usd": "EURUSD",
         "gu": "GBPUSD", "cable": "GBPUSD", "uj": "USDJPY", "gj": "GBPJPY", "silver": "XAGUSD"}


def _fill(rng: random.Random, pattern: str) -> tuple[str, Optional[str]]:
    sym, out = None, pattern
    while "{" in out:
        a = out.index("{")
        b = out.index("}", a)
        key = out[a + 1:b]
        val = rng.choice(SLOTS[key])
        if key == "sym" and sym is None:
            sym = CANON.get(val.lower(), val.upper().replace("/", ""))
        out = out[:a] + val + out[b + 1:]
    return out, sym


def _typo(rng: random.Random, text: str) -> str:
    words = text.split(" ")
    idx = [i for i, w in enumerate(words) if len(w) > 4 and w.isalpha()]
    if not idx:
        return text
    i = rng.choice(idx)
    w, k = words[i], rng.randrange(1, len(words[i]) - 1)
    words[i] = w[:k] + w[k + 1:] if rng.random() < 0.5 else w[:k - 1] + w[k] + w[k - 1] + w[k + 1:]
    return " ".join(words)


def _dress(rng: random.Random, text: str, decorate: bool = True) -> str:
    if decorate and text:
        text = rng.choice(PREFIX) + text + rng.choice(SUFFIX)
    if rng.random() < 0.15:
        text = _typo(rng, text)
    r = rng.random()
    if r < 0.6:
        text = text.lower()
    elif r < 0.7:
        text = text[:1].upper() + text[1:]
    elif r < 0.75:
        text = text.upper()
    return text.strip()


def _attachments(rng: random.Random, p: float) -> int:
    return rng.choice([1, 1, 1, 2, 2, 3, 4]) if rng.random() < p else 0


def generate(seed: int = 7, per_template: int = 16) -> list[dict]:
    """Rows: {text, attachments, timeframes, symbol, intent, style, group}. Same seed -> same rows."""
    rng = random.Random(seed)
    rows: list[dict] = []
    seen: set = set()

    def add(text, intent, style, att, tfs, sym, group):
        key = (text, att, tuple(tfs), sym, intent, style)
        if key in seen:
            return
        seen.add(key)
        rows.append({"text": text, "attachments": att, "timeframes": list(tfs), "symbol": sym, "intent": intent,
                     "style": style, "group": group})

    def position(patterns, style, tf_sets, tag):
        for pattern in patterns:
            for _ in range(per_template):
                text, sym = _fill(rng, pattern)
                tfs = rng.choice(tf_sets) if rng.random() < 0.5 else []
                if sym is None and rng.random() < 0.3:
                    sym = rng.choice(["XAUUSD", "EURUSD", "GBPJPY", "USDJPY"])
                elif sym is not None and rng.random() < 0.25:
                    sym = None  # the caller did not detect it
                add(_dress(rng, text), "position", style, _attachments(rng, 0.4), tfs, sym, f"{tag}:{pattern}")

    def screens(patterns, style, tf_sets, tag):
        for pattern in patterns:
            for _ in range(per_template):
                text, sym = _fill(rng, pattern)
                tfs = rng.choice(tf_sets) if (rng.random() < 0.6 or style == "swing") else []
                if sym is None and rng.random() < 0.5:
                    sym = rng.choice(["XAUUSD", "EURUSD", "GBPJPY", "USDJPY"])
                add(_dress(rng, text, decorate=False), "position", style, rng.choice([1, 1, 2, 2, 3, 4]), tfs,
                    sym, f"{tag}:{pattern}")

    position(DAY, "daytrade", DAY_TF_SETS, "day")
    position(SWING, "swing", SWING_TF_SETS, "swing")
    screens(SCREEN_DAY, "daytrade", DAY_TF_SETS, "screen_day")
    screens(SCREEN_SWING, "swing", SWING_TF_SETS, "screen_swing")
    for pattern in RESEARCH:
        for _ in range(per_template):
            text, sym = _fill(rng, pattern)
            if sym is not None and rng.random() < 0.5:
                sym = None
            tfs = rng.choice(DAY_TF_SETS + SWING_TF_SETS) if rng.random() < 0.05 else []
            add(_dress(rng, text), "research", None, _attachments(rng, 0.12), tfs, sym, f"research:{pattern}")
    for pattern in OTHER:
        for _ in range(per_template):
            text, _ = _fill(rng, pattern)
            add(_dress(rng, text), "other", None, _attachments(rng, 0.12), [], None, f"other:{pattern}")
    for i, (text, intent, style, att, tfs, sym) in enumerate(PINNED):
        add(text, intent, style, att, tfs, sym, f"pinned:{i}")
    return rows
