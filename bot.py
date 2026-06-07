"""
bot.py — Telegram-бот для силовых тренировок.
"""

import csv
import io
import json
import os
import traceback
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import db
from config import (
    DEFAULT_SETS, DEFAULT_REPS, DEFAULT_REST_S,
    MIN_SETS_FOR_PROGRESS, REPS_MIN, REPS_MAX,
    WEIGHT_KEYBOARD_RANGE, DEFAULT_TZ,
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("❌ TOKEN не задан")

TZ = ZoneInfo(os.environ.get("TZ", DEFAULT_TZ))

# ============================================================
# СОСТОЯНИЯ FSM
# ============================================================

(
    CRT_NAME,
    CRT_PROGRESS,
    CRT_EX_NAME,
    CRT_EX_EQUIP,
    CRT_EX_EQUIP_NEW_NAME,
    CRT_EX_EQUIP_NEW_WEIGHTS,
    CRT_EX_SETS,
    CRT_EX_REPS,
    CRT_EX_REST,
    CRT_EX_COMMENT,
    CRT_EX_MORE,
) = range(11)

(
    WRK_SELECT,
    WRK_WEIGHT,
    WRK_REPS,
    WRK_REST,
    WRK_CONFIRM_CHANGE,
) = range(11, 16)

EXPORT_FORMAT = 16

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_closest_weight(available: list[float], target: float) -> float:
    return min(available, key=lambda w: abs(w - target))


def compute_new_weight(
    current: float, available: list[float],
    set3_reps: int, set4_reps: int, set4_weight: float
) -> float:
    if set3_reps < 10:
        try:
            idx = available.index(current)
            return available[idx - 1] if idx > 0 else current
        except ValueError:
            return current
    elif set4_reps == 10 and set4_weight == current:
        try:
            idx = available.index(current)
            return available[idx + 1] if idx + 1 < len(available) else current
        except ValueError:
            return current
    return current


async def send(update: Update, context: ContextTypes.DEFAULT_TYPE,
               text: str, reply_markup=None):
    kwargs = dict(text=text, reply_markup=reply_markup, parse_mode="HTML")
    if update.callback_query:
        await update.callback_query.message.reply_text(**kwargs)
    elif update.message:
        await update.message.reply_text(**kwargs)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, **kwargs)


def yn_keyboard(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да", callback_data=yes_data),
        InlineKeyboardButton("❌ Нет", callback_data=no_data),
    ]])


