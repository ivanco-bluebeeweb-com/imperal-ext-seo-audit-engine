"""Отчёты: JSON для машин, Markdown для людей.

Агентству нужны ДВА разных документа:

* по КАЖДОМУ сайту — что чинить и в каком порядке (идёт исполнителю);
* по ПОРТФЕЛЮ — где горит и кем заняться первым (идёт руководителю).

Второй документ важнее на 200 сайтах: без него менеджер листает 200 отчётов
и не понимает, с чего начать.
"""

from __future__ import annotations

import json
from typing import Any

from .severity import CRITICAL, HIGH, INFO, LAYER_NAMES, LOW, MEDIUM
from .tasks import Task, summarise_tasks

_SEV_RU = {
    CRITICAL: "критично",
    HIGH: "важно",
    MEDIUM: "средне",
    LOW: "мелочь",
    INFO: "наблюдение",
}

_SEV_MARK = {
    CRITICAL: "[!]",
    HIGH: "[+]",
    MEDIUM: "[-]",
    LOW: "[ ]",
    INFO: "[.]",
}

_SEV_SEQ = (CRITICAL, HIGH, MEDIUM, LOW, INFO)


def host_of(origin: str) -> str:
    return (origin or "").replace("https://", "").replace("http://", "").strip("/")


# ══════════════════════════════════════════════════════════════════════════
# ОТЧЁТ ПО ОДНОМУ САЙТУ — для исполнителя
# ══════════════════════════════════════════════════════════════════════════

