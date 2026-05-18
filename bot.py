"""
Главный файл бота-дайджеста.

Логика:
- Слушает канал-источник (SOURCE_CHANNEL) и сохраняет все приходящие туда сообщения
  (форварды + опциональный хэштег) в список через storage.py.
- Принимает команды от владельца (OWNER_ID) в личке:
    /start    — приветствие и текущий статус
    /list     — показать накопленный список
    /tag N hashtag — установить тег для новости N
    /tag N    — снять тег с новости N
    /remove N — удалить новость N из списка
    /clear    — очистить весь список
    /digest   — собрать дайджест: загрузить статьи, сгенерировать пересказы,
                опубликовать в TARGET_CHANNEL

Версия v0.2 — с подключённым Claude API и загрузкой полных текстов.
"""

import logging
import re
from typing import Optional

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import storage
import article_fetcher
import digest_builder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("digestbot")


# ===== Вспомогательные функции =====

def _owner_only(func):
    """Декоратор: пропускает только команды от OWNER_ID."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id != config.OWNER_ID:
            log.warning("Команда от чужого пользователя id=%s", user.id if user else None)
            return
        return await func(update, context)
    return wrapper


def _extract_url(text: str) -> Optional[str]:
    """Найти первый URL в тексте (для случая, когда форвард — это просто пересылка ссылки)."""
    if not text:
        return None
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


def _extract_hashtag(text: str) -> Optional[tuple[str, int]]:
    """
    Найти первый хэштег в тексте и вернуть (имя_темы, уровень_развёрнутости).

    Уровень = количество знаков '+' в конце имени:
        #выборы    -> ("выборы", 0)   стандартная сводка
        #выборы+   -> ("выборы", 1)   средняя
        #выборы++  -> ("выборы", 2)   расширенная
        #выборы+++ -> ("выборы", 3)   большая

    Все варианты — одна и та же тема, плюсы только регулируют объём пересказа.
    Возвращает None если хэштега не найдено.
    """
    if not text:
        return None
    # Слово после # + опционально несколько плюсов в конце
    match = re.search(r"#(\w+)(\++)?", text, flags=re.UNICODE)
    if not match:
        return None
    name = match.group(1).lower()
    pluses = match.group(2) or ""
    return name, len(pluses)


# ===== Обработчик сообщений в канале-источнике =====

# "Ожидающий" хэштег: если пришло сообщение только с #хэштегом (без URL),
# запоминаем его и привязываем к следующему форварду.
# Хранится в виде (имя_темы, уровень).
_pending_tag: Optional[tuple[str, int]] = None


def _parse_news_message(text: str) -> tuple[str, str, str]:
    """
    Разобрать пост из основного бота.

    Поддерживает два формата:
      1. Старый (без эмодзи):
            HH:MM | Источник
            Заголовок
            https://...
      2. Новый (с эмодзи, текущий в основном боте):
            🕐 HH:MM | Источник 📰 Заголовок 🔗 [🔁]
            https://...
         (всё в одной строке, эмодзи играют роль разделителей)

    Дополнительно: в любом формате может быть блок "Ранее по теме: ..." —
    его игнорируем.

    Возвращает (source, title, url). Любое поле может быть пустым, если не нашлось.
    """
    if not text:
        return "", "", ""

    # Отрезаем блок "Ранее по теме" — он содержит ссылку на канал @vse_novosti_lv
    # и не должен попадать ни в заголовок, ни как URL новости.
    text = re.split(r"Ранее по теме\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0]

    # Сразу выдёргиваем первый URL — это URL самой новости
    url_match = re.search(r"https?://\S+", text)
    url = url_match.group(0) if url_match else ""

    # Убираем URL из текста для разбора шапки/заголовка
    text_no_url = text.replace(url, "") if url else text

    source = ""
    title = ""

    # Сначала пробуем новый формат с эмодзи: "🕐 HH:MM | Источник 📰 Заголовок 🔗"
    # Эмодзи 🕐 (часы) и 📰 (газета) — устойчивые разделители; 🔗 и 🔁 — служебные хвосты.
    new_format = re.search(
        r"🕐\s*\d{1,2}:\d{2}\s*\|\s*(.+?)\s*📰\s*(.+?)(?:\s*🔗|\s*🔁|$)",
        text_no_url,
        flags=re.DOTALL,
    )
    if new_format:
        source = new_format.group(1).strip()
        title = new_format.group(2).strip()
    else:
        # Fallback на старый формат: "HH:MM | Источник\nЗаголовок"
        lines = [ln.strip() for ln in text_no_url.split("\n")]
        lines = [
            ln for ln in lines
            if ln and not re.fullmatch(r"(#\w+\s*)+", ln, flags=re.UNICODE)
        ]
        title_parts: list = []
        for ln in lines:
            header_match = re.match(r"^\d{1,2}:\d{2}\s*\|\s*(.+)$", ln)
            if header_match and not source:
                source = header_match.group(1).strip()
                continue
            title_parts.append(ln)
        title = " ".join(title_parts).strip()

    # Дополнительная чистка заголовка от служебных эмодзи и префиксов
    # (на случай, если какие-то эмодзи "просочились").
    title = re.sub(r"[🔗🔁📰🕐]", "", title).strip()
    title = re.sub(r"^[А-ЯЁA-Z0-9\s]+⟩\s*", "", title)
    title = title[:300]

    return source, title, url


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сообщение пришло в канал — сохраняем."""
    global _pending_tag

    msg = update.channel_post
    if msg is None:
        return

    # Фильтр по нашему каналу-источнику (на случай если бот вдруг попадёт в другие каналы)
    chat = msg.chat
    expected_username = config.SOURCE_CHANNEL.lstrip("@").lower()
    if (chat.username or "").lower() != expected_username:
        log.info("Игнорирую сообщение из чужого канала: %s", chat.username)
        return

    combined = msg.text or msg.caption or ""

    url = _extract_url(combined)
    parsed_tag = _extract_hashtag(combined)  # (имя, уровень) или None

    # Случай 1: только хэштег, без URL — запоминаем для следующего форварда
    if parsed_tag and not url:
        _pending_tag = parsed_tag
        log.info("Запомнил ожидающий тег: #%s (уровень %d)", parsed_tag[0], parsed_tag[1])
        return

    # Случай 2: нет URL — игнорируем
    if not url:
        log.warning("В сообщении нет URL — пропускаю. Текст: %r", combined[:200])
        return

    # Случай 3: есть URL — разбираем пост на источник/заголовок и сохраняем
    source, title, _parsed_url = _parse_news_message(combined)

    # Если хэштег пришёл в этом же сообщении — используем его, иначе подбираем ожидающий
    final_tag = parsed_tag or _pending_tag
    if _pending_tag and not parsed_tag:
        log.info("Привязываю ожидающий тег #%s (уровень %d) к новой новости",
                 _pending_tag[0], _pending_tag[1])
    _pending_tag = None  # сбрасываем в любом случае

    tag_name = final_tag[0] if final_tag else None
    tag_level = final_tag[1] if final_tag else 0

    new_id = storage.add_item(
        url=url, title=title, source=source, tag=tag_name, level=tag_level,
    )
    log.info("Добавлена новость #%d (tag=%s, level=%d, source=%s, title=%r)",
             new_id, tag_name, tag_level, source, title[:80])


