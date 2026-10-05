"""Builders for realistic `getTransaction(jsonParsed)` payloads and fake network services."""
from __future__ import annotations

import asyncio
import time

from fomo.market import Market
from fomo.swaps import TOKEN_PROGRAM, USDC, WSOL

WHALE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
WHALE2 = "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS"
ME = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
SPONSOR = "Fomo1111111111111111111111111111111111111111"[:44]
MINT = "CAsHEDmemeTokenMint1111111111111111111pump"[:44]
MINT2 = "MoiNmemeTokenMint22222222222222222222222pump"[:44]
POOL = "Bonding1Curve11111111111111111111111111111111"[:44]
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
LAMPORTS = 1_000_000_000
RENT = 2_039_280


def ata(owner: str, mint: str) -> str:
    # deterministic fake account address (valid base58 characters, 44 chars)
    raw = (owner[:20] + mint[:24]).replace("0", "1").replace("O", "o").replace("I", "i").replace("l", "L")
    return raw[:44]


def token_row(index, mint, owner, raw, decimals=6, program=TOKEN_PROGRAM):
    return {"accountIndex": index, "mint": mint, "owner": owner, "programId": program,
            "uiTokenAmount": {"amount": str(raw), "decimals": decimals, "uiAmount": raw / 10 ** decimals,
                              "uiAmountString": str(raw / 10 ** decimals)}}


def build_tx(*, fee_payer: str, wallet: str, wallet_lamports_delta: int, fee: int = 5000,
             tokens: list | None = None, create_ata_for: list | None = None, close_ata_for: list | None = None,
             extra_signers: list | None = None, program: str = PUMP, block_time: int | None = None,
             err=None, parsed_instructions: bool = True):
    """tokens: [(owner, mint, pre_raw or None, post_raw or None, decimals)] — None = account absent.

    `wallet_lamports_delta` is the wallet's full native balance change (fees, deposits, everything).
    """
    tokens = tokens or []
    keys = [fee_payer] + ([wallet] if wallet != fee_payer else [])
    signer_set = {fee_payer, wallet, *(extra_signers or [])}
    token_accounts = []
    for owner, mint, pre, post, dec in tokens:
        acct = ata(owner, mint)
        if acct not in keys:
            keys.append(acct)
        token_accounts.append((keys.index(acct), owner, mint, pre, post, dec, acct))
    for extra in (POOL, program, TOKEN_PROGRAM, ATA_PROGRAM):
        if extra not in keys:
            keys.append(extra)
    pre_bal = [10 * LAMPORTS for _ in keys]
    post_bal = list(pre_bal)
    widx = keys.index(wallet)
    post_bal[widx] += wallet_lamports_delta
    if fee_payer != wallet:
        post_bal[0] -= fee
    instructions = [{"accounts": [], "data": "3Bxs4h24hBtQy9rw", "programId": "ComputeBudget111111111111111111111111111111",
                     "stackHeight": None}]
    for entry in create_ata_for or []:
        owner, mint = entry[0], entry[1]
        payer = entry[2] if len(entry) > 2 else (wallet if owner == wallet else fee_payer)
        info = {"account": ata(owner, mint), "mint": mint, "source": payer,
                "systemProgram": "11111111111111111111111111111111", "tokenProgram": TOKEN_PROGRAM, "wallet": owner}
        instructions.append({"parsed": {"info": info, "type": "createIdempotent"}, "program": "spl-associated-token-account",
                             "programId": ATA_PROGRAM, "stackHeight": None})
    instructions.append({"accounts": keys[:4], "data": "AJTQ2h9DXrBV8", "programId": program, "stackHeight": None})
    for owner, mint in close_ata_for or []:
        instructions.append({"parsed": {"info": {"account": ata(owner, mint), "destination": owner, "owner": owner},
                                        "type": "closeAccount"}, "program": "spl-token", "programId": TOKEN_PROGRAM,
                             "stackHeight": None})
    if not parsed_instructions:
        instructions = [{"accounts": [], "data": "x", "programId": program, "stackHeight": None}]
    account_keys = [{"pubkey": k, "signer": k in signer_set, "source": "transaction", "writable": True} for k in keys]
    return {
        "blockTime": block_time or int(time.time()),
        "slot": 300_000_000,
        "meta": {
            "err": err, "fee": fee, "innerInstructions": [], "loadedAddresses": {"readonly": [], "writable": []},
            "logMessages": [f"Program {program} invoke [1]", f"Program {program} success"],
            "preBalances": pre_bal, "postBalances": post_bal,
            "preTokenBalances": [token_row(i, m, o, pre, d) for i, o, m, pre, post, d, _ in token_accounts if pre is not None],
            "postTokenBalances": [token_row(i, m, o, post, d) for i, o, m, pre, post, d, _ in token_accounts if post is not None],
            "rewards": [], "status": {"Ok": None} if err is None else {"Err": err},
        },
        "transaction": {"message": {"accountKeys": account_keys, "instructions": instructions,
                                    "recentBlockhash": "9sHcv6xwn9YkB8nxTUGKDwPwNnmqVp5oAXxU8Fdkm4J6"},
                        "signatures": ["5sig"]},
        "version": 0,
    }


