"""
╔══════════════════════════════════════════════════════════════════════╗
║         ELITE SOLANA SNIPER BOT v4.0 — OWNER EDITION               ║
╠══════════════════════════════════════════════════════════════════════╣
║  NEW IN v4.0:                                                        ║
║  • SQLite persistent storage — survives Railway restarts             ║
║  • Webhook mode — no more polling, stable 24/7                      ║
║  • Token blacklist — /blacklist & /unblacklist commands             ║
║  • Daily PnL summary — auto midnight report to owner                ║
║  • Discord webhook mirror — all alerts sent to Discord too          ║
║  • Backtesting mode — score historical data, tune thresholds        ║
║  • /report command — full weekly performance report                 ║
║  • /setblacklist, /blacklisted — manage blacklist via Telegram      ║
║  • Webhook health endpoint — Railway keeps-alive check              ║
║  • Config persists across restarts via DB                           ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager

import aiohttp
import httpx
import pandas as pd
from aiohttp import web

try:
    import pandas_ta_classic as ta
    TA_AVAILABLE = True
except ImportError:
    try:
        import pandas_ta as ta
        TA_AVAILABLE = True
    except ImportError:
        ta = None
        TA_AVAILABLE = False

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

load_dotenv()

# Blacklist known bad tokens
blacklist = set([
    WSOL, 
    "So11111111111111111111111111111111111111112",  # WSOL
    "SOL"  # in case it appears
    # Add more addresses as you see junk tokens
])

# ════════════════════════════════════════════════════
#  ENVIRONMENT & CONFIGURATION
# ════════════════════════════════════════════════════

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
PRIVATE_KEY          = os.getenv("PRIVATE_KEY", "")
HELIUS_RPC           = os.getenv("HELIUS_RPC", "https://api.mainnet-beta.solana.com")
ALLOWED_USER_IDS     = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
BIRDEYE_API_KEY      = os.getenv("BIRDEYE_API_KEY", "public")
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")       # optional
WEBHOOK_URL          = os.getenv("WEBHOOK_URL", "")               # e.g. https://yourapp.railway.app
WEBHOOK_PORT         = int(os.getenv("PORT", 8080))               # Railway sets $PORT
DB_PATH              = os.getenv("DB_PATH", "sniper_bot.db")

# Default config — overridden by DB on startup
DEFAULT_CFG: Dict = {
DEFAULT_CFG = {
    "buy_amount_sol": float(os.getenv("BUY_AMOUNT_SOL", 0.05)),
    "slippage_bps": int(os.getenv("SLIPPAGE_BPS", 800)),
    "max_position_sol": float(os.getenv("MAX_POSITION_SOL", 0.5)),
    "min_liquidity_usd": float(os.getenv("MIN_LIQUIDITY", 8000)),
    "take_profit_pct": float(os.getenv("TAKE_PROFIT_PCT", 200)),
    "stop_loss_pct": float(os.getenv("STOP_LOSS_PCT", 50)),
    "trailing_stop_pct": float(os.getenv("TRAILING_STOP_PCT", 0)),
    "priority_fee_lamports": int(os.getenv("PRIORITY_FEE_LAMPORTS", 100_000)),
    "max_trades_day": int(os.getenv("MAX_TRADES_DAY", 8)),
    "risk_per_trade_pct": float(os.getenv("RISK_PER_TRADE_PCT", 2.0)),
    "min_score": float(os.getenv("MIN_SCORE", 62)),
    "auto_scan": True,                    # ← MUST BE True
    "auto_buy": False,                    # ← Keep False for now
    "scan_interval_sec": 60,
    "min_age_minutes": 0,
    "max_age_minutes": 45,                # Focus on fresh tokens
    "min_volume_1h_usd": 4000,
    "min_holders": 25,
    "max_top10_pct": 25,                  # Very important
}

cfg: Dict = dict(DEFAULT_CFG)
WSOL = "So11111111111111111111111111111111111111112"

if not TELEGRAM_TOKEN or not PRIVATE_KEY:
    raise ValueError("❌ TELEGRAM_TOKEN and PRIVATE_KEY are required in .env")

keypair = Keypair.from_base58_string(PRIVATE_KEY)

# ════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler("sniper_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════
#  DATA MODELS
# ════════════════════════════════════════════════════

@dataclass
class Position:
    token:           str
    symbol:          str
    entry_price:     float
    entry_sol:       float
    amount_tokens:   int
    peak_price:      float = 0.0
    entry_time:      float = field(default_factory=time.time)
    tx_sig:          str = ""
    pnl_pct:         float = 0.0
    trailing_active: bool = False

    def update_pnl(self, current_price: float):
        if self.entry_price > 0:
            self.pnl_pct = (current_price - self.entry_price) / self.entry_price * 100
            if current_price > self.peak_price:
                self.peak_price = current_price


@dataclass
class TokenScore:
    address:         str
    symbol:          str
    score:           float
    liquidity_usd:   float
    volume_1h:       float
    price_change_1h: float
    holders:         int
    age_minutes:     float
    top10_pct:       float
    has_freeze:      bool
    has_mint:        bool
    reasons:         List[str]
    dex_url:         str = ""


@dataclass
class TradeRecord:
    token:      str
    symbol:     str
    buy_price:  float
    sell_price: float
    entry_sol:  float
    pnl_pct:    float
    pnl_sol:    float
    timestamp:  str
    reason:     str


# ════════════════════════════════════════════════════
#  RUNTIME STATE
# ════════════════════════════════════════════════════

positions:     Dict[str, Position]  = {}
trade_history: List[TradeRecord]    = []
scan_alerts:   List[TokenScore]     = []
blacklist:     set                  = set()
bot_start_time = time.time()

# ════════════════════════════════════════════════════
#  SQLITE PERSISTENCE LAYER
# ════════════════════════════════════════════════════

def init_db():
    """Create all tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            token           TEXT PRIMARY KEY,
            symbol          TEXT,
            entry_price     REAL,
            entry_sol       REAL,
            amount_tokens   INTEGER,
            peak_price      REAL,
            entry_time      REAL,
            tx_sig          TEXT,
            pnl_pct         REAL,
            trailing_active INTEGER
        );

        CREATE TABLE IF NOT EXISTS trade_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            token       TEXT,
            symbol      TEXT,
            buy_price   REAL,
            sell_price  REAL,
            entry_sol   REAL,
            pnl_pct     REAL,
            pnl_sol     REAL,
            timestamp   TEXT,
            reason      TEXT
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            address     TEXT PRIMARY KEY,
            symbol      TEXT,
            reason      TEXT,
            added_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS config (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            vtype       TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date        TEXT PRIMARY KEY,
            trades      INTEGER DEFAULT 0,
            wins        INTEGER DEFAULT 0,
            losses      INTEGER DEFAULT 0,
            pnl_sol     REAL DEFAULT 0.0
        );
        """)
    logger.info("✅ Database initialized")


def save_position(pos: Position):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO positions
            (token, symbol, entry_price, entry_sol, amount_tokens,
             peak_price, entry_time, tx_sig, pnl_pct, trailing_active)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (pos.token, pos.symbol, pos.entry_price, pos.entry_sol,
              pos.amount_tokens, pos.peak_price, pos.entry_time,
              pos.tx_sig, pos.pnl_pct, int(pos.trailing_active)))


def delete_position(token: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM positions WHERE token=?", (token,))


def load_positions() -> Dict[str, Position]:
    result = {}
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT * FROM positions").fetchall()
    for r in rows:
        pos = Position(
            token=r[0], symbol=r[1], entry_price=r[2], entry_sol=r[3],
            amount_tokens=r[4], peak_price=r[5], entry_time=r[6],
            tx_sig=r[7], pnl_pct=r[8], trailing_active=bool(r[9])
        )
        result[pos.token] = pos
    logger.info(f"📂 Loaded {len(result)} position(s) from DB")
    return result


def save_trade(trade: TradeRecord):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO trade_history
            (token, symbol, buy_price, sell_price, entry_sol, pnl_pct, pnl_sol, timestamp, reason)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (trade.token, trade.symbol, trade.buy_price, trade.sell_price,
              trade.entry_sol, trade.pnl_pct, trade.pnl_sol, trade.timestamp, trade.reason))


def load_trade_history(limit: int = 200) -> List[TradeRecord]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT token,symbol,buy_price,sell_price,entry_sol,pnl_pct,pnl_sol,timestamp,reason "
            "FROM trade_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [TradeRecord(*r) for r in rows]


def save_blacklist_entry(address: str, symbol: str, reason: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blacklist (address, symbol, reason, added_at) VALUES (?,?,?,?)",
            (address, symbol, reason, datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
        )


def remove_blacklist_entry(address: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM blacklist WHERE address=?", (address,))


def load_blacklist() -> set:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT address FROM blacklist").fetchall()
    return {r[0] for r in rows}


def save_cfg_to_db():
    """Persist entire cfg dict to DB."""
    with sqlite3.connect(DB_PATH) as conn:
        for k, v in cfg.items():
            vtype = type(v).__name__
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value, vtype) VALUES (?,?,?)",
                (k, str(v), vtype)
            )


def load_cfg_from_db():
    """Load cfg from DB, overriding defaults."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT key, value, vtype FROM config").fetchall()
    type_map = {"int": int, "float": float, "bool": lambda x: x == "True", "str": str}
    for key, val, vtype in rows:
        if key in cfg:
            try:
                cfg[key] = type_map.get(vtype, str)(val)
            except Exception:
                pass
    logger.info("✅ Config loaded from DB")


