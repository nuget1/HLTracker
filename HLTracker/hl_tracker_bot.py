"""
╔══════════════════════════════════════════════════════════╗
║   HYPERLIQUID MULTI-WALLET TRACKER  v6  (Production)     ║
╚══════════════════════════════════════════════════════════╝

TELEGRAM COMMANDS:
  /add  0xADDRESS  Nickname    — start tracking a wallet
  /del  0xADDRESS              — stop tracking a wallet
  /list                        — show all tracked wallets
  /status                      — show live positions & PnL
  /help                        — show command list

FIXES IN v6:
  - /status called hl_account_state twice per wallet (wasted API calls) → fixed
  - Telegram message length cap (4096 chars) → /status now splits if too long
  - /add blocked the command thread for 10-30s while loading fills → runs in background
  - save_state() called inside state_lock → could deadlock → fixed
  - positions dict in check_trader was shallow copy → nested dicts shared → deep copy
  - Telegram flood limit (30 msgs/sec) → added rate limiter
  - HL API returns None on timeout → positions cleared to {} falsely → now skipped
  - /del with partial address match was case-sensitive → now case-insensitive
  - No retry on HL API failure → transient errors cause missed fills → added retry
"""

import os
import json
import copy
import requests
import time
import threading
from datetime import datetime, timezone

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
POLL_INTERVAL  = 15        # seconds between full cycles
HL_API         = "https://api.hyperliquid.xyz/info"
STATE_FILE     = "/tmp/hl_tracker_state.json"
MAX_TG_LEN     = 4000      # Telegram hard limit is 4096, stay under safely
API_RETRIES    = 3         # retry failed HL API calls this many times
API_RETRY_WAIT = 3         # seconds between retries

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("❌ TELEGRAM_TOKEN and CHAT_ID must be set as environment variables!")

# ══════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════

def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                print(f"[State] Loaded: {len(data.get('wallets', {}))} wallet(s)")
                return data
    except Exception as e:
        print(f"[State] Could not load: {e}")
    return {"wallets": {}, "tg_offset": 0}


state      = load_state()
state_lock = threading.Lock()


def save_state():
    """Save state to disk. Must be called WITHOUT holding state_lock."""
    try:
        with state_lock:
            snapshot = copy.deepcopy(state)
        with open(STATE_FILE, "w") as f:
            json.dump(snapshot, f)
    except Exception as e:
        print(f"[State] Could not save: {e}")


def get_wallets() -> dict:
    with state_lock:
        return copy.deepcopy(state.get("wallets", {}))


def wallet_exists(address: str) -> bool:
    with state_lock:
        return address in state.get("wallets", {})


def get_label(address: str) -> str:
    with state_lock:
        return state.get("wallets", {}).get(address, {}).get("label", short_addr(address))

# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════

TG_BASE      = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
tg_last_send = 0.0
tg_lock      = threading.Lock()


def tg_send(msg: str):
    """Send a Telegram message with flood-rate protection and length splitting."""
    global tg_last_send

    # Split messages that exceed Telegram's limit
    chunks = []
    while len(msg) > MAX_TG_LEN:
        # Find a clean split point (newline) near the limit
        split_at = msg.rfind("\n", 0, MAX_TG_LEN)
        if split_at == -1:
            split_at = MAX_TG_LEN
        chunks.append(msg[:split_at])
        msg = msg[split_at:].lstrip("\n")
    chunks.append(msg)

    for chunk in chunks:
        if not chunk.strip():
            continue
        with tg_lock:
            # Enforce minimum 0.05s between sends (~20/sec, well under 30/sec limit)
            elapsed = time.time() - tg_last_send
            if elapsed < 0.05:
                time.sleep(0.05 - elapsed)
            try:
                r = requests.post(f"{TG_BASE}/sendMessage", json={
                    "chat_id":    CHAT_ID,
                    "text":       chunk,
                    "parse_mode": "HTML"
                }, timeout=10)
                if not r.ok:
                    print(f"[Telegram] Error {r.status_code}: {r.text[:100]}")
            except Exception as e:
                print(f"[Telegram] Exception: {e}")
            tg_last_send = time.time()


def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(f"{TG_BASE}/getUpdates", params={
            "offset": offset, "timeout": 5
        }, timeout=10)
        if r.ok:
            return r.json().get("result", [])
    except Exception as e:
        print(f"[Telegram] getUpdates error: {e}")
    return []

