"""Мост между движком `seoaudit` и платформой.

Здесь одна забота: движок думает файлами и потоками, платформа — пользователями
и объектами. Всё преобразование собрано в одном месте, чтобы движок не знал про
платформу, а инструменты — про SQLite.

ГДЕ ЖИВЁТ БАЗА. Движку нужен файл SQLite: в нём состояние каждой страницы, ради
этого и возможен `resume` после обрыва. Но файловая система воркера эфемерна —
между двумя вызовами инструмента файла может не быть. Поэтому база выгружается
в `ctx.storage` (единственное durable-хранилище для бинарей; `ctx.store`
документный и для десятков мегабайт не годится), а следующий вызов скачивает её
во временный файл.

Сводку прогона дублируем в `ctx.store` — чтобы «покажи прогоны» отвечало без
скачивания всей базы.

ИЗОЛЯЦИЯ ПОЛЬЗОВАТЕЛЕЙ. Путь всегда начинается с `ctx.user.imperal_id`.
Федеральный слой это за нас не проверяет — контракт держим мы.

ФОРМЫ ДАННЫХ. Отчёты движка требуют РАЗНЫХ форм, и это не случайность:
`portfolio_report_md` ждёт строки, где `tasks` — ЧИСЛО задач и есть `by_severity`
с `top_issue`; `site_report_md` ждёт сам сайт, список находок, список задач и
отдельно `pages=`/`score=`. Обе формы собираются здесь одной функцией — ровно
как в CLI, чтобы отчёт в чате и отчёт в терминале не могли разойтись.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Движок лежит рядом как обычный пакет — расширение его ИМПОРТИРУЕТ, а не
# копирует логику: правило аудита правится в одном месте.
# `with_scheme` живёт В ДВИЖКЕ, а не здесь: ту же догадку о схеме должен
# делать и CLI. Две копии одного решения разъехались бы при первой же правке —
# и именно так баг с «https:climtec.md» уцелел в CLI, пока платформа была цела.
from seoaudit.discover import with_scheme  # noqa: F401  (реэкспорт для инструментов)
from seoaudit.engine import AuditConfig, Engine
from seoaudit.extract import same_url  # ОДНА функция нормализации URL на всё
# приложение — та же, что движок использует для canonical. Изобретать вторую
# копию правил нормализации (регистр схемы/хоста, конечный слэш) означало бы
# рисковать тем, что «страница = страница» в отчёте и в page-фильтре разойдутся.
from seoaudit.fetcher import FetchPolicy
from seoaudit.reports import (
    host_of,
    page_report_md,
    portfolio_report_md,
    site_report_md,
)
from seoaudit.severity import CRITICAL, HIGH, LAYER_NAMES, SEVERITY_ORDER
from seoaudit.store import Store
from seoaudit.tasks import build_tasks
from seoaudit.export_tracker import plan_for_tracker, summarise_plan
from seoaudit.compare import compare_findings, summarise as summarise_comparison
from seoaudit.fixes import build_fixes

# Коллекция сводок прогонов в ctx.store — быстрые ответы без скачивания базы.
RUNS_COLLECTION = "seo_runs"

_DB_BASENAME = "portfolio.db"


def storage_key(ctx) -> str:
    """Путь базы в ctx.storage, всегда внутри пространства пользователя."""
    return f"{ctx.user.imperal_id}/seo-audit/{_DB_BASENAME}"


def parse_sites(raw: str) -> list[str]:
    """Разобрать список сайтов из человеческого ввода.

    Принимаем запятые, точки с запятой и переводы строк: список вставляют как
    получится, а не по нашему формату. Дубликаты убираем — аудит одного сайта
    дважды в прогоне это просто двойная нагрузка на чужой сервер.
    """
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(",", "\n").replace(";", "\n").splitlines():
        item = chunk.strip().strip(",").strip()
        if item:
            parts.append(item)
    seen: set[str] = set()
    out: list[str] = []
    for item in parts:
        key = item.lower().removeprefix("https://").removeprefix("http://")
        key = key.strip("/").removeprefix("www.")
        if key and key not in seen:
            seen.add(key)
            out.append(with_scheme(item))
    return out


async def download_db(ctx) -> str | None:
    """Скачать базу портфеля во временный файл. None — базы ещё нет.

    Отсутствие базы это НЕ ошибка: так выглядит пользователь, который ещё не
    запускал аудит. Различать «пусто» и «сломано» обязательно, иначе совет
    будет неверный — «запусти аудит» вместо «что-то пошло не так».
    """
    try:
        data = await ctx.storage.download(storage_key(ctx))
    except Exception:
        return None
    if not data:
        return None
    fd, path = tempfile.mkstemp(prefix="seo-audit-", suffix=".db")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


async def upload_db(ctx, path: str) -> None:
    """Сохранить базу портфеля в durable-хранилище."""
    data = Path(path).read_bytes()
    await ctx.storage.upload(
        storage_key(ctx), data, content_type="application/x-sqlite3"
    )


def new_db_path() -> str:
    """Свежий путь для базы прогона."""
    fd, path = tempfile.mkstemp(prefix="seo-audit-", suffix=".db")
    os.close(fd)
    os.unlink(path)  # движок создаст файл сам
    return path


def open_store(path: str) -> Store:
    return Store(path)


def run_audit_blocking(
    db_path: str,
    origins: list[str],
    *,
    label: str = "",
    max_pages: int = 50,
    site_workers: int = 4,
    page_workers: int = 4,
    on_event=None,
) -> int:
    """Синхронный прогон движка. Возвращает run_id.

    Без async намеренно: движок держит пул потоков и блокирующие сокеты.
    Вызывать только через `to_thread`, иначе заблокируется весь воркер.
    """
    store = open_store(db_path)
    cfg = AuditConfig(
        max_pages_per_site=max_pages,
        site_workers=site_workers,
        page_workers=page_workers,
        policy=FetchPolicy(),
    )
    engine = Engine(store, cfg, on_event=on_event)
    try:
        return engine.run(origins, label=label)
    finally:
        store.close()


def resume_blocking(db_path: str, run_id: int, *, on_event=None) -> int:
    """Продолжить прерванный прогон."""
    store = open_store(db_path)
    engine = Engine(store, AuditConfig(), on_event=on_event)
    try:
        return engine.resume(run_id)
    finally:
        store.close()


def resolve_run(store: Store, run_id: int = 0, *, site: str = "") -> int:
    """Номер прогона: явный, «где был этот сайт», или последний. 0 — прогонов нет.

    `site` меняет смысл «последнего»: не последний прогон ВООБЩЕ, а последний,
    в котором проверялся именно этот домен. Портфель аудят частями — сегодня
    один сайт, завтра другой. Без этого спросить отчёт по climtec.md сразу
    после аудита другого сайта значило получить «такого сайта нет», хотя он
    проверен и лежит в базе. Для человека это неотличимо от потери данных.
    Найдено на живом портфеле.

    Явный `run_id` всегда сильнее: если человек назвал прогон, подменять его
    нельзя — он спрашивает про конкретную проверку.
    """
    if run_id:
        row = store.get_run(run_id)
        return int(row["id"]) if row else 0
    if site:
        for_host = latest_run_for_host(store, site)
        if for_host:
            return for_host
    row = store.latest_run()
    return int(row["id"]) if row else 0


def site_rows(store: Store, run_id: int, *, min_severity: str = "medium"
              ) -> tuple[list[dict[str, Any]], dict[str, list]]:
    """Строки портфеля + задачи по сайтам — ровно как их готовит CLI.

    Возвращает КОРТЕЖ: строки для сводного отчёта (где `tasks` — число) и
    задачи по origin (где нужны сами объекты). Разделение не косметика:
    `portfolio_report_md` ждёт первую форму, работа с задачами — вторую.
    """
    rows: list[dict[str, Any]] = []
    tasks_by_site: dict[str, list] = {}

    for site in store.sites(run_id):
        findings = [dict(f) for f in store.findings(site["id"])]
        pages = store.count_pages(site["id"])
        tasks = build_tasks(site["origin"], findings, min_severity=min_severity)
        tasks_by_site[site["origin"]] = tasks

        by_sev: dict[str, int] = {}
        for f in findings:
            by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1

        top = ""
        for f in sorted(findings, key=lambda x: (x["layer"],
                                                 SEVERITY_ORDER.get(x["severity"], 9))):
            if f["severity"] in (CRITICAL, HIGH):
                top = f["message"]
                break

        keys = site.keys()
        rows.append({
            "id": site["id"],
            "origin": site["origin"],
            "state": site["state"],
            "error": site["error"] if "error" in keys else "",
            "pages": pages,
            "score": store.site_score(site["id"], pages),
            "tasks": len(tasks),
            "by_severity": by_sev,
            "top_issue": top,
            "rules": sorted({f["rule"] for f in findings}),
            "findings": findings,
        })
    return rows, tasks_by_site


def match_site(rows: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """Найти сайт по тому, как его назвал человек.

    «climtec.md», «https://climtec.md/», «www.climtec.md» — один сайт.
    Сравниваем по хосту без www, иначе пользователь обязан вводить origin
    символ в символ.
    """
    if not wanted:
        return None
    needle = (host_of(wanted) or wanted.strip()).lower().removeprefix("www.")
    if not needle:
        return None
    for row in rows:
        host = host_of(row["origin"]).lower().removeprefix("www.")
        if host == needle or needle in host:
            return row
    return None


# ══════════════════════════════════════════════════════════════════════════
# PAGE-LEVEL SLICE — opt-in фильтр по точному адресу одной страницы.
#
# Строгий контракт (сознательное решение, не забывчивость): сравнение ТОЛЬКО
# через `same_url` движка (та же функция, что судит canonical) — без fuzzy-
# подмены на «похожий» URL. Не найдено — явная ошибка с примерами известных
# адресов, а не тихий выбор ближайшего.
# ══════════════════════════════════════════════════════════════════════════

def _evidence_urls(finding: dict[str, Any]) -> list[str]:
    """Все URL, упомянутые внутри evidence находки уровня сайта.

    Правила уровня сайта (сироты, битые внутренние ссылки, тупики, глубокие
    страницы, кластеры canonical, дубли title/description) кладут ПОЛНЫЙ
    список затронутых страниц в evidence — под разными ключами, потому что
    правила писались в разное время. Список ключей закрыт тем, что реально
    есть в `seoaudit/rules.py` — если правило заведёт новый ключ со списком
    страниц, эта функция придётся дополнить (закрывающий список — осознанное
    решение, а не забытая ветка).
    """
    ev = finding.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except (ValueError, TypeError):
            ev = {}
    if not isinstance(ev, dict):
        return []

    out: list[str] = []

    def _add(u):
        u = (str(u) or "").strip()
        if u and u not in out:
            out.append(u)

    for key in ("urls", "orphans", "targets", "dead"):
        for u in ev.get(key) or []:
            _add(u)
    # {"pages": [{"url": ..., "depth": ...}]} — structure.deep_page.
    # ВНИМАНИЕ: у structure.orphan_suspect тот же ключ "pages" хранит просто
    # ЧИСЛО (сколько всего страниц проверено), а не список — оба правила
    # писались независимо. isinstance-проверка ниже отличает одно от другого.
    pages_ev = ev.get("pages")
    if isinstance(pages_ev, list):
        for p in pages_ev:
            if isinstance(p, dict):
                _add(p.get("url"))
            else:
                _add(p)
    # {"examples": {target: [source, ...]}} — structure.broken_internal_link:
    # источники (страницы, которые ССЫЛАЮТСЯ на битый адрес) тоже затронуты.
    examples = ev.get("examples")
    if isinstance(examples, dict):
        for sources in examples.values():
            for u in sources or []:
                _add(u)
    return out


def filter_findings_by_page(findings: list[dict[str, Any]], page_url: str,
                             ) -> list[dict[str, Any]]:
    """Находки этой страницы: свои напрямую + встреченные в evidence чужой.

    Возвращает НОВЫЕ dict с добавленным `matched_via`, оригиналы не трогаем —
    вызывающий код читает те же строки в других формах (site-wide отчёты).
    """
    out: list[dict[str, Any]] = []
    for f in findings:
        via = ""
        own_url = f.get("url") or ""
        if own_url and same_url(own_url, page_url):
            via = "url"
        else:
            for u in _evidence_urls(f):
                if same_url(u, page_url):
                    via = "evidence"
                    break
        if via:
            item = dict(f)
            item["matched_via"] = via
            out.append(item)
    return out


def filter_tasks_by_page(tasks: list, page_url: str) -> list[tuple[Any, str]]:
    """Задачи, затрагивающие эту страницу — как (task, matched_url) пары.

    `matched_url` — тот конкретный URL из `task.urls`, что совпал с запросом
    (может отличаться от `page_url` буквально: со слэшем/без, другая схема).
    Задача возвращается ЦЕЛОЙ, а не projection с обрезанным списком urls —
    её `fingerprint` не должен клониться под каждую страницу, иначе повторный
    экспорт в трекер создал бы дубли одной и той же работы.
    """
    out: list[tuple[Any, str]] = []
    for t in tasks:
        for u in t.urls:
            if same_url(u, page_url):
                out.append((t, u))
                break
    return out


def find_page(store: Store, site_id: int, page_url: str) -> dict[str, Any] | None:
    """Найти строку страницы по точному адресу — как её видел движок при обходе.

    Сравниваем и `url` (адрес, с которого начали), и `final_url` (адрес после
    редиректов) — посетитель и поисковик видят именно final_url, но человек
    мог назвать исходный адрес. Не найдено — вызывающий код обязан вернуть
    SEO_PAGE_NOT_FOUND, а не подставлять «похожую» страницу.
    """
    for row in store.pages(site_id, only_done=False):
        d = dict(row)
        if same_url(d.get("url") or "", page_url) or \
           same_url(d.get("final_url") or "", page_url):
            head = d.get("head")
            if isinstance(head, str):
                try:
                    head = json.loads(head)
                except (ValueError, TypeError):
                    head = {}
            head = head if isinstance(head, dict) else {}
            return {
                "url": d.get("final_url") or d.get("url") or page_url,
                "title": head.get("title") or "",
                "canonical": head.get("canonical") or "",
                "status": d.get("status"),
                "fetched_at": d.get("fetched_at"),
            }
    return None


def known_page_urls(store: Store, site_id: int, limit: int = 8) -> list[str]:
    """Несколько известных адресов сайта — подсказка, если page_url не найден."""
    out: list[str] = []
    for row in store.pages(site_id, only_done=False):
        u = row["final_url"] or row["url"]
        if u and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def run_hint(site: str, page_url: str) -> str:
    """Домен-подсказка для `resolve_run`, когда site не назван, а page_url есть.

    Без этого «дай находки по https://g4s.md/» после аудита ДРУГОГО сайта
    брало бы последний прогон ВООБЩЕ (не содержащий g4s.md) и отвечало бы
    «сайта нет» — хотя он проверен и лежит в базе, просто не в последнем
    прогоне. Тот же баг класса, что уже был описан для `site=...`
    (см. `resolve_run`), просто для нового входа.
    """
    if site:
        return site
    if page_url:
        return urlsplit(page_url).netloc
    return ""


def resolve_page_site(rows: list[dict[str, Any]], site: str, page_url: str,
                      ) -> tuple[dict[str, Any] | None, bool]:
    """Определить строку сайта из `site` и/или `page_url`.

    `site` НЕОБЯЗАТЕЛЕН, когда задан `page_url` — сайт тогда угадывается по
    хосту самого адреса: «дай отчёт по https://g4s.md/» не должно требовать
    отдельно назвать домен, который и так виден в URL.

    Возвращает (строка_сайта_или_None, mismatch). `mismatch=True` — оба
    параметра заданы и указывают на РАЗНЫЕ хосты; вызывающий код обязан вернуть
    SEO_PAGE_SITE_MISMATCH, а не молча выбрать один из двух — тихий выбор
    ответил бы не на тот вопрос, который задали.
    """
    page_host = ""
    if page_url:
        page_host = urlsplit(page_url).netloc.lower().removeprefix("www.")

    if site:
        row = match_site(rows, site)
        if row is not None and page_host:
            if host_label(row["origin"]) != page_host:
                return row, True
        return row, False

    if page_host:
        for r in rows:
            if host_label(r["origin"]) == page_host:
                return r, False
        return None, False

    return None, False


def page_markdown(store: Store, row: dict[str, Any], page: dict[str, Any],
                  findings: list[dict[str, Any]], tasks: list) -> str:
    """Отчёт по одной странице — обёртка в той же форме, что и `site_markdown`."""
    return page_report_md(row["origin"], page, findings, tasks)


def site_markdown(store: Store, row: dict[str, Any], tasks: list) -> str:
    """Отчёт по одному сайту.

    `pages` и `score` передаются ИМЕНОВАННО — такова сигнатура движка.
    """
    site = {"origin": row["origin"], "state": row["state"]}
    return site_report_md(site, row["findings"], tasks,
                          pages=row["pages"], score=row["score"])


def portfolio_markdown(rows: list[dict[str, Any]], label: str = "") -> str:
    return portfolio_report_md(rows, label=label)


def layer_name(layer: int) -> str:
    return LAYER_NAMES.get(layer, f"слой {layer}")


def host_label(origin: str) -> str:
    """Домен так, как его называет человек: без схемы, без www, без слеша.

    В базе origin хранится полным (`https://climtec.md`) — это нужно движку.
    Но показывать пользователю схему незачем: он написал «climtec.md» и ждёт
    ровно этого в ответе.
    """
    host = (host_of(origin) or origin or "").strip().lower()
    return host.removeprefix("www.").rstrip("/")


def severity_rank(severity: str) -> int:
    """Числовой ранг уровня: 0 = critical. Неизвестный — в самый низ.

    Своя функция, а не обращение к словарю движка на месте: сортировка и
    фильтрация встречаются в нескольких инструментах, и разъехавшийся порядок
    показывал бы «сначала важное» по-разному в списке находок и в задачах.
    """
    return SEVERITY_ORDER.get((severity or "").strip().lower(), 9)


def plan_entries(tasks_by_site: dict[str, list], *, project: str = "",
                 assignee: str = "") -> list[dict[str, Any]]:
    """План выгрузки в трекер по всем сайтам.

    `assignee` прокидывается в движок, а не подставляется потом: назначение —
    часть готовых аргументов задачи, и дописывать его сверху означало бы
    расхождение между планом из чата и планом из CLI.
    """
    plan: list[dict[str, Any]] = []
    for tasks in tasks_by_site.values():
        if not tasks:
            continue
        kwargs: dict[str, Any] = {}
        if project:
            kwargs["project"] = project
        if assignee:
            kwargs["assignee"] = assignee
        plan.extend(plan_for_tracker(tasks, **kwargs))
    return plan


def plan_summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
    return summarise_plan(plan)


def run_label(store: Store, run_id: int) -> str:
    row = store.get_run(run_id)
    if not row:
        return ""
    keys = row.keys()
    return (row["label"] if "label" in keys else "") or ""


def run_finished(store: Store, run_id: int) -> bool:
    row = store.get_run(run_id)
    if not row:
        return False
    keys = row.keys()
    return bool(row["finished_at"]) if "finished_at" in keys else False


def estimate_minutes(site_count: int, max_pages: int) -> str:
    """Честная оценка длительности по замерам из README.

    200 сайтов ≈ 11-22 минуты при 50 страницах. Линейная прикидка достаточна:
    задача оценки — управлять ожиданием, а не предсказать секунды.
    """
    if site_count <= 0:
        return "меньше минуты"
    per_site = max(0.05, min(0.5, max_pages / 100.0))
    total = site_count * per_site
    if total < 1:
        return "меньше минуты"
    if total < 2:
        return "около минуты"
    return f"примерно {int(total)}-{int(total * 2)} мин"


async def to_thread(fn, *args, **kwargs):
    """`asyncio.to_thread` под явным именем — движок блокирующий."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


