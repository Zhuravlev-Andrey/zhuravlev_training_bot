"""
bot.py — Telegram-бот для силовых тренировок.
"""

import os
import traceback
from datetime import datetime, timezone
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

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("❌ TOKEN не задан")

TZ = ZoneInfo(os.environ.get("TZ", DEFAULT_TZ))

# ============================================================
# СОСТОЯНИЯ FSM
# ============================================================

(
    CRT_NAME, CRT_PROGRESS, CRT_EX_NAME, CRT_EX_EQUIP,
    CRT_EX_EQUIP_NEW_NAME, CRT_EX_EQUIP_NEW_WEIGHTS,
    CRT_EX_SETS, CRT_EX_REPS, CRT_EX_REST, CRT_EX_COMMENT, CRT_EX_MORE,
) = range(11)

(
    WRK_SELECT,
    WRK_EX_SELECT,
    WRK_WEIGHT,
    WRK_REPS,
    WRK_REST,
    WRK_CONFIRM_CHANGE,
    WRK_FIRST_WEIGHT,
    WRK_EX_COMMENT,
    WRK_FINISH_CONFIRM,
    EDIT_SELECT,
    EDIT_MENU,
    EDIT_EX_LIST,
) = range(11, 23)

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def sets_keyboard(default: int) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(f"{'→ ' if i == default else ''}{i}", callback_data=f"sets_{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(f"{'→ ' if i == default else ''}{i}", callback_data=f"sets_{i}") for i in range(6, 11)]
    return InlineKeyboardMarkup([row1, row2])

def reps_setup_keyboard(default: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i in range(1, 21):
        row.append(InlineKeyboardButton(f"{'→ ' if i == default else ''}{i}", callback_data=f"reps_{i}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def rest_keyboard(default: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{'→ ' if s == default else ''}{s} сек", callback_data=f"rest_{s}")
        for s in [30, 60, 90, 120]
    ]])

def weight_keyboard(available: list, recommended: float, set_num: int, target_reps: int) -> InlineKeyboardMarkup:
    try: idx = available.index(recommended)
    except ValueError: idx = 0
    subset = available[max(0, idx - WEIGHT_KEYBOARD_RANGE):min(len(available), idx + WEIGHT_KEYBOARD_RANGE + 1)]
    keyboard, row = [], []
    for w in subset:
        row.append(InlineKeyboardButton(f"{'→ ' if w == recommended else ''}{w}", callback_data=f"w_{w}_{set_num}_{target_reps}"))
        if len(row) == 3: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("📋 Все веса", callback_data=f"wall_{set_num}_{target_reps}")])
    keyboard.append([InlineKeyboardButton("↩️ К упражнениям", callback_data="back_to_exlist"),
                     InlineKeyboardButton("❌ Отменить", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)

def all_weights_keyboard(available: list, set_num: int, target_reps: int) -> InlineKeyboardMarkup:
    keyboard, row = [], []
    for w in available:
        row.append(InlineKeyboardButton(str(w), callback_data=f"w_{w}_{set_num}_{target_reps}"))
        if len(row) == 3: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("↩️ К упражнениям", callback_data="back_to_exlist"),
                     InlineKeyboardButton("❌ Отменить", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)

def reps_keyboard(target: int, set_num: int) -> InlineKeyboardMarkup:
    keyboard, row = [], []
    for r in range(REPS_MIN, REPS_MAX + 1):
        row.append(InlineKeyboardButton(f"{'→ ' if r == target else ''}{r}", callback_data=f"r_{r}_{set_num}"))
        if len(row) == 4: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("↩️ К упражнениям", callback_data="back_to_exlist"),
                     InlineKeyboardButton("❌ Отменить", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)

def yn_keyboard(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да", callback_data=yes_data),
        InlineKeyboardButton("❌ Нет", callback_data=no_data),
    ]])

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_closest_weight(available: list, target: float) -> float:
    return min(available, key=lambda w: abs(w - target))

def compute_new_weight(current: float, available: list, set3_reps: int, set4_reps: int, set4_weight: float) -> float:
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

def get_set_params(ex: dict, set_overrides: dict, set_num: int) -> tuple:
    ov = set_overrides.get(set_num, {})
    return (
        ov.get("reps") or ex["default_reps"],
        ov.get("rest_s") or ex["default_rest_s"],
        ov.get("weight_pct") or 1.0,
    )

async def send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    kwargs = dict(text=text, reply_markup=reply_markup, parse_mode="HTML")
    if update.callback_query:
        await update.callback_query.message.reply_text(**kwargs)
    elif update.message:
        await update.message.reply_text(**kwargs)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, **kwargs)

async def build_month_history(user_id: int) -> str:
    sessions = await db.get_month_session(user_id)
    if not sessions:
        return "📅 В этом месяце тренировок ещё не было."
    lines = ["📅 <b>Тренировки в этом месяце:</b>"]
    icons = {"completed": "✅", "cancelled": "❌", "active": "🔄"}
    for i, s in enumerate(sessions, 1):
        dt = s["started_at"].astimezone(TZ).strftime("%d.%m %H:%M")
        lines.append(f"{i}. {icons.get(s['status'], '•')} {dt} — {s['template_name']}")
    return "\n".join(lines)