def pump_buy(wallet=WHALE, mint=MINT, sol=1.5, tokens=3_000_000.0, new=True, fee=105_000, pre_tokens=0.0,
             block_time=None):
    """A wallet buying `tokens` for `sol` SOL on Pump.fun (pays fee + ATA deposit when new)."""
    delta = -int(sol * LAMPORTS) - fee - (RENT if new else 0)
    pre_raw = None if new else int(pre_tokens * 1e6)
    post_raw = int((pre_tokens + tokens) * 1e6)
    return build_tx(fee_payer=wallet, wallet=wallet, wallet_lamports_delta=delta, fee=fee,
                    tokens=[(wallet, mint, pre_raw, post_raw, 6), (POOL, mint, 900_000_000_000_000,
                                                                   900_000_000_000_000 - int(tokens * 1e6), 6)],
                    create_ata_for=[(wallet, mint)] if new else None, block_time=block_time)


def pump_sell(wallet=WHALE, mint=MINT, sol=2.0, tokens=1_500_000.0, holding=3_000_000.0, close=False, fee=105_000,
              block_time=None):
    delta = int(sol * LAMPORTS) - fee + (RENT if close else 0)
    post_raw = None if close else int((holding - tokens) * 1e6)
    return build_tx(fee_payer=wallet, wallet=wallet, wallet_lamports_delta=delta, fee=fee,
                    tokens=[(wallet, mint, int(holding * 1e6), post_raw, 6), (POOL, mint, 800_000_000_000_000,
                                                                              800_000_000_000_000 + int(tokens * 1e6), 6)],
                    close_ata_for=[(wallet, mint)] if close else None, block_time=block_time)


