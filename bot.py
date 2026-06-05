"""
Personal Rubika sender — controlled from a Telegram panel.
==========================================================

What it does (and ONLY this):
  * lets the owner log into THEIR OWN Rubika account (phone + code + 2FA),
  * forwards a message the owner marked in their OWN Saved Messages
    (e.g. caption ending in `کد135`) to their OWN contacts,
  * recipients are ordered: chat-first, then online, then last-seen,
  * configurable delay between sends (0.2 - 10s),
  * stops the whole run after MAX_ERRORS failed sends,
  * posts styled log cards to a private Telegram report group.

What it deliberately does NOT do: proxies, multi-account orchestration,
batch broadcasting, or "send to everyone" automation.

Panel text is Persian. Only the configured owner id may use it.
"""
import asyncio
import os
from datetime import datetime

from telethon import TelegramClient, events, Button

import config
import db
import rubika_client as rb

# Make sure the data dir exists BEFORE the Telethon session file is created.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---- counter (total sends the bot has done), persisted in a small file ----
COUNTER_FILE = os.path.join(DATA_DIR, "send_count.txt")


def _read_counter() -> int:
    try:
        with open(COUNTER_FILE) as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def _next_counter() -> int:
    n = _read_counter() + 1
    try:
        os.makedirs(os.path.dirname(COUNTER_FILE), exist_ok=True)
        with open(COUNTER_FILE, "w") as f:
            f.write(str(n))
    except Exception:
        pass
    return n


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


LINE = "━━━━━━━━━━━━━━━━"


def card(title: str, rows: list) -> str:
    return f"{title}\n{LINE}\n" + "\n".join(rows)


bot = TelegramClient(os.path.join(DATA_DIR, "panel_bot"), config.API_ID, config.API_HASH)

# conversation state per owner: {"step": "..."}
state: dict = {}
# rubpy login clients mid-flow (waiting for code / password)
pending: dict = {}
# prepared sends waiting for confirmation: owner_id -> payload
pending_send: dict = {}
# stop flags per account id
stop_flags: dict = {}


def is_owner(event) -> bool:
    return event.sender_id in config.ALLOWED_IDS


async def log(text: str):
    """Post a report card to the log group (never crash the bot)."""
    try:
        await bot.send_message(config.LOG_GROUP_ID, text)
    except Exception as e:  # noqa: BLE001
        print(f"[log error] {e}")


# --------------------------------------------------------------------------- #
# Menus
# --------------------------------------------------------------------------- #
def main_menu():
    return [
        [Button.inline("➕ افزودن اکانت", b"add_account"),
         Button.inline("👤 اکانت من", b"accounts")],
        [Button.inline("🚀 ارسال", b"send_menu")],
        [Button.inline("⚙️ تنظیم سرعت ارسال", b"speed")],
        [Button.inline("💾 بکاپ", b"backup")],
    ]


WELCOME = (
    "╭───────────────────╮\n"
    "     🤖  پنل روبیکا\n"
    "╰───────────────────╯\n"
    "خوش اومدی 👋 یکی از گزینه‌ها رو انتخاب کن:"
)


@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if not is_owner(event):
        await event.respond("⛔ شما به این ربات دسترسی ندارید.")
        return
    state.pop(event.sender_id, None)
    await event.respond(WELCOME, buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await event.edit(WELCOME, buttons=main_menu())


@bot.on(events.CallbackQuery(data=b"cancel"))
async def cancel_cb(event):
    if not is_owner(event):
        return
    p = pending.pop(event.sender_id, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass
    state.pop(event.sender_id, None)
    await event.edit("لغو شد. منوی اصلی:", buttons=main_menu())


# --------------------------------------------------------------------------- #
# Add account
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"add_account"))
async def add_account_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_phone"}
    await event.edit(
        "📱 شماره اکانت روبیکای خودت رو بفرست.\nمثال: `09123456789`",
        buttons=[[Button.inline("🔙 لغو", b"cancel")]],
    )


# --------------------------------------------------------------------------- #
# Accounts list / dashboard
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"accounts"))
async def accounts_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.edit(
            "هنوز اکانتی اضافه نکردی.",
            buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                     [Button.inline("🔙 بازگشت", b"home")]],
        )
        return
    buttons = []
    for i, acc in enumerate(accounts, start=1):
        mark = "" if acc["status"] == "active" else " ⚠️"
        buttons.append([Button.inline(f"{i}- {acc['phone']}{mark}",
                                      f"acc_{acc['id']}".encode())])
    buttons.append([Button.inline("🔙 بازگشت", b"home")])
    await event.edit("👤 اکانت‌های تو:", buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"acc_(\\d+)"))