# ══════════════════════════════════════════════
#  HYPERLIQUID API  (with retry)
# ══════════════════════════════════════════════

def hl(payload: dict, retries: int = API_RETRIES):
    """POST to Hyperliquid API with automatic retry on failure."""
    for attempt in range(retries):
        try:
            r = requests.post(HL_API, json=payload, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                print(f"[HL API] Attempt {attempt+1} failed: {e} — retrying in {API_RETRY_WAIT}s")
                time.sleep(API_RETRY_WAIT)
            else:
                print(f"[HL API] All {retries} attempts failed for {payload.get('type')}: {e}")
    return None


def hl_fills(address: str) -> list:
    data = hl({"type": "userFills", "user": address})
    return data if isinstance(data, list) else []


def hl_account_state(address: str):
    return hl({"type": "clearinghouseState", "user": address})


def hl_positions_from_state(account_state: dict) -> dict:
    """Parse positions out of an already-fetched account state dict."""
    if not account_state:
        return {}
    result = {}
    for item in account_state.get("assetPositions", []):
        pos   = item.get("position", {})
        asset = pos.get("coin")
        szi   = float(pos.get("szi", 0))
        if asset and szi != 0:
            result[asset] = pos
    return result


def validate_wallet(address: str) -> bool:
    s = hl_account_state(address)
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
    return f"👤 <b>{get_label(address)}</b>  <code>{short_addr(address)}</code>\n"

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
#  COMMANDS
# ══════════════════════════════════════════════

def cmd_help() -> str:
    return (
        "🤖 <b>HL Tracker Commands</b>\n"
        "─────────────────────────────\n"
        "/add <code>0xADDRESS</code> <code>Nickname</code>\n"
        "  → Start tracking a wallet\n\n"
        "/del <code>0xADDRESS</code>\n"
        "  → Stop tracking a wallet\n\n"
        "/list\n"
        "  → Show all tracked wallets\n\n"
        "/status\n"
        "  → Live positions & PnL for all wallets\n\n"
        "/help\n"
        "  → Show this message"
    )


def _do_add(address: str, label: str):
    """
    Runs in a background thread so /add doesn't block the command loop.
    Loads full fill history, then adds the wallet to state.
    """
    try:
        tg_send(f"🔍 Validating <code>{short_addr(address)}</code>...")

        if not validate_wallet(address):
            tg_send("❌ Could not find that wallet on Hyperliquid. Check the address.")
            return

        tg_send(f"⏳ Loading fill history for <b>{label}</b>...")

        fills    = hl_fills(address)
        fill_ids = []
        for fill in fills:
            fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
            fill_ids.append(fid)

        # Fetch positions and account value in one call
        account_s = hl_account_state(address)
        positions = hl_positions_from_state(account_s)
        margin    = account_s.get("marginSummary", {}) if account_s else {}
        acct_val  = float(margin.get("accountValue", 0))

        with state_lock:
            if "wallets" not in state:
                state["wallets"] = {}
            state["wallets"][address] = {
                "label":     label,
                "fill_ids":  fill_ids,
                "positions": positions,
                "added_at":  datetime.utcnow().isoformat()
            }
        save_state()

        pos_count = len(positions)
        pos_str   = f"{pos_count} open position(s)" if pos_count else "no open positions"

        tg_send(
            f"✅ Now tracking <b>{label}</b>\n"
            f"<code>{short_addr(address)}</code>\n"
            f"─────────────────────────────\n"
            f"📊 Currently: {pos_str}\n"
            f"🏦 Account value: {fmt_usd(acct_val)}\n"
            f"📜 History: {len(fill_ids)} past fills pre-loaded\n"
            f"🔔 You'll only be alerted on NEW trades from now."
        )

    except Exception as e:
        tg_send(f"❌ Failed to add wallet: {e}")
        print(f"[/add error] {e}")


def cmd_add(parts: list):
    if len(parts) < 2:
        tg_send(
            "❌ Usage:\n"
            "/add <code>0xADDRESS</code> <code>Nickname</code>\n\n"
            "Example:\n"
            "/add 0xABC...123 Whale Guy"
        )
        return

    address = parts[1].strip()
    label   = " ".join(parts[2:]).strip() if len(parts) > 2 else short_addr(address)

    if not address.startswith("0x") or len(address) < 10:
        tg_send("❌ Invalid address — must start with 0x and be a full wallet address.")
        return

    with state_lock:
        wallets = state.get("wallets", {})
        if address in wallets:
            existing = wallets[address]["label"]
            tg_send(f"⚠️ Already tracking <b>{existing}</b>\n<code>{short_addr(address)}</code>")
            return

    # Run in background so command loop isn't blocked for 10-30s
    t = threading.Thread(target=_do_add, args=(address, label), daemon=True)
    t.start()


def cmd_del(parts: list) -> str:
    if len(parts) < 2:
        return "❌ Usage: /del <code>0xADDRESS</code>"

    query = parts[1].strip().lower()

    with state_lock:
        wallets = state.get("wallets", {})

        # Exact match first
        if parts[1].strip() in wallets:
            address = parts[1].strip()
        else:
            # Case-insensitive partial match
            matches = [a for a in wallets if a.lower().startswith(query)]
            if len(matches) == 1:
                address = matches[0]
            elif len(matches) > 1:
                labels = "\n".join(
                    f"  • {wallets[a]['label']} — <code>{a}</code>"
                    for a in matches
                )
                return f"⚠️ Multiple matches:\n{labels}\n\nUse the full address."
            else:
                return "❌ Wallet not found.\nUse /list to see tracked wallets."

        label = wallets[address]["label"]
        del state["wallets"][address]

    save_state()
    return f"🗑️ Stopped tracking <b>{label}</b>\n<code>{short_addr(address)}</code>"


def cmd_list() -> str:
    wallets = get_wallets()
    if not wallets:
        return (
            "📋 No wallets tracked yet.\n\n"
            "Add one with:\n"
            "/add <code>0xADDRESS</code> <code>Nickname</code>"
        )

    lines = f"📋 <b>Tracked Wallets ({len(wallets)})</b>\n{'─'*30}"
    for i, (address, data) in enumerate(wallets.items(), 1):
        label     = data.get("label", "?")
        pos_count = len(data.get("positions", {}))
        lines += (
            f"\n{i}. <b>{label}</b>\n"
            f"   <code>{address}</code>\n"
            f"   Open positions: {pos_count}"
        )
    return lines


def cmd_status() -> str:
    wallets = get_wallets()
    if not wallets:
        return "📊 No wallets tracked. Use /add to start."

    lines = f"📊 <b>Live Status ({len(wallets)} wallets)</b>\n{'─'*30}"

    for address, data in wallets.items():
        label = data.get("label", "?")

        # Single API call per wallet — parse both positions and margin from it
        account_s  = hl_account_state(address)
        margin     = account_s.get("marginSummary", {}) if account_s else {}
        acct_val   = float(margin.get("accountValue", 0))
        positions  = hl_positions_from_state(account_s)
        total_upnl = sum(float(p.get("unrealizedPnl", 0)) for p in positions.values())

        lines += f"\n\n👤 <b>{label}</b>  <code>{short_addr(address)}</code>"
        lines += f"\n🏦 Account: <b>{fmt_usd(acct_val)}</b> | UPnL: <b>{fmt_usd(total_upnl)}</b>"

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
                    f" | {fmt_usd(entry)} | UPnL <b>{fmt_usd(upnl)}</b>"
                )
        else:
            lines += "\n  (no open positions)"

        time.sleep(0.3)  # don't hammer the API

    return lines  # tg_send handles splitting if too long