# ============================================================
# ГЛАВНЫЙ ЭКРАН
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    user_id = update.effective_user.id
    templates = await db.get_user_workout_templates(user_id)
    history = await build_month_history(user_id)

    if not templates:
        await send(update, context,
                   f"{history}\n\nУ вас ещё нет тренировок. Давайте создадим первую! 💪",
                   reply_markup=InlineKeyboardMarkup([[
                       InlineKeyboardButton("➕ Создать тренировку", callback_data="new_workout")
                   ]]))
        return WRK_SELECT

    buttons = [[InlineKeyboardButton(t["name"], callback_data=f"tmpl_{t['id']}")] for t in templates]
    buttons.append([
        InlineKeyboardButton("➕ Создать", callback_data="new_workout"),
        InlineKeyboardButton("✏️ Редактировать", callback_data="edit_workout"),
    ])
    await send(update, context, f"{history}\n\nВыберите тренировку:",
               reply_markup=InlineKeyboardMarkup(buttons))
    return WRK_SELECT

# ============================================================
# РЕДАКТИРОВАНИЕ
# ============================================================

async def edit_workout_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    templates = await db.get_user_workout_templates(user_id)
    buttons = [[InlineKeyboardButton(t["name"], callback_data=f"edit_{t['id']}")] for t in templates]
    buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="go_start")])
    await query.message.reply_text("Выберите тренировку для редактирования:",
                                   reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_SELECT

async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    template_id = int(query.data.split("_")[1])
    user_id = update.effective_user.id
    template = await db.get_workout_template(template_id, user_id)
    if not template:
        await query.message.reply_text("⚠️ Тренировка не найдена.")
        return EDIT_SELECT
    exercises = await db.get_exercise_templates(template_id)
    ex_list = "\n".join(f"{i}. {e['name']}" for i, e in enumerate(exercises, 1)) or "нет упражнений"
    context.user_data["edit_template_id"] = template_id
    await query.message.reply_text(
        f"✏️ <b>{template['name']}</b>\n\nУпражнения:\n{ex_list}\n\n"
        f"Прогрессия: {'включена ✅' if template['use_weight_progress'] else 'выключена ❌'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить упражнение", callback_data="edit_add_ex")],
            [InlineKeyboardButton("🔀 Удалить / переместить", callback_data="edit_ex_list")],
            [InlineKeyboardButton("🗑 Удалить тренировку", callback_data="edit_delete")],
            [InlineKeyboardButton("↩️ Назад", callback_data="go_start")],
        ])
    )
    return EDIT_MENU

async def edit_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "go_start":
        return await start(update, context)
    if query.data == "edit_add_ex":
        context.user_data["new_wt"] = {"exercises": [], "edit_mode": True,
                                        "template_id": context.user_data["edit_template_id"]}
        await query.message.reply_text("Введите название упражнения:")
        return CRT_EX_NAME
    if query.data == "edit_ex_list":
        return await show_edit_ex_list(update, context)
    if query.data == "edit_delete":
        await db.delete_workout_template(context.user_data["edit_template_id"])
        await query.message.reply_text("🗑 Тренировка удалена.")
        return await start(update, context)
    return EDIT_MENU

