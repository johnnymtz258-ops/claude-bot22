# 🐋 FomoBot Whale Copy

A rebuild of FomoBot around the one thing that has actually made you money: **following whales
into coins early**. It watches the wallets you choose around the clock and tells you, within
seconds, when one of them buys, how that whale's picks have really performed, and when the
whale sells.

The old scanner (400+ settings, 15 alert types, sell alarms on every dip) is in `legacy/` and no
longer runs. Its own two-month review found it had no edge. Your manual whale copying did.

## What you get

| Message | When |
|---|---|
| 🟢/🟡/⚪️ **WHALE BUY** (grade A/B/C) | A whale you follow buys a coin: entry market cap, how far price has moved since, the whale's measured record, safety checks |
| 🐋🐋 **2ND WHALE IN** | Another of your whales buys the same coin within 6h — the strongest signal |
| 📈 **COMMUNITY RUNNER** | No whale, but 2,000+ trades a day, heavy buying, 2+ social links and the top-10 wallets hold under 30%. Measured separately (scored as sold after 24h) |
| 🟠 **WHALE SOLD x%** / 🔴 **WHALE EXITED** | A whale sells a coin **you hold**. Partial trims are labeled as trims |
| 🚨 **LIQUIDITY PULLED** | Only when liquidity really disappears (confirmed twice, and far more than a dip explains) |
| 🌙 Daily summary | Once a day, silent |

**There are no "price dropped, sell now" messages.** Meme coins often dip -30% before the real
run; the exit signal is the whale leaving, not a red candle.

### How an alert is graded

- **Whale record** — every alert opens a simulated copy: bought at the price when the alert
  arrived, sold when the whale sells, 1% fee each way, no stop-loss. A whale's status comes from
  those copies, not from what the whale says or a guess: 🔥 HOT (copying made money), ✅ OK,
  🆕 NEW (under 5 copies), 〰️ WEAK, 🧊 COLD (losing — auto-muted but still scored).
- **Confluence** — two or three of your whales in the same coin.
- **Chase** — how far the price already moved since the whale's buy (LATE above 50%).
- **Safety** — live freeze authority = skipped; live mint authority, RugCheck dangers or thin
  liquidity lower the grade.
- **Community** — top-10 *wallets'* share of supply (pools excluded), trades in 24h, social links.
  STRONG lifts the grade, THIN lowers it, and WHALE-ONLY (a few wallets hold most of it, hardly
  anyone trades it) isn't sent at all by default (`HIDE_WHALE_ONLY`) but is still tracked and scored.
- **Conviction** — a buy 3× bigger than that whale's usual size.

A = strong, B = normal, C = weak (sent silently). `/stats` shows whether A really beats C for you.

## Setup (Mac)

