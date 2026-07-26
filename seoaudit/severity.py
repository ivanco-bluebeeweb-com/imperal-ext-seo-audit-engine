"""Слои важности и приоритизация находок.

Порядок слоёв не произвольный: он отражает СТОИМОСТЬ ОШИБКИ. Сломанная
индексируемость обнуляет качество текстов — на climtec.md русская главная
имела canonical на румынскую версию, то есть просила Google себя не
индексировать. Сколько бы мы ни улучшали её тексты, эффекта не было бы.

Поэтому находки слоя INDEXABILITY всегда важнее находок слоя CONTENT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Слои (меньше номер = раньше исправлять) ───────────────────────────────
LAYER_INDEXABILITY = 1   # canonical, robots, noindex, sitemap, hreflang
LAYER_TECHNICAL = 2      # HTTPS, редиректы, статусы, скорость
LAYER_STRUCTURE = 3      # URL, иерархия, внутренние ссылки
LAYER_CONTENT = 4        # title/description, H1, дубли
LAYER_I18N = 5           # языковая консистентность
LAYER_ENHANCEMENT = 6    # микроразметка, alt, прочие улучшения

LAYER_NAMES = {
    LAYER_INDEXABILITY: "Индексируемость",
    LAYER_TECHNICAL: "Техническое здоровье",
    LAYER_STRUCTURE: "Структура",
    LAYER_CONTENT: "Контент и метаданные",
    LAYER_I18N: "Языки",
    LAYER_ENHANCEMENT: "Улучшения",
}

# ── Серьёзность ───────────────────────────────────────────────────────────
CRITICAL = "critical"  # страница/раздел выпадает из выдачи
HIGH = "high"          # заметная потеря трафика или доверия
MEDIUM = "medium"      # упущенная выгода
LOW = "low"            # гигиена
INFO = "info"          # наблюдение, не требует работы

SEVERITY_WEIGHT = {
    CRITICAL: 100,
    HIGH: 40,
    MEDIUM: 12,
    LOW: 4,
    INFO: 0,
}

SEVERITY_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}

# ── Трудозатраты на исправление ───────────────────────────────────────────
EFFORT_TRIVIAL = 1   # правится инструментом автоматически (например, мета)
EFFORT_SMALL = 2     # ручная правка в админке, минуты
EFFORT_MEDIUM = 4    # нужен человек и решение (перенос, редирект)
EFFORT_LARGE = 8     # разработка, миграция, работа с сервером

EFFORT_NAMES = {
    EFFORT_TRIVIAL: "мелкая правка",
    EFFORT_SMALL: "минуты",
    EFFORT_MEDIUM: "требует решения",
    EFFORT_LARGE: "разработка",
}


@dataclass
class Finding:
    """Одна находка на одной странице (или на сайте, если url пуст)."""

    rule_id: str
    title: str                  # человеческая формулировка проблемы
    severity: str
    layer: int
    effort: int
    url: str = ""
    detail: str = ""            # что именно не так, с фактами
    evidence: dict[str, Any] = field(default_factory=dict)
    fix_hint: str = ""          # что делать
    auto_fixable: bool = False  # может ли починить wp-site-connector
    fix_field: str = ""         # какое поле править (meta_title/canonical_url/...)
    fix_value: str = ""         # предлагаемое значение, если оно вычислимо

    @property
    def score(self) -> float:
        """Приоритет: эффект, делённый на трудозатраты."""
        return SEVERITY_WEIGHT.get(self.severity, 0) / float(self.effort or 1)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["score"] = round(self.score, 2)
        d["layer_name"] = LAYER_NAMES.get(self.layer, str(self.layer))
        d["effort_name"] = EFFORT_NAMES.get(self.effort, str(self.effort))
        return d


def sort_findings(items: list[Finding]) -> list[Finding]:
    """Сортировка для отчёта: слой, затем серьёзность, затем выгода/труд."""
    return sorted(
        items,
        key=lambda f: (
            f.layer,
            SEVERITY_ORDER.get(f.severity, 9),
            -getattr(f, "score", 0.0),
            f.key,
            f.url,
        ),
    )


def site_health_score(items: list[Finding], page_count: int) -> int:
    """Грубая оценка 0-100. Нужна агентству для сравнения сайтов между собой."""
    if page_count <= 0:
        return 0
    penalty = 0.0
    for f in items:
        if f.severity == INFO:
            continue
        w = SEVERITY_WEIGHT.get(f.severity, 0)
        # находки на весь сайт весят полностью, постраничные — с затуханием
        penalty += w if not f.url else w / max(1.0, page_count ** 0.5)
    return max(0, min(100, int(round(100 - penalty / 3.0))))
