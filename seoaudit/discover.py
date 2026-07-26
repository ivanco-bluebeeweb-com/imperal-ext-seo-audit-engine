"""Обнаружение URL сайта: robots.txt -> sitemap -> список страниц.

Почему сначала sitemap, а не обход по ссылкам: на 20-200 сайтах полный краул
неприемлемо долог и груб к серверам клиента. Карта сайта даёт готовый список
страниц, которые владелец САМ считает важными, за 2-3 запроса.

Обход по ссылкам остаётся как резерв (fallback) и как способ найти страницы,
которых в карте нет — на climtec.md именно так обнаружилось, что 9 товаров
отсутствуют в sitemap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .fetcher import Fetcher, Robots

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAPINDEX = re.compile(r"<sitemapindex", re.I)
_LASTMOD = re.compile(
    r"<url>(?:(?!</url>).)*?<loc>\s*([^<\s]+)\s*</loc>"
    r"(?:(?!</url>).)*?(?:<lastmod>\s*([^<\s]+)\s*</lastmod>)?"
    r"(?:(?!</url>).)*?</url>",
    re.I | re.S,
)

# Кандидаты карты сайта, если в robots.txt её не объявили.
SITEMAP_GUESSES = (
    "/sitemap_index.xml",
    "/sitemap.xml",
    "/wp-sitemap.xml",
    "/sitemap-index.xml",
    "/sitemap1.xml",
)

# Расширения, которые заведомо не HTML-страницы.
_SKIP_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".css", ".js", ".mjs", ".map",
    ".pdf", ".zip", ".gz", ".rar", ".7z", ".tar",
    ".mp3", ".mp4", ".avi", ".mov", ".webm", ".wav", ".ogg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


def normalize_url(url: str, *, drop_fragment: bool = True) -> str:
    """Приводит URL к сравнимому виду: без фрагмента, с нормализованным хостом."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    # убрать стандартный порт
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    elif netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    path = parts.path or "/"
    frag = "" if drop_fragment else parts.fragment
    return urlunsplit((scheme, netloc, path, parts.query, frag))


def is_html_candidate(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return not path.endswith(_SKIP_EXT)


def same_site(url: str, origin: str) -> bool:
    """Тот же сайт? www и без www считаем одним сайтом."""
    a, b = urlsplit(url).netloc.lower(), urlsplit(origin).netloc.lower()
    return a.removeprefix("www.") == b.removeprefix("www.")


@dataclass
class Discovery:
    origin: str
    robots: Robots
    robots_url: str = ""
    robots_txt: str = ""
    robots_status: int | None = None
    sitemaps_seen: list[str] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    sitemap_lastmod: dict[str, str] = field(default_factory=dict)
    sitemap_errors: list[dict[str, Any]] = field(default_factory=list)
    sitemap_discovered_via: str = ""     # robots | guess | none
    urls: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "robots_present": self.robots.present,
            "robots_url": self.robots_url,
            "robots_status": self.robots_status,
            "robots_rules": len(self.robots.rules),
            "robots_sitemaps": list(self.robots.sitemaps),
            "robots_crawl_delay": self.robots.crawl_delay,
            "sitemaps_seen": self.sitemaps_seen,
            "sitemap_url_count": len(self.sitemap_urls),
            "sitemap_errors": self.sitemap_errors,
            "sitemap_discovered_via": self.sitemap_discovered_via,
            "url_count": len(self.urls),
            "notes": self.notes,
        }


def _parse_sitemap(text: str) -> tuple[bool, list[str], dict[str, str]]:
    """Возвращает (это_индекс, список_loc, lastmod-по-url)."""
    is_index = bool(_SITEMAPINDEX.search(text or ""))
    locs = [m.strip() for m in _LOC.findall(text or "")]
    lastmod: dict[str, str] = {}
    if not is_index:
        for loc, lm in _LASTMOD.findall(text or ""):
            if lm:
                lastmod[loc.strip()] = lm.strip()
    return is_index, locs, lastmod


