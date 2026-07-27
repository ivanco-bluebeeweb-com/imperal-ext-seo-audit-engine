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

# Движок лежит рядом как обычный пакет — расширение его ИМПОРТИРУЕТ, а не
# копирует логику: правило аудита правится в одном месте.
from seoaudit.engine import AuditConfig, Engine
from seoaudit.fetcher import FetchPolicy
from seoaudit.reports import host_of, portfolio_report_md, site_report_md
from seoaudit.severity import CRITICAL, HIGH, LAYER_NAMES, SEVERITY_ORDER
from seoaudit.store import Store
from seoaudit.tasks import build_tasks
from seoaudit.export_tracker import plan_for_tracker, summarise_plan

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


def with_scheme(site: str) -> str:
    """Дописать https://, если человек назвал домен без схемы.

    ЗАЧЕМ ЭТО ЗДЕСЬ. `normalize_url` внутри движка опирается на `urlsplit`, а
    тот в строке `climtec.md` видит ПУТЬ, а не хост: hostname пустой, и функция
    честно возвращает ввод как есть. Дальше движок склеивал схему с таким
    «origin» и получалось `https:///climtec.md` — три слэша, и сайт становился
    неоткрываемым.

    Починка сделана на границе ВВОДА, а не внутри движка: `normalize_url`
    применяется и к ссылкам, найденным на страницах, где голая строка без схемы
    — это действительно относительный путь, и додумывать ей схему было бы
    ошибкой. Схему домысливаем ровно там, где человек ввёл домен руками.
    """
    s = (site or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://")):
        return s
    return "https://" + s.lstrip("/")


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


def resolve_run(store: Store, run_id: int = 0) -> int:
    """Номер прогона: явный или последний. 0 — прогонов нет вовсе."""
    if run_id:
        row = store.get_run(run_id)
        return int(row["id"]) if row else 0
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