1. Put your `.env` in this folder (or let the launcher copy Telegram settings from your old bot
   folder next to it). Only three lines matter:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   HELIUS_API_KEY=...          # free at helius.dev — faster alerts, needed for quick /find
   MY_WALLETS=your_public_wallet_address   # optional, for exact profit/loss
   ```
   Never put a private key or seed phrase anywhere. The bot only needs public addresses.
2. Double-click `start_mac.command`.
3. Open **http://localhost:8787** for the dashboard, or send `/help` to your bot.

Whales you tracked in the old bot are imported automatically on first start.

## Finding whales

- `/find COIN` — for a coin that already ran (like CASHED), reads its trade history, finds the
  peak, and lists the wallets that bought at **a third of the peak or lower**, with how much they
  put in and whether they still hold. Launch-block snipers, the creator and bot-like wallets are
  flagged. Give it 2–4 winners (`/find COIN1 COIN2`) and wallets that were early in **more than
  one** are listed first — one lucky call is common, repeats are rare.
- `/analyze WALLET` — reads a wallet's last ~200 swaps and rebuilds its round trips: win rate,
  profit in SOL, median hold time, typical entry market cap. Verdict: ✅ copyable, 🆕 not enough
  data, ❌ losing, 🤖 too fast to copy by hand.
- `/suggest` — runs `/find` on today's biggest runners (2x+ in 24h with real liquidity). The
  quickest way to grow your whale list — more good whales means more alerts.
- Tap **➕ Follow** under either result, or use the dashboard's *Find whales* tab.

## Whale autopilot

Every few hours (`AUTO_SCOUT_HOURS`, default 6) the bot researches today's biggest runners and
coins your alerts caught that went 3x+, reads their early buyers' recent trading, and **follows
only wallets that are profitable right now**: 6+ closed trades, +1 SOL or more, 45%+ won, active
in the last 2 days, not a sniper or bot. Once a day it **drops its own picks** that turned COLD,
went quiet for 4 days, or stopped being profitable. Whales you add yourself are never dropped
(adding an auto-picked whale yourself makes it yours). Up to `AUTO_WHALE_LIMIT` (15) at a time.
Every follow/drop is reported with the reason. `/scout` shows it; `/scout now` runs it.
Uses a few thousand Helius requests per run — fine on the free plan.

## Selling: too early vs too late

- **Exit lab** (`/exits`, dashboard *Exit lab*): replays your whales' last 30 days of alerts with
  their recorded price paths under five exit styles (sell with the whale, all at 2x, half at 2x,
  trail 35% after 2x, ladder ⅓-⅓-trail) and shows which actually made the most.
- **Your habits**: for each of your sells, how much higher the coin went in the next 24h (too
  early) and how far below your best price you sold (too late), with a plain recommendation.
- **Ladder nudges** (`PROFIT_LADDER`): one message at 2x, 3x, 5x, 10x on your cost — "take about a
  third, let the rest ride while the whales hold."
- **Profit protector** (`PROTECT_AFTER_X` 2, `PROTECT_TRAIL_PCT` 35): once a coin has been 2x+,
  one warning if it gives back 35% from its peak (confirmed twice). It never fires before 2x, so
  early dips still don't trigger anything.
- The dashboard's **exit coach** column shows each position's peak and what the ladder/protector says,
  and each coin's chart marks every whale and your own buy/sell on the price line.

## Your profit/loss

- **With `MY_WALLETS` set** every buy and sell in your wallet is read from the chain. Costs and
  proceeds are exact, including every fee you paid. The ~0.002 SOL token-account deposit is *not*
  counted as a loss (you get it back when the account closes; on a $5 trade it used to look like -8%).
- **Without it**, reply to an alert with `/bought 20` or `/bought 20 at 850k` (the market cap your
  app showed), then `/sold 5`, `/sold 30%` or `/sold all`. Typing the market cap makes the numbers
  match your app exactly. No invented fees are subtracted — a flat coin shows $0, not a loss.

## Dashboard and browser extension

The dashboard (http://localhost:8787, this computer only) has: live whale buys, coins with several
whales, your whales' measured records, your positions with which whales are still in, a
realized-profit chart, copy results by alert grade, whale discovery, and settings.

The **browser extension** adds a small panel to DexScreener, GMGN, Axiom, Photon, pump.fun,
BullX, Birdeye and Solscan. On a coin it shows which of your whales are in, their entry market
cap and whether they still hold; on a wallet it shows its record or a **Follow** button.
Install: Chrome → `chrome://extensions` → turn on *Developer mode* → *Load unpacked* → pick the
`browser_extension` folder. It only talks to the bot on your own computer.

## Commands

```
/whales                     who you follow + copy results
/add WALLET name            follow a whale        /remove name · /mute name · /unmute name
/whale name                 one whale's record    /analyze WALLET   check before following
/find COIN [COIN2 …]        early buyers of coins that ran
/recent · /hot · /coin COIN latest whale buys · coins with 2+ whales · who's in a coin
/bought 20 (at 850k)        /sold 5 | 30% | all (at 1.2m) · /positions · /undo
/stats · /status            what copying returned · health
/settings · /set NAME VALUE · /pause · /resume
```

Paste any coin or wallet address into the chat to look it up.

## Settings

All in `.env` with defaults; change live with `/set` or on the dashboard. The useful ones:
`MIN_WHALE_BUY_USD` (100), `MAX_ENTRY_MC_USD` (30M), `LATE_CHASE_PCT` (50), `CONFLUENCE_HOURS` (6),
`MIN_SELL_ALERT_PCT` (25), `QUIET_LOW_GRADE` (on), `AUTO_MUTE_COLD_WHALES` (on),
`TAKE_INITIAL_AT_X` (0 = off; e.g. 2 sends one "take your initial out" note at 2x).

## Honest limits

- Copying a whale can still lose: you enter after them, they may size differently, hedge
  elsewhere, or change style. That's why every whale's *copy* result is measured instead of
  trusted, and cold whales are muted.
- The public Solana RPC is slow and rate-limited; a free Helius key makes alerts arrive in a few
  seconds and makes `/find` practical.
- `/find` samples a coin's history within a request budget. For coins with millions of trades it
  may not reach the launch; it says so.

## Files

`fomo/` the bot · `fomo/web/` dashboard page · `browser_extension/` Chrome add-on · `tests/`
(run `python3 -m pytest tests`) · `legacy/` the previous bot, untouched.
History lives in `~/Library/Application Support/FomoBot/whales.db` so updates never lose it.
