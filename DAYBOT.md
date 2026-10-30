# 📈 DayBot — your trading assistant (stocks, options and crypto)

DayBot sits next to your Robinhood and Chase accounts and does the things a good trading partner would:

- **Checks every trade you make, the moment it fills.** It gives a 🔴 / 🟡 / 🟢 verdict with a stop, targets and a size check. It catches chasing, fighting VWAP, revenge trades, the daily loss limit, oversized trades, wide option spreads, thin open interest and late-day 0DTE.
- **Finds what's in play before you'd see it.** It builds a gapper list with the headline behind each move, and every 10 minutes during the session it flags new stocks waking up on heavy volume.
- **Watches for setups with a measured record.** The day's **stocks in play** (the busiest stocks at the open, with a buy-stop at the first 5-minute high), **SPY / QQQ momentum** (breaking out of the day's usual range), opening-range breakouts, VWAP reclaims, gap-and-go, and the *second leg* (pump → orderly pullback → next leg). Every alert comes with a chart, entry, stop, exit plan, share size for your cash, and a defined-risk option idea.
- **Reads charts.** Send it a screenshot (Robinhood, TradingView, anything) and Claude reads it. The bot adds its own numbers for that ticker.
- **Keeps your journal and tells you where the money leaks.** Losses get grouped by time of day, 0DTE, chasing, revenge, held past the stop, own ideas vs. setups, and so on. Each leak is ranked in dollars.
- **Answers questions from your live data:** `/ask should I hold AMD into the close?`
- **📋 Tells you what to do today — or honestly that there's nothing to do.** One view (`/today`, top of the dashboard) pulls every part into actions, each with a trust level from its own test, and what each part is worth in dollars on the money you actually put in it.
- **🧺 Invest lab.** Slow, well-studied ETF plans (trend filters, dual momentum, sector rotation…) tested on 10 years of prices against simply holding SPY, with what $50 a month would have become. It tells you when your plan changes. If nothing beats holding, it says "keep buying a broad index fund".
- **🪙 Crypto trend plan.** Once a day it says which of the biggest coins Robinhood sells to hold (the ones trending up) and which to sell. A pretend crypto account follows it. Honest numbers below: it trails Bitcoin in a bull market and falls far less in a crash.
- **⏱ BTC 15-minute Up or Down.** The odds Bitcoin finishes the current quarter hour up or down, compared with the real Polymarket / Kalshi prices, with a running record of whether the cheap-side signal actually pays after fees.
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
| **3:30** | 🔄 **End-of-day bounce:** the day's biggest losers, to hold into the close (one message) |
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
| **In-play 5-min breakout** | Only the ~20 stocks, out of the whole market, trading far more than usual in their first 5 minutes (against their own last 14 days). A green first 5-minute candle sets a buy-stop at its high; the stop is 10% of the stock's daily range. From Zarattini, Barbon & Aziz (2024), *A Profitable Day Trading Strategy for the U.S. Equity Market*. | No target: held to 3:55pm unless stopped. Most trades lose a little; a few run all day. |
| **Index momentum (noise band)** | SPY / QQQ, checked every half hour: buy when it's further from the open than it usually gets by that time of day (the "noise band", from the last 14 days) and above VWAP. From Zarattini, Aziz & Barbon (2024), *Beat the Market*. | Out when a half-hour close falls back inside the band or under VWAP, or at 3:55pm. |
| **End-of-day bounce** | At 3:30pm, the bottom tenth of the liquid universe by return since yesterday's close: the day's biggest losers tend to bounce in the last half hour. From Baltussen, Da & Soebhag, *End-of-Day Reversal* (2024; 2nd place, Quantpedia Awards 2025). The edge is a few hundredths of a percent, so trading costs decide whether it pays. | No stop: sold at 3:59pm. |
| Opening-range breakout | First close beyond the first 15 minutes' range, with VWAP and volume behind it |
| VWAP reclaim / rejection | A stock on the right side of yesterday's close dips through VWAP and takes it back on volume | Half at 1R, stop to break-even, rest at 2R |
| Gap-and-go | A gapper's first close through its premarket high (or low), in the first hour, in play | Half at 1R, stop to break-even, rest at 2R |
| **Second leg** (bull/bear flag) | A strong first leg, an orderly pullback on lighter volume that holds VWAP, then the break of the flag | Half at 1R, stop to break-even, rest at 2R |

The opening-range breakout uses the same 1R / 2R plan. Everything is out by 3:55pm.

