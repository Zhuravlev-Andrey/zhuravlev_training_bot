# zhuravlev_training_bot

Телеграм-бот для отслеживания силовых тренировок.

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TOKEN` | Токен бота от @BotFather |
| `DATABASE_URL` | URL PostgreSQL (Railway подставляет автоматически) |

## Деплой на Railway

1. Создать новый проект → Add Service → GitHub repo
2. Add Service → Database → PostgreSQL (Railway сам пропишет `DATABASE_URL`)
3. В Variables добавить `TOKEN`
4. Deploy — таблицы создадутся автоматически при первом запуске

## Файлы проекта

| Файл | Назначение |
|---|---|
| `bot.py` | Логика бота и handlers |
| `db.py` | Работа с PostgreSQL (asyncpg) |
| `Procfile` | Команда запуска для Railway |
| `requirements.txt` | Зависимости Python |
