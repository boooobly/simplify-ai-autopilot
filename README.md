# Telegram-бот модерации (MVP)

Простой MVP-бот для модерации контента в AI Telegram-канале.

## Возможности

- `/start` доступен только администратору
- `/draft` создаёт тестовый черновик и отправляет его на модерацию
- `/generate [source_url]` создаёт черновик для Telegram через OpenAI на русском для `@simplify_ai`
- Сгенерированные черновики используют правила стиля из `prompts/post_style.md` (человечный тон, краткость, без канцелярита)
- Кнопки модерации в интерфейсе:
  - ✅ Опубликовать
  - ❌ Отклонить
  - ✍️ Переписать
- Публикация одобренного контента в канал
- Сохранение текста черновика, статуса и `source_url` в SQLite
- Работа через long polling (подходит для Railway worker service)

## Структура проекта

```text
main.py
bot/
  config.py
  database.py
  handlers.py
  publisher.py
  drafts.py
  writer.py
prompts/
  post_style.md
data/
  .gitkeep
requirements.txt
.env.example
README.md
```

## Требования

- Python 3.11+
- Telegram bot token от BotFather
- OpenAI API key

## Установка

1. Клонируй репозиторий.
2. Создай и активируй виртуальное окружение.
3. Установи зависимости:

```bash
pip install -r requirements.txt
```

4. Создай env-файл:

```bash
cp .env.example .env
```

5. Заполни значения в `.env`:

- `BOT_TOKEN` — токен бота
- `ADMIN_ID` — числовой user ID администратора в Telegram
- `CHANNEL_ID` — username канала (пример: `@my_channel`) или id канала
- `OPENAI_API_KEY` — OpenAI API key для команды `/generate`

## Команды

- `/start` — приветствие бота (только для администратора)
- `/draft` — создание тестового черновика (для проверки)
- `/generate` — создание AI-черновика
- `/generate https://example.com/article` — создание AI-черновика и сохранение source URL в БД

`source_url` показывается в сообщениях модерации, чтобы администратор мог проверить контекст. Он не добавляется в пост автоматически, если только сам сгенерированный текст его не содержит.

## Локальный запуск

```bash
python main.py
```

## Примечания по деплою в Railway

Используй такую стартовую команду:

```bash
python main.py
```

Важно:
- Разворачивай бота как **worker/background service** (долгоживущий процесс).
- Задай переменные окружения в настройках проекта Railway:
  - `BOT_TOKEN`
  - `ADMIN_ID`
  - `CHANNEL_ID`
  - `OPENAI_API_KEY`

## Безопасность

- Никогда не коммить `.env` с реальными токенами и секретами.
- В `.env.example` должны быть только плейсхолдеры.
