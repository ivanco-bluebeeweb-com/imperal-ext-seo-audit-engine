"""Инструменты чтения результатов аудита.

Все они читают ОДНУ базу портфеля и ничего не меняют ни на сайтах, ни в
трекере: `action_type="read"` у каждого. Даже `export_plan` только СТРОИТ план —
задачи создаёт коннектор трекера по подтверждению. Аудит не должен уметь писать
в чужой трекер, ровно как не должен уметь править чужой сайт.

ПУСТО — НЕ ОШИБКА. «Находок нет» после аудита значит «сайт в порядке», а не
поломку. Поэтому пустой результат — это успех с внятным текстом, а ошибка
остаётся для настоящих сбоев: аудита не было вовсе, база не читается, названный
сайт в прогоне отсутствует. Путать эти случаи нельзя — совет пользователю будет
неверный.
"""

from __future__ import annotations

from imperal_sdk import ActionResult, sdl

import bridge as br
import codes as c
from app import chat
from models import (
    AuditComparison,
    AuditTask,
    CompareParams,
    ConnectedSite,
    ExportPlan,
    ExportPlanParams,
    Finding,
    FixPlan,
    FixPlanParams,
    GetReportParams,
    ListConnectedParams,
    ListFindingsParams,
    ListRunsParams,
    ListTasksParams,
    Report,
    RunSummary,
    SiteScore,
)
from shared import error as _error, open_portfolio


@chat.function(
    "list_runs",
    "Показать прошлые аудиты: когда, по каким сайтам, сколько задач нашлось.",
    action_type="read",
    data_model=RunSummary,
)
async def list_runs(ctx, params: ListRunsParams) -> ActionResult:
    """Список прогонов — свежие сверху."""
    store, _run, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        rows = store.db.execute(
            "SELECT id, label, started_at, finished_at FROM runs "
            "ORDER BY id DESC LIMIT ?",
            (params.limit,),
        ).fetchall()

        items: list[RunSummary] = []
        for r in rows:
            site_rows, tasks_by_site = br.site_rows(store, int(r["id"]))
            done = [x for x in site_rows if x["state"] == "done"]
            failed = [x for x in site_rows if x["state"] != "done"]
            tasks_total = sum(len(t) for t in tasks_by_site.values())
            findings_total = sum(len(x["findings"]) for x in site_rows)
            crit = sum(x["by_severity"].get("critical", 0) for x in site_rows)
            high = sum(x["by_severity"].get("high", 0) for x in site_rows)
            worst = min(done, key=lambda x: x["score"], default=None)

            items.append(RunSummary(
                id=str(r["id"]),
                title=(r["label"] or f"Аудит #{r['id']}"),
                subtitle=f"{len(done)} сайтов · {tasks_total} задач",
                kind="seo_run",
                run_id=int(r["id"]),
                label=r["label"] or "",
                sites_total=len(site_rows),
                sites_done=len(done),
                sites_failed=len(failed),
                pages_checked=sum(x["pages"] for x in site_rows),
                findings_total=findings_total,
                tasks_total=tasks_total,
                critical=crit,
                high=high,
                worst_site=br.host_label(worst["origin"]) if worst else "",
                worst_score=worst["score"] if worst else 0,
                finished=bool(r["finished_at"]),
            ))

        if not items:
            return ActionResult.success(
                sdl.EntityList(items=[]),
                "Аудитов пока не было. Скажите, какие сайты проверить.",
            )
        return ActionResult.success(
            sdl.EntityList(items=items),
            f"Прогонов: {len(items)}. Свежий — {items[0].title}.",
        )
    finally:
        store.close()


