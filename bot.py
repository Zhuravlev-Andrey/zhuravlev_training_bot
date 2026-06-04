import os
from datetime import datetime
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
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)

import db

# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("❌ Не задана переменная окружения TOKEN")

TZ = ZoneInfo("Europe/Moscow")  # поменяй на свою таймзону при необходимости

# ============================================================
# СПРАВОЧНИКИ
# ============================================================

TRAININGS = {
    "1": {
        "name": "🏋️ Грудь + Трицепс",
        "exercises": [
            {"id": "bench_press",       "name": "Жим лёжа в Смите",        "eq_id": "smith_machine"},
            {"id": "svend_press",       "name": "Жим Свенда (гантели)",     "eq_id": "dumbbells"},
            {"id": "french_press",      "name": "Французский жим лёжа",     "eq_id": "dumbbells"},
            {"id": "triceps_extension", "name": "Разгибание рук в блоке",   "eq_id": "cable_machine"},
        ],
    },
    "2": {
        "name": "🦵 Ноги + Плечи",
        "exercises": [
            {"id": "squat",                 "name": "Приседания в Смите",   "eq_id": "smith_machine"},
            {"id": "leg_press",             "name": "Жим ногами",           "eq_id": "leg_press"},
            {"id": "seated_dumbbell_press", "name": "Жим гантелей сидя",    "eq_id": "dumbbells"},
            {"id": "lateral_raises",        "name": "Махи в стороны",       "eq_id": "shoulder_machine"},
        ],
    },
    "3": {
        "name": "💪 Спина + Бицепс",
        "exercises": [
            {"id": "bent_over_row", "name": "Тяга в наклоне в Смите",       "eq_id": "smith_machine"},
            {"id": "lat_pulldown",  "name": "Вертикальная тяга блока",       "eq_id": "cable_machine"},
            {"id": "dumbbell_curl", "name": "Сгибания рук с гантелями",      "eq_id": "dumbbells"},
            {"id": "cable_curl",    "name": "Сгибания рук в блоке",          "eq_id": "cable_machine"},
        ],
    },
}

# Схема подходов: (номер, целевые повторения, % от рабочего веса)
SETS_PLAN = [
    (1, 10, 0.40),
    (2,  5, 0.70),
    (3, 10, 1.00),
    (4, 10, 1.00),
]

# ============================================================
# СОСТОЯНИЯ ConversationHandler
# ============================================================

SELECTING_TRAINING, SELECTING_WEIGHT, ENTERING_REPS, CONFIRMING_WEIGHT_CHANGE = range(4)

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================



def get_closest_weight(available: list, target: float) -> float:
    return min(available, key=lambda w: abs(w - target))


def compute_new_weight(current: float, available: list, set3_reps: int, set4_reps: int, set4_weight: float) -> float:
    """
    Приоритет:
    1. Подход 3 < 10 → снижаем (даже если подход 4 на 10)
    2. Подход 4 == 10 с рабочим весом → повышаем
    3. Иначе → оставляем
    """
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


async def send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """Универсальная отправка — работает и для message, и для callback_query."""
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text,
            reply_markup=reply_markup, parse_mode="HTML"
        )


def training_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(TRAININGS[d]["name"], callback_data=f"train_{d}")]
        for d in ("1", "2", "3")
    ]
    return InlineKeyboardMarkup(buttons)


def weight_keyboard(available: list, set_num: int, target_reps: int) -> InlineKeyboardMarkup:
    weights_to_show = available[:12] if len(available) > 12 else available
    keyboard = []
    row = []
    for w in weights_to_show:
        row.append(InlineKeyboardButton(str(w), callback_data=f"w_{w}_{set_num}_{target_reps}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отменить тренировку", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)


def reps_keyboard(set_num: int) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for r in range(0, 13):
        row.append(InlineKeyboardButton(str(r), callback_data=f"r_{r}_{set_num}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отменить тренировку", callback_data="cancel_workout")])
    return InlineKeyboardMarkup(keyboard)


async def build_month_history(user_id: int) -> str:
    sessions = await db.get_month_sessions(user_id)
    if not sessions:
        return "📅 В этом месяце тренировок ещё не было."
    lines = ["📅 <b>Тренировки в этом месяце:</b>"]
    for i, s in enumerate(sessions, 1):
        dt = s["started_at"].astimezone(TZ)
        lines.append(f"{i}. {dt.strftime('%d.%m %H:%M')} — {s['name']}")
    return "\n".join(lines)


# ============================================================
# ГЛАВНЫЙ ЭКРАН — /start и кнопка «Начать тренировку»
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    history = await build_month_history(user_id)

    text = (
        f"{history}\n\n"
        "Выберите тренировку:"
    )
    await send(update, context, text, reply_markup=training_keyboard())
    return SELECTING_TRAINING


# ============================================================
# ВЫБОР ТРЕНИРОВКИ
# ============================================================