async def account_menu_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    status = "فعال ✅" if acc["status"] == "active" else "غیرفعال ⚠️"
    text = (
        "╭───── 👤 اکانت ─────╮\n"
        f"  نام    : {acc['name'] or '-'}\n"
        f"  شماره  : {acc['phone']}\n"
        f"  آیدی   : {acc['user_id']}\n"
        f"  وضعیت  : {status}\n"
        "╰────────────────────╯"
    )
    buttons = [
        [Button.inline("🚀 ارسال با این اکانت", f"send_{account_id}".encode())],
        [Button.inline("🗑 حذف اکانت", f"del_{account_id}".encode())],
        [Button.inline("🔙 بازگشت", b"accounts")],
    ]
    await event.edit(text, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"del_(\\d+)"))
async def delete_confirm_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    await event.edit(
        "از حذف این اکانت مطمئنی؟",
        buttons=[[Button.inline("✅ بله، حذف کن", f"delyes_{account_id}".encode())],
                 [Button.inline("🔙 خیر", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"delyes_(\\d+)"))
async def delete_do_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.delete_account(account_id)
    await event.edit("اکانت حذف شد. ✅",
                     buttons=[[Button.inline("🔙 بازگشت", b"accounts")]])


# --------------------------------------------------------------------------- #
# Speed (delay) setting
# --------------------------------------------------------------------------- #
def speed_buttons():
    return [
        [Button.inline("0.2s", b"sp_0.2"), Button.inline("0.5s", b"sp_0.5"),
         Button.inline("1s", b"sp_1")],
        [Button.inline("2s", b"sp_2"), Button.inline("5s", b"sp_5"),
         Button.inline("10s", b"sp_10")],
        [Button.inline("🔙 بازگشت", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"speed"))
async def speed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_delay"}
    await event.edit(
        f"⏱ تأخیر فعلی: {db.get_delay()} ثانیه\n{LINE}\n"
        "یک سرعت انتخاب کن، یا یک عدد بین ۰.۲ تا ۱۰ بفرست:",
        buttons=speed_buttons(),
    )


@bot.on(events.CallbackQuery(pattern=b"sp_([0-9.]+)"))
async def speed_set_cb(event):
    if not is_owner(event):
        return
    value = config.clamp_delay(event.pattern_match.group(1).decode())
    db.set_delay(value)
    state.pop(event.sender_id, None)
    await event.edit(f"✅ تأخیر روی {value} ثانیه تنظیم شد.",
                     buttons=[[Button.inline("🔙 منوی اصلی", b"home")]])


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"backup"))
async def backup_cb(event):
    if not is_owner(event):
        return
    if os.path.exists(db.DB_PATH):
        await bot.send_file(event.sender_id, db.DB_PATH, caption=f"💾 بکاپ • {now()}")
        await event.answer("بکاپ ارسال شد.")
    else:
        await event.answer("هنوز دیتابیسی وجود ندارد.", alert=True)


# --------------------------------------------------------------------------- #
# Send menu (pick which account)
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"send_menu"))
async def send_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.edit("اول یک اکانت اضافه کن.",
                         buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                  [Button.inline("🔙 بازگشت", b"home")]])
        return
    buttons = [[Button.inline(f"🚀 {a['phone']}", f"send_{a['id']}".encode())]
               for a in accounts]
    buttons.append([Button.inline("🔙 بازگشت", b"home")])
    await event.edit("با کدوم اکانت ارسال بشه؟", buttons=buttons)


# --------------------------------------------------------------------------- #
# Message router (conversation steps)
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage)
async def message_router(event):
    if not is_owner(event):
        return
    if event.raw_text.startswith("/start"):
        return
    st = state.get(event.sender_id)
    if not st:
        return
    step = st.get("step")
    if step == "await_phone":
        await handle_phone(event)
    elif step == "await_code":
        await handle_code(event)
    elif step == "await_password":
        await handle_password(event)
    elif step == "await_delay":
        await handle_delay(event)


async def handle_delay(event):
    value = config.clamp_delay(event.raw_text.strip())
    db.set_delay(value)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ تأخیر روی {value} ثانیه تنظیم شد.", buttons=main_menu())