@chat.function(
    "list_connected_sites",
    "Показать все сайты, подключённые к аудиту — по всем прогонам, с датой "
    "последней проверки. Есть поиск по части домена и постраничный вывод: "
    "портфель бывает на сотни сайтов.",
    action_type="read",
    data_model=ConnectedSite,
)
async def list_connected_sites(ctx, params: ListConnectedParams) -> ActionResult:
    """Список подключённых сайтов — «что у меня вообще есть».

    Отличается от `list_sites` намеренно. Тот показывает сайты ОДНОГО прогона с
    оценками — это отчёт. Здесь вопрос другой: какие сайты подключены вообще, по
    всем прогонам. Домен, проверенный трижды, здесь одна строка с датой
    последней проверки, а не три.

    Постраничность не украшение: на портфеле из 500 доменов полный список
    невозможно прочитать в чате, а ответ раздувается. Отдаём страницу и всегда
    говорим, сколько всего и как посмотреть дальше.
    """
    store, _run, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        rows, total = br.connected_sites(
            store,
            query=params.query,
            offset=params.offset,
            limit=params.limit,
        )
    except Exception as exc:
        await ctx.log(f"list_connected_sites failed: {type(exc).__name__}", "error")
        return _error(
            "Не удалось прочитать список сайтов. Попробуйте ещё раз.",
            c.SEO_DB_UNREADABLE,
        )
    finally:
        store.close()

    if not total:
        # Пусто — не ошибка, а состояние. Но при поиске и при пустом портфеле
        # человеку нужен РАЗНЫЙ следующий шаг, поэтому тексты разные.
        if params.query:
            return ActionResult.success(
                sdl.EntityList(items=[]),
                f"По запросу «{params.query}» подключённых сайтов не нашлось. "
                f"Попробуйте часть домена покороче.",
            )
        return _error(
            "Подключённых сайтов пока нет. Скажите, например, «проверь "
            "climtec.md» — и сайт появится в списке.",
            c.SEO_NO_SITES,
        )

    items = [
        ConnectedSite(
            id=row["host"],
            title=row["host"],
            subtitle=br.state_label(row["state"]) + (
                f" · {row['pages']} стр." if row["pages"] else ""),
            kind="seo_connected_site",
            origin=row["origin"],
            host=row["host"],
            state=row["state"],
            state_label=br.state_label(row["state"]),
            pages=row["pages"],
            runs=row["runs"],
            last_checked=br.when_label(row["last_seen"]),
            failure=row["error"],
        )
        for row in rows
    ]

    shown_to = params.offset + len(items)
    parts = [f"Подключённых сайтов: {total}"]
    if total > len(items):
        parts.append(f"показаны {params.offset + 1}–{shown_to}")
    if params.query:
        parts.append(f"поиск: «{params.query}»")

    broken = [r for r in rows if r["state"] == "error"]
    if broken:
        parts.append(f"не открылись: {len(broken)}")

    summary = ". ".join(parts) + "."
    if shown_to < total:
        summary += f" Дальше — offset={shown_to}."

    return ActionResult.success(sdl.EntityList(items=items), summary)


@chat.function(
    "list_sites",
    "Показать сайты последнего аудита с оценкой здоровья и главной проблемой "
    "каждого — сводка по портфелю.",
    action_type="read",
    data_model=SiteScore,
)
async def list_sites(ctx, params: ListFindingsParams) -> ActionResult:
    """Портфель: по строке на сайт, слабые сверху."""
    store, run_id, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        run_id = br.resolve_run(store, params.run_id,
                                site=getattr(params, "site", "") or "")
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден. Посмотрите список аудитов.",
                c.SEO_RUN_NOT_FOUND,
            )

        rows, _tasks = br.site_rows(store, run_id,
                                    min_severity=params.min_severity)
        rows.sort(key=lambda r: (r["state"] == "done", r["score"]))

        items = [
            SiteScore(
                id=br.host_label(r["origin"]),
                title=br.host_label(r["origin"]),
                subtitle=(f"{r['score']}/100 · {r['pages']} стр. · "
                          f"{r['tasks']} задач"),
                kind="seo_site",
                origin=r["origin"],
                score=r["score"],
                pages=r["pages"],
                tasks=r["tasks"],
                top_issue=r["top_issue"],
                state=r["state"],
                failure=r["error"],
            )
            for r in rows
        ]
        if not items:
            return ActionResult.success(
                sdl.EntityList(items=[]),
                "В этом прогоне нет сайтов.",
            )

        done = [r for r in rows if r["state"] == "done"]
        failed = [r for r in rows if r["state"] != "done"]
        avg = round(sum(r["score"] for r in done) / len(done)) if done else 0
        summary = f"Сайтов: {len(done)}, средняя оценка {avg}/100"
        if failed:
            # Упавшие сайты называем прямо: молчание о них выглядит как «всё
            # хорошо», хотя часть портфеля вообще не проверена.
            summary += f". Не удалось проверить: {len(failed)}"
        return ActionResult.success(sdl.EntityList(items=items), summary)
    finally:
        store.close()


