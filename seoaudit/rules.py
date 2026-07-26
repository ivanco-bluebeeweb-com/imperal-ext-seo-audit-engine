"""Правила аудита.

Каждое правило — чистая функция от данных страницы (или от корпуса сайта)
к списку находок. Никаких обращений к сети: правила работают по уже
собранным данным, поэтому их легко тестировать и легко переигрывать без
повторного обхода сайта.

Правила выведены из РЕАЛЬНЫХ дефектов, найденных на живом сайте, а не
придуманы: canonical на чужую языковую версию, языковое расхождение
метаданных, подмена ответа кешем, отсутствие раздела в sitemap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from .extract import HeadData, collapse, same_url, visible_len
from .severity import (
    CRITICAL,
    EFFORT_LARGE,
    EFFORT_MEDIUM,
    EFFORT_SMALL,
    EFFORT_TRIVIAL,
    HIGH,
    INFO,
    LAYER_CONTENT,
    LAYER_ENHANCEMENT,
    LAYER_I18N,
    LAYER_INDEXABILITY,
    LAYER_STRUCTURE,
    LAYER_TECHNICAL,
    LOW,
    MEDIUM,
)

# ── Нормы длины метаданных ────────────────────────────────────────────────
# Не «стандарт Google» (его нет), а рабочие рамки: то, что обычно влезает в
# выдачу без обрезания. Ровно эти рамки применялись на climtec.md.
TITLE_MIN = 30
TITLE_MAX = 60
DESC_MIN = 120
DESC_MAX = 160

# Порог, ниже которого страница считается тонкой по содержанию.
THIN_WORDS = 150

# Медленный ответ (мс). Не Core Web Vitals, а грубый сигнал по TTFB+загрузке.
SLOW_MS = 2500
VERY_SLOW_MS = 5000


@dataclass
class Finding:
    """Одна находка. `key` — стабильный идентификатор правила."""

    key: str
    layer: int
    severity: str
    effort: int
    title: str
    detail: str = ""
    url: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    fixable: bool = False  # можно ли починить автоматически через коннектор

    @property
    def score(self) -> float:
        """Выгода на единицу труда — этим сортируется список работ.

        Смысл простой: сначала то, что даёт много эффекта и правится дешево.
        Один сломанный canonical на главной важнее двадцати переписанных
        описаний — именно этот случай был на climtec.md.
        """
        from .severity import SEVERITY_WEIGHT

        return SEVERITY_WEIGHT.get(self.severity, 0) / max(1, self.effort)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "layer": self.layer,
            "severity": self.severity,
            "effort": self.effort,
            "title": self.title,
            "detail": self.detail,
            "url": self.url,
            "evidence": self.evidence,
            "fixable": self.fixable,
        }


def _lang_base(code: str) -> str:
    """'ru-RU' -> 'ru'; 'x-default' -> ''."""
    c = (code or "").strip().lower().replace("_", "-")
    if not c or c == "x-default":
        return ""
    return c.split("-")[0]


_CYR = re.compile(r"[а-яёА-ЯЁ]")
_RO_DIACRITICS = re.compile(r"[ăâîșțĂÂÎȘȚ]")


def guess_script_lang(text: str) -> str:
    """Очень грубое определение: кириллица или латиница.

    Задача не в полноценной детекции языка, а в поиске ГРУБЫХ расхождений —
    например, русский meta title на румынской странице. Именно такой дефект
    был найден на climtec.md (посты #2100 и #2072).
    """
    s = text or ""
    if not s.strip():
        return ""
    cyr = len(_CYR.findall(s))
    lat = len(re.findall(r"[a-zA-Z]", s))
    if cyr == 0 and lat == 0:
        return ""
    if cyr > lat * 2:
        return "cyrillic"
    if lat > cyr * 2:
        return "latin"
    return "mixed"


def expected_script(lang_code: str) -> str:
    """Какое письмо ожидается для языка страницы."""
    base = _lang_base(lang_code)
    if base in {"ru", "uk", "be", "bg", "sr", "mk", "kk"}:
        return "cyrillic"
    if base in {
        "ro", "en", "de", "fr", "it", "es", "pt", "pl", "cs", "sk",
        "hu", "tr", "nl", "sv", "da", "no", "fi", "et", "lv", "lt",
    }:
        return "latin"
    return ""


# ══════════════════════════════════════════════════════════════════════════
# СЛОЙ 1 — ИНДЕКСИРУЕМОСТЬ
# ══════════════════════════════════════════════════════════════════════════

def rule_canonical(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """canonical: отсутствие, дубли, чужой адрес, чужой язык.

    Самое дорогое правило набора. Если canonical указывает на другую
    страницу, поисковик считает эту страницу дублем и не индексирует её.
    """
    out: list[Finding] = []
    url = page.get("final_url") or page.get("url") or ""
    n = len(head.canonical_all)

    if n == 0:
        out.append(Finding(
            key="canonical.missing",
            layer=LAYER_INDEXABILITY,
            severity=MEDIUM,
            effort=EFFORT_TRIVIAL,
            title="Нет канонического адреса",
            detail=(
                "У страницы не указан canonical. Обычно SEO-плагин ставит его "
                "автоматически; если его нет — страница уязвима к дублям "
                "(один товар по нескольким адресам, параметры сортировки и т.п.)."
            ),
            url=url,
            fixable=True,
        ))
        return out

    if n > 1:
        out.append(Finding(
            key="canonical.duplicate_tag",
            layer=LAYER_INDEXABILITY,
            severity=HIGH,
            effort=EFFORT_SMALL,
            title=f"Канонический адрес указан {n} раза",
            detail=(
                "На странице несколько тегов canonical. Поисковик может "
                "проигнорировать все. Обычная причина — тема и SEO-плагин "
                "выводят тег одновременно."
            ),
            url=url,
            evidence={"canonicals": head.canonical_all[:5]},
        ))

    canon = head.canonical
    if not canon:
        return out

    # canonical на другой хост — почти всегда ошибка миграции/копирования
    canon_host = urlsplit(canon).netloc.lower().removeprefix("www.")
    page_host = urlsplit(url).netloc.lower().removeprefix("www.")
    if canon_host and page_host and canon_host != page_host:
        out.append(Finding(
            key="canonical.cross_host",
            layer=LAYER_INDEXABILITY,
            severity=CRITICAL,
            effort=EFFORT_SMALL,
            title="Канонический адрес ведёт на другой домен",
            detail=(
                f"Страница объявляет каноническим адрес на «{canon_host}». "
                "Это указание поисковику индексировать чужой домен вместо "
                "этой страницы. Частая причина — копирование сайта без "
                "правки настроек."
            ),
            url=url,
            evidence={"canonical": canon},
            fixable=True,
        ))
        return out

    if same_url(canon, url):
        return out  # всё в порядке: самоссылка

    # canonical на ДРУГУЮ страницу того же сайта.
    # Проверяем самый коварный случай: другая языковая версия.
    others = ctx.get("by_url") or {}
    target = others.get(canon.rstrip("/")) or others.get(canon)
    target_lang = ""
    if target:
        target_lang = (target.get("html_lang") or "")
    page_lang = head.html_lang or ""

    cross_language = bool(
        target_lang and page_lang
        and _lang_base(target_lang) != _lang_base(page_lang)
    )

    # либо докажем по hreflang: canonical равен адресу ДРУГОГО языка
    if not cross_language and head.hreflang:
        for alt in head.hreflang:
            if same_url(alt.get("href", ""), canon):
                if _lang_base(alt.get("lang", "")) != _lang_base(page_lang):
                    cross_language = True
                    target_lang = alt.get("lang", "")
                    break

    if cross_language:
        out.append(Finding(
            key="canonical.cross_language",
            layer=LAYER_INDEXABILITY,
            severity=CRITICAL,
            effort=EFFORT_TRIVIAL,
            title="Канонический адрес ведёт на другую языковую версию",
            detail=(
                f"Страница на языке «{page_lang}» объявляет каноническим адрес "
                f"версии на «{target_lang or 'другом языке'}». Это прямая "
                "инструкция поисковику НЕ индексировать эту страницу и "
                "считать её копией. Языковые версии — разные страницы, "
                "каждая должна быть канонична сама себе, а связь между ними "
                "выражается через hreflang."
            ),
            url=url,
            evidence={"canonical": canon, "page_lang": page_lang,
                      "target_lang": target_lang},
            fixable=True,
        ))
    else:
        out.append(Finding(
            key="canonical.points_elsewhere",
            layer=LAYER_INDEXABILITY,
            severity=HIGH,
            effort=EFFORT_SMALL,
            title="Канонический адрес ведёт на другую страницу",
            detail=(
                "Страница просит индексировать вместо себя другой адрес. "
                "Иногда это осознанно (варианты товара), но чаще — ошибка "
                "копирования настроек между страницами."
            ),
            url=url,
            evidence={"canonical": canon},
            fixable=True,
        ))
    return out


def rule_noindex(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """noindex на странице, которую сайт сам заявил в карте сайта."""
    out: list[Finding] = []
    if not head.is_noindex:
        return out
    url = page.get("final_url") or page.get("url") or ""
    in_sitemap = page.get("source") == "sitemap"
    out.append(Finding(
        key="robots.noindex_in_sitemap" if in_sitemap else "robots.noindex",
        layer=LAYER_INDEXABILITY,
        severity=CRITICAL if in_sitemap else INFO,
        effort=EFFORT_SMALL,
        title=(
            "Страница закрыта от индексации, но заявлена в карте сайта"
            if in_sitemap else
            "Страница закрыта от индексации"
        ),
        detail=(
            "Сайт одновременно говорит поисковику «вот важная страница» "
            "(sitemap) и «не индексируй её» (noindex). Противоречие: либо "
            "убрать из карты, либо снять запрет."
            if in_sitemap else
            "У страницы стоит noindex. Если это раздел служебный — нормально."
        ),
        url=url,
        evidence={"robots": head.robots},
    ))
    return out


def rule_status_and_redirects(page: dict[str, Any], head: HeadData,
                              ctx: dict[str, Any]) -> list[Finding]:
    """Коды ответа и цепочки редиректов."""
    out: list[Finding] = []
    url = page.get("url") or ""
    status = page.get("status")
    redirects = int(page.get("redirects") or 0)

    if page.get("state") == "error" and not status:
        out.append(Finding(
            key="http.unreachable",
            layer=LAYER_TECHNICAL,
            severity=HIGH,
            effort=EFFORT_MEDIUM,
            title="Страница недоступна",
            detail=f"Не удалось загрузить: {page.get('error') or 'нет ответа'}",
            url=url,
        ))
        return out

    if status and status >= 500:
        out.append(Finding(
            key="http.server_error",
            layer=LAYER_TECHNICAL,
            severity=CRITICAL,
            effort=EFFORT_MEDIUM,
            title=f"Ошибка сервера {status}",
            detail="Страница отдаёт ошибку сервера — она выпадет из индекса.",
            url=url,
            evidence={"status": status},
        ))
    elif status == 404 and page.get("source") == "sitemap":
        out.append(Finding(
            key="http.404_in_sitemap",
            layer=LAYER_INDEXABILITY,
            severity=HIGH,
            effort=EFFORT_SMALL,
            title="В карте сайта указан несуществующий адрес",
            detail=(
                "Карта сайта ведёт на страницу, которой нет. Это тратит бюджет "
                "обхода и снижает доверие к карте."
            ),
            url=url,
            evidence={"status": status},
        ))
    elif status and 400 <= status < 500 and status not in (401, 403):
        out.append(Finding(
            key="http.client_error",
            layer=LAYER_TECHNICAL,
            severity=MEDIUM,
            effort=EFFORT_SMALL,
            title=f"Страница отдаёт {status}",
            detail="Адрес недоступен для посетителей и поисковиков.",
            url=url,
            evidence={"status": status},
        ))

    if redirects >= 3:
        out.append(Finding(
            key="http.redirect_chain",
            layer=LAYER_TECHNICAL,
            severity=MEDIUM,
            effort=EFFORT_SMALL,
            title=f"Цепочка из {redirects} перенаправлений",
            detail=(
                "Длинные цепочки замедляют загрузку и размывают сигналы "
                "ссылок. Лучше вести на конечный адрес одним переходом."
            ),
            url=url,
            evidence={"chain": page.get("chain") or []},
        ))

    if page.get("source") == "sitemap" and redirects >= 1:
        out.append(Finding(
            key="sitemap.redirecting_url",
            layer=LAYER_INDEXABILITY,
            severity=LOW,
            effort=EFFORT_SMALL,
            title="В карте сайта указан перенаправляемый адрес",
            detail=(
                "Карта должна содержать конечные адреса, иначе поисковик "
                "тратит обход на переходы."
            ),
            url=url,
            evidence={"final_url": page.get("final_url")},
        ))
    return out


def rule_https(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """HTTP вместо HTTPS."""
    url = page.get("final_url") or page.get("url") or ""
    if url.startswith("http://"):
        return [Finding(
            key="security.no_https",
            layer=LAYER_TECHNICAL,
            severity=HIGH,
            effort=EFFORT_MEDIUM,
            title="Страница работает без HTTPS",
            detail=(
                "Браузеры помечают такие страницы как небезопасные, поисковики "
                "занижают. Нужен сертификат и переадресация на https."
            ),
            url=url,
        )]
    return []


# ══════════════════════════════════════════════════════════════════════════
# СЛОЙ 4 — КОНТЕНТ И МЕТАДАННЫЕ
# ══════════════════════════════════════════════════════════════════════════

def rule_title(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """Заголовок: отсутствие, длина, дубли тега."""
    out: list[Finding] = []
    url = page.get("final_url") or page.get("url") or ""
    t = collapse(head.title)
    n = visible_len(t)

    if head.title_count > 1:
        out.append(Finding(
            key="title.duplicate_tag",
            layer=LAYER_CONTENT,
            severity=MEDIUM,
            effort=EFFORT_SMALL,
            title=f"Тег <title> присутствует {head.title_count} раза",
            detail="Дублирующийся заголовок — обычно конфликт темы и SEO-плагина.",
            url=url,
        ))

    if not t:
        out.append(Finding(
            key="title.missing",
            layer=LAYER_CONTENT,
            severity=HIGH,
            effort=EFFORT_TRIVIAL,
            title="Нет заголовка страницы",
            detail=(
                "Заголовок — главная строка в результатах поиска. Без него "
                "поисковик придумает её сам, обычно неудачно."
            ),
            url=url,
            fixable=True,
        ))
    elif n < TITLE_MIN:
        out.append(Finding(
            key="title.too_short",
            layer=LAYER_CONTENT,
            severity=MEDIUM,
            effort=EFFORT_TRIVIAL,
            title=f"Заголовок короткий ({n} симв.)",
            detail=(
                f"Рабочая рамка — {TITLE_MIN}-{TITLE_MAX} символов. Короткий "
                "заголовок не использует место в выдаче."
            ),
            url=url,
            evidence={"title": t, "length": n},
            fixable=True,
        ))
    elif n > TITLE_MAX:
        out.append(Finding(
            key="title.too_long",
            layer=LAYER_CONTENT,
            severity=LOW,
            effort=EFFORT_TRIVIAL,
            title=f"Заголовок длинный ({n} симв.)",
            detail=f"Свыше {TITLE_MAX} символов выдача обрезает конец.",
            url=url,
            evidence={"title": t, "length": n},
            fixable=True,
        ))
    return out


def rule_description(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """Описание: отсутствие и длина."""
    out: list[Finding] = []
    url = page.get("final_url") or page.get("url") or ""
    d = collapse(head.description)
    n = visible_len(d)

    if head.description_count > 1:
        out.append(Finding(
            key="description.duplicate_tag",
            layer=LAYER_CONTENT,
            severity=LOW,
            effort=EFFORT_SMALL,
            title=f"Описание указано {head.description_count} раза",
            detail="Несколько тегов description — конфликт плагинов или темы.",
            url=url,
        ))

    if not d:
        out.append(Finding(
            key="description.missing",
            layer=LAYER_CONTENT,
            severity=MEDIUM,
            effort=EFFORT_TRIVIAL,
            title="Нет описания страницы",
            detail=(
                "Без описания поисковик составит фрагмент сам, выдернув случайный "
                "текст. Своё описание повышает кликабельность."
            ),
            url=url,
            fixable=True,
        ))
    elif n < DESC_MIN:
        out.append(Finding(
            key="description.too_short",
            layer=LAYER_CONTENT,
            severity=LOW,
            effort=EFFORT_TRIVIAL,
            title=f"Описание короткое ({n} симв.)",
            detail=f"Рабочая рамка — {DESC_MIN}-{DESC_MAX} символов.",
            url=url,
            evidence={"length": n},
            fixable=True,
        ))
    elif n > DESC_MAX:
        out.append(Finding(
            key="description.too_long",
            layer=LAYER_CONTENT,
            severity=LOW,
            effort=EFFORT_TRIVIAL,
            title=f"Описание длинное ({n} симв.)",
            detail=f"Свыше {DESC_MAX} символов выдача обрезает конец.",
            url=url,
            evidence={"length": n},
            fixable=True,
        ))
    return out


def rule_h1(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """H1: отсутствие или несколько."""
    out: list[Finding] = []
    url = page.get("final_url") or page.get("url") or ""
    h1s = [collapse(x) for x in head.h1 if collapse(x)]

    if not h1s:
        out.append(Finding(
            key="h1.missing",
            layer=LAYER_CONTENT,
            severity=MEDIUM,
            effort=EFFORT_SMALL,
            title="Нет заголовка H1",
            detail=(
                "H1 — главный заголовок в самом содержании страницы. Помогает "
                "и поисковику, и читателю понять, о чём страница."
            ),
            url=url,
        ))
    elif len(h1s) > 1:
        out.append(Finding(
            key="h1.multiple",
            layer=LAYER_CONTENT,
            severity=LOW,
            effort=EFFORT_SMALL,
            title=f"На странице {len(h1s)} заголовков H1",
            detail="Размывает тему страницы. Обычно достаточно одного.",
            url=url,
            evidence={"h1": h1s[:5]},
        ))
    return out


def rule_thin_content(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """Мало текста на странице."""
    url = page.get("final_url") or page.get("url") or ""
    wc = int(head.word_count or 0)
    if wc and wc < THIN_WORDS:
        return [Finding(
            key="content.thin",
            layer=LAYER_CONTENT,
            severity=MEDIUM,
            effort=EFFORT_LARGE,
            title=f"Мало текста на странице ({wc} слов)",
            detail=(
                "Поисковику почти нечего индексировать. Метаданные этого не "
                "заменяют: описание обещает содержание, которого нет. Частый "
                "случай — карточки товара, где все характеристики лежат в "
                "полях, невидимых в тексте страницы."
            ),
            url=url,
            evidence={"word_count": wc},
        )]
    return []


def rule_images_alt(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """Картинки без alt."""
    url = page.get("final_url") or page.get("url") or ""
    total = int(head.images_total or 0)
    noalt = int(head.images_no_alt or 0)
    if total >= 5 and noalt >= max(3, total // 2):
        return [Finding(
            key="images.missing_alt",
            layer=LAYER_ENHANCEMENT,
            severity=LOW,
            effort=EFFORT_MEDIUM,
            title=f"{noalt} из {total} картинок без описания (alt)",
            detail=(
                "alt нужен для доступности и для поиска по картинкам. "
                "Заполняется по смыслу изображения."
            ),
            url=url,
            evidence={"images_total": total, "images_no_alt": noalt},
        )]
    return []


def rule_performance(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """Медленный ответ (грубый сигнал, не Core Web Vitals)."""
    url = page.get("final_url") or page.get("url") or ""
    ms = int(page.get("elapsed_ms") or 0)
    if ms >= VERY_SLOW_MS:
        sev, key = HIGH, "performance.very_slow"
    elif ms >= SLOW_MS:
        sev, key = MEDIUM, "performance.slow"
    else:
        return []
    return [Finding(
        key=key,
        layer=LAYER_TECHNICAL,
        severity=sev,
        effort=EFFORT_MEDIUM,
        title=f"Медленный ответ ({ms} мс)",
        detail=(
            "Замер загрузки HTML без картинок и скриптов, то есть реальная "
            "скорость для посетителя ещё ниже. Стоит проверить кеширование "
            "и хостинг."
        ),
        url=url,
        evidence={"elapsed_ms": ms},
    )]


# ══════════════════════════════════════════════════════════════════════════
# СЛОЙ 5 — ЯЗЫКИ
# ══════════════════════════════════════════════════════════════════════════

def rule_lang_declared(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """Не объявлен язык страницы."""
    url = page.get("final_url") or page.get("url") or ""
    if not (head.html_lang or "").strip():
        return [Finding(
            key="i18n.lang_missing",
            layer=LAYER_I18N,
            severity=LOW,
            effort=EFFORT_SMALL,
            title="Не указан язык страницы",
            detail=(
                "Атрибут lang помогает поисковику и браузеру понять язык "
                "текста. Особенно важен на многоязычных сайтах."
            ),
            url=url,
        )]
    return []


def rule_lang_mismatch(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """Метаданные на языке, отличном от языка страницы.

    Так на climtec.md нашлись румынские статьи с РУССКИМИ заголовками в
    выдаче: страница ro-RO, а meta title кириллицей.
    """
    out: list[Finding] = []
    url = page.get("final_url") or page.get("url") or ""
    want = expected_script(head.html_lang)
    if not want:
        return out

    for field_name, value, human in (
        ("title", head.title, "заголовок"),
        ("description", head.description, "описание"),
    ):
        got = guess_script_lang(value)
        if got and got != "mixed" and got != want:
            out.append(Finding(
                key=f"i18n.{field_name}_language_mismatch",
                layer=LAYER_I18N,
                severity=HIGH,
                effort=EFFORT_TRIVIAL,
                title=f"{human.capitalize()} не на языке страницы",
                detail=(
                    f"Страница объявлена как «{head.html_lang}», а {human} "
                    f"написан другим письмом ({got} вместо {want}). "
                    "В результатах поиска посетитель увидит текст на чужом "
                    "языке — почти гарантированный отказ. Частая причина: "
                    "перевод страницы сделали, а метаданные скопировали."
                ),
                url=url,
                evidence={"html_lang": head.html_lang, "value": collapse(value)[:120]},
                fixable=True,
            ))
    return out


def rule_hreflang(page: dict[str, Any], head: HeadData, ctx: dict[str, Any]) -> list[Finding]:
    """hreflang: нет самоссылки, или язык страницы не заявлен в наборе."""
    out: list[Finding] = []
    url = page.get("final_url") or page.get("url") or ""
    if not head.hreflang:
        return out

    has_self = any(same_url(a.get("href", ""), url) for a in head.hreflang)
    if not has_self:
        out.append(Finding(
            key="i18n.hreflang_no_self",
            layer=LAYER_I18N,
            severity=MEDIUM,
            effort=EFFORT_SMALL,
            title="В наборе hreflang нет ссылки на саму страницу",
            detail=(
                "Правило hreflang требует, чтобы страница перечисляла и себя. "
                "Иначе поисковик может игнорировать всю группу."
            ),
            url=url,
            evidence={"hreflang": head.hreflang[:6]},
        ))

    page_base = _lang_base(head.html_lang)
    if page_base:
        declared = {_lang_base(a.get("lang", "")) for a in head.hreflang}
        declared.discard("")
        if declared and page_base not in declared:
            out.append(Finding(
                key="i18n.hreflang_lang_absent",
                layer=LAYER_I18N,
                severity=LOW,
                effort=EFFORT_SMALL,
                title="Язык страницы отсутствует в наборе hreflang",
                detail=(
                    f"Страница на «{head.html_lang}», а в наборе перечислены "
                    f"только: {', '.join(sorted(declared))}."
                ),
                url=url,
            ))
    return out


# ══════════════════════════════════════════════════════════════════════════
# ПРАВИЛА УРОВНЯ САЙТА — видят весь корпус сразу
# ══════════════════════════════════════════════════════════════════════════

def site_rule_duplicate_meta(pages: list[dict[str, Any]],
                             ctx: dict[str, Any]) -> list[Finding]:
    """Одинаковые заголовки/описания на разных страницах.

    Видно только на корпусе: по одной странице такой дефект не обнаружить.
    Дубли заставляют страницы конкурировать друг с другом.
    """
    out: list[Finding] = []
    for field_name, human, sev in (
        ("title", "заголовок", MEDIUM),
        ("description", "описание", LOW),
    ):
        groups: dict[str, list[str]] = {}
        for p in pages:
            if p.get("status") != 200 or p.get("noindex"):
                continue
            val = collapse(p.get(field_name) or "")
            if not val:
                continue
            groups.setdefault(val.lower(), []).append(
                p.get("final_url") or p.get("url") or ""
            )
        for val, urls in groups.items():
            if len(urls) < 2:
                continue
            out.append(Finding(
                key=f"duplicate.{field_name}",
                layer=LAYER_CONTENT,
                severity=sev,
                effort=EFFORT_TRIVIAL,
                title=f"Одинаковый {human} на {len(urls)} страницах",
                detail=(
                    f"{human.capitalize()} «{val[:70]}» повторяется. Страницы "
                    "начинают конкурировать между собой, а поисковик не понимает, "
                    "какую показывать. Каждая страница должна отвечать на своё "
                    "отдельное намерение."
                ),
                url=urls[0],
                evidence={"value": val[:160], "urls": urls[:12], "count": len(urls)},
                fixable=True,
            ))
    return out


def site_rule_canonical_clusters(pages: list[dict[str, Any]],
                                 ctx: dict[str, Any]) -> list[Finding]:
    """Много страниц, канонизированных на один адрес.

    Признак того, что целый раздел выпал из индекса.
    """
    clusters: dict[str, list[str]] = {}
    for p in pages:
        if p.get("status") != 200:
            continue
        canon = (p.get("canonical") or "").strip()
        url = p.get("final_url") or p.get("url") or ""
        if not canon or same_url(canon, url):
            continue
        clusters.setdefault(canon.rstrip("/"), []).append(url)

    out: list[Finding] = []
    for canon, urls in clusters.items():
        if len(urls) < 3:
            continue
        out.append(Finding(
            key="canonical.mass_collapse",
            layer=LAYER_INDEXABILITY,
            severity=CRITICAL,
            effort=EFFORT_SMALL,
            title=f"{len(urls)} страниц канонизированы на один адрес",
            detail=(
                f"Целая группа страниц объявляет каноническим «{canon}». "
                "Фактически весь этот раздел просит поисковик себя не "
                "индексировать. Обычно результат неверной настройки шаблона."
            ),
            url=urls[0],
            evidence={"canonical": canon, "urls": urls[:15], "count": len(urls)},
            fixable=True,
        ))
    return out


def site_rule_sitemap(pages: list[dict[str, Any]], ctx: dict[str, Any]) -> list[Finding]:
    """Карта сайта: отсутствие, пустота, пропущенные разделы.

    Правило про пропущенные разделы выведено из climtec.md: в карте было
    26 адресов и НИ ОДНОГО товара — product-sitemap.xml отдавал 404.
    Метаданные товарам прописаны, а поисковику про них не сказано.
    """
    out: list[Finding] = []
    disc = ctx.get("discovery") or {}

    if not disc.get("robots_present"):
        out.append(Finding(
            key="robots.txt_missing",
            layer=LAYER_INDEXABILITY,
            severity=LOW,
            effort=EFFORT_SMALL,
            title="Нет файла robots.txt",
            detail=(
                "Файл не обязателен, но это стандартное место, где сообщают "
                "адрес карты сайта и закрывают служебные разделы."
            ),
            url=disc.get("origin", ""),
        ))

    if not disc.get("sitemaps_seen"):
        out.append(Finding(
            key="sitemap.missing",
            layer=LAYER_INDEXABILITY,
            severity=HIGH,
            effort=EFFORT_SMALL,
            title="Карта сайта не найдена",
            detail=(
                "Без карты поисковик находит страницы только по ссылкам — "
                "медленнее и не полностью. Для сайта с товарами или большим "
                "блогом это ощутимая потеря."
            ),
            url=disc.get("origin", ""),
        ))
        return out

    if not disc.get("robots_sitemaps"):
        out.append(Finding(
            key="sitemap.not_in_robots",
            layer=LAYER_INDEXABILITY,
            severity=LOW,
            effort=EFFORT_TRIVIAL,
            title="Карта сайта не указана в robots.txt",
            detail="Строка Sitemap: в robots.txt — стандартный способ её объявить.",
            url=disc.get("origin", ""),
        ))

    # Разделы, найденные по ссылкам, но отсутствующие в карте.
    in_sitemap: set[str] = set()
    seen_sections: dict[str, list[str]] = {}
    for p in pages:
        url = p.get("final_url") or p.get("url") or ""
        if not url:
            continue
        if p.get("source") == "sitemap":
            in_sitemap.add(url.rstrip("/"))
        parts = [x for x in urlsplit(url).path.split("/") if x]
        section = parts[0] if parts else ""
        # для многоязычных путей вида /ru/product/... берём осмысленный сегмент
        if section and len(section) <= 3 and len(parts) > 1:
            section = parts[1]
        if section:
            seen_sections.setdefault(section, []).append(url)

    for section, urls in seen_sections.items():
        if len(urls) < 3:
            continue
        covered = sum(1 for u in urls if u.rstrip("/") in in_sitemap)
        if covered == 0:
            out.append(Finding(
                key="sitemap.section_absent",
                layer=LAYER_INDEXABILITY,
                severity=HIGH,
                effort=EFFORT_SMALL,
                title=f"Раздел «{section}» ({len(urls)} стр.) отсутствует в карте сайта",
                detail=(
                    f"Найдено {len(urls)} страниц раздела «{section}», и ни одна "
                    "не заявлена в карте сайта. Поисковик узнает о них только "
                    "случайно, по ссылкам. Для товаров это прямая потеря продаж: "
                    "метаданные можно вылизать идеально, но если страницы не "
                    "заявлены — их не ищут."
                ),
                url=urls[0],
                evidence={"section": section, "count": len(urls), "urls": urls[:10]},
            ))
    return out


def site_rule_cache_masking(pages: list[dict[str, Any]],
                            ctx: dict[str, Any]) -> list[Finding]:
    """Кеш отдаёт устаревшую версию страницы.

    Найдено вживую на climtec.md: LiteSpeed отдавал копию со сроком 7 дней,
    в которой не было ни новых описаний, ни исправленного canonical. Проверка
    «в живом HTML» с добавленным параметром в адресе давала ПРОМАХ кеша и
    показывала свежую версию — то есть проверка обманывала.

    Для аудита 20-200 сайтов это системный риск: без этого правила движок
    будет уверенно отчитываться по данным, которых посетитель не видит.
    """
    out: list[Finding] = []
    hits = [p for p in pages if (p.get("cache_state") or "") == "hit"]
    stale = [p for p in pages if p.get("cache_stale")]

    if stale:
        out.append(Finding(
            key="cache.serving_stale",
            layer=LAYER_TECHNICAL,
            severity=HIGH,
            effort=EFFORT_TRIVIAL,
            title=f"Кеш отдаёт устаревшую версию ({len(stale)} стр.)",
            detail=(
                "Свежая версия страницы отличается от той, которую получает "
                "посетитель: содержимое обновили, а кеш продолжает отдавать "
                "старую копию. Правки метаданных не дойдут до поисковика, "
                "пока кеш не сброшен. Нужно очистить кеш сайта."
            ),
            url=(stale[0].get("final_url") or stale[0].get("url") or ""),
            evidence={
                "count": len(stale),
                "urls": [s.get("final_url") or s.get("url") for s in stale[:10]],
                "cache_layer": (stale[0].get("cache_layer") or ""),
            },
        ))
    elif hits and len(hits) == len([p for p in pages if p.get("status") == 200]):
        out.append(Finding(
            key="cache.fully_cached",
            layer=LAYER_TECHNICAL,
            severity=INFO,
            effort=EFFORT_TRIVIAL,
            title="Все страницы отдаются из кеша",
            detail=(
                "Само по себе хорошо для скорости. Важно помнить: после правок "
                "метаданных кеш нужно сбросить, иначе изменения не увидит ни "
                "посетитель, ни поисковик."
            ),
            url=(hits[0].get("final_url") or hits[0].get("url") or ""),
            evidence={"cached_pages": len(hits)},
        ))
    return out


def site_rule_https_mixed(pages: list[dict[str, Any]], ctx: dict[str, Any]) -> list[Finding]:
    """Часть сайта на http, часть на https."""
    http_pages = [p for p in pages
                  if (p.get("final_url") or "").startswith("http://")]
    https_pages = [p for p in pages
                   if (p.get("final_url") or "").startswith("https://")]
    if http_pages and https_pages:
        return [Finding(
            key="security.mixed_scheme",
            layer=LAYER_TECHNICAL,
            severity=HIGH,
            effort=EFFORT_MEDIUM,
            title=f"Часть страниц без HTTPS ({len(http_pages)} шт.)",
            detail=(
                "Сайт доступен и по http, и по https. Это раздваивает адреса "
                "в индексе и показывает посетителям предупреждение о "
                "небезопасном соединении."
            ),
            url=(http_pages[0].get("final_url") or ""),
            evidence={"http_count": len(http_pages)},
        )]
    return []


# ══════════════════════════════════════════════════════════════════════════
# РЕЕСТР
# ══════════════════════════════════════════════════════════════════════════

PAGE_RULES: tuple[Callable[..., list[Finding]], ...] = (
    rule_status_and_redirects,
    rule_https,
    rule_canonical,
    rule_noindex,
    rule_title,
    rule_description,
    rule_h1,
    rule_thin_content,
    rule_images_alt,
    rule_performance,
    rule_lang_declared,
    rule_lang_mismatch,
    rule_hreflang,
)

SITE_RULES: tuple[Callable[..., list[Finding]], ...] = (
    site_rule_sitemap,
    site_rule_duplicate_meta,
    site_rule_canonical_clusters,
    site_rule_cache_masking,
    site_rule_https_mixed,
)


def all_rule_keys() -> list[str]:
    """Все известные ключи правил — для документации и настроек."""
    return sorted({
        "canonical.missing", "canonical.duplicate_tag", "canonical.cross_host",
        "canonical.cross_language", "canonical.points_elsewhere",
        "canonical.mass_collapse",
        "robots.noindex", "robots.noindex_in_sitemap", "robots.txt_missing",
        "http.unreachable", "http.server_error", "http.client_error",
        "http.404_in_sitemap", "http.redirect_chain",
        "sitemap.missing", "sitemap.not_in_robots", "sitemap.redirecting_url",
        "sitemap.section_absent",
        "security.no_https", "security.mixed_scheme",
        "title.missing", "title.too_short", "title.too_long",
        "title.duplicate_tag",
        "description.missing", "description.too_short", "description.too_long",
        "description.duplicate_tag",
        "h1.missing", "h1.multiple",
        "content.thin", "images.missing_alt",
        "performance.slow", "performance.very_slow",
        "i18n.lang_missing", "i18n.title_language_mismatch",
        "i18n.description_language_mismatch",
        "i18n.hreflang_no_self", "i18n.hreflang_lang_absent",
        "duplicate.title", "duplicate.description",
        "cache.serving_stale", "cache.fully_cached",
    })


# ══════════════════════════════════════════════════════════════════════════
# ЕДИНАЯ ТОЧКА ПРОГОНА
# ══════════════════════════════════════════════════════════════════════════
# Одно правило не должно рушить весь аудит: на 200 сайтах встретится любая
# экзотика в разметке. Поэтому исключение внутри правила глушится, а не
# всплывает наружу.

def page_was_fetched(item: dict[str, Any]) -> bool:
    """Была ли страница реально загружена.

    Аудит имеет право судить только то, что видел. Запись в состоянии
    queued (ничего не скачано) — не «страница без заголовка», а страница
    с неизвестным содержимым. Разница принципиальная: в первом случае мы
    создаём людям работу по выдуманному дефекту.
    """
    if item.get("state") in ("queued", "pending", ""):
        return False
    if item.get("state") == "error" or item.get("error"):
        return True   # ошибку загрузки судить МОЖНО — это и есть находка
    return item.get("status") is not None


# Правила, которым нужен ответ сервера, а не содержимое страницы.
_RULES_WITHOUT_CONTENT = {"rule_status_and_redirects", "rule_https", "rule_cache"}


def run_page_rules(item: dict[str, Any], head: HeadData,
                   ctx: dict[str, Any] | None = None) -> list[Finding]:
    """Прогнать все постраничные правила по одной странице."""
    ctx = ctx or {}
    out: list[Finding] = []
    fetched = page_was_fetched(item)
    for rule in PAGE_RULES:
        # По незагруженной странице разрешены только правила про сам ответ:
        # претензии к её содержанию были бы приговором по неувиденному.
        if not fetched and getattr(rule, "__name__", "") not in _RULES_WITHOUT_CONTENT:
            continue
        try:
            out.extend(rule(item, head, ctx))
        except Exception:
            continue
    return out


def run_site_rules(pages: list[dict[str, Any]],
                   ctx: dict[str, Any] | None = None) -> list[Finding]:
    """Прогнать правила уровня сайта по всему корпусу страниц."""
    ctx = ctx or {}
    out: list[Finding] = []
    for rule in SITE_RULES:
        try:
            out.extend(rule(pages, ctx))
        except Exception:
            continue
    return out