# ===== Команды бота =====

@_owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = storage.get_items()
    text = (
        "👋 Привет! Я бот-дайджест.\n\n"
        f"Канал-источник: {config.SOURCE_CHANNEL}\n"
        f"Канал публикации: {config.TARGET_CHANNEL}\n\n"
        f"📊 Накоплено сегодня: {len(items)} новостей\n\n"
        "Команды:\n"
        "/list — показать список\n"
        "/tag N hashtag — поставить тег\n"
        "/tag N hashtag+ — средняя сводка\n"
        "/tag N hashtag++ — расширенная сводка\n"
        "/tag N hashtag+++ — большая сводка\n"
        "/tag N — снять тег\n"
        "/remove N — удалить новость\n"
        "/clear — очистить список\n"
        "/digest — собрать дайджест\n\n"
        "Хэштеги в канале-источнике тоже понимают плюсы:\n"
        "  #выборы — стандартная сводка\n"
        "  #выборы+ — средняя\n"
        "  #выборы++ — расширенная\n"
        "  #выборы+++ — большая"
    )
    await update.message.reply_text(text)


@_owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = storage.get_items()
    if not items:
        await update.message.reply_text("📭 Список пуст.")
        return

    lines = [f"📋 Список на сегодня ({len(items)} новостей):\n"]
    for it in items:
        if it.get("tag"):
            plus_str = "+" * it.get("level", 0)
            tag_str = f" #{it['tag']}{plus_str}"
        else:
            tag_str = ""
        title = it["title"] or it["url"]
        # Обрезаем длинный заголовок для компактности
        if len(title) > 80:
            title = title[:77] + "..."
        lines.append(f"{it['id']}. [{it['source']}] {title}{tag_str}")
    await update.message.reply_text("\n".join(lines))


