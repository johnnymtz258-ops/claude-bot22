# FomoBot v16.2 QUALITY MEASUREMENT

> **Simple mode (latest):** WIDE signals (~10x more alerts), 7 core commands, `/bought` works on any alert, `/sold 5` partial sales, fee-aware P/L. See `QUICK_START.txt`.
>
> **Overnight data review (Sep 24):** two months of your data show no measurable edge yet: paper trades lose ~3.9% per trade after fees and your journaled trades lost 9.7%. The bot now shows each alert type's real track record in every BUY alert, records unbiased price paths for fast research, rejects price-feed glitches, and can reclaim token-account rent. See `OVERNIGHT_REPORT.txt`.
>
> **Reliability patch applied.** See `V16_2_RELIABILITY_NOTES.txt`. Live stop-losses no longer freeze on an unrelated stuck transaction. Dropped transactions no longer lock live trading forever. Exits run in their own loop and survive `/autolive off`. Copy positions have their own stop-loss and max hold. Fills are measured from the transaction itself. The proof gate needs a statistically consistent edge that doesn't depend on one lucky trade before it allows real money.

## What v16 changes

v16 adds a public-on-chain smart-wallet copy engine on top of the hardened v15.3 scanner. The goal is to improve **discovery**, not to stack more loss filters. GET READY scout chatter is OFF by default; internal scouting still runs. Telegram prioritizes actual core BUY signals and smart-wallet copy events.

### Copy-trading architecture

1. Tracks up to 20 public Solana wallets over WebSocket. With a Helius key it uses Helius RPC/WSS; without one it falls back to public Solana WSS.
2. On every confirmed wallet transaction, fetches the confirmed Solana transaction and parses the tracked wallet's **final balance deltas**. This is DEX-agnostic, so Jupiter, Raydium, Meteora, Pump.fun/PumpSwap and multi-hop routes do not need separate buy/sell parsers just to identify the final asset. Failed transactions are ignored.
3. Opens a fee-aware paper copy immediately at the follower-time market price and sends `COPY BUY NOW — PAPER/MANUAL TEST` when the tracked wallet score is high enough. Leader sells create proportional COPY EXIT alerts.
4. Birdeye smart-trader/top-flow data can auto-discover candidate wallets. `/copyanalyze WALLET` reconstructs recent confirmed SOL/USDC round trips and assigns an observed-history score.
5. Optional live copying reuses the hardened `live_execution.py` Jupiter path. It is OFF and kill-switched by default and cannot arm until at least 48 hours plus 20 closed copy-paper trades with positive net P/L.

### Copy commands

- `/copy` — copy-engine status
- `/copywallet add ADDRESS [label] [score]` — track a public wallet immediately
- `/copywallets` — tracked wallets and current scores
- `/copydiscover` — wallets discovered from Birdeye smart/top-flow activity
- `/copyanalyze ADDRESS` — reconstruct recent wallet swaps/round trips and update its score
- `/copyreport` — follower-time paper-copy P/L
- `/copykill on|off` — hard new-live-buy kill switch
- `/copylive arm|confirm|off` — optional live copying after the paper gate passes

### First run

1. Unzip the release over a **new folder**. Do not delete `~/Library/Application Support/FomoBot/fomo_master.db`.
2. Run `setup_edge_sources.command` to configure Helius/Birdeye/X/PumpPortal if available, or `setup_copy_trading.command` to add public wallets. Never paste a seed phrase/private key into these source setup scripts.
3. Run `start_mac.command`.
4. In Telegram run `/copy`, `/copydiscover`, `/copywallets`, `/sources`, `/status`.
5. Leave live copy OFF initially. Paper copy is automatic. Add known public smart wallets with `/copywallet add ...` or let the Birdeye auto-discovery build candidates.
6. After at least 48 hours, use `/copyreport` and export the database before considering any live setting.

### Safety and execution

Copy Edge keeps v15.1+ atomic accounting, idempotent execution intents, confirmation polling, actual post-trade wallet-delta reconciliation, RPC failover, keypair revalidation, max-position and daily-loss limits, and a hard kill switch. A leader's failed transaction is never copied. Multi-hop routes are classified from final balance changes, and live follower orders use the existing Jupiter executor rather than rewriting execution.

No public-wallet strategy can guarantee profit. Copying a profitable wallet can still lose because of latency, slippage, hidden hedges, different size/liquidity, or changed behavior.

---

