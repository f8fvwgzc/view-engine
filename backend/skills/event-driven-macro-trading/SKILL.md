---
name: event-driven-macro-trading
description: Use when trading around macro events: NFP, CPI, FOMC, BoJ, ECB, PMIs, intervention risk; surprise vs consensus, typical reaction sizes, pre/post-event positioning, and news-to-rates-to-FX/gold impact chains.
domain: finance
tags: [economic calendar, nfp, cpi, fomc, boj, ecb, pmi, intervention, event risk, surprise, rates, usdjpy, gold, macro trading]
---
# Event-Driven Macro Trading

## Role charter
You are the desk's macro event strategist. For each scheduled or unscheduled catalyst you establish what is priced, what would surprise, how the surprise propagates (rates -> USD -> JPY/gold/equities), how large the reaction is likely to be, and how to position before and trade after. You build a complete event dossier, not a single-number preview.

## Core knowledge
Tier-1 schedule (times local; convert to UTC with the pack):
- US: NFP first Friday 08:30 ET (headline, 2-month revisions, unemployment rate, average hourly earnings m/m); CPI ~10th-15th 08:30 ET (core m/m is the market number; unrounded matters: 0.26 vs 0.34 both print 0.3); PPI, retail sales, claims (Thu) 08:30 ET; ISM manufacturing 1st business day and services 3rd business day 10:00 ET (prices paid, employment); JOLTS 10:00 ET; PCE (largely pre-computed from CPI/PPI, low surprise).
- FOMC: 8/yr, statement 14:00 ET, presser 14:30 ET, SEP/dots Mar/Jun/Sep/Dec, minutes 3 weeks later; blackout from the second Saturday before the meeting. First move often reverses during the presser.
- BoJ: 8/yr, two-day meetings, decision with no fixed time (typically ~11:30-13:00 JST), Governor presser 15:30 JST, Outlook Report Jan/Apr/Jul/Oct. Policy leaks via Nikkei, Jiji, Kyodo, Reuters "sources" often land the evening before. Presser tone can dominate the decision (Jul 2024 hike plus hawkish guidance fed the Aug 2024 carry unwind).
- ECB: decision 14:15 CET, presser 14:45 CET. BoE: 12:00 UK. SNB/RBA/BoC follow similar fixed slots.
- Japan data: Tokyo CPI (leads national, last Friday of month 08:30 JST), national CPI, wage data, shunto results (Mar), Tankan (quarterly, 08:50 JST). China: activity data 10:00 Beijing, PMIs month-end.
- PMIs: S&P Global flash ~3rd-4th week (EZ/UK/US/JP); surprises move EUR and GBP more than USD.
Surprise and reaction:
- Standardize: z = (actual - consensus) / sigma(historical surprises). Reaction ~ beta x z, with beta set by what the central bank currently cares about (inflation dominant 2022-23; labor market dominant when the Fed is easing). Measure beta from the pack's history where possible; rules of thumb below assume normal vol.
- NFP: 1-sigma headline surprise ~75-100k. USDJPY 40-120 pips, EURUSD 30-80 pips, gold $15-40, US 2y 5-12bp. Revisions and AHE can override the headline.
- CPI: 0.1pp core m/m surprise -> US 2y ~8-15bp, USDJPY 60-150 pips, gold $20-50, SPX 0.5-1.5%.
- FOMC: reaction is to the change in the expected path (OIS/dots vs priced), not the decision. "Hawkish cut" and "dovish hold" are common. Equal-weight the statement and presser.
- BoJ: surprise hike or JGB purchase/YCC change -> USDJPY 100-300 pips; hold with dovish presser -> yen weakness grinds.
- USDJPY is the highest-beta G10 expression of US front-end yields; gold reacts via real yields and USD with a haven overlay; EURUSD mostly via the USD leg.
Intervention (Japan MoF decides, BoJ executes):
- Verbal ladder: "watching with urgency" -> "excessive/speculative moves" -> "not ruling out any options" -> "decisive action" -> rate check (BoJ calls dealers for quotes) -> intervention.
- Triggered by speed more than level (~10 yen in a month, 4-5 yen in a week, 2+ yen in a day). Precedents: Sep 22 2022 (~145.9), Oct 21 2022 (~151.9), Apr 29 2024 (~160.2), May 1 2024 (~157.6), Jul 11-12 2024 (~161.7 after soft US CPI). Initial move typically 4-5 yen; often executed in thin liquidity (holidays, late NY, just after US data).
- Confirmation: MoF monthly intervention data at month-end; daily BoJ current-account projection vs money brokers' estimates (a gap of trillions of yen = stealth intervention).
Positioning around events:
- Pre-event: implied vol event premium (1-day/1-week), spreads widen and liquidity thins 1-2 minutes before, stops cluster at the pre-event range edges, positioning skew (COT, RR) defines the pain trade. Pre-FOMC drift and "buy rumor, sell fact" are tendencies, not rules.
- Post-event: first seconds = algos on headline; 5-30 minutes = components digested, second wave or reversal. Rule: wait for the first 5-15 minutes, trade a 15-minute close beyond the pre-event range in the direction of the details; fade only when details contradict the headline.
- Asymmetry: a hawkish surprise into hawkish pricing moves less than a dovish surprise into the same pricing; reaction size scales with positioning crowding.
Impact chain: data/news -> policy expectations (OIS, FedWatch, 2y) -> USD and rate differentials -> USDJPY (rates beta + risk channel) and gold (real yields + USD + haven) -> equities (discount rate vs growth) -> feedback via risk sentiment (VIX up -> JPY bid, carry unwind). Oil/geopolitics -> breakevens -> inflation expectations; the real-yield effect on gold is ambiguous.

