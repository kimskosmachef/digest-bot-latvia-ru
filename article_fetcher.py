"""
Загрузка полного текста статьи по URL.

Стратегия в два прохода:
1. Сначала trafilatura — универсальный экстрактор основного контента.
   Хорошо работает на большинстве новостных сайтов без специальных правил.
2. Если trafilatura не справилась (вернула пусто или слишком короткий текст) —
   fallback на BeautifulSoup с теми же селекторами, что используются
   в основном проекте (newsbot/scraper.py): article p, .article-body p и т.д.

User-Agent и набор заголовков — те же, что в основном боте, плюс пара
дополнительных полей для пущей надёжности.
"""

import logging
from typing import Optional
from urllib.parse import urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

import config

log = logging.getLogger("digestbot.fetcher")

# Заголовки, имитирующие обычный браузер. Тот же UA, что в основном проекте.
# Важно: НЕ объявляем поддержку Brotli (br) — requests без отдельной библиотеки
# его не распакует, а некоторые сайты (например bb.lv) тогда отдадут именно br-сжатый
# ответ, и мы получим мусор вместо HTML.
HEADERS = {
    "User-Agent": config.ARTICLE_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Селекторы для fallback-извлечения текста (порядок имеет значение —
# от более специфичных к универсальным).
_FALLBACK_SELECTORS = [
    "article.article-data p",   # rus.jauns.lv
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

# Минимальная длина "значимого" текста в символах. Меньше — считаем что не получилось.
_MIN_TEXT_LENGTH = 200

# Минимальная длина одного абзаца, чтобы он считался содержательным.
# Слишком высокий порог отсекает короткие фразы на сайтах вроде jauns.lv,
# где новости иногда состоят из 1-2 коротких предложений.
_MIN_PARAGRAPH_LENGTH = 40


def _proxy_for_url(url: str) -> Optional[dict]:
    """
    Определить, нужен ли прокси для этого URL.
    Возвращает dict для requests (proxies=...) если домен в списке PROXY_DOMAINS
    и PROXY_URL задан. Иначе None (прямой доступ).
    """
    if not config.PROXY_URL:
        return None
    host = (urlparse(url).hostname or "").lower()
    for domain in config.PROXY_DOMAINS:
        if domain in host:
            return {"http": config.PROXY_URL, "https": config.PROXY_URL}
    return None


def _fetch_html(url: str) -> Optional[str]:
    """Скачать HTML страницы. Вернуть текст или None в случае ошибки."""
    proxies = _proxy_for_url(url)
    # Через прокси используем отдельный тайм-аут (телефон может быть офлайн).
    timeout = config.PROXY_FETCH_TIMEOUT if proxies else config.ARTICLE_FETCH_TIMEOUT

    if proxies:
        log.info("Запрос к %s идёт через прокси %s", url, config.PROXY_URL)

    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers=HEADERS,
            proxies=proxies,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        # Отдельно поясняем в логе, если падал именно прокси-запрос —
        # вероятная причина: телефон-exit-node офлайн.
        if proxies:
            log.warning("Прокси-запрос к %s не удался (телефон офлайн?): %s", url, e)
        else:
            log.warning("Не удалось загрузить %s: %s", url, e)
        return None

    # requests обычно правильно угадывает кодировку, но на всякий случай
    # подстрахуемся: если encoding явно не задан и контент — байты, дадим
    # ему шанс с apparent_encoding.
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"

    return response.text


def _extract_with_trafilatura(html: str) -> Optional[str]:
    """Универсальное извлечение основного контента."""
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if text and len(text.strip()) >= _MIN_TEXT_LENGTH:
        return text.strip()
    return None


def _extract_with_bs4(html: str) -> Optional[str]:
    """
    Fallback-извлечение: проходим по списку селекторов, собираем все абзацы
    длиной > 60 символов (как в основном боте), склеиваем в один текст.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        log.warning("BeautifulSoup не смог распарсить HTML: %s", e)
        return None

    for selector in _FALLBACK_SELECTORS:
        paragraphs = []
        for p in soup.select(selector):
            text = p.get_text(strip=True)
            if len(text) >= _MIN_PARAGRAPH_LENGTH:
                paragraphs.append(text)
        if paragraphs:
            combined = "\n\n".join(paragraphs)
            if len(combined) >= _MIN_TEXT_LENGTH:
                return combined
    return None


def fetch_article(url: str) -> Optional[str]:
    """
    Скачать статью и вернуть её основной текст.
    Возвращает None, если не удалось получить значимый текст.
    """
    html = _fetch_html(url)
    if html is None:
        return None

    # Сначала пробуем trafilatura
    text = _extract_with_trafilatura(html)
    if text:
        log.info("trafilatura извлекла %d симв. из %s", len(text), url)
    else:
        # Fallback на BeautifulSoup
        log.info("trafilatura не справилась с %s, пробую BeautifulSoup", url)
        text = _extract_with_bs4(html)
        if text:
            log.info("BeautifulSoup извлёк %d симв. из %s", len(text), url)
        else:
            log.warning("Ни trafilatura, ни BeautifulSoup не извлекли значимый текст из %s", url)
            return None

    # Обрезаем длинные статьи, чтобы не раздувать стоимость API
    if len(text) > config.ARTICLE_MAX_CHARS:
        text = text[:config.ARTICLE_MAX_CHARS] + "\n[...текст обрезан]"

    return text


def fetch_article_or_placeholder(url: str, title: str = "") -> tuple[Optional[str], Optional[str]]:
    """
    Удобная обёртка: возвращает (текст, причина_ошибки).
    Если текст получен — причина None. Если нет — текст None и описание причины.
    """
    text = fetch_article(url)
    if text:
        return text, None
    return None, "не удалось извлечь текст статьи"
