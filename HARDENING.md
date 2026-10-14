# FomoBot v28 — production hardening audit

Scope: the six areas of the audit brief, applied to what this bot is — a Python asyncio Solana bot that alerts on
whale buys and hype, paper-trades every alert, and (optionally) mirrors those trades with real SOL through Jupiter.
Every finding below comes from the code or from your own exported data (Oct 2–12, 856 whale trades, 251 hype alerts).

## 1. Latency & ingestion

**Found (your data):** whale trades from the live stream reached the bot in **2 s** typical — the stream is fast. The
damage came from elsewhere: 268 trades (31%) were only found by the backup poller, typically **65 minutes late**;
108 stream trades took 10+ minutes. Days with the Mac asleep or Helius out of credits had 70–100% late; Oct 9 (awake,
RPC fine) had 0 of 42 late. Code gaps: a wallet whose `logsSubscribe` failed was never retried until a full
reconnect; nothing noticed a subscription that silently stopped delivering; every whale trade needed a
`getTransaction` round trip before it could be judged.

**Built:**
- Pump.fun buys are decoded straight from the stream's logs (the `TradeEvent` pump.fun logs as
  `Program data: vdt/007m…`): no RPC call, no wait for the node to index the transaction, works even when the RPC plan
  is used up. Only events logged by the pump.fun program itself count. (`fomo/pumplog.py`)
- Failed subscriptions are retried every 30 s; subscriptions are paced (no burst of 50 at once).
- Self-healing stream: when the poller finds a fresh trade the stream never delivered, twice in 30 minutes for one
  wallet, that wallet is unsubscribed and subscribed again.
- Adaptive backup poller: every 15 s while the stream is down or missing trades, 45 s otherwise.
- Always-on hosting (v27) and any free RPC with automatic failover (v27.1) remove the two biggest causes.

