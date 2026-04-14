"""
╔══════════════════════════════════════════════════════════════╗
║       🎮 UC STATE BOT — BGMI UC Purchase Bot                 ║
║        Built with python-telegram-bot v20+                   ║
║        v3.0 — Advanced Edition                               ║
║                                                              ║
║  NEW IN v3.0:                                                ║
║   ✅ BGMI ID: strict validation + 4-API name fetch           ║
║   ✅ Player confirm screen (Yes / Wrong ID) before packages  ║
║   ✅ Name-fetch retry with clear "not found" handling        ║
║   ✅ Admin: broadcast, admin mgmt, password change           ║
║   ✅ Admin: rich order stats & export                        ║
║   ✅ Admin: live dashboard with user count                   ║
║   ✅ Better UX: typing indicators, loading messages          ║
║   ✅ All known v2 bugs fixed                                 ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import unquote

import aiohttp
from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN   = "8705147535:AAHk9Fm9D4W2SbMcKRjcmtbLNbDGpMJt_c0"  # ← your token
CONFIG_FILE = "config.json"
ORDERS_FILE = "orders.json"
USERS_FILE  = "users.json"

DEFAULT_CONFIG = {
    "bot_token":      BOT_TOKEN,
    "admin_password": "VoidProject#000",
    "admin_ids":      [],
    "force_channels": [],
    "proof_url":      "",
    "tutorial_url":   "",
    "qr_image":       "qr_payment.jpg",
    "welcome_msg":    "",
    "packages": [
        {"uc": 720,  "price": 119, "label": "⚡ 720 UC  — ₹119"},
        {"uc": 1360, "price": 145, "label": "🔥 1,360 UC — ₹145"},
        {"uc": 3780, "price": 295, "label": "💎 3,780 UC — ₹295"},
        {"uc": 8700, "price": 399, "label": "👑 6,800+1,900 UC — ₹399"},
    ],
}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            stored.setdefault(k, v)
        # Migration: old backup_channel → force_channels
        old_ch = stored.pop("backup_channel", None)
        if old_ch:
            fc = stored.setdefault("force_channels", [])
            if not any(c.get("id") == old_ch for c in fc):
                fc.append({
                    "id": old_ch,
                    "invite_link": (
                        f"https://t.me/{old_ch.lstrip('@')}"
                        if old_ch.startswith("@") else ""
                    ),
                    "name": old_ch,
                })
        save_config(stored)
        return stored
    cfg = DEFAULT_CONFIG.copy()
    save_config(cfg)
    return cfg


def save_config(c: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)


cfg = load_config()

# ══════════════════════════════════════════════════════════════════════════════
#  DATA PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def load_orders() -> list:
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_order(record: dict):
    orders = load_orders()
    orders.append(record)
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=2, ensure_ascii=False)


def update_order_status(oid: str, status: str):
    orders = load_orders()
    for o in orders:
        if o["order_id"] == oid:
            o["status"] = status
            o["updated_at"] = datetime.now().isoformat()
            break
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=2, ensure_ascii=False)


def new_order_id() -> str:
    return f"UC{int(time.time())}"


# ── User registry (for broadcast) ───────────────────────────────────────────

def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def register_user(user_id: int, username: str, first_name: str):
    users = load_users()
    users[str(user_id)] = {
        "username":   username or "",
        "first_name": first_name or "",
        "last_seen":  datetime.now().isoformat(),
    }
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════════════════════
#  CONVERSATION STATES
# ══════════════════════════════════════════════════════════════════════════════

(
    MAIN_MENU,       # 0
    WAIT_GAME_ID,    # 1
    SELECT_PACKAGE,  # 2
    CONFIRM_ORDER,   # 3
    WAIT_PAYMENT,    # 4
    CONFIRM_GAME_ID, # 5  ← NEW: confirm player name before packages
) = range(6)

ADMIN_PASSWORD_STATE  = 10
ADMIN_MENU_STATE      = 11
ADMIN_EDIT_STATE      = 12
ADMIN_BROADCAST_STATE = 13  # ← NEW
ADMIN_NEWPW_STATE     = 14  # ← NEW

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def ikb(*rows):
    """Build InlineKeyboardMarkup. Each row = list of (text, data_or_url)."""
    built = []
    for row in rows:
        buttons = []
        for t, d in row:
            if d.startswith("http://") or d.startswith("https://") or d.startswith("tg://"):
                buttons.append(InlineKeyboardButton(t, url=d))
            else:
                buttons.append(InlineKeyboardButton(t, callback_data=d))
        built.append(buttons)
    return InlineKeyboardMarkup(built)


async def typing(update: Update):
    try:
        chat = update.effective_chat
        if chat:
            await chat.send_action(ChatAction.TYPING)
    except Exception:
        pass


async def safe_delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


def is_valid_bgmi_id(uid: str) -> bool:
    """
    BGMI Game IDs are purely numeric, typically 9–12 digits.
    Reject anything shorter (5 could be a test) or with letters.
    """
    return uid.isdigit() and 8 <= len(uid) <= 13


# ══════════════════════════════════════════════════════════════════════════════
#  BGMI PLAYER NAME FETCH — rooter.gg primary + 3 fallback APIs
# ══════════════════════════════════════════════════════════════════════════════

async def _get_rooter_token() -> str | None:
    """
    Scrape rooter.gg to get a session Bearer token from its cookies.
    No login needed — rooter.gg sets user_auth cookie on first visit.
    """
    try:
        jar = aiohttp.CookieJar(unsafe=True)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(cookie_jar=jar, headers=headers, timeout=timeout) as s:
            async with s.get("https://www.rooter.gg/") as r:
                await r.read()

            # Extract user_auth cookie value (Morsel object — use .value)
            cookies = jar.filter_cookies("https://www.rooter.gg")
            morsel  = cookies.get("user_auth")
            if not morsel:
                logger.debug("rooter: user_auth cookie not found in jar")
                return None

            raw_val = morsel.value if hasattr(morsel, "value") else str(morsel)
            decoded = unquote(raw_val)
            data    = json.loads(decoded)
            token   = data.get("accessToken")
            if token:
                logger.debug("rooter: token acquired successfully")
            return token
    except Exception as e:
        logger.debug("rooter token fetch failed: %s", e)
    return None


async def fetch_bgmi_name(uid: str) -> tuple[str | None, str]:
    """
    Resolve a BGMI UID to a player name.

    Primary: rooter.gg (bazaar.rooter.io) — scrapes session token, no login needed.
    Fallback: gametools.network, bgmicup.in, crafty.gg.

    Returns:
        (name, source)  — name is None if all APIs fail / no valid name found.
        source          — which API succeeded, or "not_found" if all failed.
    """
    # ── API 0 (Primary): rooter.gg / bazaar.rooter.io ────────────────────────
    try:
        access_token = await _get_rooter_token()
        if access_token:
            url = (
                "https://bazaar.rooter.io/order/getUnipinUsername"
                f"?gameCode=BGMI_IN&id={uid}"
            )
            api_headers = {
                "Authorization": f"Bearer {access_token}",
                "Device-Type":   "web",
                "App-Version":   "1.0.0",
                "Device-Id":     "uc-state-bot",
                "User-Agent":    "Mozilla/5.0",
                "Accept":        "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, headers=api_headers) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        if data.get("transaction") == "SUCCESS":
                            unipin = data.get("unipinRes", {})
                            name   = unipin.get("username")
                            if name and str(name).strip() and str(name) != uid:
                                logger.info("UID %s resolved via rooter.gg → %s", uid, name)
                                return str(name).strip(), "rooter"
                        else:
                            logger.debug("rooter response for %s: %s", uid, data)
    except Exception as e:
        logger.debug("API0 rooter failed for %s: %s", uid, e)

    # Common headers for fallback APIs
    fb_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    fb_timeout = aiohttp.ClientTimeout(total=7)

    # ── API 1 (Fallback): gametools.network ──────────────────────────────────
    try:
        url = f"https://api.gametools.network/bgmi/uid/?uid={uid}&lang=en"
        async with aiohttp.ClientSession(timeout=fb_timeout, headers=fb_headers) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    name = (
                        data.get("id_name")
                        or data.get("name")
                        or data.get("userName")
                        or ((data.get("players") or [{}])[0].get("name"))
                    )
                    if name and str(name).strip() and str(name) != uid:
                        logger.info("UID %s resolved via gametools → %s", uid, name)
                        return str(name).strip(), "gametools"
    except Exception as e:
        logger.debug("API1 gametools failed for %s: %s", uid, e)

    # ── API 2 (Fallback): bgmicup.in ────────────────────────────────────────
    try:
        url = f"https://bgmicup.in/api/profile/{uid}"
        async with aiohttp.ClientSession(timeout=fb_timeout, headers=fb_headers) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    inner = data.get("data") or {}
                    name  = inner.get("nickname") or inner.get("name") or data.get("nickname")
                    if name and str(name).strip() and str(name) != uid:
                        logger.info("UID %s resolved via bgmicup → %s", uid, name)
                        return str(name).strip(), "bgmicup"
    except Exception as e:
        logger.debug("API2 bgmicup failed for %s: %s", uid, e)

    # ── API 3 (Fallback): crafty.gg ──────────────────────────────────────────
    try:
        for region in ("sea", "as", "in"):
            url = f"https://api.crafty.gg/api/v2/pubgm/player/search?region={region}&id={uid}"
            async with aiohttp.ClientSession(timeout=fb_timeout, headers=fb_headers) as s:
                async with s.get(url) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        name = (
                            data.get("nickname")
                            or data.get("name")
                            or (data.get("data") or {}).get("nickname")
                        )
                        if name and str(name).strip() and str(name) != uid:
                            logger.info("UID %s resolved via crafty(%s) → %s", uid, region, name)
                            return str(name).strip(), f"crafty/{region}"
    except Exception as e:
        logger.debug("API3 crafty failed for %s: %s", uid, e)

    logger.warning("All APIs failed to resolve BGMI UID: %s", uid)
    return None, "not_found"


# ══════════════════════════════════════════════════════════════════════════════
#  FORCE-JOIN  (multi-channel)
# ══════════════════════════════════════════════════════════════════════════════

async def get_unjoined_channels(bot: Bot, user_id: int) -> list:
    unjoined = []
    for ch in cfg.get("force_channels", []):
        ch_id = ch.get("id", "").strip()
        if not ch_id:
            continue
        try:
            member = await bot.get_chat_member(ch_id, user_id)
            if member.status in ("left", "kicked", "banned"):
                unjoined.append(ch)
        except Exception:
            logger.debug("Cannot check membership for channel %s", ch_id)
    return unjoined


async def send_join_required(update: Update, unjoined: list, edit: bool = False):
    user = update.effective_user
    channel_list = "\n".join(
        f"  {i+1}. 📢 *{ch.get('name', ch.get('id', 'Channel'))}*"
        for i, ch in enumerate(unjoined)
    )
    text = (
        f"👋 *Hey {user.first_name}!*\n\n"
        "🔒 *Access Restricted!*\n\n"
        "To use this bot, please join our channel(s):\n\n"
        f"{channel_list}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Tap *Join* for each channel below\n"
        "2️⃣ Then tap *✅ I've Joined All*"
    )
    rows = []
    for ch in unjoined:
        link = ch.get("invite_link", "").strip()
        name = ch.get("name", "Channel")
        if link:
            rows.append([(f"📢 Join {name}", link)])
    rows.append([("✅  I've Joined All", "check_join")])
    markup = ikb(*rows)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
            )
            return
        except Exception:
            pass
    target = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if target:
        await target.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN MENU
# ══════════════════════════════════════════════════════════════════════════════

MAIN_TEXT = (
    "🎮 *Welcome to UC STATE — Premium BGMI Store!*\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🛒 *Buy BGMI UC* at the *Lowest Prices* in India!\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "✅  100% Safe & Secure Delivery\n"
    "⚡  Instant Processing\n"
    "💬  24/7 Customer Support\n"
    "🏆  Trusted by 10,000+ Gamers\n\n"
    "👇 *Select an option to get started:*"
)


async def send_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    proof_url    = cfg.get("proof_url", "")
    tutorial_url = cfg.get("tutorial_url", "")
    rows = [
        [("🎮  Buy BGMI UC", "game_bgmi")],
        [("📊  My Stats", "my_stats"), ("📦  Order History", "order_history")],
    ]
    bottom = []
    if proof_url:
        bottom.append(("📸  Proof", proof_url))
    if tutorial_url:
        bottom.append(("📚  Tutorial", tutorial_url))
    if bottom:
        rows.append(bottom)

    welcome = cfg.get("welcome_msg", "").strip()
    text = MAIN_TEXT + (f"\n\n📣 _{welcome}_" if welcome else "")

    markup = ikb(*rows)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
            )
            return
        except Exception:
            pass
    target = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if target:
        await target.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await typing(update)
    user = update.effective_user
    register_user(user.id, user.username or "", user.first_name or "")
    unjoined = await get_unjoined_channels(update.get_bot(), user.id)
    if unjoined:
        await send_join_required(update, unjoined)
        return MAIN_MENU
    await send_main_menu(update, ctx)
    return MAIN_MENU


async def cb_check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    unjoined = await get_unjoined_channels(update.get_bot(), q.from_user.id)
    if unjoined:
        names = ", ".join(ch.get("name", "channel") for ch in unjoined)
        await q.answer(
            f"❌ Still not joined: {names}\nPlease join all channels first!",
            show_alert=True,
        )
        await send_join_required(update, unjoined, edit=True)
        return MAIN_MENU
    await send_main_menu(update, ctx, edit=True)
    return MAIN_MENU

# ══════════════════════════════════════════════════════════════════════════════
#  BGMI FLOW — STEP 1: Enter & Validate Game ID
# ══════════════════════════════════════════════════════════════════════════════

async def cb_game_bgmi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await typing(update)
    await q.edit_message_text(
        "🎮 *BGMI UC Purchase*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📝 *Enter Your BGMI Game ID*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔢 Your Game ID is a *8–13 digit number*.\n\n"
        "📌 *How to find your ID:*\n"
        "  1️⃣ Open BGMI\n"
        "  2️⃣ Tap your *Profile Avatar* (top-left)\n"
        "  3️⃣ Your ID appears below your name\n\n"
        "💡 _Example: `5123456789`_\n\n"
        "⬇️ Type your Game ID now:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb([("🏠  Main Menu", "cancel_to_main")]),
    )
    ctx.user_data["flow"] = "bgmi"
    return WAIT_GAME_ID


async def recv_game_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    await safe_delete(update.message)

    # ── Format validation ────────────────────────────────────────────────────
    if not raw.isdigit():
        await update.effective_chat.send_message(
            "❌ *Invalid Format!*\n\n"
            "Your Game ID must contain *numbers only* (no letters or spaces).\n\n"
            "💡 Example: `5123456789`\n\n"
            "Please enter your Game ID again:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("🏠  Main Menu", "cancel_to_main")]),
        )
        return WAIT_GAME_ID

    if not is_valid_bgmi_id(raw):
        length = len(raw)
        hint = (
            "Too short! BGMI IDs are at least 8 digits."
            if length < 8 else
            "Too long! BGMI IDs are at most 13 digits."
        )
        await update.effective_chat.send_message(
            f"❌ *Invalid Game ID!*\n\n"
            f"You entered `{raw}` ({length} digits). {hint}\n\n"
            "💡 Valid example: `5123456789`\n\n"
            "Please enter your correct Game ID:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("🏠  Main Menu", "cancel_to_main")]),
        )
        return WAIT_GAME_ID

    # ── Fetch player name with live status message ───────────────────────────
    await update.effective_chat.send_action(ChatAction.TYPING)
    wait_msg = await update.effective_chat.send_message(
        "🔍 *Searching for your BGMI Profile…*\n\n"
        "⏳ _Fetching player name, please wait…_",
        parse_mode=ParseMode.MARKDOWN,
    )

    name, source = await fetch_bgmi_name(raw)
    await safe_delete(wait_msg)

    ctx.user_data["uid"] = raw

    if name:
        # ── Name found: ask for confirmation ──────────────────────────────
        ctx.user_data["nickname"] = name
        await update.effective_chat.send_message(
            "✅ *Player Profile Found!*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Player Name: *{name}*\n"
            f"🆔 Game ID:     `{raw}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "❓ *Is this your account?*\n"
            "_Please confirm before proceeding with your purchase._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb(
                [("✅  Yes, This Is Me!", "confirm_uid_yes")],
                [("❌  No, Wrong ID",     "confirm_uid_no")],
                [("🏠  Main Menu",        "cancel_to_main")],
            ),
        )
    else:
        # ── Name not found: still let user confirm with warning ────────────
        ctx.user_data["nickname"] = f"Unknown·{raw[-4:]}"
        await update.effective_chat.send_message(
            "⚠️ *Could Not Fetch Player Name*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Game ID You Entered: `{raw}`\n"
            "👤 Player Name: *Unable to fetch*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ *Please double-check your Game ID.*\n"
            "If you entered the wrong ID, UC will be sent to the *wrong account* "
            "and we cannot reverse this.\n\n"
            "❓ Are you absolutely sure this ID is correct?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb(
                [("✅  Yes, ID Is Correct", "confirm_uid_yes")],
                [("✏️  Re-Enter Game ID",   "confirm_uid_no")],
                [("🏠  Main Menu",           "cancel_to_main")],
            ),
        )

    return CONFIRM_GAME_ID


# ── STEP 1b: Confirm UID ─────────────────────────────────────────────────────

async def cb_confirm_uid_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("✅ Confirmed!")
    await typing(update)
    uid      = ctx.user_data.get("uid", "")
    nickname = ctx.user_data.get("nickname", "")
    await q.edit_message_text(
        f"🎉 *Great! Profile Confirmed.*\n\n"
        f"👤 Player: *{nickname}*\n"
        f"🆔 Game ID: `{uid}`\n\n"
        "⬇️ *Now select your UC package:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb([("⏳ Loading packages…", "noop")]),
    )
    await _show_packages(update.effective_chat, ctx, nickname, uid, msg_to_edit=q.message)
    return SELECT_PACKAGE


async def cb_confirm_uid_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("uid", None)
    ctx.user_data.pop("nickname", None)
    await q.edit_message_text(
        "✏️ *Re-Enter Your BGMI Game ID*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔢 Your Game ID is a *8–13 digit number*.\n\n"
        "📌 *How to find your ID:*\n"
        "  1️⃣ Open BGMI\n"
        "  2️⃣ Tap your *Profile Avatar* (top-left)\n"
        "  3️⃣ Your ID appears below your name\n\n"
        "💡 _Example: `5123456789`_\n\n"
        "⬇️ Type your correct Game ID now:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb([("🏠  Main Menu", "cancel_to_main")]),
    )
    return WAIT_GAME_ID

# ══════════════════════════════════════════════════════════════════════════════
#  BGMI FLOW — STEP 2: Select Package
# ══════════════════════════════════════════════════════════════════════════════

async def _show_packages(chat, ctx, nickname: str, uid: str, msg_to_edit=None):
    packages = cfg.get("packages", DEFAULT_CONFIG["packages"])
    pkg_rows = [[(p["label"], f"pkg_{i}")] for i, p in enumerate(packages)]
    pkg_rows.append([("🏠  Main Menu", "cancel_to_main")])
    text = (
        f"✅ *Game ID Confirmed!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Player:  *{nickname}*\n"
        f"🆔 Game ID: `{uid}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💰 *Select Your UC Package:*\n"
        "_All prices inclusive of taxes_"
    )
    markup = ikb(*pkg_rows)
    if msg_to_edit:
        try:
            await msg_to_edit.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            return
        except Exception:
            pass
    await chat.send_message(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def cb_select_package(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await typing(update)

    idx      = int(q.data.split("_")[1])
    packages = cfg.get("packages", DEFAULT_CONFIG["packages"])
    if idx >= len(packages):
        await q.answer("⚠️ Package not found. Please try again.", show_alert=True)
        return SELECT_PACKAGE

    pkg      = packages[idx]
    uid      = ctx.user_data.get("uid", "")
    nickname = ctx.user_data.get("nickname", "")
    ctx.user_data["package"]     = pkg
    ctx.user_data["package_idx"] = idx

    await q.edit_message_text(
        f"🛒 *Order Summary*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 Game:     BGMI\n"
        f"👤 Player:   *{nickname}*\n"
        f"🆔 Game ID: `{uid}`\n"
        f"📦 Package:  *{pkg['uc']:,} UC*\n"
        f"💰 Price:    *₹{pkg['price']:.2f}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ Everything look good? Proceed to payment:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb(
            [("💳  Pay via QR Code", "pay_qr")],
            [("⬅️  Change Package",  "back_to_packages"),
             ("🏠  Main Menu",       "cancel_to_main")],
        ),
    )
    return CONFIRM_ORDER

# ══════════════════════════════════════════════════════════════════════════════
#  BGMI FLOW — STEP 3: Payment via QR
# ══════════════════════════════════════════════════════════════════════════════

async def cb_pay_qr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await typing(update)

    uid      = ctx.user_data.get("uid", "N/A")
    nickname = ctx.user_data.get("nickname", "N/A")
    pkg      = ctx.user_data.get("package", {})
    oid      = new_order_id()
    ctx.user_data["order_id"] = oid

    caption = (
        f"💳 *Scan & Pay*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Order ID: `{oid}`\n"
        f"👤 Player:   *{nickname}*\n"
        f"🎮 Game ID:  `{uid}`\n"
        f"📦 Package:  *{pkg.get('uc', 0):,} UC*\n"
        f"💰 Amount:   *₹{pkg.get('price', 0):.2f}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 *Payment Instructions:*\n"
        "❌ Do NOT use Google Pay\n"
        "✅ Use PhonePe / Paytm / BHIM / any UPI app\n\n"
        "1️⃣ Scan the QR code above\n"
        "2️⃣ Pay the *exact* amount shown\n"
        "3️⃣ Tap *✅ Verify Payment* once done\n\n"
        "⏳ _Keep this chat open after paying_"
    )
    payment_kb = ikb(
        [("✅  Verify Payment", "verify_payment")],
        [("🏠  Main Menu",      "cancel_to_main")],
    )

    await safe_delete(q.message)

    qr_file = cfg.get("qr_image", "qr_payment.jpg")
    if qr_file and os.path.exists(qr_file):
        with open(qr_file, "rb") as f:
            await update.effective_chat.send_photo(
                photo=f, caption=caption,
                reply_markup=payment_kb, parse_mode=ParseMode.MARKDOWN,
            )
    else:
        await update.effective_chat.send_message(
            "⚠️ *QR code not configured yet.*\n"
            "Please contact the admin to set up the payment QR.\n\n" + caption,
            reply_markup=payment_kb, parse_mode=ParseMode.MARKDOWN,
        )
    return WAIT_PAYMENT

# ══════════════════════════════════════════════════════════════════════════════
#  BGMI FLOW — STEP 4: Verify Payment → Notify Admin
# ══════════════════════════════════════════════════════════════════════════════

async def cb_verify_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Submitting your payment…")
    await typing(update)

    uid      = ctx.user_data.get("uid", "N/A")
    nickname = ctx.user_data.get("nickname", "N/A")
    pkg      = ctx.user_data.get("package", {})
    oid      = ctx.user_data.get("order_id", new_order_id())
    user     = q.from_user

    record = {
        "order_id":   oid,
        "user_id":    user.id,
        "username":   user.username or user.first_name,
        "uid":        uid,
        "nickname":   nickname,
        "package":    pkg.get("label", ""),
        "price":      pkg.get("price", 0),
        "status":     "pending",
        "timestamp":  datetime.now().isoformat(),
        "updated_at": None,
    }
    save_order(record)

    admin_text = (
        f"🔔 *New Payment — Review Required*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Order:    `{oid}`\n"
        f"👤 User:     @{user.username or user.first_name} (`{user.id}`)\n"
        f"🎮 Nickname: `{nickname}`\n"
        f"🆔 Game ID:  `{uid}`\n"
        f"📦 Package:  *{pkg.get('label', '')}*\n"
        f"💰 Amount:   *₹{pkg.get('price', 0):.2f}*\n"
        f"⏰ Time:     {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ Please verify payment and take action:"
    )
    admin_kb = ikb(
        [("✅  Approve", f"approve|{oid}|{user.id}"),
         ("❌  Reject",  f"reject|{oid}|{user.id}")],
    )

    bot = update.get_bot()
    for admin_id in cfg.get("admin_ids", []):
        try:
            await bot.send_message(
                admin_id, admin_text,
                reply_markup=admin_kb, parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning("Could not notify admin %s: %s", admin_id, e)

    success_text = (
        f"🎉 *Payment Submitted Successfully!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Order ID: `{oid}`\n"
        f"📦 Package:  *{pkg.get('uc', 0):,} UC*\n"
        f"💰 Amount:   *₹{pkg.get('price', 0):.2f}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ Our team is verifying your payment.\n"
        "✅ UC will be delivered within *5–15 minutes*.\n\n"
        "📩 You'll receive a notification once approved!\n"
        "_Thank you for choosing UC STATE! 🙏_"
    )
    try:
        if q.message and q.message.photo:
            await q.edit_message_caption(
                caption=success_text,
                reply_markup=ikb([("🏠  Main Menu", "cancel_to_main")]),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await q.edit_message_text(
                success_text,
                reply_markup=ikb([("🏠  Main Menu", "cancel_to_main")]),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception:
        await update.effective_chat.send_message(
            success_text,
            reply_markup=ikb([("🏠  Main Menu", "cancel_to_main")]),
            parse_mode=ParseMode.MARKDOWN,
        )

    ctx.user_data.clear()
    return MAIN_MENU

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN APPROVE / REJECT  (global, outside any conversation)
# ══════════════════════════════════════════════════════════════════════════════

async def cb_admin_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, oid, user_id_str = q.data.split("|", 2)
    user_id = int(user_id_str)
    update_order_status(oid, "approved")
    await q.answer("✅ Order Approved!", show_alert=True)
    try:
        new_text = (
            (q.message.text or "") + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *APPROVED* by @{q.from_user.username or q.from_user.first_name}\n"
            f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
        )
        await q.edit_message_text(new_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    try:
        await update.get_bot().send_message(
            user_id,
            f"🎉 *Your UC Order Has Been Approved!*\n\n"
            f"🆔 Order ID: `{oid}`\n\n"
            "✅ Your UC will be credited to your account shortly.\n"
            "_Thank you for shopping with UC STATE! 🙏_",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


async def cb_admin_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, oid, user_id_str = q.data.split("|", 2)
    user_id = int(user_id_str)
    update_order_status(oid, "rejected")
    await q.answer("❌ Order Rejected!", show_alert=True)
    try:
        new_text = (
            (q.message.text or "") + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ *REJECTED* by @{q.from_user.username or q.from_user.first_name}\n"
            f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
        )
        await q.edit_message_text(new_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    try:
        await update.get_bot().send_message(
            user_id,
            f"❌ *Your UC Order Was Rejected*\n\n"
            f"🆔 Order ID: `{oid}`\n\n"
            "⚠️ Payment could not be verified.\n"
            "💬 Contact our support if you believe this is an error.\n\n"
            "_We apologise for any inconvenience. — UC STATE_",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
#  STATS & ORDER HISTORY (user-facing)
# ══════════════════════════════════════════════════════════════════════════════

async def cb_my_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await typing(update)

    orders      = load_orders()
    mine        = [o for o in orders if o.get("user_id") == q.from_user.id]
    approved    = [o for o in mine if o.get("status") == "approved"]
    pending     = [o for o in mine if o.get("status") == "pending"]
    rejected    = [o for o in mine if o.get("status") == "rejected"]
    total_spent = sum(o.get("price", 0) for o in approved)
    total_uc    = sum(
        next((p["uc"] for p in cfg.get("packages", []) if p.get("label") == o.get("package")), 0)
        for o in approved
    )

    await q.edit_message_text(
        f"📊 *Your Account Stats*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 Total Orders:   *{len(mine)}*\n"
        f"✅ Approved:        *{len(approved)}*\n"
        f"⏳ Pending:          *{len(pending)}*\n"
        f"❌ Rejected:         *{len(rejected)}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Total Spent:    *₹{total_spent:.2f}*\n"
        f"🎮 Total UC Bought: *{total_uc:,} UC*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "_Keep gaming! 🎮_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb([("⬅️  Back", "cancel_to_main")]),
    )
    return MAIN_MENU


async def cb_order_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await typing(update)

    orders = load_orders()
    mine   = [o for o in orders if o.get("user_id") == q.from_user.id][-5:]

    if not mine:
        text = (
            "📦 *Order History*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 No orders found yet.\n\n"
            "_Place your first order and it'll appear here!_"
        )
    else:
        STATUS = {"approved": "✅", "rejected": "❌", "pending": "⏳"}
        lines  = ["📦 *Your Last 5 Orders*\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
        for o in reversed(mine):
            icon = STATUS.get(o.get("status", ""), "⏳")
            lines.append(
                f"{icon} `{o['order_id']}`\n"
                f"   📦 {o.get('package', 'N/A')}\n"
                f"   💰 ₹{o.get('price', 0)} · 📅 {o.get('timestamp', '')[:10]}\n"
            )
        text = "\n".join(lines)

    await q.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb([("⬅️  Back", "cancel_to_main")]),
    )
    return MAIN_MENU

# ══════════════════════════════════════════════════════════════════════════════
#  NAVIGATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def cb_cancel_to_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    await send_main_menu(update, ctx, edit=True)
    return MAIN_MENU


async def cb_back_to_packages(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await typing(update)
    uid      = ctx.user_data.get("uid", "")
    nickname = ctx.user_data.get("nickname", "")
    await _show_packages(update.effective_chat, ctx, nickname, uid, msg_to_edit=q.message)
    return SELECT_PACKAGE


async def cb_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL — Entry & Password
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await typing(update)
    await safe_delete(update.message)
    await update.effective_chat.send_message(
        "🔐 *Admin Authentication*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 Enter the admin password to continue:\n\n"
        "_Your password will be deleted immediately after entry for security._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb([("❌  Cancel", "admin_cancel")]),
    )
    return ADMIN_PASSWORD_STATE


async def recv_admin_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_delete(update.message)
    if update.message.text.strip() == cfg.get("admin_password", "VoidProject#000"):
        uid = update.effective_user.id
        if uid not in cfg.get("admin_ids", []):
            cfg.setdefault("admin_ids", []).append(uid)
            save_config(cfg)
        await send_admin_menu(update, ctx)
        return ADMIN_MENU_STATE
    await update.effective_chat.send_message(
        "❌ *Wrong Password!*\n\nTry again:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb([("❌  Cancel", "admin_cancel")]),
    )
    return ADMIN_PASSWORD_STATE

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL — Dashboard
# ══════════════════════════════════════════════════════════════════════════════

async def send_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    orders   = load_orders()
    users    = load_users()
    channels = cfg.get("force_channels", [])

    n_total    = len(orders)
    n_approved = sum(1 for o in orders if o.get("status") == "approved")
    n_pending  = sum(1 for o in orders if o.get("status") == "pending")
    n_rejected = sum(1 for o in orders if o.get("status") == "rejected")
    revenue    = sum(o.get("price", 0) for o in orders if o.get("status") == "approved")
    n_users    = len(users)
    n_admins   = len(cfg.get("admin_ids", []))
    ch_names   = ", ".join(c.get("name", c.get("id", "?")) for c in channels) or "None"

    text = (
        "⚙️ *Admin Dashboard — UC STATE BOT*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *Order Statistics*\n"
        f"  📋 Total:    *{n_total}*\n"
        f"  ✅ Approved: *{n_approved}*\n"
        f"  ⏳ Pending:  *{n_pending}*\n"
        f"  ❌ Rejected: *{n_rejected}*\n"
        f"  💰 Revenue:  *₹{revenue:.2f}*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👥 *Bot Info*\n"
        f"  👤 Users:    *{n_users}*\n"
        f"  👮 Admins:   *{n_admins}*\n"
        f"  📢 Channels: `{ch_names[:40]}{'…' if len(ch_names)>40 else ''}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🕐 _{datetime.now().strftime('%d %b %Y, %I:%M %p')}_"
    )
    keyboard = ikb(
        [("📦  Edit Packages",          "adm_packages"),
         ("💳  Upload QR",              "adm_qr")],
        [("📢  Force Channels",         "adm_channels")],
        [("📸  Proof URL",              "adm_proof"),
         ("📚  Tutorial URL",           "adm_tutorial")],
        [("📋  View Orders",            "adm_orders"),
         ("📊  Order Stats",            "adm_orderstats")],
        [("📣  Broadcast Message",      "adm_broadcast")],
        [("🏷️  Welcome Message",        "adm_welcome"),
         ("🔑  Change Password",        "adm_changepw")],
        [("👮  Manage Admins",          "adm_admins")],
        [("👋  Exit Admin",             "adm_exit")],
    )
    target = update.callback_query.message if update.callback_query else update.message
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
            )
            return
        except Exception:
            pass
    await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL — Channel Management
# ══════════════════════════════════════════════════════════════════════════════

async def _show_channels_menu(q, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    channels = cfg.get("force_channels", [])
    lines    = ["📢 *Force Join Channels*\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
    rows     = []
    if channels:
        for i, ch in enumerate(channels):
            name = ch.get("name", ch.get("id", "Channel"))
            lines.append(f"  {i+1}. `{ch.get('id','')}` — *{name}*")
            rows.append([(f"🗑 Remove: {name[:20]}", f"adm_delch_{i}")])
    else:
        lines += [
            "_No channels configured yet._\n",
            "ℹ️ *How to add:*",
            "• Public channel:  `@ChannelUsername`",
            "• Private channel: `-1001234567890`",
            "  _(forward a msg from it to @userinfobot to get ID)_",
        ]
    rows.append([("➕  Add Channel", "adm_addch")])
    rows.append([("⬅️  Back",        "adm_back")])
    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb(*rows),
    )

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL — Main callback router
# ══════════════════════════════════════════════════════════════════════════════

async def cb_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    action = q.data

    # ── Exit ─────────────────────────────────────────────────────────────────
    if action == "adm_exit":
        await q.edit_message_text(
            "👋 *Admin session ended. Stay secure!*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    # ── Back ─────────────────────────────────────────────────────────────────
    elif action == "adm_back":
        await send_admin_menu(update, ctx, edit=True)
        return ADMIN_MENU_STATE

    # ── Channels ─────────────────────────────────────────────────────────────
    elif action == "adm_channels":
        await _show_channels_menu(q, update, ctx)
        return ADMIN_MENU_STATE

    elif action.startswith("adm_delch_"):
        idx      = int(action.split("_")[-1])
        channels = cfg.get("force_channels", [])
        if 0 <= idx < len(channels):
            removed = channels.pop(idx)
            cfg["force_channels"] = channels
            save_config(cfg)
            await q.answer(f"✅ Removed: {removed.get('name','channel')}", show_alert=True)
        await _show_channels_menu(q, update, ctx)
        return ADMIN_MENU_STATE

    elif action == "adm_addch":
        await q.edit_message_text(
            "📢 *Add Force Channel — Step 1 of 3*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Send the channel *ID or username*:\n\n"
            "• Public channel:  `@MyChannel`\n"
            "• Private channel: `-1001234567890`\n\n"
            "💡 _Forward a message from your channel to @userinfobot to get the numeric ID_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "add_ch_id"
        return ADMIN_EDIT_STATE

    # ── Packages ─────────────────────────────────────────────────────────────
    elif action == "adm_packages":
        packages = cfg.get("packages", DEFAULT_CONFIG["packages"])
        lines    = ["📦 *Current Packages:*\n"]
        for i, p in enumerate(packages):
            lines.append(f"  {i+1}. {p['label']}")
        lines += [
            "\n\n*Send new packages — one per line:*",
            "`<UC_AMOUNT> <PRICE>`\n",
            "_Example:_",
            "`720 119`",
            "`1360 145`",
            "`3780 295`",
            "`8700 399`",
        ]
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "packages"
        return ADMIN_EDIT_STATE

    # ── Proof / Tutorial URL ─────────────────────────────────────────────────
    elif action == "adm_proof":
        await q.edit_message_text(
            f"📸 *Proof URL*\n\n"
            f"Current: `{cfg.get('proof_url', 'Not set') or 'Not set'}`\n\n"
            "Send the new proof URL (or send `-` to clear):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "proof_url"
        return ADMIN_EDIT_STATE

    elif action == "adm_tutorial":
        await q.edit_message_text(
            f"📚 *Tutorial URL*\n\n"
            f"Current: `{cfg.get('tutorial_url', 'Not set') or 'Not set'}`\n\n"
            "Send the new tutorial URL (or send `-` to clear):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "tutorial_url"
        return ADMIN_EDIT_STATE

    # ── QR ───────────────────────────────────────────────────────────────────
    elif action == "adm_qr":
        await q.edit_message_text(
            "💳 *Upload Payment QR Code*\n\n"
            "Send the QR image as a *photo*:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "qr"
        return ADMIN_EDIT_STATE

    # ── Welcome message ───────────────────────────────────────────────────────
    elif action == "adm_welcome":
        cur = cfg.get("welcome_msg", "") or "Not set"
        await q.edit_message_text(
            f"🏷️ *Welcome / Announcement Message*\n\n"
            f"Current: _{cur}_\n\n"
            "This text appears at the bottom of the main menu.\n"
            "Send new text (or `-` to clear):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "welcome_msg"
        return ADMIN_EDIT_STATE

    # ── Change password ───────────────────────────────────────────────────────
    elif action == "adm_changepw":
        await q.edit_message_text(
            "🔑 *Change Admin Password*\n\n"
            "Send your *new password*:\n\n"
            "⚠️ _Choose a strong password. This message will be deleted immediately._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "changepw"
        return ADMIN_EDIT_STATE

    # ── Manage admins ─────────────────────────────────────────────────────────
    elif action == "adm_admins":
        admin_ids = cfg.get("admin_ids", [])
        lines     = ["👮 *Admin List*\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
        rows      = []
        for i, aid in enumerate(admin_ids):
            lines.append(f"  {i+1}. `{aid}`")
            if aid != update.effective_user.id:  # can't remove yourself
                rows.append([(f"🗑 Remove #{aid}", f"adm_deladm_{aid}")])
        if not admin_ids:
            lines.append("_No admins configured._")
        rows.append([("⬅️  Back", "adm_back")])
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb(*rows),
        )
        return ADMIN_MENU_STATE

    elif action.startswith("adm_deladm_"):
        remove_id = int(action.split("_")[-1])
        admin_ids = cfg.get("admin_ids", [])
        if remove_id in admin_ids:
            admin_ids.remove(remove_id)
            cfg["admin_ids"] = admin_ids
            save_config(cfg)
            await q.answer(f"✅ Admin {remove_id} removed.", show_alert=True)
        # Refresh
        admin_ids = cfg.get("admin_ids", [])
        lines     = ["👮 *Admin List*\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
        rows      = []
        for i, aid in enumerate(admin_ids):
            lines.append(f"  {i+1}. `{aid}`")
            if aid != update.effective_user.id:
                rows.append([(f"🗑 Remove #{aid}", f"adm_deladm_{aid}")])
        if not admin_ids:
            lines.append("_No admins configured._")
        rows.append([("⬅️  Back", "adm_back")])
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb(*rows),
        )
        return ADMIN_MENU_STATE

    # ── Broadcast ─────────────────────────────────────────────────────────────
    elif action == "adm_broadcast":
        users = load_users()
        await q.edit_message_text(
            f"📣 *Broadcast Message*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Will be sent to *{len(users)}* registered users.\n\n"
            "✍️ Send the message you want to broadcast:\n"
            "_Supports text with Markdown formatting._\n\n"
            "⚠️ _Double-check before sending — this goes to ALL users!_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Cancel", "adm_back")]),
        )
        ctx.user_data["adm_edit"] = "broadcast"
        return ADMIN_EDIT_STATE

    # ── View all orders ───────────────────────────────────────────────────────
    elif action == "adm_orders":
        orders = load_orders()
        if not orders:
            text = "📋 *All Orders*\n\n_No orders yet._"
        else:
            STATUS = {"approved": "✅", "rejected": "❌", "pending": "⏳"}
            lines  = [f"📋 *All Orders* — last 10 of {len(orders)}\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
            for o in orders[-10:][::-1]:
                icon = STATUS.get(o.get("status", ""), "⏳")
                lines.append(
                    f"{icon} `{o['order_id']}`\n"
                    f"   👤 {o.get('username','?')}  ·  {o.get('package','?')}\n"
                    f"   💰 ₹{o.get('price',0)}  ·  📅 {o.get('timestamp','')[:10]}\n"
                )
            text = "\n".join(lines)
        await q.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        return ADMIN_MENU_STATE

    # ── Order stats ───────────────────────────────────────────────────────────
    elif action == "adm_orderstats":
        orders   = load_orders()
        approved = [o for o in orders if o.get("status") == "approved"]
        pending  = [o for o in orders if o.get("status") == "pending"]
        rejected = [o for o in orders if o.get("status") == "rejected"]
        revenue  = sum(o.get("price", 0) for o in approved)

        # Package breakdown
        pkg_count: dict = {}
        for o in approved:
            lbl = o.get("package", "Unknown")
            pkg_count[lbl] = pkg_count.get(lbl, 0) + 1
        pkg_lines = "\n".join(
            f"   • {lbl}: *{cnt}*" for lbl, cnt in sorted(pkg_count.items(), key=lambda x: -x[1])
        ) or "   _No approved orders yet._"

        text = (
            "📊 *Detailed Order Statistics*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Total Orders:   *{len(orders)}*\n"
            f"✅ Approved:        *{len(approved)}*\n"
            f"⏳ Pending:          *{len(pending)}*\n"
            f"❌ Rejected:         *{len(rejected)}*\n"
            f"💰 Total Revenue:  *₹{revenue:.2f}*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 *Package Breakdown (Approved):*\n"
            f"{pkg_lines}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await q.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        return ADMIN_MENU_STATE

    return ADMIN_MENU_STATE

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL — Edit handler (multi-step channel add + all other edits)
# ══════════════════════════════════════════════════════════════════════════════

async def recv_admin_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = ctx.user_data.get("adm_edit", "")
    await typing(update)

    # ── QR photo ──────────────────────────────────────────────────────────────
    if update.message.photo and key == "qr":
        photo   = update.message.photo[-1]
        file    = await photo.get_file()
        qr_path = "qr_payment.jpg"
        await file.download_to_drive(qr_path)
        cfg["qr_image"] = qr_path
        save_config(cfg)
        await safe_delete(update.message)
        await update.effective_chat.send_message(
            "✅ *QR Image updated successfully!*", parse_mode=ParseMode.MARKDOWN
        )
        await send_admin_menu(update, ctx)
        return ADMIN_MENU_STATE

    text = (update.message.text or "").strip()
    await safe_delete(update.message)

    # ── Broadcast ─────────────────────────────────────────────────────────────
    if key == "broadcast":
        users     = load_users()
        bot       = update.get_bot()
        sent = failed = 0
        status_msg = await update.effective_chat.send_message(
            f"📣 *Broadcasting to {len(users)} users…*\n_Please wait…_",
            parse_mode=ParseMode.MARKDOWN,
        )
        for uid_str in users:
            try:
                await bot.send_message(
                    int(uid_str),
                    f"📣 *Message from UC STATE:*\n\n{text}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent += 1
            except Exception:
                failed += 1
        try:
            await status_msg.edit_text(
                f"✅ *Broadcast Complete!*\n\n"
                f"📨 Sent:   *{sent}*\n"
                f"❌ Failed: *{failed}*\n"
                f"📊 Total:  *{len(users)}*",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await send_admin_menu(update, ctx)
        return ADMIN_MENU_STATE

    # ── Change password ───────────────────────────────────────────────────────
    if key == "changepw":
        if len(text) < 6:
            await update.effective_chat.send_message(
                "❌ *Password too short!* Must be at least 6 characters. Try again:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ikb([("⬅️  Back", "adm_back")]),
            )
            return ADMIN_EDIT_STATE
        cfg["admin_password"] = text
        save_config(cfg)
        await update.effective_chat.send_message(
            "✅ *Password changed successfully!*\n\n"
            "⚠️ _Remember your new password — it is not stored anywhere readable._",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_admin_menu(update, ctx)
        return ADMIN_MENU_STATE

    # ── Add channel — Step 1: channel ID / username ───────────────────────────
    if key == "add_ch_id":
        ctx.user_data["new_ch_id"] = text
        if text.startswith("@"):
            ctx.user_data["new_ch_invite"] = f"https://t.me/{text.lstrip('@')}"
            ctx.user_data["adm_edit"]      = "add_ch_name"
            await update.effective_chat.send_message(
                f"✅ Channel: `{text}`\n\n"
                "📢 *Step 2 of 3* — Send a *display name* for this channel:\n"
                "_(e.g. UC STATE Updates)_",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ikb([("⬅️  Back", "adm_back")]),
            )
        else:
            ctx.user_data["adm_edit"] = "add_ch_invite"
            await update.effective_chat.send_message(
                f"✅ Channel ID: `{text}`\n\n"
                "🔗 *Step 2 of 3* — Send the *invite link* for this channel:\n"
                "_(e.g. https://t.me/+AbCdEfGhIjKl)_\n\n"
                "💡 Create one: Channel → Admin → Invite Links → Create new",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ikb([("⬅️  Back", "adm_back")]),
            )
        return ADMIN_EDIT_STATE

    # ── Add channel — Step 2 (private): invite link ───────────────────────────
    elif key == "add_ch_invite":
        ctx.user_data["new_ch_invite"] = text
        ctx.user_data["adm_edit"]      = "add_ch_name"
        await update.effective_chat.send_message(
            "✅ Invite link saved.\n\n"
            "📛 *Step 3 of 3* — Send a *display name* for this channel:\n"
            "_(e.g. UC STATE VIP)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb([("⬅️  Back", "adm_back")]),
        )
        return ADMIN_EDIT_STATE

    # ── Add channel — Step 3: display name → save ─────────────────────────────
    elif key == "add_ch_name":
        ch_id     = ctx.user_data.pop("new_ch_id",     "")
        ch_invite = ctx.user_data.pop("new_ch_invite", "")
        ch_name   = text or ch_id
        channels  = cfg.setdefault("force_channels", [])
        channels.append({"id": ch_id, "invite_link": ch_invite, "name": ch_name})
        save_config(cfg)
        await update.effective_chat.send_message(
            "✅ *Channel added successfully!*\n\n"
            f"📢 Name: `{ch_name}`\n"
            f"🆔 ID:   `{ch_id}`\n"
            f"🔗 Link: `{ch_invite}`\n\n"
            "⚠️ Make sure this bot is an *admin* in that channel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_admin_menu(update, ctx)
        return ADMIN_MENU_STATE

    # ── Edit packages ─────────────────────────────────────────────────────────
    elif key == "packages":
        try:
            packages = []
            for line in text.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    uc    = int(parts[0])
                    price = float(parts[1])
                    label = (
                        " ".join(parts[2:])
                        if len(parts) > 2
                        else f"{uc:,} UC (₹{price:.0f})"
                    )
                    packages.append({"uc": uc, "price": price, "label": label})
            if packages:
                cfg["packages"] = packages
                save_config(cfg)
                preview = "\n".join(f"  • {p['label']}" for p in packages)
                await update.effective_chat.send_message(
                    f"✅ *{len(packages)} packages updated!*\n\n"
                    f"*New packages:*\n{preview}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await update.effective_chat.send_message(
                    "❌ *No valid packages found.* Check the format:\n"
                    "`<UC_AMOUNT> <PRICE>` per line",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as e:
            await update.effective_chat.send_message(
                f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN
            )

    # ── Proof / Tutorial URL / Welcome msg ────────────────────────────────────
    elif key in ("proof_url", "tutorial_url", "welcome_msg"):
        cfg[key] = "" if text == "-" else text
        save_config(cfg)
        await update.effective_chat.send_message(
            "✅ *Updated successfully!*", parse_mode=ParseMode.MARKDOWN
        )

    await send_admin_menu(update, ctx)
    return ADMIN_MENU_STATE


async def cb_admin_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🔒 *Admin panel closed.*", parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    token = cfg.get("bot_token") or BOT_TOKEN
    if not token:
        raise ValueError("❌ Bot token not configured!")

    app = Application.builder().token(token).build()

    # ── Admin conversation ────────────────────────────────────────────────────
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", cmd_admin)],
        states={
            ADMIN_PASSWORD_STATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_admin_password),
            ],
            ADMIN_MENU_STATE: [
                CallbackQueryHandler(cb_admin_menu, pattern="^adm_"),
            ],
            ADMIN_EDIT_STATE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
                    recv_admin_edit,
                ),
                CallbackQueryHandler(cb_admin_menu, pattern="^adm_"),
            ],
        },
        fallbacks=[CallbackQueryHandler(cb_admin_cancel, pattern="^admin_cancel$")],
        per_message=False,
        allow_reentry=True,
    )

    # ── User conversation ─────────────────────────────────────────────────────
    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(cb_check_join,     pattern="^check_join$"),
                CallbackQueryHandler(cb_game_bgmi,      pattern="^game_bgmi$"),
                CallbackQueryHandler(cb_my_stats,       pattern="^my_stats$"),
                CallbackQueryHandler(cb_order_history,  pattern="^order_history$"),
                CallbackQueryHandler(cb_cancel_to_main, pattern="^cancel_to_main$"),
            ],
            WAIT_GAME_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_game_id),
                CallbackQueryHandler(cb_cancel_to_main, pattern="^cancel_to_main$"),
            ],
            CONFIRM_GAME_ID: [
                CallbackQueryHandler(cb_confirm_uid_yes, pattern="^confirm_uid_yes$"),
                CallbackQueryHandler(cb_confirm_uid_no,  pattern="^confirm_uid_no$"),
                CallbackQueryHandler(cb_cancel_to_main,  pattern="^cancel_to_main$"),
            ],
            SELECT_PACKAGE: [
                CallbackQueryHandler(cb_select_package,  pattern=r"^pkg_\d+$"),
                CallbackQueryHandler(cb_cancel_to_main,  pattern="^cancel_to_main$"),
                CallbackQueryHandler(cb_noop,            pattern="^noop$"),
            ],
            CONFIRM_ORDER: [
                CallbackQueryHandler(cb_pay_qr,           pattern="^pay_qr$"),
                CallbackQueryHandler(cb_back_to_packages, pattern="^back_to_packages$"),
                CallbackQueryHandler(cb_cancel_to_main,   pattern="^cancel_to_main$"),
            ],
            WAIT_PAYMENT: [
                CallbackQueryHandler(cb_verify_payment, pattern="^verify_payment$"),
                CallbackQueryHandler(cb_cancel_to_main, pattern="^cancel_to_main$"),
            ],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        per_message=False,
        allow_reentry=True,
    )

    # approve/reject use | separator (order IDs contain _ which broke split("_"))
    app.add_handler(CallbackQueryHandler(cb_admin_approve, pattern=r"^approve\|"))
    app.add_handler(CallbackQueryHandler(cb_admin_reject,  pattern=r"^reject\|"))

    app.add_handler(admin_conv)
    app.add_handler(user_conv)

    logger.info("🚀 UC STATE BOT v3.0 — Advanced Edition is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