def pair(mint=MINT, symbol="CASHED", price=0.00085, mc=850_000.0, liq=90_000.0, pair_addr=None, created_ms=None,
         **over):
    p = _pair(mint, symbol, price, mc, liq, pair_addr, created_ms)
    for key, value in over.items():  # e.g. buys_h1=50, trades_h24=100, change_h1=200, socials=0
        if key == "buys_h1":
            p["txns"]["h1"]["buys"] = value
        elif key == "trades_h24":
            p["txns"]["h24"] = {"buys": value // 2, "sells": value - value // 2}
        elif key == "change_h1":
            p["priceChange"]["h1"] = value
        elif key == "dex":
            p["dexId"] = value
        elif key == "socials":
            p["info"]["socials"], p["info"]["websites"] = [{"type": "twitter"}] * value, []
    return p


def _pair(mint, symbol, price, mc, liq, pair_addr, created_ms):
    return {"chainId": "solana", "dexId": "pumpswap", "url": f"https://dexscreener.com/solana/{pair_addr or 'P' + mint[1:]}",
            "pairAddress": pair_addr or ("P" + mint[1:]), "baseToken": {"address": mint, "name": symbol.title(), "symbol": symbol},
            "quoteToken": {"address": WSOL, "name": "Wrapped SOL", "symbol": "SOL"}, "priceNative": "0.0000057",
            "priceUsd": str(price), "txns": {"h1": {"buys": 420, "sells": 310}, "h24": {"buys": 5200, "sells": 4100}},
            "volume": {"h1": 120000, "h24": 900000},
            "info": {"imageUrl": "", "websites": [{"url": "https://x.io"}],
                     "socials": [{"type": "twitter", "url": "https://x.com/c"}, {"type": "telegram", "url": "https://t.me/c"}]},
            "priceChange": {"m5": 2.1, "h1": 12.5, "h24": 80}, "liquidity": {"usd": liq} if liq is not None else {},
            "fdv": mc, "marketCap": mc, "pairCreatedAt": created_ms or int((time.time() - 3 * 3600) * 1000)}


ON_CURVE = ['AKnL4NNf3DGWZJS6cPknBuEGnVsV4A4m5tgebLHaRSZ9', '9hSR6S7WPtxmTojgo6GG3k4yDPecgJY292j7xrsUGWBu',
            'GyGKxMyg1p9SsHfm15MkNUu1u9TN2JtTspcdmrtGUdse', 'EdmxWPmx2WH6WgFfTdu9xfkYf3k1g5wD1zccTVySEEh1',
            '8SFqwqnq4whPhs8icwHA2hQg3hUoN1qrCLK1SBx3WKwe', 'AKkzLhjhyFtM9j7WAhbaqYpFe49cXeJBg2kzLRC2PnNa',
            'GmaDrppBC7P5ARKV8g3djiwP89vz1jLK23V2GBjuAEGB', '2KW2XRd9kwqet15Aha2oK3tYvd3nWbTFH1MBiRAv1BE1',
            'J2xccRtuG43drESLYznHhLhQkLTdfepcKYbiQ9BsJVaf', '5Z6Ay5NEcbg3xhopc522sBCRXQujkTiuDRnHGfQdcnSf',
            '7v54NWdBtkjuAFJrLGsS2SXnuk8nKam81mZJeeYxVFi9', 'mBKqcnGotbsSb5vNrdyhzZ5EhqZdids9QYiTRckvi7v']
POOL_PDA = "83ZH8AYNycZZsMduTSrLDeXk4pWj6KxyuvT2cXZ2NuMX"  # off-curve, like a pool / bonding curve


class FakeRPC:
    def __init__(self):
        self.holders: dict[str, list] = {}  # mint -> [(owner, amount)] for holder-concentration checks
        self.txs: dict[str, dict] = {}
        self.mints: dict[str, dict] = {}
        self.balances: dict[tuple[str, str], float] = {}
        self.sigs: dict[str, list] = {}

    async def transaction(self, sig, wait=0):
        return self.txs.get(sig)

    async def mint_info(self, mint):
        return self.mints.get(mint, {"mint_authority": "", "freeze_authority": "", "decimals": 6,
                                     "supply": 1_000_000_000.0, "program": TOKEN_PROGRAM})

    async def token_balance(self, owner, mint):
        return self.balances.get((owner, mint))

    async def signatures(self, address, limit=100, before=None):
        rows = self.sigs.get(address, [])  # newest first, like the real RPC
        if before:
            idx = next((i for i, r in enumerate(rows) if r["signature"] == before), len(rows))
            rows = rows[idx + 1:]
        return rows[:limit]

    async def call(self, method, params, **kw):
        if method == "getTokenLargestAccounts" and params[0] in self.holders:
            return {"value": [{"address": f"{params[0][:8]}-acct{i}", "uiAmountString": str(amount)}
                              for i, (_, amount) in enumerate(self.holders[params[0]])]}
        if method == "getMultipleAccounts":
            owners = {f"{m[:8]}-acct{i}": o for m, rows in self.holders.items() for i, (o, _) in enumerate(rows)}
            return {"value": [{"data": {"parsed": {"info": {"owner": owners.get(a, "")}}}} for a in params[0]]}
        return None

    def health(self):
        return {"calls": 0, "errors": 0, "rate_limited": 0, "last_error": "", "last_ok_ts": 0}


class FakeMarket(Market):
    """Real Market logic with canned HTTP responses."""

    def __init__(self, db, rpc, sol=150.0):
        super().__init__(session=None, rpc=rpc, db=db)
        self.pairs: dict[str, dict] = {}
        self.sol = sol
        self.rug: dict | None = {"score": 5, "danger": [], "warn": []}
        self.watchlist: list[str] = []
        self.gecko_every = 0.0
        self.paths: dict[str, list] = {}   # mint -> [(ts, price)] served as GeckoTerminal candles

    async def price_path(self, mint, start_ts, end_ts):
        return [(t, p) for t, p in self.paths.get(mint, []) if start_ts <= t <= end_ts]

    def set_pair(self, **kw):
        p = pair(**kw)
        self.pairs[p["baseToken"]["address"]] = p
        self._cache.pop(p["baseToken"]["address"], None)
        return p

    async def _get(self, url, timeout=8.0):
        if "/tokens/v1/solana/" in url:
            wanted = url.rsplit("/", 1)[1].split(",")
            if wanted == [WSOL]:
                return [{"chainId": "solana", "baseToken": {"address": WSOL}, "quoteToken": {"address": USDC},
                         "priceUsd": str(self.sol), "liquidity": {"usd": 5e7}}]
            return [self.pairs[m] for m in wanted if m in self.pairs]
        if "rugcheck" in url:
            return None
        return None

    async def discovery_lists(self):
        return list(self.watchlist)

    async def rugcheck(self, mint, timeout=3.0):
        await asyncio.sleep(0.01)  # a real network call yields to other tasks
        return self.rug


class Notes:
    """Collects messages the bot would send to Telegram."""

    def __init__(self):
        self.sent: list[dict] = []
        self._id = 100

    async def __call__(self, text, *, buttons=None, silent=False, mint="", wallet="", kind=""):
        self._id += 1
        self.sent.append({"id": self._id, "text": text, "buttons": buttons, "silent": silent, "mint": mint,
                          "wallet": wallet, "kind": kind})
        return self._id

    def kinds(self):
        return [m["kind"] for m in self.sent]
