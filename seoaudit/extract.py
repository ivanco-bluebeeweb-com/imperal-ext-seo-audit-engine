"""Извлечение SEO-полей из HTML — на html.parser, не на регулярках.

Почему это важно: на climtec.md регулярками легко было принять за canonical
случайный текст, а посчитать ДУБЛИ тега без нормального парсера практически
невозможно. Здесь считаем всё честно: сколько раз встретился тег, в каком
порядке, что в атрибутах.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

_WS = re.compile(r"\s+")


def collapse(s: str) -> str:
    return _WS.sub(" ", (s or "")).strip()


def visible_len(s: str) -> int:
    """Длина после схлопывания пробелов — так её видит поисковик."""
    return len(collapse(s))


@dataclass
class HeadData:
    """Всё, что нужно правилам, в одном месте."""

    title: str = ""
    title_count: int = 0
    description: str = ""
    description_count: int = 0
    canonical: str = ""
    canonical_all: list[str] = field(default_factory=list)
    robots: str = ""
    robots_all: list[str] = field(default_factory=list)
    html_lang: str = ""
    hreflang: list[dict[str, str]] = field(default_factory=list)
    h1: list[str] = field(default_factory=list)
    og_url: str = ""
    og_title: str = ""
    og_description: str = ""
    og_locale: str = ""
    viewport: str = ""
    charset: str = ""
    text_sample: str = ""       # начало видимого текста — для проверки языка
    text_len: int = 0
    word_count: int = 0
    links_internal: list[str] = field(default_factory=list)
    links_external: int = 0
    images_total: int = 0
    images_no_alt: int = 0
    json_ld_types: list[str] = field(default_factory=list)
    has_noscript_only: bool = False
    generator: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        # ссылки могут быть огромными — в БД храним ограниченно
        d["links_internal"] = self.links_internal[:300]
        d["text_sample"] = self.text_sample[:1500]
        return d

    @property
    def robots_tokens(self) -> set[str]:
        toks: set[str] = set()
        for r in self.robots_all or ([self.robots] if self.robots else []):
            for t in r.split(","):
                t = t.strip().lower()
                if t:
                    toks.add(t)
        return toks

    @property
    def is_noindex(self) -> bool:
        return "noindex" in self.robots_tokens


_SKIP_TEXT_TAGS = {"script", "style", "template", "svg", "noscript"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "section", "article", "header",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}


class _HeadParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.d = HeadData()
        self._stack: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._h1_depth = 0
        self._h1_parts: list[str] = []
        self._text: list[str] = []
        self._text_budget = 200_000
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []
        self._origin = self._origin_of(base_url)

    @staticmethod
    def _origin_of(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""

    # ------------------------------------------------------------------ handlers

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        self._stack.append(tag)

        if tag == "html":
            if not self.d.html_lang:
                self.d.html_lang = collapse(attrs.get("lang", ""))
        elif tag == "title":
            self._in_title = True
            self.d.title_count += 1
            self._title_parts = []
        elif tag == "meta":
            self._meta(attrs)
        elif tag == "link":
            self._link(attrs)
        elif tag == "h1":
            self._h1_depth += 1
            self._h1_parts = []
        elif tag == "img":
            self.d.images_total += 1
            if not collapse(attrs.get("alt", "")):
                self.d.images_no_alt += 1
        elif tag == "a":
            href = attrs.get("href", "").strip()
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                absolute = urljoin(self.base, href)
                if self._origin and absolute.startswith(self._origin):
                    if len(self.d.links_internal) < 3000:
                        self.d.links_internal.append(absolute)
                elif absolute.startswith(("http://", "https://")):
                    self.d.links_external += 1
        elif tag == "script":
            if (attrs.get("type", "") or "").lower().strip() == "application/ld+json":
                self._in_jsonld = True
                self._jsonld_parts = []

    def handle_startendtag(self, tag, attrs):  # <meta ... />
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._in_title:
            self._in_title = False
            text = collapse("".join(self._title_parts))
            if text and not self.d.title:
                self.d.title = text
        elif tag == "h1" and self._h1_depth > 0:
            self._h1_depth -= 1
            text = collapse("".join(self._h1_parts))
            if text:
                self.d.h1.append(text)
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self._collect_jsonld("".join(self._jsonld_parts))
        if tag in _BLOCK_TAGS and self._text_budget > 0:
            self._text.append(" ")
        while self._stack:
            popped = self._stack.pop()
            if popped == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_jsonld:
            self._jsonld_parts.append(data)
            return
        if self._h1_depth > 0:
            self._h1_parts.append(data)
        if self._text_budget <= 0:
            return
        cur = self._stack[-1] if self._stack else ""
        if cur in _SKIP_TEXT_TAGS or any(t in _SKIP_TEXT_TAGS for t in self._stack[-3:]):
            return
        chunk = data
        self._text.append(chunk)
        self._text_budget -= len(chunk)

    # -------------------------------------------------------------------- parts

    def _meta(self, attrs: dict[str, str]) -> None:
        name = (attrs.get("name") or "").lower().strip()
        prop = (attrs.get("property") or "").lower().strip()
        content = attrs.get("content", "")
        if "charset" in attrs and not self.d.charset:
            self.d.charset = collapse(attrs["charset"])
        if name == "description":
            self.d.description_count += 1
            if not self.d.description:
                self.d.description = collapse(content)
        elif name == "robots":
            self.d.robots_all.append(collapse(content))
            if not self.d.robots:
                self.d.robots = collapse(content)
        elif name == "viewport":
            self.d.viewport = collapse(content)
        elif name == "generator" and not self.d.generator:
            self.d.generator = collapse(content)
        elif name == "google-site-verification":
            pass
        if prop == "og:url" and not self.d.og_url:
            self.d.og_url = collapse(content)
        elif prop == "og:title" and not self.d.og_title:
            self.d.og_title = collapse(content)
        elif prop == "og:description" and not self.d.og_description:
            self.d.og_description = collapse(content)
        elif prop == "og:locale" and not self.d.og_locale:
            self.d.og_locale = collapse(content)
        # http-equiv="content-language"
        if (attrs.get("http-equiv") or "").lower() == "content-language" and not self.d.html_lang:
            self.d.html_lang = collapse(content)

    def _link(self, attrs: dict[str, str]) -> None:
        rels = {r.strip().lower() for r in (attrs.get("rel") or "").split()}
        href = (attrs.get("href") or "").strip()
        if not rels:
            return
        if "canonical" in rels and href:
            absolute = urljoin(self.base, href)
            self.d.canonical_all.append(absolute)
            if not self.d.canonical:
                self.d.canonical = absolute
        if "alternate" in rels and attrs.get("hreflang") and href:
            self.d.hreflang.append(
                {
                    "lang": collapse(attrs["hreflang"]).lower(),
                    "href": urljoin(self.base, href),
                }
            )

    def _collect_jsonld(self, raw: str) -> None:
        import json

        try:
            data = json.loads(raw.strip())
        except Exception:
            return

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                t = node.get("@type")
                if isinstance(t, str):
                    self.d.json_ld_types.append(t)
                elif isinstance(t, list):
                    self.d.json_ld_types.extend(str(x) for x in t)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)

    # ------------------------------------------------------------------- финал

    def finish(self) -> HeadData:
        text = collapse("".join(self._text))
        self.d.text_sample = text[:1500]
        self.d.text_len = len(text)
        self.d.word_count = len([w for w in text.split() if any(ch.isalnum() for ch in w)])
        # дедуп hreflang с сохранением порядка
        seen = set()
        uniq = []
        for h in self.d.hreflang:
            key = (h["lang"], h["href"])
            if key not in seen:
                seen.add(key)
                uniq.append(h)
        self.d.hreflang = uniq
        return self.d


def parse_head(html_text: str, base_url: str) -> HeadData:
    """Разобрать HTML. Никогда не бросает — битую разметку встречаем часто."""
    p = _HeadParser(base_url)
    try:
        p.feed(html_text or "")
    except Exception:
        pass  # добираем то, что успели распарсить
    try:
        p.close()
    except Exception:
        pass
    return p.finish()


# --------------------------------------------------------------- нормализация


def normalize_url(url: str, *, keep_query: bool = True) -> str:
    """Приведение URL к сравнимому виду.

    Осторожно: query по умолчанию СОХРАНЯЕМ — на многих сайтах он значим.
    Убираем только фрагмент, дефолтный порт и приводим хост к нижнему регистру.
    """
    if not url:
        return ""
    p = urlsplit(url.strip())
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    if not host:
        return url.strip()
    port = p.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = p.path or "/"
    query = p.query if keep_query else ""
    return urlunsplit((scheme, host, path, query, ""))


def same_url(a: str, b: str) -> bool:
    """Сравнение с точностью до завершающего слеша.

    Именно так надо сравнивать canonical с адресом страницы: /blog и /blog/ —
    для WordPress это одна и та же страница.
    """
    na, nb = normalize_url(a), normalize_url(b)
    if na == nb:
        return True
    return na.rstrip("/") == nb.rstrip("/")


def unescape(s: str) -> str:
    return html_mod.unescape(s or "")