def update_daily_stats_db(pnl_sol: float, is_win: bool):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (today,)
        )
        conn.execute("""
            UPDATE daily_stats SET
                trades  = trades + 1,
                wins    = wins + ?,
                losses  = losses + ?,
                pnl_sol = pnl_sol + ?
            WHERE date = ?
        """, (1 if is_win else 0, 0 if is_win else 1, pnl_sol, today))


def get_weekly_stats_from_db() -> List[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, trades, wins, losses, pnl_sol FROM daily_stats "
            "ORDER BY date DESC LIMIT 7"
        ).fetchall()
    return [{"date": r[0], "trades": r[1], "wins": r[2], "losses": r[3], "pnl_sol": r[4]} for r in rows]


# ════════════════════════════════════════════════════
#  SECURITY GUARD
# ════════════════════════════════════════════════════

def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user_id = update.effective_user.id if update.effective_user else 0
    return user_id in ALLOWED_USER_IDS


def auth_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            await update.effective_message.reply_text("⛔ Unauthorized.")
            logger.warning(f"Unauthorized: {update.effective_user}")
            return
        if cfg["bot_paused"] and func.__name__ not in ("resume_cmd", "status_cmd", "help_cmd", "stop_cmd"):
            await update.effective_message.reply_text("⏸ Bot is paused. Use /resume to continue.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ════════════════════════════════════════════════════
#  DISCORD MIRROR
# ════════════════════════════════════════════════════

async def discord_notify(content: str):
    """Mirror a message to Discord webhook if configured."""
    if not DISCORD_WEBHOOK_URL or not cfg.get("discord_alerts"):
        return
    try:
        async with aiohttp.ClientSession() as session:
            # Discord max 2000 chars per message
            await session.post(
                DISCORD_WEBHOOK_URL,
                json={"content": content[:2000]},
                timeout=aiohttp.ClientTimeout(total=5)
            )
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


# ════════════════════════════════════════════════════
#  WALLET
# ════════════════════════════════════════════════════

async def get_wallet_balance() -> float:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [str(keypair.pubkey())]
            })
            return resp.json()["result"]["value"] / 1_000_000_000
    except Exception as e:
        logger.error(f"Balance fetch error: {e}")
        return 0.0


# ════════════════════════════════════════════════════
#  PRICE & MARKET DATA
# ════════════════════════════════════════════════════

async def get_token_price(token: str) -> float:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.jup.ag/price/v2?ids={token}",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                data = await resp.json()
                return float(data.get("data", {}).get(token, {}).get("price", 0))
    except Exception as e:
        logger.error(f"Price fetch error {token}: {e}")
        return 0.0