# --- подключённые сайты ------------------------------------------------------
#
# «Подключённый сайт» — это УНИКАЛЬНЫЙ origin по ВСЕМ прогонам, а не строка
# таблицы `sites`. В схеме `sites` привязана к run_id (UNIQUE(run_id, origin)),
# поэтому один и тот же домен, проверенный трижды, лежит тремя записями. Для
# списка портфеля это один сайт с датой последней проверки.
#
# Почему отдельная функция, а не `site_rows`: та готовит ОТЧЁТ и на каждый сайт
# делает четыре обращения к базе плюс строит задачи и тянет ВСЕ находки. На
# портфеле из 500 сайтов это ~2000 запросов и ~460 КБ данных ради списка имён.
# Здесь один агрегирующий запрос, а поиск и постраничность выполняет SQLite:
# объём ответа зависит от размера СТРАНИЦЫ, а не от размера портфеля.

SITES_PAGE = 50          # сколько сайтов на странице по умолчанию
SITES_PAGE_MAX = 200     # предохранитель: не отдаём в UI неограниченную выборку


# Домен из origin средствами SQLite — ровно та же нормализация, что делает
# host_label() в Python: снять схему, лишние слэши и www.
#
# Зачем это в SQL. Группировать список по `origin` НЕЛЬЗЯ: живой вызов показал
# climtec.md ДВАЖДЫ — записями `https://climtec.md/` и `https:///climtec.md`
# (след раннего бага с доменом без схемы; данные в базе остались). Для человека
# это ОДИН подключённый сайт. Считать и группировать в Python значило бы
# выгрузить весь портфель, чтобы посчитать его размер, — то есть потерять всю
# постраничность.
_SQL_HOST = (
    "rtrim("
    "  ltrim("
    "    replace(replace(replace(lower(s.origin),'https://',''),'http://',''),'///','')"
    "  , '/')"
    ", '/')"
)
_SQL_HOST = f"CASE WHEN {_SQL_HOST} LIKE 'www.%' THEN substr({_SQL_HOST}, 5) ELSE {_SQL_HOST} END"