@_owner_only
async def cmd_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование:\n"
            "/tag N hashtag — поставить тег\n"
            "/tag N hashtag+ — средняя сводка\n"
            "/tag N hashtag++ — расширенная сводка\n"
            "/tag N hashtag+++ — большая сводка\n"
            "/tag N — снять тег"
        )
        return
    try:
        item_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Первый аргумент должен быть числом — id новости.")
        return

    if len(args) > 1:
        # Отделяем имя темы от плюсов
        raw = args[1].lstrip("#")
        match = re.match(r"^(\w+)(\++)?$", raw, flags=re.UNICODE)
        if not match:
            await update.message.reply_text(
                "Тег должен состоять из букв/цифр, опционально с '+' в конце."
            )
            return
        new_tag = match.group(1).lower()
        new_level = len(match.group(2) or "")
    else:
        new_tag = None
        new_level = 0

    ok = storage.set_tag(item_id, new_tag, new_level)
    if not ok:
        await update.message.reply_text(f"Новости #{item_id} нет в списке.")
        return
    if new_tag:
        plus_str = "+" * new_level
        await update.message.reply_text(
            f"✅ Новости #{item_id} установлен тег: #{new_tag}{plus_str}"
        )
    else:
        await update.message.reply_text(f"✅ С новости #{item_id} тег снят.")


@_owner_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /remove N")
        return
    try:
        item_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Аргумент должен быть числом — id новости.")
        return
    ok = storage.remove_item(item_id)
    if ok:
        await update.message.reply_text(f"🗑 Новость #{item_id} удалена.")
    else:
        await update.message.reply_text(f"Новости #{item_id} нет в списке.")


@_owner_only
async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage.clear()
    await update.message.reply_text("🧹 Список полностью очищен.")