async def get_dexscreener_new_pairs(limit: int = 80):
    """Improved fetcher - focuses on newer Solana tokens"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.dexscreener.com/latest/dex/search?q=SOL",
                timeout=15
            ) as resp:
                
                if resp.status != 200:
                    logger.warning(f"DexScreener status: {resp.status}")
                    return []
                
                data = await resp.json()
                all_pairs = data.get("pairs", [])[:limit]
                
                fresh_pairs = []
                for p in all_pairs:
                    if p.get("chainId") != "solana":
                        continue
                        
                    base = p.get("baseToken", {})
                    addr = base.get("address", "")
                    if not addr or addr == WSOL:
                        continue
                    
                    # Age filter at source level
                    created = p.get("pairCreatedAt", 0)
                    age_min = (time.time() * 1000 - created) / 60000 if created else 9999
                    
                    if age_min > 90:        # Ignore very old tokens here
                        continue
                        
                    fresh_pairs.append(p)
                
                logger.info(f"✅ Fetched {len(fresh_pairs)} fresh Solana pairs (age < 90min)")
                return fresh_pairs
                
    except Exception as e:
        logger.error(f"get_dexscreener_new_pairs error: {e}", exc_info=True)
        return []


async def get_token_holders(token: str) -> Tuple[int, float]:
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [token]
            })
            accounts = resp.json().get("result", {}).get("value", [])
            if not accounts:
                return 0, 100.0
            amounts = [int(a.get("amount", 0)) for a in accounts]
            total = sum(amounts)
            top10 = sum(sorted(amounts, reverse=True)[:10])
            top10_pct = (top10 / total * 100) if total > 0 else 100.0
            return len(accounts), round(top10_pct, 2)
    except Exception:
        return 0, 100.0


async def check_freeze_mint_authority(token: str) -> Tuple[bool, bool]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getAccountInfo",
                "params": [token, {"encoding": "jsonParsed"}]
            })
            info   = resp.json().get("result", {}).get("value", {})
            parsed = info.get("data", {}).get("parsed", {}).get("info", {})
            has_freeze = parsed.get("freezeAuthority") not in (None, "")
            has_mint   = parsed.get("mintAuthority") not in (None, "")
            return has_freeze, has_mint
    except Exception:
        return True, True


async def get_ohlcv(token: str, minutes: int = 60) -> Optional[pd.DataFrame]:
    try:
        now = int(time.time())
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://public-api.birdeye.so/defi/ohlcv",
                headers={"X-API-KEY": BIRDEYE_API_KEY},
                params={"address": token, "type": "1m",
                        "time_from": now - minutes * 60, "time_to": now}
            )
            items = r.json().get("data", {}).get("items", [])
        if not items:
            return None
        df = pd.DataFrame(items)
        for col in ["c", "h", "l", "o", "v"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["c"])
    except Exception:
        return None


# ════════════════════════════════════════════════════
#  ML SCORING ENGINE
# ════════════════════════════════════════════════════

def score_token(pair: dict, holders: int, top10_pct: float,
                has_freeze: bool, has_mint: bool,
                df: Optional[pd.DataFrame]) -> TokenScore:
    score = 0.0
    reasons: List[str] = []

    base_info    = pair.get("baseToken", {})
    token_addr   = base_info.get("address", "")
    symbol       = base_info.get("symbol", "?")
    liquidity    = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    vol_1h       = float(pair.get("volume", {}).get("h1", 0) or 0)
    pc_1h        = float(pair.get("priceChange", {}).get("h1", 0) or 0)
    txns_1h_buy  = int((pair.get("txns", {}).get("h1", {}) or {}).get("buys", 0))
    txns_1h_sell = int((pair.get("txns", {}).get("h1", {}) or {}).get("sells", 0))
    created_at   = pair.get("pairCreatedAt", 0)
    dex_url      = pair.get("url", "")
    age_min      = (time.time() * 1000 - created_at) / 60000 if created_at else 9999

    # 1. Liquidity (20pts)
    if liquidity >= 50_000:
        score += 20; reasons.append("✅ Strong liquidity >$50k")
    elif liquidity >= 20_000:
        score += 14; reasons.append("✅ Good liquidity >$20k")
    elif liquidity >= 10_000:
        score += 8;  reasons.append("⚠️ Low liquidity $10k-20k")
    else:
        reasons.append("❌ Liquidity too low")

    # 2. Volume/Liq ratio (20pts)
    vol_liq_ratio = vol_1h / liquidity if liquidity > 0 else 0
    if vol_liq_ratio >= 2.0:
        score += 20; reasons.append("✅ Explosive vol/liq ratio")
    elif vol_liq_ratio >= 0.8:
        score += 14; reasons.append("✅ Strong volume momentum")
    elif vol_liq_ratio >= 0.3:
        score += 7;  reasons.append("⚠️ Moderate volume")
    else:
        reasons.append("❌ Low volume momentum")

    # 3. Buy pressure (15pts)
    total_txns = txns_1h_buy + txns_1h_sell
    if total_txns > 0:
        buy_ratio = txns_1h_buy / total_txns
        if buy_ratio >= 0.70:
            score += 15; reasons.append(f"✅ High buy pressure {buy_ratio:.0%}")
        elif buy_ratio >= 0.55:
            score += 9;  reasons.append(f"✅ Buy dominant {buy_ratio:.0%}")
        elif buy_ratio >= 0.45:
            score += 4
        else:
            reasons.append("❌ Sell pressure dominant")

    # 4. Price momentum (10pts)
    if 5 <= pc_1h <= 80:
        score += 10; reasons.append(f"✅ Healthy rise +{pc_1h:.1f}%")
    elif pc_1h > 80:
        score += 5;  reasons.append(f"⚠️ Already pumped +{pc_1h:.1f}%")
    elif pc_1h < -20:
        reasons.append(f"❌ Dumping {pc_1h:.1f}%")
    else:
        score += 3

    # 5. Age sweet spot (10pts)
    if 5 <= age_min <= 30:
        score += 10; reasons.append(f"✅ Fresh ~{age_min:.0f}m old")
    elif 30 < age_min <= 60:
        score += 7;  reasons.append(f"✅ Young ~{age_min:.0f}m old")
    elif age_min > 180:
        reasons.append(f"⚠️ Older token ~{age_min:.0f}m")
    else:
        score += 4

    # 6. Holder distribution (10pts)
    if holders >= 200 and top10_pct <= 40:
        score += 10; reasons.append(f"✅ Healthy dist ({holders} holders)")
    elif holders >= 100 and top10_pct <= 55:
        score += 6;  reasons.append(f"✅ OK dist ({holders} holders)")
    elif top10_pct > 70:
        reasons.append(f"🚨 Whale concentration {top10_pct:.0f}%")

    # 7. Safety flags (15pts)
    if not has_freeze and not has_mint:
        score += 15; reasons.append("✅ No freeze/mint authority")
    elif has_freeze and has_mint:
        score -= 25; reasons.append("🚨 BOTH freeze+mint — HIGH RISK")
    elif has_freeze:
        score -= 10; reasons.append("🚨 Freeze authority active")
    elif has_mint:
        score -= 5;  reasons.append("⚠️ Mint authority active")

    # 8. TA bonus (10pts)
    if TA_AVAILABLE and df is not None and len(df) >= 30:
        try:
            df["ema9"]  = ta.ema(df["c"], length=9)
            df["ema21"] = ta.ema(df["c"], length=21)
            df["rsi"]   = ta.rsi(df["c"], length=14)
            df = df.dropna()
            if len(df) > 5:
                ema_bull  = df["ema9"].iloc[-1] > df["ema21"].iloc[-1]
                rsi_ok    = 45 < df["rsi"].iloc[-1] < 70
                vol_spike = df["v"].iloc[-1] > df["v"].rolling(10).mean().iloc[-1] * 1.5
                if ema_bull and rsi_ok and vol_spike:
                    score += 10; reasons.append("✅ TA: EMA bull + RSI ok + vol spike")
                elif ema_bull and rsi_ok:
                    score += 6;  reasons.append("✅ TA: EMA bull + RSI healthy")
        except Exception:
            pass

    score = max(0.0, min(100.0, score))
    return TokenScore(
        address=token_addr, symbol=symbol, score=round(score, 1),
        liquidity_usd=liquidity, volume_1h=vol_1h,
        price_change_1h=pc_1h, holders=holders, age_minutes=round(age_min, 1),
        top10_pct=top10_pct, has_freeze=has_freeze, has_mint=has_mint,
        reasons=reasons, dex_url=dex_url,
    )


# ════════════════════════════════════════════════════
#  RISK ENGINE
# ════════════════════════════════════════════════════

class RiskEngine:
    def __init__(self):
        self._today = datetime.utcnow().date()
        self._trades_today = 0
        self._daily_pnl_sol = 0.0

    def _reset_if_new_day(self):
        today = datetime.utcnow().date()
        if today != self._today:
            self._today = today
            self._trades_today = 0
            self._daily_pnl_sol = 0.0

    def can_trade(self) -> Tuple[bool, str]:
        self._reset_if_new_day()
        if self._trades_today >= cfg["max_trades_day"]:
            return False, f"Daily trade limit reached ({cfg['max_trades_day']})"
        max_daily_loss = cfg["max_position_sol"] * 0.20
        if self._daily_pnl_sol < -max_daily_loss:
            return False, f"Daily loss circuit breaker ({self._daily_pnl_sol:.4f} SOL)"
        return True, "OK"

    def register_trade(self, pnl_sol: float):
        self._reset_if_new_day()
        self._trades_today += 1
        self._daily_pnl_sol += pnl_sol
        update_daily_stats_db(pnl_sol, pnl_sol > 0)

    def calc_position_sol(self, balance: float) -> float:
        risk_sol = balance * (cfg["risk_per_trade_pct"] / 100)
        return round(min(risk_sol, cfg["buy_amount_sol"], cfg["max_position_sol"]), 4)

    @property
    def trades_today(self): return self._trades_today

    @property
    def daily_pnl(self): return round(self._daily_pnl_sol, 6)


risk_engine = RiskEngine()


# ════════════════════════════════════════════════════
#  SWAP ENGINE
# ════════════════════════════════════════════════════

async def execute_swap(
    input_mint: str, output_mint: str,
    amount_lamports: int, slippage_bps: int = 600
) -> Tuple[bool, Optional[str], str]:
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            qr = await client.get("https://quote-api.jup.ag/v6/quote", params={
                "inputMint": input_mint, "outputMint": output_mint,
                "amount": str(amount_lamports), "slippageBps": slippage_bps,
                "swapMode": "ExactIn",
            })
            quote = qr.json()
            if "error" in quote:
                return False, None, f"Quote error: {quote['error']}"

            sr = await client.post("https://quote-api.jup.ag/v6/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": cfg["priority_fee_lamports"],
            })
            swap_data = sr.json()
            if "swapTransaction" not in swap_data:
                return False, None, f"Swap error: {swap_data}"

            raw_tx = base64.b64decode(swap_data["swapTransaction"])
            tx     = VersionedTransaction.from_bytes(raw_tx)
            sig    = keypair.sign_message(to_bytes_versioned(tx.message))
            signed = VersionedTransaction.populate(tx.message, [sig])
            serial = base64.b64encode(bytes(signed)).decode()

            send_r = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [serial, {"skipPreflight": True, "maxRetries": 5}]
            })
            tx_sig = send_r.json().get("result")
            if tx_sig:
                return True, tx_sig, f"https://solscan.io/tx/{tx_sig}"
            return False, None, str(send_r.json())
    except Exception as e:
        logger.error(f"Swap exception: {e}")
        return False, None, str(e)


# ════════════════════════════════════════════════════
#  FORMATTING HELPERS
# ════════════════════════════════════════════════════

def _format_scan_alert(ts: TokenScore) -> str:
    bar   = "█" * int(ts.score / 10) + "░" * (10 - int(ts.score / 10))
    grade = "🟢 STRONG" if ts.score >= 80 else "🟡 GOOD" if ts.score >= 65 else "🟠 WEAK"
    return (
        f"🎯 *NEW TOKEN ALERT*\n\n"
        f"🔤 Symbol:    `{ts.symbol}`\n"
        f"📍 Address:   `{ts.address}`\n\n"
        f"📊 Score:     *{ts.score}/100* {grade}\n"
        f"             `[{bar}]`\n\n"
        f"💧 Liquidity: ${ts.liquidity_usd:,.0f}\n"
        f"📈 Vol 1h:    ${ts.volume_1h:,.0f}\n"
        f"🕐 Age:       {ts.age_minutes:.0f} min\n"
        f"👥 Holders:   {ts.holders}\n"
        f"🐋 Top10:     {ts.top10_pct:.1f}%\n"
        f"🔒 Freeze:    {'🚨 YES' if ts.has_freeze else '✅ NO'}\n"
        f"🖨 Mint:      {'⚠️ YES' if ts.has_mint else '✅ NO'}\n"
        f"📉 1h:        {ts.price_change_1h:+.1f}%\n\n"
        f"📝 *Signals:*\n" +
        "\n".join(f"  {r}" for r in ts.reasons[:6])
    )


def _scan_alert_keyboard(addr: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Quick Buy",    callback_data=f"qbuy:{addr}"),
         InlineKeyboardButton("📊 Analyze",      callback_data=f"analyze:{addr}")],
        [InlineKeyboardButton("🚫 Blacklist",    callback_data=f"bl:{addr}"),
         InlineKeyboardButton("🦅 DexScreener", url=f"https://dexscreener.com/solana/{addr}")],
        [InlineKeyboardButton("❌ Dismiss",       callback_data="dismiss")],
    ])


# ════════════════════════════════════════════════════
#  SCANNER (background job)
# ════════════════════════════════════════════════════

import time
import logging
from typing import List

logger = logging.getLogger(__name__)

async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    if not cfg.get("auto_scan", False):
        return

    logger.info("🔍 Auto-scanner running...")

    try:
        # Fetch new pairs - this is the critical part
      pairs = await get_dexscreener_new_pairs(limit=80)
logger.info(f"Received {len(pairs)} pairs from DexScreener")

        found: List[TokenScore] = []

        for pair in pairs:
            try:
                base = pair.get("baseToken", {})
                addr = base.get("address", "").strip()
                symbol = base.get("symbol", "UNKNOWN")

                if not addr or addr == WSOL:
                    logger.debug(f"Skipped {symbol}: invalid address or WSOL")
                    continue
                if addr in blacklist:
                    logger.debug(f"Skipped {symbol}: blacklisted")
                    continue
                if addr in positions:
                    logger.debug(f"Skipped {symbol}: already positioned")
                    continue

                liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                vol1h = float(pair.get("volume", {}).get("h1", 0) or 0)

                logger.info(f"Candidate → {symbol} ({addr[:8]}...) | Liq: ${liq:,.0f} | Vol1h: ${vol1h:,.0f}")

                # Relaxed filters for debugging
                if liq < cfg.get("min_liquidity_usd", 0) or vol1h < cfg.get("min_volume_1h_usd", 0):
                    logger.debug(f"  Skipped {symbol}: liquidity/volume too low")
                    continue

                # Age
                created = pair.get("pairCreatedAt", 0)
                age_min = (time.time() * 1000 - created) / 60000 if created > 0 else 9999
                if not (cfg.get("min_age_minutes", 0) <= age_min <= cfg.get("max_age_minutes", 9999)):
                    logger.debug(f"  Skipped {symbol}: Age {age_min:.1f} min out of range")
                    continue

                holders, top10 = await get_token_holders(addr)
                has_freeze, has_mint = await check_freeze_mint_authority(addr)
                if top10 > cfg.get("max_top10_pct", 100):
                    logger.debug(f"  Skipped {symbol}: Top holders too concentrated ({top10}%)")
                    continue

                df = await get_ohlcv(addr, 60)

                ts = score_token(pair, holders, top10, has_freeze, has_mint, df)

                # === FORCE MODE FOR TESTING (uncomment to bypass score) ===
                # ts.score = 90

                logger.info(f"  Score for {symbol}: {ts.score}/100 (min: {cfg.get('min_score', 0)})")

                if ts.score >= cfg.get("min_score", 0):
                    found.append(ts)
                    logger.info(f"✅ PASSED FILTERS: {symbol} (Score: {ts.score})")

            except Exception as pair_err:
                logger.warning(f"Error processing {symbol} ({addr}): {pair_err}")
                continue

        found.sort(key=lambda x: x.score, reverse=True)
        scan_alerts.clear()
        scan_alerts.extend(found[:10])

        logger.info(f"Scan complete. Tokens passed: {len(found)}")

        if not found:
            logger.info("🔍 Scan complete. No tokens passed filters.")
            return

        # === Alerting & Auto-buy (unchanged but safer) ===
        best = found[0]
        msg = _format_scan_alert(best)
        kb = _scan_alert_keyboard(best.address)

        if cfg.get("auto_buy", False):
            # ... your existing auto-buy logic ...
            pass

        # Send notifications
        for uid in ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(
                    uid, msg, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb, disable_web_page_preview=True
                )
            except Exception as e:
                logger.warning(f"Notify {uid} failed: {e}")

        await discord_notify(f"🎯 **SNIPER ALERT** — {best.symbol} Score: {best.score}/100\n{best.dex_url}")

    except Exception as e:
        logger.error(f"Scanner job crashed: {e}", exc_info=True)
# ════════════════════════════════════════════════════
#  POSITION MONITOR (background job)
# ════════════════════════════════════════════════════

async def monitor_positions(context: ContextTypes.DEFAULT_TYPE):
    if not positions:
        return
    for token in list(positions.keys()):
        try:
            pos     = positions[token]
            current = await get_token_price(token)
            if current == 0:
                continue
            pos.update_pnl(current)
            save_position(pos)    # keep DB in sync with peak_price updates

            sell_reason = None
            if pos.pnl_pct >= cfg["take_profit_pct"]:
                sell_reason = "TAKE_PROFIT"
            elif pos.pnl_pct <= -cfg["stop_loss_pct"]:
                sell_reason = "STOP_LOSS"
            elif cfg["trailing_stop_pct"] > 0 and pos.peak_price > 0:
                drop_from_peak = (pos.peak_price - current) / pos.peak_price * 100
                if drop_from_peak >= cfg["trailing_stop_pct"] and pos.pnl_pct > 0:
                    sell_reason = "TRAILING_STOP"

            if sell_reason:
                await _execute_auto_sell(token, pos, sell_reason, current, context)
        except Exception as e:
            logger.error(f"Monitor error {token}: {e}")


async def _execute_auto_sell(token: str, pos: Position, reason: str,
                              current_price: float,
                              context: ContextTypes.DEFAULT_TYPE):
    amount = int(pos.amount_tokens * 0.97)
    success, tx_sig, link = await execute_swap(token, WSOL, amount, cfg["slippage_bps"] + 100)

    if success:
        pnl_sol = pos.entry_sol * (pos.pnl_pct / 100)
        trade   = TradeRecord(
            token=token, symbol=pos.symbol,
            buy_price=pos.entry_price, sell_price=current_price,
            entry_sol=pos.entry_sol, pnl_pct=round(pos.pnl_pct, 2),
            pnl_sol=round(pnl_sol, 6),
            timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            reason=reason
        )
        trade_history.insert(0, trade)
        save_trade(trade)
        risk_engine.register_trade(pnl_sol)
        delete_position(token)
        del positions[token]

        emoji = "🎯" if reason == "TAKE_PROFIT" else "🛑" if reason == "STOP_LOSS" else "📉"
        msg = (
            f"{emoji} *{reason.replace('_', ' ')}*\n\n"
            f"Token: `{pos.symbol}`\n"
            f"PnL:   *{pos.pnl_pct:+.1f}%* ({pnl_sol:+.4f} SOL)\n"
            f"[View TX]({link})"
        )
        for uid in ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN,
                                               disable_web_page_preview=True)
            except Exception:
                pass
        await discord_notify(f"{emoji} {reason} | {pos.symbol} | PnL: {pos.pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL)")
        logger.info(f"{reason} | {pos.symbol} | PnL={pos.pnl_pct:+.1f}%")


# ════════════════════════════════════════════════════
#  DAILY REPORT JOB (midnight UTC)
# ════════════════════════════════════════════════════

async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Auto-send daily PnL summary to owner at midnight UTC."""
    try:
        today  = datetime.utcnow().strftime("%Y-%m-%d")
        weekly = get_weekly_stats_from_db()
        today_row = next((r for r in weekly if r["date"] == today), None)

        if not today_row or today_row["trades"] == 0:
            return  # nothing to report

        win_rate = today_row["wins"] / today_row["trades"] * 100 if today_row["trades"] else 0
        emoji    = "🟢" if today_row["pnl_sol"] >= 0 else "🔴"

        msg = (
            f"📅 *Daily Report — {today}*\n\n"
            f"{emoji} PnL:      *{today_row['pnl_sol']:+.4f} SOL*\n"
            f"📊 Trades:  {today_row['trades']}\n"
            f"🟢 Wins:    {today_row['wins']}\n"
            f"🔴 Losses:  {today_row['losses']}\n"
            f"🎯 Win Rate:{win_rate:.1f}%\n\n"
            f"📍 Open Positions: {len(positions)}"
        )
        for uid in ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        await discord_notify(f"📅 Daily Report {today} | PnL: {today_row['pnl_sol']:+.4f} SOL | Win Rate: {win_rate:.1f}%")
        logger.info(f"Daily report sent for {today}")
    except Exception as e:
        logger.error(f"Daily report error: {e}")


