# 🐋 FomoBot Whale Copy

Copy-trading whales into coins early — but only the whales you can actually copy at human speed.

## What v10 changed (from your data, Sep 25 – Oct 5)

- **Whale profiles.** Every whale is labelled by how fast it starts selling: 🟢 holder, 🔵 swing, 🟠 flipper,
  🤖 bot. Its trades are also replayed as if you bought a minute after it, which gives its **copy score**.
  7yuq made itself +$40K but its coins were at 0.27x an hour after it bought, so it's a 🟠 flipper. EC2f
  holds for ~11h and its coins were at 1.28x after an hour, so it's a 🟢 holder.
  **Flippers and whales that lose money themselves are tracked but never alerted.**
- **Scanner finds copyable whales by itself** (`AUTO_WHALES` on): early buyers of today's runners, replayed
  with GeckoTerminal minute candles; only holders/swing traders whose copies made money are followed.
- **Paper autopilot** trades a pretend $1,000 on every alert with the exit plan, so you see whether the
  system makes money before risking any (Telegram `/paper`, dashboard **Autopilot** tab).
- **📌 Live card**: one pinned Telegram message updated every minute: health, what was blocked and why,
  your positions with the plan, and the paper balance. Buttons for positions, whales, stats and pause.
- **📍 Tracked coins**: `/add COIN` (a coin address instead of a wallet, optionally `at 850k` for your entry)
  or the dashboard's My trades tab. You get take-profit messages for it: 2x/3x/5x/10x, gave back 35% after
  1.5x, -40%, and when your whales sell it. Coin addresses that were in your whale list are moved here.
- **🔴 Live autopilot (optional, off by default)**: makes the paper autopilot's trades for real from a separate
  wallet — see "Live autopilot" below. Starts in dry run. Sell buttons on the dashboard and `/sellnow`.
- **💤 Sleep warning**: if the Mac sleeps (lid closed, battery), the bot tells you how long it missed.
  `install_autostart.command` starts the bot by itself at login.
- **Dashboard**: overview tiles (paper balance, live status, buys sent vs blocked), Autopilot tab with the
  balance chart and live controls, tracked coins, sell/close buttons, whale style + copy score, grouped settings.
- The scanner runs every 2h over more coins (runners, your alerts that went 3x, coins 2+ whales bought,
  coins you made money on). Only one copy of the bot can run at once. Slimmer alerts.

Replaying your last 10 days with these rules ($50 a trade, 3% slippage each way): the 18 alerts v10 would
have sent averaged 1.42x, 50% won (≈ +$381). The 43 it blocks (flippers / losing whales) averaged 0.96x,
26% won. It's a small sample from two good whales, which is why the scanner keeps looking for more.

## What you get

| Message | When |
|---|---|
| 🟢/🟡/⚪️ **WHALE BUY** (grade A/B/C) | A copyable whale bought, and 45s later the price is still within −20%…+50% of its price |
| 🐋🐋 **2ND WHALE IN** | Another of your whales buys the same coin within 6h |
| ⚡ **SCALP** | Still on the pump.fun bonding curve, or a whale whose coins usually die — sell into the pump |
| 🟠 **WHALE SOLD x%** / 🔴 **WHALE EXITED** | A whale sells a coin **you hold** |
| 📈 / 🛡 / ✂️ | Your coin hit 2x/3x/5x/10x · gave back 35% after 1.5x · is 40% below your cost (once each) |
| 🚨 **LIQUIDITY PULLED** | Only when liquidity really disappears (confirmed twice) |
| 🤖 **Paper** | The paper autopilot sold (silent) |
| 📌 Live card | Pinned, refreshed every minute |

### How an alert is graded

