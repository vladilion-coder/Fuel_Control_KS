import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, CallbackQueryHandler, TypeHandler, filters
)

# ── Імпорт із sheets.py ───────────────────────────────────────────────────────
# ОБОВ'ЯЗКОВО: у sheets.py має бути функція get_objects_for_report()
from sheets import (
    get_objects_for_report,         # форматовані значення для звіту/відображення
    add_object, delete_object,      # адміністрування
    update_capacity, update_usage,  # адміністрування
    update_object_fuel_with_calc    # логіка збереження даних + обчислення
)

# ── Налаштування ──────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x]

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

# ── Стейти ────────────────────────────────────────────────────────────────────
(
    ENTER_OBJECT_ID,
    ENTER_ENGINE_HOURS,
    ENTER_FUEL,
    ENTER_NEW_OBJECT_ID,
    ENTER_NEW_CAPACITY,
    ENTER_DELETE_OBJECT_ID,
    ENTER_UPDATE_OBJECT_ID,
    ENTER_UPDATE_CAPACITY,
    ENTER_UPDATE_USAGE_OBJECT_ID,
    ENTER_UPDATE_USAGE_VALUE,
    ENTER_REPORT_OBJECT_ID,
) = range(11)

# ── Хелпери ───────────────────────────────────────────────────────────────────
TELEGRAM_MAX_LEN = 3900  # нижче 4096, щоб не ловити BadRequest: message is too long