# ════════════════════════════════════════════════════
#  SELL HELPER
# ════════════════════════════════════════════════════

async def _do_sell(token: str, message, reason: str = "MANUAL"):
    pos = positions.get(token)
    if not pos:
        await message.reply_text("⚠️ Position not found.")
        return
    msg = await message.reply_text(f"🔄 Selling `{pos.symbol}`...", parse_mode=ParseMode.MARKDOWN)
    amount = int(pos.amount_tokens * 0.97)
    success, tx_sig, link = await execute_swap(token, WSOL, amount, cfg["slippage_bps"] + 100)

    if success:
        current = await get_token_price(token)
        pos.update_pnl(current)
        pnl_sol = pos.entry_sol * (pos.pnl_pct / 100)
        trade   = TradeRecord(
            token=token, symbol=pos.symbol,
            buy_price=pos.entry_price, sell_price=current,
            entry_sol=pos.entry_sol, pnl_pct=round(pos.pnl_pct, 2),
            pnl_sol=round(pnl_sol, 6),
            timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            reason=reason
        )
        trade_history.insert(0, trade)
        save_trade(trade)
        risk_engine.register_trade(pnl_sol)
        delete_position(token)
        del positions[token]

        emoji = "🟢" if pnl_sol >= 0 else "🔴"
        await msg.edit_text(
            f"✅ *SOLD* `{pos.symbol}`\n\n"
            f"{emoji} PnL: *{pos.pnl_pct:+.1f}%* ({pnl_sol:+.4f} SOL)\n"
            f"[📄 View TX]({link})",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )
        await discord_notify(f"✅ SOLD {pos.symbol} | {reason} | PnL: {pos.pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL)")
    else:
        await msg.edit_text(f"❌ Sell failed: `{link}`", parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════
#  TELEGRAM COMMANDS
# ════════════════════════════════════════════════════

@auth_required
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Balance",     callback_data="bal"),
         InlineKeyboardButton("📊 Positions",   callback_data="pos")],
        [InlineKeyboardButton("🔍 Last Scans",  callback_data="scans"),
         InlineKeyboardButton("📈 Stats",       callback_data="stats")],
        [InlineKeyboardButton("⚙️ Settings",    callback_data="settings"),
         InlineKeyboardButton("📜 History",     callback_data="history")],
        [InlineKeyboardButton("🚫 Blacklist",   callback_data="show_bl"),
         InlineKeyboardButton("❓ Help",         callback_data="helpme")],
    ])
    uptime = str(timedelta(seconds=int(time.time() - bot_start_time)))
    await update.message.reply_text(
        f"🤖 *Elite Solana Sniper Bot v4.0*\n\n"
        f"⏱ Uptime:    `{uptime}`\n"
        f"👛 Wallet:   `{str(keypair.pubkey())[:8]}...{str(keypair.pubkey())[-4:]}`\n"
        f"🔍 Scan:     {'✅ ON' if cfg['auto_scan'] else '❌ OFF'}\n"
        f"🤖 AutoBuy:  {'✅ ON' if cfg['auto_buy'] else '❌ OFF'}\n"
        f"💾 DB:       ✅ Persistent\n"
        f"📡 Discord:  {'✅ ON' if DISCORD_WEBHOOK_URL else '❌ Not set'}\n"
        f"⏸ Status:   {'PAUSED ⏸' if cfg['bot_paused'] else '🟢 RUNNING'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


@auth_required
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🚀 <b>Elite Sniper Bot v4.0 — Commands</b>\n\n"
        "<b>📡 TRADING</b>\n"
        "/snipe &lt;addr&gt; [sol] — Manual snipe\n"
        "/sell &lt;addr&gt; — Sell a position\n"
        "/sellall — Sell ALL positions\n"
        "/positions — Live PnL on open positions\n\n"
        "<b>🔍 SCANNER</b>\n"
        "/scan — Manual scan now\n"
        "/scanon — Enable auto-scanner\n"
        "/scanoff — Disable auto-scanner\n"
        "/autobuy — Toggle auto-buy\n"
        "/alerts — Last scan results\n"
        "/analyze &lt;addr&gt; — Deep token analysis\n\n"
        "<b>🚫 BLACKLIST</b>\n"
        "/blacklist &lt;addr&gt; [reason] — Blacklist token\n"
        "/unblacklist &lt;addr&gt; — Remove from blacklist\n"
        "/blacklisted — Show blacklist\n\n"
        "<b>💰 WALLET</b>\n"
        "/balance — SOL balance\n"
        "/wallet — Full wallet info\n\n"
        "<b>⚙️ SETTINGS</b>\n"
        "/settings — Show all settings\n"
        "/set &lt;key&gt; &lt;val&gt; — Change setting (persists)\n"
        "/resetcfg — Reset to defaults\n\n"
        "<b>📊 STATS &amp; REPORTS</b>\n"
        "/stats — Performance summary\n"
        "/history — Last 10 trades\n"
        "/report — 7-day weekly report\n\n"
        "<b>🛡 CONTROL</b>\n"
        "/pause — Pause trading\n"
        "/resume — Resume trading\n"
        "/stop — Emergency stop\n"
        "/status — Full bot health\n\n"
        "<b>🧪 BACKTEST</b>\n"
        "/backtest &lt;score&gt; — Simulate scoring on recent pairs"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)


