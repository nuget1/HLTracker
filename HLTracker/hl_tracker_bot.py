"""
╔══════════════════════════════════════════════════════════╗
║   HYPERLIQUID MULTI-WALLET TRACKER  v4  (Interactive)    ║
║   Manage wallets directly from Telegram — no code edits  ║
╚══════════════════════════════════════════════════════════╝

TELEGRAM COMMANDS:
  /add  0xADDRESS  Nickname    — start tracking a wallet
  /del  0xADDRESS              — stop tracking a wallet
  /list                        — show all tracked wallets
  /status                      — show positions & PnL for all wallets
  /help                        — show command list

SETUP:
  1. pip install requests
  2. Set Railway env vars: TELEGRAM_TOKEN, CHAT_ID
  3. Deploy — then control everything from Telegram
"""

import os
import json
import requests
import time
import threading
from datetime import datetime, timezone

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
POLL_INTERVAL  = 15   # seconds between full cycles
HL_API         = "https://api.hyperliquid.xyz/info"
STATE_FILE     = "/tmp/hl_tracker_state.json"

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("❌ TELEGRAM_TOKEN and CHAT_ID must be set as environment variables!")

# ══════════════════════════════════════════════
#  STATE  (persists across restarts)
# ══════════════════════════════════════════════
# Structure:
# {
#   "wallets": {
#     "0xABC...": {
#       "label":     "Nickname",
#       "fill_ids":  [...],
#       "positions": {...}
#     }
#   },
#   "tg_offset": 0   ← for polling Telegram updates
# }

def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                wallets = data.get("wallets", {})
                print(f"[State] Loaded: {len(wallets)} wallet(s) from disk")
                return data
    except Exception as e:
        print(f"[State] Could not load: {e}")
    return {"wallets": {}, "tg_offset": 0}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[State] Could not save: {e}")


state = load_state()   # single global state dict

def get_wallets() -> dict:
    return state.get("wallets", {})

def get_wallet(address: str) -> dict:
    return state["wallets"].get(address, {})

# ══════════════════════════════════════════════
#  TELEGRAM — SENDING
# ══════════════════════════════════════════════

TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def tg_send(msg: str, chat_id: str = None):
    cid = chat_id or CHAT_ID
    try:
        r = requests.post(f"{TG_BASE}/sendMessage", json={
            "chat_id":    cid,
            "text":       msg,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            print(f"[Telegram] Send error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Telegram] Exception: {e}")


def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(f"{TG_BASE}/getUpdates", params={
            "offset":  offset,
            "timeout": 5
        }, timeout=10)
        if r.ok:
            return r.json().get("result", [])
    except Exception as e:
        print(f"[Telegram] getUpdates error: {e}")
    return []

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

def hl_fills(address: str) -> list:
    data = hl({"type": "userFills", "user": address})
    return data if isinstance(data, list) else []

def hl_state(address: str):
    return hl({"type": "clearinghouseState", "user": address})

def hl_positions(address: str) -> dict:
    s = hl_state(address)
    if not s:
        return {}
    result = {}
    for item in s.get("assetPositions", []):
        pos   = item.get("position", {})
        asset = pos.get("coin")
        szi   = float(pos.get("szi", 0))
        if asset and szi != 0:
            result[asset] = pos
    return result

def validate_wallet(address: str) -> bool:
    """Check if a wallet address exists on Hyperliquid."""
    s = hl_state(address)
    return s is not None

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

def short_addr(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"

def detect_fill_type(fill: dict) -> str:
    d = fill.get("dir", "").lower()
    if "open"  in d and "long"  in d: return "OPEN LONG"
    if "open"  in d and "short" in d: return "OPEN SHORT"
    if "close" in d and "long"  in d: return "CLOSE LONG"
    if "close" in d and "short" in d: return "CLOSE SHORT"
    pnl = fill.get("closedPnl")
    if pnl and float(pnl) != 0:       return "CLOSE / TRIM"
    return "OPEN / ADD"

def trader_header(address: str) -> str:
    w = get_wallet(address)
    label = w.get("label", short_addr(address))
    return f"👤 <b>{label}</b>  <code>{short_addr(address)}</code>\n"

# ══════════════════════════════════════════════
#  ALERT FORMATTERS
# ══════════════════════════════════════════════

def msg_fill(fill: dict, address: str) -> str:
    asset      = fill.get("coin", "?")
    side       = fill.get("side", "?")
    price      = float(fill.get("px", 0))
    size       = float(fill.get("sz", 0))
    closed_pnl = fill.get("closedPnl")
    fee        = float(fill.get("fee", 0))
    ts         = fill.get("time", 0)
    crossed    = fill.get("crossed", False)
    fill_type  = detect_fill_type(fill)

    is_close = "close" in fill_type.lower()
    is_open  = "open"  in fill_type.lower()
    icon     = "🏁" if is_close else ("🆕" if is_open else "⚡")
    side_str = "🟢 BUY / LONG" if side == "B" else "🔴 SELL / SHORT"

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
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"{side_str}\n"
        f"💲 Price:    <b>{fmt_usd(price)}</b>\n"
        f"📦 Size:     <b>{size}</b>\n"
        f"💼 Notional: <b>{fmt_usd(price * size)}</b>"
        f"{pnl_section}\n"
        f"🔖 {'Taker' if crossed else 'Maker'} | 🕐 {ts_to_str(ts)}"
    )


def msg_opened(asset: str, pos: dict, address: str) -> str:
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
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"{side_str}  {lev_val}x leverage\n"
        f"📦 Size:        <b>{abs(szi)}</b>\n"
        f"💲 Entry:       <b>{fmt_usd(entry)}</b>\n"
        f"💣 Liq Price:   <b>{fmt_usd(liq)}</b>\n"
        f"💼 Notional:    <b>{fmt_usd(abs(szi) * entry)}</b>\n"
        f"🏦 Margin Used: <b>{fmt_usd(margin)}</b>\n"
        f"📊 Unrealized:  <b>{fmt_usd(upnl)}</b>"
    )


def msg_closed(asset: str, old_pos: dict, address: str) -> str:
    szi     = float(old_pos.get("szi", 0))
    entry   = float(old_pos.get("entryPx", 0))
    lev     = old_pos.get("leverage", {})
    lev_val = lev.get("value", "?") if isinstance(lev, dict) else "?"
    side_str = "🔴 SHORT" if szi < 0 else "🟢 LONG"
    return (
        f"🏁 <b>POSITION FULLY CLOSED — {asset}</b>\n"
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"Was: {side_str}  {lev_val}x\n"
        f"Entry was: <b>{fmt_usd(entry)}</b>\n"
        f"Size was:  <b>{abs(szi)}</b>\n"
        f"↑ See fill alert above for PnL"
    )


def msg_updated(asset: str, old_pos: dict, new_pos: dict, address: str) -> str:
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
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"{side_str} | {action}\n"
        f"📦 Size: {abs(old_szi):.4f} → <b>{abs(new_szi):.4f}</b> (Δ {sz_delta:+.4f})\n"
        f"💲 Entry: <b>{fmt_usd(entry)}</b>\n"
        f"💣 Liq:   <b>{fmt_usd(liq)}</b>\n"
        f"📊 UPnL:  <b>{fmt_usd(new_upnl)}</b>"
    )

# ══════════════════════════════════════════════
#  COMMANDS  (called when you message the bot)
# ══════════════════════════════════════════════

def cmd_help() -> str:
    return (
        "🤖 <b>HL Tracker Commands</b>\n"
        "{'─'*30}\n"
        "/add  <code>0xADDRESS</code>  <code>Nickname</code>\n"
        "  → Start tracking a wallet\n\n"
        "/del  <code>0xADDRESS</code>\n"
        "  → Stop tracking a wallet\n\n"
        "/list\n"
        "  → Show all tracked wallets\n\n"
        "/status\n"
        "  → Show live positions & PnL for all wallets\n\n"
        "/help\n"
        "  → Show this message"
    )


def cmd_add(parts: list) -> str:
    # /add 0xADDRESS Nickname
    if len(parts) < 2:
        return "❌ Usage: /add <code>0xADDRESS</code> <code>Nickname</code>\nExample: /add 0xABC...123 Whale Guy"

    address = parts[1].strip()
    label   = " ".join(parts[2:]).strip() if len(parts) > 2 else short_addr(address)

    if not address.startswith("0x") or len(address) < 10:
        return "❌ Invalid address. Must start with 0x"

    wallets = get_wallets()
    if address in wallets:
        return f"⚠️ Already tracking <b>{wallets[address]['label']}</b>\n<code>{short_addr(address)}</code>"

    # Validate on Hyperliquid
    tg_send(f"🔍 Validating <code>{short_addr(address)}</code>...")
    if not validate_wallet(address):
        return "❌ Could not find that wallet on Hyperliquid. Check the address."

    # Prime fill IDs silently
    fills    = hl_fills(address)
    fill_ids = []
    for fill in fills:
        fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
        fill_ids.append(fid)

    positions = hl_positions(address)

    # Save to state
    state["wallets"][address] = {
        "label":     label,
        "fill_ids":  fill_ids,
        "positions": positions
    }
    save_state()

    pos_count = len(positions)
    pos_str   = f"{pos_count} open position(s)" if pos_count else "no open positions"

    return (
        f"✅ Now tracking <b>{label}</b>\n"
        f"<code>{short_addr(address)}</code>\n"
        f"📊 Currently: {pos_str}\n"
        f"🔔 You'll be alerted on every trade."
    )


