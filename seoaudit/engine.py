"""Движок аудита: обход -> разбор -> правила -> находки в БД.

Устройство рассчитано на 20-200 сайтов в одном прогоне:

* всё состояние в SQLite, поэтому обрыв не теряет работу (resume);
* параллельность по САЙТАМ, а внутри сайта — вежливый лимит на хост;
* каждая страница грузится ДВАЖДЫ: как посетитель и с обходом кеша.
  Второй запрос ловит ситуацию «кеш отдаёт старую версию» — на climtec.md
  именно это скрывало и новые описания, и исправленный canonical.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from . import rules as R
from .discover import discover, expand_by_links, normalize_url
from .extract import HeadData, collapse, parse_head, visible_len
from .fetcher import Fetcher, FetchPolicy
from .severity import site_health_score, sort_findings
from .store import Store


@dataclass
class AuditConfig:
    """Настройки прогона."""

    max_pages_per_site: int = 150      # хватает, чтобы увидеть системные дефекты
    site_workers: int = 4              # сколько САЙТОВ одновременно
    page_workers: int = 4              # сколько страниц одновременно внутри сайта
    follow_links: bool = True          # искать страницы вне карты сайта
    cache_probe: bool = True           # второй запрос для проверки кеша
    cache_probe_limit: int = 12        # на скольких страницах проверять кеш
    respect_robots: bool = True
    policy: FetchPolicy = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.policy is None:
            self.policy = FetchPolicy()


# Поля, которые правила уровня сайта читают из «плоской» записи страницы.
def _flat_page(page_row: dict[str, Any], head: HeadData | None) -> dict[str, Any]:
    d = dict(page_row)
    if head is not None:
        d.update({
            "title": head.title,
            "description": head.description,
            "canonical": head.canonical,
            "html_lang": head.html_lang,
            "noindex": head.is_noindex,
            "word_count": head.word_count,
        })
    return d


class Engine:
    """Выполняет аудит списка сайтов и сохраняет находки."""

    def __init__(self, store: Store, config: AuditConfig | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None):
        self.store = store
        self.cfg = config or AuditConfig()
        self.fetcher = Fetcher(self.cfg.policy)
        self._on_event = on_event or (lambda kind, data: None)

    # ── события для прогресса (CLI/панель) ────────────────────────────────
    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self._on_event(kind, data)
        except Exception:
            pass  # телеметрия никогда не должна ломать аудит

    # ── публичный вход ────────────────────────────────────────────────────
    def run(self, origins: list[str], *, label: str = "",
            run_id: int | None = None) -> int:
        """Аудит списка сайтов. Возвращает run_id.

        Если передан существующий run_id — продолжает незавершённый прогон
        (страницы и сайты со статусом done не перезагружаются).
        """
        if run_id is None:
            run_id = self.store.create_run(label=label)
            for origin in origins:
                norm = normalize_url(origin) or origin
                self.store.add_site(run_id, norm)

        pending = self.store.sites(run_id, states=["pending", "discovering", "fetching"])
        self._emit("run_start", run_id=run_id, sites=len(pending))

        if self.cfg.site_workers <= 1 or len(pending) <= 1:
            for row in pending:
                self._audit_site(row["id"], row["origin"])
        else:
            with ThreadPoolExecutor(max_workers=self.cfg.site_workers) as pool:
                futs = {
                    pool.submit(self._audit_site, row["id"], row["origin"]): row["origin"]
                    for row in pending
                }
                for fut in as_completed(futs):
                    origin = futs[fut]
                    try:
                        fut.result()
                    except Exception as exc:  # один сайт не должен валить прогон
                        self._emit("site_error", origin=origin, error=str(exc))

        self.store.finish_run(run_id)
        self._emit("run_done", run_id=run_id)
        return run_id

    def resume(self, run_id: int) -> int:
        """Продолжить прерванный прогон, не повторяя сделанное.

        Ради этого всё состояние и живёт в SQLite: на 200 сайтах обрыв
        (сеть, Ctrl-C, перезагрузка) — вопрос времени, а перепроверять
        уже проверенное значит платить за работу дважды.
        """
        return self.run([], run_id=run_id)

    # ── один сайт ─────────────────────────────────────────────────────────
    def _audit_site(self, site_id: int, origin: str) -> None:
        t0 = time.time()
        self._emit("site_start", origin=origin)
        try:
            self.store.set_site_state(site_id, "discovering")
            disc = discover(
                self.fetcher, origin,
                max_urls=self.cfg.max_pages_per_site,
                respect_robots=self.cfg.respect_robots,
            )
            self.store.set_site_notes(site_id, disc.to_dict())
            if disc.robots_txt:
                with self.store.tx() as con:
                    con.execute("UPDATE sites SET robots_txt=? WHERE id=?",
                                (disc.robots_txt[:20000], site_id))

            urls = list(disc.urls)
            if not urls:
                urls = [normalize_url(origin)]
            self.store.queue_urls(site_id, urls, "sitemap" if disc.sitemaps_seen else "seed")

            self.store.set_site_state(site_id, "fetching")
            heads = self._fetch_pages(site_id)

            # Достраиваем список ссылками, если карта бедная или её нет.
            # Так на climtec.md обнаружилось, что 9 товаров существуют,
            # но в sitemap их нет вообще.
            if self.cfg.follow_links:
                have = self.store.count_pages(site_id)
                if have < self.cfg.max_pages_per_site:
                    known = [r["final_url"] or r["url"]
                             for r in self.store.pages(site_id, only_done=False)]
                    internal: dict[str, list[str]] = {}
                    for row in self.store.pages(site_id, only_done=True):
                        h = heads.get(row["id"])
                        if h is not None and h.links_internal:
                            internal[row["final_url"] or row["url"]] = h.links_internal
                    extra = expand_by_links(
                        self.fetcher, origin, known, internal,
                        limit=self.cfg.max_pages_per_site - have,
                        respect_robots=self.cfg.respect_robots,
                        robots=disc.robots,
                    )
                    if extra:
                        self.store.queue_urls(site_id, extra, "link")
                        heads.update(self._fetch_pages(site_id))

            found = self._analyse(site_id, origin, disc.to_dict(), heads)
            self.store.set_site_state(site_id, "done")
            self._emit("site_done", origin=origin,
                       pages=self.store.count_pages(site_id),
                       findings=found,
                       seconds=round(time.time() - t0, 1))
        except Exception as exc:
            self.store.set_site_state(site_id, "error", error=str(exc)[:500])
            self._emit("site_error", origin=origin, error=str(exc))
            raise

    # ── загрузка страниц сайта ────────────────────────────────────────────
    def _fetch_pages(self, site_id: int) -> dict[int, HeadData]:
        heads: dict[int, HeadData] = {}
        pending = self.store.pending_pages(site_id, limit=self.cfg.max_pages_per_site)
        if not pending:
            return heads

        def work(row) -> tuple[int, dict[str, Any], HeadData | None]:
            res = self.fetcher.fetch(row["url"])
            head = None
            payload: dict[str, Any] = {
                "status": res.get("status"),
                "final_url": res.get("final_url") or row["url"],
                "redirects": res.get("redirects") or 0,
                "chain": res.get("chain") or [],
                "elapsed_ms": res.get("elapsed_ms") or 0,
                "bytes": res.get("bytes") or 0,
                "content_type": res.get("content_type") or "",
                "error": res.get("error") or "",
                "state": "done" if res.get("ok") else "error",
                "cache_state": res.get("cache_state") or "",
                "cache_layer": res.get("cache_layer") or "",
            }
            text = res.get("text") or ""
            if text:
                head = parse_head(text, payload["final_url"])
                payload["head"] = head.to_dict()
            return row["id"], payload, head

        with ThreadPoolExecutor(max_workers=self.cfg.page_workers) as pool:
            futs = [pool.submit(work, row) for row in pending]
            batch: list[tuple[int, dict[str, Any]]] = []
            for fut in as_completed(futs):
                try:
                    pid, payload, head = fut.result()
                except Exception as exc:
                    continue
                batch.append((pid, payload))
                if head is not None:
                    heads[pid] = head
            self.store.save_page_results(batch)

        if self.cfg.cache_probe:
            self._probe_cache(site_id, heads)
        return heads

    # ── проверка «кеш отдаёт устаревшее» ──────────────────────────────────
    def _probe_cache(self, site_id: int, heads: dict[int, HeadData]) -> None:
        """Сравнивает ответ посетителю и ответ в обход кеша.

        Ровно эта проверка вскрыла на climtec.md, что LiteSpeed неделю отдавал
        страницы без новых описаний. Без неё аудит рапортует по данным,
        которых посетитель не видит.
        """
        rows = [r for r in self.store.pages(site_id, only_done=True)
                if r["status"] == 200][: self.cfg.cache_probe_limit]
        if not rows:
            return

        def probe(row) -> tuple[int, bool, str]:
            url = row["final_url"] or row["url"]
            fresh = self.fetcher.fetch(url, cache_bust=True)
            if not fresh.get("ok") or not fresh.get("text"):
                return row["id"], False, ""
            fh = parse_head(fresh["text"], fresh.get("final_url") or url)
            old = heads.get(row["id"])
            if old is None:
                return row["id"], False, fresh.get("cache_state") or ""
            differs = (
                collapse(old.title) != collapse(fh.title)
                or collapse(old.description) != collapse(fh.description)
                or (old.canonical or "").rstrip("/") != (fh.canonical or "").rstrip("/")
            )
            return row["id"], differs, fresh.get("cache_state") or ""

        with ThreadPoolExecutor(max_workers=min(3, self.cfg.page_workers)) as pool:
            futs = [pool.submit(probe, row) for row in rows]
            for fut in as_completed(futs):
                try:
                    pid, differs, _state = fut.result()
                except Exception:
                    continue
                if differs:
                    with self.store.tx() as con:
                        con.execute(
                            "UPDATE pages SET cache_stale=1 WHERE id=?", (pid,)
                        )

    # ── применение правил ─────────────────────────────────────────────────
    def _analyse(self, site_id: int, origin: str, discovery: dict[str, Any],
                 heads: dict[int, HeadData]) -> int:
        """Применить правила и сохранить находки. Возвращает их число."""
        # ТОЛЬКО загруженные страницы. С only_done=False сюда попадали записи
        # в состоянии queued (ничего не скачано), и правила выносили по ним
        # приговоры «нет заголовка / нет описания / нет H1» — 35 ложных находок
        # на icnli.org при нуле реально прочитанных страниц. Аудит, уверенно
        # судящий о том, чего не видел, хуже отсутствия аудита.
        rows = [r for r in self.store.pages(site_id, only_done=False)
                if r["state"] in ("done", "error")]
        flat: list[dict[str, Any]] = []
        by_url: dict[str, dict[str, Any]] = {}
        seen_final: set[str] = set()
        parsed: dict[int, HeadData] = {}

        for row in rows:
            d = dict(row)
            head = heads.get(row["id"])
            if head is None and d.get("head"):
                # В БД head лежит JSON-СТРОКОЙ. При чтении сохранённого прогона
                # (resume, повторный анализ) сюда приходит строка, а не словарь —
                # без разбора весь сайт падал с 'str' has no attribute 'items'.
                raw = d["head"]
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (ValueError, TypeError):
                        raw = {}
                if isinstance(raw, dict) and raw:
                    head = HeadData(**{k: v for k, v in raw.items()
                                       if k in HeadData.__annotations__})
            item = _flat_page(d, head)
            if head is not None:
                # Запоминаем РАЗОБРАННЫЙ head: ниже правила берут его отсюда.
                # Иначе при продолжении прогона (данные читаются из БД, а память
                # пуста) правила получили бы пустой head и насочиняли бы
                # ложных находок вида «нет заголовка» на нормальных страницах.
                parsed[row["id"]] = head
            key = (item.get("final_url") or item.get("url") or "").rstrip("/")
            # Разные исходные URL могут после редиректа вести на ОДНУ страницу
            # (например /ru/ -> /ru/home-ru/). Иначе одна и та же находка
            # попадёт в отчёт дважды и задача продублируется.
            if key and key in seen_final:
                continue
            if key:
                seen_final.add(key)
                by_url[key] = item
            flat.append(item)

        ctx = {"discovery": discovery, "by_url": by_url, "origin": origin}
        found: list[R.Finding] = []

        for item in flat:
            head = parsed.get(item.get("id")) or heads.get(item.get("id"))
            if head is None:
                head = HeadData()
            # Через единую точку: она не даёт судить незагруженные страницы
            # и глушит сбой одного правила, не роняя весь сайт.
            found.extend(R.run_page_rules(item, head, ctx))

        for rule in R.SITE_RULES:
            try:
                found.extend(rule(flat, ctx))
            except Exception as exc:
                self._emit("rule_error", rule=getattr(rule, "__name__", "?"),
                           error=str(exc))

        found = sort_findings(found)
        self.store.clear_findings(site_id)
        self.store.add_findings(site_id, [f.to_dict() for f in found])

        notes = dict(discovery)
        notes["health_score"] = site_health_score(found, len(flat))
        notes["pages_analysed"] = len(flat)
        self.store.set_site_notes(site_id, notes)
        return len(found)