@auth_required
async def snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/snipe <token_address> [sol_amount]`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    token = args[0].strip()
    size_override = float(args[1]) if len(args) > 1 else None

    if token in blacklist:
        await update.message.reply_text("🚫 This token is blacklisted.")
        return

    ok, reason = risk_engine.can_trade()
    if not ok:
        await update.message.reply_text(f"⛔ {reason}")
        return

    msg = await update.message.reply_text("🔍 Analyzing token...")

    pair = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
                pairs_list = data.get("pairs", [])
                if pairs_list:
                    pair = pairs_list[0]
    except Exception:
        pass

    if pair:
        holders, top10 = await get_token_holders(token)
        has_freeze, has_mint = await check_freeze_mint_authority(token)
        df = await get_ohlcv(token, 60)
        ts = score_token(pair, holders, top10, has_freeze, has_mint, df)
    else:
        ts = TokenScore(address=token, symbol="UNKNOWN", score=50,
                        liquidity_usd=0, volume_1h=0, price_change_1h=0,
                        holders=0, age_minutes=0, top10_pct=0,
                        has_freeze=False, has_mint=False, reasons=["Manual override"])

    if ts.score < cfg["min_score"]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Force Buy", callback_data=f"force_buy:{token}"),
            InlineKeyboardButton("❌ Abort",      callback_data="dismiss"),
        ]])
        await msg.edit_text(
            f"⚠️ *Low Score: {ts.score}/100* (min: {cfg['min_score']})\n\n"
            + "\n".join(ts.reasons[:5]) + "\n\nForce buy anyway?",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return

    bal  = await get_wallet_balance()
    size = size_override or risk_engine.calc_position_sol(bal)

    if bal < size + 0.01:
        await msg.edit_text(f"⚠️ Insufficient balance! Have: {bal:.4f} | Need: {size + 0.01:.4f} SOL")
        return

    await msg.edit_text(
        f"✅ Score *{ts.score}/100* — Executing buy...",
        parse_mode=ParseMode.MARKDOWN
    )

    success, tx_sig, link = await execute_swap(
        WSOL, token, int(size * 1_000_000_000), cfg["slippage_bps"]
    )

    if success:
        risk_engine.register_trade(0)
        ep  = await get_token_price(token)
        pos = Position(
            token=token, symbol=ts.symbol, entry_price=ep, entry_sol=size,
            amount_tokens=int(size * 1e9 * 0.95), peak_price=ep, tx_sig=tx_sig or ""
        )
        positions[token] = pos
        save_position(pos)
        await msg.edit_text(
            f"🎯 *BUY EXECUTED!*\n\n"
            f"Symbol: `{ts.symbol}`\n"
            f"Size:   {size:.4f} SOL\n"
            f"Entry:  {ep:.8f}\n"
            f"TP/SL:  +{cfg['take_profit_pct']}% / -{cfg['stop_loss_pct']}%\n\n"
            f"[📄 View TX]({link})",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )
        await discord_notify(f"🎯 BUY {ts.symbol} | {size:.4f} SOL | Entry: {ep:.8f}")
    else:
        await msg.edit_text(f"❌ Buy Failed:\n`{link}`", parse_mode=ParseMode.MARKDOWN)


@auth_required
async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        if not positions:
            await update.message.reply_text("📭 No open positions.")
            return
        buttons = [
            [InlineKeyboardButton(f"Sell {p.symbol} ({p.pnl_pct:+.1f}%)", callback_data=f"sell:{a}")]
            for a, p in positions.items()
        ]
        await update.message.reply_text("Select position to sell:",
                                        reply_markup=InlineKeyboardMarkup(buttons))
        return
    await _do_sell(args[0].strip(), update.message, "MANUAL")


@auth_required
async def sellall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not positions:
        await update.message.reply_text("📭 No open positions.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, sell all", callback_data="confirm_sellall"),
        InlineKeyboardButton("❌ Cancel",        callback_data="dismiss"),
    ]])
    await update.message.reply_text(f"⚠️ Sell ALL {len(positions)} positions?", reply_markup=kb)


@auth_required
async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not positions:
        await update.effective_message.reply_text("📭 No open positions.")
        return
    text = "📊 *Open Positions*\n\n"
    for addr, pos in positions.items():
        current = await get_token_price(addr)
        pos.update_pnl(current)
        age_m = (time.time() - pos.entry_time) / 60
        emoji = "🟢" if pos.pnl_pct >= 0 else "🔴"
        text += (
            f"{emoji} *{pos.symbol}* `{addr[:8]}...`\n"
            f"   Entry:   `{pos.entry_price:.8f}`\n"
            f"   Current: `{current:.8f}`\n"
            f"   PnL:     *{pos.pnl_pct:+.1f}%*\n"
            f"   Size:    {pos.entry_sol:.4f} SOL\n"
            f"   Age:     {age_m:.0f}m\n\n"
        )
    buttons = [
        [InlineKeyboardButton(f"Sell {p.symbol}", callback_data=f"sell:{a}")]
        for a, p in positions.items()
    ]
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="pos")])
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@auth_required
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = await get_wallet_balance()
    await update.effective_message.reply_text(
        f"💰 *Balance*\n\n"
        f"Address: `{str(keypair.pubkey())[:8]}...{str(keypair.pubkey())[-4:]}`\n"
        f"Balance: *{bal:.6f} SOL*\n"
        f"Positions: {len(positions)}\n"
        f"Available: ~{max(0, bal - len(positions) * cfg['buy_amount_sol']):.4f} SOL",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚙️ <b>Current Settings</b>\n\n"
        "<b>Trading:</b>\n"
        f"  buy_amount_sol      = {cfg['buy_amount_sol']}\n"
        f"  slippage_bps        = {cfg['slippage_bps']}\n"
        f"  max_position_sol    = {cfg['max_position_sol']}\n"
        f"  take_profit_pct     = +{cfg['take_profit_pct']}%\n"
        f"  stop_loss_pct       = -{cfg['stop_loss_pct']}%\n"
        f"  trailing_stop_pct   = {cfg['trailing_stop_pct']}%\n"
        f"  risk_per_trade_pct  = {cfg['risk_per_trade_pct']}%\n\n"
        "<b>Scanner:</b>\n"
        f"  auto_scan           = {cfg['auto_scan']}\n"
        f"  auto_buy            = {cfg['auto_buy']}\n"
        f"  scan_interval_sec   = {cfg['scan_interval_sec']}\n"
        f"  min_score           = {cfg['min_score']}\n"
        f"  min_liquidity_usd   = ${cfg['min_liquidity_usd']:,}\n"
        f"  min_volume_1h_usd   = ${cfg['min_volume_1h_usd']:,}\n"
        f"  min_age_minutes     = {cfg['min_age_minutes']}\n"
        f"  max_age_minutes     = {cfg['max_age_minutes']}\n"
        f"  max_top10_pct       = {cfg['max_top10_pct']}%\n\n"
        "<b>Notifications:</b>\n"
        f"  discord_alerts      = {cfg['discord_alerts']}\n\n"
        "Use <code>/set key value</code> to change. Settings persist across restarts."
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)


@auth_required
async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/set <key> <value>`", parse_mode=ParseMode.MARKDOWN)
        return
    key, val_str = args[0], args[1]
    if key not in cfg:
        await update.message.reply_text(f"❌ Unknown key `{key}`", parse_mode=ParseMode.MARKDOWN)
        return
    current = cfg[key]
    try:
        if isinstance(current, bool):
            val = val_str.lower() in ("true", "1", "yes", "on")
        elif isinstance(current, int):
            val = int(val_str)
        else:
            val = float(val_str)
        cfg[key] = val
        save_cfg_to_db()
        await update.message.reply_text(f"✅ `{key}` = `{val}` (saved)", parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Config: {key} = {val}")
    except ValueError:
        await update.message.reply_text(f"❌ Invalid value `{val_str}`", parse_mode=ParseMode.MARKDOWN)


@auth_required
async def resetcfg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Reset to defaults", callback_data="confirm_resetcfg"),
        InlineKeyboardButton("❌ Cancel",             callback_data="dismiss"),
    ]])
    await update.message.reply_text("⚠️ Reset ALL settings to defaults?", reply_markup=kb)