def cmd_del(parts: list) -> str:
    # /del 0xADDRESS
    if len(parts) < 2:
        return "❌ Usage: /del <code>0xADDRESS</code>"

    address = parts[1].strip()
    wallets = get_wallets()

    if address not in wallets:
        # Try partial match
        matches = [a for a in wallets if a.lower().startswith(address.lower())]
        if len(matches) == 1:
            address = matches[0]
        elif len(matches) > 1:
            return "⚠️ Multiple matches — use the full address."
        else:
            return "❌ Wallet not found in tracking list.\nUse /list to see tracked wallets."

    label = wallets[address]["label"]
    del state["wallets"][address]
    save_state()

    return f"🗑️ Stopped tracking <b>{label}</b>\n<code>{short_addr(address)}</code>"


def cmd_list() -> str:
    wallets = get_wallets()
    if not wallets:
        return "📋 No wallets tracked yet.\nUse /add <code>0xADDRESS</code> <code>Nickname</code> to add one."

    lines = f"📋 <b>Tracked Wallets ({len(wallets)})</b>\n{'─'*30}"
    for i, (address, data) in enumerate(wallets.items(), 1):
        label     = data.get("label", "?")
        positions = data.get("positions", {})
        lines += f"\n{i}. <b>{label}</b>\n   <code>{address}</code>\n   Positions: {len(positions)}"

    return lines


def cmd_status() -> str:
    wallets = get_wallets()
    if not wallets:
        return "📊 No wallets tracked. Use /add to start."

    lines = f"📊 <b>Live Status ({len(wallets)} wallets)</b>\n{'─'*30}"

    for address, data in wallets.items():
        label     = data.get("label", "?")
        s         = hl_state(address)
        margin    = s.get("marginSummary", {}) if s else {}
        acct_val  = float(margin.get("accountValue", 0))
        positions = hl_positions(address)
        total_upnl = sum(float(p.get("unrealizedPnl", 0)) for p in positions.values())

        lines += f"\n\n👤 <b>{label}</b>  <code>{short_addr(address)}</code>"
        lines += f"\n🏦 Acct Value: <b>{fmt_usd(acct_val)}</b>"
        lines += f"\n💰 Total UPnL: <b>{fmt_usd(total_upnl)}</b>"

        if positions:
            for asset, pos in positions.items():
                szi     = float(pos.get("szi", 0))
                upnl    = float(pos.get("unrealizedPnl", 0))
                entry   = float(pos.get("entryPx", 0))
                lev     = pos.get("leverage", {})
                lev_val = lev.get("value", "?") if isinstance(lev, dict) else "?"
                side    = "SHORT" if szi < 0 else "LONG"
                lines  += (
                    f"\n  {'📉' if upnl < 0 else '📈'} {asset} {side} {lev_val}x"
                    f" | Entry {fmt_usd(entry)} | UPnL <b>{fmt_usd(upnl)}</b>"
                )
        else:
            lines += "\n  (no open positions)"

        time.sleep(0.3)

    return lines

# ══════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLER
# ══════════════════════════════════════════════

def handle_update(update: dict):
    """Process a single incoming Telegram message."""
    msg  = update.get("message", {})
    text = msg.get("text", "").strip()
    cid  = str(msg.get("chat", {}).get("id", ""))

    # Only respond to your own chat
    if cid != str(CHAT_ID):
        return

    if not text.startswith("/"):
        return

    parts   = text.split()
    command = parts[0].lower().split("@")[0]  # handle /cmd@botname format

    print(f"[CMD] {text}")

    if command == "/help":
        tg_send(cmd_help())
    elif command == "/add":
        tg_send(cmd_add(parts))
    elif command == "/del" or command == "/delete" or command == "/remove":
        tg_send(cmd_del(parts))
    elif command == "/list":
        tg_send(cmd_list())
    elif command == "/status":
        tg_send("⏳ Fetching live data...")
        tg_send(cmd_status())
    else:
        tg_send(f"❓ Unknown command: {command}\nSend /help for the list.")

