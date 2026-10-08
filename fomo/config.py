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
    Tunable("COPY_DELAY_SECONDS", 60, "How long after a whale's buy you usually get filled (used to score whales at your speed)", 0, 600),
    Tunable("BLOCK_FLIPPERS", 1, "Don't send entry alerts from flipper/bot whales (they sell within minutes) (1 = on)", 0, 1, True),
    Tunable("MIN_COPY_SCORE", 0.95, "Don't send a whale's buys once copying them at your speed averaged below this multiple (5+ coins)", 0, 10),
    Tunable("CONFIRM_SECONDS", 45, "Wait this long after a whale buy, then re-check the price before alerting (0 = alert at once)", 0, 300),
    Tunable("LATE_CHASE_PCT", 50, "Don't alert if the confirmed price is already this % above the whale's buy", 5, 1000),
    Tunable("DUMP_GATE_PCT", 20, "Don't alert if the confirmed price is already this % below the whale's buy", 5, 100),
    Tunable("CONFLUENCE_HOURS", 6, "Window for counting several whales buying the same coin", 0.25, 72),
    Tunable("REPEAT_ALERT_HOURS", 12, "Same whale + same coin alerts at most once per this many hours", 0.25, 168),
    Tunable("MIN_SELL_ALERT_PCT", 25, "Only tell you about whale sells of at least this % of their bag", 1, 100),
    Tunable("COPY_MAX_HOLD_HOURS", 72, "Simulated copies close after this long if the whale never sells", 1, 720),
    Tunable("COPY_FEE_PCT", 1.0, "Fee+slippage per buy or sell used for simulated copies", 0, 20),
    Tunable("MANUAL_FEE_PCT", 0.0, "Fee applied to manual /bought /sold entries (wallet sync is always exact)", 0, 20),
    Tunable("RUG_LIQUIDITY_DROP_PCT", 70, "Liquidity drop (confirmed twice) that counts as a rug alarm", 20, 100),
    Tunable("TAKE_INITIAL_AT_X", 0, "Optional one-time 'take your initial out' note at this multiple (0 = off)", 0, 100),
    Tunable("PROFIT_LADDER", 1, "One nudge per coin when you reach 2x, 3x, 5x, 10x: take a slice, let the rest ride (1 = on)", 0, 1, True),
    Tunable("PROTECT_AFTER_X", 1.5, "Profit protector arms once a coin reaches this multiple of your cost (0 = off)", 0, 100),
    Tunable("PROTECT_TRAIL_PCT", 35, "Profit protector: warn when an armed coin falls this % from its peak", 10, 90),
    Tunable("MICRO_MC_USD", 0, "Also treat coins below this market cap like bonding-curve coins (0 = off)", 0, 10_000_000),
    Tunable("QUALITY_GATE", 0, "Hold back grade C whale buys (off by default: in your data the held-back ones did as well as the sent ones). Small buys / tiny caps / grade C are always marked 🔸 (1 = hold back)", 0, 1, True),
    Tunable("QUALITY_MIN_BUY_USD", 500, "Below this whale buy size an alert is marked as a smaller signal", 0, 100000),
    Tunable("QUALITY_MIN_MC_USD", 30000, "Below this market cap an alert is marked as a smaller signal", 0, 10000000),
    Tunable("NEW_WHALE_WATCH_DAYS", 7, "Watch new auto-found / linked wallets this many days before their buys are sent (they still count for stacking and hype); 0 = off", 0, 60),
    Tunable("STACK_MIN_AGE_DAYS", 7, "Smart money stacking: 2+ of your wallets buying a coin at least this many days old gets a 🎯 alert", 0, 60),
    Tunable("FOLLOW_FUNDING", 1, "Follow the funding path: when a whale sends FUNDING_MIN_SOL+ to a fresh wallet, watch that wallet too (1 = on)", 0, 1, True),
    Tunable("FUNDING_MIN_SOL", 5, "Smallest SOL transfer from a whale to a fresh wallet that gets followed", 0.5, 10000),
    Tunable("HYPE_ALERTS", 1, "Hype scanner: alert coins at the start of a push — buy rush, volume surge, your whales, boosts, trending (1 = on)", 0, 1, True),
    Tunable("HYPE_MIN_SCORE", 45, "Hype scanner: score (0-100) a coin needs for an alert", 20, 100),
    Tunable("HYPE_TP_PCT", 30, "Hype alerts' exit: sell everything at this % profit", 5, 1000),
    Tunable("HYPE_STOP_PCT", 25, "Hype alerts' exit: stop loss %", 5, 90),
    Tunable("HYPE_MAX_MINUTES", 60, "Hype alerts' exit: sell whatever is left after this many minutes", 5, 1440),
    Tunable("HYPE_MAX_MC_USD", 5000000, "Hype scanner: ignore coins above this market cap (past early)", 50000, 1000000000),
    Tunable("HYPE_MIN_LIQ_USD", 10000, "Hype scanner: smallest liquidity a coin needs", 0, 10000000),
    Tunable("HYPE_MAX_H1_PCT", 100, "Hype scanner: don't alert coins already up more than this % in the last hour (late)", 20, 1000),
    Tunable("HYPE_MAX_PER_HOUR", 6, "Hype scanner: most hype alerts per hour", 1, 60),
    Tunable("RUNNER_SETUP", 1, "Flag the setup most of your 10x coins had — whale buys $1K+ of a just-graduated coin at $40K-250K MC — and send it even from flipper whales (1 = on)", 0, 1, True),
    Tunable("RUNNER_SETUP_MIN_BUY_USD", 1000, "Runner setup: smallest whale buy that counts", 100, 100000),
    Tunable("MICRO_ALERTS", 1, "Send buys of coins still on the pump.fun bonding curve (flagged ⚡; they either graduate and run or die — off = tracked, not sent)", 0, 1, True),
    Tunable("SCALP_PROTECT_AFTER_X", 1.5, "On SCALP coins you hold, the profit protector arms at this multiple", 0, 100),
    Tunable("SCALP_TRAIL_PCT", 25, "On SCALP coins you hold, warn when it falls this % from its peak", 10, 90),
    Tunable("STOP_LOSS_PCT", 40, "One warning when a coin you hold is this % below your cost (0 = off)", 0, 95),
    Tunable("QUIET_UPDATE_MINUTES", 15, "If nothing was sent for this long, send a short 'what I saw and skipped' update (0 = off)", 0, 1440),
    Tunable("ALERT_REPORTS", 1, "An hour after each alert, reply to it with how it actually went (1 = on)", 0, 1, True),
    Tunable("LIVE_CARD", 1, "Keep one pinned Telegram message updated with health, positions and the paper balance (1 = on)", 0, 1, True),
    Tunable("LIVE_CARD_SECONDS", 60, "How often the pinned live card refreshes", 20, 3600),
    Tunable("PAPER_TRADING", 1, "Paper autopilot: trade a pretend balance on every alert with the exit plan (1 = on)", 0, 1, True),
    Tunable("PAPER_START_USD", 1000, "Paper autopilot starting balance", 10, 10_000_000),
    Tunable("PAPER_TRADE_USD", 50, "Paper autopilot: size of each trade", 1, 1_000_000),
    Tunable("PAPER_MAX_OPEN", 8, "Paper autopilot: most coins held at once", 1, 50),
    Tunable("PAPER_SLIPPAGE_PCT", 3, "Paper autopilot: price you lose on every buy and sell (slippage + fees)", 0, 30),
    Tunable("PAPER_NOTIFY", 1, "Send a silent message when the paper autopilot sells (1 = on)", 0, 1, True),
    Tunable("LIVE_TRADING", 0, "LIVE autopilot: make the paper autopilot's trades for real from TRADING_PRIVATE_KEY's wallet (1 = on)", 0, 1, True),
    Tunable("LIVE_DRY_RUN", 1, "Live autopilot dry run: real quotes and signed transactions, but nothing is sent (1 = on)", 0, 1, True),
    Tunable("LIVE_TRADE_SOL", 0.05, "Live autopilot: SOL spent per buy", 0.001, 100),
    Tunable("LIVE_MAX_OPEN", 3, "Live autopilot: most coins held at once", 1, 20),
    Tunable("LIVE_DAILY_LOSS_SOL", 0.3, "Live autopilot: stop buying for the day once realized loss reaches this (0 = no limit)", 0, 1000),
    Tunable("LIVE_SLIPPAGE_BPS", 1500, "Live autopilot: max slippage in basis points (1500 = 15%; memecoins move fast)", 50, 5000),
    Tunable("LIVE_PRIORITY_LAMPORTS", 300_000, "Live autopilot: max priority fee per swap in lamports (300000 = 0.0003 SOL)", 0, 50_000_000),
    Tunable("WHALE_PICKS", 1, "Send you profitable wallets found in today's runners, with Follow buttons (1 = on)", 0, 1, True),
    Tunable("AUTO_WHALES", 1, "Follow whales the scanner proves copyable at your speed (holders, profitable copy replay) automatically (1 = on)", 0, 1, True),
    Tunable("AUTO_WHALE_LIMIT", 25, "Most whales the autopilot may follow at once (yours don't count)", 0, 50),
    Tunable("AUTO_SCOUT_HOURS", 1, "How often to look for new whale picks", 1, 48),
    Tunable("AUTO_MUTE_COLD_WHALES", 1, "Stop alerts from whales whose copies keep losing (1 = on)", 0, 1, True),
    Tunable("HIDE_WHALE_ONLY", 1, "Don't send whale buys of coins a few wallets hold with no real community (1 = on)", 0, 1, True),
    Tunable("RUNNER_ALERTS", 0, "Also alert high-activity community coins with no whale involved (1 = on; off by default — they underperformed whale alerts)", 0, 1, True),
    Tunable("RUNNER_MAX_PER_HOUR", 4, "Most community-runner alerts per hour", 0, 30),
    Tunable("RUNNER_MIN_MC_USD", 80_000, "Community runners: smallest market cap", 5_000, 100_000_000),
    Tunable("RUNNER_MAX_MC_USD", 8_000_000, "Community runners: largest market cap", 50_000, 1_000_000_000),
    Tunable("RUNNER_HOLD_HOURS", 24, "Community-runner copies are scored as if sold after this many hours", 1, 168),
    Tunable("REBUY_GUARD", 1, "Warn when you buy a coin every tracked whale has already sold (1 = on)", 0, 1, True),
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
    rpc_wss_fallback: str = ""
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


PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
PUBLIC_WSS = "wss://api.mainnet-beta.solana.com"


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
    if PUBLIC_RPC not in rpc_http:
        rpc_http.append(PUBLIC_RPC)   # last resort: if Helius stops answering (credits used up, outage), keep working
    rpc_wss = (f"wss://mainnet.helius-rpc.com/?api-key={helius}" if helius
               else _env("SOLANA_RPC_WSS") or PUBLIC_WSS)

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
        rpc_wss_fallback=PUBLIC_WSS if rpc_wss != PUBLIC_WSS else "",
        my_wallets=my_wallets,
        state_dir=state_dir,
        db_path=db_path,
        legacy_db_path=legacy_db,
        dashboard_enabled=_env("DASHBOARD_ENABLED", "true").lower() not in {"0", "false", "off", "no"},
        dashboard_port=int(num(_env("DASHBOARD_PORT", "8787"), 8787)),
        max_whales=int(num(_env("MAX_WHALES", "60"), 60)),
        values=values,
    )