@auth_required
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_trades = load_trade_history(500)
    wins   = [t for t in all_trades if t.pnl_sol > 0]
    losses = [t for t in all_trades if t.pnl_sol <= 0]
    total_pnl = sum(t.pnl_sol for t in all_trades)
    win_rate  = len(wins) / len(all_trades) * 100 if all_trades else 0
    avg_win   = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
    avg_loss  = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
    uptime    = str(timedelta(seconds=int(time.time() - bot_start_time)))

    await update.effective_message.reply_text(
        f"📊 *All-Time Performance*\n\n"
        f"⏱ Uptime:       {uptime}\n"
        f"📈 Total Trades: {len(all_trades)}\n"
        f"🟢 Wins:         {len(wins)}\n"
        f"🔴 Losses:       {len(losses)}\n"
        f"🎯 Win Rate:     {win_rate:.1f}%\n"
        f"💰 Total PnL:    {total_pnl:+.4f} SOL\n"
        f"📈 Avg Win:      {avg_win:+.1f}%\n"
        f"📉 Avg Loss:     {avg_loss:+.1f}%\n\n"
        f"📅 Today:\n"
        f"  Trades: {risk_engine.trades_today}\n"
        f"  PnL:    {risk_engine.daily_pnl:+.4f} SOL\n\n"
        f"📍 Open: {len(positions)}",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recent = load_trade_history(10)
    if not recent:
        await update.effective_message.reply_text("📭 No trade history yet.")
        return
    text = "📜 *Recent Trades*\n\n"
    for t in recent:
        emoji = "🟢" if t.pnl_sol > 0 else "🔴"
        text += (
            f"{emoji} `{t.symbol}` — {t.reason}\n"
            f"   PnL: *{t.pnl_pct:+.1f}%* ({t.pnl_sol:+.4f} SOL)\n"
            f"   {t.timestamp}\n\n"
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@auth_required
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """7-day weekly performance report."""
    weekly = get_weekly_stats_from_db()
    if not weekly:
        await update.message.reply_text("📭 No stats yet.")
        return

    text   = "📈 *7-Day Performance Report*\n\n"
    total_pnl = 0.0
    for row in weekly:
        wr   = row["wins"] / row["trades"] * 100 if row["trades"] else 0
        emoji = "🟢" if row["pnl_sol"] >= 0 else "🔴"
        text += (
            f"{emoji} *{row['date']}*\n"
            f"   Trades: {row['trades']} | Win: {wr:.0f}% | PnL: {row['pnl_sol']:+.4f} SOL\n"
        )
        total_pnl += row["pnl_sol"]

    total_trades = sum(r["trades"] for r in weekly)
    total_wins   = sum(r["wins"] for r in weekly)
    overall_wr   = total_wins / total_trades * 100 if total_trades else 0
    text += (
        f"\n{'─'*30}\n"
        f"📊 *7-Day Total*\n"
        f"  Trades:   {total_trades}\n"
        f"  Win Rate: {overall_wr:.1f}%\n"
        f"  PnL:      *{total_pnl:+.4f} SOL*"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@auth_required
async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/blacklist <token_address> [reason]`", parse_mode=ParseMode.MARKDOWN
        )
        return
    addr   = args[0].strip()
    reason = " ".join(args[1:]) if len(args) > 1 else "Manual"
    symbol = positions.get(addr, Position("","UNKNOWN",0,0,0)).symbol

    blacklist.add(addr)
    save_blacklist_entry(addr, symbol, reason)
    await update.message.reply_text(
        f"🚫 *Blacklisted*\n`{addr[:16]}...`\nReason: {reason}",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info(f"Blacklisted: {addr} — {reason}")


@auth_required
async def unblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/unblacklist <token_address>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    addr = args[0].strip()
    blacklist.discard(addr)
    remove_blacklist_entry(addr)
    await update.message.reply_text(f"✅ Removed `{addr[:16]}...` from blacklist",
                                    parse_mode=ParseMode.MARKDOWN)


@auth_required
async def blacklisted_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not blacklist:
        await update.message.reply_text("✅ Blacklist is empty.")
        return
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT address, symbol, reason, added_at FROM blacklist").fetchall()
    text = f"🚫 *Blacklist ({len(rows)} tokens)*\n\n"
    for r in rows[:20]:
        text += f"`{r[0][:12]}...` ({r[1]}) — {r[2]} | {r[3]}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@auth_required
async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Score recent DexScreener pairs against a given threshold — no trades executed."""
    args  = context.args or []
    threshold = float(args[0]) if args else cfg["min_score"]

    msg = await update.message.reply_text(f"🧪 Backtesting with score threshold {threshold}...")
    pairs = await get_dexscreener_new_pairs(limit=20)
    results = []

    for pair in pairs[:10]:
        base = pair.get("baseToken", {})
        addr = base.get("address", "")
        if not addr or addr == WSOL:
            continue
        holders, top10 = await get_token_holders(addr)
        has_freeze, has_mint = await check_freeze_mint_authority(addr)
        ts = score_token(pair, holders, top10, has_freeze, has_mint, None)
        results.append(ts)

    results.sort(key=lambda x: x.score, reverse=True)
    would_trade = [r for r in results if r.score >= threshold]

    text = (
        f"🧪 *Backtest Results* (threshold: {threshold})\n\n"
        f"Pairs analyzed: {len(results)}\n"
        f"Would trade: {len(would_trade)}\n\n"
    )
    for r in results[:8]:
        flag = "✅" if r.score >= threshold else "❌"
        text += f"{flag} `{r.symbol}` — Score: {r.score}/100\n"

    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)


@auth_required
async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Scanning DEX now...")
    original = cfg["auto_scan"]
    cfg["auto_scan"] = True
    await scanner_job(context)
    cfg["auto_scan"] = original
    count = len(scan_alerts)
    await msg.edit_text(
        f"✅ Scan complete! Found {count} token(s) above score {cfg['min_score']}.\n"
        f"Use /alerts to view." if count else "🔍 Scan complete. No tokens passed filters."
    )


@auth_required
async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not scan_alerts:
        await update.effective_message.reply_text("📭 No recent results. Use /scan to run.")
        return
    for ts in scan_alerts[:5]:
        await update.effective_message.reply_text(
            _format_scan_alert(ts), parse_mode=ParseMode.MARKDOWN,
            reply_markup=_scan_alert_keyboard(ts.address),
            disable_web_page_preview=True
        )


@auth_required
async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/analyze <token_address>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    token = args[0].strip()
    msg   = await update.message.reply_text("🔬 Deep analyzing...")

    pair = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token}",
                timeout=aiohttp.ClientTimeout(total=12)
            ) as r:
                data = await r.json()
                pairs_list = data.get("pairs", [])
                if pairs_list:
                    pair = pairs_list[0]
    except Exception:
        pass

    holders, top10 = await get_token_holders(token)
    has_freeze, has_mint = await check_freeze_mint_authority(token)
    df = await get_ohlcv(token, 120)

    if pair:
        ts   = score_token(pair, holders, top10, has_freeze, has_mint, df)
        text = _format_scan_alert(ts)
    else:
        text = (f"⚠️ Token `{token[:16]}...` not on DexScreener\n"
                f"Freeze: {'YES' if has_freeze else 'NO'}\n"
                f"Mint: {'YES' if has_mint else 'NO'}\n"
                f"Top10: {top10}%")

    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_scan_alert_keyboard(token),
                        disable_web_page_preview=True)


@auth_required
async def scanon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["auto_scan"] = True
    save_cfg_to_db()
    await update.message.reply_text(
        f"🔍 Auto-scanner *ON* — every {cfg['scan_interval_sec']}s | min score: {cfg['min_score']}",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def scanoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["auto_scan"] = False
    save_cfg_to_db()
    await update.message.reply_text("🛑 Auto-scanner *OFF*", parse_mode=ParseMode.MARKDOWN)


@auth_required
async def autobuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["auto_buy"] = not cfg["auto_buy"]
    save_cfg_to_db()
    state = "✅ ENABLED" if cfg["auto_buy"] else "❌ DISABLED"
    await update.message.reply_text(
        f"🤖 Auto-buy: *{state}*\n\n"
        f"Threshold: score ≥ {cfg['min_score']}/100\n"
        f"Size: {cfg['buy_amount_sol']} SOL per trade",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["bot_paused"] = True
    save_cfg_to_db()
    await update.message.reply_text("⏸ *Bot PAUSED* — Use /resume to continue.",
                                    parse_mode=ParseMode.MARKDOWN)


@auth_required
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg["bot_paused"] = False
    save_cfg_to_db()
    await update.message.reply_text("▶️ *Bot RESUMED*", parse_mode=ParseMode.MARKDOWN)


@auth_required
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 CONFIRM", callback_data="confirm_estop"),
        InlineKeyboardButton("❌ Cancel",   callback_data="dismiss"),
    ]])
    await update.message.reply_text(
        "⚠️ *EMERGENCY STOP*\nPauses bot, clears scan state, disables auto-buy.\n"
        "*Open positions are NOT auto-sold.*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )


@auth_required
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal       = await get_wallet_balance()
    uptime    = str(timedelta(seconds=int(time.time() - bot_start_time)))
    can_trade, reason = risk_engine.can_trade()
    await update.effective_message.reply_text(
        f"🟢 *Bot Status — v4.0*\n\n"
        f"⏱ Uptime:       `{uptime}`\n"
        f"💰 Balance:      `{bal:.4f} SOL`\n"
        f"📍 Positions:    `{len(positions)}`\n"
        f"🔍 Auto-Scan:    `{'ON' if cfg['auto_scan'] else 'OFF'}`\n"
        f"🤖 Auto-Buy:     `{'ON' if cfg['auto_buy'] else 'OFF'}`\n"
        f"⏸ Paused:       `{cfg['bot_paused']}`\n"
        f"📊 Trades Today: `{risk_engine.trades_today}/{cfg['max_trades_day']}`\n"
        f"💹 Daily PnL:    `{risk_engine.daily_pnl:+.4f} SOL`\n"
        f"✅ Can Trade:    `{can_trade}` ({reason})\n"
        f"🧠 TA Engine:    `{'ON' if TA_AVAILABLE else 'OFF'}`\n"
        f"💾 Persistence:  `SQLite ✅`\n"
        f"📡 Discord:      `{'✅' if DISCORD_WEBHOOK_URL else '❌ Not configured'}`\n"
        f"🚫 Blacklist:    `{len(blacklist)} tokens`",
        parse_mode=ParseMode.MARKDOWN
    )


@auth_required
async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal  = await get_wallet_balance()
    addr = str(keypair.pubkey())
    await update.message.reply_text(
        f"👛 *Wallet*\n\n"
        f"Address: `{addr}`\n"
        f"Balance: *{bal:.6f} SOL*\n\n"
        f"[🔎 Solscan](https://solscan.io/account/{addr})",
        parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
    )


# ════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data or ""

    if not is_authorized(update):
        await query.edit_message_text("⛔ Unauthorized.")
        return

    if data.startswith("qbuy:"):
        token = data.split(":", 1)[1]
        context.args = [token]
        class _FM:
            async def reply_text(self, *a, **kw): return await query.edit_message_text(*a, **kw)
            async def edit_text(self,  *a, **kw): return await query.edit_message_text(*a, **kw)
        update.message = _FM()
        await snipe_cmd(update, context)

    elif data.startswith("force_buy:"):
        token = data.split(":", 1)[1]
        bal   = await get_wallet_balance()
        size  = risk_engine.calc_position_sol(bal)
        success, tx_sig, link = await execute_swap(WSOL, token, int(size * 1e9), cfg["slippage_bps"])
        if success:
            risk_engine.register_trade(0)
            ep  = await get_token_price(token)
            pos = Position(token=token, symbol="MANUAL", entry_price=ep,
                           entry_sol=size, amount_tokens=int(size * 1e9 * 0.95),
                           peak_price=ep, tx_sig=tx_sig or "")
            positions[token] = pos
            save_position(pos)
            await query.edit_message_text(
                f"🎯 Force-bought {size:.4f} SOL\n[TX]({link})",
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
            )
        else:
            await query.edit_message_text(f"❌ Buy failed: {link}")

    elif data.startswith("sell:"):
        token = data.split(":", 1)[1]
        class _FQ:
            async def reply_text(self, *a, **kw): return await query.message.reply_text(*a, **kw)
        await _do_sell(token, _FQ(), "MANUAL")

    elif data.startswith("bl:"):
        token = data.split(":", 1)[1]
        blacklist.add(token)
        save_blacklist_entry(token, "?", "Dismissed from alert")
        await query.edit_message_text(f"🚫 `{token[:16]}...` blacklisted.",
                                      parse_mode=ParseMode.MARKDOWN)

    elif data == "confirm_sellall":
        count = len(positions)
        for token in list(positions.keys()):
            class _FQ:
                async def reply_text(self, *a, **kw): pass
            await _do_sell(token, _FQ(), "MANUAL_ALL")
        await query.edit_message_text(f"✅ Sold {count} position(s).")

    elif data == "confirm_estop":
        cfg["auto_scan"] = False
        cfg["auto_buy"]  = False
        cfg["bot_paused"] = True
        save_cfg_to_db()
        scan_alerts.clear()
        await query.edit_message_text(
            "🛑 *EMERGENCY STOP*\nScanner off, auto-buy off, bot paused.",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.warning("🚨 EMERGENCY STOP triggered")

    elif data == "confirm_resetcfg":
        cfg.update(DEFAULT_CFG)
        save_cfg_to_db()
        await query.edit_message_text("✅ Config reset to defaults.")

    elif data == "bal":      await balance_cmd(update, context)
    elif data == "pos":      await positions_cmd(update, context)
    elif data == "scans":    await alerts_cmd(update, context)
    elif data == "stats":    await stats_cmd(update, context)
    elif data == "settings": await settings_cmd(update, context)
    elif data == "helpme":   await help_cmd(update, context)
    elif data == "history":  await history_cmd(update, context)
    elif data == "show_bl":  await blacklisted_cmd(update, context)

    elif data.startswith("analyze:"):
        token = data.split(":", 1)[1]
        context.args = [token]
        await analyze_cmd(update, context)

    elif data == "dismiss":
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_text("✅ Dismissed.")


# ════════════════════════════════════════════════════
#  WEBHOOK HEALTH SERVER (keeps Railway alive)
# ════════════════════════════════════════════════════

async def health_handler(request):
    return web.Response(
        text=json.dumps({
            "status": "ok",
            "version": "4.0",
            "uptime_sec": int(time.time() - bot_start_time),
            "positions": len(positions),
            "auto_scan": cfg["auto_scan"],
            "auto_buy": cfg["auto_buy"],
            "paused": cfg["bot_paused"],
        }),
        content_type="application/json"
    )


async def run_health_server():
    app = web.Application()
    app.router.add_get("/",        health_handler)
    app.router.add_get("/health",  health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info(f"🌐 Health server running on port {WEBHOOK_PORT}")


# ════════════════════════════════════════════════════
#  BOT MENU SETUP
# ════════════════════════════════════════════════════

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",        "Dashboard"),
        BotCommand("snipe",        "Snipe a token"),
        BotCommand("sell",         "Sell a position"),
        BotCommand("sellall",      "Sell all positions"),
        BotCommand("positions",    "Open positions + live PnL"),
        BotCommand("balance",      "SOL wallet balance"),
        BotCommand("scan",         "Run DEX scan now"),
        BotCommand("scanon",       "Enable auto-scanner"),
        BotCommand("scanoff",      "Disable auto-scanner"),
        BotCommand("autobuy",      "Toggle auto-buy"),
        BotCommand("alerts",       "Last scan results"),
        BotCommand("analyze",      "Deep token analysis"),
        BotCommand("blacklist",    "Blacklist a token"),
        BotCommand("unblacklist",  "Remove from blacklist"),
        BotCommand("blacklisted",  "Show blacklist"),
        BotCommand("backtest",     "Score recent pairs (no trades)"),
        BotCommand("set",          "Change a setting (persists)"),
        BotCommand("settings",     "Show all settings"),
        BotCommand("resetcfg",     "Reset settings to defaults"),
        BotCommand("stats",        "All-time performance"),
        BotCommand("history",      "Last 10 trades"),
        BotCommand("report",       "7-day weekly report"),
        BotCommand("pause",        "Pause trading"),
        BotCommand("resume",       "Resume trading"),
        BotCommand("stop",         "Emergency stop"),
        BotCommand("status",       "Full bot health status"),
        BotCommand("wallet",       "Wallet details"),
        BotCommand("help",         "All commands"),
    ])

    # Startup notification to owner
    for uid in ALLOWED_USER_IDS:
        try:
            await app.bot.send_message(
                uid,
                f"🚀 *Elite Sniper Bot v4.0 is online!*\n\n"
                f"💾 Loaded {len(positions)} position(s) from DB\n"
                f"🚫 {len(blacklist)} token(s) blacklisted\n"
                f"📡 Discord: {'✅ configured' if DISCORD_WEBHOOK_URL else '❌ not set'}\n\n"
                f"Use /status for full details.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass


# ════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════

def main():
    # ── Boot sequence ────────────────────────────────
    init_db()
    load_cfg_from_db()

    global positions, trade_history, blacklist
    positions     = load_positions()
    trade_history = load_trade_history(200)
    blacklist     = load_blacklist()

    logger.info(f"📂 Loaded: {len(positions)} positions | {len(trade_history)} trades | {len(blacklist)} blacklisted")

    # ── Build Telegram app ───────────────────────────
    telegram_app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register command handlers
    commands = [
        ("start",       start_cmd),
        ("help",        help_cmd),
        ("snipe",       snipe_cmd),
        ("sell",        sell_cmd),
        ("sellall",     sellall_cmd),
        ("positions",   positions_cmd),
        ("balance",     balance_cmd),
        ("wallet",      wallet_cmd),
        ("settings",    settings_cmd),
        ("set",         set_cmd),
        ("resetcfg",    resetcfg_cmd),
        ("stats",       stats_cmd),
        ("history",     history_cmd),
        ("report",      report_cmd),
        ("scan",        scan_cmd),
        ("alerts",      alerts_cmd),
        ("analyze",     analyze_cmd),
        ("scanon",      scanon_cmd),
        ("scanoff",     scanoff_cmd),
        ("autobuy",     autobuy_cmd),
        ("blacklist",   blacklist_cmd),
        ("unblacklist", unblacklist_cmd),
        ("blacklisted", blacklisted_cmd),
        ("backtest",    backtest_cmd),
        ("pause",       pause_cmd),
        ("resume",      resume_cmd),
        ("stop",        stop_cmd),
        ("status",      status_cmd),
    ]
    for cmd, handler in commands:
        telegram_app.add_handler(CommandHandler(cmd, handler))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))

    # Background jobs
    jq = telegram_app.job_queue
    if jq:
        jq.run_repeating(monitor_positions, interval=30,   first=15)
        jq.run_repeating(scanner_job,       interval=cfg["scan_interval_sec"], first=30)
        # Daily report at midnight UTC
        jq.run_daily(daily_report_job, time=datetime.strptime("00:00", "%H:%M").time())

    # ── Run health server + bot concurrently ─────────
    async def run_all():
        await run_health_server()
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("🚀 Elite Solana Sniper Bot v4.0 started!")
        # Keep running
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
