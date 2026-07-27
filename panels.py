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
import schedule_settings as sched
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
    "Аудитов ещё не было. Нажмите «Добавить сайт» и введите домен — например "
    "climtec.md. Или скажите в чате: «проверь climtec.md». Аудит только читает "
    "сайты и ничего на них не меняет."
)


async def _load_site(ctx, host: str) -> dict[str, Any]:
    """Прочитать всё про ОДИН сайт. Точечно, без обхода портфеля."""
    try:
        path = await br.download_db(ctx)
    except Exception as exc:
        await ctx.log(f"panel: storage read failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось получить данные сайта. Попробуйте обновить."}

    if not path:
        return {"first_run": True}

    try:
        store = br.open_store(path)
    except Exception as exc:
        await ctx.log(f"panel: db unreadable: {type(exc).__name__}", "error")
        return {"problem": "Данные аудита не читаются. Запустите аудит заново."}

    try:
        detail = br.site_detail(store, host)
        if detail is None:
            # Не ошибка: сайт мог быть удалён из портфеля или домен набран
            # руками с опечаткой. Человеку нужен путь назад, а не «сбой».
            return {"missing": br.host_label(host) or host}
        return {"detail": detail}
    except Exception as exc:
        await ctx.log(f"panel: site detail failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось собрать данные сайта. Попробуйте обновить."}
    finally:
        try:
            store.close()
        except Exception:
            pass