async def select_training(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    day = query.data.split("_")[1]  # "train_1" → "1"
    training = TRAININGS[day]
    user_id = update.effective_user.id

    # Создаём сессию в БД
    session_id = await db.create_session(user_id, day, training["name"])

    context.user_data.update({
        "user_id":        user_id,
        "day":            day,
        "session_id":     session_id,
        "exercise_index": 0,
        "equipment":      await db.get_all_equipment(),
    })

    await query.edit_message_text(
        f"✅ Начинаем: <b>{training['name']}</b>",
        parse_mode="HTML"
    )
    return await start_exercise(update, context)


# ============================================================
# УПРАЖНЕНИЕ
# ============================================================

async def start_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = context.user_data["day"]
    idx = context.user_data["exercise_index"]
    exercises = TRAININGS[day]["exercises"]

    if idx >= len(exercises):
        return await finish_workout(update, context)

    ex = exercises[idx]
    user_id = context.user_data["user_id"]
    current_weight = await db.get_weight(user_id, day, ex["id"])

    equipment = context.user_data["equipment"]
    available = equipment[ex["eq_id"]]["available_weights"]

    context.user_data.update({
        "current_exercise":      ex,
        "current_weight":        current_weight,
        "available_weights":     available,
        "current_exercise_sets": [],
    })

    total = len(exercises)
    text = (
        f"📋 <b>Упражнение {idx + 1}/{total}: {ex['name']}</b>\n"
        f"🎯 Рабочий вес: <b>{current_weight} кг</b>\n\n"
        f"Начинаем <b>подход 1</b> (10 повт., ~40% веса)"
    )
    await send(update, context, text)

    # Первый подход
    _, target_reps, pct = SETS_PLAN[0]
    default = get_closest_weight(available, current_weight * pct)
    await ask_weight(update, context, set_num=1, default_weight=default, target_reps=target_reps)
    return SELECTING_WEIGHT


async def ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     set_num: int, default_weight: float, target_reps: int):
    _, plan_reps, pct = SETS_PLAN[set_num - 1]
    pct_label = f"{int(pct * 100)}%"

    text = (
        f"<b>Подход {set_num}</b> — цель: {plan_reps} повт., {pct_label} веса\n"
        f"💡 Рекомендуемый вес: <b>{default_weight} кг</b>"
    )
    available = context.user_data["available_weights"]
    await send(update, context, text,
               reply_markup=weight_keyboard(available, set_num, target_reps))


# ============================================================
# ВЫБОР ВЕСА
# ============================================================

async def weight_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_workout":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏋️ Начать новую тренировку", callback_data="go_start")]
        ])
        await query.edit_message_text(
            "❌ Тренировка отменена.\n\n"
            "📊 Выполненные упражнения сохранены.",
            reply_markup=keyboard
        )
        context.user_data.clear()
        return ConversationHandler.END

    parts = query.data.split("_")  # w_22.5_2_10
    try:
        weight   = float(parts[1])
        set_num  = int(parts[2])
        target_reps = int(parts[3])
    except (IndexError, ValueError):
        await query.edit_message_text("⚠️ Ошибка, попробуйте снова.")
        return SELECTING_WEIGHT

    context.user_data["temp_weight"]      = weight
    context.user_data["temp_set_num"]     = set_num
    context.user_data["temp_target_reps"] = target_reps

    await query.edit_message_text(
        f"<b>Подход {set_num}</b> — вес: <b>{weight} кг</b>\n"
        f"Сколько повторений сделали? (цель: {target_reps})",
        reply_markup=reps_keyboard(set_num),
        parse_mode="HTML"
    )
    return ENTERING_REPS


# ============================================================
# ВВОД ПОВТОРЕНИЙ
# ============================================================

async def reps_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_workout":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏋️ Начать новую тренировку", callback_data="go_start")]
        ])
        await query.edit_message_text(
            "❌ Тренировка отменена.\n\n"
            "📊 Выполненные упражнения сохранены.",
            reply_markup=keyboard
        )
        context.user_data.clear()
        return ConversationHandler.END

    parts = query.data.split("_")  # r_8_2
    try:
        reps    = int(parts[1])
        set_num = int(parts[2])
    except (IndexError, ValueError):
        await query.edit_message_text("⚠️ Ошибка, попробуйте снова.")
        return ENTERING_REPS

    weight     = context.user_data["temp_weight"]
    session_id = context.user_data["session_id"]
    ex         = context.user_data["current_exercise"]

    # Сохраняем подход в БД
    await db.save_set(session_id, ex["id"], ex["name"], set_num, weight, reps)

    context.user_data["current_exercise_sets"].append({
        "set": set_num, "weight_kg": weight, "reps": reps
    })

    await query.edit_message_text(
        f"✅ Подход {set_num} сохранён: <b>{weight} кг × {reps} повт.</b>",
        parse_mode="HTML"
    )

    if set_num < 4:
        next_set = set_num + 1
        _, target_reps, pct = SETS_PLAN[next_set - 1]
        current = context.user_data["current_weight"]
        available = context.user_data["available_weights"]
        default = get_closest_weight(available, current * pct)
        await ask_weight(update, context, set_num=next_set, default_weight=default, target_reps=target_reps)
        return SELECTING_WEIGHT
    else:
        return await finish_exercise(update, context)