def _to_float_safe(x, default: float = 0.0) -> float:
    """Парсер чисел (коми/крапки/пробіли/нерозривні пробіли)."""
    if x is None:
        return float(default)
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s:
        return float(default)
    s = s.replace(" ", "").replace("\u00A0", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return float(default)

def _fmt_hours(val) -> str:
    """Відображає мотогодини так, як у Sheets (щоб не було ×10)."""
    if isinstance(val, (int, float)):
        s = f"{float(val):.2f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    return str(val).strip()

async def send_long_text(update: Update, text: str, parse_mode: Optional[str] = None):
    """Розбиває довгий текст на частини і надсилає кількома повідомленнями."""
    logger.debug("send_long_text: start, len=%d", len(text))
    lines, chunk, length = text.split("\n"), [], 0

    async def _flush():
        if chunk:
            payload = "\n".join(chunk)
            logger.debug("send_long_text: flushing chunk len=%d", len(payload))
            await update.effective_message.reply_text(payload, parse_mode=parse_mode)
            chunk.clear()

    for line in lines:
        add_len = len(line) + 1
        if length + add_len > TELEGRAM_MAX_LEN:
            await _flush()
            length = 0
        chunk.append(line)
        length += add_len
    await _flush()
    logger.debug("send_long_text: done")

def is_admin(uid: int) -> bool:
    res = uid in ADMINS
    logger.debug("is_admin(%s) -> %s", uid, res)
    return res

def main_kb(is_admin_user: bool) -> ReplyKeyboardMarkup:
    base = [
        ["➕ Нові показники", "📊 Звіт", "📊 Звіт по об’єкту"],
        ["🔻 Недозаправка"],
    ]
    if is_admin_user:
        base.append(["⚙️ Додати об’єкт", "🗑 Видалити об’єкт", "✏️ Змінити бак", "🔧 Витрата л/год"])
    return ReplyKeyboardMarkup(base, resize_keyboard=True)

# ── Error handler ─────────────────────────────────────────────────────────────
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Помилка. Спробуйте ще раз або /start")
    except Exception:
        pass

# ── Всеосяжний логер ─────────────────────────────────────────────────────────
async def log_every_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("UPDATE: %s", [k for k, v in update.to_dict().items() if v is not None])

# ── Команди / Хендлери ───────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    logger.debug("ADMINS: %r; user_id=%s", ADMINS, uid)
    await update.message.reply_text("Привіт 👋 Оберіть дію:", reply_markup=main_kb(is_admin(uid)))

# === Нові показники ===========================================================
async def new_data_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objs = get_objects_for_report()
    if not objs:
        await update.message.reply_text("⚠️ Лист 'Objects' порожній.")
        return ConversationHandler.END
    ids = [str(o.get("ObjectID", "")).strip() for o in objs if o.get("ObjectID")]
    await update.message.reply_text("Вкажіть ID об’єкта:\nДоступні: " + (", ".join(ids) if ids else "—"))
    return ENTER_OBJECT_ID

async def enter_object_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["object_id"] = update.message.text.strip()
    await update.message.reply_text("Введіть поточні мотогодини ⏱️ (число):")
    return ENTER_ENGINE_HOURS

async def enter_engine_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # те, що ввів користувач
    text = update.message.text.strip()
    object_id = context.user_data.get("object_id", "").strip()

    # парсимо як число (допускаємо кому)
    try:
        entered_hours = float(text.replace(",", "."))
    except Exception:
        await update.message.reply_text("Будь ласка, введіть число (наприклад, 735.4). Спробуйте ще раз:")
        return ENTER_ENGINE_HOURS

    # дістаємо поточні значення із Sheets і шукаємо наш об’єкт
    objs = get_objects_for_report()
    current_in_sheet = None
    for o in objs:
        if str(o.get("ObjectID", "")).strip() == object_id:
            current_in_sheet = _to_float_safe(o.get("EngineHours", 0.0))
            break

    # якщо об'єкт не знайдено у таблиці — повідомимо і зупинимо розмову (це нетипово)
    if current_in_sheet is None:
        await update.message.reply_text(f"⚠️ Об’єкт {object_id} не знайдено у таблиці. Спробуйте /start")
        return ConversationHandler.END

    # перевірка: нове значення не може бути меншим за те, що вже у таблиці
    if entered_hours < current_in_sheet:
        pretty = _fmt_hours(current_in_sheet)
        await update.message.reply_text(
            f"❌ Значення мотогодин має бути не меншим за поточне у таблиці ({pretty}). "
            f"Введіть коректне число ще раз:"
        )
        return ENTER_ENGINE_HOURS

    # все гаразд — збережемо у контекст і підемо далі
    context.user_data["engine_hours"] = str(entered_hours)
    await update.message.reply_text("Скільки літрів пального заправлено? ⛽ (можна 0):")
    return ENTER_FUEL


    context.user_data["engine_hours"] = text
    await update.message.reply_text("Скільки літрів пального заправлено? ⛽ (можна 0):")
    return ENTER_FUEL

async def enter_fuel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fuel_added"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Так, повний", callback_data="full_yes"),
         InlineKeyboardButton("Ні", callback_data="full_no")]
    ])
    await update.message.reply_text("Чи бак повний після заправки?", reply_markup=kb)
    return ConversationHandler.END

async def confirm_full_tank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    full_tank = (query.data == "full_yes")
    object_id = context.user_data.get("object_id")
    engine_hours = float(str(context.user_data.get("engine_hours", "0")).replace(",", "."))
    fuel_added = float(str(context.user_data.get("fuel_added", "0")).replace(",", "."))

    try:
        ok = update_object_fuel_with_calc(
            object_id, engine_hours, fuel_added, full_tank,
            update.effective_user.id, update.effective_user.username or ""
        )
    except ValueError as ve:
        # показуємо людині причину (наприклад, якщо new < prev у бекенді)
        await query.edit_message_text(f"❌ {ve}")
        return

    if ok:
        txt = (f"✅ Збережено!\nОб’єкт: {object_id}\nМотогодини: {engine_hours}\n"
               f"Заправлено: {fuel_added} л\nПовний бак: {'так' if full_tank else 'ні'}")
    else:
        txt = "❌ Об’єкт не знайдено."
    await query.edit_message_text(txt)
    await query.message.reply_text("Готово ✅", reply_markup=main_kb(is_admin(update.effective_user.id)))

