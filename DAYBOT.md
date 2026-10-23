# 📈 DayBot — your day-trading assistant (stocks + options)

DayBot sits next to your Robinhood and Chase accounts and does the things a good trading partner would:

- **Checks every trade you make, the moment it fills.** It gives a 🔴 / 🟡 / 🟢 verdict with a stop, targets and a size check. It catches chasing, fighting VWAP, revenge trades, the daily loss limit, oversized trades, wide option spreads, thin open interest and late-day 0DTE.
- **Finds what's in play before you'd see it.** It builds a gapper list with the headline behind each move, and every 10 minutes during the session it flags new stocks waking up on heavy volume.
- **Watches for setups with a measured record.** The day's **stocks in play** (the busiest stocks at the open, with a buy-stop at the first 5-minute high), **SPY / QQQ momentum** (breaking out of the day's usual range), opening-range breakouts, VWAP reclaims, gap-and-go, and the *second leg* (pump → orderly pullback → next leg). Every alert comes with a chart, entry, stop, exit plan, share size for your cash, and a defined-risk option idea.
- **Reads charts.** Send it a screenshot (Robinhood, TradingView, anything) and Claude reads it. The bot adds its own numbers for that ticker.
- **Keeps your journal and tells you where the money leaks.** Losses get grouped by time of day, 0DTE, chasing, revenge, held past the stop, own ideas vs. setups, and so on. Each leak is ranked in dollars.
- **Answers questions from your live data:** `/ask should I hold AMD into the close?`
- **🤖 Autopilot: proves it with pretend money.** A pretend $1,000 account takes every trade the bot alerts you on, by itself, with the alert's plan and real costs, and tells you each buy and sell. After a couple of weeks you know in dollars whether following DayBot makes money, which setups do, and at what time of day, before you risk yours.

It **never places a trade.** Market data is read-only, and your broker link is read-only.

---

## What it costs

| Piece | What for | Cost |
|---|---|---|
| Alpaca | prices, charts, history, option quotes | **free** |
| SnapTrade | reading your Robinhood / Chase trades | **free** (personal use) |
| Telegram | messages and commands | free |
| Claude (optional) | chart screenshots, /ask, coach notes | pay as you go, a few cents per chart read |
| Finnhub (optional) | earnings dates | free |
| Alpaca Algo Trader Plus (optional, later) | full-market real-time data | $99/month — not needed to start |

**Free data, honestly:**
- Real-time prices come from IEX, one exchange. That's fine for prices and patterns, but IEX volume is only a slice of the market. The bot always compares IEX volume with past IEX volume, so relative volume still works.
- The full market, including the premarket, is 15 minutes late. That's why the gapper scan is complete by about 9:15 and the premarket high is exact from 9:46.
- Free option quotes are "indicative" (estimates). Alerts and checks say so: confirm the real bid/ask in your app before you buy.

---

## Setup (about 10 minutes)