- **Whale record** — every alert opens a simulated copy: bought at the price when the alert
  arrived, sold when the whale sells, 1% fee each way, no stop-loss. A whale's status comes from
  those copies, not from what the whale says or a guess: 🔥 HOT (copying made money consistently:
  most copies won and the median copy was positive — one or two lucky +1000% hits don't count), ✅ OK,
  🆕 NEW (under 5 copies), 〰️ WEAK, 🧊 COLD (losing — auto-muted but still scored).
- **Confluence** — two or three of your whales in the same coin.
- **Chase** — how far the price already moved since the whale's buy (LATE above 50%).
- **Safety** — live freeze authority = skipped; live mint authority, RugCheck dangers or thin
  liquidity lower the grade.
- **Community** — top-10 *wallets'* share of supply (pools excluded), trades in 24h, social links.
  STRONG lifts the grade, THIN lowers it, and WHALE-ONLY (a few wallets hold most of it, hardly
  anyone trades it) isn't sent at all by default (`HIDE_WHALE_ONLY`) but is still tracked and scored.
- **Conviction** — a buy 3× bigger than that whale's usual size.

- **⚡ SCALP** — whales whose coins don't last (60%+ of their last 5+ alerted coins were down 50% or
  more six hours later) are never graded A, and their alerts say not to hold overnight.

### What is never sent (tracked and scored, shown in /status)

- **Whale profile** (`BLOCK_FLIPPERS`): buys from 🟠 flippers / 🤖 bots, whales that lose money themselves
  (under 35% won over 6+ trades), and whales whose copies averaged under `MIN_COPY_SCORE` (0.95x over 5+
  coins) at your speed are not sent. You get one message per whale explaining why, with a Remove button.
- **Confirm price** (`CONFIRM_SECONDS` 45): the bot waits until 45s after the whale's buy and re-checks
  the price. Copy-trade bots spike these coins in the first seconds.
  - already more than `LATE_CHASE_PCT` (50%) above the whale's price → not sent (you'd be their exit);
  - already more than `DUMP_GATE_PCT` (20%) below it → not sent (usually a rug or a bot dump).
- **Bonding curve** coins are *sent*, flagged ⚡ (`/set MICRO_ALERTS off` to skip them). A good whale's 13
  curve buys averaged 1.46x: the ones that graduated ran (up to 5.7x), the rest died — you can't tell which
  at alert time, so small size and the -40% stop do the work. Earlier "micro-caps rug" results came almost
  entirely from 7yuq, which is now blocked as a flipper.

### The exit plan on every alert

"📋 Plan: sell half at 2x (~$X MC). Sell the rest if it falls 35% from its top or 40% below your entry."
The bot backs it up on coins you hold: a 🛡 warning once the coin has been 1.5x and falls 35% from its
top (`PROTECT_AFTER_X`, `PROTECT_TRAIL_PCT`), and one ✂️ warning at -40% on your cost (`STOP_LOSS_PCT`).
Every alert has a 🎯 Ping me at 2x button.

A = strong and fine to hold, B = normal or scalp, C = weak (sent silently). `/stats` shows where
each grade's coins were 6h after the alert, so you can check the grades are honest.

## Live autopilot (real money — optional)

Off unless you turn it on. When on, it makes exactly the trades the paper autopilot makes: buys
`LIVE_TRADE_SOL` (0.05) of each alert's coin, then sells half at 2x and the rest on the 35% trail, the -40%
stop, the whale selling half, or after 24h. Swaps go through Jupiter and are signed on your Mac.

