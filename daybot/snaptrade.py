"""SnapTrade, read-only: your Robinhood / Chase accounts, positions, balances and fills.

DayBot never trades through SnapTrade. The connection link it makes asks for read access only, and the client below
has no order endpoints at all.

Auth (personal keys): every request carries ?clientId=…&timestamp=… and a `Signature` header: base64 of
HMAC-SHA256(consumerKey, '{"content":<body or null>,"path":"<path>","query":"<query string>"}') with sorted keys —
exactly what SnapTrade's own SDK sends.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .alpaca import parse_ts
from .options import occ, parse_occ
from .util import num

BASE = "https://api.snaptrade.com"
STOCK_KINDS = {"stock", "adr", "etf", "mutualfund", "cef", "equity", "cs", "et", "ps"}


class SnapTradeError(Exception):
    def __init__(self, status: int, text: str, path: str = ""):
        super().__init__(f"SnapTrade {status} {path}: {text[:200]}")
        self.status = status
        self.text = text
        self.path = path


def sign(consumer_key: str, path: str, query: str, body) -> str:
    content = {"content": body if body not in (None, {}) else None, "path": path, "query": query}
    raw = json.dumps(content, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(consumer_key.encode(), raw.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _sym(value) -> str:
    """A symbol out of SnapTrade's nested symbol objects (or a plain string)."""
    if isinstance(value, str):
        return value.strip().upper()
    if isinstance(value, dict):
        for key in ("symbol", "raw_symbol", "ticker"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().upper()
            if isinstance(v, dict):
                inner = _sym(v)
                if inner:
                    return inner
    return ""


def _occ_from(opt: dict) -> dict:
    """Normalized option fields from a SnapTrade option symbol object."""
    ticker = str(opt.get("ticker") or opt.get("symbol") or "").replace(" ", "").upper()
    parsed = parse_occ(ticker) or {}
    underlying = _sym(opt.get("underlying_symbol") or opt.get("underlying") or {}) or parsed.get("underlying", "")
    kind = str(opt.get("option_type") or "").upper() or ("CALL" if parsed.get("type") == "call" else
                                                          "PUT" if parsed.get("type") == "put" else "")
    strike = num(opt.get("strike_price")) or num(parsed.get("strike"))
    expiry = str(opt.get("expiration_date") or parsed.get("expiry") or "")[:10]
    mult = 10.0 if opt.get("is_mini_option") else num(opt.get("multiplier"), 100) or 100.0

    symbol = ticker if parsed else (occ(underlying, expiry, kind[:1], strike) if underlying and expiry else ticker)
    return {"symbol": symbol, "underlying": underlying, "option_type": kind, "strike": strike, "expiry": expiry,
            "multiplier": mult}


def side_effect(action: str) -> tuple[str, str]:
    a = str(action or "").upper().replace(" ", "_")
    side = "BUY" if a.startswith("BUY") or a in {"BTO", "BTC"} else "SELL" if a.startswith("SELL") or a in {"STO", "STC"} else ""
    effect = "open" if "OPEN" in a or a in {"BTO", "STO"} else "close" if "CLOSE" in a or a in {"BTC", "STC"} else ""
    return side, effect


def normalize_order(o: dict, account_id: str) -> dict | None:
    """One executed order -> a fill dict (filled quantity at the average execution price)."""
    filled = num(o.get("filled_quantity"))
    status = str(o.get("status") or "").upper()
    if filled <= 0 and status == "EXECUTED":
        filled = num(o.get("total_quantity"))
    price = num(o.get("execution_price"))
    side, effect = side_effect(o.get("action"))
    if filled <= 0 or price <= 0 or not side:
        return None
    ts = parse_ts(o.get("time_executed")) or parse_ts(o.get("time_updated")) or parse_ts(o.get("time_placed"))
    fill = {"account_id": account_id, "order_id": str(o.get("brokerage_order_id") or ""), "ts": ts, "side": side,
            "effect": effect, "qty": filled, "price": price, "fee": 0.0, "source": "order"}
    opt = o.get("option_symbol")
    if isinstance(opt, dict) and (opt.get("ticker") or opt.get("strike_price")):
        fill.update(kind="option", **_occ_from(opt))
    else:
        sym = _sym(o.get("universal_symbol")) or _sym(o.get("symbol"))
        if not sym:
            return None
        fill.update(kind="stock", symbol=sym, underlying=sym, option_type="", strike=0.0, expiry="", multiplier=1.0)
    return fill


def normalize_activity(a: dict, account_id: str) -> dict | None:
    """A BUY / SELL / option expiration from the account's history -> a fill dict."""
    kind = str(a.get("type") or "").upper()
    if kind not in {"BUY", "SELL", "OPTIONEXPIRATION", "OPTIONASSIGNMENT", "OPTIONEXERCISE"}:
        return None
    units = abs(num(a.get("units")))
    price = num(a.get("price"))
    ts = parse_ts(a.get("trade_date")) or parse_ts(a.get("settlement_date"))
    if units <= 0 or not ts:
        return None
    if ts % 86400 == 0:
        ts += 12 * 3600                 # a date at midnight UTC: keep it on that New York day
    opt = a.get("option_symbol")
    fill = {"account_id": account_id, "order_id": f"act:{a.get('id')}", "ts": ts, "qty": units, "fee": abs(num(a.get("fee"))),
            "source": "history"}
    if isinstance(opt, dict) and (opt.get("ticker") or opt.get("strike_price")):
        o = _occ_from(opt)
        amount = abs(num(a.get("amount")))
        if amount and price and units and abs(price * units - amount) < abs(price * units * o["multiplier"] - amount):
            price = price / o["multiplier"]            # reported per contract: keep prices per share like orders
        fill.update(kind="option", **o)
        if kind in {"OPTIONEXPIRATION", "OPTIONASSIGNMENT", "OPTIONEXERCISE"}:
            # the contract is gone (worthless, or turned into shares that show up as their own buy/sell):
            # whatever was open closes at 0, long or short
            fill.update(side="EXPIRE", effect="close", price=0.0)
            return fill
    else:
        if kind not in {"BUY", "SELL"}:
            return None
        sym = _sym(a.get("symbol"))
        if not sym:
            return None
        fill.update(kind="stock", symbol=sym, underlying=sym, option_type="", strike=0.0, expiry="", multiplier=1.0)
    if price <= 0:
        return None
    side, effect = side_effect(kind)
    fill.update(side=side, effect=effect or fill.get("effect", ""), price=price)
    return fill


def normalize_position(r: dict, account_id: str) -> dict | None:
    inst = r.get("instrument") or {}
    kind = str(inst.get("kind") or "").lower()
    units = num(r.get("units"))
    if not units:
        return None
    price = num(r.get("price"))
    if kind == "option":
        o = _occ_from({**inst, "ticker": inst.get("symbol"), "underlying_symbol": inst.get("underlying") or {}})
        mult = o["multiplier"]
        cost = num(r.get("cost_basis"))
        avg = cost / (abs(units) * mult) if cost else 0.0
        if avg and price and avg > price * 20:         # cost basis given per contract instead of in total
            avg = avg / mult
        return {"account_id": account_id, "key": o["symbol"], "symbol": o["symbol"], "underlying": o["underlying"],
                "kind": "option", "option_type": o["option_type"], "strike": o["strike"], "expiry": o["expiry"],
                "qty": units, "avg_price": avg, "price": price, "multiplier": mult}
    if kind and kind not in STOCK_KINDS:
        return None                                   # crypto, bonds, futures: not what DayBot trades
    sym = _sym(inst) or _sym(r.get("symbol") or {})
    if not sym:
        return None
    cost = num(r.get("cost_basis"))
    avg = cost / abs(units) if cost else num(r.get("average_purchase_price"))
    return {"account_id": account_id, "key": sym, "symbol": sym, "underlying": sym, "kind": "stock", "option_type": "",
            "strike": 0.0, "expiry": "", "qty": units, "avg_price": avg, "price": price, "multiplier": 1.0}


class SnapTrade:
    def __init__(self, session: aiohttp.ClientSession | None, client_id: str, consumer_key: str,
                 user_id: str = "", user_secret: str = ""):
        self.session = session
        self.client_id, self.consumer_key = client_id, consumer_key
        self.user_id, self.user_secret = user_id, user_secret   # only for commercial keys
        self.base = BASE
        self.last_connections: list[dict] = []
        self.last_ok = 0.0
        self.last_fail = 0.0
        self.last_error = ""
        self.requests = 0
        self._lock = asyncio.Lock()
        self._last = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.client_id and self.consumer_key and self.session)

    @property
    def healthy(self) -> bool:
        return self.ok and self.last_fail <= self.last_ok

    async def _request(self, method: str, path: str, params: dict | None = None, body=None):
        if not self.ok:
            raise SnapTradeError(0, "no SnapTrade keys", path)
        for attempt in range(3):
            async with self._lock:                      # keep well under the per-account limit (10 a minute)
                wait = 0.5 - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            query = {"clientId": self.client_id, "timestamp": str(int(time.time()))}
            if self.user_id and self.user_secret:
                query.update(userId=self.user_id, userSecret=self.user_secret)
            query.update({k: (str(v).lower() if isinstance(v, bool) else str(v))
                          for k, v in (params or {}).items() if v not in (None, "")})
            qs = urlencode(query)
            headers = {"Signature": sign(self.consumer_key, path, qs, body), "accept": "application/json"}
            url = URL(f"{self.base}{path}?{qs}", encoded=True)
            self.requests += 1
            try:
                async with self.session.request(method, url, headers=headers,
                                                json=body if body is not None else None,
                                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    text = await resp.text()
                    if resp.status == 429 or resp.status >= 500:
                        retry = num(resp.headers.get("Retry-After"), 2 ** attempt)
                        self.last_error = f"{time.strftime('%H:%M:%S')} {path} {resp.status}"
                        await asyncio.sleep(min(max(retry, 1), 30))
                        continue
                    if resp.status >= 400:
                        self.last_error = f"{time.strftime('%H:%M:%S')} {path} {resp.status}: {text[:160]}"
                        self.last_fail = time.time()
                        raise SnapTradeError(resp.status, text, path)
                    self.last_ok = time.time()
                    return json.loads(text) if text else None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {path} {type(exc).__name__}"
                self.last_fail = time.time()
                await asyncio.sleep(2 ** attempt)
        raise SnapTradeError(0, self.last_error or "no answer", path)

    async def status(self) -> dict:
        return await self._request("GET", "/") or {}

    async def connect_link(self, reconnect: str = "", broker: str = "") -> str:
        """A Connection Portal link (valid 5 minutes) to connect Robinhood / Chase read-only."""
        body = {"connectionType": "read"}
        if reconnect:
            body["reconnect"] = reconnect
        if broker:
            body["broker"] = broker
        data = await self._request("POST", "/snapTrade/login", body=body) or {}
        return str(data.get("redirectURI") or "")

    async def connections(self) -> list[dict]:
        data = await self._request("GET", "/authorizations") or []
        out = []
        for c in data if isinstance(data, list) else []:
            brokerage = c.get("brokerage") or {}
            out.append({"id": str(c.get("id") or ""), "name": str(brokerage.get("display_name") or brokerage.get("name")
                                                                  or c.get("name") or ""),
                        "disabled": bool(c.get("disabled"))})
        return out

    @staticmethod
    def _investment(a: dict, institution: str) -> dict | None:
        if str(a.get("kind") or "investment") != "investment":
            return None                                 # bank / credit accounts aren't trading accounts
        net = a.get("net_value") or {}
        return {"id": str(a.get("id")), "name": str(a.get("display_name") or ""), "institution": institution,
                "number": str(a.get("masked_account_number") or ""), "margin": str(a.get("cash_or_margin") or ""),
                "total": num(net.get("amount") if isinstance(net, dict) else net)}

    @staticmethod
    def _legacy(a: dict, institution: str = "") -> dict:
        bal = (a.get("balance") or {}).get("total") or {}
        return {"id": str(a.get("id")), "name": str(a.get("name") or ""),
                "institution": str(a.get("institution_name") or institution or ""), "number": str(a.get("number") or ""),
                "margin": str(a.get("raw_type") or ""), "total": num(bal.get("amount") if isinstance(bal, dict) else bal)}

    async def accounts(self) -> list[dict]:
        """Every trading account under your linked brokerages. Asks each connection directly first (live — a
        brokerage linked a minute ago shows up), then the all-accounts list (refreshed once a day)."""
        found: dict[str, dict] = {}
        conns = await self.connections()                # bad keys raise here: nothing else would work either
        self.last_connections = conns
        for c in conns:
            rows = None
            try:
                data = await self._request("GET", f"/connections/{c['id']}/accounts") or {}
                rows = [x for x in (self._investment(a, c["name"]) for a in data.get("results") or []) if x]
            except SnapTradeError as exc:
                if exc.status in (0, 401):
                    raise
            if not rows:
                try:
                    data = await self._request("GET", f"/authorizations/{c['id']}/accounts") or []
                    rows = [self._legacy(a, c["name"]) for a in data if isinstance(a, dict)] if isinstance(data, list) else []
                except SnapTradeError as exc:
                    if exc.status in (0, 401):
                        raise
            for a in rows or []:
                found[a["id"]] = a
        if not found:
            names = {c["id"]: c["name"] for c in conns}
            try:
                data = await self._request("GET", "/accounts/all") or {}
                for a in data.get("results") or []:
                    x = self._investment(a, names.get(str(a.get("connection_id")), ""))
                    if x:
                        found[x["id"]] = x
            except SnapTradeError as exc:
                if exc.status in (0, 401):
                    raise
        if not found:
            try:
                data = await self._request("GET", "/accounts") or []
                for a in data if isinstance(data, list) else []:
                    x = self._legacy(a)
                    found[x["id"]] = x
            except SnapTradeError as exc:
                if exc.status in (0, 401):
                    raise
        return list(found.values())

    async def balances(self, account_id: str) -> dict:
        data = await self._request("GET", f"/accounts/{account_id}/balances") or []
        cash = bp = 0.0
        for b in data if isinstance(data, list) else []:
            code = str((b.get("currency") or {}).get("code") or "USD") if isinstance(b.get("currency"), dict) else "USD"
            if code == "USD":
                cash += num(b.get("cash"))
                bp += num(b.get("buying_power"))
        return {"cash": cash, "buying_power": bp}

    async def positions(self, account_id: str) -> list[dict]:
        data = await self._request("GET", f"/accounts/{account_id}/positions/all") or {}
        rows = data.get("results") if isinstance(data, dict) else data
        return [p for p in (normalize_position(r, account_id) for r in rows or []) if p]

    async def recent_fills(self, account_id: str) -> list[dict]:
        """Orders executed in the last 24 hours (real-time)."""
        data = await self._request("GET", f"/accounts/{account_id}/recentOrders", {"only_executed": True}) or {}
        rows = data.get("orders") if isinstance(data, dict) else data
        return [f for f in (normalize_order(o, account_id) for o in rows or []) if f]

    async def history(self, account_id: str, start_day: str, end_day: str) -> list[dict]:
        """Past buys/sells (daily-refreshed history) for the journal."""
        out, offset = [], 0
        for _ in range(20):
            data = await self._request("GET", f"/accounts/{account_id}/activities", {
                "startDate": start_day, "endDate": end_day, "offset": offset, "limit": 1000,
                "type": "BUY,SELL,OPTIONEXPIRATION,OPTIONASSIGNMENT,OPTIONEXERCISE"})
            rows = data.get("data") if isinstance(data, dict) else data
            rows = rows or []
            out.extend(f for f in (normalize_activity(a, account_id) for a in rows) if f)
            if len(rows) < 1000:
                break
            offset += len(rows)
        return out