async def show_edit_ex_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    template_id = context.user_data["edit_template_id"]
    exercises = await db.get_exercise_templates(template_id)
    if not exercises:
        await send(update, context, "В тренировке нет упражнений.")
        return EDIT_MENU
    buttons = []
    for ex in exercises:
        buttons.append([
            InlineKeyboardButton(ex["name"], callback_data=f"exinfo_{ex['id']}"),
            InlineKeyboardButton("⬆️", callback_data=f"exup_{ex['id']}"),
            InlineKeyboardButton("⬇️", callback_data=f"exdown_{ex['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"exdel_{ex['id']}"),
        ])
    buttons.append([InlineKeyboardButton("✅ Готово", callback_data="edit_done")])
    await send(update, context, "⬆️⬇️ — порядок  🗑 — удалить",
               reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_EX_LIST

async def edit_ex_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data in ("edit_done", "go_start"):
        return await start(update, context)

    # Определяем действие и id по последнему _ в строке
    last_sep = query.data.rfind("_")
    action = query.data[:last_sep]
    ex_id = int(query.data[last_sep + 1:])
    template_id = context.user_data["edit_template_id"]
    exercises = await db.get_exercise_templates(template_id)
    ids = [e["id"] for e in exercises]

    if action == "exdel":
        await db.delete_exercise_template(ex_id)
        remaining = [e for e in exercises if e["id"] != ex_id]
        for i, ex in enumerate(remaining, 1):
            await db.update_exercise_order(ex["id"], i)
    elif action == "exup":
        idx = ids.index(ex_id)
        if idx > 0:
            # Используем временное значение чтобы избежать конфликта UNIQUE
            await db.update_exercise_order(ex_id, -1)
            await db.update_exercise_order(ids[idx - 1], exercises[idx]["order_index"])
            await db.update_exercise_order(ex_id, exercises[idx - 1]["order_index"])
    elif action == "exdown":
        idx = ids.index(ex_id)
        if idx < len(ids) - 1:
            await db.update_exercise_order(ex_id, -1)
            await db.update_exercise_order(ids[idx + 1], exercises[idx]["order_index"])
            await db.update_exercise_order(ex_id, exercises[idx + 1]["order_index"])

    return await show_edit_ex_list(update, context)

# ============================================================
# СОЗДАНИЕ ТРЕНИРОВКИ
# ============================================================

async def new_workout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["new_wt"] = {"exercises": []}
    await send(update, context, "📝 <b>Создание тренировки</b>\n\nВведите название тренировки:")
    return CRT_NAME

async def crt_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return CRT_NAME
    context.user_data["new_wt"]["name"] = name
    await update.message.reply_text(
        f"Тренировка «<b>{name}</b>»\n\n"
        "Включить <b>прогрессию весов</b>?\n\n"
        "• 3-й подход <b>&lt; 10 повт.</b> → вес снижается на одну ступень\n"
        "• Последний подход <b>= 10 повт. с рабочим весом</b> → вес повышается на одну ступень\n"
        "• Иначе → вес остаётся\n\n"
        "Ступени = доступные веса тренажёра. Изменение требует подтверждения.",
        parse_mode="HTML",
        reply_markup=yn_keyboard("prog_yes", "prog_no")
    )
    return CRT_PROGRESS

async def crt_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_wt"]["use_weight_progress"] = (query.data == "prog_yes")
    await query.message.reply_text("✅ Сохранено.\n\n<b>Введите название первого упражнения:</b>",
                                   parse_mode="HTML")
    return CRT_EX_NAME

async def crt_ex_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data["new_ex"] = {"name": name}
    equipment = await db.get_all_equipment()
    buttons = [[InlineKeyboardButton(e["name"], callback_data=f"eq_{e['id']}")] for e in equipment]
    buttons.append([InlineKeyboardButton("➕ Новый тренажёр", callback_data="eq_new")])
    buttons.append([InlineKeyboardButton("— Без тренажёра", callback_data="eq_none")])
    await update.message.reply_text(f"Упражнение: <b>{name}</b>\n\nВыберите тренажёр:",
                                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
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
    await query.message.reply_text(f"Сколько подходов? (по умолчанию {DEFAULT_SETS})",
                                   reply_markup=sets_keyboard(DEFAULT_SETS))
    return CRT_EX_SETS

async def crt_ex_equip_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_eq_name"] = update.message.text.strip()
    await update.message.reply_text("Введите веса через запятую (кг).\nПример: <code>10, 15, 20, 25</code>",
                                    parse_mode="HTML")
    return CRT_EX_EQUIP_NEW_WEIGHTS

async def crt_ex_equip_new_weights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weights = sorted([float(w.strip()) for w in update.message.text.split(",")])
        if not weights: raise ValueError
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите числа через запятую:")
        return CRT_EX_EQUIP_NEW_WEIGHTS
    eq_id = await db.create_equipment(context.user_data["new_eq_name"], weights)
    context.user_data["new_ex"]["equipment_id"] = eq_id
    context.user_data["new_ex"]["available_weight"] = weights
    await update.message.reply_text(f"✅ Тренажёр создан.\n\nСколько подходов? (по умолчанию {DEFAULT_SETS})",
                                    reply_markup=sets_keyboard(DEFAULT_SETS))
    return CRT_EX_SETS

async def crt_ex_sets_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sets = int(query.data.split("_")[1])
    context.user_data["new_ex"]["sets"] = sets
    await query.message.reply_text(f"✅ Подходов: {sets}\n\nСколько повторений? (по умолчанию {DEFAULT_REPS})",
                                   reply_markup=reps_setup_keyboard(DEFAULT_REPS))
    return CRT_EX_REPS

async def crt_ex_sets_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sets = int(update.message.text.strip())
        if not 1 <= sets <= 10: raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число от 1 до 10 или выберите кнопкой:",
                                        reply_markup=sets_keyboard(DEFAULT_SETS))
        return CRT_EX_SETS
    context.user_data["new_ex"]["sets"] = sets
    await update.message.reply_text(f"✅ Подходов: {sets}\n\nСколько повторений? (по умолчанию {DEFAULT_REPS})",
                                    reply_markup=reps_setup_keyboard(DEFAULT_REPS))
    return CRT_EX_REPS

async def crt_ex_reps_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    reps = int(query.data.split("_")[1])
    context.user_data["new_ex"]["reps"] = reps
    await query.message.reply_text(f"✅ Повторений: {reps}\n\nВремя отдыха:",
                                   reply_markup=rest_keyboard(DEFAULT_REST_S))
    return CRT_EX_REST

async def crt_ex_reps_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reps = int(update.message.text.strip())
        if not 1 <= reps <= 50: raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число от 1 до 50 или выберите кнопкой:",
                                        reply_markup=reps_setup_keyboard(DEFAULT_REPS))
        return CRT_EX_REPS
    context.user_data["new_ex"]["reps"] = reps
    await update.message.reply_text(f"✅ Повторений: {reps}\n\nВремя отдыха:",
                                    reply_markup=rest_keyboard(DEFAULT_REST_S))
    return CRT_EX_REST

async def crt_ex_rest_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rest = int(query.data.split("_")[1])
    context.user_data["new_ex"]["rest"] = rest
    await query.message.reply_text(
        f"✅ Отдых: {rest} сек\n\nКомментарий к упражнению (необязательно):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data="comment_skip")]]))
    return CRT_EX_COMMENT

async def crt_ex_rest_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rest = int(update.message.text.strip())
        if not 0 <= rest <= 600: raise ValueError
    except ValueError:
        await update.message.reply_text("Введите число от 0 до 600 или выберите кнопкой:",
                                        reply_markup=rest_keyboard(DEFAULT_REST_S))
        return CRT_EX_REST
    context.user_data["new_ex"]["rest"] = rest
    await update.message.reply_text(
        f"✅ Отдых: {rest} сек\n\nКомментарий (необязательно):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data="comment_skip")]]))
    return CRT_EX_COMMENT

async def crt_ex_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ex"]["comment"] = update.message.text.strip()
    return await _finish_exercise_creation(update, context)

async def crt_ex_comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_ex"]["comment"] = None
    return await _finish_exercise_creation(update, context)

async def _finish_exercise_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wt = context.user_data["new_wt"]
    wt["exercises"].append(context.user_data.pop("new_ex"))
    n = len(wt["exercises"])
    if wt.get("edit_mode"):
        ex = wt["exercises"][-1]
        exercises = await db.get_exercise_templates(wt["template_id"])
        await db.create_exercise_template(
            workout_id=wt["template_id"], equipment_id=ex.get("equipment_id"),
            name=ex["name"], order_index=len(exercises) + 1,
            default_sets=ex["sets"], default_reps=ex["reps"],
            default_rest_s=ex["rest"], comment=ex.get("comment"),
        )
        await send(update, context, f"✅ Упражнение «{ex['name']}» добавлено.",
                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ На главную", callback_data="go_start")]]))
        context.user_data.pop("new_wt", None)
        return WRK_SELECT
    await send(update, context, f"✅ Упражнение {n} добавлено.",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("➕ Ещё упражнение", callback_data="ex_more"),
                   InlineKeyboardButton("✅ Завершить", callback_data="ex_done"),
               ]]))
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
    template_id = await db.create_workout_template(user_id, data["name"], data["use_weight_progress"])
    for i, ex in enumerate(data["exercises"], 1):
        await db.create_exercise_template(
            workout_id=template_id, equipment_id=ex.get("equipment_id"),
            name=ex["name"], order_index=i, default_sets=ex["sets"],
            default_reps=ex["reps"], default_rest_s=ex["rest"], comment=ex.get("comment"),
        )
    await send(update, context,
               f"🎉 Тренировка «<b>{data['name']}</b>» создана!\n"
               f"Упражнений: {len(data['exercises'])}\n"
               f"Прогрессия: {'✅' if data['use_weight_progress'] else '❌'}\n\nНачать сейчас?",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("▶️ Начать", callback_data=f"tmpl_{template_id}"),
                   InlineKeyboardButton("↩️ На главную", callback_data="go_start"),
               ]]))
    return WRK_SELECT

