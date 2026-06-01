import json
import os
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, ContextTypes, filters

# ============================================================
# НАСТРОЙКИ И ПУТИ К ФАЙЛАМ
# ============================================================

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    print("❌ Ошибка: не найден токен. Установите переменную окружения TOKEN")
    exit(1)

EQUIPMENT_FILE = "equipment.json"
DATA_FILE = "training_data.json"
LOG_FILE = "workout_log.json"

# ------------------------------------------------------------
# ЗАГРУЗКА ДАННЫХ
# ------------------------------------------------------------

def load_equipment():
    if not os.path.exists(EQUIPMENT_FILE):
        print(f"❌ Ошибка: файл {EQUIPMENT_FILE} не найден!")
        exit(1)
    with open(EQUIPMENT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_training_data():
    initial = {
        "1": {
            "bench_press": 55,
            "svend_press": 12,
            "french_press": 8,
            "triceps_extension": 15
        },
        "2": {
            "squat": 65,
            "leg_press": 90,
            "seated_dumbbell_press": 12,
            "lateral_raises": 23
        },
        "3": {
            "bent_over_row": 45,
            "lat_pulldown": 54,
            "dumbbell_curl": 8,
            "cable_curl": 13
        }
    }

    if not os.path.exists(DATA_FILE):
        save_training_data(initial)
        return initial

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "1" not in data:
                print("⚠️ Файл training_data.json повреждён, создаю новый")
                save_training_data(initial)
                return initial
            return data
    except (json.JSONDecodeError, KeyError):
        print("⚠️ Файл training_data.json повреждён, создаю новый")
        save_training_data(initial)
        return initial

def save_training_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def save_workout_log(entry):
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except json.JSONDecodeError:
            logs = []
    logs.append(entry)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)

# ------------------------------------------------------------
# ДАННЫЕ О ТРЕНИРОВКАХ
# ------------------------------------------------------------

TRAININGS = {
    "1": {"name": "🏋️ Грудь + Трицепс",
        "exercises": [
            {"id": "bench_press", "name": "Жим лёжа в Смите", "eq_id": "smith_machine"},
            {"id": "svend_press", "name": "Жим Свенда (гантели)", "eq_id": "dumbbells"},
            {"id": "french_press", "name": "Французский жим лёжа", "eq_id": "dumbbells"},
            {"id": "triceps_extension", "name": "Разгибание рук в блоке", "eq_id": "cable_machine"},
        ]},
    "2": {"name": "🦵 Ноги + Плечи",
        "exercises": [
            {"id": "squat", "name": "Приседания в Смите", "eq_id": "smith_machine"},
            {"id": "leg_press", "name": "Жим ногами", "eq_id": "leg_press"},
            {"id": "seated_dumbbell_press", "name": "Жим гантелей сидя", "eq_id": "dumbbells"},
            {"id": "lateral_raises", "name": "Махи в стороны", "eq_id": "shoulder_machine"},
        ]},
    "3": {"name": "💪 Спина + Бицепс",
        "exercises": [
            {"id": "bent_over_row", "name": "Тяга в наклоне в Смите", "eq_id": "smith_machine"},
            {"id": "lat_pulldown", "name": "Вертикальная тяга блока", "eq_id": "cable_machine"},
            {"id": "dumbbell_curl", "name": "Сгибания рук с гантелями", "eq_id": "dumbbells"},
            {"id": "cable_curl", "name": "Сгибания рук в блоке", "eq_id": "cable_machine"},
        ]},
}

# ------------------------------------------------------------
# СОСТОЯНИЯ
# ------------------------------------------------------------

SELECTING_TRAINING, SELECTING_WEIGHT, ENTERING_REPS = range(3)

# ------------------------------------------------------------
# ФУНКЦИИ ЛОГИКИ
# ------------------------------------------------------------

def get_closest_weight(available, target):
    return min(available, key=lambda w: abs(w - target))

def update_max_weight(current, available, set3_reps, set4_reps, set4_weight):
    """
    Приоритет:
    1. Подход 3 < 10 повторений → снижаем вес (даже если подход 4 вышел на 10)
    2. Подход 4 == 10 повторений с рабочим весом → повышаем вес
    3. Иначе → сохраняем вес
    """
    if set3_reps < 10:
        try:
            idx = available.index(current)
            if idx > 0:
                return available[idx - 1]
        except ValueError:
            pass
        return current
    elif set4_reps == 10 and set4_weight == current:
        try:
            idx = available.index(current)
            if idx + 1 < len(available):
                return available[idx + 1]
        except ValueError:
            pass
        return current
    return current