def weight_keyboard(available: list[float], recommended: float,
                    set_num: int, target_reps: int) -> InlineKeyboardMarkup:
    try:
        idx = available.index(recommended)
    except ValueError:
        idx = 0
    start = max(0, idx - WEIGHT_KEYBOARD_RANGE)
    end = min(len(available), idx + WEIGHT_KEYBOARD_RANGE + 1)
    subset = available[start:end]
    keyboard = []
    row = []
    for w in subset:
        label = f"→ {w}" if w == recommended else str(w)
        row.append(InlineKeyboardButton(label, callback_data=f"w_{w}_{set_num}_{target_reps}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("📋 Все веса", callback_data=f"wall_{set_num}_{target_reps}")])
    keyboard.append([InlineKeyboardButton("❌ Отменить тренировку", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)


def all_weights_keyboard(available: list[float],
                          set_num: int, target_reps: int) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for w in available:
        row.append(InlineKeyboardButton(str(w), callback_data=f"w_{w}_{set_num}_{target_reps}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отменить тренировку", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)


def reps_keyboard(target: int, set_num: int) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for r in range(REPS_MIN, REPS_MAX + 1):
        label = f"→ {r}" if r == target else str(r)
        row.append(InlineKeyboardButton(label, callback_data=f"r_{r}_{set_num}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отменить тренировку", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)


async def build_month_history(user_id: int) -> str:
    sessions = await db.get_month_session(user_id)
    if not sessions:
        return "📅 В этом месяце тренировок ещё не было."
    lines = ["📅 <b>Тренировки в этом месяце:</b>"]
    icons = {"completed": "✅", "cancelled": "❌", "active": "🔄"}
    for i, s in enumerate(sessions, 1):
        dt = s["started_at"].astimezone(TZ).strftime("%d.%m %H:%M")
        icon = icons.get(s["status"], "•")
        lines.append(f"{i}. {icon} {dt} — {s['template_name']}")
    return "\n".join(lines)


def get_set_params(ex: dict, set_overrides: dict, set_num: int) -> tuple[int, int, float]:
    ov = set_overrides.get(set_num, {})
    reps = ov.get("reps") or ex["default_reps"]
    rest_s = ov.get("rest_s") or ex["default_rest_s"]
    weight_pct = ov.get("weight_pct") or 1.0
    return reps, rest_s, weight_pct


# ============================================================
# /start — ГЛАВНЫЙ ЭКРАН
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()

    user_id = update.effective_user.id
    templates = await db.get_user_workout_templates(user_id)
    history = await build_month_history(user_id)

    if not templates:
        await send(update, context,
                   f"{history}\n\n"
                   "У вас ещё нет тренировок.\n"
                   "Давайте создадим первую! 💪",
                   reply_markup=InlineKeyboardMarkup([[
                       InlineKeyboardButton("➕ Создать тренировку", callback_data="new_workout")
                   ]]))
        return WRK_SELECT

    buttons = [[InlineKeyboardButton(t["name"], callback_data=f"tmpl_{t['id']}")]
               for t in templates]
    buttons.append([InlineKeyboardButton("➕ Создать новую тренировку", callback_data="new_workout")])
    buttons.append([InlineKeyboardButton("📤 Экспорт данных", callback_data="export")])

    await send(update, context,
               f"{history}\n\nВыберите тренировку:",
               reply_markup=InlineKeyboardMarkup(buttons))
    return WRK_SELECT


# ============================================================
# СОЗДАНИЕ ТРЕНИРОВКИ
# ============================================================

async def new_workout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["new_wt"] = {"exercises": []}
    await send(update, context,
               "📝 <b>Создание тренировки</b>\n\nВведите название тренировки:")
    return CRT_NAME


async def crt_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым. Попробуйте снова:")
        return CRT_NAME
    context.user_data["new_wt"]["name"] = name
    await update.message.reply_text(
        f"Тренировка «<b>{name}</b>»\n\n"
        "Включить автоматическую прогрессию весов?\n"
        "(бот будет повышать/снижать вес по результатам подходов)",
        parse_mode="HTML",
        reply_markup=yn_keyboard("prog_yes", "prog_no")
    )
    return CRT_PROGRESS


async def crt_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_wt"]["use_weight_progress"] = (query.data == "prog_yes")
    await query.message.reply_text(
        "✅ Настройки сохранены.\n\n"
        "Теперь добавим упражнения.\n"
        "<b>Введите название первого упражнения:</b>",
        parse_mode="HTML"
    )
    return CRT_EX_NAME


async def crt_ex_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data["new_ex"] = {"name": name}
    equipment = await db.get_all_equipment()
    buttons = [[InlineKeyboardButton(e["name"], callback_data=f"eq_{e['id']}")]
               for e in equipment]
    buttons.append([InlineKeyboardButton("➕ Создать новый тренажёр", callback_data="eq_new")])
    buttons.append([InlineKeyboardButton("— Без тренажёра", callback_data="eq_none")])
    await update.message.reply_text(
        f"Упражнение: <b>{name}</b>\n\nВыберите тренажёр:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CRT_EX_EQUIP


async def crt_ex_equip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "eq_new":
        await query.message.reply_text("Введите название нового тренажёра:")
        return CRT_EX_EQUIP_NEW_NAME

    if query.data == "eq_none":
        context.user_data["new_ex"]["equipment_id"] = None
        context.user_data["new_ex"]["available_weight"] = []
    else:
        eq_id = int(query.data.split("_")[1])
        eq = await db.get_equipment_by_id(eq_id)
        context.user_data["new_ex"]["equipment_id"] = eq_id
        context.user_data["new_ex"]["available_weight"] = list(eq["available_weight"])

    await query.message.reply_text(
        f"Сколько подходов? (по умолчанию {DEFAULT_SETS})\n"
        "Введите число или отправьте пустое сообщение:"
    )
    return CRT_EX_SETS


async def crt_ex_equip_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_eq_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите доступные веса тренажёра через запятую (в кг).\n"
        "Пример: <code>10, 15, 20, 25, 30</code>",
        parse_mode="HTML"
    )
    return CRT_EX_EQUIP_NEW_WEIGHTS


async def crt_ex_equip_new_weights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weights = sorted([float(w.strip()) for w in update.message.text.split(",")])
        if not weights:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите числа через запятую:")
        return CRT_EX_EQUIP_NEW_WEIGHTS

    eq_id = await db.create_equipment(context.user_data["new_eq_name"], weights)
    context.user_data["new_ex"]["equipment_id"] = eq_id
    context.user_data["new_ex"]["available_weight"] = weights

    await update.message.reply_text(
        f"✅ Тренажёр «{context.user_data['new_eq_name']}» создан.\n\n"
        f"Сколько подходов? (по умолчанию {DEFAULT_SETS}):"
    )
    return CRT_EX_SETS


async def crt_ex_sets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        sets = int(text) if text else DEFAULT_SETS
        if sets < 1 or sets > 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число от 1 до 10:")
        return CRT_EX_SETS
    context.user_data["new_ex"]["sets"] = sets
    await update.message.reply_text(
        f"Сколько повторений в каждом подходе? (по умолчанию {DEFAULT_REPS})\n"
        "Можно будет переопределить для отдельных подходов позже:"
    )
    return CRT_EX_REPS


async def crt_ex_reps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        reps = int(text) if text else DEFAULT_REPS
        if reps < 1 or reps > 50:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число от 1 до 50:")
        return CRT_EX_REPS
    context.user_data["new_ex"]["reps"] = reps
    await update.message.reply_text(
        f"Время отдыха между подходами в секундах? (по умолчанию {DEFAULT_REST_S})\n"
        "Например: 60, 90, 120:"
    )
    return CRT_EX_REST


async def crt_ex_rest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        rest = int(text) if text else DEFAULT_REST_S
        if rest < 0 or rest > 600:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число от 0 до 600 секунд:")
        return CRT_EX_REST
    context.user_data["new_ex"]["rest"] = rest
    await update.message.reply_text(
        "Комментарий к упражнению (необязательно).\n"
        "Например: «держать спину прямо»\n"
        "Или отправьте /skip чтобы пропустить."
    )
    return CRT_EX_COMMENT


async def crt_ex_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_ex"]["comment"] = None if text == "/skip" else text
    context.user_data["new_wt"]["exercises"].append(context.user_data.pop("new_ex"))
    n = len(context.user_data["new_wt"]["exercises"])
    await update.message.reply_text(
        f"✅ Упражнение {n} добавлено.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Ещё упражнение", callback_data="ex_more"),
            InlineKeyboardButton("✅ Завершить", callback_data="ex_done"),
        ]])
    )
    return CRT_EX_MORE


async def crt_ex_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "ex_done":
        return await _save_workout_template(update, context)
    await query.message.reply_text("Введите название следующего упражнения:")
    return CRT_EX_NAME


async def _save_workout_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.pop("new_wt")
    user_id = update.effective_user.id

    template_id = await db.create_workout_template(
        user_id, data["name"], data["use_weight_progress"]
    )
    for i, ex in enumerate(data["exercises"], 1):
        await db.create_exercise_template(
            workout_id=template_id,
            equipment_id=ex.get("equipment_id"),
            name=ex["name"],
            order_index=i,
            default_sets=ex["sets"],
            default_reps=ex["reps"],
            default_rest_s=ex["rest"],
            comment=ex.get("comment"),
        )

    await send(update, context,
               f"🎉 Тренировка «<b>{data['name']}</b>» создана!\n"
               f"Упражнений: {len(data['exercises'])}\n"
               f"Прогрессия весов: {'включена ✅' if data['use_weight_progress'] else 'выключена ❌'}\n\n"
               "Хотите начать эту тренировку сейчас?",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("▶️ Начать", callback_data=f"tmpl_{template_id}"),
                   InlineKeyboardButton("↩️ На главную", callback_data="go_start"),
               ]]))
    return WRK_SELECT


