"""
db.py — работа с PostgreSQL через asyncpg.

Таблицы:
  - equipment         : снаряды и доступные веса
  - training_weights  : текущие рабочие веса пользователя по упражнениям
  - workout_sessions  : одна строка на тренировку (дата, день, user_id)
  - workout_sets      : каждый подход каждого упражнения
"""

import os
import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ Не задана переменная окружения DATABASE_URL")

# Railway иногда отдаёт postgres://, asyncpg требует postgresql://
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


# ============================================================
# НАЧАЛЬНЫЕ ДАННЫЕ
# ============================================================

# Снаряды: eq_id → (название, список весов)
INITIAL_EQUIPMENT: dict[str, tuple[str, list[float]]] = {
    "smith_machine": ("Тренажер Смита", [
        20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100
    ]),
    "dumbbells": ("Гантели", [
        2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30
    ]),
    "cable_machine": ("Блочный тренажер", [
        9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33, 35,
        37, 39, 41, 43, 45, 47, 49, 51, 53, 55, 57, 59
    ]),
    "leg_press": ("Горизонтальная платформа", [
        45, 62, 72, 81, 90, 99, 108, 117, 126, 135,
        144, 153, 162, 171, 180, 189, 198
    ]),
    "shoulder_machine": ("Тренажер для плеч (махи)", [
        14, 16, 18, 20, 23, 26, 29, 32, 35, 38, 41, 44,
        47, 50, 53, 56, 59, 62, 65, 68, 71, 74, 77, 80
    ]),
}

# Стартовые рабочие веса (используются при первом запуске нового пользователя)
DEFAULT_WEIGHTS = {
    "1": {"bench_press": 55, "svend_press": 12, "french_press": 8,  "triceps_extension": 15},
    "2": {"squat": 65, "leg_press": 90, "seated_dumbbell_press": 12, "lateral_raises": 23},
    "3": {"bent_over_row": 45, "lat_pulldown": 54, "dumbbell_curl": 8, "cable_curl": 13},
}


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БД
# ============================================================

async def init_db():
    """Создаёт таблицы и заполняет equipment начальными данными."""
    pool = await get_pool()
    async with pool.acquire() as conn:

        # --- equipment ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS equipment (
                eq_id            TEXT    PRIMARY KEY,
                name             TEXT    NOT NULL,
                available_weights REAL[] NOT NULL
            )
        """)

        # --- training_weights ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS training_weights (
                user_id     BIGINT  NOT NULL,
                day         TEXT    NOT NULL,
                exercise_id TEXT    NOT NULL,
                weight_kg   REAL    NOT NULL,
                PRIMARY KEY (user_id, day, exercise_id)
            )
        """)

        # --- workout_sessions ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workout_sessions (
                id         SERIAL      PRIMARY KEY,
                user_id    BIGINT      NOT NULL,
                day        TEXT        NOT NULL,
                name       TEXT        NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # --- workout_sets ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workout_sets (
                id            SERIAL  PRIMARY KEY,
                session_id    INT     NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
                exercise_id   TEXT    NOT NULL,
                exercise_name TEXT    NOT NULL,
                set_num       INT     NOT NULL,
                weight_kg     REAL    NOT NULL,
                reps          INT     NOT NULL,
                new_max_kg    REAL            -- NULL до завершения упражнения
            )
        """)

        # Заполняем equipment начальными данными (один раз, не перезаписываем)
        for eq_id, (name, weights) in INITIAL_EQUIPMENT.items():
            await conn.execute("""
                INSERT INTO equipment (eq_id, name, available_weights)
                VALUES ($1, $2, $3)
                ON CONFLICT (eq_id) DO NOTHING
            """, eq_id, name, weights)


# ============================================================
# ОБОРУДОВАНИЕ
# ============================================================

async def get_available_weights(eq_id: str) -> list[float]:
    """Возвращает список доступных весов для снаряда."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT available_weights FROM equipment WHERE eq_id = $1", eq_id
        )
        if not row:
            raise ValueError(f"Снаряд '{eq_id}' не найден в таблице equipment")
        return list(row["available_weights"])


async def get_all_equipment() -> dict[str, list[float]]:
    """Загружает все снаряды разом → {eq_id: [weights]}. Используется при старте тренировки."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT eq_id, available_weights FROM equipment")
        return {r["eq_id"]: list(r["available_weights"]) for r in rows}


# ============================================================
# РАБОЧИЕ ВЕСА ПОЛЬЗОВАТЕЛЯ
# ============================================================

async def get_weight(user_id: int, day: str, exercise_id: str) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT weight_kg FROM training_weights WHERE user_id=$1 AND day=$2 AND exercise_id=$3",
            user_id, day, exercise_id
        )
        if row:
            return float(row["weight_kg"])
        # Первый запуск — взять дефолт и сохранить
        default = DEFAULT_WEIGHTS[day][exercise_id]
        await set_weight(user_id, day, exercise_id, default)
        return float(default)


async def set_weight(user_id: int, day: str, exercise_id: str, weight_kg: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO training_weights (user_id, day, exercise_id, weight_kg)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, day, exercise_id)
            DO UPDATE SET weight_kg = EXCLUDED.weight_kg
        """, user_id, day, exercise_id, weight_kg)


# ============================================================
# СЕССИИ ТРЕНИРОВОК
# ============================================================

async def create_session(user_id: int, day: str, name: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO workout_sessions (user_id, day, name) VALUES ($1, $2, $3) RETURNING id",
            user_id, day, name
        )
        return row["id"]


async def save_set(session_id: int, exercise_id: str, exercise_name: str,
                   set_num: int, weight_kg: float, reps: int,
                   new_max_kg: float | None = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO workout_sets
                (session_id, exercise_id, exercise_name, set_num, weight_kg, reps, new_max_kg)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, session_id, exercise_id, exercise_name, set_num, weight_kg, reps, new_max_kg)


async def update_sets_new_max(session_id: int, exercise_id: str, new_max_kg: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE workout_sets SET new_max_kg = $1
            WHERE session_id = $2 AND exercise_id = $3
        """, new_max_kg, session_id, exercise_id)


# ============================================================
# ИСТОРИЯ
# ============================================================

async def get_month_sessions(user_id: int) -> list[asyncpg.Record]:
    """Тренировки текущего календарного месяца, от старых к новым."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT id, day, name, started_at
            FROM workout_sessions
            WHERE user_id = $1
              AND date_trunc('month', started_at) = date_trunc('month', NOW())
            ORDER BY started_at ASC
        """, user_id)