def discover(
    fetcher: Fetcher,
    origin: str,
    *,
    max_urls: int = 2000,
    max_sitemaps: int = 60,
    respect_robots: bool = True,
) -> Discovery:
    """robots.txt -> карты сайта -> список URL-кандидатов."""
    origin = normalize_url(origin).rstrip("/")
    if not origin:
        raise ValueError("пустой origin")

    robots_url = f"{origin}/robots.txt"
    rr = fetcher.fetch(robots_url)
    robots = Robots.parse(rr.get("text") or "" if rr.get("ok") else "")
    d = Discovery(
        origin=origin,
        robots=robots,
        robots_url=robots_url,
        robots_txt=(rr.get("text") or "") if rr.get("ok") else "",
        robots_status=rr.get("status"),
    )

    # 1) карты из robots.txt
    queue: list[str] = []
    if robots.sitemaps:
        queue = [normalize_url(s) for s in robots.sitemaps]
        d.sitemap_discovered_via = "robots"
    else:
        # 2) угадать по стандартным путям
        for guess in SITEMAP_GUESSES:
            cand = origin + guess
            r = fetcher.fetch(cand)
            if r.get("ok") and (r.get("status") == 200) and _LOC.search(r.get("text") or ""):
                queue = [cand]
                d.sitemap_discovered_via = "guess"
                break
        if not queue:
            d.sitemap_discovered_via = "none"

    # 3) развернуть карты (индексы -> вложенные), с защитой от цикла
    seen_sm: set[str] = set()
    pages: dict[str, None] = {}
    while queue and len(seen_sm) < max_sitemaps:
        sm = normalize_url(queue.pop(0))
        if not sm or sm in seen_sm:
            continue
        seen_sm.add(sm)
        d.sitemaps_seen.append(sm)
        r = fetcher.fetch(sm)
        if not r.get("ok") or r.get("status") != 200:
            d.sitemap_errors.append(
                {"url": sm, "status": r.get("status"), "error": r.get("error", "")}
            )
            continue
        is_index, locs, lastmod = _parse_sitemap(r.get("text") or "")
        if is_index:
            for loc in locs:
                if len(seen_sm) + len(queue) < max_sitemaps:
                    queue.append(loc)
        else:
            d.sitemap_lastmod.update(lastmod)
            for loc in locs:
                u = normalize_url(loc)
                if u and same_site(u, origin) and is_html_candidate(u):
                    pages.setdefault(u, None)

    d.sitemap_urls = list(pages)

    # 4) домашняя страница обязательно в списке
    home = origin + "/"
    urls: dict[str, None] = {normalize_url(home): None}
    for u in d.sitemap_urls:
        urls.setdefault(u, None)

    # 5) фильтр по robots (аудит не должен требовать того, что запрещено)
    result: list[str] = []
    blocked = 0
    for u in urls:
        if respect_robots and not robots.allowed(urlsplit(u).path or "/"):
            blocked += 1
            continue
        result.append(u)
        if len(result) >= max_urls:
            break

    d.urls = result
    d.notes = {
        "blocked_by_robots": blocked,
        "sitemap_had_home": home in d.sitemap_urls or normalize_url(home) in d.sitemap_urls,
        "truncated": len(urls) > max_urls,
    }
    return d


def expand_by_links(
    fetcher: Fetcher,
    origin: str,
    known: list[str],
    internal_links: dict[str, list[str]],
    *,
    limit: int = 200,
    respect_robots: bool = True,
    robots: Robots | None = None,
) -> list[str]:
    """Найти страницы, которых НЕТ в карте сайта.

    Именно этот путь показал на climtec.md, что 9 товаров отсутствуют в
    sitemap: они находятся по внутренним ссылкам, но карта их не содержит.
    """
    known_set = {normalize_url(u) for u in known}
    found: dict[str, None] = {}
    for _src, links in internal_links.items():
        for raw in links:
            u = normalize_url(raw)
            if not u or u in known_set or u in found:
                continue
            if not same_site(u, origin) or not is_html_candidate(u):
                continue
            if respect_robots and robots and not robots.allowed(urlsplit(u).path or "/"):
                continue
            found[u] = None
            if len(found) >= limit:
                return list(found)
    return list(found)
