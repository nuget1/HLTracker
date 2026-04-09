"""
╔══════════════════════════════════════════════════════════╗
║         HYPERLIQUID TRADER TRACKER BOT                   ║
║  Alerts you on Telegram for EVERY transaction the        ║
║  target trader makes — fills, opens, closes, trims, etc. ║
╚══════════════════════════════════════════════════════════╝

SETUP:
  1. pip install requests
  2. Create Telegram bot: message @BotFather → /newbot → copy token
  3. Get your Chat ID: message @userinfobot → copy id
  4. Fill TELEGRAM_TOKEN and CHAT_ID below
  5. python hl_tracker_bot.py

TARGET: 0x8A820d3B050BAFC0A1f3156706f28038aa292dce
"""

import os
import requests
import time
from datetime import datetime, timezone

# ══════════════════════════════════════════════
#  CONFIG — set these in Railway → Variables
# ══════════════════════════════════════════════

TRADER_ADDRESS = "0x8A820d3B050BAFC0A1f3156706f28038aa292dce"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")

POLL_INTERVAL  = 10   # seconds — lower = faster alerts, more requests

# Crash early if tokens are missing
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("❌ TELEGRAM_TOKEN and CHAT_ID must be set as environment variables!")

HL_API = "https://api.hyperliquid.xyz/info"

# ══════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════

seen_fill_ids  = set()
last_positions = {}   # {asset: position_dict}

# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════