# ══════════════════════════════════════════════
#  TRACKER LOOP
# ══════════════════════════════════════════════

def check_trader(address: str):
    if not wallet_exists(address):
        return

    with state_lock:
        data = state["wallets"].get(address)
        if not data:
            return
        fill_ids  = set(data.get("fill_ids", []))
        positions = copy.deepcopy(data.get("positions", {}))  # deep copy — fix shared ref bug

    changed   = False
    new_fills = []

    # ── Fills ──────────────────────────────────
    fills = hl_fills(address)
    for fill in fills:
        fid = f"{fill.get('time')}_{fill.get('oid')}_{fill.get('tid', '')}"
        if fid not in fill_ids:
            fill_ids.add(fid)
            new_fills.append(fill)
            changed = True

    new_fills.sort(key=lambda f: f.get("time", 0))

    for fill in new_fills:
        if not wallet_exists(address):
            return
        tg_send(msg_fill(fill, address))
        print(f"  [{get_label(address)}] Fill: {fill.get('coin')} @ {fill.get('px')}")

    # ── Positions ──────────────────────────────
    if not wallet_exists(address):
        return

    # Single API call — reuse for both positions and any future fields
    account_s = hl_account_state(address)

    # IMPORTANT: if API call failed entirely, skip position checks this cycle
    # to avoid falsely triggering "position closed" alerts
    if account_s is None:
        print(f"  [{get_label(address)}] API returned None — skipping position check this cycle")
        # Still save updated fill_ids
        if changed and wallet_exists(address):
            with state_lock:
                if address in state.get("wallets", {}):
                    state["wallets"][address]["fill_ids"] = list(fill_ids)
            save_state()
        return

    current = hl_positions_from_state(account_s)

    for asset, pos in current.items():
        if not wallet_exists(address):
            return
        if asset not in positions:
            tg_send(msg_opened(asset, pos, address))
            print(f"  [{get_label(address)}] Opened: {asset}")
            changed = True

    for asset in list(positions.keys()):
        if not wallet_exists(address):
            return
        if asset not in current:
            tg_send(msg_closed(asset, positions[asset], address))
            print(f"  [{get_label(address)}] Closed: {asset}")
            changed = True

    for asset, new_pos in current.items():
        if not wallet_exists(address):
            return
        if asset in positions:
            old_szi = float(positions[asset].get("szi", 0))
            new_szi = float(new_pos.get("szi", 0))
            if abs(abs(new_szi) - abs(old_szi)) > 0.0001:
                tg_send(msg_updated(asset, positions[asset], new_pos, address))
                print(f"  [{get_label(address)}] Updated: {asset}")
                changed = True

    # Save state
    if wallet_exists(address):
        with state_lock:
            if address in state.get("wallets", {}):
                state["wallets"][address]["fill_ids"]  = list(fill_ids)
                state["wallets"][address]["positions"] = current
        if changed:
            save_state()