async def send_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """Универсальная функция отправки сообщения"""
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)

async def save_current_progress(context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет прогресс уже выполненных упражнений"""
    if "workout_log" not in context.user_data:
        return

    workout_log = context.user_data["workout_log"]
    if workout_log["exercises"]:
        save_workout_log(workout_log)

        # Обновляем веса в training_data
        training_data = load_training_data()
        day = workout_log["day"]

        for exercise_log in workout_log["exercises"]:
            for ex in TRAININGS[day]["exercises"]:
                if ex["name"] == exercise_log["name"]:
                    training_data[day][ex["id"]] = exercise_log["new_max_weight"]
                    break

        save_training_data(training_data)

# ------------------------------------------------------------
# ОБРАБОТЧИКИ
# ------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # FIX: загружаем оборудование один раз при старте диалога
    context.user_data["equipment"] = load_equipment()

    keyboard = [
        [KeyboardButton(TRAININGS["1"]["name"])],
        [KeyboardButton(TRAININGS["2"]["name"])],
        [KeyboardButton(TRAININGS["3"]["name"])],
        [KeyboardButton("❌ Отмена")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await send_message(update, context,
        "🏋️ Добро пожаловать в тренировочного бота!\n\n"
        "Выберите тип тренировки:",
        reply_markup=reply_markup
    )
    return SELECTING_TRAINING

async def select_training(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        # FIX: ReplyKeyboardRemove() вместо ReplyKeyboardMarkup.remove()
        await send_message(update, context, "Действие отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    for day, training in TRAININGS.items():
        if training["name"] == text:
            context.user_data["day"] = day
            context.user_data["exercise_index"] = 0
            context.user_data["workout_log"] = {
                "date": datetime.now().isoformat(),
                "day": day,
                "name": training["name"],
                "exercises": []
            }
            await start_exercise(update, context)
            return SELECTING_WEIGHT

    await send_message(update, context, "Пожалуйста, выберите тренировку из меню.")
    return SELECTING_TRAINING

async def start_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = context.user_data["day"]
    idx = context.user_data["exercise_index"]
    exercises = TRAININGS[day]["exercises"]

    if idx >= len(exercises):
        await finish_workout(update, context)
        return ConversationHandler.END

    ex = exercises[idx]
    training_data = load_training_data()
    current_weight = training_data[day][ex["id"]]

    # FIX: используем оборудование из user_data, загруженное один раз при /start
    equipment = context.user_data["equipment"]
    available = equipment[ex["eq_id"]]["available_weights"]
    # Подход 1: 40% рабочего веса
    default_40 = get_closest_weight(available, current_weight * 0.4)

    context.user_data["current_exercise"] = ex
    context.user_data["current_weight"] = current_weight
    context.user_data["available_weights"] = available
    context.user_data["current_exercise_sets"] = []

    await send_message(update, context,
        f"📋 Упражнение {idx+1}/{len(exercises)}: {ex['name']}\n"
        f"🎯 Текущий максимум: {current_weight} кг\n\n"
        f"Теперь **подход 1** (цель: 10 повторений, ~40% веса)"
    )
    await ask_weight(update, context, set_num=1, default_weight=default_40, target_reps=10)
    return SELECTING_WEIGHT

async def ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE, set_num: int, default_weight: float, target_reps: int):
    available = context.user_data["available_weights"]
    weights_to_show = available[:10] if len(available) > 10 else available
    keyboard = []
    row = []
    for w in weights_to_show:
        # FIX: якорим паттерн через префикс "w_" — достаточно уникален
        row.append(InlineKeyboardButton(str(w), callback_data=f"w_{w}_{set_num}_{target_reps}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отмена тренировки", callback_data="cancel_workout")])

    await send_message(update, context,
        f"Выберите вес для **подхода {set_num}** (цель: {target_reps} повторений)\n"
        f"💡 Рекомендуемый вес: **{default_weight} кг**",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def weight_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "cancel_workout":
        await save_current_progress(context)
        # FIX: ReplyKeyboardRemove() вместо ReplyKeyboardMarkup.remove()
        await query.edit_message_text("❌ Тренировка отменена. Прогресс сохранён.")
        context.user_data.clear()
        return ConversationHandler.END

    # FIX: валидация формата callback_data
    parts = data.split("_")
    if len(parts) < 4:
        await query.edit_message_text("⚠️ Ошибка данных, попробуйте снова.")
        return SELECTING_WEIGHT

    try:
        weight = float(parts[1])
        set_num = int(parts[2])
        target_reps = int(parts[3])
    except (ValueError, IndexError):
        await query.edit_message_text("⚠️ Ошибка данных, попробуйте снова.")
        return SELECTING_WEIGHT

    context.user_data["temp_weight"] = weight
    context.user_data["temp_set_num"] = set_num
    context.user_data["temp_target_reps"] = target_reps

    keyboard = []
    row = []
    for r in range(0, 13):
        row.append(InlineKeyboardButton(str(r), callback_data=f"r_{r}_{set_num}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отмена тренировки", callback_data="cancel_workout")])

    await query.edit_message_text(
        f"⚡ Вес: **{weight} кг**\n\n"
        f"Сколько повторений сделали? (цель: {target_reps})",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ENTERING_REPS

async def reps_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "cancel_workout":
        await save_current_progress(context)
        await query.edit_message_text("❌ Тренировка отменена. Прогресс сохранён.")
        context.user_data.clear()
        return ConversationHandler.END

    # FIX: валидация формата callback_data
    parts = data.split("_")
    if len(parts) < 2:
        await query.edit_message_text("⚠️ Ошибка данных, попробуйте снова.")
        return ENTERING_REPS

    try:
        reps = int(parts[1])
    except (ValueError, IndexError):
        await query.edit_message_text("⚠️ Ошибка данных, попробуйте снова.")
        return ENTERING_REPS

    set_num = context.user_data["temp_set_num"]
    weight = context.user_data["temp_weight"]

    context.user_data["current_exercise_sets"].append({
        "set": set_num,
        "weight_kg": weight,
        "reps": reps
    })

    await query.edit_message_text(
        f"✅ **Подход {set_num}** сохранён: {weight} кг × {reps} повторений"
    )

    if set_num < 4:
        next_set = set_num + 1
        target_reps_next = 10  # FIX: было 5 для подхода 2 — исправлено на 10

        current = context.user_data["current_weight"]
        available = context.user_data["available_weights"]

        # FIX: убрано мёртвое условие next_set == 1 (never True)
        if next_set == 2:
            default = get_closest_weight(available, current * 0.7)
        else:  # подходы 3 и 4 — рабочий вес
            default = current

        await ask_weight(update, context, set_num=next_set, default_weight=default, target_reps=target_reps_next)
        return SELECTING_WEIGHT
    else:
        await finish_exercise(update, context)
        return SELECTING_WEIGHT

async def finish_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sets_data = context.user_data["current_exercise_sets"]
    ex = context.user_data["current_exercise"]
    current_weight = context.user_data["current_weight"]
    available = context.user_data["available_weights"]
    day = context.user_data["day"]

    set3_reps = sets_data[2]["reps"]
    set4_reps = sets_data[3]["reps"]
    set4_weight = sets_data[3]["weight_kg"]

    perfect_technique = False

    if set4_reps == 10 and set4_weight == current_weight:
        keyboard = InlineKeyboardMarkup([
            # FIX: используем паттерн ^tech_ — обрабатывается внутри ConversationHandler
            [InlineKeyboardButton("✅ Да, идеально", callback_data=f"tech_yes_{current_weight}")],
            [InlineKeyboardButton("❌ Нет, были ошибки", callback_data=f"tech_no_{current_weight}")],
            [InlineKeyboardButton("❌ Отмена тренировки", callback_data="cancel_workout")]
        ])
        await send_message(update, context,
            f"В 4-м подходе вы сделали **10 повторений** с весом {current_weight} кг.\n"
            f"Техника была идеальной?",
            reply_markup=keyboard
        )
        context.user_data["awaiting_technique"] = True
        return

    new_weight = update_max_weight(current_weight, available, set3_reps, set4_reps, set4_weight, perfect_technique)

    training_data = load_training_data()
    training_data[day][ex["id"]] = new_weight
    save_training_data(training_data)

    exercise_log = {
        "name": ex["name"],
        "sets": sets_data,
        "new_max_weight": new_weight
    }
    context.user_data["workout_log"]["exercises"].append(exercise_log)

    context.user_data["exercise_index"] += 1
    context.user_data["current_exercise_sets"] = []
    await start_exercise(update, context)

# FIX: technique_answer перенесён в ConversationHandler (states -> SELECTING_WEIGHT)
async def technique_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "cancel_workout":
        await save_current_progress(context)
        await query.edit_message_text("❌ Тренировка отменена. Прогресс сохранён.")
        context.user_data.clear()
        return ConversationHandler.END

    # FIX: валидация формата callback_data
    parts = data.split("_")
    if len(parts) < 3:
        await query.edit_message_text("⚠️ Ошибка данных, попробуйте снова.")
        return SELECTING_WEIGHT

    try:
        perfect = (parts[1] == "yes")
        current_weight = float(parts[2])
    except (ValueError, IndexError):
        await query.edit_message_text("⚠️ Ошибка данных, попробуйте снова.")
        return SELECTING_WEIGHT

    await query.edit_message_text(f"✅ Спасибо! Техника: **{'идеальная' if perfect else 'неидеальная'}**")

    sets_data = context.user_data["current_exercise_sets"]
    ex = context.user_data["current_exercise"]
    available = context.user_data["available_weights"]
    day = context.user_data["day"]

    set3_reps = sets_data[2]["reps"]
    set4_reps = sets_data[3]["reps"]
    set4_weight = sets_data[3]["weight_kg"]

    new_weight = update_max_weight(current_weight, available, set3_reps, set4_reps, set4_weight, perfect)

    training_data = load_training_data()
    training_data[day][ex["id"]] = new_weight
    save_training_data(training_data)

    exercise_log = {
        "name": ex["name"],
        "sets": sets_data,
        "new_max_weight": new_weight
    }
    context.user_data["workout_log"]["exercises"].append(exercise_log)

    context.user_data["exercise_index"] += 1
    context.user_data["current_exercise_sets"] = []
    await start_exercise(update, context)

async def finish_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # FIX: лог уже сохраняется инкрементально через save_current_progress;
    # здесь дополнительно сохраняем финальный лог только если он ещё не пустой
    workout_log = context.user_data.get("workout_log", {})
    if workout_log.get("exercises"):
        save_workout_log(workout_log)

    # FIX: ReplyKeyboardRemove() вместо ReplyKeyboardMarkup.remove()
    await send_message(update, context,
        f"🎉 **Тренировка завершена!**\n\n"
        f"📊 Данные сохранены.\n\n"
        f"Чтобы начать новую тренировку, отправьте /start",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_current_progress(context)
    # FIX: ReplyKeyboardRemove() вместо ReplyKeyboardMarkup.remove()
    await send_message(update, context, "❌ Действие отменено. Прогресс сохранён.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# ------------------------------------------------------------
# ЗАПУСК
# ------------------------------------------------------------

def main():
    if not os.path.exists(EQUIPMENT_FILE):
        print(f"❌ Ошибка: файл {EQUIPMENT_FILE} не найден!")
        print(f"   Убедитесь, что файл находится в папке: {os.getcwd()}")
        return

    app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_TRAINING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_training)
            ],
            SELECTING_WEIGHT: [
                # FIX: technique_answer добавлен сюда с паттерном ^tech_
                # чтобы ConversationHandler перехватывал его раньше weight_selected
                CallbackQueryHandler(technique_answer, pattern="^tech_"),
                CallbackQueryHandler(weight_selected),
            ],
            ENTERING_REPS: [
                CallbackQueryHandler(reps_entered)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    # FIX: убраны дублирующие обработчики вне ConversationHandler:
    # - CallbackQueryHandler(technique_answer, pattern="tech_") — теперь внутри conv
    # - CallbackQueryHandler(weight_selected, pattern="cancel_workout") — никогда не срабатывал
    app.add_handler(CommandHandler("cancel", cancel))

    print("🤖 Бот запущен! Нажмите /start в Telegram")
    print(f"📁 Файлы в папке: {os.getcwd()}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()