# ============================================================
# ЗАВЕРШЕНИЕ УПРАЖНЕНИЯ
# ============================================================

async def finish_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sets_data      = context.user_data["current_exercise_sets"]
    ex             = context.user_data["current_exercise"]
    current_weight = context.user_data["current_weight"]
    available      = context.user_data["available_weights"]
    user_id        = context.user_data["user_id"]
    day            = context.user_data["day"]
    session_id     = context.user_data["session_id"]

    set3_reps  = sets_data[2]["reps"]
    set4_reps  = sets_data[3]["reps"]
    set4_weight = sets_data[3]["weight_kg"]

    new_weight = compute_new_weight(current_weight, available, set3_reps, set4_reps, set4_weight)

    if new_weight != current_weight:
        # Нужно подтверждение изменения веса
        direction = "увеличен" if new_weight > current_weight else "уменьшен"
        emoji     = "📈" if new_weight > current_weight else "📉"
        context.user_data["pending_new_weight"] = new_weight

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтверждаю", callback_data=f"wchange_yes")],
            [InlineKeyboardButton("❌ Оставить текущий", callback_data=f"wchange_no")],
        ])
        await send(
            update, context,
            f"{emoji} На следующей тренировке вес будет <b>{direction}</b>: "
            f"{current_weight} → <b>{new_weight} кг</b>\n\nПодтверждаете?",
            reply_markup=keyboard
        )
        return CONFIRMING_WEIGHT_CHANGE
    else:
        await _apply_exercise_result(update, context, new_weight)
        return SELECTING_WEIGHT


async def confirm_weight_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    confirmed = query.data == "wchange_yes"
    current_weight = context.user_data["current_weight"]
    pending        = context.user_data.pop("pending_new_weight", current_weight)
    new_weight     = pending if confirmed else current_weight

    if confirmed:
        direction = "увеличен" if new_weight > current_weight else "уменьшен"
        await query.edit_message_text(
            f"✅ Принято. Вес на следующую тренировку будет <b>{direction}</b>: <b>{new_weight} кг</b>",
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text(
            f"↩️ Вес сохранён: <b>{current_weight} кг</b>",
            parse_mode="HTML"
        )
        new_weight = current_weight

    await _apply_exercise_result(update, context, new_weight)
    return SELECTING_WEIGHT


async def _apply_exercise_result(update: Update, context: ContextTypes.DEFAULT_TYPE, new_weight: float):
    """Сохраняет новый вес в БД и переходит к следующему упражнению."""
    ex         = context.user_data["current_exercise"]
    user_id    = context.user_data["user_id"]
    day        = context.user_data["day"]
    session_id = context.user_data["session_id"]

    await db.set_weight(user_id, day, ex["id"], new_weight)
    await db.update_sets_new_max(session_id, ex["id"], new_weight)

    context.user_data["exercise_index"] += 1
    context.user_data["current_exercise_sets"] = []
    await start_exercise(update, context)


# ============================================================
# ЗАВЕРШЕНИЕ ТРЕНИРОВКИ
# ============================================================

async def finish_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏋️ Начать новую тренировку", callback_data="go_start")]
    ])
    await send(
        update, context,
        "🎉 <b>Тренировка завершена!</b>\n\n📊 Все данные сохранены в базе.",
        reply_markup=keyboard
    )
    context.user_data.clear()
    return ConversationHandler.END


async def go_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Начать новую тренировку» после завершения."""
    query = update.callback_query
    await query.answer()
    return await start(update, context)


# ============================================================
# ОТМЕНА
# ============================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await send(update, context, "❌ Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ============================================================
# ЗАПУСК
# ============================================================

async def post_init(application: Application):
    await db.init_db()
    print("✅ БД инициализирована")


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(go_start_callback, pattern="^go_start$"),
        ],
        states={
            SELECTING_TRAINING: [
                CallbackQueryHandler(select_training, pattern="^train_"),
            ],
            SELECTING_WEIGHT: [
                CallbackQueryHandler(weight_selected, pattern="^w_"),
                CallbackQueryHandler(weight_selected, pattern="^cancel_workout$"),
            ],
            ENTERING_REPS: [
                CallbackQueryHandler(reps_entered, pattern="^r_"),
                CallbackQueryHandler(reps_entered, pattern="^cancel_workout$"),
            ],
            CONFIRMING_WEIGHT_CHANGE: [
                CallbackQueryHandler(confirm_weight_change, pattern="^wchange_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # Позволяет войти в диалог заново через /start в любой момент
        allow_reentry=True,
    )

    app.add_handler(conv)

    print("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()