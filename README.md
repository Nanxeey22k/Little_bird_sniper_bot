# ðŸ¤– Elite solana Sniper Bot v3.0

A professional-grade Telegram-controlled Solana token sniper with **ML scoring**, **real-time DEX scanning**, **anti-rug detection**, and a **full inline control panel** â€” all from inside Telegram.

---

## âœ¨ What's New vs. Original Bot

| Feature | Original | v3.0 |
|---|---|---|
| Token discovery | Manual only | âœ… Auto-scanner (DexScreener) |
| Signal scoring | Basic TA | âœ… 8-factor ML scoring (0-100) |
| Anti-rug detection | âŒ None | âœ… Freeze/Mint authority checks |
| Holder analysis | âŒ None | âœ… Whale concentration detection |
| Telegram UI | Text commands only | âœ… Inline keyboards + dashboard |
| Position sizing | Fixed | âœ… Kelly-inspired dynamic sizing |
| Trailing stop | âŒ None | âœ… Configurable trailing stop |
| Trade history | âŒ None | âœ… Full PnL log with stats |
| Security | No auth | âœ… User ID whitelist |
| Emergency stop | Basic clear | âœ… Full halt with confirmation |
| Live settings | Restart required | âœ… /set command at runtime |

---

## ðŸš€ Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure your environment
```bash
cp .env.example .env
# Edit .env with your values
```

### 3. Run the bot
```bash
python main.py
```

---

## âš™ï¸ Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | âœ… | From @BotFather |
| `PRIVATE_KEY` | âœ… | Solana wallet base58 private key |
| `HELIUS_RPC` | âœ… | Helius RPC URL (get free key at helius.xyz) |
| `ALLOWED_USER_IDS` | Recommended | Comma-separated Telegram user IDs |
| `BIRDEYE_API_KEY` | Optional | Better OHLCV data (free tier works) |

---

## ðŸ“± Telegram Commands

### Trading
| Command | Description |
|---|---|
| `/snipe <addr> [sol]` | Manually snipe a token |
| `/sell <addr>` | Sell a specific position |
| `/sellall` | Sell all open positions |
| `/positions` | Live PnL on all open positions |

### Scanner
| Command | Description |
|---|---|
| `/scan` | Run a manual DEX scan now |
| `/scanon` | Enable automatic scanning |
| `/scanoff` | Disable automatic scanning |
| `/autobuy` | Toggle auto-buy on scan alerts |
| `/alerts` | Show last scan results with buy buttons |
| `/analyze <addr>` | Deep analysis of any token |

### Settings
| Command | Description |
|---|---|
| `/settings` | Show all current settings |
| `/set <key> <value>` | Change any setting at runtime |

### Control & Stats
| Command | Description |
|---|---|
| `/status` | Full bot health overview |
| `/stats` | Win rate, PnL, trade count |
| `/history` | Last 10 closed trades |
| `/balance` | SOL wallet balance |
| `/pause` | Pause all new trades |
| `/resume` | Resume trading |
| `/stop` | Emergency stop (confirmation required) |

---

## ðŸ§  Scoring Engine

Each token is scored 0-100 across 8 weighted factors:

| Factor | Max Points | Signal |
|---|---|---|
| Liquidity | 20 | >$50k = 20pts, >$20k = 14pts |
| Volume/Liq Ratio | 20 | >2x = explosive, >0.8x = strong |
| Buy Pressure | 15 | >70% buys = 15pts |
| Price Momentum | 10 | +5% to +80% = sweet spot |
| Token Age | 10 | 5-30min = fresh, 30-60min = young |
| Holder Distribution | 10 | >200 holders + top10 <40% |
| Safety (Freeze/Mint) | 15 | No authorities = +15pts |
| Technical Analysis | 10 | EMA bull + RSI 45-70 + vol spike |

**Penalties:**
- Both freeze+mint authority: **-25pts**
- Freeze authority only: **-10pts**
- Top10 holders >70%: signal blocked

Default minimum score to trade: **65/100** (configurable via `/set min_score`)

---

## ðŸ›¡ Risk Management

- **Daily trade limit** â€” max trades per day (default: 8)
- **Loss circuit breaker** â€” stops trading if daily loss exceeds 20% of max_position
- **Dynamic position sizing** â€” Kelly-inspired, based on wallet balance and risk %
- **Trailing stop** â€” locks in profits as price climbs (set `trailing_stop_pct > 0`)
- **Security whitelist** â€” only ALLOWED_USER_IDS can control the bot

---

## âš ï¸ Disclaimer

This bot trades real cryptocurrency. Use at your own risk. Start with small amounts and test thoroughly. The developer is not responsible for any financial losses. Always DYOR (Do Your Own Research).