# ============================================================
# ВЫПОЛНЕНИЕ ТРЕНИРОВКИ
# ============================================================

async def select_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "go_start":
        return await start(update, context)
    if query.data == "new_workout":
        return await new_workout_start(update, context)
    if query.data == "export":
        return await export_start(update, context)

    template_id = int(query.data.split("_")[1])
    user_id = update.effective_user.id
    template = await db.get_workout_template(template_id, user_id)
    if not template:
        await query.message.reply_text("⚠️ Тренировка не найдена.")
        return WRK_SELECT

    exercises = await db.get_exercise_templates(template_id)
    if not exercises:
        await query.message.reply_text("⚠️ В тренировке нет упражнений.")
        return WRK_SELECT

    session_id = await db.create_session(user_id, template_id, template["name"])

    context.user_data.update({
        "user_id":        user_id,
        "session_id":     session_id,
        "template":       dict(template),
        "exercises":      [dict(e) for e in exercises],
        "exercise_index": 0,
        "use_progress":   template["use_weight_progress"],
    })

    await query.edit_message_text(
        f"▶️ Начинаем: <b>{template['name']}</b>",
        parse_mode="HTML"
    )
    return await start_exercise(update, context)


async def start_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exercises = context.user_data["exercises"]
    idx = context.user_data["exercise_index"]

    if idx >= len(exercises):
        return await finish_workout(update, context)

    ex = exercises[idx]
    user_id = context.user_data["user_id"]
    available = list(ex["available_weight"]) if ex["available_weight"] else []

    set_overrides_raw = await db.get_set_templates(ex["id"])
    set_overrides = {r["set_number"]: dict(r) for r in set_overrides_raw}

    current_weight = await db.get_weight(user_id, ex["id"], 0.0)

    context.user_data.update({
        "current_exercise": ex,
        "available":        available,
        "set_overrides":    set_overrides,
        "current_weight":   current_weight,
        "current_sets":     [],
        "current_set_num":  1,
    })

    total = len(exercises)
    comment_str = f"\n💬 {ex['comment']}" if ex.get("comment") else ""
    last = await db.get_last_session_for_template(
        user_id, context.user_data["template"]["id"])
    last_str = ""
    if last:
        dt = last["started_at"].astimezone(TZ).strftime("%d.%m.%Y")
        last_str = f"\n📅 Последнее: {dt}"

    await send(update, context,
               f"📋 <b>Упражнение {idx + 1}/{total}: {ex['name']}</b>"
               f"{comment_str}{last_str}\n"
               f"🎯 Рабочий вес: <b>{current_weight} кг</b>")

    return await ask_weight(update, context)