v15.3 fixes the usability dead-end where proof-first safety could suppress every BUY alert. Fully-qualified, proof-eligible core setups now surface immediately under the default ACTIONABLE signal policy. During fresh proof probation they are labeled **BUY NOW — TEST SIZE / MANUAL ONLY**; if the proof/risk engine is quarantined they remain visible as **PAPER BUY NOW** with $0 live sizing. Only proof-ACTIVE setups with bankroll/risk gates open are labeled **LIVE-QUALIFIED BUY NOW**. Real autopilot remains proof-gated and fail-closed. Use `/signalpolicy strict` to restore the older behavior or `/signalpolicy actionable` to receive test-entry alerts.

v15.2 keeps the v15.1 execution hardening but changes how forward proof is measured after the Sep-20 export showed that broad shadow research was poisoning the live-proof gate. It is not designed to maximize Telegram alerts. It separates **research shadows** from a smaller **proof-eligible core** and keeps live-money guidance locked until that core proves positive net behavior after simulated friction.

Release package: `FOMO_BOT_v16_2_QUALITY_MEASUREMENT_FINAL.zip`

No trading bot can guarantee profit. Meme coins can rug, gap, lose liquidity, become unroutable, or move faster than an alert/exit. v16 uses public market/on-chain information only.

## What changed in v16.2 QUALITY MEASUREMENT

v16.2 is a surgical accounting and measurement hotfix on top of the v16.1 signal-velocity release. It does not loosen entry gates, change discovery logic, or enable real trading. The main fixes are:

- `FAST ENTRY` is now included in the proof counter, matching the existing proof-eligibility classifier;
- v16.1 and v16.2 shadow outcomes are treated as one quality cohort because the entry rules are unchanged, so upgrading does not erase proof evidence;
- `/paperreport` now shows per-tier trade count, win rate, net P/L, ROI and friction;
- `/edge` and the paper risk circuit use the same carried-forward quality cohort;
- the stale V15.2 paper-report label is removed.

The underlying v16.1 velocity features remain in place:

- proof-eligible core score default: **55** instead of 70; fresh proof target: **8** completed paths instead of 12;
- ordinary stability window: **20s** instead of 45s; FAST lane can qualify from **2 observations / 15s**;
- new **FAST ENTRY** lane uses stronger live liquidity/turnover/buyer-flow requirements but can act on basic RugCheck/on-chain safety while premium enrichment finishes in the background;
- FAST entries are deliberately smaller and are **not eligible for real live autopilot**; they can surface as ACTIONABLE manual/test signals;
- strict breadth/historical vetoes remain on the STRICT lane, while the FAST lane treats them as soft warnings and relies on regime-aware size reduction instead of automatically silencing the opportunity;
- market regime changes suggested size: STRESS 0.60x, RISK_OFF 0.78x, RISK_ON 1.10x, UNKNOWN 0.85x, still bounded by the normal configured dollar caps;
- scanner, hot-scout/FAST monitoring, and COPY wallet streaming run as separate async tasks;
- slow-provider circuit breakers protect Birdeye/Jupiter/GeckoTerminal calls, plus a separate short circuit breaker protects copy-trading RPC calls;
- TEST BUY signals get a small capped manual-test dollar amount even when `/bankroll` is not configured. Real automatic execution still requires the existing proof, bankroll, risk, idempotency, confirmation and wallet-delta checks.

The objective is **more qualified opportunities with measurable outcomes**, not a guarantee of profitability. Keep COPY LIVE and real autopilot off while validating the new lane on fresh data.

## Why v15 exists

The Sep-9 database contains 81 completed 30-minute v13.3 delivered-entry paths. Across all chains the median maximum upside was only about +5.0%, while one-third suffered at least an -8% drawdown. Solana-only results were also asymmetric: 69 paths had median maximum upside about +6.8%, median endpoint slightly negative, ~39% touched +10%, but ~39% also touched -8% and ~16% touched -15%.

A single fixed liquidity/momentum box did not generalize well enough. v15 therefore separates **discovery** from **authorization** and replaces the static v14 positive filter with a stateful sequence:

1. broad multi-source discovery,
2. legacy safety/classifier filters,
3. market-breadth regime veto,
4. historical analog downside veto,
5. confirmation over time,
6. stateful setup-path quality,
7. Jupiter route/friction + token-safety preflight,
8. always-on net-of-friction shadow trade,
9. fresh-build proof gate,
10. bankroll/risk sizing only after proof is ACTIVE.

## Proof-first behavior

