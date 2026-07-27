"""Автоматический аудит по расписанию и управление им.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ ФАЙЛ. Здесь живёт единственное место, где приложение
действует БЕЗ человека: само ходит по чужим сайтам и само пишет в чат.
Такую способность лучше держать на виду, а не растворять среди инструментов
чтения.

ЧТО ДЕЛАЕТ НОЧНОЙ ПРОГОН. Проверяет сайты, а утром присылает не «вот 40
находок» (это второй отчёт подряд, который никто не читает), а РАЗНИЦУ с
прошлым разом: что починилось, и главное — что появилось нового. Появившееся
и есть причина, по которой ночной аудит вообще имеет смысл: сайт ломают
обновлением темы или плагина, и заметить это в общем списке невозможно.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import bridge as br
import codes as c
import schedule_settings as sched
from app import chat, ext
from models import GetScheduleParams, ScheduleParams, ScheduleState
from shared import error as _error, store_run_summary


def _fmt_changes(items: list, limit: int = 5) -> str:
    """Список изменений в строку — только самое важное, с честным хвостом."""
    lines = []
    for ch in items[:limit]:
        host = br.host_label(ch.url) if ch.url else ""
        where = f" ({ch.url})" if ch.url and host else ""
        lines.append(f"- {ch.message or ch.rule}{where}")
    if len(items) > limit:
        lines.append(f"- …и ещё {len(items) - limit}")
    return "\n".join(lines)


async def _morning_report(ctx, run_id: int, origins: list[str]) -> str:
    """Текст утреннего сообщения: разница, а не повторный отчёт."""
    db_path = await br.download_db(ctx)
    if not db_path:
        return f"Ночной аудит #{run_id} прошёл, но результат недоступен."

    store = br.open_store(db_path)
    try:
        rows, tasks_by_site = br.site_rows(store, run_id)
        done = [r for r in rows if r["state"] == "done"]
        avg = int(sum(r["score"] for r in done) / len(done)) if done else 0

        head = [f"**Ночной аудит #{run_id}** — проверено сайтов "
                f"{len(done)} из {len(rows)}, средняя оценка {avg}/100."]

        regressions: list[str] = []
        improved: list[str] = []
        for row in rows:
            cmp = br.compare_runs(store, row["origin"], after_run=run_id)
            if cmp is None:
                continue
            host = br.host_label(row["origin"])
            if cmp.appeared:
                regressions.append(
                    f"\n**{host}** — появилось нового: {len(cmp.appeared)}\n"
                    + _fmt_changes(cmp.appeared)
                )
            elif cmp.fixed:
                improved.append(f"{host} (починено {len(cmp.fixed)})")

        if regressions:
            head.append("\n### Появилось с прошлого раза")
            head.extend(regressions)
        if improved:
            head.append("\n**Стало лучше:** " + ", ".join(improved) + ".")
        if not regressions and not improved:
            head.append("\nПо сравнению с прошлым разом изменений нет.")

        task_total = sum(len(t) for t in tasks_by_site.values())
        head.append(f"\nЗадач к работе: {task_total}. "
                    f"Скажите «покажи задачи» или «покажи правки».")
        return "\n".join(head)
    finally:
        store.close()


@ext.schedule("seo_auto_audit", sched.TICK_CRON)
async def seo_auto_audit(ctx) -> None:
    """Будильник: спрашивает «уже пора?» и обычно уходит спать.

    Пропущенный тик стоит одно чтение настройки и НИ ОДНОГО обращения к
    чужим сайтам. Поэтому частый тик здесь дёшев, а редкий аудит — честно
    редкий: тик не есть прогон, он лишь будильник рядом с ним.
    """
    ok, reason = await sched.due(ctx)
    if not ok:
        return

    settings = await sched.get_settings(ctx)

    origins = br.parse_sites(str(settings.get("sites") or ""))
    if not origins:
        # Сайты не заданы — берём те, что проверяли в прошлый раз. Это то,
        # чего человек ждёт от «проверяй каждую неделю»: тот же портфель.
        db_path = await br.download_db(ctx)
        if db_path:
            store = br.open_store(db_path)
            try:
                last = br.resolve_run(store, 0)
                if last:
                    origins = [s["origin"] for s in store.sites(last)]
            finally:
                store.close()

    if not origins:
        await ctx.log("scheduled audit skipped: no sites known", "info")
        return

    # Потолок: автопрогон не должен вырасти в многочасовой обход чужих
    # серверов из-за того, что портфель разросся, а расписание никто не
    # пересматривал.
    if len(origins) > sched.MAX_SITES_PER_RUN:
        origins = origins[: sched.MAX_SITES_PER_RUN]

    # Отметка ДО прогона: см. mark_ran — упавший аудит не должен повторяться
    # на каждом тике.
    await sched.mark_ran(ctx)

    db_path = await br.download_db(ctx) or br.new_db_path()
    try:
        run_id = await br.to_thread(
            br.run_audit_blocking,
            db_path,
            origins,
            label=f"по расписанию ({reason})",
            max_pages=int(settings.get("max_pages", 50)),
        )
    except Exception as exc:
        await ctx.log(f"scheduled audit failed: {type(exc).__name__}: {exc}",
                      "error")
        await ctx.deliver_chat_message(
            "Ночной аудит не удалось завершить. Подробности в журнале; "
            "можно продолжить командой «продолжи аудит».",
            msg_type="system",
        )
        return

    try:
        await br.upload_db(ctx, db_path)
    except Exception as exc:
        await ctx.log(f"scheduled audit upload failed: {exc}", "error")
        return

    await sched.mark_ran(ctx, run_id=run_id)

    text = await _morning_report(ctx, run_id, origins)
    try:
        await ctx.deliver_chat_message(text, refresh_panels=["seo", "seo_nav"])
    except Exception as exc:
        # Доставка в чат — не часть аудита. Прогон уже сохранён, и терять
        # его из-за недоставленного сообщения нельзя.
        await ctx.log(f"morning report not delivered: {exc}", "error")


@chat.function(
    "set_schedule",
    "Настроить автоматический аудит: включить или выключить, в какой час, "
    "по каким дням недели и какие сайты проверять.",
    action_type="write",
    data_model=ScheduleState,
    event="seo-audit-engine.set_schedule",
    effects=["update:audit_schedule"],
)
async def set_schedule(ctx, params: ScheduleParams) -> ActionResult:
    """Изменить расписание. Не переданное — не трогаем."""
    if (params.enabled is None and params.hour is None
            and not params.days and not params.sites
            and params.max_pages is None):
        return _error(
            "Скажите, что именно поменять: включить, выключить, час "
            "запуска, дни недели или список сайтов.",
            c.SEO_BAD_INPUT,
        )

    d = await sched.set_settings(
        ctx,
        enabled=params.enabled,
        hour=params.hour,
        days=params.days or None,
        sites=params.sites or None,
        max_pages=params.max_pages,
        reason="по просьбе пользователя",
    )
    entity = ScheduleState(
        id="schedule",
        title=sched.describe(d),
        kind="seo_schedule",
        enabled=bool(d["enabled"]),
        hour=int(d["hour"]),
        days=str(d["days"]),
        days_label=str(d["days_label"]),
        sites=str(d["sites"]),
        max_pages=int(d["max_pages"]),
        last_run_id=int(d["last_run_id"]),
    )
    return ActionResult.success(entity, sched.describe(d))


@chat.function(
    "get_schedule",
    "Показать, как настроен автоматический аудит: включён ли, когда "
    "запускается и какие сайты проверяет.",
    action_type="read",
    data_model=ScheduleState,
)
async def get_schedule(ctx, params: GetScheduleParams) -> ActionResult:
    """Текущее расписание."""
    d = await sched.get_settings(ctx)
    entity = ScheduleState(
        id="schedule",
        title=sched.describe(d),
        kind="seo_schedule",
        enabled=bool(d["enabled"]),
        hour=int(d["hour"]),
        days=str(d["days"]),
        days_label=str(d["days_label"]),
        sites=str(d["sites"]),
        max_pages=int(d["max_pages"]),
        last_run_id=int(d["last_run_id"]),
    )
    hint = ""
    if not d["enabled"]:
        hint = (" Скажите «проверяй каждую неделю в 3 ночи», и я буду "
                "запускать аудит сама.")
    return ActionResult.success(entity, sched.describe(d) + hint)
