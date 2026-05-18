"""
Диагностический скрипт. Запускать прямо из каталога digestbot
с активированным venv:
    python diagnose_url.py <URL>

Покажет:
- HTTP статус и размер ответа
- что извлекла trafilatura
- какие селекторы BS4 нашли значимый текст
- если ничего не нашло — какие классы у крупных контейнеров (article, main, div)
"""

import sys
import requests
import trafilatura
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

SELECTORS = [
    "article p",
    ".article-body p",
    ".article__body p",
    ".article-content p",
    ".content p",
    ".text p",
    ".entry-content p",
    "main p",
    "p",
]

def main():
    if len(sys.argv) < 2:
        print("Использование: python diagnose_url.py <URL>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"URL: {url}")
    print()

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"Ошибка запроса: {e}")
        return

    print(f"HTTP статус: {r.status_code}")
    print(f"Content-Type: {r.headers.get('Content-Type')}")
    print(f"Размер HTML: {len(r.text)} симв.")
    print()

    if r.status_code != 200:
        print("Сервер не отдал HTML — диагностика дальше не имеет смысла.")
        return

    # 1. trafilatura
    text = trafilatura.extract(
        r.text, include_comments=False, include_tables=False, favor_precision=True,
    )
    print(f"=== trafilatura ===")
    print(f"Извлечено: {len(text) if text else 0} симв.")
    if text:
        print(f"Первые 400 симв.:\n{text[:400]}")
    print()

    # 2. BeautifulSoup по селекторам
    print(f"=== BeautifulSoup селекторы ===")
    soup = BeautifulSoup(r.text, "html.parser")
    for sel in SELECTORS:
        paragraphs = [p.get_text(strip=True) for p in soup.select(sel)]
        long_p = [p for p in paragraphs if len(p) > 60]
        if long_p:
            total = sum(len(p) for p in long_p)
            mark = "[+]" if total >= 200 else "[?]"
            print(f"  {mark} '{sel}': {len(long_p)} абзацев, {total} симв.")
            if total >= 200:
                print(f"     Превью: {long_p[0][:200]}")
        else:
            print(f"  [-] '{sel}': пусто")
    print()

    # 3. Если основные селекторы пустые — посмотрим что вообще есть на странице
    print(f"=== Структура страницы ===")
    article = soup.find("article")
    print(f"<article>: {'есть' if article else 'нет'}")
    if article:
        cls = article.get("class")
        print(f"   classes: {cls}")
        print(f"   текст: {len(article.get_text(strip=True))} симв.")
        print(f"   <p> внутри: {len(article.find_all('p'))}")
    main_tag = soup.find("main")
    print(f"<main>: {'есть' if main_tag else 'нет'}")
    if main_tag:
        cls = main_tag.get("class")
        print(f"   classes: {cls}")
        print(f"   <p> внутри: {len(main_tag.find_all('p'))}")

    # Крупные div'ы по классам
    print()
    print("Крупные div'ы (текст > 1000 симв.):")
    for div in soup.find_all("div", recursive=True):
        text_len = len(div.get_text(strip=True))
        if 1000 < text_len < 10000:
            cls = div.get("class")
            if cls:
                print(f"   <div class={cls}>: {text_len} симв.")


if __name__ == "__main__":
    main()
