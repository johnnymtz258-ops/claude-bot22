"""A fully wired bot with fake network services and realistic sample data (tests + screenshots)."""
from __future__ import annotations

import asyncio
import time

from fomo import copies
from fomo.config import Config
from fomo.db import Database
from fomo.discovery import Discovery
from fomo.engine import Engine
from fomo.portfolio import Portfolio
from fomo.runners import RunnerScanner
from fomo.scout import WhaleScout
from fomo.telegram import Notifier
from fomo.tracker import Tracker
from fomo.whales import Whales
from tests.helpers import FakeMarket, FakeRPC, pump_buy, pump_sell

WHALES = {
    "Rocket": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
    "Dino": "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
    "Cupsey": "suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK",
    "Bagholder": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
}
ME = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
COINS = {  # symbol: (mint, price now, liquidity)
    "CASHED": ("CAsHEDmemeTokenMint1111111111111111111pump", 0.00132, 180_000),
    "MOIN": ("MoiNmemeTokenMint22222222222222222222222pump"[:44], 0.00041, 62_000),
    "TE": ("TEmemeTokenMint333333333333333333333333pump"[:44], 0.00022, 30_000),
    "BAGSPAY": ("BAGSPAYmemeTokenMint44444444444444444444pump"[:44], 0.00056, 75_000),
}


class FakeStream:
    connected = True
    subscribed = 5
    last_error = ""


