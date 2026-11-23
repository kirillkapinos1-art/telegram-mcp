# Telegram MCP Server & Gemini Auto-Responder

Локальный **MCP-сервер (`stdio`)** на Python + **Telethon (MTProto)** для работы с личным аккаунтом Telegram, а также реалтайм AI-автоответчик `auto_reply.py` на базе **Google Gemini API**.

## Возможности

### MCP-сервер (`server.py`)
- `telegram_status` — проверка конфигурации и авторизации сессии
- `list_dialogs` — список диалогов, групп и каналов с фильтрацией по непрочитанным/поиску
- `resolve_chat` — поиск чата по имени, `@username`, номеру или ID
- `get_messages` — чтение истории сообщений чата
- `search_global_messages` — глобальный поиск по всем доступным чатам
- `send_message` — отправка сообщения или ответа (`reply_to_msg_id`)
- `edit_message` — редактирование исходящего сообщения
- `mark_as_read` — отметка чата прочитанным

### Реалтайм AI-автоответчик (`auto_reply.py`)
- Слушает входящие сообщения в указанных чатах (`--chats` или `TELEGRAM_AUTOREPLY_CHATS`).
- Склеивает серии сообщений через настраиваемый дебаунс (`--debounce`, по умолчанию `4.0` сек).
- Анализирует контекст диалога (до 100 последних сообщений) + опциональный профиль стиля (`style_profile.txt`, шаблон в `style_profile.example.txt`).
- Поддерживает автоматический fallback моделей Gemini при 429/503 и режим `--dry-run`.

## Установка и авторизация

1. Установите зависимости:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Получите `api_id` и `api_hash` на [my.telegram.org/apps](https://my.telegram.org/apps), скопируйте `.env.example` в `.env` и заполните ключи.

3. Пройдите одноразовую авторизацию по QR-коду (или по номеру телефона с `--phone`):
   ```bash
   python auth.py --qr
   ```

4. Подключите MCP-сервер в конфигурации вашего клиента:
   ```json
   {
     "mcpServers": {
       "telegram": {
         "command": "/path/to/telegram-mcp/.venv/Scripts/python.exe",
         "args": ["/path/to/telegram-mcp/server.py"]
       }
     }
   }
   ```

5. Для реалтайм-автоответчика создайте отдельную сессию (чтобы не блокировать `telegram.session`):
   ```bash
   python auth.py --qr --session telegram_autoreply.session
   python auto_reply.py --chats @username1 123456789 --dry-run
   ```

## Тесты

```bash
pytest
```
