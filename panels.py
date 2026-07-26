"""Панели SEO Audit Engine.

ОДНА ПАНЕЛЬ НА СЛОТ — с самого начала, а не как исправление.

В Notion Connector две панели были объявлены `slot="center"` с
`center_overlay=True`. Хост забирает все слоты одним пакетом при инициализации
сессии, а центральный слот держит РОВНО ОДНУ панель с семантикой замены: без
стека и без вкладок. Обе загружались, одна молча вытесняла другую, и кнопка,
адресованная проигравшей, выглядела так: «сайдбар перезагрузился, ничего не
произошло». Починить саму кнопку было невозможно — ошибка структурная.

Поэтому здесь центральная панель ОДНА (`seo`), а экран — ПАРАМЕТР `view`:

    ui.Call("__panel__seo")                  -> портфель (по умолчанию)
    ui.Call("__panel__seo", view="findings")  -> находки
    ui.Call("__panel__seo", view="tasks")     -> задачи

Так вызов всегда попадает в панель, которая реально смонтирована. Структурный
тест падает, если кто-то снова заведёт вторую панель на том же слоте.

ВТОРОЙ УРОК: панель никогда не показывает пустой экран при сбое. Если чтение
упало — рисуем баннер с человеческим текстом и кнопкой «обновить», а внутренности
(пути, типы исключений) в UI не утекают: они уходят в `ctx.log`.
"""

from __future__ import annotations

from typing import Any

from imperal_sdk import ui

import bridge as br
from app import ext

# Человеческие подписи уровней. Английские коды движка в UI не место.
SEV_LABEL = {
    "critical": "Критично",
    "high": "Важно",
    "medium": "Средне",
    "low": "Гигиена",
    "info": "Наблюдение",
}

# Текст, который объясняет пустой экран новичку. Пустота у нас — не ошибка,
# а состояние «ещё не запускали», и она обязана содержать следующий шаг.
FIRST_RUN = (
    "Аудитов ещё не было. Напишите в чате, например: «проверь climtec.md» — "
    "или «проверь climtec.md и ksrenovationgroup.com». Аудит только читает "
    "сайты и ничего на них не меняет."
)


async def _load(ctx, min_severity: str = "medium") -> dict[str, Any]:
    """Прочитать портфель для панели.

    Возвращает словарь с данными либо с полем `problem` — коротким человеческим
    текстом. Панель обязана объяснить пустоту, а не молчать.
    """
    try:
        path = await br.download_db(ctx)
    except Exception as exc:
        await ctx.log(f"panel: storage read failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось получить результаты аудита. Попробуйте обновить."}

    if not path:
        return {"first_run": True}

    try:
        store = br.open_store(path)
    except Exception as exc:
        await ctx.log(f"panel: db unreadable: {type(exc).__name__}", "error")
        return {"problem": "Результаты прошлых аудитов не читаются. "
                           "Запустите аудит заново — новые данные это исправят."}

    try:
        run_id = br.resolve_run(store)
        if not run_id:
            return {"first_run": True}
        rows, tasks_by_site = br.site_rows(store, run_id,
                                           min_severity=min_severity)
        return {
            "run_id": run_id,
            "label": br.run_label(store, run_id),
            "rows": rows,
            "tasks_by_site": tasks_by_site,
        }
    except Exception as exc:
        await ctx.log(f"panel: load failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось собрать сводку. Попробуйте обновить."}
    finally:
        try:
            store.close()
        except Exception:
            pass


def _banner(text: str) -> Any:
    """Баннер с кнопкой обновления — вместо пустого экрана."""
    return ui.Stack(direction="vertical", gap=3, children=[
        ui.Alert(type="error", message=text),
        ui.Button(label="Обновить", on_click=ui.Call("__panel__seo"),
                  variant="secondary"),
    ])


def _first_run() -> Any:
    return ui.Stack(direction="vertical", gap=3, children=[
        ui.Header("SEO-аудит портфеля", subtitle="Пока пусто"),
        ui.Empty(message=FIRST_RUN),
    ])


