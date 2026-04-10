"""
╔══════════════════════════════════════════════════════════╗
║         HYPERLIQUID TRADER TRACKER BOT  v2               ║
║  Fixed: saves state to disk so restarts don't cause      ║
║  false "position opened/closed" alerts                   ║
╚══════════════════════════════════════════════════════════╝
"""

import os
import json
import requests
import time
from datetime import datetime, timezone

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════

TRADER_ADDRESS = "0x8A820d3B050BAFC0A1f3156706f28038aa292dce"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
POLL_INTERVAL  = 15   # seconds

HL_API     = "https://api.hyperliquid.xyz/info"
STATE_FILE = "/tmp/hl_tracker_state.json"  # persists between restarts on Railway

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("❌ TELEGRAM_TOKEN and CHAT_ID must be set as environment variables!")

# ══════════════════════════════════════════════
#  PERSISTENT STATE  (survives restarts)
# ══════════════════════════════════════════════

def load_state() -> dict:
    """Load saved state from disk. Returns empty state if none exists."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                print(f"[State] Loaded from disk: "
                      f"{len(data.get('fill_ids', []))} fills, "
                      f"{len(data.get('positions', {}))} positions")
                return data
    except Exception as e:
        print(f"[State] Could not load: {e}")
    return {"fill_ids": [], "positions": {}}


def save_state(fill_ids: set, positions: dict):
    """Save current state to disk."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "fill_ids":  list(fill_ids),
                "positions": positions,
                "saved_at":  datetime.utcnow().isoformat()
            }, f)
    except Exception as e:
        print(f"[State] Could not save: {e}")


# Load on startup
_state         = load_state()
seen_fill_ids  = set(_state.get("fill_ids", []))
last_positions = _state.get("positions", {})

# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════

def telegram(msg: str):
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


def get_fills() -> list:
    data = hl({"type": "userFills", "user": TRADER_ADDRESS})
    return data if isinstance(data, list) else []


def get_state_hl():
    return hl({"type": "clearinghouseState", "user": TRADER_ADDRESS})


def get_positions() -> dict:
    """Returns {asset: position_dict} for all open positions."""
    state = get_state_hl()
    if not state:
        return {}
    result = {}
    for item in state.get("assetPositions", []):
        pos   = item.get("position", {})
        asset = pos.get("coin")
        szi   = float(pos.get("szi", 0))
        if asset and szi != 0:
            result[asset] = pos
    return result

# ══════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════