async def ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex = context.user_data["current_exercise"]
    set_num = context.user_data["current_set_num"]
    set_overrides = context.user_data["set_overrides"]
    current = context.user_data["current_weight"]
    available = context.user_data["available"]

    target_reps, rest_s, weight_pct = get_set_params(ex, set_overrides, set_num)
    context.user_data["temp_target_reps"] = target_reps
    context.user_data["temp_rest_s"] = rest_s

    pct_label = f"{int(weight_pct * 100)}%" if weight_pct != 1.0 else "100% (рабочий)"

    if not available:
        await send(update, context,
                   f"<b>Подход {set_num}</b> — цель: {target_reps} повт., {pct_label}\n"
                   f"Введите вес (кг):")
        return WRK_WEIGHT

    recommended = get_closest_weight(available, current * weight_pct)
    await send(update, context,
               f"<b>Подход {set_num}</b> — цель: {target_reps} повт., {pct_label}\n"
               f"💡 Рекомендуемый вес: <b>{recommended} кг</b>",
               reply_markup=weight_keyboard(available, recommended, set_num, target_reps))
    return WRK_WEIGHT


async def weight_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_workout":
        return await cancel_workout(update, context)

    if query.data.startswith("wall_"):
        parts = query.data.split("_")
        set_num, target_reps = int(parts[1]), int(parts[2])
        await query.edit_message_reply_markup(
            reply_markup=all_weights_keyboard(
                context.user_data["available"], set_num, target_reps))
        return WRK_WEIGHT

    parts = query.data.split("_")
    try:
        weight = float(parts[1])
        set_num = int(parts[2])
        target_reps = int(parts[3])
    except (IndexError, ValueError):
        await query.message.reply_text("⚠️ Ошибка, попробуйте снова.")
        return WRK_WEIGHT

    context.user_data["temp_weight"] = weight
    await query.edit_message_text(
        f"<b>Подход {set_num}</b> — вес: <b>{weight} кг</b>\n"
        f"Сколько повторений сделали? (цель: {target_reps})",
        reply_markup=reps_keyboard(target_reps, set_num),
        parse_mode="HTML"
    )
    return WRK_REPS


async def weight_text_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите число (например: 20 или 22.5):")
        return WRK_WEIGHT
    set_num = context.user_data["current_set_num"]
    target_reps = context.user_data["temp_target_reps"]
    context.user_data["temp_weight"] = weight
    await update.message.reply_text(
        f"<b>Подход {set_num}</b> — вес: <b>{weight} кг</b>\n"
        f"Сколько повторений? (цель: {target_reps})",
        reply_markup=reps_keyboard(target_reps, set_num),
        parse_mode="HTML"
    )
    return WRK_REPS