Every fully qualified v15.3 setup is still recorded as a shadow trade, while signal visibility and live-capital authorization are now separate. In the v16.1/v16.2 quality cohort, a core setup is proof-eligible when it meets the selective score floor (55 by default). The build begins in `PROBATION` and targets at least 8 completed proof-eligible paths spanning at least 6 unique tokens. FAST ENTRY participates in forward proof but remains excluded from real live autopilot.

The proof gate becomes `ACTIVE` only if that eligible cohort has positive net P/L, at least 1% net ROI after simulated friction, at least 45% wins, and profit factor >=1.20. Otherwise it remains probationary or becomes `QUARANTINED`. Research-only shadows never authorize live risk.

While proof is not ACTIVE:
- bot-guided live size is $0,
- optional real autopilot cannot open new trades,
- scanning, shadow simulation, diagnostics and learning continue.

Even after proof becomes ACTIVE, live-money guidance remains $0 until `/bankroll AMOUNT` is explicitly set. This prevents an unknown account size from receiving arbitrary dollar guidance.

## Solana-first discovery stack

v15 retains and combines:
- DexScreener token profiles, community takeovers and boosts (attention only; paid boosts are never quality evidence),
- GeckoTerminal new pools and 5m/1h trending pools,
- PumpPortal new-token/migration WebSocket discovery,
- realtime Solana WebSocket candidates,
- Birdeye smart-money/new-listing enrichment when available,
- RSS/Reddit and optional X exact-contract social context,
- manual watches and recent-scout persistence,
- **Runner Radar** for high-flow WATCH ONLY candidates,
- **Jupiter Tokens V2** top-trending, top-organic and recent-pool discovery when `JUPITER_API_KEY` is configured.

Jupiter discovery never grants a BUY bonus by itself. It only broadens the candidate universe.

## Stateful SOL Edge Engine

A candidate is not authorized from a single screenshot. It must build a short history with:
- multiple observations,
- enough elapsed confirmation time,
- controlled move from first confirmation,
- limited drawdown from the local peak,
- preserved liquidity,
- persistent buyer pressure,
- a controlled continuation or modest reclaim shape.

This reduces single-candle/chase entries. It is intentionally separate from crash-memory and token-trauma protection.

## Market breadth and historical analogs

`breadth_regime()` measures the latest candidate universe. `RISK_OFF` requires multiple simultaneous stress symptoms (for example weak median 5m movement plus poor green breadth or widespread flushes), not merely a red SOL candle.

The historical analog layer uses transparent nearest-neighbor comparisons from the Decision Ledger. It is **veto-only**. Similar historical setups with repeated severe drawdowns can block a candidate; historical similarity can never create a BUY by itself.

## Jupiter execution and token preflight

`execution_quality.py` is read-only:
- If a PUBLIC Solana wallet address is configured, it can request unsigned Jupiter Swap V2 `/order` quotes and immediately discard transaction/request material. It never calls `/execute` or `/build`.
- Without a public taker address, it can use Jupiter's legacy quote endpoint only for route measurement.
- It estimates SOL -> token -> SOL round-trip route loss plus configurable frontend friction.
- When `JUPITER_API_KEY` is configured, Tokens V2 exact-mint metadata is an independent safety source. Explicit Jupiter `audit.isSus` or banned status is a hard veto. Organic Score/verification are context, not automatic BUY signals.

No private key or seed phrase is needed for these read-only checks.

## v15.1 execution hardening retained in v15.3

v15.3 retains the v15.1 live-money execution hardening and v15.2 selective-proof cohorting while adding actionable pre-proof signaling. Real autopilot remains optional and OFF by default. New safeguards include:

- SQLite WAL mode, a 10-second busy timeout and explicit `BEGIN IMMEDIATE` transactions for critical multi-step accounting.
- A durable `execution_intents` journal. The bot stores the signed Solana transaction signature before submitting to Jupiter.
- Idempotency keys and cross-process execution leases so concurrent loops/processes cannot intentionally submit the same logical order twice.
- Solana confirmation polling. The chain is the source of execution truth even if Jupiter's HTTP response is lost or contradictory.
- Post-trade wallet-delta reconciliation. A confirmed transaction is not treated as fully accounted until the received wallet balance can be verified.
- Quote-vs-actual output slippage measurement and persistent warnings when the realized wallet delta is materially worse than the quote.
- Explicit wallet balance checks, reserve checks and per-trade keypair fingerprint validation immediately before signing.
- Configurable RPC fallback pool plus explicit network and confirmation timeouts.
- Centralized live-order authorization: unresolved execution state, proof failure, governor quarantine, paper loss circuits, missing bankroll or daily live risk limits lock new real orders at one gate.
- Startup/runtime reconciliation for ambiguous signed transactions. The bot never blindly resends an unresolved signed order.
- Structured `trade_failures` records and `/execstatus` for post-restart auditability.

