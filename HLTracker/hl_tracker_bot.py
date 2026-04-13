"""
╔══════════════════════════════════════════════════════════╗
║   HYPERLIQUID MULTI-WALLET TRACKER  v8                   ║
╚══════════════════════════════════════════════════════════╝

TELEGRAM COMMANDS:
  /add 0xADDRESS Nickname, 0xADDRESS2 Nickname2  — track wallet(s)
  /del 0xADDRESS, 0xADDRESS2                     — stop tracking wallet(s)
  /list                                           — show all tracked wallets
  /status                                         — live positions for all wallets
  /setfilter 0xADDRESS 20                         — set size change threshold %
  /filters                                        — show filter settings
  /help                                           — show command list

WHAT IT TRACKS (position-based, no fill spam):
  - New asset appears in positions  → OPENED alert
  - Asset disappears from positions → CLOSED alert
  - Position size changes by ≥ threshold % → UPDATED alert
    (ignores TWAP micro-fills and funding rate noise)
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

TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID              = os.environ.get("CHAT_ID", "")
POLL_INTERVAL        = 15     # seconds between full check cycles
HL_API               = "https://api.hyperliquid.xyz/info"
STATE_FILE           = "/tmp/hl_tracker_state.json"
MAX_TG_LEN           = 4000
API_RETRIES          = 3
API_RETRY_WAIT       = 3
DEFAULT_THRESHOLD    = 20.0   # % size change needed to trigger UPDATED alert

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
    """Save state to disk. Must NOT be called while holding state_lock."""
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
        return state.get("wallets", {}).get(address, {}).get("label", address)


def get_threshold(address: str) -> float:
    with state_lock:
        return float(state.get("wallets", {}).get(address, {}).get(
            "threshold", DEFAULT_THRESHOLD))

# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════

TG_BASE      = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
tg_last_send = 0.0
tg_lock      = threading.Lock()


def tg_send(msg: str):
    global tg_last_send
    chunks = []
    while len(msg) > MAX_TG_LEN:
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
#  HYPERLIQUID API
# ══════════════════════════════════════════════

def hl(payload: dict, retries: int = API_RETRIES):
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
                print(f"[HL API] All {retries} attempts failed: {e}")
    return None


def hl_account_state(address: str):
    return hl({"type": "clearinghouseState", "user": address})


def hl_all_mids() -> dict:
    """Returns {asset: mark_price} for all tradeable assets."""
    data = hl({"type": "allMids"})
    if isinstance(data, dict):
        return {k: float(v) for k, v in data.items()}
    return {}


def hl_positions_from_state(account_state: dict) -> dict:
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
    return hl_account_state(address) is not None

# ══════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%b %d %H:%M:%S UTC")

def fmt_usd(val: float) -> str:
    return f"${val:,.2f}" if abs(val) >= 1000 else f"${val:.2f}"

def fmt_size(val: float) -> str:
    """Format raw contract size cleanly — trim trailing zeros."""
    if val == int(val):
        return f"{int(val):,}"
    return f"{val:,.4f}".rstrip("0").rstrip(".")

def pnl_str(val: float, pos: dict = None) -> str:
    """PnL with +/- sign, emoji, and optional % from returnOnEquity."""
    sign  = "+" if val >= 0 else ""
    emoji = "🟢" if val >= 0 else "🔴"
    base  = f"{emoji} {sign}{fmt_usd(val)}"
    if pos is not None:
        try:
            roe = float(pos.get("returnOnEquity", 0)) * 100
            pct_sign = "+" if roe >= 0 else ""
            base += f"  ({pct_sign}{roe:.2f}%)"
        except Exception:
            pass
    return base

def trader_header(address: str) -> str:
    label = get_label(address)
    return f"👤 <b>{label}</b>\n<code>{address}</code>\n"

# ══════════════════════════════════════════════
#  ALERT FORMATTERS
# ══════════════════════════════════════════════

def msg_opened(asset: str, pos: dict, mids: dict, address: str) -> str:
    szi      = float(pos.get("szi", 0))
    entry    = float(pos.get("entryPx", 0))
    liq      = float(pos.get("liquidationPx") or 0)
    upnl     = float(pos.get("unrealizedPnl", 0))
    margin   = float(pos.get("marginUsed", 0))
    lev      = pos.get("leverage", {})
    lev_val  = lev.get("value", "?") if isinstance(lev, dict) else "?"
    mark     = mids.get(asset, entry)
    value    = abs(szi) * mark
    side_str = "🔴 SHORT" if szi < 0 else "🟢 LONG"

    return (
        f"🆕 <b>OPENED — {asset}</b>\n"
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"{side_str}  {lev_val}x\n"
        f"\n"
        f"📦 Size:    <b>{fmt_size(abs(szi))}</b>\n"
        f"💵 Value:   <b>{fmt_usd(value)}</b>\n"
        f"📍 Entry:   <b>{fmt_usd(entry)}</b>\n"
        f"📊 PnL:     {pnl_str(upnl, pos)}\n"
        f"💣 Liq:     <b>{fmt_usd(liq)}</b>\n"
        f"🏦 Margin:  <b>{fmt_usd(margin)}</b>\n"
        f"\n"
        f"🕐 {now_str()}"
    )


def msg_closed(asset: str, old_pos: dict, mids: dict, address: str) -> str:
    szi      = float(old_pos.get("szi", 0))
    entry    = float(old_pos.get("entryPx", 0))
    upnl     = float(old_pos.get("unrealizedPnl", 0))
    lev      = old_pos.get("leverage", {})
    lev_val  = lev.get("value", "?") if isinstance(lev, dict) else "?"
    mark     = mids.get(asset, entry)
    side_str = "🔴 SHORT" if szi < 0 else "🟢 LONG"

    return (
        f"🏁 <b>CLOSED — {asset}</b>\n"
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"{side_str}  {lev_val}x\n"
        f"\n"
        f"📦 Size was:  <b>{fmt_size(abs(szi))}</b>\n"
        f"📍 Entry was: <b>{fmt_usd(entry)}</b>\n"
        f"📌 Mark:      <b>{fmt_usd(mark)}</b>\n"
        f"📊 Last PnL:  {pnl_str(upnl, old_pos)}\n"
        f"\n"
        f"🕐 {now_str()}"
    )


def msg_updated(asset: str, old_pos: dict, new_pos: dict,
                mids: dict, address: str, pct_change: float) -> str:
    old_szi  = float(old_pos.get("szi", 0))
    new_szi  = float(new_pos.get("szi", 0))
    sz_delta = abs(new_szi) - abs(old_szi)
    action   = "➕ Added" if sz_delta > 0 else "✂️ Trimmed"
    side_str = "🔴 SHORT" if new_szi < 0 else "🟢 LONG"
    entry    = float(new_pos.get("entryPx", 0))
    liq      = float(new_pos.get("liquidationPx") or 0)
    upnl     = float(new_pos.get("unrealizedPnl", 0))
    margin   = float(new_pos.get("marginUsed", 0))
    lev      = new_pos.get("leverage", {})
    lev_val  = lev.get("value", "?") if isinstance(lev, dict) else "?"
    mark     = mids.get(asset, entry)
    value    = abs(new_szi) * mark

    # Dollar value of the change
    delta_val = abs(sz_delta) * mark
    delta_str = f"+{fmt_usd(delta_val)}" if sz_delta > 0 else f"-{fmt_usd(delta_val)}"

    return (
        f"🔄 <b>UPDATED — {asset}</b>\n"
        f"{trader_header(address)}"
        f"{'─'*30}\n"
        f"{side_str}  {lev_val}x  |  {action}\n"
        f"\n"
        f"📦 Size:    {fmt_size(abs(old_szi))} → <b>{fmt_size(abs(new_szi))}</b>\n"
        f"           ({delta_str}, {pct_change:.1f}% change)\n"
        f"💵 Value:   <b>{fmt_usd(value)}</b>\n"
        f"📍 Entry:   <b>{fmt_usd(entry)}</b>\n"
        f"📌 Mark:    <b>{fmt_usd(mark)}</b>\n"
        f"📊 PnL:     {pnl_str(upnl, new_pos)}\n"
        f"💣 Liq:     <b>{fmt_usd(liq)}</b>\n"
        f"🏦 Margin:  <b>{fmt_usd(margin)}</b>\n"
        f"\n"
        f"🕐 {now_str()}"
    )

# ══════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════

def cmd_help() -> str:
    return (
        "🤖 <b>HL Tracker Commands</b>\n"
        "─────────────────────────────\n"
        "/add <code>0xADDR Nickname, 0xADDR2 Nickname2</code>\n"
        "  → Track one or more wallets\n\n"
        "/del <code>0xADDR, 0xADDR2</code>\n"
        "  → Stop tracking one or more wallets\n\n"
        "/list\n"
        "  → Show all tracked wallets\n\n"
        "/status\n"
        "  → Live positions for all wallets\n"
        "/status <code>2</code>\n"
        "  → Live positions for wallet #2 only\n\n"
        "/setfilter <code>0xADDR 20</code>\n"
        "  → Set size change threshold % for a wallet\n"
        "  → Default is 20% — alerts when size changes by ≥ this\n\n"
        "/filters\n"
        "  → Show threshold settings for all wallets\n\n"
        "/help\n"
        "  → Show this message"
    )


def _do_add(address: str, label: str):
    """Background thread: validate, load positions, register wallet."""
    try:
        tg_send(f"🔍 Validating <code>{address}</code>...")

        if not validate_wallet(address):
            tg_send(f"❌ Could not find <code>{address}</code> on Hyperliquid.")
            return

        account_s = hl_account_state(address)
        positions = hl_positions_from_state(account_s)
        margin    = account_s.get("marginSummary", {}) if account_s else {}
        acct_val  = float(margin.get("accountValue", 0))

        with state_lock:
            if "wallets" not in state:
                state["wallets"] = {}
            state["wallets"][address] = {
                "label":     label,
                "positions": positions,
                "added_at":  datetime.utcnow().isoformat()
            }
        save_state()

        pos_count = len(positions)
        pos_str   = f"{pos_count} open position(s)" if pos_count else "no open positions"

        tg_send(
            f"✅ Now tracking <b>{label}</b>\n"
            f"<code>{address}</code>\n"
            f"─────────────────────────────\n"
            f"📊 Currently: {pos_str}\n"
            f"🏦 Account value: {fmt_usd(acct_val)}\n"
            f"🔔 Alerts on: position opens, closes, ≥{DEFAULT_THRESHOLD}% size changes"
        )

    except Exception as e:
        tg_send(f"❌ Failed to add <code>{address}</code>: {e}")
        print(f"[/add error] {e}")


def cmd_add(parts: list):
    if len(parts) < 2:
        tg_send(
            "❌ Usage:\n"
            "/add <code>0xADDRESS Nickname</code>\n\n"
            "Multiple:\n"
            "/add <code>0xABC Whale, 0xDEF Sniper</code>"
        )
        return

    raw     = " ".join(parts[1:])
    entries = [e.strip() for e in raw.split(",") if e.strip()]

    for entry in entries:
        tokens  = entry.split()
        address = tokens[0].strip()
        label   = " ".join(tokens[1:]).strip() if len(tokens) > 1 else address[:10] + "..."

        if not address.startswith("0x") or len(address) < 10:
            tg_send(f"❌ Invalid address: <code>{address}</code>")
            continue

        with state_lock:
            if address in state.get("wallets", {}):
                existing = state["wallets"][address]["label"]
                tg_send(f"⚠️ Already tracking <b>{existing}</b>\n<code>{address}</code>")
                continue

        threading.Thread(target=_do_add, args=(address, label), daemon=True).start()


def cmd_del(parts: list) -> str:
    if len(parts) < 2:
        return (
            "❌ Usage:\n"
            "/del <code>0xADDRESS</code>\n\n"
            "Multiple:\n"
            "/del <code>0xABC, 0xDEF</code>"
        )

    raw     = " ".join(parts[1:])
    queries = [q.strip() for q in raw.split(",") if q.strip()]
    results = []

    for query in queries:
        query_lower = query.lower()
        with state_lock:
            wallets = state.get("wallets", {})
            if query in wallets:
                address = query
            else:
                matches = [a for a in wallets if a.lower().startswith(query_lower)]
                if len(matches) == 1:
                    address = matches[0]
                elif len(matches) > 1:
                    labels = ", ".join(wallets[a]["label"] for a in matches)
                    results.append(f"⚠️ <code>{query[:10]}...</code> matched multiple ({labels}) — use full address")
                    continue
                else:
                    results.append(f"❌ Not found: <code>{query[:10]}...</code>")
                    continue
            label = wallets[address]["label"]
            del state["wallets"][address]

        save_state()
        results.append(f"🗑️ Stopped tracking <b>{label}</b>\n<code>{address}</code>")

    return "\n\n".join(results)


def cmd_setfilter(parts: list) -> str:
    if len(parts) < 3:
        return (
            "❌ Usage: /setfilter <code>0xADDRESS threshold%</code>\n\n"
            "Example: /setfilter 0xABC 20\n"
            "  → alert when size changes by ≥ 20%\n\n"
            "Set to 0 to disable size-change alerts entirely."
        )

    address = parts[1].strip()
    try:
        threshold = float(parts[2])
    except ValueError:
        return "❌ Threshold must be a number. Example: /setfilter 0xABC 20"

    with state_lock:
        wallets = state.get("wallets", {})
        if address not in wallets:
            matches = [a for a in wallets if a.lower().startswith(address.lower())]
            if len(matches) == 1:
                address = matches[0]
            else:
                return "❌ Wallet not found. Use /list to see tracked wallets."
        label = wallets[address]["label"]
        state["wallets"][address]["threshold"] = threshold

    save_state()

    if threshold == 0:
        thresh_str = "disabled (no size-change alerts)"
    else:
        thresh_str = f"≥ {threshold:.0f}% size change"

    return (
        f"✅ Filter updated for <b>{label}</b>\n"
        f"<code>{address}</code>\n"
        f"─────────────────────────────\n"
        f"📐 Threshold: {thresh_str}"
    )


def cmd_filters() -> str:
    wallets = get_wallets()
    if not wallets:
        return "📋 No wallets tracked yet."

    lines = f"⚙️ <b>Filter Settings</b>\n{'─'*30}"
    for address, data in wallets.items():
        label     = data.get("label", "?")
        threshold = data.get("threshold", DEFAULT_THRESHOLD)
        thresh_str = f"≥ {float(threshold):.0f}% size change" if float(threshold) > 0 else "disabled"
        lines += (
            f"\n\n👤 <b>{label}</b>\n"
            f"<code>{address}</code>\n"
            f"📐 Threshold: {thresh_str}"
        )
    return lines


def cmd_list() -> str:
    wallets = get_wallets()
    if not wallets:
        return (
            "📋 No wallets tracked yet.\n\n"
            "Add one with:\n"
            "/add <code>0xADDRESS Nickname</code>"
        )

    lines = f"📋 <b>Tracked Wallets ({len(wallets)})</b>\n{'─'*30}"
    for i, (address, data) in enumerate(wallets.items(), 1):
        label     = data.get("label", "?")
        pos_count = len(data.get("positions", {}))
        lines += (
            f"\n\n{i}. <b>{label}</b>\n"
            f"<code>{address}</code>\n"
            f"Open positions: {pos_count}"
        )
    return lines


def cmd_status(parts: list = None) -> str:
    wallets     = get_wallets()
    wallet_list = list(wallets.items())

    if not wallet_list:
        return "📊 No wallets tracked. Use /add to start."

    # /status <number> — single wallet lookup
    if parts and len(parts) > 1:
        try:
            idx = int(parts[1]) - 1
            if idx < 0 or idx >= len(wallet_list):
                return f"❌ Invalid number. Use 1–{len(wallet_list)}."
            wallet_list = [wallet_list[idx]]
        except ValueError:
            return "❌ Usage: /status or /status <number>"

    mids  = hl_all_mids()
    title = "📊 <b>Live Status</b>" if len(wallet_list) == len(wallets) \
            else f"📊 <b>Live Status — Wallet {parts[1]}</b>"
    lines = f"{title}\n{'─'*30}"

    for address, data in wallet_list:
        label      = data.get("label", "?")
        account_s  = hl_account_state(address)
        margin     = account_s.get("marginSummary", {}) if account_s else {}
        acct_val   = float(margin.get("accountValue", 0))
        positions  = hl_positions_from_state(account_s)
        total_upnl = sum(float(p.get("unrealizedPnl", 0)) for p in positions.values())

        lines += (
            f"\n\n👤 <b>{label}</b>\n"
            f"<code>{address}</code>\n"
            f"🏦 Acct Value: <b>{fmt_usd(acct_val)}</b>\n"
            f"📊 Total PnL:  {pnl_str(total_upnl)}"
        )

        if positions:
            for asset, pos in positions.items():
                szi     = float(pos.get("szi", 0))
                upnl    = float(pos.get("unrealizedPnl", 0))
                entry   = float(pos.get("entryPx", 0))
                liq     = float(pos.get("liquidationPx") or 0)
                margin_pos = float(pos.get("marginUsed", 0))
                mark    = mids.get(asset, entry)
                value   = abs(szi) * mark
                lev     = pos.get("leverage", {})
                lev_val = lev.get("value", "?") if isinstance(lev, dict) else "?"
                side    = "🔴 SHORT" if szi < 0 else "🟢 LONG"
                lines  += (
                    f"\n\n  <b>{asset}</b>  {side}  {lev_val}x\n"
                    f"  📦 Size:   <b>{fmt_size(abs(szi))}</b>\n"
                    f"  💵 Value:  <b>{fmt_usd(value)}</b>\n"
                    f"  📍 Entry:  <b>{fmt_usd(entry)}</b>\n"
                    f"  📌 Mark:   <b>{fmt_usd(mark)}</b>\n"
                    f"  📊 PnL:    {pnl_str(upnl, pos)}\n"
                    f"  💣 Liq:    <b>{fmt_usd(liq)}</b>\n"
                    f"  🏦 Margin: <b>{fmt_usd(margin_pos)}</b>"
                )
        else:
            lines += "\n  (no open positions)"

        time.sleep(0.3)

    return lines

# ══════════════════════════════════════════════
#  TRACKER LOOP
# ══════════════════════════════════════════════

def check_trader(address: str, mids: dict):
    if not wallet_exists(address):
        return

    with state_lock:
        data = state["wallets"].get(address)
        if not data:
            return
        positions = copy.deepcopy(data.get("positions", {}))

    threshold = get_threshold(address)

    account_s = hl_account_state(address)
    if account_s is None:
        print(f"  [{get_label(address)}] API returned None — skipping this cycle")
        return

    current = hl_positions_from_state(account_s)
    changed = False

    # ── New positions ──────────────────────────
    for asset, pos in current.items():
        if not wallet_exists(address):
            return
        if asset not in positions:
            tg_send(msg_opened(asset, pos, mids, address))
            print(f"  [{get_label(address)}] Opened: {asset}")
            changed = True

    # ── Closed positions ───────────────────────
    for asset in list(positions.keys()):
        if not wallet_exists(address):
            return
        if asset not in current:
            tg_send(msg_closed(asset, positions[asset], mids, address))
            print(f"  [{get_label(address)}] Closed: {asset}")
            changed = True

    # ── Size changes ───────────────────────────
    if threshold > 0:
        for asset, new_pos in current.items():
            if not wallet_exists(address):
                return
            if asset in positions:
                old_szi    = float(positions[asset].get("szi", 0))
                new_szi    = float(new_pos.get("szi", 0))
                abs_change = abs(abs(new_szi) - abs(old_szi))
                pct_change = (abs_change / abs(old_szi) * 100) if old_szi != 0 else 0
                if pct_change >= threshold:
                    tg_send(msg_updated(asset, positions[asset], new_pos,
                                        mids, address, pct_change))
                    print(f"  [{get_label(address)}] Updated: {asset} ({pct_change:.1f}%)")
                    changed = True

    # ── Save updated positions ─────────────────
    if wallet_exists(address):
        with state_lock:
            if address in state.get("wallets", {}):
                state["wallets"][address]["positions"] = current
        if changed:
            save_state()


def tracker_loop():
    print("[Tracker] Background loop started.")
    while True:
        addresses = list(get_wallets().keys())
        if addresses:
            # Single allMids call shared across all wallets this cycle
            mids = hl_all_mids()
            for address in addresses:
                try:
                    label = get_label(address)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {label}...", end=" ")
                    check_trader(address, mids)
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
        tg_send(cmd_status(parts))
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
    print(f"  HYPERLIQUID TRACKER v8")
    print(f"  Wallets: {len(get_wallets())}")
    print(f"{'='*50}\n")

    wallets = get_wallets()

    if not wallets:
        tg_send(
            "✅ <b>HL Tracker v8 is LIVE!</b>\n"
            "─────────────────────────────\n"
            "No wallets tracked yet.\n\n"
            "/add <code>0xADDRESS Nickname</code>\n"
            "/help for all commands."
        )
    else:
        wallet_list = "\n".join(
            f"  • <b>{d['label']}</b>  <code>{a}</code>"
            for a, d in wallets.items()
        )
        tg_send(
            f"✅ <b>HL Tracker v8 is LIVE!</b>\n"
            f"─────────────────────────────\n"
            f"Tracking <b>{len(wallets)}</b> wallet(s):\n"
            f"{wallet_list}\n\n"
            f"📡 Polling every {POLL_INTERVAL}s\n"
            f"📐 Default threshold: ≥{DEFAULT_THRESHOLD:.0f}% size change\n"
            f"/help for commands."
        )

# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    startup()
    threading.Thread(target=tracker_loop, daemon=True).start()
    command_loop()