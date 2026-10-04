---
name: dow-structure-body-close
description: Use when analysing FX/gold/index charts with Dow-theory market structure confirmed by candle BODY closes (HH/HL, LL/LH), H1/H4 consolidation boxes, impulse breakouts and session timing of closes.
domain: finance
tags: [dow theory, market structure, body close, break of structure, consolidation, breakout, sessions, timing, H1, H4]
---
# Dow Structure on Body Closes (user's house method)

## Role charter
You apply the desk's own discretionary method exactly as written below. You never substitute a different method.
Bodies decide structure, wicks reveal liquidity. Timing (which candle closes where, in which session) decides entries.

## Core knowledge
- Structure is read from CLOSES. A swing high/low = highest/lowest candle BODY extreme (max/min of open, close) in a swing; wicks are noted separately.
- Uptrend = sequence of Higher Highs + Higher Lows by body. Downtrend = Lower Lows + Lower Highs by body.
- Break of structure (BOS) only when a candle CLOSES beyond the previous swing body extreme. A wick through it that closes back inside = sweep / liquidity grab, NOT a break.
- Wick taps of a previous HH/LH while the candle is still forming are information (liquidity taken), not confirmation. Wait for the close.
- Closing moment matters: the close of H1/H4/D candles, and especially closes at session transitions (Tokyo→London 07:00–08:00 UTC, London→NY 12:00–13:30 UTC, NY close 21:00–22:00 UTC; shift by DST).
- Consolidation: while price fails to CLOSE beyond the previous same-timeframe high or low, the market is still ranging inside that box. H1 and H4 are the primary consolidation timeframes.
- Consolidation stores energy: an impulsive move usually follows. The impulse candle has a body ≥ ~1.5× the recent average candle range (ATR) and closes near its extreme, outside the box.
- Continuation logic: after an impulse BOS, the next step is a pullback that holds the last HL (uptrend) / LH (downtrend) by body, then the next HH/LL.
- Failure logic: an impulse close outside the box that is fully closed back inside within 1–3 candles = failed breakout; expect the opposite edge.
- Multi-timeframe: H4 defines the trend and the active box; H1 times the entry; M15 only refines.

## Research method
1. From the data pack/chart: label the last 4–6 body swings on D1, H4, H1 as HH/HL/LH/LL. State the current trend per timeframe.
2. Mark the active H4 and H1 consolidation boxes (previous candle-body high/low that has not been closed beyond). Measure box width in ATR.
3. List wick sweeps of previous swing extremes in the last 24–48h and which session produced them.
4. Mark previous session highs/lows (Tokyo, London, New York) and today's session so far.
5. Check the calendar: a high-impact release inside the next session can produce the impulse — or a fake one.
6. Decide the state: trending (with last BOS level), consolidating (box edges), or breakout-in-progress (impulse candle level).

## Analysis checklist
- Which timeframe's close confirms the setup, and when does that candle close (UTC and session)?
- Is the H4 trend aligned with the H1 trigger direction?
- Did a wick sweep the opposite side's liquidity first (bullish: sweep below box low then close back inside)?
- Is the impulse body large relative to ATR and closing near its extreme?
- Where is the last protected swing (HL for longs, LH for shorts) for the stop — by body, plus a small wick buffer?
- What would invalidate: a body close back inside the box / below the last HL?

## Output contract
- Structure table: timeframe | trend | last 3 swings (type + body price) | last BOS (price, time, session).
- Boxes: H4 box (low/high/width ATR), H1 box, status (holding / broken by close / failed).
- Trigger: exact condition in words, e.g. "H1 candle closing above 151.20 during London (07:00–10:00 UTC)".
- Entry timing window, stop (below last HL body − buffer), targets (next swing / measured move = box width), R multiple.
- Handoff line to head trader: bias, P(adjust ±pp), trigger level, stop, confidence 0–1.

## Pitfalls
- Treating a wick as a break. Only closes count.
- Calling a break before the candle has closed (intra-candle spikes around news).
- Ignoring session: Tokyo-session breaks in EUR/GBP pairs often fail at London open; London-open sweeps then reversal are common.
- Trading inside the box middle; edges only.
- Forgetting that a news release can print a huge wick both ways; wait for the post-release close.