# ============================================================
# ВЫБОР ТРЕНИРОВКИ
# ============================================================

async def select_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "go_start": return await start(update, context)
    if query.data == "new_workout": return await new_workout_start(update, context)
    if query.data == "edit_workout": return await edit_workout_list(update, context)

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
        "session_start":  datetime.now(timezone.utc),
        "template":       dict(template),
        "exercises":      [dict(e) for e in exercises],
        # ex_id -> {"sets": [...], "done": bool}
        "ex_state":       {e["id"]: {"sets": [], "done": False} for e in exercises},
        "use_progress":   template["use_weight_progress"],
    })
    await query.edit_message_text(f"▶️ <b>{template['name']}</b>", parse_mode="HTML")
    return await show_exercise_select(update, context)

# ============================================================
# СПИСОК УПРАЖНЕНИЙ
# ============================================================

async def show_exercise_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exercises = context.user_data["exercises"]
    ex_state = context.user_data["ex_state"]

    lines = ["<b>Упражнения тренировки:</b>"]
    buttons = []
    all_done = True
    for ex in exercises:
        state = ex_state[ex["id"]]
        done = state["done"]
        sets_done = len(state["sets"])
        if not done:
            all_done = False
        if done:
            label = f"✅ {ex['name']}"
        elif sets_done > 0:
            label = f"🔄 {ex['name']} ({sets_done} п.)"
        else:
            label = f"⬜ {ex['name']}"
        lines.append(f"• {label}")
        buttons.append([InlineKeyboardButton(label, callback_data=f"doex_{ex['id']}")])

    if all_done:
        return await ask_finish_workout(update, context)

    buttons.append([InlineKeyboardButton("🏁 Завершить тренировку", callback_data="ask_finish")])
    await send(update, context, "\n".join(lines) + "\n\nВыберите упражнение:",
               reply_markup=InlineKeyboardMarkup(buttons))
    return WRK_EX_SELECT