@chat.function(
    "list_findings",
    "Показать найденные SEO-проблемы: что не так, на какой странице и "
    "насколько это важно. Можно по одному сайту или по всему портфелю.",
    action_type="read",
    data_model=Finding,
)
async def list_findings(ctx, params: ListFindingsParams) -> ActionResult:
    """Находки, отсортированные по слою и важности."""
    store, run_id, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        run_id = br.resolve_run(store, params.run_id,
                                site=br.run_hint(params.site, getattr(params, "page_url", "") or ""))
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден.", c.SEO_RUN_NOT_FOUND)

        rows, _tasks = br.site_rows(store, run_id,
                                    min_severity=params.min_severity)
        page_url = (params.page_url or "").strip()

        if page_url:
            row, mismatch = br.resolve_page_site(rows, params.site, page_url)
            if mismatch:
                return _error(
                    f"«{params.site}» и хост в page_url не совпадают — "
                    f"уточните один из двух.",
                    c.SEO_PAGE_SITE_MISMATCH,
                )
            if row is None:
                known = ", ".join(br.host_label(r["origin"]) for r in rows[:8])
                return _error(
                    f"Сайт для страницы «{page_url}» в этом прогоне не "
                    f"найден. Проверялись: {known or '—'}.",
                    c.SEO_SITE_NOT_FOUND,
                )
            page = br.find_page(store, row["id"], page_url)
            if page is None:
                known = ", ".join(br.known_page_urls(store, row["id"]))
                return _error(
                    f"Страницы «{page_url}» нет среди проверенных на "
                    f"{br.host_label(row['origin'])}. Известные адреса: "
                    f"{known or '—'}.",
                    c.SEO_PAGE_NOT_FOUND,
                )
            rows = [row]
        elif params.site:
            row = br.match_site(rows, params.site)
            if row is None:
                known = ", ".join(br.host_label(r["origin"]) for r in rows[:8])
                return _error(
                    f"Сайта «{params.site}» в этом прогоне нет. "
                    f"Проверялись: {known or '—'}.",
                    c.SEO_SITE_NOT_FOUND,
                )
            rows = [row]

        limit_order = br.severity_rank(params.min_severity)
        picked: list[Finding] = []
        for r in rows:
            findings = r["findings"]
            matched_via: dict[int, str] = {}
            if page_url:
                filtered = br.filter_findings_by_page(findings, page_url)
                findings = filtered
                matched_via = {id(f): f.get("matched_via", "") for f in filtered}
            for f in findings:
                if br.severity_rank(f["severity"]) > limit_order:
                    continue
                picked.append(Finding(
                    id=f"{br.host_label(r['origin'])}:{f['rule']}:{f['id']}"
                       if "id" in f.keys() else f"{f['rule']}",
                    title=f["message"],
                    subtitle=f"{f['severity']} · {br.layer_name(f['layer'])}",
                    kind="seo_finding",
                    rule=f["rule"],
                    severity=f["severity"],
                    layer=f["layer"],
                    layer_name=br.layer_name(f["layer"]),
                    site=br.host_label(r["origin"]),
                    url=f["url"] or r["origin"],
                    message=f["message"],
                    detail=f["detail"] or "",
                    matched_via=matched_via.get(id(f), ""),
                ))

        # Порядок слоёв = порядок работ: сначала то, из-за чего страница вообще
        # выпадает из выдачи, и только потом косметика.
        picked.sort(key=lambda x: (x.layer, br.severity_rank(x.severity)))
        shown = picked[: params.limit]

        if not picked:
            where = f" на {page_url}" if page_url else (
                f" на {params.site}" if params.site else "")
            return ActionResult.success(
                sdl.EntityList(items=[]),
                f"Проблем уровня «{params.min_severity}» и выше{where} нет.",
            )
        tail = (f" Показаны первые {len(shown)} из {len(picked)}."
                if len(picked) > len(shown) else "")
        return ActionResult.success(
            sdl.EntityList(items=shown, total=len(picked)),
            f"Находок: {len(picked)}.{tail}",
        )
    finally:
        store.close()