async def reps_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_workout":
        return await cancel_workout(update, context)

    parts = query.data.split("_")
    try:
        reps = int(parts[1])
        set_num = int(parts[2])
    except (IndexError, ValueError):
        await query.message.reply_text("⚠️ Ошибка, попробуйте снова.")
        return WRK_REPS

    weight = context.user_data["temp_weight"]
    rest_s = context.user_data["temp_rest_s"]
    session_id = context.user_data["session_id"]
    ex = context.user_data["current_exercise"]

    await db.save_set(session_id, ex["id"], ex["name"], set_num, weight, reps)
    context.user_data["current_sets"].append(
        {"set": set_num, "weight_kg": weight, "reps": reps})

    await query.edit_message_text(
        f"✅ Подход {set_num}: <b>{weight} кг × {reps} повт.</b>",
        parse_mode="HTML"
    )

    total_sets = ex["default_sets"]
    if set_num < total_sets:
        context.user_data["current_set_num"] = set_num + 1
        mins, secs = divmod(rest_s, 60)
        time_str = f"{mins}:{secs:02d}" if mins else f"{secs} сек"
        await send(update, context,
                   f"⏱ Отдых: <b>{time_str}</b>",
                   reply_markup=InlineKeyboardMarkup([[
                       InlineKeyboardButton("⏭ Пропустить", callback_data="skip_rest")
                   ]]))
        return WRK_REST
    else:
        return await finish_exercise(update, context)


async def skip_rest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏭ Отдых пропущен.")
    return await ask_weight(update, context)


async def finish_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sets_data = context.user_data["current_sets"]
    ex = context.user_data["current_exercise"]
    current = context.user_data["current_weight"]
    available = context.user_data["available"]
    use_progress = context.user_data["use_progress"]

    if use_progress and len(sets_data) >= MIN_SETS_FOR_PROGRESS and available:
        set3_reps = sets_data[2]["reps"]
        set4_reps = sets_data[3]["reps"]
        set4_weight = sets_data[3]["weight_kg"]
        new_weight = compute_new_weight(current, available, set3_reps, set4_reps, set4_weight)
    else:
        new_weight = current

    if new_weight != current:
        direction = "увеличен" if new_weight > current else "уменьшен"
        emoji = "📈" if new_weight > current else "📉"
        context.user_data["pending_new_weight"] = new_weight
        await send(update, context,
                   f"{emoji} На следующей тренировке вес будет <b>{direction}</b>: "
                   f"{current} → <b>{new_weight} кг</b>\n\nПодтверждаете?",
                   reply_markup=InlineKeyboardMarkup([[
                       InlineKeyboardButton("✅ Подтверждаю", callback_data="wchange_yes"),
                       InlineKeyboardButton("❌ Оставить", callback_data="wchange_no"),
                   ]]))
        return WRK_CONFIRM_CHANGE

    await _apply_exercise_result(update, context, current)
    return WRK_WEIGHT


async def confirm_weight_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    current = context.user_data["current_weight"]
    pending = context.user_data.pop("pending_new_weight", current)
    new_weight = pending if query.data == "wchange_yes" else current

    if query.data == "wchange_yes":
        direction = "увеличен" if new_weight > current else "уменьшен"
        await query.edit_message_text(
            f"✅ Вес будет <b>{direction}</b>: <b>{new_weight} кг</b>",
            parse_mode="HTML")
    else:
        await query.edit_message_text(
            f"↩️ Вес сохранён: <b>{current} кг</b>", parse_mode="HTML")

    await _apply_exercise_result(update, context, new_weight)
    return WRK_WEIGHT


async def _apply_exercise_result(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  new_weight: float):
    ex = context.user_data["current_exercise"]
    user_id = context.user_data["user_id"]
    session_id = context.user_data["session_id"]

    await db.set_weight(user_id, ex["id"], new_weight)
    await db.update_set_new_max(session_id, ex["id"], new_weight)

    context.user_data["exercise_index"] += 1
    context.user_data["current_sets"] = []
    context.user_data["current_set_num"] = 1
    await start_exercise(update, context)