1. **Alpaca:** at [alpaca.markets](https://alpaca.markets), sign up, switch to *Paper Trading*, open *API Keys* and click *Generate*. DayBot only reads prices with it.
2. **SnapTrade:** at [dashboard.snaptrade.com](https://dashboard.snaptrade.com), sign up and verify your email. Create your *Personal* key (a *Client ID* and a *Consumer Key*, and save the Consumer Key because it's shown once), then connect Robinhood and Chase right there, read-only. You can also do this later with `/connect`.
3. **A new Telegram bot for DayBot:** in Telegram, open @BotFather, send `/newbot`, copy the token, then open your new bot and press *Start*. It needs its own bot because two programs can't both read commands from one.
4. *(optional)* **Claude:** at [console.anthropic.com](https://console.anthropic.com), open *API keys* and create one. It needs Python 3.10+; the start script finds it if you install Python from python.org.
5. *(optional)* **Finnhub:** get a free key at [finnhub.io](https://finnhub.io) for earnings dates.
6. Unzip the new version **next to your current bot folder** (same parent folder), so it can copy FomoBot's Telegram and wallet settings over. Then double-click **`start_daybot.command`**. The first time, it asks for the keys above and saves them in `.env`, the same file FomoBot uses. If macOS blocks it, right-click it, choose *Open*, then *Open* again.
7. In Telegram, send **`/connect`** to DayBot. Log in to Robinhood (you'll get a text code) and Chase, read-only. Then send `/accounts`.

To add or change a key later (say you skipped Claude), double-click **`daybot_setup.command`**. Keys you already entered show as *saved*, and Enter keeps them.

In Telegram, `/status` shows what's connected.

**First run:** the bot backtests its setups on the last 60 trading days, which takes a few minutes. Until a setup proves itself, its alerts come silently and are labelled 🧪 *unproven*.

---

## Your day with DayBot (New York time)

| When | What you get |
|---|---|
| 7:00–9:30 | Gapper scan every 10 minutes |
| **9:00** | 🌅 **Game plan:** market tone, which setups the data backs, gappers with headline, levels and an if-then plan, your positions, and your rules in dollars |
| **9:35** | 🔥 **Stocks in play:** the day's busiest stocks, each with a buy-stop and a stop (one message) |
| 9:30–4:00 | ✅ proven setups ping you, 🧪 unproven ones arrive silently, 🆕 in-play stocks, 🔔 your price alerts |
| any time you trade | 🧾 a check of your fill, a ✂️ note on trims, and a ✅/❌ recap on exits with what the stock did after you left |
| while you hold | ⚠️ stop hit, 🎯 targets, 🟡 VWAP lost, ⏰ 15 minutes to the close, ⏰ a 0DTE running out of time |
| 9:30–3:55 | 🤖 the autopilot buys and sells the alerts on pretend money (silent messages) |
| **4:10** | 📒 **Day report:** P&L, rules broken, how the bot's setups did on paper (the market gauge), the autopilot's day, tomorrow's focus |
| Friday 4:20 | 📒 Week report |
| Saturday | 🧪 Fresh backtest (and after the close on any day the setups changed) |

---

## Reading a trade check

```
🧾 You bought 60 GAPR @ $107.50 · 🔴 AVOID
GAPR $107.50 · +7.5% today · VWAP $106.72 · vol 4.1×
🔴 Revenge trade? You took a loss on GAPR 5 min ago
🟢 Above VWAP $106.72, not stretched
🟢 In play: 4.1× normal volume
🟢 Size OK: $49.28 at risk (0.2% of the account)
🎯 Plan: stop $106.68 on GAPR (just past VWAP $106.72) · take half $108.32 · rest $109.14 · out by 3:55pm
```

- 🔴 **AVOID:** breaks one of your rules, or it's the kind of trade that loses money (a dilution headline, an untradeable option, a late 0DTE…).
- 🟡 **CAREFUL:** a risk you can manage with size and the plan.
- 🟢 **CLEAN:** none of the traps the bot can see. That isn't a promise it works.

Options checks also look at the bid/ask spread, open interest, delta (lottery tickets), the move needed to break even versus what the market expects, time decay per day, IV, and earnings before expiry.

---

## The setups, and why you can trust the labels

| Setup | What it is | Exit |
|---|---|---|
| **In-play 5-min breakout** | Only the ~20 stocks trading far more than usual in their first 5 minutes (against their own last 14 days). A green first 5-minute candle sets a buy-stop at its high; the stop is 10% of the stock's daily range. From Zarattini, Barbon & Aziz (2024), *A Profitable Day Trading Strategy for the U.S. Equity Market*. | No target: held to 3:55pm unless stopped. Most trades lose a little; a few run all day. |
| **Index momentum (noise band)** | SPY / QQQ, checked every half hour: buy when it's further from the open than it usually gets by that time of day (the "noise band", from the last 14 days) and above VWAP. From Zarattini, Aziz & Barbon (2024), *Beat the Market*. | Out when a half-hour close falls back inside the band or under VWAP, or at 3:55pm. |
| Opening-range breakout | First close beyond the first 15 minutes' range, with VWAP and volume behind it |
| VWAP reclaim / rejection | A stock on the right side of yesterday's close dips through VWAP and takes it back on volume | Half at 1R, stop to break-even, rest at 2R |
| Gap-and-go | A gapper's first close through its premarket high (or low), in the first hour, in play | Half at 1R, stop to break-even, rest at 2R |
| **Second leg** (bull/bear flag) | A strong first leg, an orderly pullback on lighter volume that holds VWAP, then the break of the flag | Half at 1R, stop to break-even, rest at 2R |

The opening-range breakout uses the same 1R / 2R plan. Everything is out by 3:55pm.

**✅ Proven** means it made money after costs in **both halves** of the backtest, still did **without its 3 best trades**, and has **40+ trades**. The backtest replays the last **120 trading days**: the core liquid names every day, every stock on the days it gapped, each day's 20 stocks in play (picked the same way as live, traded on 1-minute bars), and SPY / QQQ with the noise band rebuilt for each day. Costs are charged on every buy and sell. If a bar touches both the stop and the target, the stop is assumed to come first, and a buy-stop filled in a minute that also touches the stop counts as stopped out. The dashboard's **Setups** tab shows each setup's backtest trade by trade, and a plain verdict on top.

The short side of every setup is measured too, but stays on paper unless `SHORT_ALERTS` is on (Robinhood and Chase don't let you short stocks).

Every signal, alerted or not, is also **paper-traded** (`/setups`). If a proven setup starts losing on paper, it's **benched** and goes silent again. A setup that **wins on paper by the same standard** (30+ trades, +0.12R or better on average, positive in both halves and without its 3 best) is promoted to **✅ proven live**, even if the backtest didn't prove it. Setups that lose money in the backtest are never sent, but paper keeps measuring them, so they can earn their way back.

---

## 🤖 The autopilot

A pretend account (`AUTOPILOT_START`, $1,000 to begin with) that follows every alert the bot sends you, like FomoBot's paper autopilot:

- **Buys** at the next minute's price after the alert, plus costs (`SLIPPAGE_PCT` each way). For stocks in play it places the buy-stop and only buys if the price breaks the first 5-minute high (cancelled at 3pm).
- **Sizes** like a real small account: 1% of its balance at risk at the stop, at most a third of the balance in one trade (so it can hold 3 at once), never more cash than it has, fractional shares when a whole one doesn't fit.
- **Sells** by each setup's plan: stocks in play hold to 3:55pm unless stopped; SPY / QQQ band trades get out at the first half-hour close back inside the band; the others sell half at the first target, move the stop to breakeven and sell the rest at the second. No new trades in the last 30 minutes.
- Tells you each buy and close in Telegram (silent). `/autopilot` and the dashboard's Autopilot tab show the balance against simply holding SPY, results by setup and by time of day, and a plain verdict once there are 20+ trades.
- `/autopilot reset 500` starts over with $500. `/autopilot off` stops it. It never touches a real account.

Read it like this: if it's up after 20+ trades, the alerts are worth following, especially the setups marked ✅ on its page. If it's down, don't follow them blindly; follow only the setups that are green there, or none.

`/setups` shows each one's record. `/backtest` re-runs the test any time.

---

## Commands

| Command | |
|---|---|
| `NVDA`, `NVDA 140c 10/24`, `TSLA short 50 @250` | check a trade (just paste it) |
| 📷 *send a screenshot* | Claude reads the chart (add a question as the caption) |
| `/chart NVDA [1\|5\|15]` | today's chart with VWAP and levels |
| `/plan` `/gappers` `/alerts` | the game plan, gappers, today's setups |
| `/watch NVDA AMD` · `/watch -NVDA` | stocks always watched |
| `/alert NVDA 141.50 note` · `/alert` · `/alert -3` | price alerts |
| `/trades` `/positions` `/accounts` | your day, holdings, balances · `/accounts off 2`: an investing account stays synced but isn't day-trade checked |
| `/journal [days]` `/day` `/week` | where the money leaks, reports |
| `/autopilot` · `/autopilot reset 1000` · `/autopilot off` | the pretend account that follows every alert |
| `/setups` `/backtest` | what the setups have earned |
| `/ask …` or end a message with `?` | the assistant, using your live data |
| `/rules` · `/set NAME value` | your rules and settings |
| `/pause` `/resume` · `/connect` · `/status` · `/export` | |

Dashboard: **http://localhost:8788** (only on your computer). Tabs: **Today** (your numbers, a trade check box, alerts, open trades), **Autopilot**, **Market** (gappers, watchlist, click any ticker for its chart), **My trades** (journal, leaks, holdings, accounts), **Setups**, **Settings**.

---

## Your rules (change with `/set`)

| Setting | Default | |
|---|---|---|
| `RISK_PER_TRADE_PCT` | 1 | most you mean to lose on one trade, as % of the account |
| `DAILY_MAX_LOSS_PCT` | 3 | done for the day at this loss |
| `MAX_TRADES_PER_DAY` | 6 | past this, it's overtrading |
| `LOSS_STREAK_BREAK` / `BREAK_MINUTES` | 2 / 30 | after 2 losses in a row, take a 30-minute break |
| `REVENGE_MINUTES` | 15 | re-entering a loser this soon is flagged |
| `OPTIONS_MAX_PREMIUM_PCT` | 5 | most premium in one options trade |
| `ALLOW_0DTE` | 0 | same-day expiry flagged (red after 2:30pm) |
| `ACCOUNT_SIZE` | 0 | 0 = read from your broker |
| `SHORT_ALERTS` | 0 | short setups stay on paper: Robinhood and Chase don't let you short stocks |
| `AUTOPILOT` / `AUTOPILOT_START` | 1 / 1000 | the pretend account that follows the alerts |
| `ORB5_TOP` | 20 | how many stocks in play the in-play breakout trades each day |

**Sizes fit your cash.** Alerts and checks size by your risk rule, then cap it at the cash you can actually buy with (the smaller of cash and buying power, so margin is never counted as your money). When a whole share doesn't fit, you get a fractional size (Robinhood and Chase both sell fractional shares of most stocks).

`/rules` lists everything else (scanner filters, option filters, alert limits…).

---

## Good to know

- **The $25K day-trading rule is gone.** FINRA replaced the pattern-day-trader rule with intraday margin standards on June 4, 2026, and Robinhood removed its day-trade limits on margin accounts. Brokers have until Oct 20, 2027 to switch over, so check your own broker. DayBot doesn't count day trades. It watches your size and your losses instead.
- **Robinhood asks you to log in again every so often.** When the link breaks, DayBot tells you, and `/connect` fixes it.
- **Crypto, cash sweeps and funds aren't day trades.** They're left out of checks and the journal. Leveraged and inverse ETFs (TQQQ, SOXL…) are left out of the gapper list: they "gap" every day only because the index moved.
- **Cash accounts (like Chase) settle the next business day.** Money from a sale is usable to buy right away, but sell what you bought with it before it settles and you get a good-faith violation (three in a year restricts the account). With a small cash account, one round trip a day is the safe pace.
- **Selling a holding from before DayBot** (stock you've had for months) is recognised as such: priced against your average cost, never mistaken for a short, and kept out of the day-trading journal.
- **Fills arrive in seconds to a minute** through SnapTrade. The very first sync books your existing positions quietly. A fill DayBot only sees well after the fact (it was off) is booked and recapped, but not "checked".
- **A backtest is not the future.** The labels say what the data showed after costs. Paper keeps measuring, and sizes stay your call.

## Running it on your server

`send_to_server.command` installs DayBot next to FomoBot once your Alpaca keys are in `.env`. It runs as its own service (`daybot`). `open_server_dashboard.command` opens both dashboards: DayBot's is at http://localhost:8798.