@chat.function(
    "list_tasks",
    "Показать задачи по итогам аудита: одна задача = один дефект на одном "
    "сайте со списком затронутых страниц внутри. Это то, что можно отдать "
    "в работу или выгрузить в трекер.",
    action_type="read",
    data_model=AuditTask,
)
async def list_tasks(ctx, params: ListTasksParams) -> ActionResult:
    """Задачи — сгруппированные находки, готовые к работе."""
    store, run_id, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        run_id = br.resolve_run(store, params.run_id,
                                site=br.run_hint(params.site, getattr(params, "page_url", "") or ""))
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден.", c.SEO_RUN_NOT_FOUND)

        rows, tasks_by_site = br.site_rows(store, run_id,
                                           min_severity=params.min_severity)
        page_url = (params.page_url or "").strip()

        if page_url:
            row, mismatch = br.resolve_page_site(rows, params.site, page_url)
            if mismatch:
                return _error(
                    f"«{params.site}» и хост в page_url не совпадают — "
                    f"уточните один из двух.",
                    c.SEO_PAGE_SITE_MISMATCH,
                )
            if row is None:
                known = ", ".join(br.host_label(r["origin"]) for r in rows[:8])
                return _error(
                    f"Сайт для страницы «{page_url}» в этом прогоне не "
                    f"найден. Проверялись: {known or '—'}.",
                    c.SEO_SITE_NOT_FOUND,
                )
            page = br.find_page(store, row["id"], page_url)
            if page is None:
                known = ", ".join(br.known_page_urls(store, row["id"]))
                return _error(
                    f"Страницы «{page_url}» нет среди проверенных на "
                    f"{br.host_label(row['origin'])}. Известные адреса: "
                    f"{known or '—'}.",
                    c.SEO_PAGE_NOT_FOUND,
                )
            tasks_by_site = {row["origin"]: tasks_by_site.get(row["origin"], [])}
        elif params.site:
            row = br.match_site(rows, params.site)
            if row is None:
                return _error(
                    f"Сайта «{params.site}» в этом прогоне нет.",
                    c.SEO_SITE_NOT_FOUND,
                )
            tasks_by_site = {row["origin"]: tasks_by_site.get(row["origin"], [])}

        items: list[AuditTask] = []
        for origin, tasks in tasks_by_site.items():
            matched_page_by: dict[str, str] = {}
            if page_url:
                pairs = br.filter_tasks_by_page(tasks, page_url)
                tasks = [t for t, _u in pairs]
                matched_page_by = {t.fingerprint: u for t, u in pairs}
            for t in tasks:
                items.append(AuditTask(
                    id=t.fingerprint,
                    title=t.title,
                    subtitle=(f"{t.severity} · {t.count} стр. · "
                              f"срок {t.due_days} дн."),
                    kind="seo_task",
                    site=br.host_label(origin),
                    rule=t.rule,
                    task_title=t.title,
                    body=t.body,
                    severity=t.severity,
                    layer_name=br.layer_name(t.layer),
                    pages=t.count,
                    urls=list(t.urls[:20]),
                    due_days=t.due_days,
                    tags=list(t.tags),
                    autofixable=t.autofixable,
                    fingerprint=t.fingerprint,
                    matched_page=matched_page_by.get(t.fingerprint, ""),
                ))

        items.sort(key=lambda x: (br.severity_rank(x.severity), x.site))
        shown = items[: params.limit]

        if not items:
            where = f" на странице {page_url}" if page_url else ""
            return ActionResult.success(
                sdl.EntityList(items=[]),
                f"Задач{where} нет — по выбранному порогу важности всё в "
                f"порядке.",
            )
        auto = sum(1 for x in items if x.autofixable)
        note = f" Из них {auto} правится автоматически." if auto else ""
        return ActionResult.success(
            sdl.EntityList(items=shown, total=len(items)),
            f"Задач: {len(items)}.{note}",
        )
    finally:
        store.close()