def _portfolio_view(data: dict[str, Any]) -> Any:
    """Сводка по портфелю: оценка, страницы, задачи, главная проблема."""
    rows = data["rows"]
    done = [r for r in rows if r["state"] == "done"]
    failed = [r for r in rows if r["state"] != "done"]
    total_tasks = sum(r["tasks"] for r in done)
    total_pages = sum(r["pages"] for r in done)
    avg = round(sum(r["score"] for r in done) / len(done)) if done else 0

    label = data.get("label") or ""
    head = ui.Header(
        "SEO-аудит портфеля" + (f": {label}" if label else ""),
        subtitle=(f"Сайтов: {len(done)} · страниц проверено: {total_pages} · "
                  f"задач к работе: {total_tasks} · средняя оценка: {avg}/100"),
    )

    table = ui.DataTable(
        columns=[
            ui.DataColumn(key="site", label="Сайт"),
            ui.DataColumn(key="score", label="Оценка"),
            ui.DataColumn(key="pages", label="Страниц"),
            ui.DataColumn(key="tasks", label="Задач"),
            ui.DataColumn(key="top", label="Главное"),
        ],
        rows=[{
            "site": br.host_label(r["origin"]),
            "score": f"{r['score']}/100",
            "pages": str(r["pages"]),
            "tasks": str(r["tasks"]),
            "top": (r["top_issue"] or "—")[:80],
        } for r in sorted(rows, key=lambda x: x["score"])],
    )

    children = [head, table]

    # Сайты, которые не открылись, показываем ОТДЕЛЬНО и честно: движок про них
    # ничего не утверждает, поэтому и панель не должна выдавать их за «0 проблем».
    if failed:
        names = ", ".join(br.host_label(r["origin"]) for r in failed[:6])
        children.append(ui.Alert(
            type="warn",
            message=(f"Не удалось проверить: {names}. Про эти сайты аудит "
                     f"ничего не утверждает — это не «всё хорошо»."),
        ))

    children.append(ui.Row(gap=2, children=[
        ui.Button(label="Находки",
                  on_click=ui.Call("__panel__seo", view="findings")),
        ui.Button(label="Задачи",
                  on_click=ui.Call("__panel__seo", view="tasks")),
        ui.Button(label="Обновить", variant="ghost",
                  on_click=ui.Call("__panel__seo")),
    ]))
    return ui.Stack(direction="vertical", gap=3, children=children)


def _findings_view(data: dict[str, Any]) -> Any:
    """Находки всего портфеля, важное сверху."""
    rows = data["rows"]

    table = ui.DataTable(
        columns=[
            ui.DataColumn(key="site", label="Сайт"),
            ui.DataColumn(key="critical", label="Критично"),
            ui.DataColumn(key="high", label="Важно"),
            ui.DataColumn(key="medium", label="Средне"),
            ui.DataColumn(key="low", label="Гигиена"),
        ],
        rows=[{
            "site": br.host_label(r["origin"]),
            "critical": str(r["by_severity"].get("critical", 0)),
            "high": str(r["by_severity"].get("high", 0)),
            "medium": str(r["by_severity"].get("medium", 0)),
            "low": str(r["by_severity"].get("low", 0)),
        } for r in sorted(rows, key=lambda x: -(
            x["by_severity"].get("critical", 0) * 10
            + x["by_severity"].get("high", 0)))],
    )

    return ui.Stack(direction="vertical", gap=3, children=[
        ui.Header("Находки по уровням",
                  subtitle="Подробности по сайту — спросите в чате: "
                           "«покажи находки climtec.md»"),
        table,
        ui.Row(gap=2, children=[
            ui.Button(label="К портфелю", on_click=ui.Call("__panel__seo")),
            ui.Button(label="Задачи", variant="ghost",
                      on_click=ui.Call("__panel__seo", view="tasks")),
        ]),
    ])


