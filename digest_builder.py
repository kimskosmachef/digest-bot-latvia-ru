"""
Сборка дайджеста: группировка новостей, обращение к Claude API для пересказов,
формирование итогового текста с разбиением на части под лимит Telegram.
"""

import logging
from datetime import datetime
from typing import Optional

from anthropic import Anthropic, APIError

import config

log = logging.getLogger("digestbot.builder")


# ===== Промпты =====

_STYLE_PROMPTS = {
    "compact": "1-2 предложения, только суть",
    "detailed": "3-5 предложений с ключевыми деталями",
}

# Описание объёма пересказа для каждого уровня (0..3).
# Уровень считается как количество '+' после хэштега.
_LEVEL_PROMPTS = {
    0: "1-2 предложения, только самая суть",
    1: "3-5 предложений с ключевыми деталями",
    2: "1-2 абзаца (5-10 предложений) — развёрнутое изложение с фактами и контекстом",
    3: "несколько абзацев — подробное изложение со всеми важными деталями, "
       "цитатами и контекстом, как мини-статья",
}


def _single_summary_prompt(title: str, source: str, text: str) -> str:
    """Промпт для пересказа одиночной новости."""
    style = _STYLE_PROMPTS.get(config.SUMMARY_STYLE, _STYLE_PROMPTS["compact"])
    return (
        "Перескажи новость на русском языке в формате: "
        f"{style}. Без вводных фраз вроде «В статье говорится», "
        "сразу к фактам. Не добавляй заголовок и не повторяй его в начале — "
        "только сам пересказ.\n\n"
        f"Заголовок: {title}\n"
        f"Источник: {source}\n\n"
        f"Текст статьи:\n{text}"
    )


def _group_summary_prompt(tag: str, items_with_text: list, level: int = 0) -> str:
    """
    Промпт для сводного пересказа тематической группы.
    level — уровень развёрнутости (0..3).
    """
    style = _LEVEL_PROMPTS.get(level, _LEVEL_PROMPTS[0])

    articles_block = []
    for i, it in enumerate(items_with_text, 1):
        articles_block.append(
            f"=== Статья {i} ({it['source']}) ===\n"
            f"Заголовок: {it['title']}\n"
            f"Текст:\n{it['text']}\n"
        )

    return (
        f"Перед тобой несколько статей на одну тему «{tag}», "
        "опубликованных разными изданиями в один день. "
        "Сделай ОДИН сводный пересказ темы, объединяющий информацию из всех "
        "статей: общая картина события, ключевые факты, разные акценты "
        f"если есть. Объём пересказа: {style}. "
        "Не перечисляй статьи отдельно — это должен быть единый связный текст. "
        "\n\n"
        "ФОРМАТ ОТВЕТА:\n"
        "Первая строка — короткий заголовок темы (3-7 слов), обёрнутый в "
        "звёздочки для жирного шрифта Telegram: *Заголовок темы*\n"
        "Далее с новой строки — сам пересказ без вводных фраз, сразу к фактам. "
        "Не используй markdown-заголовки (# ## ###), не дублируй заголовок "
        "в тексте пересказа.\n\n"
        + "\n".join(articles_block)
    )


# ===== Вызов Claude API =====

def _ask_claude(prompt: str) -> str:
    """Вызвать Claude API и вернуть текстовый ответ."""
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.CLAUDE_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    # Ответ — список content-блоков, нас интересуют текстовые
    return "".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()


# ===== Telegram-форматирование =====

def _escape_md(text: str) -> str:
    """
    Минимальное экранирование для Telegram parse_mode=Markdown (НЕ MarkdownV2).
    Защищаемся от неприятностей в заголовках/тексте, не ломая ссылки.
    """
    # Старый Markdown в Telegram чувствителен к [_*`[]. Заменяем безопасно.
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _format_source_link(source: str, url: str) -> str:
    """Кликабельная ссылка с названием источника."""
    safe_source = _escape_md(source) if source else "Источник"
    return f"[{safe_source}]({url})"