def connected_sites(
    store: Store,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = SITES_PAGE,
) -> tuple[list[dict[str, Any]], int]:
    """Список подключённых сайтов: (страница, всего).

    Сортировка сознательно не по алфавиту: сначала те, что НЕ в порядке
    (ошибка/не проверялся), затем по свежести проверки. В портфеле из 500
    доменов алфавит бесполезен — «что сломалось» важнее «что на букву А».

    `query` фильтрует по подстроке домена на стороне SQLite (LIKE), поэтому
    поиск не требует выгружать портфель в память.
    """
    limit = max(1, min(int(limit or SITES_PAGE), SITES_PAGE_MAX))
    offset = max(0, int(offset or 0))

    where = ""
    params: list[Any] = []
    if query:
        where = f"WHERE {_SQL_HOST} LIKE ? ESCAPE '\\'"
        needle = str(query).strip().lower()
        for ch in ("\\", "%", "_"):      # экранируем метасимволы LIKE
            needle = needle.replace(ch, "\\" + ch)
        params.append(f"%{needle}%")

    db = store.db
    total = db.execute(
        f"SELECT COUNT(DISTINCT {_SQL_HOST}) FROM sites s {where}", params
    ).fetchone()[0]

    # Одна строка на ДОМЕН (не на origin и не на запись таблицы).
    #
    # `sites` привязана к run_id, поэтому домен, проверенный трижды, лежит тремя
    # записями; плюс один и тот же сайт мог попасть в базу как
    # `https://climtec.md/` и как `https:///climtec.md`. Группировка по
    # нормализованному домену сводит всё это в одну строку — «подключённый
    # сайт» в понимании человека.
    #
    # Состояние, ошибка и id берутся из САМОГО СВЕЖЕГО прогона этого домена:
    # в списке важно, как сайт чувствует себя сейчас, а не как год назад.
    rows = db.execute(
        f"""
        WITH norm AS (
            SELECT
                s.id           AS site_id,
                s.origin       AS origin,
                s.state        AS state,
                s.error        AS error,
                s.run_id       AS run_id,
                {_SQL_HOST}    AS host,
                r.started_at   AS started_at
            FROM sites s
            JOIN runs r ON r.id = s.run_id
            {where}
        ),
        ranked AS (
            SELECT norm.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY host ORDER BY started_at DESC, site_id DESC
                   ) AS rn
            FROM norm
        )
        SELECT
            host,
            MAX(started_at)                AS last_seen,
            COUNT(DISTINCT run_id)         AS runs,
            MAX(CASE WHEN rn = 1 THEN origin  END) AS origin,
            MAX(CASE WHEN rn = 1 THEN state   END) AS state,
            MAX(CASE WHEN rn = 1 THEN error   END) AS error,
            MAX(CASE WHEN rn = 1 THEN site_id END) AS last_site_id
        FROM ranked
        GROUP BY host
        ORDER BY
            CASE WHEN state = 'done' THEN 1 ELSE 0 END,  -- проблемные сверху
            last_seen DESC,
            host
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        site_id = row["last_site_id"]
        pages = store.count_pages(site_id) if site_id else 0
        out.append({
            "origin": row["origin"],
            "host": row["host"] or host_label(row["origin"] or ""),
            "state": row["state"] or "pending",
            "error": row["error"] or "",
            "runs": int(row["runs"] or 0),
            "pages": pages,
            "last_seen": float(row["last_seen"] or 0.0),
            "last_site_id": site_id,
        })
    return out, int(total)


def when_label(ts: float) -> str:
    """Человеческое «когда»: сегодня / вчера / N дней назад / дата.

    Точное время в списке из 500 строк не читают — важно «свежо или давно».
    """
    if not ts:
        return "не проверялся"
    import time as _time

    delta = _time.time() - float(ts)
    if delta < 0:
        return "только что"
    days = int(delta // 86400)
    if days == 0:
        hours = int(delta // 3600)
        if hours == 0:
            return "только что"
        return f"{hours} ч назад"
    if days == 1:
        return "вчера"
    if days < 30:
        return f"{days} дн назад"
    return _time.strftime("%d.%m.%Y", _time.localtime(ts))


# Подписи состояний сайта — ОДНО место на всё приложение: и панель, и чат.
# Дублировать их в двух файлах значит однажды поправить в одном и получить
# «проверен» в панели и «done» в чате на одном и том же сайте.
STATE_LABELS = {
    "done": "проверен",
    "error": "не открылся",
    "pending": "в очереди",
    "discovering": "изучается",
    "fetching": "проверяется",
}


def state_label(state: str) -> str:
    """Человеческая подпись состояния. Неизвестное — как есть, без выдумок."""
    key = (state or "").strip().lower()
    return STATE_LABELS.get(key, key or "—")


# --- всё про ОДИН сайт -------------------------------------------------------

def site_detail(store: Store, host: str, *, min_severity: str = "medium",
                ) -> dict[str, Any] | None:
    """Полная картина одного сайта. None — если такого домена в базе нет.

    Точечно по одному домену, а НЕ через `site_rows`: та перебирает все сайты
    прогона, поднимая находки и строя задачи для каждого. На портфеле из 500
    доменов это ~460 КБ, чтобы показать один сайт. Здесь берётся ровно одна
    запись — самая свежая для этого домена.

    Домен ищется нормализованным (без схемы, слэшей и www) по той же причине,
    что и в списке: одна площадка могла попасть в базу как `https://climtec.md/`
    и как `https:///climtec.md`. Пользователь нажал на ОДНУ строку и обязан
    получить эту строку, а не «сайт не найден» из-за формы записи.
    """
    needle = host_label(host)
    if not needle:
        return None

    row = store.db.execute(
        f"""
        SELECT s.id AS site_id, s.origin AS origin, s.state AS state,
               s.error AS error, s.run_id AS run_id, r.started_at AS started_at,
               r.label AS run_label
        FROM sites s
        JOIN runs r ON r.id = s.run_id
        WHERE {_SQL_HOST} = ?
        ORDER BY r.started_at DESC, s.id DESC
        LIMIT 1
        """,
        [needle],
    ).fetchone()
    if row is None:
        return None

    site_id = row["site_id"]
    findings = [dict(f) for f in store.findings(site_id)]
    pages = store.count_pages(site_id)
    tasks = build_tasks(row["origin"], findings, min_severity=min_severity)

    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1

    # История: тот же домен в других прогонах — видно, стало лучше или хуже.
    history: list[dict[str, Any]] = []
    for h in store.db.execute(
        f"""
        SELECT s.id AS site_id, s.state AS state, r.started_at AS started_at,
               r.id AS run_id, r.label AS run_label
        FROM sites s
        JOIN runs r ON r.id = s.run_id
        WHERE {_SQL_HOST} = ?
        ORDER BY r.started_at DESC, s.id DESC
        LIMIT 12
        """,
        [needle],
    ):
        h_pages = store.count_pages(h["site_id"])
        history.append({
            "run_id": h["run_id"],
            "run_label": h["run_label"] or "",
            "state": h["state"],
            "pages": h_pages,
            "score": store.site_score(h["site_id"], h_pages),
            "when": when_label(h["started_at"]),
        })

    return {
        "host": needle,
        "origin": row["origin"],
        "site_id": site_id,
        "state": row["state"],
        "error": row["error"] or "",
        "run_id": row["run_id"],
        "run_label": row["run_label"] or "",
        "checked": when_label(row["started_at"]),
        "pages": pages,
        "score": store.site_score(site_id, pages),
        "findings": findings,
        "tasks": tasks,
        "by_severity": by_sev,
        "history": history,
    }


def site_pages(store: Store, site_id: int, findings: list[dict[str, Any]],
               *, only_issues: bool = False) -> list[dict[str, Any]]:
    """Каждая страница сайта + её находки — основа экрана «Страницы».

    Находки на страницу поднимаются через `filter_findings_by_page` — ТУ ЖЕ
    функцию, что использует `list_findings(page_url=...)` в чате. Если бы
    здесь было отдельное правило «что относится к странице», экран и чат со
    временем ответили бы на один вопрос по-разному.

    `only_issues=True` прячет страницы без единой находки — фильтр для
    портфеля, где страниц может быть много больше, чем реальных проблем.
    """
    out: list[dict[str, Any]] = []
    for row in store.pages(site_id, only_done=False):
        d = dict(row)
        url = d.get("final_url") or d.get("url") or ""
        if not url:
            continue
        head = d.get("head")
        if isinstance(head, str):
            try:
                head = json.loads(head)
            except (ValueError, TypeError):
                head = {}
        head = head if isinstance(head, dict) else {}

        page_findings = filter_findings_by_page(findings, url)
        if only_issues and not page_findings:
            continue

        by_sev: dict[str, int] = {}
        for f in page_findings:
            by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        worst = ""
        best_rank = 99
        for sev in by_sev:
            r = severity_rank(sev)
            if r < best_rank:
                best_rank, worst = r, sev

        out.append({
            "url": url,
            "title": head.get("title") or "",
            "status": d.get("status"),
            "state": d.get("state"),
            "fetched_at": d.get("fetched_at"),
            "findings_count": len(page_findings),
            "by_severity": by_sev,
            "worst_severity": worst,
        })

    out.sort(key=lambda p: (severity_rank(p["worst_severity"]) if p["worst_severity"] else 99,
                            p["url"]))
    return out


def findings_by_layer(findings: list[dict[str, Any]]) -> list[tuple[int, str, list[dict[str, Any]]]]:
    """Находки, сгруппированные по слою проверки, слои — по порядку.

    Слой отвечает на вопрос «где болит»: доступность, скорость, разметка. Плоский
    список из сорока находок читать невозможно, а по слоям видно, что сайт
    вообще-то живой, просто разметка хромает.
    """
    groups: dict[int, list[dict[str, Any]]] = {}
    for f in findings:
        groups.setdefault(int(f.get("layer") or 0), []).append(f)

    out: list[tuple[int, str, list[dict[str, Any]]]] = []
    for layer in sorted(groups):
        items = sorted(groups[layer],
                       key=lambda x: severity_rank(x.get("severity", "")))
        out.append((layer, layer_name(layer), items))
    return out


def latest_run_for_host(store: Store, host: str) -> int | None:
    """Последний прогон, в котором ЭТОТ домен действительно проверялся.

    Нужно, потому что `resolve_run` даёт последний прогон ВООБЩЕ. Портфель
    проверяют частями: сегодня один сайт, завтра другой. Спросить отчёт по
    climtec.md сразу после аудита другого сайта — и получить «такого сайта
    нет», хотя он проверен и лежит в базе. Для человека это выглядит как
    потеря данных, а не как «вы смотрите не тот прогон».

    Домен нормализуется так же, как в списке и в `site_detail`.
    """
    needle = host_label(host)
    if not needle:
        return None
    row = store.db.execute(
        f"""
        SELECT s.run_id AS run_id
        FROM sites s
        JOIN runs r ON r.id = s.run_id
        WHERE {_SQL_HOST} = ?
        ORDER BY r.started_at DESC, s.id DESC
        LIMIT 1
        """,
        [needle],
    ).fetchone()
    return int(row["run_id"]) if row else None


# ── Правки: находки -> конкретные значения полей ──────────────────────────

def fixes_for_site(store: Store, site_row: dict[str, Any],
                   *, only_ready: bool = False) -> list[dict[str, Any]]:
    """Готовые правки по одному сайту.

    Собирает то, чего между аудитом и починкой всегда не хватало: не «нет
    описания на восьми страницах», а «страница X, поле Y, значение Z».

    `head` в БД лежит JSON-СТРОКОЙ — та же ловушка, что в движке при resume.
    Без разбора генератор получил бы пустые страницы и предложил бы собирать
    заголовки из адресов там, где на странице есть человеческий H1.
    """
    pages: dict[str, dict[str, Any]] = {}
    for row in store.pages(site_row["id"], only_done=True):
        d = dict(row)
        raw = d.get("head")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = {}
        if isinstance(raw, dict):
            for k in ("title", "description", "canonical", "h1", "html_lang",
                      "og_title"):
                if k in raw:
                    d[k] = raw[k]
        key = d.get("final_url") or d.get("url") or ""
        if key:
            pages[key] = d

    findings = [dict(f) for f in store.findings(site_row["id"])]
    items = build_fixes(findings, pages, site_row.get("origin", ""))
    if only_ready:
        items = [f for f in items if f.ready]
    return [f.to_dict() for f in items]


def fixes_summary(fix_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Сводка по правкам — для заголовка ответа.

    Считается по УЖЕ преобразованным словарям, а не по объектам: правки могли
    прийти из нескольких сайтов и быть склеены, и пересобирать из них объекты
    ради подсчёта — лишний повод разойтись двум формам одних данных.
    """
    by_field: dict[str, int] = {}
    for f in fix_rows:
        key = f.get("field", "")
        by_field[key] = by_field.get(key, 0) + 1
    ready = [f for f in fix_rows if f.get("ready")]
    return {
        "total": len(fix_rows),
        "ready": len(ready),
        "needs_review": len(fix_rows) - len(ready),
        "pages": len({f.get("url", "") for f in fix_rows}),
        "by_field": by_field,
    }


# ── Сравнение прогонов ────────────────────────────────────────────────────

def previous_run_for_site(store: Store, host: str, before_run: int) -> int | None:
    """Прогон того же сайта, предшествующий указанному.

    Ищем по НОРМАЛИЗОВАННОМУ хосту, а не по origin: между прогонами сайт
    мог переехать с http на https или начать отвечать с www. Сравнивать
    такие прогоны нужно — это один и тот же сайт, и как раз переезд стоит
    увидеть в изменениях, а не потерять как «нет прошлого прогона».
    """
    needle = host_label(host)
    if not needle:
        return None
    row = store.db.execute(
        f"""
        SELECT s.run_id AS run_id
        FROM sites s
        JOIN runs r ON r.id = s.run_id
        WHERE {_SQL_HOST} = ? AND s.run_id < ?
        ORDER BY r.started_at DESC, s.id DESC
        LIMIT 1
        """,
        [needle, before_run],
    ).fetchone()
    return int(row["run_id"]) if row else None


def site_in_run(store: Store, host: str, run_id: int) -> dict[str, Any] | None:
    """Строка сайта в конкретном прогоне — по нормализованному хосту."""
    needle = host_label(host)
    if not needle:
        return None
    row = store.db.execute(
        f"""
        SELECT s.id AS id, s.origin AS origin, s.state AS state
        FROM sites s
        WHERE {_SQL_HOST} = ? AND s.run_id = ?
        LIMIT 1
        """,
        [needle, run_id],
    ).fetchone()
    return dict(row) if row else None


def compare_runs(store: Store, host: str, *, after_run: int,
                 before_run: int = 0):
    """Сравнение двух прогонов одного сайта. Возвращает Comparison или None."""
    after_site = site_in_run(store, host, after_run)
    if after_site is None:
        return None

    if not before_run:
        prev = previous_run_for_site(store, host, after_run)
        if prev is None:
            return None
        before_run = prev

    before_site = site_in_run(store, host, before_run)
    if before_site is None:
        return None

    before_pages = store.count_pages(before_site["id"])
    after_pages = store.count_pages(after_site["id"])

    return compare_findings(
        [dict(f) for f in store.findings(before_site["id"])],
        [dict(f) for f in store.findings(after_site["id"])],
        origin=after_site["origin"],
        before_run=before_run,
        after_run=after_run,
        before_score=store.site_score(before_site["id"], before_pages),
        after_score=store.site_score(after_site["id"], after_pages),
        before_pages=before_pages,
        after_pages=after_pages,
    )