def _tasks_view(data: dict[str, Any]) -> Any:
    """Задачи — то, что реально пойдёт в трекер."""
    tasks_by_site: dict[str, list] = data["tasks_by_site"]
    flat: list[tuple[str, Any]] = []
    for origin, tasks in tasks_by_site.items():
        for t in tasks:
            flat.append((origin, t))
    flat.sort(key=lambda pair: (br.severity_rank(pair[1].severity),
                                pair[0], pair[1].rule))

    if not flat:
        return ui.Stack(direction="vertical", gap=3, children=[
            ui.Header("Задачи", subtitle="Пока нечего делать"),
            ui.Empty(message="Задач выше порога «средне» нет — по этим сайтам "
                             "критичных работ не найдено."),
            ui.Button(label="К портфелю", on_click=ui.Call("__panel__seo")),
        ])

    table = ui.DataTable(
        columns=[
            ui.DataColumn(key="severity", label="Важность"),
            ui.DataColumn(key="site", label="Сайт"),
            ui.DataColumn(key="title", label="Что сделать"),
            ui.DataColumn(key="pages", label="Страниц"),
        ],
        rows=[{
            "severity": SEV_LABEL.get(t.severity, t.severity),
            "site": br.host_label(origin),
            "title": t.title[:90],
            "pages": str(t.count),
        } for origin, t in flat[:60]],
    )

    return ui.Stack(direction="vertical", gap=3, children=[
        ui.Header("Задачи к работе",
                  subtitle=f"Всего: {len(flat)} · одна задача = один дефект "
                           f"на одном сайте, со списком страниц внутри"),
        table,
        ui.Text(content="Выгрузить в трекер: скажите в чате «выгрузи план "
                        "в Asana» — задачи создаст коннектор трекера, "
                        "по подтверждению.",
                variant="caption"),
        ui.Row(gap=2, children=[
            ui.Button(label="К портфелю", on_click=ui.Call("__panel__seo")),
            ui.Button(label="Находки", variant="ghost",
                      on_click=ui.Call("__panel__seo", view="findings")),
        ]),
    ])


@ext.panel("seo", slot="center", title="SEO-аудит", icon="Search",
           center_overlay=True, refresh="manual")
async def seo_center(ctx, **kwargs):
    """ЕДИНСТВЕННАЯ центральная панель. `view` выбирает экран внутри неё.

    SKETCH -- center panel
      ui.Stack (v, gap=3)
        ui.Header(<портфель | находки | задачи>)
        ui.DataTable(...)          # данные экрана
        ui.Alert(warn)             # только если часть сайтов не открылась
        ui.Row -> ui.Button * 3    # переключение экранов
    """
    view = str(kwargs.get("view") or "").strip().lower()
    data = await _load(ctx)

    if data.get("problem"):
        return _banner(data["problem"])
    if data.get("first_run"):
        return _first_run()

    if view == "findings":
        return _findings_view(data)
    if view == "tasks":
        return _tasks_view(data)
    return _portfolio_view(data)


@ext.panel("seo_nav", slot="left", title="SEO-аудит", icon="Search",
           refresh="manual")
async def seo_nav(ctx, **kwargs):
    """Боковая запись: состояние портфеля одной строкой и вход внутрь.

    SKETCH -- left nav
      ui.Stack (v, gap=2)
        ui.Text(content=<состояние>, variant="body")
        ui.Button("Открыть портфель", full_width=True)
        ui.Button("Задачи", variant="ghost", full_width=True)
    """
    data = await _load(ctx)

    if data.get("problem"):
        state = "Результаты недоступны"
    elif data.get("first_run"):
        state = "Аудитов ещё не было"
    else:
        rows = data["rows"]
        done = [r for r in rows if r["state"] == "done"]
        tasks = sum(r["tasks"] for r in done)
        state = f"Сайтов: {len(done)} · задач: {tasks}"

    return ui.Stack(direction="vertical", gap=2, children=[
        ui.Text(content=state, variant="body"),
        ui.Button(label="Открыть портфель", full_width=True,
                  on_click=ui.Call("__panel__seo")),
        ui.Button(label="Задачи", variant="ghost", full_width=True,
                  on_click=ui.Call("__panel__seo", view="tasks")),
    ])