async def exercise_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_workout": return await cancel_workout(update, context)
    if query.data == "ask_finish": return await ask_finish_workout(update, context)

    ex_id = int(query.data.split("_")[1])
    exercises = context.user_data["exercises"]
    ex = next((e for e in exercises if e["id"] == ex_id), None)
    if not ex:
        await query.message.reply_text("⚠️ Упражнение не найдено.")
        return WRK_EX_SELECT

    context.user_data["current_ex_id"] = ex_id
    return await start_exercise(update, context)

# ============================================================
# ВЫПОЛНЕНИЕ УПРАЖНЕНИЯ
# ============================================================

async def start_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex_id = context.user_data["current_ex_id"]
    exercises = context.user_data["exercises"]
    ex = next(e for e in exercises if e["id"] == ex_id)
    ex_state = context.user_data["ex_state"][ex_id]

    user_id = context.user_data["user_id"]
    available = list(ex["available_weight"]) if ex["available_weight"] else []

    set_overrides_raw = await db.get_set_templates(ex["id"])
    set_overrides = {r["set_number"]: dict(r) for r in set_overrides_raw}
    existing_weight = await db.get_existing_weight(user_id, ex["id"])

    # Следующий номер подхода — продолжаем с того где остановились
    next_set_num = len(ex_state["sets"]) + 1

    context.user_data.update({
        "current_exercise": ex,
        "available":        available,
        "set_overrides":    set_overrides,
        "current_weight":   existing_weight or 0.0,
        "current_set_num":  next_set_num,
    })

    total = len(exercises)
    idx = next(i for i, e in enumerate(exercises) if e["id"] == ex_id)
    comment_str = f"\n💬 {ex['comment']}" if ex.get("comment") else ""
    last = await db.get_last_session_for_template(user_id, context.user_data["template"]["id"])
    last_str = f"\n📅 Последнее: {last['started_at'].astimezone(TZ).strftime('%d.%m.%Y')}" if last else ""

    # П.4: первое выполнение — просто просим ввести вес без рекомендаций
    if existing_weight is None:
        await send(update, context,
                   f"📋 <b>Упражнение {idx+1}/{total}: {ex['name']}</b>{comment_str}\n\n"
                   "Первое выполнение.\n"
                   "Введите вес для <b>подхода 1</b> (кг):" +
                   ("\n\nДоступные веса тренажёра:" if available else ""),
                   reply_markup=InlineKeyboardMarkup(
                       [[InlineKeyboardButton(str(w), callback_data=f"fw_{w}") for w in available[i:i+3]]
                        for i in range(0, min(len(available), 18), 3)] +
                       [[InlineKeyboardButton("↩️ К упражнениям", callback_data="back_to_exlist")]]
                   ) if available else InlineKeyboardMarkup([[
                       InlineKeyboardButton("↩️ К упражнениям", callback_data="back_to_exlist")
                   ]]))
        return WRK_FIRST_WEIGHT

    sets_info = ""
    if ex_state["sets"]:
        sets_info = "\n" + "\n".join(
            f"  Подход {s['set']}: {s['weight_kg']} кг × {s['reps']} повт."
            for s in ex_state["sets"]
        )

    await send(update, context,
               f"📋 <b>Упражнение {idx+1}/{total}: {ex['name']}</b>"
               f"{comment_str}{last_str}\n"
               f"🎯 Рабочий вес: <b>{existing_weight} кг</b>"
               f"{sets_info}")
    return await ask_weight(update, context)