class DemoApp:
    def __init__(self):
        self.cfg = Config(my_wallets=[ME], rpc_http=["x"], max_whales=60, dashboard_port=8787)
        self.cfg.set("CONFIRM_SECONDS", "0")
        self.started = time.time() - 3 * 3600
        self.db = Database(":memory:")
        self.rpc = FakeRPC()
        self.market = FakeMarket(self.db, self.rpc)
        self.whales = Whales(self.db, self.cfg, [ME])
        self.portfolio = Portfolio(self.db, self.cfg, self.market)
        self.notify = Notifier(None, self.db)
        self.engine = Engine(self.cfg, self.db, self.rpc, self.market, self.whales, self.portfolio, self.notify)
        self.tracker = Tracker(self.cfg, self.db, self.rpc, self.market, self.whales, self.portfolio, self.engine,
                               self.notify)
        self.discovery = Discovery(self.rpc, self.market, self.db, self.cfg)
        self.stream = FakeStream()
        self.find_task = None
        self.runners = RunnerScanner(self.cfg, self.db, self.market, self.engine, self.whales, self.notify)
        self.scout = WhaleScout(self.cfg, self.db, self.rpc, self.market, self.whales, self.runners, self.notify, lambda: None)
        self._sig = 0

    def set_setting(self, name, value):
        parsed = self.cfg.set(name, value)
        self.db.set_meta(f"setting:{name.upper()}", parsed)
        return parsed

    def refresh_wallets(self):
        pass

    async def run_find(self, mints, progress=None):
        return await self.discovery.find(mints, progress)

    async def feed(self, wallet, tx):
        self._sig += 1
        sig = f"demo{self._sig:05d}" + "x" * 20
        self.rpc.txs[sig] = tx
        await self.engine.process(wallet, sig, "stream")

    def price(self, symbol, price, liq=None):
        mint, _, default_liq = COINS[symbol]
        self.market.set_pair(mint=mint, symbol=symbol, price=price, mc=price * 1e9,
                             liq=default_liq if liq is None else liq)

    async def seed(self):
        """Two weeks of history: scored whales, closed copies, your trades, and fresh buys."""
        for name, address in WHALES.items():
            self.whales.add(address, name)
        now = int(time.time())
        history = {"Rocket": [2.4, 1.6, 0.7, 3.1, 1.2, 0.9, 5.5, 1.4], "Dino": [1.3, 0.8, 1.1, 0.95, 1.6, 1.2],
                   "Cupsey": [1.8, 0.6], "Bagholder": [0.5, 0.7, 0.4, 0.8, 0.6, 0.5, 0.45, 0.9, 0.55]}
        for name, results in history.items():
            for i, x in enumerate(results):
                mint = f"Past{name[:3]}{i}" + "1" * (40 - len(name[:3]) - len(str(i)))
                t = now - (i + 1) * 36 * 3600
                c = copies.open_copy(self.db, whale=WHALES[name], mint=mint, swap_id=0, alert_id=0, price=1.0,
                                     mc_usd=300_000, whale_price=0.95, ts=t)
                row = self.db.row("select * from copies where id=?", (c,))
                for dt, p in ((300, 0.8 if x > 1.5 else 0.95), (3600, 0.7 if x > 2 else 1.02), (7200, x)):
                    row = copies.update_path(self.db, row, p, t + dt)
                peak = x * 1.8 if x > 1 else 1.3   # most coins run past where the whale finally sells
                for k, p in enumerate([1.0, 0.85, 1.3, (1.3 + peak) / 2, peak, (peak + x) / 2, x]):
                    self.db.run("insert or ignore into price_marks(mint,ts,price) values(?,?,?)", (mint, t + k * 1100, p))
                copies.sell(self.db, row, 1.0, x, 1.0, "whale exited", t + 8000)
        self.whales.refresh_auto_mutes()
        # today's action
        self.price("CASHED", 0.000075)
        await self.feed(WHALES["Rocket"], pump_buy(wallet=WHALES["Rocket"], mint=COINS["CASHED"][0], sol=6,
                                                   tokens=12_000_000, block_time=now - 540))
        self.price("CASHED", 0.00009)
        await self.feed(WHALES["Dino"], pump_buy(wallet=WHALES["Dino"], mint=COINS["CASHED"][0], sol=10,
                                                 tokens=16_500_000, block_time=now - 500))
        await self.feed(ME, pump_buy(wallet=ME, mint=COINS["CASHED"][0], sol=0.3, tokens=480_000,
                                     block_time=now - 490))
        self.price("MOIN", 0.00031)
        await self.feed(WHALES["Cupsey"], pump_buy(wallet=WHALES["Cupsey"], mint=COINS["MOIN"][0], sol=3,
                                                   tokens=1_400_000, block_time=now - 400))
        await self.feed(ME, pump_buy(wallet=ME, mint=COINS["MOIN"][0], sol=0.15, tokens=70_000,
                                     block_time=now - 390))
        self.price("TE", 0.0003)
        await self.feed(WHALES["Bagholder"], pump_buy(wallet=WHALES["Bagholder"], mint=COINS["TE"][0], sol=2,
                                                      tokens=1_000_000, block_time=now - 300))
        await self.feed(ME, pump_buy(wallet=ME, mint=COINS["TE"][0], sol=0.2, tokens=95_000, block_time=now - 290))
        await self.feed(ME, pump_sell(wallet=ME, mint=COINS["TE"][0], sol=0.12, tokens=95_000, holding=95_000,
                                      close=True, block_time=now - 200))
        self.price("CASHED", 0.00031)
        await self.feed(WHALES["Rocket"], pump_sell(wallet=WHALES["Rocket"], mint=COINS["CASHED"][0], sol=6 * 4.1 * 0.4,
                                                    tokens=4_800_000, holding=12_000_000, block_time=now - 120))
        await self.feed(ME, pump_sell(wallet=ME, mint=COINS["CASHED"][0], sol=0.3 * 3.6 * 0.5, tokens=240_000,
                                      holding=480_000, block_time=now - 110))
        self.price("BAGSPAY", 0.00054)
        await self.feed(WHALES["Cupsey"], pump_buy(wallet=WHALES["Cupsey"], mint=COINS["BAGSPAY"][0], sol=4,
                                                   tokens=1_100_000, block_time=now - 20))
        for sym, (mint, price, liq) in COINS.items():
            self.price(sym, price, liq)
        # recorded price paths for today's coins (what the tracker stores once a minute)
        paths = {"CASHED": (0.000075, 0.00132), "MOIN": (0.00031, 0.00041), "TE": (0.0003, 0.00022),
                 "BAGSPAY": (0.00054, 0.00056)}
        for sym, (a, b) in paths.items():
            mint = COINS[sym][0]
            for k in range(0, 61):
                wobble = 1 + 0.12 * ((k * 7919) % 13 - 6) / 6
                self.db.run("insert or ignore into price_marks(mint,ts,price) values(?,?,?)",
                            (mint, now - 600 + k * 10 - 3000, (a + (b - a) * k / 60) * wobble))
            if sym == "TE":  # after you sold TE it bounced +45%
                for k, px in enumerate([0.00017, 0.00021, 0.00026, 0.00027, 0.00022]):
                    self.db.run("insert or ignore into price_marks(mint,ts,price) values(?,?,?)", (mint, now - 180 + k * 30, px))
        self.whales.add("GjJyeC1rB1p4d6k1Mzw5Y6vYGZyLr8N8zQJ7XU4yzF1G", "auto-GjJy", source="auto")
        for addr, status, pnl, wr, trips, reason in (
                ("GjJyeC1rB1p4d6k1Mzw5Y6vYGZyLr8N8zQJ7XU4yzF1G", "followed", 6.4, 0.62, 13, "+6.4 SOL over 13 trades, 62% won"),
                ("AKnL4NNf3DGWZJS6cPknBuEGnVsV4A4m5tgebLHaRSZ9", "rejected", -1.9, 0.31, 11, "❌ Losing lately"),
                ("9hSR6S7WPtxmTojgo6GG3k4yDPecgJY292j7xrsUGWBu", "rejected", 0.4, 0.5, 4, "only 4 closed trades")):
            self.db.run("""insert or replace into whale_candidates(address,found_ts,analyzed_ts,coins,verdict,pnl_sol,
                win_rate,trips,status,reason) values(?,?,?,?,?,?,?,?,?,?)""",
                        (addr, now - 7200, now - 7000, "CASHED,MOIN", "", pnl, wr, trips, status, reason))
        self.db.set_meta("scout_last_run", now - 7000)
        self.scout.last_run = now - 7000
        self.db.set_meta("scout_last_summary", '{"ts": 0, "coins": ["a", "b"], "checked": 6, "followed": 1}')
        await self.tracker.tick(now)


def build_demo() -> DemoApp:
    app = DemoApp()
    asyncio.get_event_loop().run_until_complete(app.seed())
    return app
