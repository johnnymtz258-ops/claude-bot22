"""/export: one zip with everything needed to review how DayBot and your trading are doing.

No keys or tokens are in it (they live only in .env), and account numbers are left out.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

from . import VERSION
from .config import SETTINGS

TABLES = {
    "trades": "select * from trades order by opened_ts",
    "fills": "select id, ts, day, symbol, underlying, kind, option_type, strike, expiry, side, effect, qty, price, "
             "multiplier, fee, source from fills order by ts",
    "checks": "select * from checks order by ts",
    "alerts": "select * from alerts order by ts",
    "paper": "select * from paper order by opened_ts",
    "backtests": "select * from backtests order by ts",
    "watch": "select * from watch order by day, score desc",
    "events": "select * from events where ts>=? order by ts",
    "ai_log": "select ts, kind, input_tokens, output_tokens, ok, note from ai_log order by ts",
    "crypto_trades": "select * from crypto_trades order by id",
    "crypto_equity": "select * from crypto_equity order by day",
    "crypto_pos": "select * from crypto_pos",
    "updown": "select * from updown order by ts",
    "updown_quotes": "select * from updown_quotes order by ts",
}


def build(db, cfg, now: float | None = None) -> bytes:
    now = now or time.time()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, sql in TABLES.items():
            rows = db.rows(sql, (int(now) - 14 * 86400,) if "?" in sql else ())
            z.writestr(f"{name}.json", json.dumps(rows, default=str))
        accounts = [{"institution": a["institution"], "margin": a["margin"], "total": a["total"],
                     "cash": a["cash"], "buying_power": a["buying_power"]} for a in db.rows("select * from accounts")]
        z.writestr("accounts.json", json.dumps(accounts))
        meta = {r["key"]: r["value"] for r in db.rows("select key, value from meta")}
        z.writestr("meta.json", json.dumps(meta))
        settings = {n: cfg.get(n) for n in SETTINGS}
        settings.update(watchlist=cfg.watchlist, data_feed=cfg.data_feed)
        z.writestr("settings.json", json.dumps(settings))
        z.writestr("README.txt", f"DayBot {VERSION} export, {time.strftime('%Y-%m-%d %H:%M')}.\n"
                                 "No keys or tokens; account numbers removed.\n")
    return buf.getvalue()


def write(db, cfg, folder: Path, now: float | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"daybot_review_{time.strftime('%Y%m%d_%H%M')}.zip"
    path.write_bytes(build(db, cfg, now))
    return path
