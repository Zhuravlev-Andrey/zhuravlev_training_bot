"""
db.py — слой данных, PostgreSQL через asyncpg.

Таблицы (все в единственном числе):
  equipment         — снаряд и его доступные веса
  workout_template  — шаблон тренировки пользователя
  exercise_template — упражнение внутри шаблона
  set_template      — подход внутри упражнения (дефолт + переопределения)
  training_weight   — актуальный рабочий вес пользователя по упражнению
  workout_session   — факт выполненной (или отменённой) тренировки
  workout_set       — фактический подход внутри сессии
  schedule_entry    — расписание чередования тренировок
"""

import os
import asyncpg
from config import INITIAL_EQUIPMENT, DB_POOL_MIN_DEFAULT, DB_POOL_MAX_DEFAULT

DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL не задан")

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # min_size/max_size вынесены в env — легко менять без деплоя
        min_size = int(os.environ.get("DB_POOL_MIN", str(DB_POOL_MIN_DEFAULT)))
        max_size = int(os.environ.get("DB_POOL_MAX", str(DB_POOL_MAX_DEFAULT)))
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=min_size, max_size=max_size)
    return _pool


# ============================================================
# НАЧАЛЬНЫЕ ДАННЫЕ
# ============================================================


# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # --- equipment ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS equipment (
                id                SERIAL  PRIMARY KEY,
                eq_key            TEXT    UNIQUE NOT NULL,
                name              TEXT    NOT NULL,
                available_weight  REAL[]  NOT NULL
            )
        """)

        # --- workout_template ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workout_template (
                id                  SERIAL  PRIMARY KEY,
                user_id             BIGINT  NOT NULL,
                name                TEXT    NOT NULL,
                use_weight_progress BOOLEAN NOT NULL DEFAULT TRUE,
                schedule_order      INT,        -- порядок в расписании (NULL = не в расписании)
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_workout_template_user ON workout_template(user_id)")

        # --- exercise_template ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS exercise_template (
                id              SERIAL  PRIMARY KEY,
                workout_id      INT     NOT NULL REFERENCES workout_template(id) ON DELETE CASCADE,
                equipment_id    INT     REFERENCES equipment(id) ON DELETE SET NULL,
                name            TEXT    NOT NULL,
                order_index     INT     NOT NULL,   -- порядок упражнения в тренировке
                default_sets    INT     NOT NULL DEFAULT 4,
                default_reps    INT     NOT NULL DEFAULT 10,
                default_rest_s  INT     NOT NULL DEFAULT 90,  -- отдых по умолчанию в секундах
                comment         TEXT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_exercise_template_workout ON exercise_template(workout_id)")

        # --- set_template ---
        # Если для подхода нет строки — используются default_* из exercise_template
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS set_template (
                id              SERIAL  PRIMARY KEY,
                exercise_id     INT     NOT NULL REFERENCES exercise_template(id) ON DELETE CASCADE,
                set_number      INT     NOT NULL,   -- 1-based номер подхода
                reps            INT,                -- NULL = брать из exercise_template.default_reps
                rest_s          INT,                -- NULL = брать из exercise_template.default_rest_s
                weight_pct      REAL,               -- % от рабочего веса (NULL = 100%)
                UNIQUE(exercise_id, set_number)
            )
        """)

        # --- training_weight ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS training_weight (
                user_id         BIGINT  NOT NULL,
                exercise_id     INT     NOT NULL REFERENCES exercise_template(id) ON DELETE CASCADE,
                weight_kg       REAL    NOT NULL,
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, exercise_id)
            )
        """)

        # --- workout_session ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workout_session (
                id              SERIAL      PRIMARY KEY,
                user_id         BIGINT      NOT NULL,
                template_id     INT         REFERENCES workout_template(id) ON DELETE SET NULL,
                template_name   TEXT        NOT NULL,   -- денормализовано: имя на момент выполнения
                status          TEXT        NOT NULL DEFAULT 'active',  -- active | completed | cancelled
                started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at     TIMESTAMPTZ
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_workout_session_user ON workout_session(user_id)")

        # --- workout_set ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workout_set (
                id              SERIAL  PRIMARY KEY,
                session_id      INT     NOT NULL REFERENCES workout_session(id) ON DELETE CASCADE,
                exercise_id     INT     NOT NULL,
                exercise_name   TEXT    NOT NULL,   -- денормализовано
                set_number      INT     NOT NULL,
                weight_kg       REAL    NOT NULL,
                reps            INT     NOT NULL,
                rest_s          INT,                -- фактическое время отдыха (если пользователь пропустил — NULL)
                new_max_kg      REAL,               -- проставляется после завершения упражнения
                comment         TEXT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_workout_set_session ON workout_set(session_id)")

        # --- schedule_entry ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schedule_entry (
                id              SERIAL  PRIMARY KEY,
                user_id         BIGINT  NOT NULL,
                template_id     INT     NOT NULL REFERENCES workout_template(id) ON DELETE CASCADE,
                weekday         SMALLINT,           -- 0=пн … 6=вс, NULL = без привязки к дню
                remind_time     TIME,               -- время напоминания, NULL = без напоминания
                UNIQUE(user_id, template_id, weekday)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_entry_user ON schedule_entry(user_id)")

        # Seed equipment
        for eq_key, name, weights in INITIAL_EQUIPMENT:
            await conn.execute("""
                INSERT INTO equipment (eq_key, name, available_weight)
                VALUES ($1, $2, $3)
                ON CONFLICT (eq_key) DO NOTHING
            """, eq_key, name, weights)


# ============================================================
# ОБОРУДОВАНИЕ
# ============================================================

async def get_all_equipment() -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT id, eq_key, name, available_weight FROM equipment ORDER BY name")


async def get_equipment_by_id(equipment_id: int) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT id, eq_key, name, available_weight FROM equipment WHERE id=$1", equipment_id)


async def create_equipment(name: str, weights: list[float]) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO equipment (eq_key, name, available_weight)
            VALUES ($1, $2, $3) RETURNING id
        """, name.lower().replace(" ", "_"), name, weights)
        return row["id"]


