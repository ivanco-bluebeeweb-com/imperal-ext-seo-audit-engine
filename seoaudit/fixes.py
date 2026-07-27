"""Превращение находок в КОНКРЕТНЫЕ правки.

Аудит не правит сайты — это осознанное ограничение (см. handlers_read).
Но между «нашли дефект» и «починили» лежит работа, которую сейчас делает
человек вручную: решить, какое именно значение поставить в поле.

Этот модуль делает ровно её. На выходе — список правок вида
«страница X, поле meta_title, новое значение Y», готовых к применению
через wp-site-connector.

ГЛАВНОЕ ПРАВИЛО: не выдумывать. Если из данных страницы нельзя честно
собрать значение — правка помечается как требующая человека, а не
заполняется правдоподобным мусором. Плохой заголовок, поставленный
автоматически, хуже отсутствующего: он выглядит сделанной работой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# Рамки длины — те же, что в правилах: одна норма на весь инструмент.
from .rules import DESC_MAX, DESC_MIN, TITLE_MAX, TITLE_MIN

# Поля, которые умеет менять wp-site-connector (update_seo_meta).
FIELD_TITLE = "meta_title"
FIELD_DESC = "meta_description"
FIELD_CANONICAL = "canonical_url"
FIELD_ROBOTS = "robots"

# Что именно чинится автоматически. Ключ правила -> поле коннектора.
# Список сознательно КОРОТКИЙ: сюда попадает только то, где значение
# выводится из данных однозначно.
FIXABLE_RULES = {
    "title.missing": FIELD_TITLE,
    "title.too_short": FIELD_TITLE,
    "title.too_long": FIELD_TITLE,
    "description.missing": FIELD_DESC,
    "description.too_short": FIELD_DESC,
    "description.too_long": FIELD_DESC,
    "canonical.missing": FIELD_CANONICAL,
    "canonical.cross_language": FIELD_CANONICAL,
    "canonical.cross_host": FIELD_CANONICAL,
    "canonical.points_elsewhere": FIELD_CANONICAL,
    "duplicate.title": FIELD_TITLE,
    "duplicate.description": FIELD_DESC,
}

# Правки, которые НЕЛЬЗЯ применять пакетом без человека, даже если поле
# известно. Причина у каждой своя и написана в тексте правки.
NEEDS_HUMAN = {
    "duplicate.title",
    "duplicate.description",
    "title.too_short",
    "description.too_short",
}


@dataclass
class Fix:
    """Одна правка одного поля на одной странице."""

    url: str
    field: str
    current: str
    proposed: str
    rule: str
    reason: str
    confidence: str = "high"     # high | needs_review
    note: str = ""

    @property
    def ready(self) -> bool:
        """Можно ли применить без чтения человеком."""
        return bool(self.proposed) and self.confidence == "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "field": self.field,
            "current": self.current,
            "proposed": self.proposed,
            "rule": self.rule,
            "reason": self.reason,
            "confidence": self.confidence,
            "note": self.note,
            "ready": self.ready,
        }


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _slug_words(url: str) -> str:
    """Человеческие слова из адреса страницы: /aer-conditionat-mdv → 'aer conditionat mdv'."""
    path = urlsplit(url or "").path.strip("/")
    if not path:
        return ""
    last = path.split("/")[-1]
    last = re.sub(r"\.(html?|php|aspx?)$", "", last, flags=re.I)
    words = re.split(r"[-_+]+", last)
    words = [w for w in words if w and not w.isdigit()]
    return " ".join(words).strip()


def _brand_of(origin: str) -> str:
    """Название бренда из домена: https://climtec.md → 'Climtec'."""
    host = urlsplit(origin or "").netloc or (origin or "")
    host = host.split(":")[0]
    for prefix in ("www.",):
        if host.startswith(prefix):
            host = host[len(prefix):]
    base = host.split(".")[0] if host else ""
    return base.capitalize() if base else ""


def _trim_to(text: str, limit: int, *, keep_at_least: int = 0) -> str:
    """Обрезка ПО СЛОВАМ — обрубленное слово выглядит как поломка.

    `keep_at_least` включает обрезку ПО ПРЕДЛОЖЕНИЮ: если точка попадает в
    рамку и после неё остаётся не меньше указанного, режем по ней.

    Зачем это нужно, видно на живом описании: обрезка по словам оставила
    «…Analiza parametrilor și a liniei» — фраза обрывается на предлоге, и в
    выдаче это читается как сломанная страница, хотя формально длина в норме.
    Законченное предложение короче, но выглядит как текст, а не как обрубок.

    Нижняя граница обязательна: без неё описание из одного короткого первого
    предложения схлопнулось бы до пары слов — формально «красиво», по сути
    хуже исходного.
    """
    text = _clean(text)
    if len(text) <= limit:
        return text

    cut = text[:limit]

    if keep_at_least:
        # Ищем последний конец предложения внутри рамки.
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        if end == -1 and cut.rstrip().endswith((".", "!", "?")):
            end = len(cut.rstrip()) - 1
        if end >= keep_at_least:
            return cut[:end + 1].strip()

    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,.;:—-")