async def first_weight_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "back_to_exlist": return await show_exercise_select(update, context)
        if query.data == "cancel_workout": return await cancel_workout(update, context)
        try: weight = float(query.data.split("_")[1])
        except (IndexError, ValueError):
            await query.message.reply_text("⚠️ Ошибка, попробуйте снова.")
            return WRK_FIRST_WEIGHT
    else:
        try: weight = float(update.message.text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите число (например: 20 или 22.5):")
            return WRK_FIRST_WEIGHT

    ex = context.user_data["current_exercise"]
    user_id = context.user_data["user_id"]
    await db.set_weight(user_id, ex["id"], weight)
    context.user_data["current_weight"] = weight
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

    if not available:
        await send(update, context,
                   f"<b>Подход {set_num}</b> — цель: {target_reps} повт.\nВведите вес (кг):")
        return WRK_WEIGHT

    recommended = get_closest_weight(available, current * weight_pct)
    pct_label = f"~{int(weight_pct * 100)}%" if weight_pct != 1.0 else "рабочий"
    await send(update, context,
               f"<b>Подход {set_num}</b> — цель: {target_reps} повт. ({pct_label})\n"
               f"💡 Рекомендуемый вес: <b>{recommended} кг</b>",
               reply_markup=weight_keyboard(available, recommended, set_num, target_reps))
    return WRK_WEIGHT

async def weight_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_workout": return await cancel_workout(update, context)
    if query.data == "back_to_exlist": return await show_exercise_select(update, context)
    if query.data.startswith("wall_"):
        parts = query.data.split("_")
        await query.edit_message_reply_markup(
            reply_markup=all_weights_keyboard(context.user_data["available"], int(parts[1]), int(parts[2])))
        return WRK_WEIGHT
    parts = query.data.split("_")
    try:
        weight, set_num, target_reps = float(parts[1]), int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await query.message.reply_text("⚠️ Ошибка, попробуйте снова.")
        return WRK_WEIGHT
    context.user_data["temp_weight"] = weight
    await query.edit_message_text(
        f"<b>Подход {set_num}</b> — вес: <b>{weight} кг</b>\n"
        f"Сколько повторений? (цель: {target_reps})",
        reply_markup=reps_keyboard(target_reps, set_num), parse_mode="HTML")
    return WRK_REPS

async def weight_text_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: weight = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите число (например: 20 или 22.5):")
        return WRK_WEIGHT
    set_num = context.user_data["current_set_num"]
    target_reps = context.user_data["temp_target_reps"]
    context.user_data["temp_weight"] = weight
    await update.message.reply_text(
        f"<b>Подход {set_num}</b> — вес: <b>{weight} кг</b>\n"
        f"Сколько повторений? (цель: {target_reps})",
        reply_markup=reps_keyboard(target_reps, set_num), parse_mode="HTML")
    return WRK_REPS

async def reps_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_workout": return await cancel_workout(update, context)
    if query.data == "back_to_exlist": return await show_exercise_select(update, context)
    parts = query.data.split("_")
    try: reps, set_num = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        await query.message.reply_text("⚠️ Ошибка, попробуйте снова.")
        return WRK_REPS

    weight = context.user_data["temp_weight"]
    rest_s = context.user_data["temp_rest_s"]
    session_id = context.user_data["session_id"]
    ex = context.user_data["current_exercise"]
    ex_id = ex["id"]

    await db.save_set(session_id, ex_id, ex["name"], set_num, weight, reps)

    # Сохраняем в памяти
    context.user_data["ex_state"][ex_id]["sets"].append(
        {"set": set_num, "weight_kg": weight, "reps": reps})

    await query.edit_message_text(
        f"✅ Подход {set_num}: <b>{weight} кг × {reps} повт.</b>", parse_mode="HTML")

    total_sets = ex["default_sets"]
    # П.6: после последнего запланированного подхода — предлагаем добавить ещё или завершить
    if set_num >= total_sets:
        context.user_data["current_set_num"] = set_num + 1
        await send(update, context,
                   f"Выполнено {set_num} из {total_sets} подходов.\nДобавить ещё один подход или завершить упражнение?",
                   reply_markup=InlineKeyboardMarkup([[
                       InlineKeyboardButton("➕ Ещё подход", callback_data="add_set"),
                       InlineKeyboardButton("✅ Завершить упражнение", callback_data="finish_ex"),
                   ]]))
        return WRK_REST
    else:
        # Отдых между подходами
        context.user_data["current_set_num"] = set_num + 1
        mins, secs = divmod(rest_s, 60)
        time_str = f"{mins}:{secs:02d}" if mins else f"{secs} сек"
        await send(update, context, f"⏱ Отдых: <b>{time_str}</b>",
                   reply_markup=InlineKeyboardMarkup([[
                       InlineKeyboardButton("▶️ Следующий подход", callback_data="skip_rest"),
                       InlineKeyboardButton("↩️ К упражнениям", callback_data="back_to_exlist"),
                   ]]))
        return WRK_REST

async def skip_rest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_to_exlist": return await show_exercise_select(update, context)
    if query.data == "add_set":
        # П.6 — добавляем внеплановый подход
        await query.edit_message_text("➕ Дополнительный подход.")
        return await ask_weight(update, context)
    if query.data == "finish_ex":
        await query.edit_message_text("✅ Упражнение завершено.")
        return await finish_exercise(update, context)
    # skip_rest — обычный пропуск отдыха
    await query.edit_message_text("▶️ Начинаем следующий подход.")
    return await ask_weight(update, context)

async def finish_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex = context.user_data["current_exercise"]
    ex_id = ex["id"]
    ex_state = context.user_data["ex_state"][ex_id]
    sets_data = ex_state["sets"]
    current = context.user_data["current_weight"]
    available = context.user_data["available"]
    use_progress = context.user_data["use_progress"]

    if use_progress and len(sets_data) >= MIN_SETS_FOR_PROGRESS and available:
        new_weight = compute_new_weight(
            current, available,
            sets_data[2]["reps"], sets_data[3]["reps"], sets_data[3]["weight_kg"]
        )
    else:
        new_weight = current

    context.user_data["pending_new_weight"] = new_weight

    # П.3 — комментарий после упражнения
    await send(update, context,
               f"✅ «{ex['name']}» выполнено!\n\nОставить заметку на следующий раз?",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("⏭ Без заметки", callback_data="excomment_skip")
               ]]))
    return WRK_EX_COMMENT

