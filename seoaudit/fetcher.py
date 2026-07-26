"""Загрузка страниц: вежливо, устойчиво, без внешних зависимостей.

Главный риск на 20-200 сайтах — превратить аудит в DDoS клиентских серверов.
Поэтому: лимит одновременных запросов НА ХОСТ, пауза между запросами к одному
хосту, ограничение размера тела, повторы только на осмысленных ошибках.
"""

from __future__ import annotations

import gzip
import socket
import ssl
import threading
import codecs
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

USER_AGENT = (
    "ImperalSEOAudit/0.1 (+https://imperal.io; site audit on behalf of site owner)"
)


def _with_cache_buster(url: str) -> str:
    """Добавляет безвредный уникальный параметр, чтобы промахнуться по кешу.

    Нужно, чтобы УВИДЕТЬ свежую версию страницы и сравнить её с той, которую
    получает посетитель. Осторожно: считать «живым HTML» именно такой ответ
    НЕЛЬЗЯ — на climtec.md это и создало иллюзию, что правки уже видны.
    """
    marker = f"_ia={int(time.time() * 1000) % 100000}"
    parts = urlsplit(url)
    query = f"{parts.query}&{marker}" if parts.query else marker
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


# Заголовки, по которым видно, что ответ пришёл из кеша.
_CACHE_HEADERS = (
    ("x-litespeed-cache", "LiteSpeed"),
    ("x-lsadc-cache", "LiteSpeed"),
    ("cf-cache-status", "Cloudflare"),
    ("x-cache", "CDN/прокси"),
    ("x-proxy-cache", "Nginx"),
    ("x-nginx-cache", "Nginx"),
    ("x-fastcgi-cache", "Nginx FastCGI"),
    ("x-cache-status", "кеш"),
    ("x-wp-super-cache", "WP Super Cache"),
    ("x-kinsta-cache", "Kinsta"),
    ("x-sg-cachehit", "SiteGround"),
)


def read_cache_headers(headers: dict[str, str]) -> tuple[str, str]:
    """Возвращает (состояние, имя слоя): состояние = hit|miss|''."""
    for name, layer in _CACHE_HEADERS:
        raw = (headers.get(name) or "").strip().lower()
        if not raw:
            continue
        if "hit" in raw:
            return "hit", layer
        if "miss" in raw or "expired" in raw or "bypass" in raw or "dynamic" in raw:
            return "miss", layer
        return raw[:20], layer
    return "", ""

MAX_BYTES = 1_500_000  # больше для <head> не нужно, а память экономит
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchPolicy:
    """Настройки вежливости. Значения по умолчанию безопасны для прод-сайтов."""

    timeout: float = 20.0
    per_host_concurrency: int = 2
    per_host_delay: float = 0.35  # пауза между запросами к одному хосту
    retries: int = 2
    retry_backoff: float = 1.5
    max_bytes: int = MAX_BYTES
    verify_tls: bool = True


class _HostGate:
    """Пропускает к одному хосту не больше N запросов и держит паузу."""

    def __init__(self, policy: FetchPolicy):
        self.policy = policy
        self._sems: dict[str, threading.Semaphore] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            if host not in self._sems:
                self._sems[host] = threading.Semaphore(
                    max(1, self.policy.per_host_concurrency)
                )
            return self._sems[host]

    def __enter__(self):  # не используется напрямую
        raise NotImplementedError

    def acquire(self, host: str) -> None:
        self._sem(host).acquire()
        delay = self.policy.per_host_delay
        if delay <= 0:
            return
        while True:
            with self._lock:
                last = self._last.get(host, 0.0)
                now = time.monotonic()
                wait = last + delay - now
                if wait <= 0:
                    self._last[host] = now
                    return
            time.sleep(min(wait, delay))

    def release(self, host: str) -> None:
        self._sem(host).release()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Считаем переходы сами, чтобы видеть цепочку редиректов."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _decode_body(raw: bytes, encoding: str) -> bytes:
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


def _charset_from(content_type: str, body: bytes) -> str:
    ct = (content_type or "").lower()
    if "charset=" in ct:
        cs = ct.split("charset=", 1)[1].split(";")[0].strip().strip('"\'')
        # Значение из заголовка сервера тоже бывает мусорным, а неизвестная
        # кодировка валит загрузку страницы целиком. Доверяем только тому,
        # что Python действительно умеет декодировать.
        if _is_known_encoding(cs):
            return cs
    head = body[:4096].lower()
    for marker in (b'charset="', b"charset='", b"charset="):
        i = head.find(marker)
        if i != -1:
            tail = head[i + len(marker) :]
            # ВАЖНО: на разделителе нужно ОСТАНОВИТЬСЯ, а не выбросить его.
            # Если просто отфильтровать кавычки и '>', то у сайта с плотной
            # разметкой (charset="utf-8"/><meta ...) в имя кодировки склеится
            # соседний тег и получится «unknown encoding: utf-8<metaname=...».
            # Из-за этого страницы icnli.org не загружались вообще, а правила
            # судили пустые записи и выдавали ложные находки.
            cs = bytes(
                _take_until(tail[:60], b'"\'>;, \t\r\n/')
            ).decode("ascii", "ignore").strip()
            if _is_known_encoding(cs):
                return cs
    return "utf-8"