def _title_fix(page: dict[str, Any], origin: str) -> tuple[str, str, str]:
    """Готовит заголовок. Возвращает (значение, уверенность, примечание)."""
    current = _clean(page.get("title") or "")
    brand = _brand_of(origin)

    # Слишком длинный — единственный случай, где правка ОДНОЗНАЧНА:
    # смысл уже написан человеком, надо лишь уложить в рамку.
    if current and len(current) > TITLE_MAX:
        # Хвост вида « — Бренд» отрезаем первым: он повторяется на всех
        # страницах и в выдаче всё равно не виден.
        stripped = re.sub(r"\s*[|—–-]\s*" + re.escape(brand) + r"\s*$", "",
                          current, flags=re.I) if brand else current
        if TITLE_MIN <= len(stripped) <= TITLE_MAX:
            return stripped, "high", "убран повторяющийся хвост с названием сайта"
        return _trim_to(stripped, TITLE_MAX), "high", "сокращён до рамки выдачи"

    # Пустой — собираем из H1 или из адреса. H1 написан человеком, поэтому
    # он предпочтительнее слов из адреса.
    h1 = ""
    h1_list = page.get("h1") or []
    if isinstance(h1_list, list) and h1_list:
        h1 = _clean(str(h1_list[0]))
    base = h1 or _slug_words(page.get("final_url") or page.get("url") or "").capitalize()
    if not base:
        return "", "needs_review", "нет ни H1, ни говорящего адреса — нужен человек"

    candidate = f"{base} — {brand}" if brand and brand.lower() not in base.lower() else base
    if len(candidate) > TITLE_MAX:
        candidate = _trim_to(candidate, TITLE_MAX)
    if len(candidate) < TITLE_MIN:
        return candidate, "needs_review", (
            f"собран из H1, но короче {TITLE_MIN} знаков — стоит дописать")
    return candidate, "high", "собран из H1 страницы"


def _desc_fix(page: dict[str, Any]) -> tuple[str, str, str]:
    """Готовит описание. Обрезать длинное — можно, сочинять текст — нет."""
    current = _clean(page.get("description") or "")
    if current and len(current) > DESC_MAX:
        # keep_at_least=DESC_MIN: режем по концу предложения, но только если
        # после этого описание остаётся полноценным. Иначе — по словам.
        return (_trim_to(current, DESC_MAX, keep_at_least=DESC_MIN),
                "high", "сокращено до рамки выдачи")
    # Пустое описание автоматически НЕ пишем: это единственное место в
    # выдаче, где сайт говорит своими словами. Сгенерированный из текста
    # обрывок выглядит как машинный мусор и снижает кликабельность.
    return "", "needs_review", (
        "описание должен написать человек — это рекламный текст в выдаче")


def _canonical_fix(page: dict[str, Any], rule: str) -> tuple[str, str, str]:
    """Канонический адрес почти всегда = собственный адрес страницы."""
    self_url = (page.get("final_url") or page.get("url") or "").strip()
    if not self_url:
        return "", "needs_review", "неизвестен адрес самой страницы"
    if rule == "canonical.points_elsewhere":
        # Может быть НАМЕРЕННО: склейка вариантов товара, пагинация.
        return self_url, "needs_review", (
            "проверьте: указание на другую страницу иногда делают намеренно")
    return self_url, "high", "канонический адрес указывает на саму страницу"


def build_fixes(findings: list[dict[str, Any]],
                pages_by_url: dict[str, dict[str, Any]],
                origin: str = "") -> list[Fix]:
    """Превращает находки в правки. Одна страница+поле = одна правка.

    Дубли схлопываются: если у страницы и `title.missing`, и `duplicate.title`,
    поле всё равно одно, и две правки на него — это конфликт, а не работа.
    """
    out: dict[tuple[str, str], Fix] = {}

    for f in findings:
        rule = f.get("key") or f.get("rule") or ""
        field_name = FIXABLE_RULES.get(rule)
        if not field_name:
            continue
        url = (f.get("url") or "").strip()
        if not url:
            continue
        page = pages_by_url.get(url.rstrip("/")) or pages_by_url.get(url) or {}

        if field_name == FIELD_TITLE:
            proposed, conf, note = _title_fix(page, origin)
            current = _clean(page.get("title") or "")
        elif field_name == FIELD_DESC:
            proposed, conf, note = _desc_fix(page)
            current = _clean(page.get("description") or "")
        elif field_name == FIELD_CANONICAL:
            proposed, conf, note = _canonical_fix(page, rule)
            current = _clean(page.get("canonical") or "")
        else:
            continue

        if rule in NEEDS_HUMAN and conf == "high":
            conf = "needs_review"
            if rule.startswith("duplicate."):
                note = ("значение повторяется на нескольких страницах — "
                        "их надо развести по смыслу, а не выровнять машинно")

        # Правка, не меняющая значение, — не работа, а шум в отчёте.
        if proposed and _clean(proposed) == current:
            continue

        fix = Fix(
            url=url,
            field=field_name,
            current=current,
            proposed=proposed,
            rule=rule,
            reason=_clean(f.get("message") or f.get("title") or rule),
            confidence=conf,
            note=note,
        )
        key = (url.rstrip("/"), field_name)
        prior = out.get(key)
        # При конфликте оставляем ту, что требует человека: молча применить
        # автоматическую поверх спорной — это тихая потеря контроля.
        if prior is None or (prior.ready and not fix.ready):
            out[key] = fix

    return sorted(out.values(), key=lambda x: (not x.ready, x.url, x.field))


def summarise_fixes(fixes: list[Fix]) -> dict[str, Any]:
    ready = [f for f in fixes if f.ready]
    review = [f for f in fixes if not f.ready]
    by_field: dict[str, int] = {}
    for f in fixes:
        by_field[f.field] = by_field.get(f.field, 0) + 1
    return {
        "total": len(fixes),
        "ready": len(ready),
        "needs_review": len(review),
        "pages": len({f.url for f in fixes}),
        "by_field": by_field,
    }
