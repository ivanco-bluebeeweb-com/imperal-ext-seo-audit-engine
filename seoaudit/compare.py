"""Сравнение двух прогонов одного сайта: что изменилось.

Один аудит отвечает на вопрос «что не так». Два аудита отвечают на вопрос,
ради которого всё и затевалось: «стало ли лучше». Без сравнения человек
читает второй отчёт как первый — заново, целиком, и не видит главного:
подействовала ли работа, которую он проделал между прогонами.

ТРИ СОСТОЯНИЯ, И ТРЕТЬЕ ВАЖНЕЕ ПЕРВЫХ ДВУХ:

* ПОЧИНЕНО   — находка была, её нет. Подтверждение работы.
* ОСТАЛОСЬ   — находка была и есть. Работа не сделана или не подействовала.
* ПОЯВИЛОСЬ  — находки не было, теперь есть. РЕГРЕССИЯ.

Регрессия — самое ценное, что даёт сравнение, потому что заметить её иначе
невозможно: в общем списке новая беда лежит вперемешку со старыми и ничем
не выделяется. Именно так тихо ломают сайт правкой темы или плагина.

ЧЕСТНОСТЬ СРАВНЕНИЯ. Сравнивать можно только сопоставимое. Если во втором
прогоне обошли 12 страниц вместо 50, «починенными» окажутся находки на 38
непосещённых страницах — то есть отчёт соврёт в самую приятную сторону.
Поэтому охват проверяется, и при заметном расхождении сравнение помечается
как неполное, а не выдаётся за победу.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .severity import SEVERITY_ORDER, SEVERITY_WEIGHT

# Насколько может «просесть» охват, чтобы сравнение ещё считалось честным.
# 25% выбрано не из красоты: обход живого сайта редко повторяется страница
# в страницу (пагинация, товары появляются и исчезают), но падение на
# четверть уже означает, что мы смотрим на другой корпус.
COVERAGE_TOLERANCE = 0.25


def _key(finding: dict[str, Any]) -> tuple[str, str]:
    """Чем находка отличается от другой находки.

    Правило + адрес. Не текст сообщения: он содержит числа («8 страниц»,
    «11 слов»), и сравнение по нему объявляло бы починкой любое изменение
    счётчика — то есть врало бы при каждом прогоне.
    """
    return (finding.get("rule", ""), (finding.get("url", "") or "").rstrip("/"))


@dataclass
class Change:
    """Одно изменение между прогонами."""

    kind: str            # fixed | remains | appeared
    rule: str
    url: str
    severity: str
    message: str = ""
    layer: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rule": self.rule,
            "url": self.url,
            "severity": self.severity,
            "message": self.message,
            "layer": self.layer,
        }


@dataclass
class Comparison:
    """Итог сравнения двух прогонов одного сайта."""

    origin: str = ""
    before_run: int = 0
    after_run: int = 0
    before_score: int = 0
    after_score: int = 0
    before_pages: int = 0
    after_pages: int = 0
    fixed: list[Change] = field(default_factory=list)
    remains: list[Change] = field(default_factory=list)
    appeared: list[Change] = field(default_factory=list)
    reliable: bool = True
    caveat: str = ""

    @property
    def score_delta(self) -> int:
        return self.after_score - self.before_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "before_run": self.before_run,
            "after_run": self.after_run,
            "before_score": self.before_score,
            "after_score": self.after_score,
            "score_delta": self.score_delta,
            "before_pages": self.before_pages,
            "after_pages": self.after_pages,
            "fixed": [c.to_dict() for c in self.fixed],
            "remains": [c.to_dict() for c in self.remains],
            "appeared": [c.to_dict() for c in self.appeared],
            "counts": {
                "fixed": len(self.fixed),
                "remains": len(self.remains),
                "appeared": len(self.appeared),
            },
            "reliable": self.reliable,
            "caveat": self.caveat,
        }


def _sort(changes: list[Change]) -> list[Change]:
    """Сначала важное, внутри важности — по слою (порядку работ)."""
    return sorted(
        changes,
        key=lambda c: (SEVERITY_ORDER.get(c.severity, 9), c.layer, c.rule),
    )


def compare_findings(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    origin: str = "",
    before_run: int = 0,
    after_run: int = 0,
    before_score: int = 0,
    after_score: int = 0,
    before_pages: int = 0,
    after_pages: int = 0,
) -> Comparison:
    """Сопоставляет два набора находок одного сайта."""
    before_map = {_key(f): f for f in before}
    after_map = {_key(f): f for f in after}

    cmp = Comparison(
        origin=origin,
        before_run=before_run,
        after_run=after_run,
        before_score=before_score,
        after_score=after_score,
        before_pages=before_pages,
        after_pages=after_pages,
    )

    for key, f in before_map.items():
        change = Change(
            kind="fixed" if key not in after_map else "remains",
            rule=f.get("rule", ""),
            url=f.get("url", ""),
            severity=f.get("severity", ""),
            message=f.get("message", ""),
            layer=int(f.get("layer", 0) or 0),
        )
        (cmp.fixed if change.kind == "fixed" else cmp.remains).append(change)

    for key, f in after_map.items():
        if key in before_map:
            continue
        cmp.appeared.append(Change(
            kind="appeared",
            rule=f.get("rule", ""),
            url=f.get("url", ""),
            severity=f.get("severity", ""),
            message=f.get("message", ""),
            layer=int(f.get("layer", 0) or 0),
        ))

    cmp.fixed = _sort(cmp.fixed)
    cmp.remains = _sort(cmp.remains)
    cmp.appeared = _sort(cmp.appeared)

    # Охват. Падение охвата превращает «не дошли» в «починено» — это самая
    # приятная и самая вредная ошибка, какую тут можно сделать.
    if before_pages and after_pages < before_pages * (1 - COVERAGE_TOLERANCE):
        cmp.reliable = False
        cmp.caveat = (
            f"Во втором прогоне проверено меньше страниц "
            f"({after_pages} против {before_pages}). Часть «починенного» "
            f"может быть просто непроверенным — сравнивайте с одинаковой "
            f"глубиной обхода."
        )
    elif not before and not after:
        cmp.caveat = "Находок нет ни в одном прогоне — сравнивать нечего."

    return cmp


def summarise(cmp: Comparison) -> str:
    """Человеческая строка итога — то, что говорится первым."""
    if not cmp.fixed and not cmp.appeared:
        if cmp.remains:
            return (f"Ничего не изменилось: {len(cmp.remains)} находок "
                    f"как были, так и есть.")
        return "Изменений нет."

    parts: list[str] = []
    if cmp.fixed:
        parts.append(f"починено {len(cmp.fixed)}")
    if cmp.appeared:
        parts.append(f"ПОЯВИЛОСЬ НОВЫХ {len(cmp.appeared)}")
    if cmp.remains:
        parts.append(f"осталось {len(cmp.remains)}")

    delta = cmp.score_delta
    if delta > 0:
        trend = f"оценка выросла на {delta} ({cmp.before_score} → {cmp.after_score})"
    elif delta < 0:
        trend = f"оценка УПАЛА на {abs(delta)} ({cmp.before_score} → {cmp.after_score})"
    else:
        trend = f"оценка не изменилась ({cmp.after_score})"

    return f"{', '.join(parts)}; {trend}."


def weight_of(changes: list[Change]) -> int:
    """Суммарный вес набора изменений — чтобы сравнивать не числом, а важностью.

    Пять починенных мелочей не перевешивают одну появившуюся критическую
    ошибку, и отчёт не должен создавать впечатление, будто перевешивают.
    """
    return sum(SEVERITY_WEIGHT.get(c.severity, 0) for c in changes)
