"""Compact, upload-friendly performance-review export for FomoBot.

export_data.command copies the whole database, a CSV of every table and the complete
bot.log, which quickly becomes too large to upload. This exporter instead:
- reads the shared database read-only, in one consistent snapshot (safe while the bot runs);
- exports only the tables needed to evaluate signals, shadow/paper trades and executions;
- skips bulky raw data (price snapshots, social text, Telegram maps, dedupe keys);
- includes only the last ~1 MB of bot.log;
- scrubs API keys/tokens from every exported value, including key-bearing URLs in errors;
- splits the result into parts no larger than FOMO_REVIEW_PART_MB (default 15 MB).

Only the database location and the key values to scrub are read from .env. No keypair,
seed phrase or key value is ever written to the export.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import sys
import time
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Tables that answer "does this strategy make money after costs, and where?"
REVIEW_TABLES = [
    "runs", "lane_governor",
    "signals", "evals", "signal_outcomes",
    "decision_ledger", "decision_outcomes", "missed_opportunities",
    "paper_positions", "positions", "guardian_events",
    "auto_trade_events", "execution_intents", "trade_failures",
    "copy_wallets", "copy_wallet_history", "copy_signals", "copy_paper_positions", "copy_live_positions",
]
# Personal/public-wallet columns that the analysis does not need.
DROP_COLUMNS = {"execution_intents": {"wallet_address"}}
# TELEGRAM_CHAT_ID is deliberately absent: it is only a number (useless without the bot
# token) and blanket-replacing it could corrupt numeric text columns such as raw amounts.
SECRET_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "BIRDEYE_API_KEY", "HELIUS_API_KEY", "JUPITER_API_KEY",
                   "X_BEARER_TOKEN", "PUMPPORTAL_API_KEY", "PUBLIC_SOLANA_WALLET_ADDRESS")
ROWS_PER_CHUNK = 20000
LOG_TAIL_BYTES = 1_000_000
REDACTED = "<redacted>"
# Key-bearing URL/header shapes, scrubbed even when the exact value is unknown.
_SECRET_PATTERNS = [
    (re.compile(r"(api[-_]?key=)[^&\s'\"<>)]+", re.I), r"\1" + REDACTED),
    (re.compile(r"(token=)[^&\s'\"<>)]+", re.I), r"\1" + REDACTED),
    (re.compile(r"(api\.telegram\.org/bot)[^/\s'\"]+", re.I), r"\1" + REDACTED),
    (re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.I), r"\1" + REDACTED),
    (re.compile(r"(x-api-key['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.I), r"\1" + REDACTED),
]
_TRIGGERS = ("key", "token", "bot", "bearer")


def _env_values(path: Path) -> dict:
    """Minimal .env reader (KEY=value, optional quotes and trailing ' # comment')."""
    out = {}
    try:
        text = path.read_text()
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        out[key.strip()] = value
    return out


def resolve_db(env: dict) -> Path:
    state = Path(os.path.expanduser(os.getenv("FOMO_STATE_DIR") or env.get("FOMO_STATE_DIR")
                                    or "~/Library/Application Support/FomoBot"))
    explicit = (os.getenv("FOMO_STATE_DB") or env.get("FOMO_STATE_DB") or "").strip()
    return Path(os.path.expanduser(explicit)) if explicit else state / "fomo_master.db"


class Scrubber:
    def __init__(self, env: dict):
        # Longest first so a value containing another value is fully removed.
        self.secrets = sorted({v for k, v in env.items() if k in SECRET_ENV_KEYS and len(v) >= 6}, key=len, reverse=True)

    def __call__(self, value):
        if not isinstance(value, str) or not value:
            return value
        for secret in self.secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        lower = value.lower()
        if any(t in lower for t in _TRIGGERS):
            for pattern, repl in _SECRET_PATTERNS:
                value = pattern.sub(repl, value)
        return value


class PartWriter:
    """Writes zip entries into numbered parts that each stay under a size limit."""

    def __init__(self, out_dir: Path, stem: str, limit_bytes: int):
        self.out_dir, self.stem, self.limit = out_dir, stem, max(256_000, int(limit_bytes))
        self.paths: list[Path] = []
        self.zf = None
        self.size = 0

    def _new_part(self):
        self.close()
        path = self.out_dir / f"{self.stem}_part{len(self.paths) + 1}.zip"
        self.zf = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9)
        self.paths.append(path)
        self.size = 0

    def add(self, name: str, data: bytes):
        # Deflate at the same level to know the entry's size before choosing a part.
        estimate = len(zlib.compress(data, 9)) + 128 + 2 * len(name)
        if self.zf is None or (self.size > 0 and self.size + estimate > self.limit):
            self._new_part()
        self.zf.writestr(name, data)
        self.size += estimate

    def close(self):
        if self.zf is not None:
            self.zf.close()
            self.zf = None

    def finalize(self) -> list[Path]:
        self.close()
        if len(self.paths) == 1:
            final = [self.paths[0].rename(self.out_dir / f"{self.stem}.zip")]
        else:
            n = len(self.paths)
            final = [p.rename(self.out_dir / f"{self.stem}_part{i}of{n}.zip") for i, p in enumerate(self.paths, 1)]
        self.paths = final
        return final


def _connect_readonly(db: Path) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        con.execute("select 1 from sqlite_master limit 1")
    except sqlite3.Error:
        con = sqlite3.connect(str(db), timeout=30)
    con.isolation_level = None
    return con


def _bot_version() -> str:
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (ROOT / "bot.py").read_text(), re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def _log_tail(path: Path, scrub: Scrubber) -> bytes:
    if not path.exists():
        return b""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > LOG_TAIL_BYTES:
            handle.seek(size - LOG_TAIL_BYTES)
            handle.readline()  # drop the partial first line
        text = handle.read().decode("utf-8", "replace")
    return "\n".join(scrub(line) for line in text.splitlines()).encode("utf-8")


def _cell(value):
    # 7 significant digits keeps even micro-cap prices exact enough for return math while
    # dropping float noise digits that make CSVs large and poorly compressible.
    return f"{value:.7g}" if isinstance(value, float) else value


def _csv_bytes(header, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _emit(writer: "PartWriter", stem: str, header, rows):
    """Add rows as one CSV, halving recursively until each piece fits in a part."""
    data = _csv_bytes(header, rows)
    if len(rows) > 1 and len(zlib.compress(data, 9)) > writer.limit * 0.9:
        mid = len(rows) // 2
        _emit(writer, stem + "_a", header, rows[:mid])
        _emit(writer, stem + "_b", header, rows[mid:])
        return
    writer.add(stem + ".csv", data)


def export(db: Path, out_dir: Path, part_mb: float, env: dict) -> tuple[list[Path], dict]:
    scrub = Scrubber(env)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    writer = PartWriter(out_dir, f"FOMO_REVIEW_{stamp}", int(part_mb * 1024 * 1024))
    con = _connect_readonly(db)
    counts = {}
    try:
        con.execute("begin")  # one consistent snapshot across all tables
        present = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        tables = [t for t in REVIEW_TABLES if t in present]
        for table in tables:
            counts[table] = con.execute(f'select count(*) from "{table}"').fetchone()[0]
        summary = [f"FomoBot review export {stamp}", f"bot folder version: {_bot_version()}",
                   f"database size: {db.stat().st_size / 1e6:.1f} MB", "", "rows per table:"]
        summary += [f"  {t}: {counts[t]}" for t in tables]
        skipped = sorted(present - set(tables))
        summary += ["", "not exported (raw/bulky or not needed): " + ", ".join(skipped)]
        writer.add("SUMMARY.txt", ("\n".join(summary) + "\n").encode("utf-8"))

        for table in tables:
            print(f"  exporting {table} ({counts[table]:,} rows)...", flush=True)
            cur = con.execute(f'select * from "{table}"')
            cols = [d[0] for d in cur.description]
            keep = [i for i, c in enumerate(cols) if c not in DROP_COLUMNS.get(table, set())]
            header = [cols[i] for i in keep]
            chunk_no = 0
            while True:
                rows = cur.fetchmany(ROWS_PER_CHUNK)
                if not rows and chunk_no > 0:
                    break
                cleaned = [[_cell(scrub(row[i])) for i in keep] for row in rows]
                stem = table if counts[table] <= ROWS_PER_CHUNK else f"{table}_{chunk_no:03d}"
                _emit(writer, stem, header, cleaned)
                chunk_no += 1
                if not rows:
                    break
        con.execute("commit")
        tail = _log_tail(ROOT / "bot.log", scrub)
        if tail:
            writer.add("bot_log_tail.txt", tail)
    except BaseException:
        # Never leave half-written zips behind: they cannot be opened and look like results.
        writer.close()
        for p in writer.paths:
            try:
                p.unlink()
            except OSError:
                pass
        raise
    finally:
        con.close()
    return writer.finalize(), counts


def main() -> int:
    env = _env_values(ROOT / ".env")
    db = resolve_db(env)
    if not db.exists():
        print(f"No bot database found at:\n{db}\nStart the bot once, or set FOMO_STATE_DIR in .env.")
        return 1
    try:
        part_mb = max(1.0, float(os.getenv("FOMO_REVIEW_PART_MB", "15")))
    except ValueError:
        part_mb = 15.0
    print(f"Reading {db} (read-only)...")
    started = time.time()
    print("This can take a few minutes for a large database. Keep this window open until it says Done.")
    try:
        paths, counts = export(db, ROOT, part_mb, env)
    except KeyboardInterrupt:
        print("\nStopped. Partial files were removed; run it again when ready.")
        return 1
    except Exception as exc:
        print(f"\nExport failed: {type(exc).__name__}: {exc}\nPartial files were removed. Send a screenshot of this window.")
        return 1
    print(f"Done in {time.time() - started:.0f}s. Rows exported: {sum(counts.values()):,}\n")
    print("Upload " + ("this file:" if len(paths) == 1 else f"all {len(paths)} files:"))
    for p in paths:
        print(f"  {p.name}  ({p.stat().st_size / 1e6:.1f} MB)")
    print(f"\nLocation: {ROOT}")
    print("API keys, tokens and your wallet address are scrubbed. No keypair or seed phrase is included.")
    return 0


if __name__ == "__main__":
    code = main()
    if sys.stdin.isatty():
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    sys.exit(code)
