"""
Конфигурация бота-дайджеста.
Секреты (токены, ключи) берутся из .env, остальное задаётся прямо здесь.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ===== Секреты из .env =====
BOT_TOKEN = os.getenv("BOT_TOKEN")                  # Токен бота от @BotFather
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # Ключ Anthropic API

# ===== Telegram =====
# Канал-источник (откуда бот читает форварды)
SOURCE_CHANNEL = "@fghjlllkjd"

# Канал публикации дайджеста (на этапе тестирования)
TARGET_CHANNEL = "@vse_novosti_lv"

# Числовой Telegram ID владельца — только он может управлять ботом
OWNER_ID = 5793001545

# ===== Хранилище =====
# Файл со списком накопленных за день новостей
STATE_FILE = "digest_state.json"

# ===== Claude API =====
# Модель для генерации пересказов
CLAUDE_MODEL = "claude-sonnet-4-5"

# Максимум токенов в ответе на один вызов API.
CLAUDE_MAX_TOKENS = 8000
# Запас для расширенных сводок (#тема+++ может быть длинной).

# ===== Загрузка статей =====
# Тайм-аут запроса к сайту (секунд)
ARTICLE_FETCH_TIMEOUT = 15

# User-Agent для запросов
ARTICLE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Максимальная длина текста статьи для отправки в Claude (символов).
# Длинные статьи обрезаются, чтобы не раздувать расходы.
ARTICLE_MAX_CHARS = 8000

# ===== Параметры дайджеста =====
# Telegram-лимит на одно сообщение
TELEGRAM_MESSAGE_LIMIT = 4096

# Минимальный размер тематической группы
# (если новостей с тегом меньше — они идут как одиночные)
MIN_GROUP_SIZE = 2

# ===== Формат пересказов =====
# Стиль пересказа одиночной новости: "compact" (1-2 предложения) или "detailed" (3-5)
SUMMARY_STYLE = "compact"

# Стиль сводного пересказа тематической группы
GROUP_SUMMARY_STYLE = "compact"