def tracker_loop():
    print("[Tracker] Background loop started.")
    while True:
        addresses = list(get_wallets().keys())
        for address in addresses:
            try:
                label = get_label(address)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] {label}...", end=" ")
                check_trader(address)
                print("ok")
            except Exception as e:
                print(f"\n[Tracker Error] {address[:10]}: {e}")
            time.sleep(2)
        time.sleep(POLL_INTERVAL)

# ══════════════════════════════════════════════
#  COMMAND LOOP
# ══════════════════════════════════════════════

def handle_update(update: dict):
    msg  = update.get("message", {})
    text = msg.get("text", "").strip()
    cid  = str(msg.get("chat", {}).get("id", ""))

    if cid != str(CHAT_ID):
        return
    if not text.startswith("/"):
        return

    parts   = text.split()
    command = parts[0].lower().split("@")[0]
    print(f"[CMD] {text}")

    if   command == "/help":
        tg_send(cmd_help())
    elif command == "/add":
        cmd_add(parts)   # returns immediately, work done in background thread
    elif command in ("/del", "/delete", "/remove"):
        tg_send(cmd_del(parts))
    elif command == "/list":
        tg_send(cmd_list())
    elif command == "/status":
        tg_send("⏳ Fetching live data...")
        tg_send(cmd_status())
    else:
        tg_send(f"❓ Unknown command: <code>{command}</code>\nSend /help for the list.")


def command_loop():
    print("[Commands] Listening for Telegram commands...")
    with state_lock:
        offset = state.get("tg_offset", 0)

    while True:
        try:
            updates = tg_get_updates(offset)
            for update in updates:
                uid = update.get("update_id", 0)
                handle_update(update)
                offset = uid + 1
                with state_lock:
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
    print(f"  HYPERLIQUID TRACKER v6")
    print(f"  Wallets: {len(get_wallets())}")
    print(f"{'='*50}\n")

    wallets = get_wallets()

    if not wallets:
        tg_send(
            "✅ <b>HL Tracker is LIVE!</b>\n"
            "─────────────────────────────\n"
            "No wallets tracked yet.\n\n"
            "Add one:\n"
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
            f"─────────────────────────────\n"
            f"Tracking <b>{len(wallets)}</b> wallet(s):\n"
            f"{wallet_list}\n\n"
            f"📡 Polling every {POLL_INTERVAL}s\n"
            f"Send /help for commands."
        )

# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    startup()
    threading.Thread(target=tracker_loop, daemon=True).start()
    command_loop()