1. Make a **new wallet** just for this (e.g. Phantom → Add account). Send it only what you can lose.
2. Export that wallet's private key and add one line to `.env`: `TRADING_PRIVATE_KEY=...`
   From then on never share that `.env` or a zip of the folder — anyone with it can spend from that wallet.
   (Optional: add the wallet's public address to `MY_WALLETS` so its coins show under My trades.)
3. Restart, then `/live on` (or the Autopilot tab). It starts in **dry run**: real Jupiter quotes and signed
   transactions, nothing sent — you get "Dry run: would buy …". When those look right: `/live dry off`.
4. Limits: `LIVE_MAX_OPEN` 3 coins · `LIVE_DAILY_LOSS_SOL` 0.3 (no new buys for the rest of the day) ·
   `LIVE_SLIPPAGE_BPS` 1500 · a 0.02 SOL fee reserve.

Getting out by hand: every live buy has a 🔴 Sell now button; `/sellnow COIN 50%`; `/sellall`; and on the
dashboard, Sell 50% / Sell all buttons on My trades, tracked coins and live trades, plus Close on paper trades.
These sell from the trading wallet — coins in your Fomo wallet are still sold in Fomo.
`/live off` stops new buys. Watch the paper autopilot for a few days first: if it isn't making money,
the live one won't either.

## Run it 24/7 on a free server

The Mac sleeping cost real money in your data (trades left open while it slept lost $296; watched ones made +$124).
`SERVER_SETUP.txt` walks through a free Oracle Cloud "Always Free" server (or Google Cloud's free e2-micro):
`send_to_server.command` copies the bot, your `.env` and your history there and installs it as a service that starts
on boot and restarts by itself. `open_server_dashboard.command` opens its dashboard through an SSH tunnel and
`server_log.command` shows its log. Telegram works exactly as before. If two copies ever run at once (the Mac and the
server), the bot says so and holds real-money buys until one stops.

## Trade only what's proven (v29, `/edge`)

Your Oct 14 review: 99 honest paper trades since v28.3 lost $336 (-6.8% a trade, 24% won), almost all of it on hype
alerts. Replaying all 859 coins the scanner saw that week (alerted and held back), buying when a coin was spotted lost
money in every slice — score, size, pool age, launchpad, hour — with every exit, entry delay or dip-buy tried, after a
3% cost each way. Coins drift down after they start trending; the alerted ones rugged 40% of the time, the held-back
ones 9%. Copying whale buys lost too (-13% a trade even when the buy reached the bot within a minute).

So the autopilot no longer buys every alert. Every 6 hours a fixed menu (7 coin types × 4 exit plans) is replayed on the
last 7 days of coins the scanner saw:
- **Candidate**: made money on BOTH halves of the week (12+ coins and +2% a trade on each) and without its best coin →
  the paper autopilot trades only coins that fit it, with its exit plan (a forward test), including coins a filter rule
  held back from an alert.
- **Confirmed**: the candidate's own paper trades since then made money (15+ trades, +2% a trade) → only then may the
  live autopilot follow it.
- Nothing passing → the autopilot stands aside; hype alerts still arrive, silent and marked "no setup has been making
  money" (`EDGE_QUIET_ALERTS`). A candidate whose paper trades lose is benched for 3 days.

`EDGE_GATE 0` brings back "trade every alert". `/edge` and the Hype tab show every setup's numbers. On your Oct 14 data
one candidate passed (pools 1h+ old with liquidity ≥ half the market cap, half out at +30%) — but on 58 coins, resting on
2 big winners: exactly the kind of result that needs the forward test before money follows.

**Also in v29:** the live wallet stream runs on two providers at once (`STREAM_PROVIDERS`, e.g. Chainstack + Alchemy;
your health showed 5 of 17 wallets subscribed and 64% of whale trades arriving through the slow backup check);
subscriptions a provider never answers are retried instead of blocking that wallet forever, and are paced to the plan's
limit; a frozen price in a pool that still holds real money is no longer counted as a rug (that had skewed the
self-tuner and the market pulse); `/health` counts whale trades that happened while the bot wasn't running (92 of 236
late ones — only an always-on server catches those). A new paper period starts with v29.

## Production hardening (v28)

`HARDENING.md` is the full audit — ingestion, scam filters, execution & MEV, sizing & risk, exits, concurrency and
telemetry — with what your data showed and what changed. In short: pump.fun whale buys are read straight from the
stream's logs (no RPC wait); the stream re-subscribes wallets it misses; Token-2022 trap coins are blocked; every real
buy first checks it can be sold back (honeypot / tax); swaps go privately through Jito with slippage sized to the
trade; buys halt after repeated failures or when the RPC/stream is down; size follows signal strength and pool depth;
a pulled pool triggers an emergency exit; late-landing sells are booked instead of sold twice; crashed loops restart
by themselves; `/health` shows all of it.

## Market pulse (`/market`, the Market tab)

Memecoins move as a crowd, so the bot reads the market every 15 minutes and calls it 🔥 hot · 🟢 healthy · 🟡 choppy ·
🧊 cold, with the reasons and what to look out for. Inputs: SOL/BTC 24h (CoinGecko), the Crypto Fear & Greed index,
Jupiter's 100 most traded tokens (how many memecoins are up on the day), new pools per hour, and what the bot itself saw
— how many hype coins **really** doubled (2x while the pool still had money to sell into; a price printed into an
emptied pool doesn't count), how many were rugs, how much your whales bought, paper results. Everything the bot measures
is per hour it was RUNNING, so a sleeping Mac or a dead RPC never reads as a crash, and a whale count only counts while
the whale feed was up. It also lists rising themes (Chinese/Korean-name coins, AI, animals…) and words, launchpads
whose coins rug most, and the hours when real pumps were alerted.

A brief arrives every morning at `MARKET_BRIEF_HOUR` (9; −1 turns it off), a note when the market turns, and a
"🌡 Market" line on hype alerts and the live card. With `MARKET_GUARD` on (default) the live autopilot opens no new real
trades in a cold market — paper keeps going so the numbers keep coming. Day-by-day history works from your existing
data; SOL, Fear & Greed and the Jupiter board fill in from the first run of 28.4.

## Free RPC instead of Helius

Paste any provider's link into `SOLANA_RPC_HTTP=` (Chainstack, Alchemy, dRPC, QuickNode…) — `FREE_RPC.txt` shows the
3-minute Chainstack setup. The websocket is worked out from the link, Helius (if set) becomes the backup, and a second
link in `SOLANA_RPC_FALLBACKS=` takes the background work so the first plan's allowance lasts for live whale trades.
A provider that says its limit is used up (or refuses the key) is rested for 30 minutes and the next one takes over.

## Jupiter token data (free)

Jupiter's trending and top-organic lists feed the hype scanner, and for the busiest 100 coins each minute the bot reads
Jupiter's organic score (how much of the trading is real people vs bots / wash trading), organic buyers, the dev's
mint count and Jupiter's audit flag: 🌿 real buyers (+10), 🤖 bot volume (−20), 🏭 serial dev (shown, 0 — it's on most coins), ⚠️ suspicious
(−25). "Which signals have worked" on the Hype tab measures them like every other signal.

## Self-tuning hype filter (`/tuning`)

Each hype rule (the $30K–100K zone, paid profiles, "already up too much this hour", the score threshold) is measured on
the coins it BLOCKED: a coin that fails only one of them is priced for two hours and replayed with the exit plan, the
same way real alerts are. Every 6 hours, over 14 days: a rule whose blocked coins would have made money (12+ coins,
+3% a trade, half won) is loosened a step; if what it then lets in loses, it's tightened back. The exit plan is replayed
on every hype alert under 36 take-profit / stop / time-limit combinations and replaced when another one is clearly
better. One change per setting every 3 days, fixed bounds, a Telegram note for each change; a setting you `/set`
yourself is left alone for a week. `HYPE_AUTOTUNE 0` keeps the measuring and stops the changes.

## Real fills vs paper

Every closed live trade is compared with the paper trade it copied (`/live`, dashboard): real vs paper multiple, what
real swaps cost each way, how many seconds after paper it bought. After 10 real trades the paper autopilot's slippage
is set to the measured real cost, so paper results — and the self-tuner — predict what real money gets.

## Start automatically

Double-click `install_autostart.command` once and the bot opens by itself every time you log in.
`remove_autostart.command` undoes it. Only one copy can run at a time, so a double-click won't double-alert.
It still can't run while the Mac sleeps — keep the lid open and the charger in.

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

## Whale scanner (finds whales by itself)

Every `AUTO_SCOUT_HOURS` (4) the bot takes today's biggest runners and coins your alerts caught that
went 3x+, finds their early buyers (no snipers, creators or bots), and **replays each candidate's recent
trades as a copier who buys a minute late** (GeckoTerminal minute candles, exit plan, fees). It follows
(`AUTO_WHALES` on, up to `AUTO_WHALE_LIMIT`) only 🟢 holders / 🔵 swing traders that are profitable
themselves and whose copies averaged 1.10x+ with 35%+ won over 4+ coins; ones without enough price
history are sent to you as picks with ➕ Follow. Earlier auto-follow picked wallets by their *own*
profit, which happily picked flippers — that's what made it worse, and it's what the replay fixes.
Every day it drops auto-followed whales that turned into flippers, stopped being copyable, went COLD or
went quiet. Whales you added yourself are never dropped. `/scout now` runs it on demand.

## Extra signals around your entries

- **Whale form** on every buy alert: that whale's last 5 calls (🟩+120% · 🟥-30% …).
- **➕ Whale added more**: when a whale you follow buys more of a coin you hold — a hold signal.
- **⚠️ Whale-exit re-buy warning**: buying a coin after every tracked whale has sold. In real data
  those buys lost a median -24% (6 of 19 won) vs +3.8% while a whale still held.
- **Bundle groups**: wallets that keep buying the same coins within a minute of each other count as
  one whale, so they can't fake a "2ND/3RD WHALE IN".
- **⚡ Fast-flipper warning**: whales that usually start selling within ~10 minutes are flagged on
  the alert so you take profit quickly.
- **🎯 /watch COIN 2x** or **/watch COIN 1.5m**: your own market-cap targets, pinged once when hit
  (works downwards too). `/watches`, `/unwatch N`.

## Selling: too early vs too late

- **Exit lab** (`/exits`, dashboard *Exit lab*): replays your whales' last 30 days of alerts with
  their recorded price paths under five exit styles (sell with the whale, all at 2x, half at 2x,
  trail 35% after 2x, ladder ⅓-⅓-trail) and shows which actually made the most.
- **Your habits**: for each of your sells, how much higher the coin went in the next 24h (too
  early) and how far below your best price you sold (too late), with a plain recommendation.
- **Ladder nudges** (`PROFIT_LADDER`): one message at 2x, 3x, 5x, 10x on your cost — "take about a
  third, let the rest ride while the whales hold."
- **Profit protector** (`PROTECT_AFTER_X` 1.5, `PROTECT_TRAIL_PCT` 35): once a coin has been 1.5x+,
  one warning if it gives back 35% from its peak (confirmed twice).
- **Stop warning** (`STOP_LOSS_PCT` 40): one warning if a coin you hold is 40% below your cost.
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
/paper                      paper autopilot trades and balance
/market                     what the market is doing, what's trending, what to look out for
/edge                       which setups made money after costs lately — the autopilot trades only those
/health · /tuning           stream, RPCs, live safety · what the self-tuner measured and changed
/card · /export             re-pin the live card · send a review file (no keys inside)
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
- The v10 numbers come from 10 days and mostly two whales (EC2f, 6qRT). Watch the paper autopilot
  for a few days before sizing up; it uses the same rules and is honest about slippage.
- The bot can't trade while your Mac sleeps. Lid open + charger in, or run it on an always-on machine.
- The public Solana RPC is slow and rate-limited; a free Helius key makes alerts arrive in a few
  seconds and makes `/find` practical.
- `/find` samples a coin's history within a request budget. For coins with millions of trades it
  may not reach the launch; it says so.

## Files

`fomo/` the bot · `fomo/web/` dashboard page · `browser_extension/` Chrome add-on · `tests/`
(run `python3 -m pytest tests`) · `legacy/` the previous bot, untouched.
History lives in `~/Library/Application Support/FomoBot/whales.db` (on a Linux server `~/.local/share/FomoBot/`)
so updates never lose it; a copy is kept for each of the last 3 days in its `backups/` folder.
`server/install.sh` installs the bot as a systemd service on any Linux server.
