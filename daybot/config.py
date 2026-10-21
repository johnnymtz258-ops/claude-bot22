"""DayBot settings.

Keys and connection details come from `.env` (the same file FomoBot uses). Trading rules have sensible defaults,
can be set in `.env`, and can be changed while the bot runs (/set in Telegram or the dashboard); a live change is
saved in the database and wins over `.env`.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .util import num

ROOT = Path(__file__).resolve().parent.parent
AI_MODEL = "claude-opus-5-5"


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


@dataclass(frozen=True)
class Setting:
    name: str
    default: float
    help: str
    lo: float
    hi: float
    is_bool: bool = False


SETTINGS = {s.name: s for s in (
    # --- your risk rules: the trade check holds you to these -------------------------------------------------
    Setting("ACCOUNT_SIZE", 0, "Account size used for sizing and limits ($). 0 = read it from your synced accounts", 0, 100_000_000),
    Setting("RISK_PER_TRADE_PCT", 1.0, "Most you mean to lose on one trade, as % of the account (sizing and the size check)", 0.1, 10),
    Setting("DAILY_MAX_LOSS_PCT", 3.0, "Stop for the day once today's realized loss reaches this % of the account", 0.5, 20),
    Setting("MAX_TRADES_PER_DAY", 6, "More round trips than this in a day is overtrading — you get a warning", 1, 100),
    Setting("LOSS_STREAK_BREAK", 2, "After this many losing trades in a row, the bot tells you to take a break", 1, 10),
    Setting("BREAK_MINUTES", 30, "How long that break should be (minutes)", 5, 240),
    Setting("REVENGE_MINUTES", 15, "Buying the same stock again this soon after a loss on it is flagged as a revenge trade", 0, 120),
    Setting("OPTIONS_MAX_PREMIUM_PCT", 5, "Most premium in one options trade, as % of the account", 0.5, 50),
    Setting("ALLOW_0DTE", 0, "Allow same-day-expiry options without a red flag (1 = allow)", 0, 1, True),
    Setting("MIN_PRICE", 5, "Stocks under this price are treated as penny-ish: thin, gappy, easy to manipulate", 0, 1000),
    # --- the scanner -----------------------------------------------------------------------------------------
    Setting("GAP_MIN_PCT", 3, "Morning gappers: smallest gap from yesterday's close (%)", 0.5, 50),
    Setting("MIN_AVG_VOLUME", 1_000_000, "Gappers / in-play names need at least this average daily volume (shares)", 0, 1e9),
    Setting("WATCH_MAX", 25, "Most symbols the live scanner watches at once", 5, 60),
    Setting("ALERTS_ENABLED", 1, "Master switch for setup alerts (1 = on)", 0, 1, True),
    Setting("ALERT_UNPROVEN", 1, "Also send setups the backtest hasn't proven — silently and labelled 'unproven' (1 = on). Proven ones always ping", 0, 1, True),
    Setting("MAX_ALERTS_PER_HOUR", 6, "Most setup alerts per hour", 1, 60),
    Setting("ORB_MINUTES", 15, "Opening range length for the opening-range breakout (5, 15 or 30)", 5, 30),
    Setting("OPTION_IDEAS", 1, "Add a defined-risk option idea to setup alerts (1 = on)", 0, 1, True),
    Setting("OPTION_MIN_DTE", 1, "Option ideas: fewest days to expiry (0 allows same-day expiry)", 0, 60),
    Setting("OPTION_MAX_SPREAD_PCT", 10, "Options with a wider bid/ask spread than this % are flagged (you lose it every round trip)", 1, 50),
    Setting("OPTION_MIN_OI", 500, "Options with less open interest than this are flagged as thin", 0, 100_000),
    # --- schedule ------------------------------------------------------------------------------------------------
    Setting("MORNING_PLAN_HHMM", 900, "Morning game plan time, New York time as HHMM (900 = 9:00am). 0 = off", 0, 929),
    Setting("EOD_REPORT", 1, "End-of-day report card after the close (1 = on)", 0, 1, True),
    # --- paper + backtest ----------------------------------------------------------------------------------------
    Setting("PAPER_ACCOUNT", 25_000, "Paper autopilot account size ($): every setup alert is paper-traded as a market gauge", 1000, 10_000_000),
    Setting("SLIPPAGE_PCT", 0.05, "Cost per side used by the backtest and paper (% of price: spread + slippage)", 0, 2),
    Setting("BACKTEST_DAYS", 60, "Trading days of history each setup is replayed over", 10, 250),
    # --- your trades ---------------------------------------------------------------------------------------------
    Setting("CHECK_MY_TRADES", 1, "Check every trade you make the moment it fills (1 = on)", 0, 1, True),
    Setting("MONITOR_POSITIONS", 1, "Watch your open day trades: stop hit, target hit, VWAP lost, close coming (1 = on)", 0, 1, True),
    Setting("AI", 1, "Use Claude for chart screenshots, /ask and the coach's notes when ANTHROPIC_API_KEY is set (1 = on)", 0, 1, True),
)}


@dataclass
class Config:
    alpaca_key: str = ""
    alpaca_secret: str = ""
    data_feed: str = "iex"
    alpaca_paper: bool = True
    snaptrade_client_id: str = ""
    snaptrade_consumer_key: str = ""
    snaptrade_user_id: str = ""
    snaptrade_user_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_send_only: bool = False
    anthropic_key: str = ""
    ai_model: str = AI_MODEL
    finnhub_key: str = ""
    watchlist: list = field(default_factory=list)
    state_dir: Path = Path(".")
    db_path: Path = Path("daybot.db")
    dashboard_enabled: bool = True
    dashboard_port: int = 8788
    values: dict = field(default_factory=dict)

    def get(self, name: str) -> float:
        return self.values.get(name, SETTINGS[name].default)

    def flag(self, name: str) -> bool:
        return self.get(name) >= 0.5

    def set(self, name: str, value) -> float:
        """Validate and apply one setting. Raises ValueError with a readable message."""
        key = str(name or "").strip().upper()
        if key not in SETTINGS:
            raise ValueError(f"unknown setting {name!r}")
        spec = SETTINGS[key]
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

    @property
    def has_data(self) -> bool:
        return bool(self.alpaca_key and self.alpaca_secret)

    @property
    def has_broker(self) -> bool:
        return bool(self.snaptrade_client_id and self.snaptrade_consumer_key)

    @property
    def has_ai(self) -> bool:
        return bool(self.anthropic_key)


def default_state_dir() -> str:
    if sys.platform == "darwin":
        return "~/Library/Application Support/DayBot"
    return os.path.join(os.environ.get("XDG_DATA_HOME") or "~/.local/share", "DayBot")


def parse_watchlist(text: str) -> list[str]:
    out = []
    for part in str(text or "").replace(";", ",").replace(" ", ",").split(","):
        sym = part.strip().upper().lstrip("$")
        if sym and sym.replace(".", "").isalnum() and len(sym) <= 6 and sym not in out:
            out.append(sym)
    return out


def load(env_file: Path | None = None) -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file or ROOT / ".env", override=False)
    except ImportError:
        pass
    # its own Telegram bot (two programs can't both read commands from one bot); without one it can still send
    # through FomoBot's bot, but then it can't take commands
    own_token = _env("DAY_TELEGRAM_BOT_TOKEN")
    token = own_token or _env("TELEGRAM_BOT_TOKEN")
    state_text = _env("DAY_STATE_DIR")
    if "Library/Application Support" in state_text and sys.platform != "darwin":
        state_text = ""
    state_dir = Path(os.path.expanduser(state_text or default_state_dir()))
    feed = _env("ALPACA_DATA_FEED", "iex").lower()
    values = {}
    for name, spec in SETTINGS.items():
        raw = _env(name) or _env(f"DAY_{name}")
        if raw:
            values[name] = min(spec.hi, max(spec.lo, num(raw, spec.default)))
    return Config(
        alpaca_key=_env("ALPACA_API_KEY_ID") or _env("APCA_API_KEY_ID") or _env("ALPACA_KEY_ID"),
        alpaca_secret=_env("ALPACA_API_SECRET_KEY") or _env("APCA_API_SECRET_KEY") or _env("ALPACA_SECRET_KEY"),
        data_feed=feed if feed in {"iex", "sip"} else "iex",
        alpaca_paper=_env("ALPACA_PAPER", "true").lower() not in {"0", "false", "off", "no"},
        snaptrade_client_id=_env("SNAPTRADE_CLIENT_ID"),
        snaptrade_consumer_key=_env("SNAPTRADE_CONSUMER_KEY"),
        snaptrade_user_id=_env("SNAPTRADE_USER_ID"),
        snaptrade_user_secret=_env("SNAPTRADE_USER_SECRET"),
        telegram_token=token,
        telegram_chat_id=_env("DAY_TELEGRAM_CHAT_ID") or _env("TELEGRAM_CHAT_ID"),
        telegram_send_only=bool(token and not own_token),
        anthropic_key=_env("ANTHROPIC_API_KEY"),
        ai_model=_env("DAY_AI_MODEL") or AI_MODEL,
        finnhub_key=_env("FINNHUB_API_KEY"),
        watchlist=parse_watchlist(_env("DAY_WATCHLIST")),
        state_dir=state_dir,
        db_path=Path(os.path.expanduser(_env("DAY_DB") or str(state_dir / "daybot.db"))),
        dashboard_enabled=_env("DAY_DASHBOARD", "true").lower() not in {"0", "false", "off", "no"},
        dashboard_port=int(num(_env("DAY_DASHBOARD_PORT", "8788"), 8788)),
        values=values,
    )