**How a setup earns a label.** The backtest replays the last **120 trading days**: the core liquid names every day, every stock in the **whole market** (every listed US stock that trades about a million shares a day — around 2,000) on the days it gapped, each day's 20 stocks in play (picked the same way as live, traded on 1-minute bars), SPY / QQQ with the noise band rebuilt for each day, and each day's 3:30pm losers and winners for the end-of-day bounce. Costs are charged on every buy and sell, **by stock**: half the spread plus slippage, from how much money trades in it a day (SPY ≈ 0.015% a trade, a big stock ≈ 0.02%, a small one ≈ 0.16%) — `SLIPPAGE_PCT` is the extra on top. If a bar touches both the stop and the target, the stop is assumed to come first, and a buy-stop filled in a minute that closes below the stop counts as stopped out.

The question isn't "did it make money" but **"could that be luck?"** Trades on the same day move together, so the test resamples whole *days* (a bootstrap) to see how sure it can be:

- **✅ Proven** — positive after costs in **both halves**, still positive **without its 3 best trades**, 40+ trades, average +0.08R or better, and the 80% range for its average is above zero.
- **🟡 Promising** — a positive average (+0.05R or better) that's 85%+ likely to be real. Small, or autopilot only.
- **⚪ No measurable edge** — positive or negative, but indistinguishable from zero. Paper only.
- **🔴 Loses money** — a negative average. Paper only.

Only proven and promising setups ever alert you or get followed by the autopilot. Each result also shows what a trade earns **in percent of the money put in** ("typical trade −0.4%"), because R-multiples can flatter a strategy with tiny stops. The dashboard's **Setups** tab shows each setup's backtest trade by trade, and a plain verdict on top.

**Honest expectations for day trading.** The first fair test (whole market, real costs) found most setups don't beat costs, and the only one with a probable edge — the in-play breakout — loses a little on most trades and makes it back on a few big runners (about 14% winners). On a $700–$1,000 account that's worth a few dollars a month at best, with big swings, before the slippage no test can see. That's why the 🧺 Invest and 🪙 Crypto plans exist, and why `/today` says so plainly when there's nothing to trade.