# ============================================================
# ШАБЛОНЫ ТРЕНИРОВОК
# ============================================================

async def get_user_workout_templates(user_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT id, name, use_weight_progress, schedule_order
            FROM workout_template
            WHERE user_id = $1
            ORDER BY COALESCE(schedule_order, 9999), name
        """, user_id)


async def create_workout_template(user_id: int, name: str, use_weight_progress: bool) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO workout_template (user_id, name, use_weight_progress)
            VALUES ($1, $2, $3) RETURNING id
        """, user_id, name, use_weight_progress)
        return row["id"]


async def get_workout_template(template_id: int, user_id: int) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT id, name, use_weight_progress, schedule_order
            FROM workout_template
            WHERE id=$1 AND user_id=$2
        """, template_id, user_id)


# ============================================================
# УПРАЖНЕНИЯ ШАБЛОНА
# ============================================================

async def get_exercise_templates(workout_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT et.id, et.name, et.order_index, et.default_sets,
                   et.default_reps, et.default_rest_s, et.comment,
                   e.id AS equipment_id, e.name AS equipment_name,
                   e.available_weight
            FROM exercise_template et
            LEFT JOIN equipment e ON e.id = et.equipment_id
            WHERE et.workout_id = $1
            ORDER BY et.order_index
        """, workout_id)


async def create_exercise_template(
    workout_id: int, equipment_id: int | None, name: str,
    order_index: int, default_sets: int, default_reps: int,
    default_rest_s: int, comment: str | None
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO exercise_template
                (workout_id, equipment_id, name, order_index,
                 default_sets, default_reps, default_rest_s, comment)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id
        """, workout_id, equipment_id, name, order_index,
             default_sets, default_reps, default_rest_s, comment)
        return row["id"]


# ============================================================
# ПОДХОДЫ ШАБЛОНА (переопределения)
# ============================================================

async def get_set_templates(exercise_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT set_number, reps, rest_s, weight_pct
            FROM set_template
            WHERE exercise_id = $1
            ORDER BY set_number
        """, exercise_id)


async def upsert_set_template(exercise_id: int, set_number: int,
                               reps: int | None, rest_s: int | None,
                               weight_pct: float | None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO set_template (exercise_id, set_number, reps, rest_s, weight_pct)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (exercise_id, set_number)
            DO UPDATE SET reps=EXCLUDED.reps, rest_s=EXCLUDED.rest_s, weight_pct=EXCLUDED.weight_pct
        """, exercise_id, set_number, reps, rest_s, weight_pct)


# ============================================================
# РАБОЧИЕ ВЕСА
# ============================================================

async def get_weight(user_id: int, exercise_id: int, default_kg: float) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT weight_kg FROM training_weight WHERE user_id=$1 AND exercise_id=$2",
            user_id, exercise_id
        )
        if row:
            return float(row["weight_kg"])
        await set_weight(user_id, exercise_id, default_kg)
        return default_kg


async def set_weight(user_id: int, exercise_id: int, weight_kg: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO training_weight (user_id, exercise_id, weight_kg, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (user_id, exercise_id)
            DO UPDATE SET weight_kg=EXCLUDED.weight_kg, updated_at=NOW()
        """, user_id, exercise_id, weight_kg)


# ============================================================
# СЕССИИ
# ============================================================

async def create_session(user_id: int, template_id: int, template_name: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO workout_session (user_id, template_id, template_name)
            VALUES ($1,$2,$3) RETURNING id
        """, user_id, template_id, template_name)
        return row["id"]


async def finish_session(session_id: int, status: str = "completed"):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE workout_session SET status=$1, finished_at=NOW() WHERE id=$2
        """, status, session_id)


