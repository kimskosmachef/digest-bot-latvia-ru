"""
Хранение списка накопленных за день новостей в JSON-файле.

Структура файла:
{
    "date": "2026-05-12",
    "items": [
        {
            "id": 1,
            "url": "https://...",
            "title": "Заголовок (если есть)",
            "source": "Источник (если удалось определить)",
            "tag": "выборы" | null,
            "level": 0,   // 0=стандарт, 1=средняя, 2=расширенная, 3=большая (для пересказа темы)
            "added_at": "2026-05-12T14:30:00"
        },
        ...
    ]
}

При смене даты файл автоматически переинициализируется (новый день — новый список).
"""

import json
import os
from datetime import datetime
from typing import Optional

import config


def _today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _empty_state() -> dict:
    return {"date": _today_iso(), "items": []}


def load() -> dict:
    """Загрузить состояние из файла. Если файл от предыдущего дня — сбросить."""
    if not os.path.exists(config.STATE_FILE):
        return _empty_state()

    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_state()

    # Если в файле дата не сегодняшняя — начинаем новый день с чистого листа
    if state.get("date") != _today_iso():
        return _empty_state()

    return state


def save(state: dict) -> None:
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def add_item(
    url: str,
    title: str = "",
    source: str = "",
    tag: Optional[str] = None,
    level: int = 0,
) -> int:
    """
    Добавить новость в список. Возвращает её порядковый номер (id).
    level — уровень развёрнутости сводки (0 = compact, 1 = medium, 2 = extended, 3 = large).
    Применяется только если у новости есть tag.
    """
    state = load()
    new_id = (max((item["id"] for item in state["items"]), default=0)) + 1
    state["items"].append({
        "id": new_id,
        "url": url,
        "title": title,
        "source": source,
        "tag": tag,
        "level": level,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    })
    save(state)
    return new_id


def remove_item(item_id: int) -> bool:
    """Удалить новость по id. Возвращает True если удалена."""
    state = load()
    before = len(state["items"])
    state["items"] = [it for it in state["items"] if it["id"] != item_id]
    if len(state["items"]) == before:
        return False
    save(state)
    return True


def set_tag(item_id: int, tag: Optional[str], level: int = 0) -> bool:
    """
    Установить или снять тег у новости.
    tag=None снимает тег (level тогда игнорируется).
    """
    state = load()
    for item in state["items"]:
        if item["id"] == item_id:
            item["tag"] = tag
            item["level"] = level if tag else 0
            save(state)
            return True
    return False


def clear() -> None:
    """Полностью очистить список."""
    save(_empty_state())


def get_items() -> list:
    return load()["items"]