The short side of every setup is measured too, but stays on paper unless `SHORT_ALERTS` is on (Robinhood and Chase don't let you short stocks).

Every signal, alerted or not, is also **paper-traded** (`/setups`). If a proven setup starts losing on paper, it's **benched** and goes silent again. A setup that **wins on paper by the same standard** (30+ trades, +0.12R or better on average, positive in both halves and without its 3 best) is promoted to **✅ proven live**, even if the backtest didn't prove it. Setups that lose money in the backtest are never sent, but paper keeps measuring them, so they can earn their way back.

---

## 🤖 The autopilot

A pretend account (`AUTOPILOT_START`, $1,000 to begin with) that follows every alert the bot sends you, like FomoBot's paper autopilot:

- **Buys** at the next minute's price after the alert, plus costs (`SLIPPAGE_PCT` each way). For stocks in play it places the buy-stop and only buys if the price breaks the first 5-minute high (cancelled at 3pm).
- **Sizes** like a real small account: 1% of its balance at risk at the stop, at most a third of the balance in one trade (so it can hold 3 at once), never more cash than it has, fractional shares when a whole one doesn't fit.
- **Sells** by each setup's plan: stocks in play hold to 3:55pm unless stopped; SPY / QQQ band trades get out at the first half-hour close back inside the band; the others sell half at the first target, move the stop to breakeven and sell the rest at the second. No new trades in the last 30 minutes.
- **What it follows.** Every setup that alerts you (proven or probably real), plus — silently, never alerting — the best **candidate**: a setup with no measurable edge *yet* but a positive average that's at least 70% likely to be real (right now, the in-play breakout). That keeps live evidence building toward a promotion instead of the autopilot sitting idle. It holds up to 8 trades at once (`AUTOPILOT_MAX_OPEN`): this setup wins about 1 trade in 5 and the winners are big, so spreading over more of them measures it far better than 3 would. If nothing qualifies, it says so and waits.
- **When to believe it.** Not before 60+ trades. The verdict then says whether the result is more than luck (the same day-resampling test, ✅ / 🟡 / 🔴).
- Tells you each buy and close in Telegram (silent). `/autopilot` and the dashboard's Autopilot tab show the balance against simply holding SPY, results by setup and by time of day, and a plain verdict once there are 20+ trades.
- `/autopilot reset 500` starts over with $500. `/autopilot off` stops it. It never touches a real account.

Read it like this: if it's up after 20+ trades, the alerts are worth following, especially the setups marked ✅ on its page. If it's down, don't follow them blindly; follow only the setups that are green there, or none.

`/setups` shows each one's record. `/backtest` re-runs the test any time.

---

## 🧺 Invest lab

Day trading a few hundred dollars is a losing game after costs; **ETF plans that trade a few times a year** keep costs near zero, use free fractional shares, and have decades of research behind them. DayBot tests them on all the free daily history Alpaca has (about 10 years: the 2018 drop, the 2020 crash, the 2022 bear market, dividends included) and compares each with holding SPY.

| Plan | What it does |
|---|---|
| **Trend filter · SPY / QQQ** | Hold the index while it's above its 200-day average, else T-bills (Faber, 2007) |
| **Dual momentum** | Each month the better of US or developed-world stocks if it beat T-bills over 12 months, else bonds (Antonacci) |
| **Sector rotation** | Each month the 3 best sector ETFs over 6 months, each only while above its 200-day average |
| **Five-asset trend** | Equal parts US stocks, world stocks, bonds, real estate and gold, each only while above its 10-month average (Faber) |
| **Dip buying (RSI-2)** | Buy SPY / QQQ after a sharp 2-day drop inside an uptrend, sell on the bounce (Connors) |
| **3× Nasdaq trend (aggressive)** | The QQQ trend signal applied to TQQQ: far more upside and drops of 50%+ |
| Benchmarks | Hold SPY · 60/40 stocks and bonds |

**How it stays honest.** A decision uses only that day's close and is traded at the *next* open, paying a cost on every dollar traded (`INVEST_COST_PCT`). Everything uses the textbook numbers — nothing was tuned on this data. Sharpe is measured over T-bills with a bootstrap interval, so sitting in cash earns no credit for being calm. The engine was checked on 26 years of real SPY: holding returned 9.6% a year with a −55% worst drop; the 200-day trend filter 8.1% (before T-bill interest) with −23%; RSI-2 won 77% of 203 trades at +0.64% each — all matching the published results.

**Verdicts** against holding SPY: ✅ *beat it* (more return and a better ride) · 🟢 *smoother ride* (keeps at least half the return, falls much less, Sharpe interval above zero) · 🟠 *more return, deeper drops* · ⚪ *no better than holding*. The plan worth following is the best Sharpe among ✅/🟢 — or **"keep buying a broad index fund"** when none qualifies. Most plans earn a bit *less* than holding SPY in a rising market; they exist to dodge crashes.

- `/invest` — the verdict, your plan, a scoreboard · `/invest plan trend_spy` — follow another · `/invest test` — re-run.
- **What would my money become?** `/invest grow 50 5` (or the dashboard's calculator): $50 a month for 5 years over every 5-year stretch of the history — worst, typical, best — next to what you put in. At a few hundred dollars the money you add matters more than any plan.
- **It messages you when your plan changes** (and once a month, quietly). Dollar instructions — "sell $120 of VWO, buy $120 of VOO" — are **off** until you turn on `INVEST_MANAGE`, so it never tells you to restructure your accounts unasked. With it on, it treats your ETF holdings (or `INVEST_BUDGET`) as the plan's money and uses the tickers you already own (VOO for SPY, QQQM for QQQ, VEA, VWO, BND).
- It never places a trade. Selling in a taxable account can realize gains.

---

## 🪙 Crypto

Crypto trades around the clock, and day-trading it doesn't pay at Robinhood: studies of intraday crypto rules find an edge of a few hundredths of a percent a day, and Robinhood's spread costs about **1% on every buy and every sell**. So DayBot's crypto side is a **trend plan** that trades a few times a month, plus a 15-minute Bitcoin tool for prediction markets.

### The trend plan (`/crypto`, dashboard 🪙 Crypto tab)

From *Catching Crypto Trends* (Zarattini, Pagani & Barbon, 2025), with the 10 biggest coins Robinhood sells that Alpaca has prices for (`CRYPTO_COINS`):

- **Signal.** Seven trend models per coin look back 20, 30, 60, 90, 150, 250 and 360 days. Each one buys when the close beats every close in its lookback, and sells when it falls under its trailing stop (the middle of the lookback's high and low, which only moves up). The **trend score** (0–7) is how many are in. (The paper also uses 5 and 10 days; at a 1% spread those only add trades — leaving them out cut trades 60% and *raised* the return in both test windows.)
- **Size.** Each coin gets an equal slice of your crypto money (`CRYPTO_BUDGET`). Inside its slice it holds more the stronger the trend and the calmer the coin: score ÷ 7 × (risk level ÷ the coin's 90-day swing), at most 2× the slice, never more than 100% overall. No trends = all cash.
- **Trade.** Once a day when the daily candle closes. A coin already held is only re-sized when it's 50%+ off its target. You get a message only when something changes: "🟢 Buy SOL $4.20 · 🔴 Sell all DOGE".
- **The crypto autopilot** (`CRYPTO_AUTOPILOT_START`, $1,000) follows the plan with pretend money and Robinhood's 1% cost, and is compared with putting the same money in Bitcoin. `/crypto reset 500` starts it over.

**How it tested.** On Coin Metrics daily prices with each month's real top-10 coins, dead ones like LUNA and FTT included (no hindsight), mid-2020 to May 2026, 1% cost on every buy and sell:

| | Total | A year | Worst drop | Sharpe |
|---|---|---|---|---|
| Trend plan, risk level 50 (default) | +229% | +22% | −32% | 1.04 |
| Trend plan, risk level 25 | +88% | +11% | −16% | 1.04 |
| Trend plan, risk level 100 | +392% | +30% | −52% | 0.93 |
| Just holding Bitcoin | +767% | +43% | −77% | 0.92 |

**What that means.** It earns a much better return per unit of risk and falls far less (2022: −12% vs Bitcoin −64%), but in a big bull run it **trails Bitcoin badly** (2023: +25% vs +155%). If you can sit through −77% drops, holding Bitcoin made more; this plan is for people who don't want to. On the shorter window Alpaca provides (Dec 2021 onward, a bear market first) it made about +9% a year against Bitcoin's +7%, with a −24% worst drop against −72%. Robinhood's spread costs about half the gains here: at a 0.1%-cost exchange the same plan made +289%. DayBot re-runs the test on Alpaca's prices when it starts, then weekly, and labels it ✅ / 🟡 / 🔴 (`/crypto test`).

### ⏱ BTC 15-minute Up or Down (`/btc15`, top of the Crypto tab)

For the "Bitcoin Up or Down — 15 minutes" markets: windows 3:00–3:15, 3:15–3:30… New York time. Up wins when the price at the end is at or above the **price to beat** (the price at the start).

- **During a window the odds are real.** They come from how far Bitcoin is from the price to beat, how much time is left and how much it's been swinging (the last 2 hours of 1-minute moves). Its one setting, how fat the tails are, is fitted on the 30 days *before* the 30 days it's scored on, so the **track record** it shows (how often its favorite won at 5 and 10 minutes in, and "said 80% → won 81%") comes from windows it never saw. Live over its first four days: favorite won 68% at 5 minutes in and 76% at 10.
- **At the start of a window it's a coin flip, and it says so.** Studies find nothing reliably beats a coin toss at these horizons. DayBot checks every 6 hours whether the last 30 days had a pattern from one window to the next; it only shows a **lean** when the pattern is statistically clear.
- **The market's own prices.** DayBot reads Polymarket's and Kalshi's public prices for the current window (no keys; `BTC15_VENUE` picks one or both). A side is "cheap" when its fair value beats its price by `BTC15_MIN_EDGE` cents *after the fee* (`BTC15_FEE_CENTS`, about 1.75¢ at a 50¢ price, less near the ends). If a venue can't be read it just says so — `/btc15 62` (the Up price) and `/btc15 76543` (the market's price to beat) always work by hand.
- **The proof.** Odds that match reality aren't an edge: the market sees the same price you do. So at 5 and 10 minutes into every window DayBot saves what each venue charged, and keeps a **paper record**: what following the cheap-side signal would have made per contract after fees, next to always buying the favorite. The dashboard and `/btc15` show it. **Only when that record is positive with confidence (40+ signals, +1¢ a contract, and the day-by-day 80% range above zero) will it message you a cheap side** (`BTC15_EDGE_ALERTS`). Until then it's paper only.
- `BTC15_ALERTS` = 1 sends the call (silently) a minute into every window: 96 messages a day, so it's off by default.

DayBot never trades crypto or prediction markets for you either. Your own Robinhood crypto trades aren't read or journaled yet.

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
| `/today` | what to do today across everything, and what each part is worth at your size |
| `/invest` · `/invest plan NAME` · `/invest grow 50 5` · `/invest test` | the ETF plans tested on 10 years: which to follow, what $50 a month became |
| `/setups` `/backtest` | what the setups have earned |
| `/crypto` · `/crypto test` · `/crypto reset 1000` · `/crypto off` | the crypto trend plan, its backtest, its pretend account |
| `/btc15` · `/btc15 62` · `/btc15 76543` | Bitcoin's odds for this 15-minute window (vs a 62¢ market price, or the market's price to beat) |
| `/ask …` or end a message with `?` | the assistant, using your live data |
| `/rules` · `/set NAME value` | your rules and settings |
| `/pause` `/resume` · `/connect` · `/status` · `/export` | |

Dashboard: **http://localhost:8788** (only on your computer). Tabs: **Today** (what to do today, what each part is worth, your numbers, a trade check box, alerts, open trades), **Autopilot**, **🧺 Invest** (plans, scoreboard, growth calculator), **🪙 Crypto** (the BTC 15-minute call, the trend plan, its autopilot and backtest), **Market** (gappers, watchlist, click any ticker for its chart), **My trades** (journal, leaks, holdings, accounts), **Setups**, **Settings**.

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
| `INVEST` / `INVEST_MANAGE` | 1 / 0 | the Invest lab / whether it tells you exactly what to buy and sell (off: it shows the plan only) |
| `INVEST_BUDGET` / `INVEST_COST_PCT` | 0 / 0.03 | money the plan manages (0 = your ETF holdings) / cost per ETF trade in the test |
| `CRYPTO_BUDGET` | 100 | crypto money the plan's dollar amounts are for |
| `CRYPTO_TARGET_VOL` | 50 | risk level: 25 → ~9%/yr, worst drop −18%; 50 → ~18%/yr, −34%; 100 → ~28%/yr, −58% (tested) |
| `CRYPTO_COST_PCT` | 1 | cost per crypto buy or sell (Robinhood's spread) |
| `BTC15` / `BTC15_ALERTS` | 1 / 0 | the BTC 15-minute call / a message every window |
| `BTC15_VENUE` / `BTC15_MIN_EDGE` / `BTC15_FEE_CENTS` / `BTC15_EDGE_ALERTS` | 0 / 4 / 1.75 / 1 | which market's prices (0 both, 1 Polymarket, 2 Kalshi) / cents of edge to call a side cheap / fee at 50¢ / message proven cheap sides |

**Sizes fit your cash.** Alerts and checks size by your risk rule, then cap it at the cash you can actually buy with (the smaller of cash and buying power, so margin is never counted as your money). When a whole share doesn't fit, you get a fractional size (Robinhood and Chase both sell fractional shares of most stocks).

`/rules` lists everything else (scanner filters, option filters, alert limits…).

---

## Good to know

- **The $25K day-trading rule is gone.** FINRA replaced the pattern-day-trader rule with intraday margin standards on June 4, 2026, and Robinhood removed its day-trade limits on margin accounts. Brokers have until Oct 20, 2027 to switch over, so check your own broker. DayBot doesn't count day trades. It watches your size and your losses instead.
- **Robinhood asks you to log in again every so often.** When the link breaks, DayBot tells you, and `/connect` fixes it.
- **Your crypto, cash sweeps and funds aren't day trades.** They're left out of checks and the journal (the crypto plan is separate, above). Leveraged and inverse ETFs (TQQQ, SOXL…) are left out of the gapper list: they "gap" every day only because the index moved.
- **Cash accounts (like Chase) settle the next business day.** Money from a sale is usable to buy right away, but sell what you bought with it before it settles and you get a good-faith violation (three in a year restricts the account). With a small cash account, one round trip a day is the safe pace.
- **Selling a holding from before DayBot** (stock you've had for months) is recognised as such: priced against your average cost, never mistaken for a short, and kept out of the day-trading journal.
- **Fills arrive in seconds to a minute** through SnapTrade. The very first sync books your existing positions quietly. A fill DayBot only sees well after the fact (it was off) is booked and recapped, but not "checked".
- **A backtest is not the future.** The labels say what the data showed after costs. Paper keeps measuring, and sizes stay your call.

## Running it on your server

`send_to_server.command` installs DayBot next to FomoBot once your Alpaca keys are in `.env`. It runs as its own service (`daybot`). `open_server_dashboard.command` opens both dashboards: DayBot's is at http://localhost:8798.
