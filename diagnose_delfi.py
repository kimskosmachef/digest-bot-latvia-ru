"""
Углублённая диагностика структуры статьи (заточено под новый Delfi на Nuxt).
Запуск:
    python diagnose_delfi.py "URL"

Цель — понять, где на странице лежит ПОЛНЫЙ текст статьи и каким способом
его извлечь. Проверяет:
- HTTP статус, размер
- что находят разные селекторы (с подсчётом символов)
- есть ли в HTML JSON-данные Nuxt (__NUXT__ / __NUXT_DATA__) с текстом статьи
- ищет крупнейшие текстовые узлы
"""

import sys
import re
import json
import requests
from bs4 import BeautifulSoup

import config

HEADERS = {
    "User-Agent": config.ARTICLE_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Расширенный список селекторов, включая возможные для Nuxt-версии Delfi
SELECTORS = [
    "article p",
    "main p",
    ".article-body-container p",
    ".article-body p",
    "div.article-body-container p",
    "[class*='article-body'] p",
    "[class*='fragment'] p",
    "[class*='paragraph'] p",
    "[itemprop='articleBody'] p",
    "p",
]


def main():
    if len(sys.argv) < 2:
        print("Использование: python diagnose_delfi.py \"URL\"")
        sys.exit(1)

    url = sys.argv[1]
    print(f"URL: {url}\n")

    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except Exception as e:
        print(f"Ошибка запроса: {e}")
        return

    print(f"HTTP статус: {r.status_code}")
    print(f"Размер HTML: {len(r.text)} симв.\n")
    if r.status_code != 200:
        return

    soup = BeautifulSoup(r.text, "html.parser")

    # 1. Селекторы
    print("=== Селекторы (длинные абзацы >= 40 симв.) ===")
    best = None
    for sel in SELECTORS:
        try:
            nodes = soup.select(sel)
        except Exception:
            continue
        paras = [p.get_text(strip=True) for p in nodes]
        long_p = [p for p in paras if len(p) >= 40]
        total = sum(len(p) for p in long_p)
        if long_p:
            print(f"  [{total:6} симв., {len(long_p):2} абз.] {sel}")
            if best is None or total > best[1]:
                best = (sel, total)
        else:
            print(f"  [     0 симв.]        {sel}")
    if best:
        print(f"\n  Лучший селектор: '{best[0]}' -> {best[1]} симв.")
    print()

    # 2. Поиск данных Nuxt (часто весь текст статьи лежит в JSON внутри страницы)
    print("=== Поиск встроенных данных (Nuxt/JSON) ===")
    nuxt_found = False
    for script in soup.find_all("script"):
        content = script.string or ""
        if "__NUXT" in content or "articleBody" in content or "\"body\"" in content:
            nuxt_found = True
            print(f"  Найден <script> с потенциальными данными: {len(content)} симв.")
            # Поищем поле с телом статьи
            for key in ["articleBody", "\"body\"", "\"content\"", "\"text\""]:
                idx = content.find(key)
                if idx != -1:
                    snippet = content[idx:idx+200].replace("\n", " ")
                    print(f"    Рядом с {key}: {snippet[:160]}")
                    break
    print()

    # 2b. Попытка вытащить ПОЛНЫЙ articleBody через JSON-LD парсинг
    #     (ищем во всех script значение "articleBody":"...")
    print("=== Полный articleBody из <script> (regex) ===")
    full_html = r.text
    # Ищем "articleBody":"..." с учётом экранированных кавычек внутри
    m = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', full_html)
    if m:
        raw = m.group(1)
        # Расшифруем экранирование JSON
        try:
            decoded = json.loads('"' + raw + '"')
        except Exception:
            decoded = raw
        print(f"  Длина: {len(decoded)} симв.")
        print(f"  Начало: {decoded[:200]}")
        print(f"  ...")
        print(f"  Конец:  {decoded[-200:]}")
    else:
        print("  articleBody через regex не найден.")
    print()

    # 3. JSON-LD (структурированные данные, часто содержат articleBody целиком)
    print("=== JSON-LD (ld+json) ===")
    ld_found = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        # data может быть списком или объектом
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if isinstance(obj, dict) and "articleBody" in obj:
                body = obj["articleBody"]
                ld_found = True
                print(f"  articleBody в JSON-LD: {len(body)} симв.")
                print(f"  Превью: {body[:300]}")
    if not ld_found:
        print("  articleBody в JSON-LD не найден.")
    print()


if __name__ == "__main__":
    main()