# ===== Группировка =====

def _by_tag(items: list) -> dict[str, list]:
    """
    Сгруппировать новости по тегу. Возвращает {tag: [items]} в порядке появления.
    Новости без тега не включаются. Внутри каждой группы порядок — как в items.
    """
    by_tag: dict[str, list] = {}
    for it in items:
        if it.get("tag"):
            by_tag.setdefault(it["tag"], []).append(it)
    return by_tag


# ===== Главная функция =====

def build_digest(items: list, fetched_articles: dict, failed_items: list) -> list[str]:
    """
    Сформировать дайджест.

    Параметры:
        items            — список новостей из storage
        fetched_articles — {item_id: text} с успешно скачанными статьями
        failed_items     — список словарей {item, reason} для не загрузившихся

    Возвращает список частей сообщения (для отправки несколькими постами,
    если общий текст превышает TELEGRAM_MESSAGE_LIMIT).
    """
    today = datetime.now().strftime("%d.%m.%Y")
    parts: list[str] = []

    # Шапка
    header = f"📰 *Дайджест за {today}*\n"

    # Группировка — но в группы попадают только те, у которых есть текст
    items_with_text = [it for it in items if it["id"] in fetched_articles]

    # Группируем по тегам, определяем какие группы "настоящие" (>= MIN_GROUP_SIZE)
    by_tag = _by_tag(items_with_text)
    real_group_tags = {
        tag for tag, group in by_tag.items()
        if len(group) >= config.MIN_GROUP_SIZE
    }

    blocks: list[str] = []
    emitted_tags: set[str] = set()

    # Идём по items_with_text в том порядке, как они лежат в storage.
    # Это обеспечивает работу /maketop: первая новость в списке = первый блок в дайджесте.
    # Тег-группа выводится один раз — на позиции своего первого элемента в порядке items.
    for it in items_with_text:
        tag = it.get("tag")

        # Тегированная новость, входящая в реальную группу
        if tag and tag in real_group_tags:
            if tag in emitted_tags:
                continue
            emitted_tags.add(tag)
            group = by_tag[tag]
            max_level = max((x.get("level", 0) for x in group), default=0)
            log.info("Генерирую сводный пересказ для группы #%s (%d новостей, уровень %d)",
                     tag, len(group), max_level)
            items_with_full_text = [
                {**x, "text": fetched_articles[x["id"]]} for x in group
            ]
            try:
                summary = _ask_claude(_group_summary_prompt(tag, items_with_full_text, max_level))
            except APIError as e:
                log.error("Ошибка Claude API для группы #%s: %s", tag, e)
                summary = "_(не удалось сгенерировать пересказ темы)_"

            sources_links = " ".join(
                _format_source_link(x["source"], x["url"]) for x in group
            )
            block = (
                f"📌 {summary}\n\n"
                f"Источники: {sources_links}"
            )
            blocks.append(block)

        # Одиночная новость (без тега ИЛИ тег с группой меньше MIN_GROUP_SIZE)
        else:
            log.info("Генерирую пересказ для новости #%d", it["id"])
            try:
                summary = _ask_claude(_single_summary_prompt(
                    it["title"], it["source"], fetched_articles[it["id"]],
                ))
            except APIError as e:
                log.error("Ошибка Claude API для новости #%d: %s", it["id"], e)
                summary = "_(не удалось сгенерировать пересказ)_"

            title_safe = _escape_md(it["title"]) if it["title"] else "Без заголовка"
            source_link = _format_source_link(it["source"], it["url"])
            block = (
                f"▫️ *{title_safe}*\n"
                f"{summary}\n"
                f"{source_link}"
            )
            blocks.append(block)

    # Если ничего не вышло — короткий дайджест-заглушка
    if not blocks:
        body = header + "\n_Сегодня не удалось обработать ни одной новости._"
        if failed_items:
            body += "\n\nНе загрузились:\n" + "\n".join(
                f"• {f['item']['title'] or f['item']['url']} — {f['reason']}"
                for f in failed_items
            )
        return [body]

    # Добавляем шапку к первому блоку и собираем
    full_text = header + "\n" + "\n\n".join(blocks)

    # Список незагруженных — отдельным блоком в конце
    if failed_items:
        failed_block = "\n\n⚠️ _Не вошли в дайджест (не удалось загрузить):_\n" + "\n".join(
            f"• {_escape_md(f['item']['title'] or f['item']['url'])} — {f['reason']}"
            for f in failed_items
        )
        full_text += failed_block

    # Разбиение на части под лимит Telegram
    return _split_for_telegram(full_text, blocks, header, failed_items)