# === Звіт по всіх об’єктах ===================================================
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objects = get_objects_for_report()
    if not objects:
        await update.message.reply_text("⚠️ Дані відсутні.")
        return
    lines = ["📊 *Звіт по об’єктах:*\n"]
    for o in objects:
        obj   = str(o.get("ObjectID", "N/A"))
        cap   = _to_float_safe(o.get("FuelCapacity", 0))
        cur   = _to_float_safe(o.get("CurrentFuel", 0))
        usage = _to_float_safe(o.get("FuelUsagePerHour", 0))
        hrs_disp = _fmt_hours(o.get("EngineHours", 0))

        cur_disp = min(cur, cap) if cap > 0 else cur
        need     = max(0.0, cap - cur_disp)

        lines.append(
            f"🔹 {obj}\n"
            f"   ⏱️ Мотогодини: {hrs_disp}\n"
            f"   ⛽ Залишок: {cur_disp:.1f} / {cap:.1f} л\n"
            f"   ➕ До повного: {need:.1f} л\n"
            f"   🔧 Витрата: {usage:.2f} л/год\n"
        )
    await send_long_text(update, "\n".join(lines), parse_mode="Markdown")

# === Звіт по ОДНОМУ об’єкту ==================================================
async def single_report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objs = get_objects_for_report()
    if not objs:
        await update.message.reply_text("⚠️ Лист 'Objects' порожній.")
        return ConversationHandler.END
    ids = [str(o.get("ObjectID", "")).strip() for o in objs if o.get("ObjectID")]
    await update.message.reply_text("Вкажіть ID об’єкта для звіту:\nДоступні: " + (", ".join(ids) if ids else "—"))
    return ENTER_REPORT_OBJECT_ID

async def single_report_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj_id = update.message.text.strip()
    objects = get_objects_for_report()
    found = None
    for o in objects:
        if str(o.get("ObjectID", "")).strip() == obj_id:
            found = o
            break

    if not found:
        await update.message.reply_text(f"⚠️ Об’єкт {obj_id} не знайдено.")
        return ConversationHandler.END

    cap   = _to_float_safe(found.get("FuelCapacity", 0))
    cur   = _to_float_safe(found.get("CurrentFuel", 0))
    usage = _to_float_safe(found.get("FuelUsagePerHour", 0))
    hrs_disp = _fmt_hours(found.get("EngineHours", 0))
    cur_disp = min(cur, cap) if cap > 0 else cur
    need     = max(0.0, cap - cur_disp)

    text = (
        f"🔹 {obj_id}\n"
        f"   ⏱️ Мотогодини: {hrs_disp}\n"
        f"   ⛽ Залишок: {cur_disp:.1f} / {cap:.1f} л\n"
        f"   ➕ До повного: {need:.1f} л\n"
        f"   🔧 Витрата: {usage:.2f} л/год"
    )
    await update.message.reply_text(text)
    return ConversationHandler.END

# === Недозаправка (загальна) =================================================
async def shortage_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objects = get_objects_for_report()
    if not objects:
        await update.message.reply_text("⚠️ Дані відсутні.")
        return

    rows = []
    total_need = 0.0
    for o in objects:
        obj = str(o.get("ObjectID", "")).strip()
        cap = _to_float_safe(o.get("FuelCapacity", 0))
        cur = _to_float_safe(o.get("CurrentFuel", 0))
        cur_disp = min(cur, cap) if cap > 0 else cur
        need = max(0.0, cap - cur_disp)
        if obj and need > 0:
            rows.append((obj, need))
            total_need += need

    rows.sort(key=lambda x: x[0])  # сортуємо за ObjectID

    if not rows:
        await update.message.reply_text("✅ Усі баки заповнені (недозаправок немає).")
        return

    lines = ["🔻 Недозаправка:\n"]
    for obj, need in rows:
        lines.append(f"{obj} - {need:.1f}")
    lines.append(f"\nВсього = {total_need:.1f} літрів")

    await send_long_text(update, "\n".join(lines))

# === Адмін: Додати об’єкт ====================================================
async def add_object_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    await update.message.reply_text("Введіть ID нового об’єкта (напр. US0007):")
    return ENTER_NEW_OBJECT_ID