**Not built, on purpose:** Yellowstone gRPC / Geyser streams cost $49+/month at every provider that offers them, and
a websocket on a good RPC is already ~2 s for this use (you copy whales, you don't snipe launches in the same slot).
Bloxroute/Flashbots/private mempools are Ethereum tools; Solana has no public mempool.

## 2. Rug-pull & scam detection

**Found:** checks covered mint/freeze authority, RugCheck "danger" risks, top-10 holder concentration (bundles) and
"whale-only" coins. Missing: Token-2022 trap extensions, any check that you can actually sell, LP lock status, dev
history, and whether the buying is real. The checks for a whale alert ran three at a time; Jupiter's were absent.

**Built:** (`fomo/scam.py`, `fomo/jupiter.py`)
- Token-2022 traps, read from the mint account already loaded: permanent delegate, transfer hook, non-transferable,
  frozen-by-default accounts, pausable, transfer tax ≥ 1%. A trapped coin is never alerted and never bought.
- Honeypot / hidden-tax check before every real buy: Jupiter's sell quote for the exact tokens about to be bought; if
  selling straight back would lose more than `LIVE_MAX_ROUNDTRIP_LOSS_PCT` (12%), the buy is skipped.
- LP: RugCheck's locked/burned share; under 50% (off the pump.fun curve) lowers the grade.
- Dev history and real-vs-bot buying from Jupiter: organic score, organic buyers, the dev's minted-coin count and
  Jupiter's audit flag — in hype scores and whale grades.
- All pre-alert checks run concurrently (`asyncio.gather`): chain, RugCheck, holders, Jupiter; Jupiter is capped at
  4 s so it can never hold an alert up.

The bot doesn't block on soft signals (bot volume, serial dev, thin LP lock): they lower the score/grade and are
measured in "Which signals have worked". Hard traps (above) block — those coins can take your tokens or stop you
selling, so there is no upside to measure.

## 3. Execution, routing & MEV

**Found:** swaps were sent to the RPC (where sandwich bots watch), with a fixed 15% slippage and a "veryHigh"
priority fee. A buy cost three calls in sequence with no safety check in between.

**Built:** (`fomo/live.py`)
- Jito private sending (`LIVE_JITO`, on): Jupiter builds the swap with a Jito tip (`LIVE_JITO_TIP_LAMPORTS`, 0.0002
  SOL) and it goes to Jito's block engine — not visible to sandwich bots. If it hasn't landed in 20 s, the **same
  signed transaction** goes out through the RPC (same signature, so it can never execute twice).
- Slippage per buy: floor `LIVE_MIN_SLIPPAGE_BPS` (3%) + twice the expected price impact (trade size vs pool depth) +
  how fast the coin is moving, capped at `LIVE_SLIPPAGE_BPS`. Sells keep the cap: getting out matters more.
- Jupiter's lite API is being retired: calls fall back to `api.jup.ag` (or use it directly with `JUPITER_API_KEY`).

**Honest limits:** nothing "completely eliminates" MEV — Jito removes the public-mempool path most sandwich bots use,
and tight slippage caps what's left. Jito and Jupiter can't be exercised from the test environment; both have
fallbacks, and dry run shows exactly what would be sent.

## 4. Position sizing & risk

**Built:**
- `LIVE_DYNAMIC_SIZE` (on): ×1.5 on a strong signal (grade A or hype 70+), ×0.5 on grade C, ×0.5 after 3 real losses
  in a row, never more than `LIVE_MAX_LIQ_PCT` (1%) of the pool, never more than a quarter of the wallet. A pool too
  thin for a quarter-size buy is skipped.
- Kill switch: `LIVE_KILL_FAILS` (3) failed transactions in a row halt new buys for `LIVE_KILL_MINUTES` (30). New buys
  also pause while every private RPC is down, while the live stream is reconnecting, and while a second copy of the
  bot is running. Selling and the exit plan never stop.
- Existing: `LIVE_DAILY_LOSS_SOL`, `LIVE_MAX_OPEN`, `LIVE_PROFITABLE_ONLY`, dry run first.

## 5. Trade lifecycle & exits

**Built:**
- Emergency exit when the pool is being emptied: liquidity down `RUG_LIQUIDITY_DROP_PCT` (70%) from entry on two
  readings 10 s apart → sell everything, whatever the plan says.
- Two-step ladder for hype trades (`HYPE_HALF_AT_TP`): half at the take-profit, the rest rides on the stop / trail /
  time limit. The self-tuner replays 72 exit plans (36 all-out + 36 ladders) on your alerts and switches when one is
  clearly better. (The "+54% a trade" first reported here was wrong — see the correction below.)
- Desync fixes: a sell that doesn't confirm in time is remembered; before selling again, the bot checks whether it
  landed late and books what it really got from that transaction's balances. Every 5 minutes, open real positions
  are checked against the wallet's actual token balance; one that's gone (sold elsewhere) is closed.

## 6. Concurrency, memory & fail-safes

**Found:** all loops ran under one `asyncio.gather`, so an unexpected exception in any loop ended the whole bot (the
launcher/systemd restarted it, losing state for seconds). The market and holder caches grew forever — on a 1 GB
server that's a slow leak over weeks.

**Built:**
- Supervisor: each loop restarts by itself after a crash (2 s, 4 s … at most 60 s apart); the rest keeps running.
  Crashes are counted and shown.
- Bounded caches (market, holders, price marks, Jupiter) and pruning of old scanner data.
- Websocket reconnects already back off exponentially (1 s → 30 s) and fall back to the public stream after 3
  failures; RPC nodes rest after failures and the next one takes over.

## Telemetry

`/health` and the Settings tab: stream state (subscribed / wanted, pushed trades, reconnects, resubscriptions),
whale-trade delay by source over 24 h (typical and slowest 10%), pump.fun buys read from logs, each RPC provider's
calls / errors / average ms / resting state, live execution (sent via Jito / RPC, fallbacks, failures, skipped by the
checks, confirm time), kill-switch state, crashes, memory. A snapshot is saved every 10 minutes and included in
`/export`. RPC providers are shown by name only — links carry keys.

## New settings

`LIVE_JITO` · `LIVE_JITO_TIP_LAMPORTS` · `LIVE_MIN_SLIPPAGE_BPS` · `LIVE_MAX_ROUNDTRIP_LOSS_PCT` ·
`LIVE_DYNAMIC_SIZE` · `LIVE_MAX_LIQ_PCT` · `LIVE_KILL_FAILS` · `LIVE_KILL_MINUTES` · `HYPE_HALF_AT_TP`
(all in /settings and the dashboard; defaults above).

## Correction (v28.3) — paper was selling into emptied pools

Your Oct 12 export showed what the numbers above missed. Most hype alerts were on **pools minutes old, seeded with
more liquidity than their market cap, whose liquidity was then pulled** (the creator seeds a pool, bots pump it, the
liquidity is removed). Once that happens nobody can sell; the last price simply freezes. The paper autopilot — and the
self-tuner's replays — kept "selling" at that frozen price, so they booked profits that could never happen.

Measured honestly on 134 hype alerts: pools under 10 minutes old were emptied within 2 hours 87% of the time; market
cap under $20K 90%; liquidity ≥ market cap 97%; Jupiter "bot volume" 94%. 38 paper trades had sold into emptied pools:
+$1,184 booked, about −$1,849 in reality. The honest balance was about −$680 from $1,000, not +$2,332, and no exit plan
was profitable once those trades count as losses.

Fixed:
- Paper fills are liquidity-aware: a sale returns what the pool's depth allows (constant product); an emptied pool
  returns next to nothing; a price frozen for 10 minutes in a near-empty pool is a rug, not a price; a jump printed
  into an empty pool is never a take-profit.
- Liquidity is recorded with every price mark, and the self-tuner's replays treat an emptied or frozen pool as
  unsellable.
- Past paper trades that sold into emptied pools are re-booked once (the original figure is kept); a new paper period
  starts with honest fills; the exit plan is re-chosen on honest replays.
- The setup is blocked before entry: pools younger than `HYPE_FRESH_MINUTES` (30) with a market cap under
  `HYPE_FRESH_MIN_MC` ($60K) or liquidity ≥ ~market cap, and coins Jupiter rates as mostly bot buying
  (`HYPE_BLOCK_BOTS`). Both are measured by the tuner on what they block. Whale alerts on such pools lose 2 grade
  points; the live autopilot never buys them.

Go live only once the new paper period is in profit over a real number of trades; `LIVE_PROFITABLE_ONLY` enforces it.

## v29 — what the Oct 14 review showed

- **Stream:** 5 of 17 wallets subscribed at times; 64% of whale trades came through the backup check (typical 80 min
  late). Cause: Chainstack's free plan allows 3 requests a second, the stream sent subscriptions 20 a second after each
  reconnect, and a subscribe request the provider never answered blocked that wallet forever. Fixed: subscriptions are
  paced to the provider's limit, an unanswered one is retried after 20 s, and the same wallets are subscribed on a
  second provider at once (`STREAM_PROVIDERS`). 92 of 236 late trades happened while the bot wasn't running at all —
  only an always-on server fixes those; `/health` now counts them.
- **Replays:** a price that didn't move for 10 minutes was treated as a rug even with $40K still in the pool (144 of
  859 coins). Only a thin pool (under $5K, or unknown depth) counts now. The self-tuner re-decides its rules and exit.
- **Strategy:** buying every hype alert lost money on honest fills. The autopilot now trades only setups that made
  money on both halves of the last week of replays, forward-tested on paper before real money follows (`fomo/edge.py`).
