"""Выгрузка задач аудита в трекер (Asana и любой другой).

ГРАНИЦА ОТВЕТСТВЕННОСТИ. Этот модуль НЕ ходит в сеть и не знает про токены.
Он готовит «план выгрузки»: какая задача в какой раздел, с каким сроком и
какими тегами. Сами вызовы делает коннектор Imperal, у которого уже есть
доступ к трекеру.

Почему так, а не «движок сам пишет в Asana»:

  1. Движок работает на одной стандартной библиотеке и обходит чужие сайты.
     Давать такому процессу ключи от трекера агентства незачем.
  2. Трекеры разные: Asana, Notion, Jira, Trello. План выгрузки — общий,
     меняется только тот, кто его исполняет.
  3. План можно посмотреть ГЛАЗАМИ до того, как в трекере появятся сотни
     задач. На 200 сайтах это разница между инструментом и стихией.

ПОВТОРНЫЕ ПРОГОНЫ. У каждой задачи есть отпечаток (fingerprint) — он же
пишется в конец описания как «Метка аудита». Исполнитель обязан сначала
искать задачу по метке: нашёл — обновить, не нашёл — создать. Без этого
второй аудит удвоит содержимое трекера.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from .severity import CRITICAL, HIGH, INFO, LOW, MEDIUM
from .tasks import Task

# Раздел проекта по важности. Названия — человеческие: доска должна читаться
# без инструкции, а срок в названии задаёт ритм работы.
SECTION_BY_SEVERITY: dict[str, str] = {
    CRITICAL: "Критично — сейчас",
    HIGH: "Важно — эта неделя",
    MEDIUM: "Средне — этот месяц",
    LOW: "Средне — этот месяц",
    INFO: "Средне — этот месяц",
}

DEFAULT_PROJECT = "SEO Аудит — Портфель"


def plan_for_tracker(
    tasks: Iterable[Task],
    *,
    project: str = DEFAULT_PROJECT,
    today: date | None = None,
    assignee: str = "",
) -> list[dict[str, Any]]:
    """Превратить задачи аудита в план выгрузки.

    Возвращает список словарей, каждый — готовые аргументы для создания
    задачи в трекере: название, описание, раздел, срок, теги, метка.
    """
    base = today or date.today()
    out: list[dict[str, Any]] = []
    for t in tasks:
        out.append({
            "fingerprint": t.fingerprint,
            "name": t.title,
            "notes": t.body,
            "project": project,
            "section": SECTION_BY_SEVERITY.get(t.severity, "Средне — этот месяц"),
            "due": (base + timedelta(days=t.due_days)).isoformat(),
            "tags": list(t.tags),
            "assignee": assignee,
            # Служебное — для отчётности и сортировки на стороне исполнителя.
            "severity": t.severity,
            "site": t.site,
            "rule": t.rule,
            "pages": t.count,
            "autofixable": t.autofixable,
        })
    # Критичное первым: если выгрузка прервётся на середине, в трекере
    # окажется самое важное, а не случайная выборка.
    order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    out.sort(key=lambda d: (order.get(d["severity"], 9), d["site"], d["rule"]))
    return out


def write_plan(path: str | Path, plan: list[dict[str, Any]]) -> str:
    """Сохранить план выгрузки в JSON-файл."""
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def summarise_plan(plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Короткая сводка плана — что именно появится в трекере."""
    by_section: dict[str, int] = {}
    by_site: dict[str, int] = {}
    for d in plan:
        by_section[d["section"]] = by_section.get(d["section"], 0) + 1
        by_site[d["site"]] = by_site.get(d["site"], 0) + 1
    return {
        "tasks": len(plan),
        "by_section": by_section,
        "sites": len(by_site),
        "autofixable": sum(1 for d in plan if d["autofixable"]),
        "pages_touched": sum(d["pages"] for d in plan),
    }