def _take_until(data: bytes, stoppers: bytes) -> bytes:
    """Байты до первого разделителя (сам разделитель не включается)."""
    out = bytearray()
    for c in data:
        if c in stoppers:
            break
        out.append(c)
    return bytes(out)


def _is_known_encoding(name: str) -> bool:
    """Проверяет, что Python знает такую кодировку.

    Без проверки любое кривое значение в HTML валит загрузку страницы
    исключением LookupError, и сайт целиком выпадает из аудита.
    """
    if not name:
        return False
    try:
        codecs.lookup(name)
        return True
    except (LookupError, TypeError, ValueError):
        return False


class Fetcher:
    """Тонкий HTTP-клиент поверх urllib с вежливыми лимитами."""

    def __init__(self, policy: FetchPolicy | None = None):
        self.policy = policy or FetchPolicy()
        self.gate = _HostGate(self.policy)
        if self.policy.verify_tls:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()  # noqa: SLF001
        self.opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ctx)
        )

    # ------------------------------------------------------------------ низкий

    def _once(self, url: str, method: str) -> dict[str, Any]:
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        req.add_header("Accept-Encoding", "gzip, deflate")
        req.add_header("Accept-Language", "*")
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=self.policy.timeout) as resp:
                raw = resp.read(self.policy.max_bytes + 1) if method == "GET" else b""
                truncated = len(raw) > self.policy.max_bytes
                raw = raw[: self.policy.max_bytes]
                body = _decode_body(raw, (resp.headers.get("Content-Encoding") or "").lower())
                return {
                    "ok": True,
                    "status": resp.status,
                    "headers": {k.lower(): v for k, v in resp.headers.items()},
                    "body": body,
                    "url": resp.url,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "truncated": truncated,
                }
        except urllib.error.HTTPError as e:
            # 3xx и 4xx приходят сюда, т.к. авто-редиректы отключены
            try:
                raw = e.read(self.policy.max_bytes) if method == "GET" else b""
            except Exception:
                raw = b""
            body = _decode_body(raw, (e.headers.get("Content-Encoding") or "").lower() if e.headers else "")
            return {
                "ok": True,
                "status": e.code,
                "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
                "body": body,
                "url": e.url if getattr(e, "url", None) else url,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "truncated": False,
            }
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as e:
            reason = getattr(e, "reason", e)
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {reason}",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as e:  # непредвиденное — не валим весь прогон
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }

    # ------------------------------------------------------------------ высокий

    def fetch(self, url: str, method: str = "GET", max_redirects: int = 6,
              cache_bust: bool = False) -> dict[str, Any]:
        """Загрузить URL, проходя цепочку редиректов вручную.

        Возвращает единый словарь и НИКОГДА не бросает исключение — на 200 сайтах
        падение одной страницы не должно останавливать прогон.
        """
        chain: list[dict[str, Any]] = []
        current = _with_cache_buster(url) if cache_bust else url
        total_ms = 0
        for hop in range(max_redirects + 1):
            host = urlsplit(current).netloc
            self.gate.acquire(host)
            try:
                attempt = 0
                while True:
                    res = self._once(current, method)
                    total_ms += res.get("elapsed_ms", 0)
                    transient = (not res["ok"]) or res.get("status") in RETRY_STATUS
                    if not transient or attempt >= self.policy.retries:
                        break
                    attempt += 1
                    time.sleep(self.policy.retry_backoff ** attempt)
            finally:
                self.gate.release(host)

            if not res["ok"]:
                return {
                    "ok": False,
                    "error": res.get("error", "unknown"),
                    "requested_url": url,
                    "final_url": current,
                    "redirects": len(chain),
                    "chain": chain,
                    "elapsed_ms": total_ms,
                }

            status = res["status"]
            location = res["headers"].get("location")
            if status in (301, 302, 303, 307, 308) and location and hop < max_redirects:
                nxt = urljoin(current, location)
                chain.append({"from": current, "status": status, "to": nxt})
                current = nxt
                continue

            ct = res["headers"].get("content-type", "")
            body = res["body"]
            text = ""
            ct_low = ct.lower()
            # ВАЖНО: сюда должны попадать не только html/xml, но и text/plain —
            # иначе robots.txt молча приходит с пустым text и проверки robots
            # деградируют в "файла нет". Ровно этот баг был найден на climtec.md.
            if body and (
                ct_low.startswith("text/")
                or "html" in ct_low
                or "xml" in ct_low
                or "json" in ct_low
                or not ct
            ):
                # Двойная защита: даже если имя кодировки прошло проверку, но
                # оказалось неприменимым, страница должна прочитаться, а не
                # выбросить сайт из аудита целиком.
                try:
                    text = body.decode(_charset_from(ct, body), "replace")
                except (LookupError, UnicodeDecodeError):
                    text = body.decode("utf-8", "replace")
            cache_state, cache_layer = read_cache_headers(res["headers"])
            return {
                "ok": True,
                "status": status,
                "headers": res["headers"],
                "text": text,
                "body": body,
                "cache_state": cache_state,
                "cache_layer": cache_layer,
                "cache_busted": cache_bust,
                "requested_url": url,
                "final_url": res.get("url") or current,
                "redirects": len(chain),
                "chain": chain,
                "content_type": ct,
                "bytes": len(body),
                "elapsed_ms": total_ms,
                "truncated": res.get("truncated", False),
            }

        return {
            "ok": False,
            "error": f"too many redirects (>{max_redirects})",
            "requested_url": url,
            "final_url": current,
            "redirects": len(chain),
            "chain": chain,
            "elapsed_ms": total_ms,
        }