def ts_to_str(ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%b %d %H:%M:%S UTC")
    except Exception:
        return "?"

def fmt_usd(val: float) -> str:
    return f"${val:,.2f}" if abs(val) >= 1000 else f"${val:.2f}"

def pnl_emoji(val: float) -> str:
    return "📈" if val >= 0 else "📉"

def detect_fill_type(fill: dict) -> str:
    direction = fill.get("dir", "").lower()
    if "open"  in direction and "long"  in direction: return "OPEN LONG"
    if "open"  in direction and "short" in direction: return "OPEN SHORT"
    if "close" in direction and "long"  in direction: return "CLOSE LONG"
    if "close" in direction and "short" in direction: return "CLOSE SHORT"
    closed_pnl = fill.get("closedPnl")
    if closed_pnl and float(closed_pnl) != 0:        return "CLOSE / TRIM"
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

    is_close   = "close" in fill_type.lower()
    is_open    = "open"  in fill_type.lower()
    icon       = "🏁" if is_close else ("🆕" if is_open else "⚡")
    side_str   = ("🟢 BUY / LONG" if side == "B" else "🔴 SELL / SHORT")

    pnl_section = ""
    if closed_pnl is not None:
        pnl_val = float(closed_pnl)
        net     = pnl_val - fee
        pnl_section = (
            f"\n{pnl_emoji(pnl_val)} Realized PnL: <b>{fmt_usd(pnl_val)}</b>"
            f"\n💸 Fee:         <b>{fmt_usd(fee)}</b>"
            f"\n✅ Net PnL:     <b>{fmt_usd(net)}</b>"
        )
    else:
        pnl_section = f"\n💸 Fee: <b>{fmt_usd(fee)}</b>"

    return (
        f"{icon} <b>{fill_type} — {asset}</b>\n"
        f"{'─'*30}\n"
        f"{side_str}\n"
        f"💲 Price:    <b>{fmt_usd(price)}</b>\n"
        f"📦 Size:     <b>{size}</b>\n"
        f"💼 Notional: <b>{fmt_usd(price * size)}</b>"
        f"{pnl_section}\n"
        f"🔖 {'Taker' if crossed else 'Maker'} | 🕐 {ts_to_str(ts)}"
    )


def msg_position_opened(asset: str, pos: dict) -> str:
    szi     = float(pos.get("szi", 0))
    entry   = float(pos.get("entryPx", 0))
    liq     = float(pos.get("liquidationPx") or 0)
    upnl    = float(pos.get("unrealizedPnl", 0))
    lev     = pos.get("leverage", {})
    lev_val = lev.get("value", "?") if isinstance(lev, dict) else "?"
    margin  = float(pos.get("marginUsed", 0))
    side_str = "🔴 SHORT" if szi < 0 else "🟢 LONG"

    return (
        f"🆕 <b>POSITION OPENED — {asset}</b>\n"
        f"{'─'*30}\n"
        f"{side_str}  {lev_val}x leverage\n"
        f"📦 Size:        <b>{abs(szi)}</b>\n"
        f"💲 Entry:       <b>{fmt_usd(entry)}</b>\n"
        f"💣 Liq Price:   <b>{fmt_usd(liq)}</b>\n"
        f"💼 Notional:    <b>{fmt_usd(abs(szi) * entry)}</b>\n"
        f"🏦 Margin Used: <b>{fmt_usd(margin)}</b>\n"
        f"📊 Unrealized:  <b>{fmt_usd(upnl)}</b>"
    )


def msg_position_closed(asset: str, old_pos: dict) -> str:
    szi     = float(old_pos.get("szi", 0))
    entry   = float(old_pos.get("entryPx", 0))
    lev     = old_pos.get("leverage", {})
    lev_val = lev.get("value", "?") if isinstance(lev, dict) else "?"
    side_str = "🔴 SHORT" if szi < 0 else "🟢 LONG"

    return (
        f"🏁 <b>POSITION FULLY CLOSED — {asset}</b>\n"
        f"{'─'*30}\n"
        f"Was: {side_str}  {lev_val}x\n"
        f"Entry was: <b>{fmt_usd(entry)}</b>\n"
        f"Size was:  <b>{abs(szi)}</b>\n"
        f"↑ See fill alert above for PnL"
    )


def msg_position_update(asset: str, old_pos: dict, new_pos: dict) -> str:
    old_szi  = float(old_pos.get("szi", 0))
    new_szi  = float(new_pos.get("szi", 0))
    new_upnl = float(new_pos.get("unrealizedPnl", 0))
    sz_delta = abs(new_szi) - abs(old_szi)
    action   = "➕ Added to" if sz_delta > 0 else "✂️ Trimmed"
    side_str = "🔴 SHORT" if new_szi < 0 else "🟢 LONG"
    entry    = float(new_pos.get("entryPx", 0))
    liq      = float(new_pos.get("liquidationPx") or 0)

    return (
        f"🔄 <b>POSITION UPDATED — {asset}</b>\n"
        f"{'─'*30}\n"
        f"{side_str} | {action}\n"
        f"📦 Size: {abs(old_szi):.4f} → <b>{abs(new_szi):.4f}</b> (Δ {sz_delta:+.4f})\n"
        f"💲 Entry: <b>{fmt_usd(entry)}</b>\n"
        f"💣 Liq:   <b>{fmt_usd(liq)}</b>\n"
        f"📊 UPnL:  <b>{fmt_usd(new_upnl)}</b>"
    )

# ══════════════════════════════════════════════
#  CHECK LOOPS
# ══════════════════════════════════════════════

def check_fills():
    fills     = get_fills()
    new_fills = []

    for fill in fills:
        fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
        if fid not in seen_fill_ids:
            seen_fill_ids.add(fid)
            new_fills.append(fill)

    if not new_fills:
        return

    new_fills.sort(key=lambda f: f.get("time", 0))
    for fill in new_fills:
        telegram(msg_fill(fill))
        print(f"  [Fill] {fill.get('coin')} @ {fill.get('px')}")
        time.sleep(0.5)

    save_state(seen_fill_ids, last_positions)


def check_positions():
    current  = get_positions()
    changed  = False

    # Newly opened (not in our saved state)
    for asset, pos in current.items():
        if asset not in last_positions:
            telegram(msg_position_opened(asset, pos))
            print(f"  [Pos] Opened: {asset}")
            changed = True

    # Fully closed
    for asset in list(last_positions.keys()):
        if asset not in current:
            telegram(msg_position_closed(asset, last_positions[asset]))
            print(f"  [Pos] Closed: {asset}")
            changed = True

    # Size changed (trim / add)
    for asset, new_pos in current.items():
        if asset in last_positions:
            old_szi = float(last_positions[asset].get("szi", 0))
            new_szi = float(new_pos.get("szi", 0))
            if abs(abs(new_szi) - abs(old_szi)) > 0.0001:
                telegram(msg_position_update(asset, last_positions[asset], new_pos))
                print(f"  [Pos] Updated: {asset} {old_szi:.4f}→{new_szi:.4f}")
                changed = True

    # Always update + save positions
    last_positions.clear()
    last_positions.update(current)
    if changed:
        save_state(seen_fill_ids, last_positions)

# ══════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════

def startup():
    print(f"\n{'='*50}")
    print(f"  HYPERLIQUID TRACKER BOT v2")
    print(f"  Target: {TRADER_ADDRESS[:12]}...{TRADER_ADDRESS[-6:]}")
    print(f"{'='*50}\n")

    is_fresh = len(seen_fill_ids) == 0 and len(last_positions) == 0

    if is_fresh:
        # First ever run — prime state silently from API
        print("First run — priming state from API (no alerts)...")
        fills = get_fills()
        for fill in fills:
            fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
            seen_fill_ids.add(fid)
        current = get_positions()
        last_positions.update(current)
        save_state(seen_fill_ids, last_positions)
        print(f"✅ Primed: {len(seen_fill_ids)} fills, {len(last_positions)} positions saved to disk")
    else:
        # Restarted — state loaded from disk, no false alerts
        print(f"✅ Resumed from saved state: "
              f"{len(seen_fill_ids)} fills, {len(last_positions)} positions")

    # Send a silent startup ping (no position spam)
    state_hl    = get_state_hl()
    margin_info = state_hl.get("marginSummary", {}) if state_hl else {}
    acct_value  = float(margin_info.get("accountValue", 0))
    total_upnl  = sum(float(p.get("unrealizedPnl", 0)) for p in last_positions.values())

    pos_lines = ""
    for asset, pos in last_positions.items():
        szi     = float(pos.get("szi", 0))
        upnl    = float(pos.get("unrealizedPnl", 0))
        entry   = float(pos.get("entryPx", 0))
        lev     = pos.get("leverage", {})
        lev_val = lev.get("value", "?") if isinstance(lev, dict) else "?"
        side    = "SHORT" if szi < 0 else "LONG"
        pos_lines += (
            f"\n  {'📉' if upnl < 0 else '📈'} <b>{asset}</b> {side} {lev_val}x"
            f" | Entry {fmt_usd(entry)} | UPnL <b>{fmt_usd(upnl)}</b>"
        )

    restart_label = "🔄 Restarted" if not is_fresh else "✅ Started"

    telegram(
        f"{restart_label} — <b>Tracker is LIVE</b>\n"
        f"{'─'*30}\n"
        f"Open positions ({len(last_positions)}):"
        f"{pos_lines or chr(10) + '  (none)'}\n\n"
        f"💰 Total UPnL:    <b>{fmt_usd(total_upnl)}</b>\n"
        f"🏦 Account Value: <b>{fmt_usd(acct_value)}</b>\n"
        f"📡 Polling every {POLL_INTERVAL}s"
    )

    print(f"📡 Polling every {POLL_INTERVAL}s. Running...\n")

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
            print("ok")
        except KeyboardInterrupt:
            print("\nStopped.")
            telegram("🔴 <b>Tracker stopped.</b>")
            break
        except Exception as e:
            print(f"\n[Error] {e}")

        time.sleep(POLL_INTERVAL)