@chat.function(
    "get_report",
    "Собрать отчёт по аудиту: сводный по портфелю или подробный по одному "
    "сайту. Возвращает готовый текст в Markdown.",
    action_type="read",
    data_model=Report,
)
async def get_report(ctx, params: GetReportParams) -> ActionResult:
    """Отчёт движка — тот же, что пишет CLI в файл."""
    store, run_id, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        run_id = br.resolve_run(store, params.run_id,
                                site=br.run_hint(params.site, getattr(params, "page_url", "") or ""))
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден.", c.SEO_RUN_NOT_FOUND)

        rows, tasks_by_site = br.site_rows(store, run_id,
                                           min_severity=params.min_severity)
        if not rows:
            return _error("В этом прогоне нет сайтов.", c.SEO_NOTHING_TO_SHOW)

        page_url = (params.page_url or "").strip()

        if page_url:
            row, mismatch = br.resolve_page_site(rows, params.site, page_url)
            if mismatch:
                return _error(
                    f"«{params.site}» и хост в page_url не совпадают — "
                    f"уточните один из двух.",
                    c.SEO_PAGE_SITE_MISMATCH,
                )
            if row is None:
                known = ", ".join(br.host_label(r["origin"]) for r in rows[:8])
                return _error(
                    f"Сайт для страницы «{page_url}» в этом прогоне не "
                    f"найден. Проверялись: {known or '—'}.",
                    c.SEO_SITE_NOT_FOUND,
                )
            page = br.find_page(store, row["id"], page_url)
            if page is None:
                known = ", ".join(br.known_page_urls(store, row["id"]))
                return _error(
                    f"Страницы «{page_url}» нет среди проверенных на "
                    f"{br.host_label(row['origin'])}. Известные адреса: "
                    f"{known or '—'}.",
                    c.SEO_PAGE_NOT_FOUND,
                )
            findings = br.filter_findings_by_page(row["findings"], page_url)
            tasks = [t for t, _u in br.filter_tasks_by_page(
                tasks_by_site.get(row["origin"], []), page_url)]
            md = br.page_markdown(store, row, page, findings, tasks)
            crit_high = any(f["severity"] in (br.CRITICAL, br.HIGH)
                            for f in findings)
            entity = Report(
                id=f"{br.host_label(row['origin'])}:{page['url']}",
                title=f"Отчёт по странице: {page['url']}",
                kind="seo_report",
                scope="page",
                markdown=md,
                sites_count=1,
                tasks_total=len(tasks),
                page_url=page["url"],
                findings_total=len(findings),
                has_critical_or_high=crit_high,
            )
            return ActionResult.success(
                entity,
                f"Отчёт по странице {page['url']}: находок {len(findings)}, "
                f"задач {len(tasks)}.",
            )

        if params.site:
            row = br.match_site(rows, params.site)
            if row is None:
                return _error(
                    f"Сайта «{params.site}» в этом прогоне нет.",
                    c.SEO_SITE_NOT_FOUND,
                )
            md = br.site_markdown(store, row, tasks_by_site.get(row["origin"], []))
            entity = Report(
                id=br.host_label(row["origin"]),
                title=f"Отчёт: {br.host_label(row['origin'])}",
                kind="seo_report",
                scope="site",
                markdown=md,
                sites_count=1,
                tasks_total=row["tasks"],
            )
            return ActionResult.success(
                entity,
                f"Отчёт по {br.host_label(row['origin'])}: "
                f"{row['score']}/100, задач {row['tasks']}.",
            )

        label = br.run_label(store, run_id)
        md = br.portfolio_markdown(rows, label)
        total_tasks = sum(len(t) for t in tasks_by_site.values())
        entity = Report(
            id=f"run-{run_id}",
            title=f"Отчёт по портфелю{': ' + label if label else ''}",
            kind="seo_report",
            scope="portfolio",
            markdown=md,
            sites_count=len(rows),
            tasks_total=total_tasks,
        )
        return ActionResult.success(
            entity,
            f"Сводный отчёт: сайтов {len(rows)}, задач {total_tasks}.",
        )
    finally:
        store.close()