# ------------------------------------------------------------------ robots.txt


@dataclass
class Robots:
    """Минимальный, но корректный разбор robots.txt для нашего агента.

    Реализовано осознанно скромно: группы User-agent, Allow/Disallow с
    подстановками * и $, Sitemap, Crawl-delay. Побеждает самое длинное правило —
    как это делает Google.
    """

    rules: list[tuple[str, bool]] = field(default_factory=list)  # (шаблон, разрешено)
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    raw: str = ""
    present: bool = False

    @classmethod
    def parse(cls, text: str, agent: str = "imperalseoaudit") -> "Robots":
        rob = cls(raw=text or "", present=bool(text))
        groups: list[tuple[list[str], list[tuple[str, bool]], float | None]] = []
        agents: list[str] = []
        rules: list[tuple[str, bool]] = []
        delay: float | None = None
        seen_rule = False

        def flush() -> None:
            nonlocal agents, rules, delay, seen_rule
            if agents:
                groups.append((agents, rules, delay))
            agents, rules, delay, seen_rule = [], [], None, False

        for line in (text or "").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            key = field_name.strip().lower()
            val = value.strip()
            if key == "user-agent":
                if seen_rule:
                    flush()
                agents.append(val.lower())
            elif key in ("allow", "disallow"):
                seen_rule = True
                if val or key == "disallow":
                    rules.append((val, key == "allow"))
            elif key == "crawl-delay":
                seen_rule = True
                try:
                    delay = float(val.replace(",", "."))
                except ValueError:
                    pass
            elif key == "sitemap" and val:
                rob.sitemaps.append(val)
        flush()

        chosen: tuple[list[tuple[str, bool]], float | None] | None = None
        for names, rl, dl in groups:
            if any(n == agent for n in names):
                chosen = (rl, dl)
                break
        if chosen is None:
            for names, rl, dl in groups:
                if "*" in names:
                    chosen = (rl, dl)
                    break
        if chosen:
            rob.rules, rob.crawl_delay = chosen
        return rob

    @staticmethod
    def _match(pattern: str, path: str) -> int:
        """Длина совпавшего шаблона, или -1. Поддержаны * и $."""
        if pattern == "":
            return -1
        anchored = pattern.endswith("$")
        pat = pattern[:-1] if anchored else pattern
        parts = pat.split("*")
        pos = 0
        if not path.startswith(parts[0]):
            return -1
        pos = len(parts[0])
        for part in parts[1:]:
            if part == "":
                continue
            idx = path.find(part, pos)
            if idx == -1:
                return -1
            pos = idx + len(part)
        if anchored and pos != len(path):
            return -1
        return len(pattern)

    def allowed(self, path: str) -> bool:
        best_len, best_allow = -1, True
        for pattern, allow in self.rules:
            n = self._match(pattern, path)
            if n > best_len:
                best_len, best_allow = n, allow
            elif n == best_len and n != -1 and allow:
                best_allow = True  # при равной длине Allow имеет приоритет
        return best_allow if best_len != -1 else True