async def handle_phone(event):
    phone = event.raw_text.strip()
    await event.respond("⏳ در حال اتصال به روبیکا و ارسال کد ...")
    try:
        ctx = await rb.start_login(phone)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در ارسال کد: {e}\nدوباره شماره را بفرست یا لغو کن.")
        return
    pending[event.sender_id] = ctx
    status = str(ctx.get("status") or "").upper()
    if "PASS" in status:
        hint = ctx.get("hint") or ""
        state[event.sender_id] = {"step": "await_password"}
        await event.respond(
            "🔐 این اکانت رمز دومرحله‌ای دارد." + (f"\nراهنما: {hint}" if hint else "") +
            "\nرمز را بفرست.",
            buttons=[[Button.inline("🔙 لغو", b"cancel")]],
        )
        return
    if not ctx.get("phone_code_hash"):
        try:
            await ctx["client"].disconnect()
        except Exception:
            pass
        pending.pop(event.sender_id, None)
        await event.respond(f"❌ روبیکا کد نفرستاد (status: {status or 'نامشخص'}). دوباره تلاش کن.")
        return
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("📩 کد ورود در اپ روبیکا اومد. کد رو بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def handle_code(event):
    ctx = pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    try:
        await rb.finish_login(ctx, code)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ کد اشتباه یا خطا: {e}\nدوباره کد را بفرست یا لغو کن.")
        return
    await complete_account(event)


async def handle_password(event):
    ctx = pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    password = event.raw_text.strip()
    try:
        new_ctx = await rb.start_login(ctx["phone"], pass_key=password)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ رمز اشتباه یا خطا: {e}\nدوباره رمز را بفرست.")
        return
    pending[event.sender_id] = new_ctx
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("🔓 رمز پذیرفته شد. حالا کد ورود را بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def complete_account(event):
    ctx = pending.pop(event.sender_id, None)
    state.pop(event.sender_id, None)
    if not ctx:
        return
    client = ctx["client"]
    phone = ctx["phone"]
    try:
        me = await client.get_me()
        guid = rb._guid_of(me) or "-"
        name = rb._name_of(me)
        ordered, stats = await rb.get_ordered_recipients(client)
        account_id = db.add_account(phone, name, str(guid), rb.session_path(phone))

        await log(card("LOGIN SUCCESS ✅", [
            f"This Account : {phone}",
            LINE,
            f"Name : {name}",
            f"ID   : {guid}",
            LINE,
            f"📇 Contacts : {stats['contacts']}",
            f"👥 Groups   : {stats['groups']}",
            f"🎯 Contact with chat : {stats['with_chat']}",
        ]))
        await event.respond(
            "✅ اکانت با موفقیت اضافه شد!\n"
            f"👤 {name} | 📱 {phone}\n"
            f"📇 مخاطبین: {stats['contacts']} | 👥 گروه‌ها: {stats['groups']} | "
            f"💬 چت‌دار: {stats['with_chat']}",
            buttons=[[Button.inline("🚀 ارسال", f"send_{account_id}".encode())],
                     [Button.inline("🏠 منوی اصلی", b"home")]],
        )
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا بعد از ورود: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Send: prepare -> confirm -> run
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"send_(\\d+)"))
async def send_prepare_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    marker = db.get_marker()
    await event.edit("⏳ در حال آماده‌سازی (اتصال، پیدا کردن پیام نشان‌دار، خواندن مخاطب‌ها) ...")

    client = rb.open_client(acc["phone"])
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        if not mid:
            await event.edit(
                f"❌ توی Saved Messages پیامی با مارکر «{marker}» پیدا نشد.\n"
                "یه پیام (متن/عکس/فایل) توی Saved Messages بذار که آخر کپشنش این مارکر باشه.",
                buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]],
            )
            return
        ordered, stats = await rb.get_ordered_recipients(client)
    except Exception as e:  # noqa: BLE001
        await event.edit(f"❌ خطا در آماده‌سازی: {e}",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if not ordered:
        await event.edit("هیچ مخاطبی برای ارسال پیدا نشد.",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return

    pending_send[event.sender_id] = {
        "account_id": account_id,
        "phone": acc["phone"],
        "saved_guid": saved_guid,
        "mid": mid,
        "recipients": [r["guid"] for r in ordered],
    }

    names = "، ".join(r["name"] for r in ordered[:10])
    if len(ordered) > 10:
        names += f" و {len(ordered) - 10} نفر دیگر"
    await event.edit(
        card("🚀 آماده‌ی ارسال", [
            f"📎 محتوا : پیام نشان‌دار «{marker}» ✅",
            f"🎯 گیرنده‌ها : {len(ordered)} مخاطب",
            "ترتیب : چت‌دار ← آنلاین ← Last Seen",
            LINE,
            f"👤 {names}",
            "",
            "به این مخاطب‌ها ارسال بشه؟",
        ]),
        buttons=[[Button.inline("✅ تأیید و ارسال", f"go_{account_id}".encode())],
                 [Button.inline("🔙 لغو", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"go_(\\d+)"))
async def send_go_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    payload = pending_send.get(event.sender_id)
    if not payload or payload["account_id"] != account_id:
        await event.answer("اطلاعات ارسال منقضی شده. دوباره «ارسال» رو بزن.", alert=True)
        return
    stop_flags[account_id] = False
    await event.edit(
        f"⏳ شروع ارسال به {len(payload['recipients'])} مخاطب ... گزارش‌ها در گروه لاگ میاد.",
        buttons=[[Button.inline("⏹ توقف ارسال", f"stop_{account_id}".encode())]],
    )
    # run the send in the background so the handler returns quickly
    asyncio.create_task(run_send(event.sender_id, payload))


@bot.on(events.CallbackQuery(pattern=b"stop_(\\d+)"))
async def stop_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    stop_flags[account_id] = True
    await event.answer("درخواست توقف ثبت شد. بعد از پیام جاری متوقف می‌شود.", alert=True)


async def run_send(owner_id: int, payload: dict):
    account_id = payload["account_id"]
    phone = payload["phone"]
    saved_guid = payload["saved_guid"]
    mid = payload["mid"]
    recipients = payload["recipients"]
    marker = db.get_marker()
    delay = db.get_delay()

    count = _next_counter()
    total = len(recipients)
    ok = 0
    fail = 0
    started = datetime.now()
    reason = None

    await log(card("SEND STARTED 🚀", [
        f"🛠 Count : {count:03d}",
        f"📱 Phone : {phone}",
        f"🕒 Started : {now()}",
        LINE,
        f"🎯 Targets : {total}",
        f"⏱ Delay : {delay}s",
        f"📌 Marker : «{marker}» Found ✅",
    ]))

    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        for guid in recipients:
            if stop_flags.get(account_id):
                reason = "توقف دستی توسط کاربر"
                break
            try:
                await asyncio.wait_for(
                    rb.forward_message(client, saved_guid, guid, mid),
                    timeout=config.SEND_TIMEOUT,
                )
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                await log(card("⚠️ SEND ERROR", [
                    f"📱 Phone : {phone}",
                    f"🎯 To : {guid}",
                    f"💥 Error : {repr(e)[:200]}",
                ]))
                if fail >= config.MAX_ERRORS:
                    reason = f"رسیدن به سقف خطا ({config.MAX_ERRORS})"
                    break
            await asyncio.sleep(delay)
    except Exception as e:  # noqa: BLE001
        reason = f"خطای کلی: {repr(e)[:200]}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    dur = str(datetime.now() - started).split(".")[0]
    pending_send.pop(owner_id, None)

    if reason:
        await log(card("⛔ SEND STOPPED", [
            f"👤 Account : {phone}",
            f"📊 ✅ {ok}   ❌ {fail}   📁 {total}",
            f"⚠️ Reason : {reason}",
            f"⏱ Duration : {dur}",
            f"🕒 {now()}",
        ]))
        try:
            await bot.send_message(owner_id, f"⛔ ارسال متوقف شد. ✅ {ok} / ❌ {fail} از {total}\nدلیل: {reason}",
                                   buttons=main_menu())
        except Exception:
            pass
    else:
        await log(card("SEND FINISHED ✅", [
            "🟢 Status : Completed",
            f"👤 Account : {phone}",
            LINE,
            f"✅ {ok}   ❌ {fail}   📁 {total}",
            f"⏱ Duration : {dur}",
        ]))
        try:
            await bot.send_message(owner_id, f"✅ ارسال تمام شد. ✅ {ok} / ❌ {fail} از {total}",
                                   buttons=main_menu())
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
async def amain():
    problems = config.validate()
    if problems:
        print("Missing settings in .env: " + ", ".join(problems))
        return
    db.init()
    await bot.start(bot_token=config.BOT_TOKEN)
    await log(card("Online", [f"Rubika Project {config.VERSION}", LINE, f"🕒 {now()}"]))
    print(f"Panel is running (version {config.VERSION}).")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(amain())