# ══════════════════════════════════════════════
#  TRACKER LOOP (runs in background thread)
# ══════════════════════════════════════════════

def check_trader(address: str):
    """Check one wallet for new fills and position changes."""
    data      = state["wallets"].get(address)
    if not data:
        return

    fill_ids  = set(data.get("fill_ids", []))
    positions = data.get("positions", {})
    changed   = False

    # Fills
    fills     = hl_fills(address)
    new_fills = []
    for fill in fills:
        fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
        if fid not in fill_ids:
            fill_ids.add(fid)
            new_fills.append(fill)
            changed = True

    new_fills.sort(key=lambda f: f.get("time", 0))
    for fill in new_fills:
        tg_send(msg_fill(fill, address))
        print(f"  [{data['label']}] Fill: {fill.get('coin')} @ {fill.get('px')}")
        time.sleep(0.5)

    # Positions
    current = hl_positions(address)

    for asset, pos in current.items():
        if asset not in positions:
            tg_send(msg_opened(asset, pos, address))
            print(f"  [{data['label']}] Opened: {asset}")
            changed = True

    for asset in list(positions.keys()):
        if asset not in current:
            tg_send(msg_closed(asset, positions[asset], address))
            print(f"  [{data['label']}] Closed: {asset}")
            changed = True

    for asset, new_pos in current.items():
        if asset in positions:
            old_szi = float(positions[asset].get("szi", 0))
            new_szi = float(new_pos.get("szi", 0))
            if abs(abs(new_szi) - abs(old_szi)) > 0.0001:
                tg_send(msg_updated(asset, positions[asset], new_pos, address))
                print(f"  [{data['label']}] Updated: {asset}")
                changed = True

    # Update state
    state["wallets"][address]["fill_ids"]  = list(fill_ids)
    state["wallets"][address]["positions"] = current
    if changed:
        save_state()


def tracker_loop():
    """Background thread — polls all wallets continuously."""
    print("[Tracker] Background loop started.")
    while True:
        wallets = list(get_wallets().keys())  # snapshot to avoid mutation issues
        for address in wallets:
            try:
                label = state["wallets"].get(address, {}).get("label", "?")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking {label}...",
                      end=" ")
                check_trader(address)
                print("ok")
            except Exception as e:
                print(f"\n[Tracker Error] {address[:10]}: {e}")
            time.sleep(2)
        time.sleep(POLL_INTERVAL)

# ══════════════════════════════════════════════
#  COMMAND POLLING LOOP (main thread)
# ══════════════════════════════════════════════

def command_loop():
    """Main thread — polls Telegram for commands."""
    print("[Commands] Listening for Telegram commands...")
    offset = state.get("tg_offset", 0)

    while True:
        try:
            updates = tg_get_updates(offset)
            for update in updates:
                uid = update.get("update_id", 0)
                handle_update(update)
                offset = uid + 1
                state["tg_offset"] = offset
                save_state()
        except Exception as e:
            print(f"[Command Loop Error] {e}")
        time.sleep(2)

# ══════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════

def startup():
    print(f"\n{'='*50}")
    print(f"  HYPERLIQUID TRACKER v4  (Interactive)")
    print(f"  Tracking {len(get_wallets())} wallet(s)")
    print(f"{'='*50}\n")

    wallets = get_wallets()
    count   = len(wallets)

    if count == 0:
        tg_send(
            "✅ <b>HL Tracker is LIVE!</b>\n"
            "{'─'*30}\n"
            "No wallets tracked yet.\n\n"
            "Add one with:\n"
            "/add <code>0xADDRESS</code> <code>Nickname</code>\n\n"
            "Send /help for all commands."
        )
    else:
        wallet_list = "\n".join(
            f"  • <b>{d['label']}</b>  <code>{short_addr(a)}</code>"
            for a, d in wallets.items()
        )
        tg_send(
            f"✅ <b>HL Tracker is LIVE!</b>\n"
            f"{'─'*30}\n"
            f"Tracking <b>{count}</b> wallet(s):\n"
            f"{wallet_list}\n\n"
            f"📡 Polling every {POLL_INTERVAL}s\n"
            f"Send /help for commands."
        )

# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    startup()

    # Tracker runs in background thread
    t = threading.Thread(target=tracker_loop, daemon=True)
    t.start()

    # Command listener runs on main thread
    command_loop()