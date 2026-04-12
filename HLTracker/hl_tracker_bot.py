"""
╔══════════════════════════════════════════════════════════╗
║   HYPERLIQUID MULTI-WALLET TRACKER  v7                   ║
╚══════════════════════════════════════════════════════════╝

TELEGRAM COMMANDS:
  /add  0xADDRESS  Nickname    — start tracking a wallet
  /del  0xADDRESS              — stop tracking a wallet
  /list                        — show all tracked wallets
  /status                      — show live positions & PnL
  /help                        — show command list
  /setfilter 0xADDRESS min_notional group_secs  — set per-wallet filters
  /filters                     — show current filter settings per wallet

NEW IN v7:
  - Per-wallet filters: min notional (kills dust) + grouping window (kills TWAP spam)
  - /setfilter command to configure filters from Telegram without editing code
  - /filters command to view current filter settings
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

# ── DEFAULT per-wallet filter values ────────────────────────────────────
# These apply to wallets that have no custom filter set.
# Override per wallet using /setfilter command in Telegram.
DEFAULT_MIN_NOTIONAL = 0      # 0 = no filter, alert on all fills
DEFAULT_GROUP_SECS   = 0      # 0 = no grouping, one message per fill

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


def get_filter(address: str) -> tuple:
    """Return (min_notional, group_secs) for a wallet, falling back to defaults."""
    with state_lock:
        w = state.get("wallets", {}).get(address, {})
        min_notional = w.get("min_notional", DEFAULT_MIN_NOTIONAL)
        group_secs   = w.get("group_secs",   DEFAULT_GROUP_SECS)
    return float(min_notional), float(group_secs)

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
        "/setfilter <code>0xADDRESS</code> <code>min$</code> <code>groupSecs</code>\n"
        "  → Set filters for a wallet\n"
        "  → min$: min notional per fill (0=all)\n"
        "  → groupSecs: bundle fills within N secs (0=off)\n"
        "  → Example: /setfilter 0xABC 500 300\n\n"
        "/filters\n"
        "  → Show filter settings for all wallets\n\n"
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


def cmd_setfilter(parts: list) -> str:
    """
    /setfilter 0xADDRESS min_notional group_secs
    Example: /setfilter 0xABC 500 300
      → ignore fills under $500 notional
      → group fills within 300 seconds (5 mins)
    """
    if len(parts) < 4:
        return (
            "❌ Usage:\n"
            "/setfilter <code>0xADDRESS</code> <code>min$</code> <code>groupSecs</code>\n\n"
            "Examples:\n"
            "/setfilter 0xABC... 500 300\n"
            "  → ignore fills under $500, group within 5min\n\n"
            "/setfilter 0xABC... 0 0\n"
            "  → remove all filters for this wallet"
        )

    address = parts[1].strip()
    try:
        min_notional = float(parts[2])
        group_secs   = float(parts[3])
    except ValueError:
        return "❌ min$ and groupSecs must be numbers.\nExample: /setfilter 0xABC 500 300"

    with state_lock:
        wallets = state.get("wallets", {})
        if address not in wallets:
            # Try partial match
            matches = [a for a in wallets if a.lower().startswith(address.lower())]
            if len(matches) == 1:
                address = matches[0]
            else:
                return "❌ Wallet not found. Use /list to see tracked wallets."
        label = wallets[address]["label"]
        state["wallets"][address]["min_notional"] = min_notional
        state["wallets"][address]["group_secs"]   = group_secs

    save_state()

    min_str   = f"${min_notional:,.0f} min notional" if min_notional > 0 else "no min notional filter"
    group_str = f"group within {group_secs:.0f}s"    if group_secs   > 0 else "no grouping"

    return (
        f"✅ Filters updated for <b>{label}</b>\n"
        f"<code>{short_addr(address)}</code>\n"
        f"─────────────────────────────\n"
        f"💰 Fill filter: {min_str}\n"
        f"⏱ Grouping:    {group_str}"
    )


def cmd_filters() -> str:
    """Show current filter settings for all wallets."""
    wallets = get_wallets()
    if not wallets:
        return "📋 No wallets tracked yet."

    lines = f"⚙️ <b>Filter Settings</b>\n{'─'*30}"
    for address, data in wallets.items():
        label        = data.get("label", "?")
        min_notional = data.get("min_notional", DEFAULT_MIN_NOTIONAL)
        group_secs   = data.get("group_secs",   DEFAULT_GROUP_SECS)
        min_str   = f"${float(min_notional):,.0f}" if float(min_notional) > 0 else "none"
        group_str = f"{float(group_secs):.0f}s"    if float(group_secs)   > 0 else "none"
        lines += (
            f"\n\n👤 <b>{label}</b>\n"
            f"   <code>{short_addr(address)}</code>\n"
            f"   💰 Min notional: {min_str}\n"
            f"   ⏱ Group window: {group_str}"
        )
    return lines


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
#  FILL FILTERS (per-wallet)
# ══════════════════════════════════════════════

def apply_filters(fills: list, address: str) -> list:
    """
    Apply this wallet's filters to a list of new fills.
    Returns a list of groups — each group is a list of fills
    that will be reported as one Telegram message.
    """
    min_notional, group_secs = get_filter(address)

    # ── Filter B: minimum notional ─────────────────────────────────────
    if min_notional > 0:
        before  = len(fills)
        fills   = [
            f for f in fills
            if float(f.get("px", 0)) * float(f.get("sz", 0)) >= min_notional
        ]
        skipped = before - len(fills)
        if skipped:
            print(f"  [{get_label(address)}] Skipped {skipped} fill(s) "
                  f"below ${min_notional} notional")

    if not fills:
        return []

    # ── Filter C: group rapid fills on same asset/direction ────────────
    if group_secs <= 0:
        return [[f] for f in fills]   # no grouping — each fill is own group

    sorted_fills = sorted(fills, key=lambda f: f.get("time", 0))
    groups  = []
    current = [sorted_fills[0]]

    for fill in sorted_fills[1:]:
        prev       = current[-1]
        same_asset = fill.get("coin") == prev.get("coin")
        same_dir   = fill.get("dir",  "") == prev.get("dir",  "")
        time_diff  = (fill.get("time", 0) - prev.get("time", 0)) / 1000
        within_win = time_diff <= group_secs

        if same_asset and same_dir and within_win:
            current.append(fill)
        else:
            groups.append(current)
            current = [fill]

    groups.append(current)
    return groups


def msg_fill_group(fills: list, address: str) -> str:
    """Single fill → normal format. Multiple fills → grouped summary."""
    if len(fills) == 1:
        return msg_fill(fills[0], address)

    asset      = fills[0].get("coin", "?")
    side       = fills[0].get("side", "?")
    fill_type  = detect_fill_type(fills[0])
    total_size = sum(float(f.get("sz", 0)) for f in fills)
    total_pnl  = sum(float(f.get("closedPnl", 0) or 0) for f in fills)
    total_fee  = sum(float(f.get("fee", 0)) for f in fills)
    net_pnl    = total_pnl - total_fee
    avg_price  = (
        sum(float(f.get("px", 0)) * float(f.get("sz", 0)) for f in fills)
        / total_size if total_size else 0
    )
    notional   = avg_price * total_size
    t_first    = fills[0].get("time", 0)
    t_last     = fills[-1].get("time", 0)
    span_s     = (t_last - t_first) / 1000

    is_close  = "close" in fill_type.lower()
    is_open   = "open"  in fill_type.lower()
    icon      = "🏁" if is_close else ("🆕" if is_open else "⚡")
    side_str  = "🟢 BUY / LONG" if side == "B" else "🔴 SELL / SHORT"

    return (
        f"{icon} <b>{fill_type} — {asset}</b> "
        f"<i>({len(fills)} fills grouped)</i>\n"
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"{side_str}\n"
        f"💲 Avg Price:  <b>{fmt_usd(avg_price)}</b>\n"
        f"📦 Total Size: <b>{total_size:.4f}</b>\n"
        f"💼 Notional:   <b>{fmt_usd(notional)}</b>\n"
        f"{pnl_emoji(total_pnl)} Realized PnL: <b>{fmt_usd(total_pnl)}</b>\n"
        f"💸 Total Fee:  <b>{fmt_usd(total_fee)}</b>\n"
        f"✅ Net PnL:    <b>{fmt_usd(net_pnl)}</b>\n"
        f"🔢 {len(fills)} fills over {span_s:.0f}s\n"
        f"🕐 {ts_to_str(t_first)} → {ts_to_str(t_last)}"
    )


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

    # Apply per-wallet filters (notional floor + grouping)
    groups = apply_filters(new_fills, address)
    for group in groups:
        if not wallet_exists(address):
            return
        tg_send(msg_fill_group(group, address))
        coin = group[0].get("coin", "?")
        print(f"  [{get_label(address)}] Fill(s): {coin} x{len(group)}")

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
            abs_change = abs(abs(new_szi) - abs(old_szi))
            # Use percentage-based threshold (0.5% of position size)
            # This prevents funding rate micro-adjustments from triggering alerts
            # while still catching real trims/adds regardless of asset size
            pct_change = (abs_change / abs(old_szi)) if old_szi != 0 else 0
            if pct_change >= 0.005:  # only alert if size changed by 0.5% or more
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
        cmd_add(parts)
    elif command in ("/del", "/delete", "/remove"):
        tg_send(cmd_del(parts))
    elif command == "/list":
        tg_send(cmd_list())
    elif command == "/status":
        tg_send("⏳ Fetching live data...")
        tg_send(cmd_status())
    elif command == "/setfilter":
        tg_send(cmd_setfilter(parts))
    elif command == "/filters":
        tg_send(cmd_filters())
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
    print(f"  HYPERLIQUID TRACKER v7")
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