"""Your own trades and positions.

Two ways in, both feeding one `my_trades` table:
- Wallet sync (MY_WALLETS / PUBLIC_SOLANA_WALLET_ADDRESS in .env): every buy/sell in your
  wallet is read from the chain, so cost and proceeds are exact — including every fee you paid,
  but excluding the refundable token-account deposit.
- Manual: /bought 20 (at 850k)  and  /sold 5 | 30% | all (at 1.2m). Typing the market cap
  your app showed gives the same numbers your app shows.

Profit/loss uses average cost. Unrealized P/L is simply "value now minus what the remaining
tokens cost you" — no invented fees are subtracted, so a flat coin shows ~$0, not a loss.
"""
from __future__ import annotations

import time

from .util import num

DUST_USD = 0.50


class Portfolio:
    def __init__(self, db, cfg, market):
        self.db = db
        self.cfg = cfg
        self.market = market

    # -- recording ----------------------------------------------------------------------------
    def record_wallet_swap(self, wallet: str, sig: str, swap: dict, usd_value: float, fee_usd: float,
                           mc_usd: float, ts: int) -> int:
        """Exact trade from your wallet. Buys cost the swap + network fee; sells return swap - fee."""
        usd = usd_value + fee_usd if swap["side"] == "BUY" else max(0.0, usd_value - fee_usd)
        price = usd_value / swap["token_amount"] if swap["token_amount"] > 0 else 0.0
        return self.db.insert("""insert or ignore into my_trades(ts,mint,side,usd,tokens,price_usd,mc_usd,source,sig,wallet)
            values(?,?,?,?,?,?,?,?,?,?)""",
                              (ts, swap["mint"], swap["side"], usd, swap["token_amount"], price, mc_usd, "wallet", sig, wallet))

    async def _price_at(self, mint: str, mc_usd: float = 0.0) -> tuple[float, float]:
        """(price, market cap) now — or at the market cap the user typed."""
        info = await self.market.token(mint, max_age=10)
        price, mcap = num(info.get("price_usd")), num(info.get("mc_usd"))
        supply = mcap / price if price > 0 and mcap > 0 else 0.0
        if not supply:
            safety = await self.market.safety(mint)
            supply = num(safety.get("supply"))
        if mc_usd > 0:
            if supply <= 0:
                raise ValueError("couldn't read this coin's supply, so I can't convert that market cap")
            return mc_usd / supply, mc_usd
        if price <= 0:
            raise ValueError("no live price for this coin right now — add the market cap, e.g. 'at 850k'")
        return price, mcap or price * supply

    async def manual_buy(self, mint: str, usd: float, mc_usd: float = 0.0) -> dict:
        if usd <= 0:
            raise ValueError("amount must be more than $0")
        price, mcap = await self._price_at(mint, mc_usd)
        tokens = usd * (1 - self.cfg.get("MANUAL_FEE_PCT") / 100) / price
        tid = self.db.insert("""insert into my_trades(ts,mint,side,usd,tokens,price_usd,mc_usd,source,sig)
            values(?,?,?,?,?,?,?,?,?)""", (int(time.time()), mint, "BUY", usd, tokens, price, mcap, "manual",
                                           f"manual-{time.time_ns()}"))
        return {"id": tid, "usd": usd, "price": price, "mc": mcap, "tokens": tokens}

    async def manual_sell(self, mint: str, usd: float = 0.0, fraction: float = 0.0, mc_usd: float = 0.0) -> dict:
        """Sell by dollars received (usd) or by share of the position (fraction, 1.0 = all)."""
        pos = self.position(mint)
        if not pos or pos["tokens"] <= 0:
            raise ValueError("you have no recorded position in this coin")
        price, mcap = await self._price_at(mint, mc_usd)
        keep = 1 - self.cfg.get("MANUAL_FEE_PCT") / 100
        if fraction > 0:
            tokens = pos["tokens"] * min(1.0, fraction)
            usd = tokens * price * keep
        elif usd > 0:
            tokens = min(pos["tokens"], usd / (price * keep))
        else:
            raise ValueError("say how much you sold: /sold 5, /sold 30% or /sold all")
        tid = self.db.insert("""insert into my_trades(ts,mint,side,usd,tokens,price_usd,mc_usd,source,sig)
            values(?,?,?,?,?,?,?,?,?)""", (int(time.time()), mint, "SELL", usd, tokens, price, mcap, "manual",
                                           f"manual-{time.time_ns()}"))
        return {"id": tid, "usd": usd, "price": price, "mc": mcap, "tokens": tokens}

    def undo_last_manual(self) -> dict | None:
        row = self.db.row("select * from my_trades where source='manual' order by id desc limit 1")
        if row:
            self.db.run("delete from my_trades where id=?", (row["id"],))
        return row

    # -- positions ------------------------------------------------------------------------------
    def _ledger(self, mint: str) -> dict:
        tokens = cost = realized = bought = sold = 0.0
        entry_mc_weight = 0.0
        first_ts = last_ts = 0
        unmatched = 0
        sources = set()
        for t in self.db.rows("select * from my_trades where mint=? order by ts, id", (mint,)):
            sources.add(t["source"])
            first_ts = first_ts or t["ts"]
            last_ts = t["ts"]
            if t["side"] == "BUY":
                tokens += num(t["tokens"])
                cost += num(t["usd"])
                bought += num(t["usd"])
                entry_mc_weight += num(t["usd"]) * num(t["mc_usd"])
            else:
                if tokens <= 0:
                    unmatched += 1  # sold something bought before tracking started: no cost basis
                    continue
                q = min(num(t["tokens"]), tokens)
                share = q / tokens
                basis = cost * share
                proceeds = num(t["usd"]) * (q / num(t["tokens"])) if num(t["tokens"]) > 0 else 0.0
                realized += proceeds - basis
                cost -= basis
                tokens -= q
                sold += proceeds
        return {"mint": mint, "tokens": tokens, "cost": cost, "realized": realized, "bought": bought, "sold": sold,
                "entry_mc": entry_mc_weight / bought if bought > 0 else 0.0, "first_ts": first_ts,
                "last_ts": last_ts, "unmatched_sells": unmatched, "source": "wallet" if "wallet" in sources else "manual"}

    def position(self, mint: str, price: float | None = None) -> dict | None:
        if not self.db.scalar("select 1 from my_trades where mint=? limit 1", (mint,)):
            return None
        led = self._ledger(mint)
        info = self.market.cached(mint)
        price = num(price if price is not None else info.get("price_usd"))
        value = led["tokens"] * price
        led.update(price=price, value=value, symbol=str(info.get("symbol") or mint[:4]),
                   mc_now=num(info.get("mc_usd")), unrealized=value - led["cost"] if price > 0 else 0.0,
                   priced=price > 0)
        led["open"] = led["tokens"] > 0 and (value >= DUST_USD or not led["priced"])
        led["pnl"] = led["realized"] + led["unrealized"]
        led["pnl_pct"] = led["pnl"] / led["bought"] * 100 if led["bought"] > 0 else 0.0
        led["multiple"] = value / led["cost"] if led["cost"] > 0 and price > 0 else 0.0
        return led

    def mints(self) -> list[str]:
        return [r["mint"] for r in self.db.rows("select mint, max(ts) t from my_trades group by mint order by t desc")]

    def open_positions(self) -> list[dict]:
        out = [p for p in (self.position(m) for m in self.mints()) if p and p["open"]]
        out.sort(key=lambda p: -p["value"])
        return out

    def holds(self, mint: str) -> bool:
        p = self.position(mint)
        return bool(p and p["open"])

    def summary(self, days: int | None = None) -> dict:
        cutoff = int(time.time()) - days * 86400 if days else 0
        realized = unrealized = invested = 0.0
        closed = wins = 0
        for mint in self.mints():
            p = self.position(mint)
            if not p or (cutoff and p["last_ts"] < cutoff):
                continue
            realized += p["realized"]
            invested += p["bought"]
            if p["open"]:
                unrealized += p["unrealized"]
            elif p["bought"] > 0:
                closed += 1
                wins += p["realized"] > 0
        return {"realized": realized, "unrealized": unrealized, "total": realized + unrealized,
                "invested": invested, "closed": closed, "win_rate": wins / closed if closed else 0.0}

    def pnl_curve(self) -> list[tuple[int, float]]:
        """Cumulative realized P/L over time, one point per sell."""
        points, total = [], 0.0
        state: dict[str, list[float]] = {}
        for t in self.db.rows("select * from my_trades order by ts, id"):
            tokens, cost = state.setdefault(t["mint"], [0.0, 0.0])
            if t["side"] == "BUY":
                state[t["mint"]] = [tokens + num(t["tokens"]), cost + num(t["usd"])]
            elif tokens > 0 and num(t["tokens"]) > 0:
                q = min(num(t["tokens"]), tokens)
                basis = cost * q / tokens
                total += num(t["usd"]) * q / num(t["tokens"]) - basis
                state[t["mint"]] = [tokens - q, cost - basis]
                points.append((int(t["ts"]), round(total, 2)))
        return points