async def ex_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    ex = context.user_data["current_exercise"]
    await db.update_exercise_comment(ex["id"], comment)
    ex["comment"] = comment
    await update.message.reply_text(f"💬 Заметка сохранена: «{comment}»")
    return await _after_exercise_comment(update, context)

async def ex_comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _after_exercise_comment(update, context)

async def _after_exercise_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex = context.user_data["current_exercise"]
    ex_id = ex["id"]
    current = context.user_data["current_weight"]
    new_weight = context.user_data.pop("pending_new_weight", current)

    if new_weight != current:
        direction = "увеличен" if new_weight > current else "уменьшен"
        emoji = "📈" if new_weight > current else "📉"
        context.user_data["confirm_new_weight"] = new_weight
        await send(update, context,
                   f"{emoji} Вес на следующей тренировке будет <b>{direction}</b>: "
                   f"{current} → <b>{new_weight} кг</b>\n\nПодтверждаете?",
                   reply_markup=InlineKeyboardMarkup([[
                       InlineKeyboardButton("✅ Подтверждаю", callback_data="wchange_yes"),
                       InlineKeyboardButton("❌ Оставить", callback_data="wchange_no"),
                   ]]))
        return WRK_CONFIRM_CHANGE

    await _apply_exercise_result(update, context, current)
    return await show_exercise_select(update, context)

async def confirm_weight_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    current = context.user_data["current_weight"]
    new_weight = context.user_data.pop("confirm_new_weight", current)
    final = new_weight if query.data == "wchange_yes" else current
    if query.data == "wchange_yes":
        direction = "увеличен" if final > current else "уменьшен"
        await query.edit_message_text(f"✅ Вес будет <b>{direction}</b>: <b>{final} кг</b>", parse_mode="HTML")
    else:
        await query.edit_message_text(f"↩️ Вес сохранён: <b>{current} кг</b>", parse_mode="HTML")
    await _apply_exercise_result(update, context, final)
    return await show_exercise_select(update, context)

async def _apply_exercise_result(update: Update, context: ContextTypes.DEFAULT_TYPE, new_weight: float):
    ex = context.user_data["current_exercise"]
    user_id = context.user_data["user_id"]
    session_id = context.user_data["session_id"]
    await db.set_weight(user_id, ex["id"], new_weight)
    await db.update_set_new_max(session_id, ex["id"], new_weight)
    context.user_data["ex_state"][ex["id"]]["done"] = True

# ============================================================
# ЗАВЕРШЕНИЕ ТРЕНИРОВКИ (п.7 + п.10)
# ============================================================

async def ask_finish_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """П.7 — спрашиваем подтверждение завершения."""
    if update.callback_query:
        await update.callback_query.answer()
    await send(update, context,
               "🏁 Завершить тренировку?",
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("✅ Завершить", callback_data="finish_confirm"),
                   InlineKeyboardButton("↩️ Вернуться", callback_data="back_to_exlist"),
               ]]))
    return WRK_FINISH_CONFIRM

async def finish_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_to_exlist":
        return await show_exercise_select(update, context)
    return await finish_workout(update, context)

async def finish_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_id = context.user_data.get("session_id")
    if session_id:
        await db.finish_session(session_id, "completed")

    # П.10 — статистика
    start_time = context.user_data.get("session_start")
    duration_str = ""
    if start_time:
        delta = datetime.now(timezone.utc) - start_time
        mins = int(delta.total_seconds() // 60)
        duration_str = f"\n⏱ Продолжительность: <b>{mins} мин</b>"

    ex_state = context.user_data.get("ex_state", {})
    total_reps = 0
    total_volume = 0.0
    ex_lines = []
    for ex in context.user_data.get("exercises", []):
        state = ex_state.get(ex["id"], {})
        sets = state.get("sets", [])
        if not sets:
            continue
        ex_reps = sum(s["reps"] for s in sets)
        ex_vol = sum(s["weight_kg"] * s["reps"] for s in sets)
        total_reps += ex_reps
        total_volume += ex_vol
        ex_lines.append(f"  • {ex['name']}: {len(sets)} подх., {ex_reps} повт., {ex_vol:.0f} кг")

    stats = "\n".join(ex_lines) if ex_lines else "нет данных"
    summary = (
        f"🎉 <b>Тренировка завершена!</b>{duration_str}\n\n"
        f"📊 <b>Итоги:</b>\n{stats}\n\n"
        f"<b>Всего:</b> {total_reps} повт. | {total_volume:.0f} кг суммарного объёма"
    )
    context.user_data.clear()
    await send(update, context, summary,
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton("🏠 На главную", callback_data="go_start")
               ]]))
    return ConversationHandler.END

