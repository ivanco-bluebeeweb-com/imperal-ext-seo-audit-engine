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

import re

from imperal_sdk import ActionResult

import bridge as br
import codes as c
import schedule_settings as sched
from app import chat, ext
from models import GetScheduleParams, ScheduleParams, ScheduleState
from shared import error as _error, store_run_summary


def _bare_domain(raw: str) -> str:
    """Strip scheme/path/www from a URL or bare domain, lowercase it --

    same normalization Sites Registry itself uses, so a domain registered
    there and one already known here always compare equal."""
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/", 1)[0]
    if d.startswith("www."):
        d = d[4:]
    return d


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


@ext.expose("register_known_site", action_type="write")
async def expose_register_known_site(ctx, site_id: str = "", domain: str = "",
                                       name: str = "", **kwargs) -> dict:
    """Inter-extension IPC surface (ctx.extensions.call) for Sites Registry:
    called automatically whenever a site is registered there (manually,
    via WordPress Hub's connect, or via a registry sync/backfill), so it
    is already known here as a site to audit.

    Deliberately does NOT run an audit and does NOT enable the schedule --
    this app only ever touches someone else's site on an explicit ask, and
    a site landing in the registry is not that ask. It only appends the
    domain to the schedule's own site list (if not already present), so it
    shows up as a known/remembered site and gets picked up the next time an
    audit runs or the user turns the schedule on. Idempotent: adding an
    already-known domain changes nothing. Returns a plain dict (never
    surfaced to the LLM/user directly).
    """
    sid = _bare_domain(site_id or domain)
    if not sid:
        return {"ok": False, "error": "site_id or domain is required.", "retryable": False}
    settings = await sched.get_settings(ctx)
    known = br.parse_sites(str(settings.get("sites") or ""))
    known_bare = {_bare_domain(o) for o in known}
    if sid in known_bare:
        return {"ok": True, "site_id": sid, "created": False}
    updated = known + [f"https://{sid}"]
    await sched.set_settings(
        ctx, sites=", ".join(updated),
        reason="auto-registered from Sites Registry",
    )
    return {"ok": True, "site_id": sid, "created": True}


def _make_user_ctx(ctx, user_id: str):
    try:
        return ctx.as_user(user_id)
    except Exception:
        from copy import copy
        from imperal_sdk.types.identity import UserContext
        u_ctx = copy(ctx)
        u_ctx.user = UserContext(
            imperal_id=user_id,
            email=f"{user_id}@imperal.io",
            tenant_id=getattr(getattr(ctx, "user", None), "tenant_id", "default") or "default",
            role="user",
        )
        return u_ctx


async def _run_audit_for_user(user_ctx) -> None:
    ok, reason = await sched.due(user_ctx)
    if not ok:
        return

    settings = await sched.get_settings(user_ctx)

    origins = br.parse_sites(str(settings.get("sites") or ""))
    if not origins:
        db_path = await br.download_db(user_ctx)
        if db_path:
            store = br.open_store(db_path)
            try:
                last = br.resolve_run(store, 0)
                if last:
                    origins = [s["origin"] for s in store.sites(last)]
            finally:
                store.close()

    if not origins:
        await user_ctx.log("scheduled audit skipped: no sites known", "info")
        return

    if len(origins) > sched.MAX_SITES_PER_RUN:
        origins = origins[: sched.MAX_SITES_PER_RUN]

    await sched.mark_ran(user_ctx)

    db_path = await br.download_db(user_ctx) or br.new_db_path()
    base_max_run_id = br.max_run_id(db_path)
    try:
        run_id = await br.to_thread(
            br.run_audit_blocking,
            db_path,
            origins,
            label=f"по расписанию ({reason})",
            max_pages=int(settings.get("max_pages", 50)),
        )
    except Exception as exc:
        await user_ctx.log(f"scheduled audit failed: {type(exc).__name__}: {exc}", "error")
        await user_ctx.deliver_chat_message(
            "Ночной аудит не удалось завершить. Подробности в журнале; "
            "можно продолжить командой «продолжи аудит».",
            msg_type="system",
        )
        return

    try:
        run_id = await br.upload_run_safely(user_ctx, db_path, run_id, base_max_run_id)
    except Exception as exc:
        await user_ctx.log(f"scheduled audit upload failed: {exc}", "error")
        return

    await sched.mark_ran(user_ctx, run_id=run_id)

    text = await _morning_report(user_ctx, run_id, origins)
    try:
        await user_ctx.deliver_chat_message(text, refresh_panels=["seo", "seo_nav"])
    except Exception as exc:
        await user_ctx.log(f"morning report not delivered: {exc}", "error")


@ext.schedule("seo_auto_audit", sched.TICK_CRON)
async def seo_auto_audit(ctx) -> None:
    """Будильник: опрашивает пользователей с настроенным расписанием.

    В системном контексте выполняет multi-user fan-out через list_users(SETTINGS_COLLECTION).
    Для каждого пользователя исполняет аудит строго в его собственном изолированном контексте.
    """
    uid = getattr(getattr(ctx, "user", None), "imperal_id", "") or ""
    if uid and uid != "__system__":
        await _run_audit_for_user(ctx)
        return

    if hasattr(ctx, "store") and hasattr(ctx.store, "list_users"):
        try:
            async for user_id in ctx.store.list_users(sched.SETTINGS_COLLECTION):
                if not user_id or user_id == "__system__":
                    continue
                user_ctx = _make_user_ctx(ctx, user_id)
                try:
                    await _run_audit_for_user(user_ctx)
                except Exception as exc:
                    await ctx.log(f"seo_auto_audit failed for user {user_id}: {exc}", "error")
            return
        except Exception as exc:
            await ctx.log(f"list_users iteration failed in seo_auto_audit: {exc}", "warning")
    else:
        # Fallback for environments / test doubles where list_users is not implemented
        try:
            page = await ctx.store.query(sched.SETTINGS_COLLECTION, limit=500)
            user_ids = set()
            for doc in (page.data if page else []):
                data = getattr(doc, "data", {}) or {}
                u_id = str(data.get("user_id") or "").strip()
                if not u_id:
                    k = str(data.get("key") or "")
                    if k.startswith(f"{sched.SETTINGS_KEY}:"):
                        u_id = k.split(f"{sched.SETTINGS_KEY}:", 1)[1].strip()
                if u_id and u_id != "__system__":
                    user_ids.add(u_id)
            if user_ids:
                for u_id in sorted(user_ids):
                    user_ctx = _make_user_ctx(ctx, u_id)
                    try:
                        await _run_audit_for_user(user_ctx)
                    except Exception as exc:
                        await ctx.log(f"seo_auto_audit failed for user {u_id}: {exc}", "error")
                return
        except Exception as exc:
            await ctx.log(f"query iteration fallback failed in seo_auto_audit: {exc}", "warning")

    await _run_audit_for_user(ctx)


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
