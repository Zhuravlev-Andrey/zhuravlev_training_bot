-- 0001_initial_schema.sql
-- Начальная схема БД. Применяется автоматически через init_db() при первом запуске.
-- Здесь дублируется для версионирования истории изменений.

-- step: apply
CREATE TABLE IF NOT EXISTS equipment (
    id               SERIAL  PRIMARY KEY,
    eq_key           TEXT    UNIQUE NOT NULL,
    name             TEXT    NOT NULL,
    available_weight REAL[]  NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_template (
    id                  SERIAL      PRIMARY KEY,
    user_id             BIGINT      NOT NULL,
    name                TEXT        NOT NULL,
    use_weight_progress BOOLEAN     NOT NULL DEFAULT TRUE,
    schedule_order      INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_workout_template_user ON workout_template(user_id);

CREATE TABLE IF NOT EXISTS exercise_template (
    id              SERIAL  PRIMARY KEY,
    workout_id      INT     NOT NULL REFERENCES workout_template(id) ON DELETE CASCADE,
    equipment_id    INT     REFERENCES equipment(id) ON DELETE SET NULL,
    name            TEXT    NOT NULL,
    order_index     INT     NOT NULL,
    default_sets    INT     NOT NULL DEFAULT 4,
    default_reps    INT     NOT NULL DEFAULT 10,
    default_rest_s  INT     NOT NULL DEFAULT 90,
    comment         TEXT
);
CREATE INDEX IF NOT EXISTS idx_exercise_template_workout ON exercise_template(workout_id);

CREATE TABLE IF NOT EXISTS set_template (
    id          SERIAL  PRIMARY KEY,
    exercise_id INT     NOT NULL REFERENCES exercise_template(id) ON DELETE CASCADE,
    set_number  INT     NOT NULL,
    reps        INT,
    rest_s      INT,
    weight_pct  REAL,
    UNIQUE(exercise_id, set_number)
);

CREATE TABLE IF NOT EXISTS training_weight (
    user_id     BIGINT      NOT NULL,
    exercise_id INT         NOT NULL REFERENCES exercise_template(id) ON DELETE CASCADE,
    weight_kg   REAL        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, exercise_id)
);

CREATE TABLE IF NOT EXISTS workout_session (
    id            SERIAL      PRIMARY KEY,
    user_id       BIGINT      NOT NULL,
    template_id   INT         REFERENCES workout_template(id) ON DELETE SET NULL,
    template_name TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'active',
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_workout_session_user ON workout_session(user_id);

CREATE TABLE IF NOT EXISTS workout_set (
    id            SERIAL  PRIMARY KEY,
    session_id    INT     NOT NULL REFERENCES workout_session(id) ON DELETE CASCADE,
    exercise_id   INT     NOT NULL,
    exercise_name TEXT    NOT NULL,
    set_number    INT     NOT NULL,
    weight_kg     REAL    NOT NULL,
    reps          INT     NOT NULL,
    rest_s        INT,
    new_max_kg    REAL,
    comment       TEXT
);
CREATE INDEX IF NOT EXISTS idx_workout_set_session ON workout_set(session_id);

CREATE TABLE IF NOT EXISTS schedule_entry (
    id          SERIAL   PRIMARY KEY,
    user_id     BIGINT   NOT NULL,
    template_id INT      NOT NULL REFERENCES workout_template(id) ON DELETE CASCADE,
    weekday     SMALLINT,
    remind_time TIME,
    UNIQUE(user_id, template_id, weekday)
);
CREATE INDEX IF NOT EXISTS idx_schedule_entry_user ON schedule_entry(user_id);

-- step: rollback
-- ВНИМАНИЕ: удаляет все данные безвозвратно
DROP TABLE IF EXISTS schedule_entry;
DROP TABLE IF EXISTS workout_set;
DROP TABLE IF EXISTS workout_session;
DROP TABLE IF EXISTS training_weight;
DROP TABLE IF EXISTS set_template;
DROP TABLE IF EXISTS exercise_template;
DROP TABLE IF EXISTS workout_template;
DROP TABLE IF EXISTS equipment;