async def save_set(session_id: int, exercise_id: int, exercise_name: str,
                   set_number: int, weight_kg: float, reps: int,
                   rest_s: int | None = None, new_max_kg: float | None = None,
                   comment: str | None = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO workout_set
                (session_id, exercise_id, exercise_name, set_number,
                 weight_kg, reps, rest_s, new_max_kg, comment)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, session_id, exercise_id, exercise_name, set_number,
             weight_kg, reps, rest_s, new_max_kg, comment)


async def update_set_new_max(session_id: int, exercise_id: int, new_max_kg: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE workout_set SET new_max_kg=$1
            WHERE session_id=$2 AND exercise_id=$3
        """, new_max_kg, session_id, exercise_id)


# ============================================================
# ИСТОРИЯ
# ============================================================

async def get_month_session(user_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT id, template_name, status, started_at
            FROM workout_session
            WHERE user_id=$1
              AND date_trunc('month', started_at) = date_trunc('month', NOW())
            ORDER BY started_at ASC
        """, user_id)


async def get_last_session_for_template(user_id: int, template_id: int) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT id, started_at FROM workout_session
            WHERE user_id=$1 AND template_id=$2 AND status='completed'
            ORDER BY started_at DESC LIMIT 1
        """, user_id, template_id)


# ============================================================
# РАСПИСАНИЕ
# ============================================================

async def get_schedule(user_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT se.id, se.weekday, se.remind_time,
                   wt.id AS template_id, wt.name AS template_name
            FROM schedule_entry se
            JOIN workout_template wt ON wt.id = se.template_id
            WHERE se.user_id = $1
            ORDER BY se.weekday NULLS LAST, se.remind_time
        """, user_id)


async def upsert_schedule_entry(user_id: int, template_id: int,
                                 weekday: int | None, remind_time: str | None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO schedule_entry (user_id, template_id, weekday, remind_time)
            VALUES ($1,$2,$3,$4::time)
            ON CONFLICT (user_id, template_id, weekday)
            DO UPDATE SET remind_time=EXCLUDED.remind_time
        """, user_id, template_id, weekday, remind_time)


async def delete_schedule_entry(user_id: int, template_id: int, weekday: int | None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM schedule_entry
            WHERE user_id=$1 AND template_id=$2
              AND (weekday=$3 OR ($3 IS NULL AND weekday IS NULL))
        """, user_id, template_id, weekday)


# ============================================================
# ЭКСПОРТ
# ============================================================


async def get_existing_weight(user_id: int, exercise_id: int) -> float | None:
    """Возвращает сохранённый вес или None если записи ещё нет."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT weight_kg FROM training_weight WHERE user_id=$1 AND exercise_id=$2",
            user_id, exercise_id
        )
        return float(row["weight_kg"]) if row else None


async def delete_workout_template(template_id: int):
    """Удаляет шаблон тренировки каскадно."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM workout_template WHERE id=$1", template_id
        )

async def export_user_data(user_id: int) -> dict:
    """Полный экспорт данных пользователя в dict (→ JSON или CSV)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        templates = await conn.fetch("""
            SELECT wt.id, wt.name, wt.use_weight_progress,
                   json_agg(
                       json_build_object(
                           'exercise', et.name,
                           'equipment', e.name,
                           'sets', et.default_sets,
                           'reps', et.default_reps,
                           'rest_s', et.default_rest_s,
                           'comment', et.comment
                       ) ORDER BY et.order_index
                   ) AS exercises
            FROM workout_template wt
            LEFT JOIN exercise_template et ON et.workout_id = wt.id
            LEFT JOIN equipment e ON e.id = et.equipment_id
            WHERE wt.user_id = $1
            GROUP BY wt.id
        """, user_id)

        sessions = await conn.fetch("""
            SELECT ws.id, ws.template_name, ws.status,
                   ws.started_at, ws.finished_at,
                   json_agg(
                       json_build_object(
                           'exercise', wset.exercise_name,
                           'set', wset.set_number,
                           'weight_kg', wset.weight_kg,
                           'reps', wset.reps,
                           'rest_s', wset.rest_s,
                           'new_max_kg', wset.new_max_kg
                       ) ORDER BY wset.id
                   ) AS sets
            FROM workout_session ws
            LEFT JOIN workout_set wset ON wset.session_id = ws.id
            WHERE ws.user_id = $1
            GROUP BY ws.id
            ORDER BY ws.started_at
        """, user_id)

        current_weights = await conn.fetch("""
            SELECT et.name AS exercise, tw.weight_kg, tw.updated_at
            FROM training_weight tw
            JOIN exercise_template et ON et.id = tw.exercise_id
            WHERE tw.user_id = $1
        """, user_id)

    return {
        "user_id": user_id,
        "workout_template": [dict(r) for r in templates],
        "workout_session": [dict(r) for r in sessions],
        "training_weight": [dict(r) for r in current_weights],
    }