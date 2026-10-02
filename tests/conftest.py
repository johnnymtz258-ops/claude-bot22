import asyncio

import pytest

from fomo.config import Config
from fomo.db import Database
from fomo.engine import Engine
from fomo.portfolio import Portfolio
from fomo.runners import RunnerScanner
from fomo.scout import WhaleScout
from fomo.tracker import Tracker
from fomo.whales import Whales
from tests.helpers import FakeMarket, FakeRPC, Notes


class Bot:
    """Everything wired together with fake network services."""

    def __init__(self, my_wallets=()):
        self.cfg = Config(my_wallets=list(my_wallets), rpc_http=["x"], max_whales=60)
        self.cfg.set("CONFIRM_SECONDS", "0")  # judge whale buys at once in tests (test_gates covers the wait)
        self.db = Database(":memory:")
        self.rpc = FakeRPC()
        self.market = FakeMarket(self.db, self.rpc)
        self.whales = Whales(self.db, self.cfg, my_wallets)
        self.portfolio = Portfolio(self.db, self.cfg, self.market)
        self.notes = Notes()
        self.engine = Engine(self.cfg, self.db, self.rpc, self.market, self.whales, self.portfolio, self.notes)
        self.tracker = Tracker(self.cfg, self.db, self.rpc, self.market, self.whales, self.portfolio, self.engine,
                               self.notes)
        self._sig = 0
        self.runners = RunnerScanner(self.cfg, self.db, self.market, self.engine, self.whales, self.notes)
        self.scout = WhaleScout(self.cfg, self.db, self.rpc, self.market, self.whales, self.runners, self.notes, lambda: None)

    def run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def feed(self, wallet, tx, source="stream"):
        """Deliver one transaction for `wallet` as if the WebSocket reported it."""
        self._sig += 1
        sig = f"sig{self._sig:04d}" + "x" * 20
        self.rpc.txs[sig] = tx
        self.run(self.engine.process(wallet, sig, source))
        return sig


@pytest.fixture
def bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    b = Bot()
    yield b
    loop.close()


@pytest.fixture
def bot_with_wallet():
    from tests.helpers import ME
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    b = Bot(my_wallets=[ME])
    yield b
    loop.close()