@_owner_only
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    v0.2 — собирает реальный дайджест.

    Этапы:
    1. Берём список накопленных новостей.
    2. Загружаем полный текст каждой статьи через trafilatura.
    3. Генерируем пересказы (одиночные + сводные для тематических групп) через Claude API.
    4. Публикуем результат в TARGET_CHANNEL, разбивая на части под лимит Telegram.
    5. Если что-то не загрузилось — сообщаем владельцу и добавляем список в конец дайджеста.
    """
    items = storage.get_items()
    if not items:
        await update.message.reply_text("📭 Список пуст — собирать нечего.")
        return

    if not config.ANTHROPIC_API_KEY:
        await update.message.reply_text(
            "❌ ANTHROPIC_API_KEY не задан в .env — сгенерировать пересказы не получится."
        )
        return

    await update.message.reply_text(
        f"⏳ Начинаю сборку дайджеста ({len(items)} новостей). "
        "Загружаю статьи и генерирую пересказы — это займёт минуту-другую."
    )

    # Этап 1: загрузка статей
    fetched: dict[int, str] = {}
    failed: list = []
    for it in items:
        log.info("Загружаю статью #%d: %s", it["id"], it["url"])
        text, error = article_fetcher.fetch_article_or_placeholder(it["url"], it["title"])
        if text:
            fetched[it["id"]] = text
        else:
            failed.append({"item": it, "reason": error or "неизвестная ошибка"})

    if failed:
        failed_summary = "\n".join(
            f"• #{f['item']['id']} {f['item']['title'] or f['item']['url']}"
            for f in failed
        )
        await update.message.reply_text(
            f"⚠️ Не удалось загрузить {len(failed)} из {len(items)} статей:\n{failed_summary}\n\n"
            "Они будут отмечены в конце дайджеста. Продолжаю."
        )

    if not fetched:
        await update.message.reply_text(
            "❌ Ни одной статьи не загрузилось — дайджест не будет опубликован."
        )
        return

    # Этап 2: генерация дайджеста (это синхронный код с обращениями к Claude — может занять время)
    # Запускаем в отдельном потоке, чтобы не блокировать event loop бота.
    import asyncio
    import os
    from datetime import datetime
    try:
        loop = asyncio.get_running_loop()
        parts = await loop.run_in_executor(
            None,
            digest_builder.build_digest,
            items, fetched, failed,
        )
    except Exception as e:
        log.exception("Ошибка при сборке дайджеста")
        await update.message.reply_text(f"❌ Ошибка при сборке: {e}")
        return

    # Сохраняем сгенерированный текст на диск ДО публикации.
    # Это страховка: если публикация упадёт (длина, ошибка форматирования и т.п.),
    # текст не пропадёт.
    digest_filename = f"digest_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    try:
        with open(digest_filename, "w", encoding="utf-8") as f:
            f.write("\n\n----- РАЗРЫВ СООБЩЕНИЯ -----\n\n".join(parts))
        log.info("Дайджест сохранён в %s", digest_filename)
    except OSError as e:
        log.warning("Не удалось сохранить дайджест в файл: %s", e)

    # Этап 3: публикация в целевой канал
    try:
        for i, part in enumerate(parts, 1):
            log.info("Отправляю часть %d/%d (длина %d симв.)", i, len(parts), len(part))
            await context.bot.send_message(
                chat_id=config.TARGET_CHANNEL,
                text=part,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            log.info("Опубликована часть %d/%d", i, len(parts))
    except Exception as e:
        log.exception("Ошибка публикации в канал")
        await update.message.reply_text(
            f"❌ Ошибка публикации: {e}\n\n"
            f"📄 Сгенерированный дайджест сохранён в файле {digest_filename} "
            "(в каталоге бота)."
        )
        # Если файл сохранился — отправим его владельцу в личку
        if os.path.exists(digest_filename):
            try:
                with open(digest_filename, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=digest_filename,
                        caption="Текст дайджеста, который не удалось опубликовать."
                    )
            except Exception as e2:
                log.warning("Не удалось отправить файл с дайджестом: %s", e2)
        return

    await update.message.reply_text(
        f"✅ Дайджест опубликован в {config.TARGET_CHANNEL} ({len(parts)} сообщений)."
    )


# ===== Запуск =====

def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Проверьте .env")

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Команды (работают в личке)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("tag", cmd_tag))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("digest", cmd_digest))

    # Сообщения в канале-источнике
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    log.info("Бот запущен. Источник: %s, Цель: %s", config.SOURCE_CHANNEL, config.TARGET_CHANNEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
