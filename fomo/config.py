"""All settings in one place.

Secrets and connection details come from `.env` only. Trading preferences have sensible
defaults, can be set in `.env`, and can be changed while the bot runs (`/set` in Telegram
or the dashboard); a live change is saved in the database and wins over `.env`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .util import is_address, num

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


@dataclass(frozen=True)
class Tunable:
    name: str
    default: float
    help: str
    lo: float
    hi: float
    is_bool: bool = False


# Everything a user may want to adjust. Names double as the .env keys.
TUNABLES = {t.name: t for t in (
    Tunable("MIN_WHALE_BUY_USD", 100, "Ignore whale buys smaller than this (dust, tests, airdrops)", 0, 1_000_000),
    Tunable("MAX_ENTRY_MC_USD", 30_000_000, "Ignore whale buys above this market cap (not early any more)", 10_000, 10_000_000_000),
    Tunable("LATE_CHASE_PCT", 50, "Price already this % above the whale's entry -> alert marked LATE", 5, 1000),
    Tunable("CONFLUENCE_HOURS", 6, "Window for counting several whales buying the same coin", 0.25, 72),
    Tunable("REPEAT_ALERT_HOURS", 12, "Same whale + same coin alerts at most once per this many hours", 0.25, 168),
    Tunable("MIN_SELL_ALERT_PCT", 25, "Only tell you about whale sells of at least this % of their bag", 1, 100),
    Tunable("COPY_MAX_HOLD_HOURS", 72, "Simulated copies close after this long if the whale never sells", 1, 720),
    Tunable("COPY_FEE_PCT", 1.0, "Fee+slippage per buy or sell used for simulated copies", 0, 20),
    Tunable("MANUAL_FEE_PCT", 0.0, "Fee applied to manual /bought /sold entries (wallet sync is always exact)", 0, 20),
    Tunable("RUG_LIQUIDITY_DROP_PCT", 70, "Liquidity drop (confirmed twice) that counts as a rug alarm", 20, 100),
    Tunable("TAKE_INITIAL_AT_X", 0, "Optional one-time 'take your initial out' note at this multiple (0 = off)", 0, 100),
    Tunable("PROFIT_LADDER", 1, "One nudge per coin when you reach 2x, 3x, 5x, 10x: take a slice, let the rest ride (1 = on)", 0, 1, True),
    Tunable("PROTECT_AFTER_X", 2, "Profit protector arms once a coin reaches this multiple of your cost (0 = off)", 0, 100),
    Tunable("PROTECT_TRAIL_PCT", 35, "Profit protector: warn when an armed coin falls this % from its peak", 10, 90),
    Tunable("WHALE_PICKS", 1, "Send you profitable wallets found in today's runners, with Follow buttons (1 = on)", 0, 1, True),
    Tunable("AUTO_WHALES", 0, "Follow those picks automatically instead of asking you (1 = on; off by default)", 0, 1, True),
    Tunable("AUTO_WHALE_LIMIT", 15, "Most whales the autopilot may follow at once (yours don't count)", 0, 50),
    Tunable("AUTO_SCOUT_HOURS", 6, "How often to look for new whale picks", 1, 48),
    Tunable("AUTO_MUTE_COLD_WHALES", 1, "Stop alerts from whales whose copies keep losing (1 = on)", 0, 1, True),
    Tunable("HIDE_WHALE_ONLY", 1, "Don't send whale buys of coins a few wallets hold with no real community (1 = on)", 0, 1, True),
    Tunable("RUNNER_ALERTS", 0, "Also alert high-activity community coins with no whale involved (1 = on; off by default — they underperformed whale alerts)", 0, 1, True),
    Tunable("RUNNER_MAX_PER_HOUR", 4, "Most community-runner alerts per hour", 0, 30),
    Tunable("RUNNER_MIN_MC_USD", 80_000, "Community runners: smallest market cap", 5_000, 100_000_000),
    Tunable("RUNNER_MAX_MC_USD", 8_000_000, "Community runners: largest market cap", 50_000, 1_000_000_000),
    Tunable("RUNNER_HOLD_HOURS", 24, "Community-runner copies are scored as if sold after this many hours", 1, 168),
    Tunable("QUIET_LOW_GRADE", 1, "Send grade-C alerts without sound (1 = on)", 0, 1, True),
    Tunable("ALERTS_ENABLED", 1, "Master switch for Telegram alerts (/pause, /resume)", 0, 1, True),
    Tunable("DAILY_SUMMARY_HOUR", 21, "Local hour for the daily summary (-1 = off)", -1, 23),
)}


@dataclass
class Config:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    helius_key: str = ""
    rpc_http: list = field(default_factory=list)
    rpc_wss: str = ""
    my_wallets: list = field(default_factory=list)
    state_dir: Path = Path(".")
    db_path: Path = Path("whales.db")
    legacy_db_path: Path = Path("fomo_master.db")
    dashboard_enabled: bool = True
    dashboard_port: int = 8787
    max_whales: int = 60
    values: dict = field(default_factory=dict)

    @property
    def uses_helius(self) -> bool:
        return bool(self.helius_key)

    def get(self, name: str) -> float:
        return self.values.get(name, TUNABLES[name].default)

    def flag(self, name: str) -> bool:
        return self.get(name) >= 0.5

    def set(self, name: str, value) -> float:
        """Validate and apply one tunable. Raises ValueError with a readable message."""
        key = str(name or "").strip().upper()
        if key not in TUNABLES:
            raise ValueError(f"unknown setting {name!r}")
        spec = TUNABLES[key]
        text = str(value).strip().lower()
        if spec.is_bool and text in {"on", "true", "yes"}:
            parsed = 1.0
        elif spec.is_bool and text in {"off", "false", "no"}:
            parsed = 0.0
        else:
            try:
                parsed = float(text.replace(",", "").replace("$", "").replace("%", ""))
            except ValueError:
                raise ValueError(f"{key} needs a number") from None
        if not spec.lo <= parsed <= spec.hi:
            raise ValueError(f"{key} must be between {spec.lo:g} and {spec.hi:g}")
        self.values[key] = parsed
        return parsed


def load(env_file: Path | None = None) -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file or ROOT / ".env", override=False)
    except ImportError:  # python-dotenv is in requirements; tolerate a bare test environment
        pass

    helius = _env("HELIUS_API_KEY")
    rpc_http = []
    if helius:
        rpc_http.append(f"https://mainnet.helius-rpc.com/?api-key={helius}")
    for url in [_env("SOLANA_RPC_HTTP")] + _env("SOLANA_RPC_FALLBACKS").split(","):
        url = url.strip()
        if url and url not in rpc_http:
            rpc_http.append(url)
    if not rpc_http:
        rpc_http.append("https://api.mainnet-beta.solana.com")
    rpc_wss = (f"wss://mainnet.helius-rpc.com/?api-key={helius}" if helius
               else _env("SOLANA_RPC_WSS") or "wss://api.mainnet-beta.solana.com")

    wallets_text = _env("MY_WALLETS") or _env("PUBLIC_SOLANA_WALLET_ADDRESS")
    my_wallets = [w.strip() for w in wallets_text.replace(";", ",").split(",") if is_address(w.strip())]

    state_dir = Path(os.path.expanduser(_env("FOMO_STATE_DIR") or "~/Library/Application Support/FomoBot"))
    db_path = Path(os.path.expanduser(_env("WHALE_DB") or str(state_dir / "whales.db")))
    legacy_db = Path(os.path.expanduser(_env("FOMO_STATE_DB") or str(state_dir / "fomo_master.db")))

    values = {}
    for name, spec in TUNABLES.items():
        raw = _env(name)
        if raw:
            v = num(raw, spec.default)
            values[name] = min(spec.hi, max(spec.lo, v))

    return Config(
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        helius_key=helius,
        rpc_http=rpc_http,
        rpc_wss=rpc_wss,
        my_wallets=my_wallets,
        state_dir=state_dir,
        db_path=db_path,
        legacy_db_path=legacy_db,
        dashboard_enabled=_env("DASHBOARD_ENABLED", "true").lower() not in {"0", "false", "off", "no"},
        dashboard_port=int(num(_env("DASHBOARD_PORT", "8787"), 8787)),
        max_whales=int(num(_env("MAX_WHALES", "60"), 60)),
        values=values,
    )