async def finish_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_id = context.user_data.get("session_id")
    if session_id:
        await db.finish_session(session_id, "completed")
    await send(update, context,
               "🎉 <b>Тренировка завершена!</b>\n\n📊 Все данные сохранены.",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("🏋️ Начать новую", callback_data="go_start")
               ]]))
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_id = context.user_data.get("session_id")
    if session_id:
        await db.finish_session(session_id, "cancelled")
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "❌ Тренировка отменена.\n📊 Выполненные упражнения сохранены.")
    await send(update, context, "Вернуться на главную?",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("🏠 Главная", callback_data="go_start")
               ]]))
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================
# ЭКСПОРТ
# ============================================================

async def export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    await send(update, context,
               "📤 Выберите формат экспорта:",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("JSON", callback_data="export_json"),
                   InlineKeyboardButton("CSV", callback_data="export_csv"),
               ]]))
    return EXPORT_FORMAT


async def export_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = await db.export_user_data(user_id)
    fmt = query.data.split("_")[1]

    if fmt == "json":
        buf = io.BytesIO(
            json.dumps(data, ensure_ascii=False, indent=2, default=str).encode())
        buf.name = f"training_{user_id}.json"
        await query.message.reply_document(document=buf, filename=buf.name)
    else:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["date", "workout", "exercise", "set", "weight_kg", "reps", "new_max_kg"])
        for sess in data.get("workout_session", []):
            for s in (sess.get("sets") or []):
                writer.writerow([
                    str(sess.get("started_at", ""))[:10],
                    sess.get("template_name", ""),
                    s.get("exercise", ""),
                    s.get("set", ""),
                    s.get("weight_kg", ""),
                    s.get("reps", ""),
                    s.get("new_max_kg", ""),
                ])
        buf = io.BytesIO(out.getvalue().encode("utf-8-sig"))
        buf.name = f"training_{user_id}.csv"
        await query.message.reply_document(document=buf, filename=buf.name)

    return ConversationHandler.END


# ============================================================
# ОТМЕНА / ОШИБКИ
# ============================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await send(update, context, "❌ Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__))
    print(f"❌ Ошибка:\n{tb}")
    if isinstance(update, Update) and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Произошла внутренняя ошибка. Попробуйте /start"
        )


# ============================================================
# ЗАПУСК
# ============================================================

async def post_init(application: Application):
    await db.init_db()
    print("✅ БД инициализирована")


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("new", new_workout_start),
            CommandHandler("export", export_start),
        ],
        states={
            CRT_NAME:                 [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_name)],
            CRT_PROGRESS:             [CallbackQueryHandler(crt_progress, pattern="^prog_")],
            CRT_EX_NAME:              [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_name)],
            CRT_EX_EQUIP:             [CallbackQueryHandler(crt_ex_equip, pattern="^eq_")],
            CRT_EX_EQUIP_NEW_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_equip_new_name)],
            CRT_EX_EQUIP_NEW_WEIGHTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_equip_new_weights)],
            CRT_EX_SETS:              [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_sets)],
            CRT_EX_REPS:              [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_reps)],
            CRT_EX_REST:              [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_rest)],
            CRT_EX_COMMENT:           [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_comment)],
            CRT_EX_MORE:              [CallbackQueryHandler(crt_ex_more, pattern="^ex_")],
            WRK_SELECT: [
                CallbackQueryHandler(select_template,   pattern="^tmpl_"),
                CallbackQueryHandler(new_workout_start, pattern="^new_workout$"),
                CallbackQueryHandler(start,             pattern="^go_start$"),
                CallbackQueryHandler(export_start,      pattern="^export$"),
            ],
            WRK_WEIGHT: [
                CallbackQueryHandler(weight_selected,   pattern="^w_"),
                CallbackQueryHandler(weight_selected,   pattern="^wall_"),
                CallbackQueryHandler(cancel_workout,    pattern="^cancel_workout$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, weight_text_entered),
            ],
            WRK_REPS: [
                CallbackQueryHandler(reps_entered,      pattern="^r_"),
                CallbackQueryHandler(cancel_workout,    pattern="^cancel_workout$"),
            ],
            WRK_REST: [
                CallbackQueryHandler(skip_rest,         pattern="^skip_rest$"),
            ],
            WRK_CONFIRM_CHANGE: [
                CallbackQueryHandler(confirm_weight_change, pattern="^wchange_"),
            ],
            EXPORT_FORMAT: [
                CallbackQueryHandler(export_do,         pattern="^export_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)
    print("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