def site_report_md(site: dict[str, Any], findings: list[dict[str, Any]],
                   tasks: list[Task], *, pages: int, score: int) -> str:
    origin = site.get("origin", "")
    lines: list[str] = []
    add = lines.append

    add(f"# SEO-аудит: {host_of(origin)}")
    add("")
    add(f"Оценка здоровья: **{score}/100** · страниц проверено: **{pages}** "
        f"· задач к работе: **{len(tasks)}**")
    add("")

    if not findings:
        add("Проблем не найдено.")
        return "\n".join(lines)

    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    summary = " · ".join(
        f"{_SEV_RU[s]}: {by_sev[s]}" for s in _SEV_SEQ if by_sev.get(s)
    )
    add(f"Находки: {summary}")
    add("")

    # ── Список работ ──────────────────────────────────────────────────────
    if tasks:
        add("## Что делать, по порядку")
        add("")
        for i, t in enumerate(tasks, 1):
            auto = " · правится автоматически" if t.autofixable else ""
            add(f"**{i}. {_SEV_MARK[t.severity]} {t.title}**  ")
            add(f"Срок: {t.due_days} дн. · слой: {LAYER_NAMES.get(t.layer, t.layer)}"
                f" · страниц: {t.count}{auto}")
            add("")
            # В отчёте нужна только инструкция «что сделать» — список страниц
            # печатается ниже отдельным блоком, дублировать его незачем.
            instruction = t.body.split("ГДЕ", 1)[0]
            instruction = instruction.split("ЧТО СДЕЛАТЬ", 1)[-1].strip()
            add(instruction)
            add("")
            if t.urls:
                shown = t.urls[:10]
                for u in shown:
                    add(f"- {u}")
                if len(t.urls) > len(shown):
                    add(f"- …и ещё {len(t.urls) - len(shown)}")
                add("")

    # ── Полный перечень находок ───────────────────────────────────────────
    add("## Все находки")
    add("")
    add("| | Правило | Что не так | Страниц |")
    add("|---|---|---|---|")
    grouped: dict[str, dict[str, Any]] = {}
    for f in findings:
        g = grouped.setdefault(f["rule"], {
            "severity": f["severity"], "message": f["message"], "n": 0,
        })
        g["n"] += 1
    for rule, g in sorted(
        grouped.items(),
        key=lambda kv: (_SEV_SEQ.index(kv[1]["severity"])
                        if kv[1]["severity"] in _SEV_SEQ else 9, kv[0]),
    ):
        msg = (g["message"] or "").replace("|", "/")[:70]
        add(f"| {_SEV_MARK[g['severity']]} | `{rule}` | {msg} | {g['n']} |")
    add("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# СВОДКА ПО ПОРТФЕЛЮ — для руководителя
# ══════════════════════════════════════════════════════════════════════════

def portfolio_report_md(rows: list[dict[str, Any]], *, label: str = "") -> str:
    """rows: [{origin, score, pages, tasks, by_severity, top_issue, state}]"""
    lines: list[str] = []
    add = lines.append

    add(f"# Портфель сайтов{': ' + label if label else ''}")
    add("")

    ok = [r for r in rows if r.get("state") == "done"]
    failed = [r for r in rows if r.get("state") != "done"]
    total_tasks = sum(r.get("tasks", 0) for r in ok)
    total_pages = sum(r.get("pages", 0) for r in ok)
    crit_sites = [r for r in ok if r.get("by_severity", {}).get(CRITICAL)]

    add(f"Сайтов: **{len(rows)}** (проверено {len(ok)}"
        f"{f', с ошибкой {len(failed)}' if failed else ''}) "
        f"· страниц: **{total_pages}** · задач: **{total_tasks}**")
    add("")
    if crit_sites:
        add(f"**Требуют немедленного внимания: {len(crit_sites)}** — "
            "есть критичные дефекты индексируемости.")
        add("")

    # Худшие сверху: это и есть ответ на «с чего начать».
    add("## Сайты по состоянию (худшие сверху)")
    add("")
    add("| Сайт | Оценка | Стр. | Задач | Критич. | Важных | Главная проблема |")
    add("|---|---:|---:|---:|---:|---:|---|")
    for r in sorted(ok, key=lambda x: (x.get("score", 0),
                                       -x.get("tasks", 0))):
        sev = r.get("by_severity", {})
        add(f"| {host_of(r['origin'])} | {r.get('score', 0)} | {r.get('pages', 0)} "
            f"| {r.get('tasks', 0)} | {sev.get(CRITICAL, 0)} | {sev.get(HIGH, 0)} "
            f"| {(r.get('top_issue') or '—')[:52]} |")
    add("")

    if failed:
        add("## Не удалось проверить")
        add("")
        for r in failed:
            add(f"- **{host_of(r['origin'])}** — {r.get('error') or 'ошибка'}")
        add("")

    # Самые частые дефекты по портфелю: сигнал о СИСТЕМНОЙ причине.
    freq: dict[str, int] = {}
    for r in ok:
        for rule in r.get("rules", []):
            freq[rule] = freq.get(rule, 0) + 1
    if freq:
        add("## Частые дефекты по портфелю")
        add("")
        add("Если правило повторяется на многих сайтах — причина обычно общая: "
            "одна тема, один шаблон, один подрядчик.")
        add("")
        add("| Правило | На скольких сайтах |")
        add("|---|---:|")
        for rule, n in sorted(freq.items(), key=lambda kv: -kv[1])[:15]:
            add(f"| `{rule}` | {n} |")
        add("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# ОТЧЁТ ПО ОДНОЙ СТРАНИЦЕ — компактный, для одного адреса (например, Home)
# ══════════════════════════════════════════════════════════════════════════

def page_report_md(origin: str, page: dict[str, Any],
                   findings: list[dict[str, Any]], tasks: list[Task]) -> str:
    """Отчёт по ОДНОЙ странице сайта — opt-in режим, не портфель и не сайт.

    `page` — {"url", "title", "canonical", "status", "fetched_at"} (см.
    `bridge.find_page`). `findings` — уже отфильтрованные под эту страницу,
    каждая находка несёт `matched_via` ("url" — своя, "evidence" — страница
    затронута находкой уровня сайта). Задачи аналогично уже отфильтрованы.

    0-100 оценка здоровья сюда не переносится: это метрика САЙТА (считается
    по всем страницам), а не одной страницы — переносить её означало бы
    придумывать метрику, которой нет в движке.
    """
    lines: list[str] = []
    add = lines.append

    add(f"# SEO-аудит страницы: {page.get('url', '')}")
    add("")
    title = page.get("title") or ""
    if title:
        add(f"Заголовок страницы: «{title}»")
    canon = page.get("canonical") or ""
    if canon:
        add(f"Canonical: {canon}")
    fetched = page.get("fetched_at")
    if fetched:
        add(f"Последний обход: {fetched}")
    add("")
    add(f"Находок: **{len(findings)}** · задач к работе: **{len(tasks)}**")
    add("")

    if not findings:
        add("Проблем на этой странице не найдено.")
        return "\n".join(lines)

    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    summary = " · ".join(
        f"{_SEV_RU[s]}: {by_sev[s]}" for s in _SEV_SEQ if by_sev.get(s)
    )
    add(f"Разбивка по важности: {summary}")
    add("")

    has_critical_or_high = bool(by_sev.get(CRITICAL) or by_sev.get(HIGH))
    if has_critical_or_high:
        add("**Есть критичные или важные проблемы — начинать с них.**")
        add("")

    add("## Находки, важные сверху")
    add("")
    ordered = sorted(
        findings,
        key=lambda f: _SEV_SEQ.index(f["severity"]) if f["severity"] in _SEV_SEQ else 9,
    )
    for f in ordered:
        via = " _(через находку уровня сайта)_" if f.get("matched_via") == "evidence" else ""
        add(f"- {_SEV_MARK[f['severity']]} `{f['rule']}` — {f['message']}{via}")
    add("")

    if tasks:
        add("## Что делать")
        add("")
        for i, t in enumerate(tasks, 1):
            auto = " · правится автоматически" if t.autofixable else ""
            add(f"**{i}. {_SEV_MARK[t.severity]} {t.title}**  ")
            add(f"Срок: {t.due_days} дн. · слой: {LAYER_NAMES.get(t.layer, t.layer)}{auto}")
            add("")

    return "\n".join(lines)


def portfolio_json(rows: list[dict[str, Any]], tasks_by_site: dict[str, list[Task]]) -> str:
    payload = {
        "sites": rows,
        "tasks": {
            origin: [t.to_dict() for t in ts]
            for origin, ts in tasks_by_site.items()
        },
        "totals": {
            "sites": len(rows),
            "tasks": sum(len(ts) for ts in tasks_by_site.values()),
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