def telegram(msg: str):
    """Send a message to your Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    CHAT_ID,
            "text":       msg,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            print(f"[Telegram] Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Telegram] Exception: {e}")

# ══════════════════════════════════════════════
#  HYPERLIQUID API
# ══════════════════════════════════════════════

def hl(payload: dict):
    try:
        r = requests.post(HL_API, json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[HL API] Error: {e}")
        return None


def get_fills():
    """All historical fills for the trader."""
    data = hl({"type": "userFills", "user": TRADER_ADDRESS})
    return data if isinstance(data, list) else []


def get_state():
    """Full account state — positions, margin, etc."""
    return hl({"type": "clearinghouseState", "user": TRADER_ADDRESS})


def get_positions(state=None):
    """Returns {asset: position_dict} for all open positions."""
    if state is None:
        state = get_state()
    if not state:
        return {}
    result = {}
    for item in state.get("assetPositions", []):
        pos = item.get("position", {})
        asset = pos.get("coin")
        szi = float(pos.get("szi", 0))
        if asset and szi != 0:
            result[asset] = pos
    return result

# ══════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════

def ts_to_str(ms: int) -> str:
    """Convert millisecond timestamp to readable UTC string."""
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%b %d %H:%M:%S UTC")
    except Exception:
        return "?"


def side_emoji(side_char: str) -> str:
    """B = Buy/Long, A = Ask/Short"""
    return "BUY / LONG" if side_char == "B" else "SELL / SHORT"


def pnl_emoji(val: float) -> str:
    return "📈" if val >= 0 else "📉"


def fmt_usd(val: float) -> str:
    if abs(val) >= 1000:
        return f"${val:,.2f}"
    return f"${val:.2f}"


def detect_fill_type(fill: dict) -> str:
    """
    Classify the fill using the 'dir' field Hyperliquid provides,
    with fallback to side + closedPnl logic.
    """
    direction = fill.get("dir", "").lower()

    if "open" in direction and "long" in direction:
        return "OPEN LONG"
    elif "open" in direction and "short" in direction:
        return "OPEN SHORT"
    elif "close" in direction and "long" in direction:
        return "CLOSE LONG"
    elif "close" in direction and "short" in direction:
        return "CLOSE SHORT"

    # Fallback
    closed_pnl = fill.get("closedPnl")
    side = fill.get("side", "")
    if closed_pnl is not None and float(closed_pnl) != 0:
        return "CLOSE / TRIM"
    return "OPEN / ADD"


# ══════════════════════════════════════════════
#  MESSAGE FORMATTERS
# ══════════════════════════════════════════════

def msg_fill(fill: dict) -> str:
    asset      = fill.get("coin", "?")
    side       = fill.get("side", "?")
    price      = float(fill.get("px", 0))
    size       = float(fill.get("sz", 0))
    closed_pnl = fill.get("closedPnl")
    fee        = float(fill.get("fee", 0))
    ts         = fill.get("time", 0)
    crossed    = fill.get("crossed", False)
    fill_type  = detect_fill_type(fill)

    # Emoji for header
    is_close   = "close" in fill_type.lower()
    is_open    = "open" in fill_type.lower()
    header_ico = "🏁" if is_close else ("🆕" if is_open else "⚡")

    # Side display
    side_display = "🟢 " + side_emoji(side)

    # PnL section
    pnl_section = ""
    if closed_pnl is not None:
        pnl_val  = float(closed_pnl)
        net      = pnl_val - fee
        p_emoji  = pnl_emoji(pnl_val)
        pnl_section = (
            f"\n{p_emoji} Realized PnL:  <b>{fmt_usd(pnl_val)}</b>"
            f"\n💸 Fee:          <b>{fmt_usd(fee)}</b>"
            f"\n✅ Net PnL:      <b>{fmt_usd(net)}</b>"
        )
    else:
        pnl_section = f"\n💸 Fee: <b>{fmt_usd(fee)}</b>"

    role = "Taker" if crossed else "Maker"
    notional = price * size

    return (
        f"{header_ico} <b>{fill_type} — {asset}</b>\n"
        f"{'─' * 30}\n"
        f"{side_display}\n"
        f"💲 Price:     <b>{fmt_usd(price)}</b>\n"
        f"📦 Size:      <b>{size}</b>\n"
        f"💼 Notional:  <b>{fmt_usd(notional)}</b>"
        f"{pnl_section}\n"
        f"🔖 Role:  {role}\n"
        f"🕐 {ts_to_str(ts)}"
    )


def msg_position_opened(asset: str, pos: dict) -> str:
    szi      = float(pos.get("szi", 0))
    side_str = "🔴 SHORT" if szi < 0 else "🟢 LONG"
    entry    = float(pos.get("entryPx", 0))
    liq      = float(pos.get("liquidationPx") or 0)
    upnl     = float(pos.get("unrealizedPnl", 0))
    lev      = pos.get("leverage", {})
    lev_val  = lev.get("value", "?") if isinstance(lev, dict) else "?"
    margin   = float(pos.get("marginUsed", 0))
    notional = abs(szi) * entry

    return (
        f"🆕 <b>POSITION OPENED — {asset}</b>\n"
        f"{'─' * 30}\n"
        f"{side_str}  {lev_val}x leverage\n"
        f"📦 Size:         <b>{abs(szi)}</b>\n"
        f"💲 Entry:        <b>{fmt_usd(entry)}</b>\n"
        f"💣 Liq Price:    <b>{fmt_usd(liq)}</b>\n"
        f"💼 Notional:     <b>{fmt_usd(notional)}</b>\n"
        f"🏦 Margin Used:  <b>{fmt_usd(margin)}</b>\n"
        f"📊 Unrealized:   <b>{fmt_usd(upnl)}</b>"
    )


def msg_position_closed(asset: str, old_pos: dict) -> str:
    szi      = float(old_pos.get("szi", 0))
    side_str = "🔴 SHORT" if szi < 0 else "🟢 LONG"
    entry    = float(old_pos.get("entryPx", 0))
    lev      = old_pos.get("leverage", {})
    lev_val  = lev.get("value", "?") if isinstance(lev, dict) else "?"

    return (
        f"🏁 <b>POSITION FULLY CLOSED — {asset}</b>\n"
        f"{'─' * 30}\n"
        f"Was: {side_str}  {lev_val}x\n"
        f"Entry was: <b>{fmt_usd(entry)}</b>\n"
        f"Size was:  <b>{abs(szi)}</b>\n"
        f"(check fill alert above for PnL)"
    )


def msg_position_update(asset: str, old_pos: dict, new_pos: dict) -> str:
    old_szi   = float(old_pos.get("szi", 0))
    new_szi   = float(new_pos.get("szi", 0))
    old_upnl  = float(old_pos.get("unrealizedPnl", 0))
    new_upnl  = float(new_pos.get("unrealizedPnl", 0))
    pnl_delta = new_upnl - old_upnl
    sz_delta  = abs(new_szi) - abs(old_szi)
    action    = "➕ Added to" if sz_delta > 0 else "✂️ Trimmed"
    side_str  = "🔴 SHORT" if new_szi < 0 else "🟢 LONG"
    entry     = float(new_pos.get("entryPx", 0))
    liq       = float(new_pos.get("liquidationPx") or 0)

    return (
        f"🔄 <b>POSITION UPDATED — {asset}</b>\n"
        f"{'─' * 30}\n"
        f"{side_str} | {action}\n"
        f"📦 Size:    {abs(old_szi):.4f} → <b>{abs(new_szi):.4f}</b>  (Δ {sz_delta:+.4f})\n"
        f"💲 Entry:   <b>{fmt_usd(entry)}</b>\n"
        f"💣 Liq:     <b>{fmt_usd(liq)}</b>\n"
        f"📊 UPnL:    <b>{fmt_usd(new_upnl)}</b>  (Δ {fmt_usd(pnl_delta)})"
    )

# ══════════════════════════════════════════════
#  MAIN CHECK LOOPS
# ══════════════════════════════════════════════

def check_fills():
    """Alert on every new fill."""
    fills = get_fills()
    new_fills = []

    for fill in fills:
        fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
        if fid not in seen_fill_ids:
            seen_fill_ids.add(fid)
            new_fills.append(fill)

    # Sort oldest first so messages arrive in order
    new_fills.sort(key=lambda f: f.get("time", 0))

    for fill in new_fills:
        msg = msg_fill(fill)
        telegram(msg)
        asset = fill.get("coin", "?")
        price = fill.get("px", "?")
        print(f"  [Fill] {asset} @ {price}")
        time.sleep(0.5)  # avoid Telegram flood limit if many fills at once


def check_positions():
    """Alert on position opens, closes, and size changes."""
    current = get_positions()

    # Newly opened
    for asset, pos in current.items():
        if asset not in last_positions:
            msg = msg_position_opened(asset, pos)
            telegram(msg)
            print(f"  [Pos] Opened: {asset}")

    # Fully closed
    for asset in list(last_positions.keys()):
        if asset not in current:
            msg = msg_position_closed(asset, last_positions[asset])
            telegram(msg)
            print(f"  [Pos] Closed: {asset}")

    # Size changes (trims / adds)
    for asset, new_pos in current.items():
        if asset in last_positions:
            old_szi = float(last_positions[asset].get("szi", 0))
            new_szi = float(new_pos.get("szi", 0))
            if abs(abs(new_szi) - abs(old_szi)) > 0.0001:
                msg = msg_position_update(asset, last_positions[asset], new_pos)
                telegram(msg)
                print(f"  [Pos] Updated: {asset}  {old_szi:.4f}→{new_szi:.4f}")

    last_positions.clear()
    last_positions.update(current)

# ══════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════

def startup():
    """Prime state silently — no spam on launch."""
    print(f"\n{'='*50}")
    print(f"  HYPERLIQUID TRACKER BOT")
    print(f"  Target: {TRADER_ADDRESS[:12]}...{TRADER_ADDRESS[-6:]}")
    print(f"{'='*50}\n")
    print("Priming state (loading existing fills + positions)...")

    fills = get_fills()
    for fill in fills:
        fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
        seen_fill_ids.add(fid)

    current = get_positions()
    last_positions.update(current)

    # Build current state summary for Telegram
    state        = get_state()
    margin_info  = state.get("marginSummary", {}) if state else {}
    acct_value   = float(margin_info.get("accountValue", 0))
    total_margin = float(margin_info.get("totalMarginUsed", 0))

    total_upnl = sum(
        float(p.get("unrealizedPnl", 0))
        for p in last_positions.values()
    )

    pos_lines = ""
    for asset, pos in last_positions.items():
        szi      = float(pos.get("szi", 0))
        side     = "SHORT" if szi < 0 else "LONG"
        upnl     = float(pos.get("unrealizedPnl", 0))
        entry    = float(pos.get("entryPx", 0))
        lev      = pos.get("leverage", {})
        lev_val  = lev.get("value", "?") if isinstance(lev, dict) else "?"
        pnl_e    = "📈" if upnl >= 0 else "📉"
        pos_lines += (
            f"\n  {pnl_e} <b>{asset}</b> {side} {lev_val}x"
            f" | Entry {fmt_usd(entry)}"
            f" | UPnL <b>{fmt_usd(upnl)}</b>"
        )

    if not pos_lines:
        pos_lines = "\n  (none)"

    startup_msg = (
        f"✅ <b>Tracker is LIVE!</b>\n"
        f"{'─' * 30}\n"
        f"👤 <code>{TRADER_ADDRESS[:14]}...{TRADER_ADDRESS[-6:]}</code>\n\n"
        f"<b>Current Open Positions ({len(last_positions)}):</b>"
        f"{pos_lines}\n\n"
        f"💰 Total UPnL:     <b>{fmt_usd(total_upnl)}</b>\n"
        f"🏦 Account Value:  <b>{fmt_usd(acct_value)}</b>\n"
        f"📡 Polling every {POLL_INTERVAL}s\n\n"
        f"🔔 Will alert on: every fill, opens, closes, trims"
    )
    telegram(startup_msg)

    print(f"✅ Done — {len(seen_fill_ids)} fills cached, {len(last_positions)} positions loaded")
    print(f"📡 Polling every {POLL_INTERVAL}s. Press Ctrl+C to stop.\n")

# ══════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════

if __name__ == "__main__":
    startup()

    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking...", end=" ")
            check_fills()
            check_positions()
            print("done.")
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
            telegram("🔴 <b>Tracker stopped.</b>")
            break
        except Exception as e:
            print(f"\n[Error] {e}")

        time.sleep(POLL_INTERVAL)