def _split_long_block(block: str, limit: int) -> list[str]:
    """
    Разбить один длинный блок на несколько кусков под лимит Telegram.
    Стараемся резать по границам абзацев (\n\n), потом по предложениям,
    в крайнем случае — просто по длине.
    """
    if len(block) <= limit:
        return [block]

    chunks: list[str] = []
    remaining = block

    while len(remaining) > limit:
        # Ищем разумную точку реза в пределах последних N символов до лимита
        slice_end = limit
        # Сначала пробуем найти конец абзаца
        cut = remaining.rfind("\n\n", 0, slice_end)
        if cut < limit // 2:  # слишком близко к началу — невыгодно
            # Пробуем конец предложения
            cut = max(
                remaining.rfind(". ", 0, slice_end),
                remaining.rfind("! ", 0, slice_end),
                remaining.rfind("? ", 0, slice_end),
            )
            if cut > 0:
                cut += 1  # включаем точку
        if cut < limit // 2:
            # В крайнем случае — режем по пробелу
            cut = remaining.rfind(" ", 0, slice_end)
        if cut < limit // 2:
            # Совсем нечего использовать — режем как есть
            cut = limit

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def _split_for_telegram(
    full_text: str,
    blocks: list[str],
    header: str,
    failed_items: list,
) -> list[str]:
    """
    Если итоговый текст помещается в один пост — возвращаем его одним элементом.
    Иначе разбиваем по границам блоков. Если отдельный блок сам по себе длиннее
    лимита — разбиваем его внутри по абзацам/предложениям.
    """
    limit = config.TELEGRAM_MESSAGE_LIMIT

    if len(full_text) <= limit:
        return [full_text]

    parts: list[str] = []
    current = header + "\n"

    for block in blocks:
        # Если один блок длиннее лимита — режем его на куски
        if len(block) > limit:
            # Сначала закрываем то, что уже накопилось
            if current.strip() and current.strip() != header.strip():
                parts.append(current.rstrip())
                current = ""
            # И добавляем куски длинного блока
            sub_chunks = _split_long_block(block, limit)
            for sub in sub_chunks:
                parts.append(sub)
            current = ""
            continue

        candidate = current + ("\n" + block if current.strip() else block)
        if len(candidate) > limit:
            # Закрываем текущую часть и начинаем новую
            if current.strip():
                parts.append(current.rstrip())
            current = block + "\n\n"
        else:
            current = candidate + "\n\n"

    # "Хвост" с незагруженными
    if failed_items:
        failed_block = "⚠️ _Не вошли в дайджест:_\n" + "\n".join(
            f"• {_escape_md(f['item']['title'] or f['item']['url'])} — {f['reason']}"
            for f in failed_items
        )
        candidate = current + "\n" + failed_block
        if len(candidate) > limit:
            if current.strip():
                parts.append(current.rstrip())
            # Незагруженных тоже может быть много — режем при необходимости
            for sub in _split_long_block(failed_block, limit):
                parts.append(sub)
            current = ""
        else:
            current = candidate

    if current.strip():
        parts.append(current.rstrip())

    return parts