If an execution is ambiguous, the safest behavior is intentional: new real-money entries lock until reconciliation can establish chain/wallet truth.

## Risk controls

Defaults:
- proof sample: 12 fresh proof-eligible paths across at least 6 unique tokens,
- paper/shadow position: $5,
- per-trade bankroll risk budget: 0.5%,
- hard price-risk reference: about 8%,
- shadow losing-streak circuit: 3 consecutive losses -> temporary pause,
- paper daily loss circuit: 2% of configured bankroll,
- normal live guidance requires `/bankroll` after proof is ACTIVE,
- real autopilot remains OFF unless explicitly armed and confirmed,
- fresh/structure/micro/cross-chain BUY lanes are not default live-money lanes.

Example: `/bankroll 100` with 0.5% risk and an 8% risk line implies a raw risk-sized cap of about $6.25 before other caps/reducers.

## Main Telegram commands

### Edge / validation
- `/edge` — proof status, breadth, shadow performance, execution health and risk circuit
- `/leaders` — strongest recent Runner Radar candidates; WATCH ONLY
- `/shadow on|off` — continuous forward-validation engine
- `/paperreport` — net paper/shadow results including friction, PF, wins/losses and drawdown
- `/why CONTRACT` — current pipeline diagnosis and blocker
- `/status` — scanner/source/pipeline health
- `/sources` — discovery/provider health

### Risk / sizing
- `/bankroll 100` — set risk-sizing bankroll; `/bankroll 0` clears it
- `/size CONTRACT` — current size guidance (can be $0 by design)
- `/confidence CONTRACT`

### Manual journal / Guardian
- `/positions`, `/hold COIN`
- `/buy CONTRACT AMOUNT`
- `/bought AMOUNT` when replying to a fresh bot entry
- `/sellpct`, `/sellusd`, `/sell`
- `/sellcash`, `/closecash`, `/reconcile`, `/undo`, `/fixentry`
- `/guardian on|off|status`

### Optional real autopilot
- `/autolive arm` -> `/autolive confirm`
- `/autolive off`
- `/autostatus`, `/autotest`, `/execstatus`

Real autopilot is not required for the proof engine and should remain OFF while collecting the first fresh v15 cohort.

## Recommended first run

1. Stop the previous bot with `Control + C`.
2. Unzip `FOMO_BOT_v16_2_QUALITY_MEASUREMENT_FINAL.zip`.
3. Double-click `start_mac.command`.
4. Do **not** delete `~/Library/Application Support/FomoBot/fomo_master.db`.
5. In Telegram send `/copy`, `/copydiscover`, `/copywallets`, `/status`, `/sources`, `/edge`, `/paperreport`.
6. Keep `/shadow on` and real autopilot OFF.
7. Do not set a bankroll just to make the bot produce non-zero live sizes. Paper proof works without live capital.

After a meaningful fresh v16 sample, use `export_data.command` and evaluate copy-paper results, core TEST signals, and LIVE-qualified signals separately from older builds.

## Optional data-source upgrades

- `JUPITER_API_KEY`: Tokens V2 discovery/safety and higher-rate Jupiter access.
- `PUBLIC_SOLANA_WALLET_ADDRESS`: public address only; allows read-only wallet sync and more realistic Swap V2 preflight. Never put a seed/private key here.
- `HELIUS_API_KEY`: higher-quality Solana WebSocket/RPC path.
- `BIRDEYE_API_KEY`, `PUMPPORTAL_API_KEY`, `X_BEARER_TOKEN`: optional enrichments. Failure is isolated; core discovery continues where possible.

## Data limitation behind v15

The Sep-9 export retained Decision Ledger/outcomes across the multi-day period but its `observations` table had been pruned to roughly the final half-day. Therefore the new multi-snapshot path gate could not be honestly backtested across the entire 81-alert holdout. v15 does **not** report a fabricated historical win rate for that new gate. The always-on shadow/proof system exists specifically to collect that missing forward evidence.

## Security / packaging

The release ZIP excludes `.env`, private keys, seed phrases, the real master database, exported user data, caches and development backups. Public wallet addresses may be configured locally after installation; private signing material is not needed for ordinary scanning/shadow operation.