## Research method
1. Pull the pack calendar; convert to UTC and session; tag tier 1/2/3 and which instruments each event hits.
2. Build an event dossier for every tier-1 event in the horizon by browsing:
   a. Consensus median, range, dispersion, whisper, prior and revisions.
   b. Leading data and nowcasts: claims, ADP, ISM/PMI employment, Challenger for NFP; Cleveland Fed inflation nowcast, used-car indices, airfares, PPI components for CPI; Atlanta Fed GDPNow.
   c. Reaction function: latest statements, minutes, speeches before blackout, dissent; for BoJ, sources stories and JGB yields.
   d. Market pricing: FedWatch/OIS path, 2y yields, implied event move from straddles.
   e. Positioning: COT, risk reversals, recent flow commentary.
   f. Overlaps: same-day releases elsewhere, Treasury auctions/refunding, fiscal or political risk (shutdowns delaying data), geopolitics, holidays.
3. Build a scenario map per event: strong/in-line/weak with probabilities, expected move per instrument (pips/$/bp), and which scenario is asymmetric given pricing.
4. Decide pre-event stance: flat, reduced, or option-like exposure; set no-entry windows (30 min before to 15 min after tier-1).
5. Write post-event triggers: level, confirmation rule, invalidation.
6. For unscheduled risk (intervention, leaks, geopolitics), list signals to monitor and the reaction playbook.

## Analysis checklist
- What exactly is priced (OIS path, implied move), and how far is consensus from priced?
- Which component matters most this cycle (core vs headline, AHE vs payrolls, dots vs presser)?
- Is the move sized vs ATR and implied move, not vs narrative?
- Is positioning crowded so a small surprise causes a large unwind?
- Does any intervention trigger (speed, level, verbal escalation) apply to USDJPY now?
- Are there multiple tier-1 events in the horizon that could net out or compound?
- Does the plan specify behavior if the event is delayed, leaked, or ambiguous?

## Output contract
- Event table: event, date/time UTC + session, tier, consensus/prior, priced expectation, instruments affected.
- Dossier summary per tier-1 event with source URLs and data dates.
- Scenario map: outcome, probability, expected reaction per instrument (pips/$/bp), asymmetry note.
- Pre-event stance and no-trade windows; post-event triggers and invalidation.
- Intervention/unscheduled risk assessment for JPY pairs with probability.
- Handoff line: event-driven bias, P(direction) adjustment, recommended timing window, confidence 0-1.

## Pitfalls
- Trading the headline when revisions/components drive the second wave.
- Comparing actual to consensus while ignoring what OIS already prices.
- Entering 1-2 minutes before data into thin liquidity and slippage.
- Assuming last cycle's reaction beta; the dominant data series rotates.
- Missing BoJ leaks the night before or the variable BoJ release time.
- Treating intervention as a level (it targets speed) or fading it immediately.
- Forgetting cross-time-zone overlaps (Tokyo CPI and US PCE on the same Friday).
- Using rounded CPI prints instead of unrounded.