async def enter_new_object_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_obj_id"] = update.message.text.strip()
    await update.message.reply_text("Введіть ємність бака (л), число (напр. 300):")
    return ENTER_NEW_CAPACITY

async def save_new_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    obj_id = context.user_data.get("new_obj_id")
    cap_str = update.message.text.strip().replace(",", ".")
    try:
        cap = float(cap_str)
        add_object(obj_id, cap, usage_per_hour=0.0)
        await update.message.reply_text(f"✅ Додано {obj_id} з бака́м {cap} л", reply_markup=main_kb(True))
    except Exception as e:
        logger.exception("save_new_object error")
        await update.message.reply_text(f"❌ Помилка: {e}")
    return ConversationHandler.END

# === Адмін: Видалити об’єкт ==================================================
async def delete_object_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    await update.message.reply_text("Введіть ID об’єкта для видалення:")
    return ENTER_DELETE_OBJECT_ID

async def confirm_delete_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    obj_id = update.message.text.strip()
    ok = delete_object(obj_id)
    await update.message.reply_text("🗑 Видалено." if ok else "❌ Об’єкт не знайдено.")
    return ConversationHandler.END

# === Адмін: Змінити ємність бака =============================================
async def update_capacity_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    await update.message.reply_text("Введіть ID об’єкта:")
    return ENTER_UPDATE_OBJECT_ID

async def enter_update_object_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["upd_obj_id"] = update.message.text.strip()
    await update.message.reply_text("Введіть нову ємність бака (л), число:")
    return ENTER_UPDATE_CAPACITY

async def save_update_capacity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    obj_id = context.user_data.get("upd_obj_id")
    cap_str = update.message.text.strip().replace(",", ".")
    try:
        ok = update_capacity(obj_id, float(cap_str))
        await update.message.reply_text("✅ Оновлено." if ok else "❌ Об’єкт не знайдено.")
    except Exception as e:
        logger.exception("save_update_capacity error")
        await update.message.reply_text(f"❌ Помилка: {e}")
    return ConversationHandler.END

# === Адмін: Змінити витрату л/год ============================================
async def update_usage_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    await update.message.reply_text("Введіть ID об’єкта для зміни витрати (л/год):")
    return ENTER_UPDATE_USAGE_OBJECT_ID

async def enter_update_usage_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["usage_obj_id"] = update.message.text.strip()
    await update.message.reply_text("Введіть нову витрату пального (л/год), число:")
    return ENTER_UPDATE_USAGE_VALUE

async def save_update_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас немає прав доступу.")
        return ConversationHandler.END
    obj_id = context.user_data.get("usage_obj_id")
    usage_str = update.message.text.strip().replace(",", ".")
    try:
        ok = update_usage(obj_id, float(usage_str))
        await update.message.reply_text("✅ Оновлено." if ok else "❌ Об’єкт не знайдено.")
    except Exception as e:
        logger.exception("save_update_usage error")
        await update.message.reply_text(f"❌ Помилка: {e}")
    return ConversationHandler.END

# === Cancel ==================================================================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Скасовано.", reply_markup=main_kb(is_admin(update.effective_user.id)))
    return ConversationHandler.END

# ── Реєстрація та запуск ──────────────────────────────────────────────────────
from telegram.ext import Application, Update

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    # === тут треба зареєструвати усі твої хендлери, що були у main() ===
    app.add_error_handler(on_error)
    app.add_handler(TypeHandler(Update, log_every_update), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_new)
    app.add_handler(conv_add)
    app.add_handler(conv_del)
    app.add_handler(conv_cap)
    app.add_handler(conv_usage)
    app.add_handler(conv_single_report)
    app.add_handler(MessageHandler(filters.Regex(r"^\s*📊\s*Звіт\s*$"), report))
    app.add_handler(MessageHandler(filters.Regex(r"^\s*🔻\s*Недозаправка\s*$"), shortage_report))
    app.add_handler(CallbackQueryHandler(confirm_full_tank, pattern="^(full_yes|full_no)$"))

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    import os
    load_dotenv()
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задано у .env")
    app = build_app(TOKEN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

