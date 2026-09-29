"""The whales you follow and how copying each one has actually worked out."""
from __future__ import annotations

import statistics
import time

from .copies import return_pct
from .util import is_address, num, short

MIN_COPIES_FOR_STATUS = 5
SHRINK = 5            # pulls small samples toward 0% so one lucky trade can't make a whale HOT
RETURN_CAP = 1000.0   # +1000% cap per copy when scoring (price-feed glitches can't dominate)
SETTLE_SECONDS = 24 * 3600

STATUS_EMOJI = {"HOT": "🔥", "OK": "✅", "NEW": "🆕", "WEAK": "〰️", "COLD": "🧊"}


class Whales:
    def __init__(self, db, cfg, my_wallets=()):
        self.db = db
        self.cfg = cfg
        self.my_wallets = set(my_wallets)
        self._stats_cache: dict[str, tuple[float, dict]] = {}

    # -- book keeping -----------------------------------------------------------------------
    def add(self, address: str, name: str = "", source: str = "manual") -> tuple[bool, str]:
        address = str(address or "").strip()
        if not is_address(address):
            return False, "That doesn't look like a Solana wallet address."
        if address in self.my_wallets:
            return False, "That's your own wallet — it's already synced for your positions."
        given = str(name or "").strip()[:32]
        existing = self.db.row("select * from whales where address=?", (address,))
        if existing:
            name = given or existing["name"]
            self.db.run("update whales set name=?,active=1 where address=?", (name, address))
            return True, f"Following {name}."
        if self.count() >= self.cfg.max_whales:
            return False, f"You're following {self.cfg.max_whales} whales (the max). Remove one first."
        name = given or short(address)
        clash = self.db.scalar("select address from whales where lower(name)=lower(?) and active=1", (name,))
        if clash:
            name = f"{name}-{address[:3]}"
        self.db.run("insert into whales(address,name,added_ts,source) values(?,?,?,?)",
                    (address, name, int(time.time()), source))
        return True, f"Now following {name}."

    def remove(self, address: str) -> None:
        self.db.run("update whales set active=0 where address=?", (address,))

    def set_muted(self, address: str, muted: bool) -> None:
        """Manual mute/unmute. Unmuting also overrides auto-mute for good (auto_muted = -1):
        auto_muted is 0 normally, 1 when the bot muted a COLD whale, -1 when you chose to hear them anyway."""
        if muted:
            self.db.run("update whales set muted=1 where address=?", (address,))
        else:
            self.db.run("update whales set muted=0,auto_muted=case when auto_muted=1 then -1 else auto_muted end "
                        "where address=?", (address,))

    @staticmethod
    def is_silenced(whale: dict) -> bool:
        return bool(whale.get("muted")) or whale.get("auto_muted") == 1

    def count(self) -> int:
        return int(self.db.scalar("select count(*) from whales where active=1", default=0))

    def active(self) -> list[dict]:
        return self.db.rows("select * from whales where active=1 order by added_ts")

    def get(self, address: str) -> dict | None:
        return self.db.row("select * from whales where address=?", (address,))

    def find(self, text: str) -> dict | None:
        """Look a whale up by full address, name, or the start of the address."""
        t = str(text or "").strip()
        if not t:
            return None
        return (self.db.row("select * from whales where address=?", (t,))
                or self.db.row("select * from whales where lower(name)=lower(?) and active=1", (t,))
                or self.db.row("select * from whales where address like ? and active=1", (t + "%",)))

    def name(self, address: str) -> str:
        if address in self.my_wallets:
            return "You"
        return str(self.db.scalar("select name from whales where address=?", (address,), default="") or short(address))

    def is_alerting(self, whale: dict) -> bool:
        return bool(whale) and bool(whale.get("active")) and not self.is_silenced(whale)

    def usual_buy_usd(self, address: str) -> float:
        values = [num(r["usd_value"]) for r in self.db.rows(
            "select usd_value from swaps where wallet=? and side='BUY' and usd_value>0 order by ts desc limit 40",
            (address,))]
        return statistics.median(values) if len(values) >= 3 else 0.0

    # -- scoring ------------------------------------------------------------------------------
    def stats(self, address: str, days: int = 30, fresh: bool = False) -> dict:
        hit = self._stats_cache.get(address)
        if hit and not fresh and time.time() - hit[0] < 30:
            return hit[1]
        now = int(time.time())
        fee = self.cfg.get("COPY_FEE_PCT")
        rows = self.db.rows("select * from copies where whale=? and open_ts>=?", (address, now - days * 86400))
        settled, open_now = [], []
        for c in rows:
            if c["status"] == "closed":
                settled.append(num(c["return_pct"]))
            else:
                r = return_pct(c, num(c["last_price"]), fee)
                open_now.append(r)
                if now - int(c["open_ts"]) >= SETTLE_SECONDS:
                    settled.append(r)  # held a day or more: count it at today's price
        capped = [min(r, RETURN_CAP) for r in settled]
        n = len(capped)
        winners = [c for c in rows if num(c["peak_price"]) >= 1.5 * num(c["entry_price"]) > 0]
        dips = [num(c["dip_before_peak_pct"]) for c in winners]
        s = {
            "n": n,
            "open": len(open_now),
            "win_rate": sum(r > 0 for r in capped) / n if n else 0.0,
            "avg": sum(capped) / n if n else 0.0,
            "median": statistics.median(capped) if n else 0.0,
            "score": sum(capped) / (n + SHRINK) if n else 0.0,
            "best": max(settled) if settled else 0.0,
            "worst": min(settled) if settled else 0.0,
            "hit_2x": sum(1 for c in rows if c["hit_2x_ts"]) / len(rows) if rows else 0.0,
            "typical_dip": statistics.median(dips) if dips else 0.0,
            "winners": len(winners),
            "profit_per_100": sum(capped),  # $ made copying every settled buy with $100
            "open_avg": sum(open_now) / len(open_now) if open_now else 0.0,
        }
        s["status"] = self._status(s)
        self._stats_cache[address] = (time.time(), s)
        return s

    @staticmethod
    def _status(s: dict) -> str:
        if s["n"] < MIN_COPIES_FOR_STATUS:
            return "NEW"
        if s["score"] >= 15:
            return "HOT"
        if s["score"] >= 0:
            return "OK"
        if s["score"] < -10 and s["n"] >= 8:
            return "COLD"
        return "WEAK"

    def refresh_auto_mutes(self) -> list[tuple[dict, str]]:
        """Auto-mute whales that turned COLD and release ones that recovered. Returns changes."""
        changes = []
        for w in self.active():
            status = self.stats(w["address"], fresh=True)["status"]
            if self.cfg.flag("AUTO_MUTE_COLD_WHALES") and status == "COLD" and w["auto_muted"] == 0 and not w["muted"]:
                self.db.run("update whales set auto_muted=1 where address=?", (w["address"],))
                changes.append((w, "muted"))
            elif w["auto_muted"] == 1 and (status != "COLD" or not self.cfg.flag("AUTO_MUTE_COLD_WHALES")):
                self.db.run("update whales set auto_muted=0 where address=?", (w["address"],))
                changes.append((w, "unmuted"))
        return changes

    def leaderboard(self, days: int = 30) -> list[dict]:
        board = []
        for w in self.active():
            s = self.stats(w["address"], days)
            board.append({**w, **s, "stats": s})
        order = {"HOT": 0, "OK": 1, "NEW": 2, "WEAK": 3, "COLD": 4}
        board.sort(key=lambda r: (order[r["status"]], -r["score"], -(r["last_trade_ts"] or 0)))
        return board

    def holders_of(self, mint: str) -> list[dict]:
        """Every tracked whale that traded this coin: first entry, latest state, still holding?"""
        out = []
        for r in self.db.rows("""select wallet, min(case when side='BUY' then ts end) first_buy_ts,
                sum(case when side='BUY' then usd_value else 0 end) bought_usd,
                sum(case when side='SELL' then usd_value else 0 end) sold_usd
                from swaps where mint=? and is_me=0 group by wallet""", (mint,)):
            first = self.db.row("""select mc_usd,price_usd from swaps where wallet=? and mint=? and side='BUY'
                order by ts limit 1""", (r["wallet"], mint))
            last = self.db.row("""select side,holding_after,pre_holding,ts,sell_fraction from swaps
                where wallet=? and mint=? order by ts desc, id desc limit 1""", (r["wallet"], mint))
            peak_hold = num(self.db.scalar("""select max(max(holding_after),max(pre_holding)) from swaps
                where wallet=? and mint=?""", (r["wallet"], mint)))
            held = num(last["holding_after"]) if last else 0.0
            out.append({
                "wallet": r["wallet"], "name": self.name(r["wallet"]),
                "entry_mc": num(first["mc_usd"]) if first else 0.0,
                "entry_price": num(first["price_usd"]) if first else 0.0,
                "first_buy_ts": r["first_buy_ts"] or 0,
                "bought_usd": num(r["bought_usd"]), "sold_usd": num(r["sold_usd"]),
                "holding": held, "holding_pct": held / peak_hold * 100 if peak_hold > 0 else 0.0,
                "still_in": held > 0 and (peak_hold <= 0 or held / peak_hold >= 0.05),
                "last_ts": last["ts"] if last else 0,
            })
        out.sort(key=lambda h: h["first_buy_ts"] or 9e18)
        return out