@chat.function(
    "export_plan",
    "Построить план выгрузки задач аудита в трекер: названия, описания, "
    "разделы по сроку, теги и метки для повторного аудита. Сам план ничего "
    "не создаёт — задачи заводит коннектор трекера по подтверждению.",
    action_type="read",
    data_model=ExportPlan,
)
async def export_plan(ctx, params: ExportPlanParams) -> ActionResult:
    """Готовые аргументы задач для Asana/Notion — без создания."""
    store, run_id, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        run_id = br.resolve_run(store, params.run_id,
                                site=getattr(params, "site", "") or "")
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден.", c.SEO_RUN_NOT_FOUND)

        rows, tasks_by_site = br.site_rows(store, run_id,
                                           min_severity=params.min_severity)
        if params.site:
            row = br.match_site(rows, params.site)
            if row is None:
                return _error(
                    f"Сайта «{params.site}» в этом прогоне нет.",
                    c.SEO_SITE_NOT_FOUND,
                )
            tasks_by_site = {row["origin"]: tasks_by_site.get(row["origin"], [])}

        plan = br.plan_entries(tasks_by_site, project=params.project,
                               assignee=params.assignee)
        if not plan:
            return ActionResult.success(
                ExportPlan(id=f"run-{run_id}", title="План пуст",
                           kind="seo_plan", entries=[]),
                "Выгружать нечего — задач по выбранному порогу нет.",
            )

        s = br.plan_summary(plan)
        entity = ExportPlan(
            id=f"run-{run_id}",
            title=f"План выгрузки: {s['tasks']} задач",
            subtitle=f"{s['sites']} сайтов · {s['pages_touched']} страниц",
            kind="seo_plan",
            tasks_total=s["tasks"],
            sites_count=s["sites"],
            autofixable=s["autofixable"],
            pages_touched=s["pages_touched"],
            by_section=s["by_section"],
            entries=plan,
        )
        sections = ", ".join(f"{k}: {v}" for k, v in s["by_section"].items())
        return ActionResult.success(
            entity,
            f"План готов: {s['tasks']} задач по {s['sites']} сайтам. "
            f"Разделы — {sections}. Скажите, куда выгрузить: Asana или Notion.",
        )
    finally:
        store.close()


@chat.function(
    "fix_plan",
    "Показать готовые правки по итогам аудита: для каждой страницы — какое "
    "поле и на какое значение поменять. Сам план ничего не меняет: правки "
    "применяет коннектор сайта по подтверждению.",
    action_type="read",
    data_model=FixPlan,
)
async def fix_plan(ctx, params: FixPlanParams) -> ActionResult:
    """Находки -> конкретные значения полей.

    Между «нет описания на восьми страницах» и починкой лежит работа:
    решить, ЧТО написать. Здесь она сделана заранее и по данным самой
    страницы, а где честно вывести значение нельзя — правка помечена как
    требующая человека, а не заполнена правдоподобным мусором.
    """
    store, _run, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        run_id = br.resolve_run(store, params.run_id, site=params.site or "")
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден.", c.SEO_RUN_NOT_FOUND)

        rows, _tasks = br.site_rows(store, run_id)
        if params.site:
            row = br.match_site(rows, params.site)
            if row is None:
                return _error(
                    f"Сайта «{params.site}» в этом прогоне нет.",
                    c.SEO_SITE_NOT_FOUND,
                )
            rows = [row]

        fixes: list[dict] = []
        for row in rows:
            fixes.extend(
                br.fixes_for_site(store, row, only_ready=params.only_ready))

        s = br.fixes_summary(fixes)
        shown = fixes[: params.limit]

        if not fixes:
            return ActionResult.success(
                FixPlan(id=f"run-{run_id}", title="Править нечего",
                        kind="seo_fix_plan"),
                "Готовых правок нет — по этим находкам значения полей "
                "нельзя вывести автоматически.",
            )

        scope = br.host_label(rows[0]["origin"]) if len(rows) == 1 else "портфель"
        entity = FixPlan(
            id=f"run-{run_id}",
            title=f"Правки: {s['ready']} готовы к применению",
            subtitle=f"{scope} · {s['pages']} страниц · {s['total']} правок",
            kind="seo_fix_plan",
            total=s["total"],
            ready=s["ready"],
            needs_review=s["needs_review"],
            pages=s["pages"],
            by_field=s["by_field"],
            fixes=shown,
        )
        fields = ", ".join(f"{k}: {v}" for k, v in s["by_field"].items())
        tail = ""
        if s["needs_review"]:
            tail = (f" Ещё {s['needs_review']} требуют вашего решения — "
                    f"там значение нельзя вывести честно.")
        return ActionResult.success(
            entity,
            f"{s['ready']} правок готовы к применению на {s['pages']} "
            f"страницах ({fields}).{tail} Скажите «применяй» — внесу их "
            f"через коннектор сайта.",
        )
    finally:
        store.close()


