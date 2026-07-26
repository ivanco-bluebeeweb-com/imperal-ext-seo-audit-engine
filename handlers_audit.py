"""Инструменты запуска аудита.

ПОЧЕМУ ЗАПУСК — ЭТО ДВА ОТВЕТА, А НЕ ОДИН.
Аудит 20 сайтов идёт минуты, 200 — до 22 минут. Федеральный предел одного
вызова 180 с, фоновой задачи с `long_running=True` — 1800 с. Поэтому инструмент
СРАЗУ возвращает подтверждение с оценкой времени, а работу отдаёт в
`ctx.background_task(...)`: пользователь видит два сообщения — «начала» и позже
«готово, вот итог». Блокировать чат на двадцать минут было бы неприемлемо.

Важная деталь, которую легко потерять: `background=True` в декораторе
`@chat.function` — ТОЛЬКО ПОДСКАЗКА. Установленный SDK помечает поле как
advisory, ядро его не исполняет и автоматической обёртки нет. Единственный
работающий путь — вызвать `ctx.background_task` руками внутри обработчика.
Документация в одном месте называет это «sugar (v4.2.13+)» — по факту SDK
5.9.12 этого не делает, проверено.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import bridge as br
import codes as c
from app import chat, ext
from models import AuditSitesParams, ResumeAuditParams, RunStarted, RunSummary
from shared import error as _error, store_run_summary


@chat.function(
    "audit_sites",
    "Запустить SEO-аудит одного или нескольких сайтов. Обходит страницы, "
    "проверяет 18 правил в шести слоях и записывает находки. Аудит только "
    "читает — ничего на сайтах не меняет. Долгие прогоны идут в фоне: "
    "подтверждение приходит сразу, итог — отдельным сообщением.",
    action_type="write",  # пишет результаты аудита в хранилище пользователя
    background=True,      # подсказка каталогу; фон включает background_task ниже
    long_running=True,
    data_model=RunStarted,
    event="seo-audit-engine.audit_sites",
    effects=["create:audit_run"],
)
async def audit_sites(ctx, params: AuditSitesParams) -> ActionResult:
    """Запустить прогон и вернуть подтверждение немедленно."""
    origins = br.parse_sites(params.sites)
    if not origins:
        return _error(
            "Не указано ни одного сайта. Назовите домены через запятую, "
            "например: climtec.md, ksrenovationgroup.com",
            c.SEO_NO_SITES,
        )

    # Продолжаем существующий портфель, если он есть: одна база = один портфель,
    # так прогоны можно сравнивать между собой.
    db_path = await br.download_db(ctx) or br.new_db_path()

    estimate = br.estimate_minutes(len(origins), params.max_pages)
    label = params.label or ""

    async def work() -> ActionResult:
        """Сам аудит. Возвращает ActionResult — его доставит платформа."""
        try:
            run_id = await br.to_thread(
                br.run_audit_blocking,
                db_path,
                origins,
                label=label,
                max_pages=params.max_pages,
                site_workers=params.site_workers,
                page_workers=params.page_workers,
            )
        except Exception as exc:  # прогон упал целиком
            await ctx.log(f"audit run failed: {type(exc).__name__}: {exc}", "error")
            return _error(
                "Аудит не удалось завершить. Подробности записаны в журнал; "
                "прогон можно продолжить командой «продолжи аудит».",
                c.SEO_RUN_FAILED,
            )

        try:
            await br.upload_db(ctx, db_path)
        except Exception as exc:
            await ctx.log(f"audit db upload failed: {exc}", "error")
            return _error(
                "Аудит прошёл, но результат не удалось сохранить. "
                "Повторите прогон — данные не потеряются.",
                c.SEO_STORAGE_FAILED,
            )

        store = br.open_store(db_path)
        try:
            rows, tasks_by_site = br.site_rows(store, run_id)
            done = [r for r in rows if r["state"] == "done"]
            failed = [r for r in rows if r["state"] != "done"]
            findings_total = sum(len(r["findings"]) for r in rows)
            counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            for r in rows:
                for sev, n in r["by_severity"].items():
                    if sev in counts:
                        counts[sev] += n
            avg = int(sum(r["score"] for r in done) / len(done)) if done else 0
            worst_row = min(done, key=lambda r: r["score"]) if done else None
            worst = br.host_label(worst_row["origin"]) if worst_row else ""
            worst_score = int(worst_row["score"]) if worst_row else 0
            pages_checked = sum(int(r["pages"]) for r in rows)
            task_total = sum(len(t) for t in tasks_by_site.values())
            label_now = br.run_label(store, run_id)
        finally:
            store.close()

        await store_run_summary(ctx, run_id, {
            "run_id": run_id,
            "label": label_now,
            "sites": len(rows),
            "findings": findings_total,
            "tasks": task_total,
            "average_score": avg,
        })

        summary = RunSummary(
            id=str(run_id),
            title=f"Аудит #{run_id}: {len(done)} из {len(rows)} сайтов",
            kind="seo_run",
            run_id=run_id,
            label=label_now,
            sites_total=len(rows),
            sites_done=len(done),
            sites_failed=len(failed),
            pages_checked=pages_checked,
            findings_total=findings_total,
            tasks_total=task_total,
            critical=counts["critical"],
            high=counts["high"],
            worst_site=worst,
            worst_score=worst_score,
            finished=True,
        )

        parts = [
            f"Аудит #{run_id} готов: проверено сайтов {len(done)} из {len(rows)}",
            f"найдено {findings_total} проблем",
            f"задач к работе {task_total}",
        ]
        if done:
            parts.append(f"средняя оценка {avg}/100")
        if counts["critical"]:
            parts.append(f"критичных {counts['critical']}")
        if failed:
            names = ", ".join(r["origin"] for r in failed[:3])
            parts.append(f"не удалось открыть: {names}")

        return ActionResult.success(
            summary, ". ".join(parts) + ".",
            refresh_panels=["seo", "seo_nav"],
        )

    coro = work()
    try:
        await ctx.background_task(coro, long_running=True, name="seo-audit")
    except (RuntimeError, AttributeError):
        # Нет kernel-хука: локальный прогон, dev-режим или тестовый контекст.
        # Тогда выполняем ту же корутину синхронно — инструмент обязан работать
        # из-за среды хуже, но не падать. RuntimeError документирован SDK,
        # AttributeError возможен, если контекст вовсе не имеет метода.
        return await coro

    started = RunStarted(
        id="pending",
        title=f"Аудит запущен: сайтов {len(origins)}",
        kind="seo_run_started",
        sites_count=len(origins),
        sites=[br.host_label(o) for o in origins],
        max_pages=params.max_pages,
        estimate=estimate,
    )
    return ActionResult.success(
        started,
        f"Начала аудит {len(origins)} сайт(ов), до {params.max_pages} страниц "
        f"на каждом — {estimate}. Пришлю итог, когда закончу.",
    )


@chat.function(
    "resume_audit",
    "Продолжить прерванный аудит с того места, где он остановился — "
    "уже проверенные страницы не перезагружаются.",
    action_type="write",
    background=True,
    long_running=True,
    data_model=RunSummary,
    event="seo-audit-engine.resume_audit",
    effects=["update:audit_run"],
)
async def resume_audit(ctx, params: ResumeAuditParams) -> ActionResult:
    """Догнать незавершённый прогон."""
    db_path = await br.download_db(ctx)
    if not db_path:
        return _error(
            "Продолжать нечего: аудитов ещё не было. Скажите, какие сайты "
            "проверить, и я начну.",
            c.SEO_NO_RUNS,
        )

    store = br.open_store(db_path)
    try:
        run_id = br.resolve_run(store, params.run_id)
        if not run_id:
            return _error(
                "Такого прогона нет. Скажите «покажи прогоны», чтобы выбрать.",
                c.SEO_RUN_NOT_FOUND,
            )
        if br.run_finished(store, run_id):
            return _error(
                f"Прогон #{run_id} уже завершён — продолжать нечего. "
                "Запустите новый аудит, если нужны свежие данные.",
                c.SEO_RUN_ALREADY_DONE,
            )
    finally:
        store.close()

    async def work() -> ActionResult:
        try:
            await br.to_thread(br.resume_blocking, db_path, run_id)
            await br.upload_db(ctx, db_path)
        except Exception as exc:
            await ctx.log(f"resume failed: {type(exc).__name__}: {exc}", "error")
            return _error(
                "Не удалось продолжить прогон. Подробности в журнале.",
                c.SEO_RUN_FAILED,
            )

        store2 = br.open_store(db_path)
        try:
            rows, tasks_by_site = br.site_rows(store2, run_id)
            done = [r for r in rows if r["state"] == "done"]
            findings_total = sum(len(r["findings"]) for r in rows)
            done_pages = sum(int(r["pages"]) for r in rows)
            task_total = sum(len(t) for t in tasks_by_site.values())
        finally:
            store2.close()

        summary = RunSummary(
            id=str(run_id),
            title=f"Аудит #{run_id} продолжен",
            kind="seo_run",
            run_id=run_id,
            sites_total=len(rows),
            sites_done=len(done),
            sites_failed=len(rows) - len(done),
            pages_checked=done_pages,
            findings_total=findings_total,
            tasks_total=task_total,
            finished=True,
        )
        return ActionResult.success(
            summary,
            f"Прогон #{run_id} доведён до конца: сайтов {len(done)} из "
            f"{len(rows)}, проблем {findings_total}.",
            refresh_panels=["seo", "seo_nav"],
        )

    # Корутину создаём ОДИН раз: если хука нет, дожидаемся именно её. Иначе
    # `await work()` во втором вызове породил бы вторую корутину, а первая
    # осталась бы неожидаемой — Python предупредил бы, а работа потерялась.
    coro = work()
    try:
        await ctx.background_task(coro, long_running=True, name="seo-resume")
    except (RuntimeError, AttributeError):
        return await coro

    return ActionResult.success(
        RunStarted(
            id=str(run_id),
            title=f"Прогон #{run_id} продолжается",
            kind="seo_run_started",
            run_id=run_id,
            resumed=True,
        ),
        f"Продолжаю прогон #{run_id} с места остановки. Пришлю итог.",
    )