async def cancel_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_id = context.user_data.get("session_id")
    if session_id:
        await db.finish_session(session_id, "cancelled")
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "❌ Тренировка отменена. Выполненные упражнения сохранены.")
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Вернуться на главную?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главная", callback_data="go_start")
        ]])
    )
    return WRK_SELECT

# ============================================================
# СЛУЖЕБНЫЕ
# ============================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await send(update, context, "❌ Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    print(f"❌ Ошибка:\n{tb}")
    if isinstance(update, Update) and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Ошибка: <code>{str(context.error)[:300]}</code>\n\nПопробуйте /start",
            parse_mode="HTML"
        )

async def post_init(application: Application):
    await db.init_db()
    print("✅ БД инициализирована")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("new", new_workout_start),
        ],
        states={
            CRT_NAME:                 [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_name)],
            CRT_PROGRESS:             [CallbackQueryHandler(crt_progress, pattern="^prog_")],
            CRT_EX_NAME:              [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_name)],
            CRT_EX_EQUIP:             [CallbackQueryHandler(crt_ex_equip, pattern="^eq_")],
            CRT_EX_EQUIP_NEW_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_equip_new_name)],
            CRT_EX_EQUIP_NEW_WEIGHTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_equip_new_weights)],
            CRT_EX_SETS:  [CallbackQueryHandler(crt_ex_sets_btn, pattern="^sets_"),
                           MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_sets_text)],
            CRT_EX_REPS:  [CallbackQueryHandler(crt_ex_reps_btn, pattern="^reps_"),
                           MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_reps_text)],
            CRT_EX_REST:  [CallbackQueryHandler(crt_ex_rest_btn, pattern="^rest_"),
                           MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_rest_text)],
            CRT_EX_COMMENT: [CallbackQueryHandler(crt_ex_comment_skip, pattern="^comment_skip$"),
                             MessageHandler(filters.TEXT & ~filters.COMMAND, crt_ex_comment_text)],
            CRT_EX_MORE:  [CallbackQueryHandler(crt_ex_more, pattern="^ex_")],

            EDIT_SELECT:  [CallbackQueryHandler(edit_menu,      pattern=r"^edit_\d+$")],
            EDIT_MENU:    [CallbackQueryHandler(edit_action,    pattern="^(edit_add_ex|edit_ex_list|edit_delete|go_start)$")],
            EDIT_EX_LIST: [CallbackQueryHandler(edit_ex_action, pattern="^(exdel_|exup_|exdown_|exinfo_|edit_done|go_start)")],

            WRK_SELECT: [
                CallbackQueryHandler(select_template,   pattern="^tmpl_"),
                CallbackQueryHandler(select_template,   pattern="^(new_workout|go_start|edit_workout)$"),
                CallbackQueryHandler(edit_workout_list, pattern="^edit_workout$"),
            ],
            WRK_EX_SELECT: [
                CallbackQueryHandler(exercise_selected, pattern="^doex_"),
                CallbackQueryHandler(exercise_selected, pattern="^(cancel_workout|ask_finish)$"),
            ],
            WRK_FIRST_WEIGHT: [
                CallbackQueryHandler(first_weight_selected, pattern="^fw_"),
                CallbackQueryHandler(first_weight_selected, pattern="^(back_to_exlist|cancel_workout)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, first_weight_selected),
            ],
            WRK_WEIGHT: [
                CallbackQueryHandler(weight_selected, pattern="^w_"),
                CallbackQueryHandler(weight_selected, pattern="^(wall_|back_to_exlist|cancel_workout)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, weight_text_entered),
            ],
            WRK_REPS: [
                CallbackQueryHandler(reps_entered, pattern="^r_"),
                CallbackQueryHandler(reps_entered, pattern="^(back_to_exlist|cancel_workout)$"),
            ],
            WRK_REST: [
                CallbackQueryHandler(skip_rest, pattern="^(skip_rest|add_set|finish_ex|back_to_exlist)$"),
                CallbackQueryHandler(cancel_workout, pattern="^cancel_workout$"),
            ],
            WRK_EX_COMMENT: [
                CallbackQueryHandler(ex_comment_skip, pattern="^excomment_skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ex_comment_text),
            ],
            WRK_CONFIRM_CHANGE: [
                CallbackQueryHandler(confirm_weight_change, pattern="^wchange_"),
            ],
            WRK_FINISH_CONFIRM: [
                CallbackQueryHandler(finish_confirm, pattern="^(finish_confirm|back_to_exlist)$"),
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