@chat.function(
    "compare_audits",
    "Сравнить два аудита одного сайта: что починилось, что осталось и что "
    "ПОЯВИЛОСЬ нового. Появившееся — регрессия: в общем списке её не видно.",
    action_type="read",
    data_model=AuditComparison,
)
async def compare_audits(ctx, params: CompareParams) -> ActionResult:
    """Изменения между двумя прогонами.

    Отдельный инструмент, а не флаг отчёта: отчёт отвечает «что не так»,
    сравнение — «стало ли лучше». Второй вопрос человек задаёт ПОСЛЕ работы,
    и ответ на него другой по структуре.
    """
    store, _run, err = await open_portfolio(ctx)
    if err:
        return err
    try:
        after_run = params.after_run
        if not after_run:
            found = br.latest_run_for_host(store, params.site)
            if not found:
                return _error(
                    f"Сайт «{params.site}» ни в одном аудите не встречался.",
                    c.SEO_SITE_NOT_FOUND,
                )
            after_run = found

        cmp = br.compare_runs(store, params.site, after_run=after_run,
                              before_run=params.before_run)
        if cmp is None:
            return ActionResult.success(
                AuditComparison(
                    id=f"cmp-{params.site}", kind="seo_comparison",
                    title="Сравнивать не с чем", site=params.site,
                    after_run=after_run,
                ),
                f"У сайта «{params.site}» только один аудит — сравнивать не с "
                f"чем. Запустите проверку ещё раз после правок, и я покажу, "
                f"что изменилось.",
            )

        d = cmp.to_dict()
        head = br.summarise_comparison(cmp)
        entity = AuditComparison(
            id=f"cmp-{cmp.after_run}-{cmp.before_run}",
            kind="seo_comparison",
            title=head,
            subtitle=f"{br.host_label(cmp.origin)} · прогон "
                     f"#{cmp.before_run} → #{cmp.after_run}",
            site=br.host_label(cmp.origin),
            before_run=cmp.before_run,
            after_run=cmp.after_run,
            before_score=cmp.before_score,
            after_score=cmp.after_score,
            score_delta=cmp.score_delta,
            fixed_count=len(cmp.fixed),
            remains_count=len(cmp.remains),
            appeared_count=len(cmp.appeared),
            reliable=cmp.reliable,
            caveat=cmp.caveat,
            fixed=d["fixed"][:50],
            appeared=d["appeared"][:50],
            remains=d["remains"][:50],
        )

        message = head
        if cmp.appeared:
            worst = cmp.appeared[0]
            message += (f" Появилось новое, чего раньше не было — например: "
                        f"{worst.message or worst.rule}.")
        if cmp.caveat:
            message += f" {cmp.caveat}"
        return ActionResult.success(entity, message)
    finally:
        store.close()