def _int_param(raw: Any, default: int = 0) -> int:
    """Целое из параметра панели — молча и безопасно.

    Параметры приходят из UI строками, а иногда мусором: offset="abc" или
    пустая строка. Панель обязана в этом случае показать первую страницу, а не
    упасть с ошибкой рендера — пользователь не виноват в чужом клике.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


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


async def _load_sites(ctx, *, query: str = "", offset: int = 0,
                      limit: int = br.SITES_PAGE) -> dict[str, Any]:
    """Лёгкое чтение ТОЛЬКО списка подключённых сайтов.

    Отдельно от `_load` намеренно. `_load` готовит сводку и поднимает находки
    по всем сайтам: на портфеле из 500 доменов это ~460 КБ и 500 построенных
    задач — ради списка имён. Здесь один агрегирующий запрос, а поиск и
    постраничность делает SQLite, поэтому объём ответа зависит от размера
    СТРАНИЦЫ, а не портфеля.
    """
    try:
        path = await br.download_db(ctx)
    except Exception as exc:
        await ctx.log(f"panel: storage read failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось получить список сайтов. Попробуйте обновить."}

    if not path:
        return {"first_run": True}

    try:
        store = br.open_store(path)
    except Exception as exc:
        await ctx.log(f"panel: db unreadable: {type(exc).__name__}", "error")
        return {"problem": "Список сайтов не читается. Запустите аудит заново."}

    try:
        page, total = br.connected_sites(store, query=query,
                                         offset=offset, limit=limit)
        if not total and not query:
            return {"first_run": True}
        return {"sites": page, "total": total, "query": query,
                "offset": offset, "limit": limit}
    except Exception as exc:
        await ctx.log(f"panel: sites load failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось собрать список сайтов. Попробуйте обновить."}
    finally:
        try:
            store.close()
        except Exception:
            pass


def _banner(text: str, *, kind: str = "error", back_to_sites: bool = False) -> Any:
    """Баннер вместо пустого экрана — и всегда с выходом к действию.

    Кнопка «Добавить сайт» есть и здесь: сбой ЧТЕНИЯ прошлых результатов не
    мешает запустить новый аудит, и оставлять пользователя в тупике с одной
    кнопкой «обновить» было бы неправильно.

    `kind` отличает СБОЙ от простого «этого нет». Красный alert на «сайт не
    найден» врёт: ничего не сломалось, домен просто ещё не проверялся.
    `back_to_sites` возвращает в список — если человек пришёл со страницы
    сайта, ему нужно назад именно туда, откуда он нажал.
    """
    actions = [
        ui.Button(label="+ Добавить сайт",
                  on_click=ui.Call("__panel__seo", view="add")),
    ]
    if back_to_sites:
        actions.append(ui.Button(label="← Все сайты", variant="secondary",
                                 on_click=ui.Call("__panel__seo", view="sites")))
    actions.append(ui.Button(label="Обновить", variant="ghost",
                             on_click=ui.Call("__panel__seo")))

    return ui.Stack(direction="v", gap=3, children=[
        ui.Alert(type=kind, message=text),
        ui.Row(gap=2, children=actions),
    ])


# ТОЛЬКО цвета. Сами подписи живут в bridge.state_label — один источник на
# панель и чат, иначе однажды поправишь в одном месте и получишь «проверен» в
# панели и «done» в чате про один и тот же сайт.
STATE_COLOUR = {
    "done": "green",
    "error": "red",
    "pending": "gray",
    "discovering": "blue",
    "fetching": "blue",
}


def _sites_view(data: dict[str, Any]) -> Any:
    """Список подключённых сайтов — рассчитан на портфель из сотен доменов.

    ПОЧЕМУ ПОИСК СВОЙ, А НЕ `searchable=True`.
    `ui.List(searchable=True)` фильтрует те элементы, которые ЕМУ ПЕРЕДАЛИ. При
    постраничной выдаче это означает поиск по текущей странице — на портфеле из
    500 сайтов пользователь набрал бы «climtec», не увидел его на странице 1 из
    10 и решил, что сайт не подключён. Тихая ложь хуже отсутствия поиска.
    Поэтому поиск уходит параметром в панель и выполняется в SQLite по ВСЕМУ
    портфелю, а `searchable` выключен намеренно.

    ПОЧЕМУ СТРАНИЦАМИ. Панель обязана оставаться предсказуемой при 500 сайтах:
    отдаём страницу (50 по умолчанию) и общее число, чтобы человек видел
    «50 из 500», а не бесконечную простыню. Объём ответа зависит от размера
    страницы, а не портфеля.

    SKETCH -- view="sites"
      ui.Stack (v, gap=3)
        ui.Header("Подключённые сайты", subtitle="всего N")
        ui.Form(action="__panel__seo") -> ui.Input(param_name="q")   # поиск по всем
        ui.List(items=[ui.ListItem * page], total_items=N, extra_info="50 из 500")
        ui.Row -> ui.Button("Назад") / ui.Button("Дальше")           # если страниц > 1
    """
    sites = data.get("sites") or []
    total = int(data.get("total") or 0)
    query = str(data.get("query") or "")
    offset = int(data.get("offset") or 0)
    limit = int(data.get("limit") or br.SITES_PAGE)

    items: list[Any] = []
    for row in sites:
        host = row["host"]
        label = br.state_label(row["state"])
        colour = STATE_COLOUR.get(row["state"], "gray")
        bits = [label]
        if row["pages"]:
            bits.append(f"{row['pages']} стр.")
        if row["runs"] > 1:
            bits.append(f"проверок: {row['runs']}")
        items.append(ui.ListItem(
            id=host,
            title=host,
            subtitle=" · ".join(bits),
            meta=br.when_label(row["last_seen"]),
            badge=ui.Badge(label=label, color=colour),
            # Клик по строке открывает страницу сайта. Кнопка «Подробнее»
            # дублирует его намеренно: клик по строке — догадка, а видимая
            # кнопка сообщает, что деталь вообще существует.
            on_click=ui.Call("__panel__seo", view="site", site=host),
            actions=[
                {"icon": "ArrowRight", "label": "Подробнее",
                 "on_click": ui.Call("__panel__seo", view="site", site=host)},
                {"icon": "RefreshCw", "label": "Перепроверить",
                 "on_click": ui.Call("audit_sites", sites=host)},
            ],
        ))

    shown_from = offset + 1 if items else 0
    shown_to = offset + len(items)
    footer = (f"{shown_from}\u2013{shown_to} из {total}"
              if total > len(items) else f"всего {total}")

    children: list[Any] = [
        ui.Header("Подключённые сайты",
                  subtitle=(f"найдено {total} по запросу «{query}»" if query
                            else f"всего {total}")),
        # Поиск по ВСЕМУ портфелю: значение уходит в панель как параметр q.
        ui.Form(action="__panel__seo", submit_label="Найти",
                defaults={"view": "sites"},
                children=[
                    ui.Input(placeholder="часть домена, например climtec",
                             param_name="q", value=query),
                ]),
    ]

    if items:
        children.append(ui.List(items=items, searchable=False,
                                total_items=total, extra_info=footer))
    elif query:
        children.append(ui.Empty(
            message=f"По запросу «{query}» ничего не нашлось. "
                    f"Всего подключено сайтов: {total}."))
    else:
        children.append(ui.Empty(message="Пока ни одного сайта."))

    # Постраничная навигация — только если страниц действительно больше одной.
    nav: list[Any] = []
    if offset > 0:
        nav.append(ui.Button(label="\u2190 Назад", variant="secondary",
                             on_click=ui.Call("__panel__seo", view="sites",
                                              q=query,
                                              offset=max(0, offset - limit),
                                              limit=limit)))
    if shown_to < total:
        nav.append(ui.Button(label="Дальше \u2192", variant="secondary",
                             on_click=ui.Call("__panel__seo", view="sites",
                                              q=query, offset=offset + limit,
                                              limit=limit)))
    if query:
        nav.append(ui.Button(label="Сбросить поиск", variant="ghost",
                             on_click=ui.Call("__panel__seo", view="sites")))
    if nav:
        children.append(ui.Row(gap=2, children=nav))

    children.append(ui.Row(gap=2, children=[
        ui.Button(label="+ Добавить сайт",
                  on_click=ui.Call("__panel__seo", view="add")),
        ui.Button(label="Сводка по портфелю", variant="secondary",
                  on_click=ui.Call("__panel__seo")),
    ]))
    return ui.Stack(direction="v", gap=3, children=children)


def _first_run() -> Any:
    """Первый запуск: объяснение и сразу способ начать."""
    return ui.Stack(direction="v", gap=3, children=[
        ui.Header("SEO-аудит портфеля", subtitle="Пока пусто"),
        ui.Empty(message=FIRST_RUN,
                 action=ui.Call("__panel__seo", view="add")),
        ui.Button(label="+ Добавить сайт",
                  on_click=ui.Call("__panel__seo", view="add")),
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
        ui.Button(label="+ Добавить сайт",
                  on_click=ui.Call("__panel__seo", view="add")),
        ui.Button(label="Находки", variant="secondary",
                  on_click=ui.Call("__panel__seo", view="findings")),
        ui.Button(label="Задачи", variant="secondary",
                  on_click=ui.Call("__panel__seo", view="tasks")),
        ui.Button(label="Обновить", variant="ghost",
                  on_click=ui.Call("__panel__seo")),
    ]))
    return ui.Stack(direction="v", gap=3, children=children)


SEVERITY_LABEL = {
    "critical": ("критично", "red"),
    "high": ("важно", "orange"),
    "medium": ("средне", "yellow"),
    "low": ("гигиена", "gray"),
    "info": ("к сведению", "gray"),
}


def _site_view(data: dict[str, Any]) -> Any:
    """Страница ОДНОГО сайта: всё, что аудит про него поднял.

    Порядок блоков — по вопросам, которые человек задаёт в этом порядке:
      1. как дела вообще        -> оценка, страницы, когда проверяли
      2. что делать            -> задачи (одна задача = одна операция)
      3. что именно не так     -> находки по слоям, с адресами страниц
      4. стало лучше или хуже  -> история прошлых проверок
    Сначала «что делать», потом «что не так»: список из сорока находок без
    вывода бесполезен, а задача сразу говорит действие.

    Находки сгруппированы по слоям, потому что слой отвечает «где болит» —
    доступность, скорость, разметка. Плоский список читать невозможно, а по
    слоям видно, что сайт живой, просто разметка хромает.

    SKETCH -- view="site"
      ui.Stack (v, gap=3)
        ui.Header(<домен>, subtitle=<состояние · когда>)
        ui.Stats -> Stat * 4              # оценка, страницы, задачи, находки
        ui.Alert                          # только если сайт не открылся
        ui.Section("Что делать") -> ui.List(задачи, expandable)
        ui.Section("Что не так") -> Accordion по слоям
        ui.Section("История") -> ui.Timeline
        ui.Row -> Назад к списку / Перепроверить
    """
    host = data["host"]
    score = int(data.get("score") or 0)
    pages = int(data.get("pages") or 0)
    tasks = data.get("tasks") or []
    findings = data.get("findings") or []
    by_sev = data.get("by_severity") or {}

    children: list[Any] = [
        # «проверен 19 ч назад» рядом с «не открылся» противоречит само себе:
        # попытка была, а проверки как раз не случилось. Нейтральная
        # «последняя проверка» верна в обоих случаях.
        ui.Header(host,
                  subtitle=f"{br.state_label(data.get('state'))} · "
                           f"последняя проверка: {data.get('checked') or '—'}"),
        ui.Stats(children=[
            ui.Stat(label="Оценка", value=f"{score}/100"),
            ui.Stat(label="Страниц", value=str(pages)),
            ui.Stat(label="Задач", value=str(len(tasks))),
            ui.Stat(label="Находок", value=str(len(findings))),
        ]),
    ]

    # Сайт не открылся — это главное, что нужно сказать, и сказать первым.
    if data.get("error"):
        children.append(ui.Alert(
            f"Сайт не открылся: {data['error']}", type="error"))

    # --- что делать -----------------------------------------------------------
    if tasks:
        task_items = []
        for t in tasks:
            label, colour = SEVERITY_LABEL.get(
                t.severity, (t.severity or "—", "gray"))
            urls = list(getattr(t, "urls", []) or [])
            bits = [label]
            if t.count:
                bits.append(f"страниц: {t.count}")
            task_items.append(ui.ListItem(
                id=f"task-{t.rule}",
                title=t.title,
                subtitle=" · ".join(bits),
                badge=ui.Badge(label=label, color=colour),
                # Адреса прячем под раскрытие: их бывает десятки, и в свёрнутом
                # виде список задач остаётся читаемым.
                expandable=bool(urls or t.body),
                expanded_content=([ui.Text(content=t.body)] if t.body else []) + (
                    [ui.Text(content="Страницы:", variant="caption")] +
                    [ui.Link(label=u, href=u) for u in urls[:20]] +
                    ([ui.Text(content=f"…и ещё {len(urls) - 20}",
                              variant="caption")] if len(urls) > 20 else [])
                    if urls else []),
            ))
        children.append(ui.Section(
            title="Что делать",
            children=[ui.List(items=task_items)],
        ))

    # --- что именно не так ----------------------------------------------------
    if findings:
        layer_blocks = []
        for _layer, name, items in br.findings_by_layer(findings):
            lines = []
            for f in items:
                label, colour = SEVERITY_LABEL.get(
                    f.get("severity"), (f.get("severity") or "—", "gray"))
                url = f.get("url") or ""
                lines.append(ui.ListItem(
                    id=f"f-{f.get('id')}",
                    title=f.get("message") or f.get("rule") or "—",
                    subtitle=(f.get("detail") or "")[:200],
                    meta=br.host_label(url) if url else "весь сайт",
                    badge=ui.Badge(label=label, color=colour),
                ))
            layer_blocks.append({
                "id": f"layer-{_layer}",
                "title": f"{name} ({len(items)})",
                "children": [ui.List(items=lines)],
            })
        children.append(ui.Section(
            title="Что не так",
            children=[ui.Accordion(sections=layer_blocks)],
        ))

    # --- стало лучше или хуже -------------------------------------------------
    history = data.get("history") or []
    if len(history) > 1:
        children.append(ui.Section(
            title="История проверок",
            children=[ui.Timeline(items=[{
                "title": f"{h['score']}/100 · {h['pages']} стр.",
                "description": (h["run_label"] or f"прогон #{h['run_id']}"),
                "time": h["when"],
            } for h in history])],
        ))

    if not tasks and not findings:
        children.append(ui.Alert(
            "Проблем не найдено — сайт в порядке.", type="success"))

    children.append(ui.Row(gap=2, children=[
        ui.Button(label="← Все сайты",
                  on_click=ui.Call("__panel__seo", view="sites")),
        ui.Button(label="Перепроверить", variant="secondary",
                  on_click=ui.Call("audit_sites", sites=host)),
        ui.Button(label="Отчёт", variant="ghost",
                  on_click=ui.Call("get_report", site=host)),
    ]))

    return ui.Stack(direction="v", gap=3, children=children)


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

    return ui.Stack(direction="v", gap=3, children=[
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
        return ui.Stack(direction="v", gap=3, children=[
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

    return ui.Stack(direction="v", gap=3, children=[
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


def _add_view(data: dict[str, Any]) -> Any:
    """Экран добавления сайта — реальная форма, а не подсказка «напишите в чат».

    Форма сабмитится прямо в `audit_sites` этого же расширения. Так можно,
    потому что `action=` панельной формы резолвится против функций РЕНДЕРЯЩЕГО
    расширения — проверено на Notion Connector, где попытка позвать чужую
    функцию падала на клике с «Function not found». `audit_sites` наш, поэтому
    кнопка действительно запускает аудит.

    Поле называется `sites` — ровно как параметр инструмента, иначе значение до
    него не дойдёт. Домены разбирает `parse_sites`, поэтому здесь можно вводить
    и один сайт, и список через запятую, со схемой или без.
    """
    known = [str(h) for h in (data.get("known") or [])]

    children: list[Any] = [
        ui.Header("Добавить сайт в аудит",
                  subtitle="Домен без схемы — например climtec.md. "
                           "Несколько — через запятую."),
        ui.Form(
            action="audit_sites",
            submit_label="Проверить",
            children=[
                ui.Input(placeholder="climtec.md, ksrenovationgroup.com",
                         param_name="sites"),
            ],
        ),
        ui.Text(content="Аудит только читает сайты и ничего на них не меняет. "
                        "Обход идёт в фоне: чат остаётся свободным, итог придёт "
                        "отдельным сообщением.",
                variant="caption"),
    ]

    # Что уже в портфеле — чтобы не гонять один сайт дважды по чужому серверу.
    if known:
        children.append(ui.Text(
            content="Уже в портфеле: " + ", ".join(known[:12]),
            variant="caption"))

    children.append(ui.Row(gap=2, children=[
        ui.Button(label="К портфелю", variant="secondary",
                  on_click=ui.Call("__panel__seo")),
    ]))
    return ui.Stack(direction="v", gap=3, children=children)


async def _load_fixes(ctx, host: str = "") -> dict[str, Any]:
    """Готовые правки — по одному сайту или по всему портфелю.

    Читает ту же базу, что и остальные экраны, но НЕ строит задачи: правки
    выводятся из находок и страниц напрямую, и лишний проход по задачам был бы
    работой ради работы.
    """
    try:
        path = await br.download_db(ctx)
    except Exception as exc:
        await ctx.log(f"panel: storage read failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось получить данные. Попробуйте обновить."}
    if not path:
        return {"first_run": True}
    try:
        store = br.open_store(path)
    except Exception as exc:
        await ctx.log(f"panel: db unreadable: {type(exc).__name__}", "error")
        return {"problem": "Данные аудита не читаются. Запустите аудит заново."}
    try:
        run_id = br.resolve_run(store, 0, site=host)
        if not run_id:
            return {"first_run": True}
        rows, _tasks = br.site_rows(store, run_id)
        if host:
            row = br.match_site(rows, host)
            if row is None:
                return {"missing": br.host_label(host) or host}
            rows = [row]
        fixes: list[dict[str, Any]] = []
        for row in rows:
            fixes.extend(br.fixes_for_site(store, row))
        return {"fixes": fixes, "summary": br.fixes_summary(fixes),
                "scope": br.host_label(rows[0]["origin"]) if len(rows) == 1
                         else "весь портфель"}
    except Exception as exc:
        await ctx.log(f"panel: fixes failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось собрать правки. Попробуйте обновить."}
    finally:
        try:
            store.close()
        except Exception:
            pass


async def _load_comparison(ctx, host: str) -> dict[str, Any]:
    """Разница между двумя последними прогонами сайта."""
    try:
        path = await br.download_db(ctx)
    except Exception as exc:
        await ctx.log(f"panel: storage read failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось получить данные. Попробуйте обновить."}
    if not path:
        return {"first_run": True}
    try:
        store = br.open_store(path)
    except Exception as exc:
        await ctx.log(f"panel: db unreadable: {type(exc).__name__}", "error")
        return {"problem": "Данные аудита не читаются. Запустите аудит заново."}
    try:
        after = br.latest_run_for_host(store, host)
        if not after:
            return {"missing": br.host_label(host) or host}
        cmp = br.compare_runs(store, host, after_run=after)
        if cmp is None:
            return {"only_one": br.host_label(host) or host}
        return {"cmp": cmp}
    except Exception as exc:
        await ctx.log(f"panel: compare failed: {type(exc).__name__}", "error")
        return {"problem": "Не удалось сравнить прогоны. Попробуйте обновить."}
    finally:
        try:
            store.close()
        except Exception:
            pass


def _side(text: str, *, empty: str = "—", width: int = 58) -> str:
    """Значение поля для таблицы правок — с длиной, если оно обрезано.

    ЗАЧЕМ ДЛИНА. Половина правок — это «сократить до рамки выдачи»: текст
    остаётся тем же, меняется только хвост. В обрезанной колонке «сейчас» и
    «станет» тогда выглядят ОДИНАКОВО, и экран будто предлагает заменить
    строку на неё же. Показанная длина возвращает смысл: видно, что 167 знаков
    стали 158, даже когда видимая часть совпадает.
    """
    t = (text or "").strip()
    if not t:
        return empty
    if len(t) <= width:
        return t
    return f"{t[:width]}… ({len(t)} зн.)"


def _short_url(url: str, *, width: int = 52) -> str:
    """Адрес для таблицы: обрезаем СЛЕВА, а не справа.

    У всех страниц одного сайта начало адреса одинаковое, поэтому обрезка с
    конца оставляла бы столбец из повторяющегося домена — то есть ничего.
    Смысл живёт в хвосте пути, его и показываем.
    """
    u = (url or "").strip()
    if len(u) <= width:
        return u
    return "…" + u[-(width - 1):]


def _fixes_view(data: dict[str, Any]) -> Any:
    """Экран правок: не «что не так», а «на что поменять».

    Готовые и требующие человека показаны ОДНОЙ таблицей с явной пометкой, а
    не двумя списками: решение «это можно применить, а это нет» — главное, что
    человек здесь читает, и прятать его в переключатель вкладок значит прятать
    суть экрана.
    """
    fixes = data.get("fixes") or []
    s = data.get("summary") or {}
    scope = data.get("scope") or ""

    if not fixes:
        return ui.Stack(direction="v", gap=3, children=[
            ui.Header("Правки", subtitle="Править нечего"),
            ui.Empty(message="По текущим находкам значения полей нельзя "
                             "вывести автоматически — либо править нечего."),
            ui.Button(label="К портфелю", on_click=ui.Call("__panel__seo")),
        ])

    table = ui.DataTable(
        columns=[
            ui.DataColumn(key="state", label="Статус"),
            ui.DataColumn(key="field", label="Поле"),
            ui.DataColumn(key="page", label="Страница"),
            ui.DataColumn(key="now", label="Сейчас"),
            ui.DataColumn(key="next", label="Станет"),
        ],
        rows=[{
            "state": "готово" if f.get("ready") else "нужен человек",
            "field": {"meta_title": "Заголовок",
                      "meta_description": "Описание",
                      "canonical_url": "Канонический адрес",
                      "robots": "robots"}.get(f.get("field"), f.get("field", "")),
            "page": _short_url(f.get("url") or ""),
            "now": _side(f.get("current") or ""),
            "next": _side(f.get("proposed") or "", empty="— нужен человек"),
        } for f in fixes[:80]],
    )

    return ui.Stack(direction="v", gap=3, children=[
        ui.Header("Готовые правки",
                  subtitle=f"{scope} · готовы к применению: {s.get('ready', 0)} "
                           f"из {s.get('total', 0)} на {s.get('pages', 0)} страницах"),
        table,
        ui.Text(content="Применить: скажите в чате «примени правки» — их внесёт "
                        "коннектор сайта, по подтверждению. Аудит сам ничего на "
                        "сайтах не меняет.",
                variant="caption"),
        ui.Row(gap=2, children=[
            ui.Button(label="К портфелю", on_click=ui.Call("__panel__seo")),
            ui.Button(label="Задачи", variant="ghost",
                      on_click=ui.Call("__panel__seo", view="tasks")),
        ]),
    ])


def _comparison_view(cmp: Any) -> Any:
    """Экран сравнения. Появившееся — первым: ради него всё и затевалось."""
    def rows_of(items, mark):
        return [{
            "change": mark,
            "severity": SEV_LABEL.get(ch.severity, ch.severity),
            "what": (ch.message or ch.rule)[:70],
            "page": (ch.url or "")[:55],
        } for ch in items[:25]]

    rows = (rows_of(cmp.appeared, "появилось")
            + rows_of(cmp.fixed, "починено")
            + rows_of(cmp.remains, "осталось"))

    blocks: list[Any] = [
        ui.Header(f"Изменения: {br.host_label(cmp.origin)}",
                  subtitle=f"прогон #{cmp.before_run} → #{cmp.after_run} · "
                           f"оценка {cmp.before_score} → {cmp.after_score}"),
    ]
    if cmp.appeared:
        blocks.append(ui.Alert(
            type="warning",
            message=f"Появилось нового: {len(cmp.appeared)}. Этого в прошлый "
                    f"раз не было — в общем списке такое незаметно."))
    if cmp.caveat:
        blocks.append(ui.Alert(type="info", message=cmp.caveat))

    blocks.append(ui.DataTable(
        columns=[
            ui.DataColumn(key="change", label="Что произошло"),
            ui.DataColumn(key="severity", label="Важность"),
            ui.DataColumn(key="what", label="Дефект"),
            ui.DataColumn(key="page", label="Страница"),
        ],
        rows=rows,
    ))
    blocks.append(ui.Row(gap=2, children=[
        ui.Button(label="К портфелю", on_click=ui.Call("__panel__seo")),
        ui.Button(label="Все сайты", variant="ghost",
                  on_click=ui.Call("__panel__seo", view="sites")),
    ]))
    return ui.Stack(direction="v", gap=3, children=blocks)


def _schedule_view(d: dict[str, Any]) -> Any:
    """Экран расписания — форма, а не инструкция «напишите в чат».

    Час и дни задаются выбором, а не текстом: строка «пн,чт» требует от
    человека угадать формат, а от панели — разбирать опечатки.
    """
    enabled = bool(d.get("enabled"))
    return ui.Stack(direction="v", gap=3, children=[
        ui.Header("Автоматический аудит",
                  subtitle=sched.describe(d)),
        ui.Text(content=("Аудит ходит по чужим серверам, поэтому по умолчанию "
                         "он выключен и запускается ночью. Утром приходит не "
                         "повторный отчёт, а РАЗНИЦА с прошлым разом."),
                variant="caption"),
        # ИМЕНА ПОЛЕЙ = ИМЕНА ПАРАМЕТРОВ `set_schedule`. Значение доходит до
        # инструмента только по `param_name`; расхождение здесь означало бы
        # форму, которая нажимается и молча ничего не меняет.
        ui.Form(
            action="set_schedule",
            submit_label="Сохранить расписание",
            children=[
                ui.Toggle(label="Включить автоматический аудит",
                          value=enabled, param_name="enabled"),
                ui.Text(content="Час запуска (UTC)", variant="caption"),
                ui.Select(
                    param_name="hour",
                    value=str(int(d.get("hour", 3))),
                    options=[{"value": str(h), "label": f"{h:02d}:00"}
                             for h in range(24)],
                ),
                ui.Text(content="Дни недели: 1=понедельник … 7=воскресенье",
                        variant="caption"),
                ui.Input(param_name="days",
                         value=str(d.get("days") or "1"),
                         placeholder="например 1 или 1,4"),
                ui.Text(content="Сайты — пусто означает «как в прошлый раз»",
                        variant="caption"),
                ui.Input(param_name="sites",
                         value=str(d.get("sites") or ""),
                         placeholder="climtec.md, example.com"),
                ui.Text(content="Страниц на сайт", variant="caption"),
                # Без type="number": сигнатура SDK его принимает, а панельный
                # контракт — нет, и деплой отклоняется. Ограничение 1..500
                # всё равно живёт в модели параметров, а не в поле ввода.
                ui.Input(param_name="max_pages",
                         value=str(int(d.get("max_pages", 50))),
                         placeholder="например 50"),
            ],
        ),
        ui.Button(label="К портфелю", on_click=ui.Call("__panel__seo")),
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

    # ЛЁГКИЕ ЭКРАНЫ ИДУТ ДО ТЯЖЁЛОГО ЧТЕНИЯ.
    #
    # `_load` готовит сводку и поднимает находки по всем сайтам: на портфеле из
    # 500 доменов это ~460 КБ и 500 построенных задач. Экрану «добавить сайт»
    # эти данные не нужны вовсе, а списку сайтов нужен свой лёгкий запрос с
    # постраничностью. Если вызвать `_load` раньше ветвления, оба экрана платят
    # за чужую работу — и тем сильнее, чем больше портфель.
    #
    # «Добавить сайт» стоит первым ещё и по второй причине: главное действие
    # обязано работать в состояниях «ничего ещё нет» и «база не читается», то
    # есть до любых проверок на пустоту и до баннера об ошибке.
    if view == "add":
        # Лёгкая справка «что уже в портфеле»: одна страница имён, без находок.
        # Нужна, чтобы человек не запускал повторно то, что уже проверяется —
        # это лишняя нагрузка на чужой сервер.
        known = await _load_sites(ctx, limit=12)
        return _add_view({
            "known": [r["host"] for r in (known.get("sites") or [])],
            "total": known.get("total") or 0,
        })

    if view == "site":
        host = str(kwargs.get("site") or kwargs.get("host") or "").strip()
        if not host:
            return _banner("Не указан сайт. Откройте список и выберите сайт.")
        site_data = await _load_site(ctx, host)
        if site_data.get("problem"):
            return _banner(site_data["problem"])
        if site_data.get("first_run"):
            return _first_run()
        if site_data.get("missing"):
            # Не сбой, а состояние: домен мог не попасть в аудит или быть
            # набран с опечаткой. Поэтому спокойный тон и путь НАЗАД В СПИСОК.
            return _banner(
                f"Сайт «{site_data['missing']}» в результатах аудита не найден — "
                f"возможно, он ещё не проверялся.",
                kind="info", back_to_sites=True)
        return _site_view(site_data["detail"])

    if view == "fixes":
        # Правки читаются СВОИМ путём: экрану не нужны ни задачи, ни сводка
        # портфеля, и общий `_load` заставил бы его платить за чужую работу.
        host = str(kwargs.get("site") or kwargs.get("host") or "").strip()
        fx = await _load_fixes(ctx, host)
        if fx.get("problem"):
            return _banner(fx["problem"])
        if fx.get("first_run"):
            return _first_run()
        if fx.get("missing"):
            return _banner(
                f"Сайта «{fx['missing']}» в результатах аудита нет.",
                kind="info", back_to_sites=True)
        return _fixes_view(fx)

    if view == "compare":
        host = str(kwargs.get("site") or kwargs.get("host") or "").strip()
        if not host:
            return _banner("Не указан сайт: сравнение всегда по одному сайту.",
                           kind="info", back_to_sites=True)
        cd = await _load_comparison(ctx, host)
        if cd.get("problem"):
            return _banner(cd["problem"])
        if cd.get("first_run"):
            return _first_run()
        if cd.get("missing"):
            return _banner(f"Сайта «{cd['missing']}» в результатах аудита нет.",
                           kind="info", back_to_sites=True)
        if cd.get("only_one"):
            # Не сбой, а честное состояние: сравнивать пока не с чем.
            return _banner(
                f"У сайта «{cd['only_one']}» пока один аудит. Запустите "
                f"проверку ещё раз после правок — покажу, что изменилось.",
                kind="info", back_to_sites=True)
        return _comparison_view(cd["cmp"])

    if view == "schedule":
        return _schedule_view(await sched.get_settings(ctx))

    if view == "sites":
        sites_data = await _load_sites(
            ctx,
            query=str(kwargs.get("q") or "").strip(),
            offset=_int_param(kwargs.get("offset")),
            limit=_int_param(kwargs.get("limit")) or br.SITES_PAGE,
        )
        if sites_data.get("problem"):
            return _banner(sites_data["problem"])
        if sites_data.get("first_run"):
            return _first_run()
        return _sites_view(sites_data)

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
    """Боковая запись: добавить сайт, состояние портфеля, вход внутрь.

    ГЛАВНОЕ ДЕЙСТВИЕ ВСЕГДА ВИДНО.

    «Добавить сайт» — то, ради чего вообще открывают это приложение, поэтому
    кнопка стоит ПЕРВОЙ и собирается ВНЕ всяких условий: до любого чтения базы,
    до любой ветки состояния. Раньше сайдбар предлагал только «Открыть портфель»
    и «Задачи» — то есть новый сайт добавить было НЕОТКУДА, а при сбое чтения
    сайдбар вырождался в строку «Результаты недоступны» вообще без действий.

    Это не косметика, а инвариант: кнопка не должна зависеть от того, читается
    ли база. Именно в состоянии «всё сломалось» пользователю нужнее всего
    возможность что-то сделать. Ветвление ниже влияет ТОЛЬКО на подпись
    состояния и на вторичные кнопки — на главное действие никогда.

    SKETCH -- left nav
      ui.Stack (v, gap=2)
        ui.Button("+ Добавить сайт", full_width=True)   # ВСЕГДА, первым
        ui.Text(content=<состояние>, variant="caption")
        ui.Button("Открыть портфель", full_width=True)  # если есть что открывать
        ui.Button("Задачи", variant="ghost", full_width=True)
    """
    # Главное действие. Собирается ДО чтения данных — сознательно.
    children = [
        ui.Button(label="+ Добавить сайт", full_width=True,
                  on_click=ui.Call("__panel__seo", view="add")),
    ]

    # Сайдбар читает ЛЁГКО. Раньше здесь вызывался `_load`, который поднимает
    # находки по всем сайтам и строит задачи — 460 КБ на портфеле из 500
    # доменов ради двух чисел в подписи, и так при каждом обновлении панели.
    # Теперь берём только счётчик и одну страницу для сводки.
    data = await _load_sites(ctx, limit=1)

    if data.get("problem"):
        state = "Результаты недоступны"
        extra: list[Any] = []
    elif data.get("first_run"):
        state = "Сайтов пока нет"
        extra = []
    else:
        total = int(data.get("total") or 0)
        state = f"Подключено сайтов: {total}"
        extra = [
            # Список сайтов — второе по важности действие после «добавить»:
            # именно его спрашивают первым, когда портфель большой.
            ui.Button(label=f"Все сайты ({total})", variant="secondary",
                      full_width=True,
                      on_click=ui.Call("__panel__seo", view="sites")),
            ui.Button(label="Сводка по портфелю", variant="secondary",
                      full_width=True, on_click=ui.Call("__panel__seo")),
            ui.Button(label="Задачи", variant="ghost", full_width=True,
                      on_click=ui.Call("__panel__seo", view="tasks")),
            ui.Button(label="Готовые правки", variant="ghost",
                      full_width=True,
                      on_click=ui.Call("__panel__seo", view="fixes")),
        ]

    # Расписание видно ВСЕГДА, как и «добавить сайт»: настроить ночной аудит
    # осмысленно и до первого прогона, и когда база не читается.
    extra.append(
        ui.Button(label="Расписание", variant="ghost", full_width=True,
                  on_click=ui.Call("__panel__seo", view="schedule")))

    children.append(ui.Text(content=state, variant="caption"))
    children.extend(extra)
    return ui.Stack(direction="v", gap=2, children=children)
