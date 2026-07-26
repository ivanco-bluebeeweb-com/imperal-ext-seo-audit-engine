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
    AuditTask,
    ExportPlan,
    ExportPlanParams,
    Finding,
    GetReportParams,
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
        run_id = br.resolve_run(store, params.run_id)
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
        run_id = br.resolve_run(store, params.run_id)
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден.", c.SEO_RUN_NOT_FOUND)

        rows, _tasks = br.site_rows(store, run_id,
                                    min_severity=params.min_severity)
        if params.site:
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
            for f in r["findings"]:
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
                ))

        # Порядок слоёв = порядок работ: сначала то, из-за чего страница вообще
        # выпадает из выдачи, и только потом косметика.
        picked.sort(key=lambda x: (x.layer, br.severity_rank(x.severity)))
        shown = picked[: params.limit]

        if not picked:
            where = f" на {params.site}" if params.site else ""
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
        run_id = br.resolve_run(store, params.run_id)
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

        items: list[AuditTask] = []
        for origin, tasks in tasks_by_site.items():
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
                ))

        items.sort(key=lambda x: (br.severity_rank(x.severity), x.site))
        shown = items[: params.limit]

        if not items:
            return ActionResult.success(
                sdl.EntityList(items=[]),
                "Задач нет — по выбранному порогу важности всё в порядке.",
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
        run_id = br.resolve_run(store, params.run_id)
        if not run_id:
            return _error(
                f"Прогон #{params.run_id} не найден.", c.SEO_RUN_NOT_FOUND)

        rows, tasks_by_site = br.site_rows(store, run_id,
                                           min_severity=params.min_severity)
        if not rows:
            return _error("В этом прогоне нет сайтов.", c.SEO_NOTHING_TO_SHOW)

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
        run_id = br.resolve_run(store, params.run_id)
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
