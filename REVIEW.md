# FomoBot — a month in review (Sep 29 – Oct 15)

Written for v30 after going through every review export you sent (Oct 1, 5, 7, 8, 12, 12, 14 — merged into one
dataset: 977 alerts, 4,947 whale trades, 789K price records, 892 hype coins, 430 paper trades) and the code.

## The short version

1. **The scam you kept seeing is real and it has one signature: a brand-new pool.** The "whales" in those coins are
   launch snipers and insiders — wallets that buy minutes after a pool opens. Across all your exports:

   | whale buy into a pool… | down half within 1 h | down half within 6 h |
   |---|---|---|
   | under 30 min old | **58%** | **67%** |
   | on the pump.fun curve | 28% | 62% |
   | 30 min – 6 h old | 19% | 35% |
   | **6 h or older** | **3%** | 13–20% |
   | a day+ old, $100K+ market cap | 0% | 5% |

   Nothing else separated rugs from real coins nearly as well (buy size, holder share, grade, launchpad).
2. **Three things in the bot let those coins through**, and v30 removes all three:
   - whale buys were judged on the whale, never on the pool's age;
   - in the hype scanner, *your whales being in a coin switched the fresh-pool scam rule off* — and the snipers are
     exactly who's "in" a fresh launch;
   - the whale scanner followed wallets it couldn't score "on trial" (10 on Oct 14 alone). Wallets found by the
     scanner were the worst source of buys in your data.
3. **No strategy the bot runs has made money after costs on honest numbers.** Not hype alerts (859 coins replayed,
   every slice, every exit plan, entry delay or dip-buy), not copying whale buys in general. The best things in the data
   are small and unproven: one whale (EC2f…92MJ, buys settled coins, +5.7% a trade over 21 replayed copies), and pools
   1 h+ old with liquidity ≥ half the market cap (+5.5% a trade over 58 coins, carried by 2 of them).
4. So v30 is built to **not lose money while it looks for an edge**: scam launches are never alerted or traded,
   and the autopilot only trades a setup after it made money on a week of replays AND then on paper going forward.
   Real money comes last.

## What the numbers looked like over the month — and why the early "good" ones were wrong

| period | what the bot said | what was true |
|---|---|---|
| Oct 1–11 (v10–v28.2) | paper balance climbing, at one point +$2,332 | 38 paper sales had "sold" into pools whose liquidity was already pulled — impossible fills. Honest balance about −$680. Fixed in v28.3. |
| Oct 12–14 (v28.3–28.4, honest fills) | −$336 over 99 trades, 24% won | correct. Hype alerts lost −7.9% a trade; whale copies about flat. |
| v28.3–v29 replays | "rug" rates of 40–60% | partly wrong: a coin whose price didn't move for 10 minutes was counted as a rug even with $40K still in the pool (144 of 859 coins). Fixed in v29. |

The version that "seemed good for a while" was good only on paper, because paper could sell into empty pools. Every
time the accounting was made honest, the results dropped to what the market actually paid. That's the pattern you
described ("one seems good, then the next is shit"): the strategy didn't get worse — the measurement got more honest.

## What I got wrong (and fixed)

- Paper fills that ignored liquidity (sold into rugged pools) — v28.3.
- Replays that counted quiet coins as rugs — v29.
- A Chainstack subscription bug that left 12 of 17 whales unsubscribed for hours, and subscriptions sent faster than
  the free plan allows — v29.
- A whale scanner that ran every hour by default (up to ~2,500 RPC calls a run — a big part of why free RPC plans ran
  out) and followed unproven wallets "on trial" — v30 (every 6 h; unproven wallets are suggestions only).
- Whale presence switching the fresh-pool rule off — v30 (no exceptions now).

## What v30 does

**Never** (no alert, no paper trade, no real trade — but still recorded and scored):
- a whale buy into a pool under `WHALE_MIN_POOL_HOURS` (6 h) or still on the launch curve;
- a hype coin under `HYPE_MIN_POOL_MINUTES` (60 min), whoever is in it;
- real money into either, even if something else let it through (the live trader checks again).

**Only what's proven** (`/edge`, Hype tab → Edge): every 6 hours a fixed menu of setups is replayed on the last 7 days —
hype coins (7 types × 4 exits) and whale buys into settled pools (5 types × 5 exits: all, $100K+ day-old coins, $1K+
buys, 2+ whales, whales whose earlier copies made money). A setup has to make money after costs on **both** halves of
the week, and without its single best trade. Then paper trades it going forward; real money follows only after 15+ of
those paper trades made money. Nothing passing = the autopilot stands aside, alerts keep coming (marked), measuring
continues.

**Whale scanner**: launch snipers (half or more of their buys within 30 min of a pool opening) are rejected; unproven
wallets are sent as picks for you to decide; runs every 6 h. The 9 wallets it had followed "on trial" are unfollowed
on upgrade.

**Data**: the live stream runs on two providers at once; `/health` shows each, plus how many whale trades happened
while the computer was asleep (92 of 236 late trades in the Oct 14 export — only a server that's always on catches
those, see `SERVER_SETUP.txt`).

## What to expect, and how to judge progress

- **Fewer alerts.** The brand-new launches were a big share of what you were seeing. What's left is whale buys into
  coins that survived their first hours, and hype coins at least an hour old.
- **The autopilot will probably stand aside at first.** That's the correct answer when nothing has been making money —
  it costs nothing, where buying every alert cost $336 in two days.
- **Progress = the Edge card.** Watch it after a few days: a setup turning ✅ in the replays, then its paper trades
  staying positive over 15+ trades. That's the first point where real money is justified, and the bot enforces it.
- **Send an `/export` after 5–7 days.** It now carries everything needed (paper period, edge reports, market readings,
  health history), so the next review is about results, not reconstruction.

## Honest limits

Following whales and hype in memecoins is a crowded game: insiders, snipers and bots are ahead of any alert. A bot can
avoid the traps (v30 does) and measure honestly (v29–v30 do), but it can't create an edge that isn't in the market.
If one shows up — a whale that keeps making money on settled coins, a coin type that keeps paying after costs — the bot
will find it, prove it on paper, and only then trade it.
