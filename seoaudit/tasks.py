"""Превращение находок в РАБОТУ — задачи, которые кто-то реально сделает.

Главное проектное решение — ГРУППИРОВКА.

Наивный путь: одна находка = одна задача. На 200 сайтах по ~30 находок это
6000 задач, которые никто не откроет. Такой трекер мёртв в день создания.

Здесь задача = (сайт × правило). «Заполнить описания на 12 страницах сайта X»
— одна задача со списком URL внутри. Исполнитель делает её одним заходом,
потому что это одна и та же операция, повторённая 12 раз.

Второе решение — ПОРОГ. Находки уровня info и часть low не создают задач
вообще: они остаются в отчёте как наблюдения. Задача создаётся тогда, когда
за ней стоит работа, которую стоит делать.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .severity import (
    CRITICAL,
    EFFORT_NAMES,
    HIGH,
    INFO,
    LAYER_NAMES,
    LOW,
    MEDIUM,
    SEVERITY_ORDER,
)

# Сколько URL показывать в тексте задачи. Остальные — в приложении к задаче,
# иначе описание превращается в нечитаемую простыню.
URLS_IN_BODY = 15

# Порог создания задач: low и info по умолчанию НЕ создают работу.
DEFAULT_MIN_SEVERITY = MEDIUM

# Как срочно браться. Агентству нужен не «приоритет 1-5», а понятный срок.
DUE_DAYS = {
    CRITICAL: 2,
    HIGH: 7,
    MEDIUM: 30,
    LOW: 90,
}

# Человеческие инструкции: ЧТО сделать. Ключ — правило.
# Текст пишется для исполнителя, который не читал отчёт.
PLAYBOOK: dict[str, str] = {
    "canonical.cross_language": (
        "Прописать каждой странице канонический адрес, равный её СОБСТВЕННОМУ "
        "URL. Сейчас страница указывает на версию на другом языке, то есть сама "
        "просит поисковик её не индексировать. Проверить, что hreflang-связки "
        "остались нетронутыми."
    ),
    "canonical.cross_host": (
        "Канонический адрес ведёт на другой домен — страница отдаёт свой вес "
        "чужому сайту. Заменить на собственный адрес страницы."
    ),
    "canonical.missing": (
        "Задать канонический адрес. Если SEO-плагин уже выводит корректную "
        "самоссылку, поле можно оставить пустым — но проверить это в исходном "
        "коде страницы, а не по настройкам плагина."
    ),
    "canonical.duplicate_tag": (
        "На странице больше одного тега canonical — поисковик выберет любой. "
        "Обычно это конфликт темы и SEO-плагина: убрать вывод из темы."
    ),
    "canonical.points_elsewhere": (
        "Канонический адрес ведёт на другую страницу этого же сайта. Убедиться, "
        "что это осознанное склеивание дублей, а не ошибка."
    ),
    "canonical.mass_collapse": (
        "Много страниц указывают каноническим адресом одну и ту же страницу. "
        "Так теряется весь раздел. Найти общую причину — обычно шаблон или "
        "настройка плагина."
    ),
    "robots.noindex": (
        "Страница закрыта от индексации. Проверить, что это намеренно."
    ),
    "robots.noindex_in_sitemap": (
        "Страница закрыта от индексации, но заявлена в карте сайта — "
        "противоречивый сигнал. Либо убрать из карты, либо открыть индексацию."
    ),
    "robots.txt_missing": (
        "Добавить robots.txt и указать в нём адрес карты сайта."
    ),
    "sitemap.missing": (
        "Создать карту сайта и объявить её в robots.txt."
    ),
    "sitemap.not_in_robots": (
        "Дописать строку Sitemap: <адрес> в robots.txt — так поисковик найдёт "
        "карту сразу."
    ),
    "sitemap.section_absent": (
        "Раздел сайта существует, но его страниц нет в карте сайта. Включить "
        "тип записей в карту (в WordPress — в настройках SEO-плагина)."
    ),
    "sitemap.redirecting_url": (
        "В карте сайта указаны адреса, которые перенаправляют на другие. "
        "В карте должны быть только конечные адреса."
    ),
    "http.client_error": (
        "Страница отдаёт ошибку 4xx. Либо восстановить, либо поставить редирект "
        "на близкую по смыслу страницу, либо убрать ссылки на неё."
    ),
    "http.server_error": (
        "Страница отдаёт ошибку сервера 5xx — разобраться с причиной на сервере."
    ),
    "http.404_in_sitemap": (
        "В карте сайта заявлены несуществующие страницы. Убрать их из карты."
    ),
    "http.unreachable": (
        "Страница недоступна. Проверить доступность сервера и сам адрес."
    ),
    "http.redirect_chain": (
        "Цепочка редиректов длиннее одного шага. Сократить до одного перехода."
    ),
    "security.no_https": (
        "Сайт доступен без HTTPS. Выпустить сертификат и поставить постоянный "
        "редирект с http на https."
    ),
    "cache.serving_stale": (
        "ВАЖНО: кеш отдаёт посетителям и поисковику УСТАРЕВШУЮ версию страниц. "
        "Правки в админке уже сохранены, но наружу не попадают. Сбросить кеш "
        "и проверить срок его жизни. До сброса любые проверки метаданных будут "
        "показывать старые данные и вводить в заблуждение."
    ),
    "title.missing": "Написать заголовок страницы (30-60 символов).",
    "title.too_short": (
        "Расширить заголовок до 30-60 символов, добавив уточнение: для кого, "
        "где, какая выгода."
    ),
    "title.too_long": (
        "Сократить заголовок до 60 символов, оставив главное в начале."
    ),
    "title.duplicate_tag": (
        "На странице два тега title — убрать вывод из темы, оставить плагин."
    ),
    "description.missing": "Написать описание страницы (120-160 символов).",
    "description.too_short": "Расширить описание до 120-160 символов.",
    "description.too_long": "Сократить описание до 160 символов.",
    "duplicate.title": (
        "Одинаковые заголовки на разных страницах — они конкурируют между собой. "
        "Сделать каждый заголовок уникальным: одна страница = одно намерение."
    ),
    "duplicate.description": (
        "Одинаковые описания на разных страницах — переписать под содержание "
        "каждой."
    ),
    "h1.missing": (
        "На странице нет заголовка H1. Проверить шаблон: часто H1 теряется "
        "именно в шаблоне, а не в конкретной записи."
    ),
    "h1.multiple": "Оставить один H1 на странице, остальные понизить до H2.",
    "content.thin": (
        "Мало содержания. Либо дописать по существу, либо объединить с другой "
        "страницей. НЕ добавлять текст ради объёма."
    ),
    "images.missing_alt": (
        "Заполнить alt у изображений — описанием того, что на картинке."
    ),
    "i18n.lang_missing": (
        "Не указан язык страницы. Задать атрибут lang у тега html."
    ),
    "i18n.title_language_mismatch": (
        "Заголовок написан НЕ на языке страницы. Перевести на язык страницы."
    ),
    "i18n.description_language_mismatch": (
        "Описание написано не на языке страницы. Перевести."
    ),
    "i18n.hreflang_missing_return": (
        "Языковые версии ссылаются друг на друга не взаимно. Прописать обратные "
        "hreflang-связки."
    ),
    "performance.slow": (
        "Страница отвечает медленно. Проверить кеширование и вес страницы."
    ),
    "performance.very_slow": (
        "Страница отвечает очень медленно — разобраться с сервером и кешем."
    ),
    "structured_data.missing": (
        "Добавить микроразметку, подходящую типу страницы."
    ),
}

# Правила, которые я умею починить автоматически через WP-коннектор.
# ВАЖНО: сюда попадает только то, где правка механическая и проверяемая.
# Тексты (заголовки, описания) сюда НЕ входят: их надо писать по содержанию
# страницы, а выдуманный текст хуже отсутствующего.
AUTOFIXABLE = {
    "canonical.cross_language",
    "canonical.cross_host",
    "canonical.missing",
}

# Правила, у которых КАЖДЫЙ предмет — отдельная задача.
#
# Критерий: предмет является самостоятельным объектом работы, и правки по
# разным предметам делаются в разных местах либо разными людьми.
#
#   sitemap.section_absent — каждый раздел включается в карту отдельно;
#   duplicate.title/description — каждая пара дублей требует своего решения:
#       какой из двух текстов переписать, а какой оставить.
#
# Всё остальное группируется в ОДНУ задачу на сайт: «нет H1 на 8 страницах»
# — это одна работа со списком страниц внутри, а не 8 карточек в трекере.
SPLIT_BY_SUBJECT = {
    "sitemap.section_absent",
    "duplicate.title",
    "duplicate.description",
}


@dataclass
class Task:
    """Одна задача = одна операция, повторённая на N страницах одного сайта."""

    site: str
    rule: str
    layer: int
    severity: str
    effort: int
    title: str
    body: str
    urls: list[str] = field(default_factory=list)
    count: int = 0
    due_days: int = 30
    tags: list[str] = field(default_factory=list)
    autofixable: bool = False
    # Предмет внутри правила: раздел, значение, язык. Поле со значением по
    # умолчанию обязано идти ПОСЛЕ обязательных — иначе dataclass не собрать.
    subject: str = ""

    @property
    def fingerprint(self) -> str:
        """Устойчивый идентификатор задачи: сайт + правило.

        Аудит на 200 сайтах запускают повторно — раз в неделю, раз в месяц.
        Без отпечатка каждый прогон создавал бы копии тех же задач, и трекер
        превратился бы в свалку. По отпечатку экспортёр находит уже
        существующую задачу и обновляет её вместо создания новой.

        Намеренно НЕ включает список URL и их количество: набор страниц
        меняется от прогона к прогону, а задача остаётся той же самой
        («починить H1 на этом сайте»).

        Предмет (subject) в отпечаток входит обязательно: без него «раздел
        inspiration не в карте сайта» и «раздел project не в карте сайта»
        получили бы ОДНУ метку и в трекере затирали бы друг друга.
        """
        raw = f"{self.site}|{self.rule}|{self.subject}"
        return "seoaudit-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "site": self.site,
            "rule": self.rule,
            "subject": self.subject,
            "layer": self.layer,
            "severity": self.severity,
            "effort": self.effort,
            "title": self.title,
            "body": self.body,
            "urls": self.urls,
            "count": self.count,
            "due_days": self.due_days,
            "tags": self.tags,
            "autofixable": self.autofixable,
        }


def _host(origin: str) -> str:
    h = urlsplit(origin).netloc or origin
    return h[4:] if h.startswith("www.") else h


def _subject_of(f: dict[str, Any]) -> str:
    """Предмет находки внутри правила — то, что делает её отдельной задачей.

    Дробить нужно ИЗБИРАТЕЛЬНО, и граница здесь такая: предмет — это
    отдельный ОБЪЕКТ РАБОТЫ, а не просто содержимое очередной страницы.

    Дробим: «раздел inspiration не в карте сайта» и «раздел project» —
    два разных действия в настройках, две задачи.

    НЕ дробим: «заголовок не на языке страницы» на 8 страницах — это одна
    работа корректора, и 8 отдельных карточек только засорят трекер.
    Сначала я дробила по любому `value` и получила две одинаковые задачи
    «Заголовок не на языке страницы», различавшиеся лишь текстом внутри.
    """
    if f.get("rule") not in SPLIT_BY_SUBJECT:
        return ""
    ev = f.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except (ValueError, TypeError):
            ev = {}
    if not isinstance(ev, dict):
        return ""
    for key in ("section", "post_type", "cluster", "value"):
        v = ev.get(key)
        if v:
            return f"{key}={str(v)[:80]}"
    return ""


def _severity_word(sev: str) -> str:
    return {
        CRITICAL: "критично",
        HIGH: "важно",
        MEDIUM: "средне",
        LOW: "мелочь",
        INFO: "наблюдение",
    }.get(sev, sev)


def build_tasks(
    site_origin: str,
    findings: list[dict[str, Any]],
    *,
    min_severity: str = DEFAULT_MIN_SEVERITY,
    include_rules: set[str] | None = None,
) -> list[Task]:
    """Сгруппировать находки одного сайта в задачи.

    findings — строки из БД (dict-like) с полями rule, severity, layer,
    effort, url, message, detail.
    """
    limit = SEVERITY_ORDER.get(min_severity, 2)
    host = _host(site_origin)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in findings:
        rule = f["rule"]
        if include_rules is not None and rule not in include_rules:
            continue
        if SEVERITY_ORDER.get(f["severity"], 9) > limit:
            continue
        # Группируем по правилу И ПРЕДМЕТУ находки. Иначе «раздел inspiration
        # (6 стр.) не в карте сайта» и «раздел project (4 стр.)» склеивались в
        # ОДНУ задачу: заголовок от первой, ссылки от обеих — исполнитель
        # половину работы просто не видел.
        groups.setdefault((rule, _subject_of(f)), []).append(f)

    tasks: list[Task] = []
    for (rule, subject), items in groups.items():
        # Внутри группы берём САМУЮ высокую серьёзность и самую дорогую оценку
        # труда: задача не может быть проще своей тяжелейшей части.
        worst = min(items, key=lambda x: SEVERITY_ORDER.get(x["severity"], 9))
        sev = worst["severity"]
        layer = int(worst["layer"])
        effort = max(int(i.get("effort", 2)) for i in items)

        # Правила уровня САЙТА кладут полный перечень страниц в evidence.urls,
        # а в поле url оставляют лишь один адрес как пример. Если читать
        # только url, задача скажет «10 стр.» и покажет одну — исполнитель не
        # узнает, где остальные девять. Поэтому берём оба источника.
        urls: list[str] = []

        def _add(u: Any) -> None:
            u = (str(u) or "").strip()
            if u and u not in urls:
                urls.append(u)

        for i in items:
            ev = i.get("evidence")
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except (ValueError, TypeError):
                    ev = {}
            if isinstance(ev, dict):
                for u in ev.get("urls") or []:
                    _add(u)
            _add(i.get("url"))

        n = len(urls) or len(items)
        head = worst.get("message") or rule
        # Правило могло уже вписать объём в текст («… (10 стр.)») — второй раз
        # его добавлять не нужно, иначе заголовок начинает противоречить себе.
        already_counted = "стр.)" in head
        if n > 1 and not already_counted:
            title = f"[{host}] {head} — {n} стр."
        else:
            title = f"[{host}] {head}"

        what = PLAYBOOK.get(rule, worst.get("detail") or "Разобраться и исправить.")
        parts = [
            f"Сайт: {site_origin}",
            f"Слой: {LAYER_NAMES.get(layer, layer)} · Важность: {_severity_word(sev)}"
            f" · Объём работы: {EFFORT_NAMES.get(effort, effort)}"
            # «Объём работы» — это цена правки. Отдельно говорим, может ли её
            # внести наш WP-коннектор: раньше строка «Трудозатраты:
            # автоматически» читалась как «оно само починится», хотя, например,
            # сброс кеша коннектор не делает — человек шёл с ложным ожиданием.
            + (" · правится инструментом Imperal"
               if rule in AUTOFIXABLE else " · правится вручную"),
            "",
            "ЧТО СДЕЛАТЬ",
            what,
            "",
            "ПОЧЕМУ ЭТО ВАЖНО",
            (worst.get("detail") or "").strip() or "—",
        ]

        if urls:
            parts += ["", f"ГДЕ ({n})"]
            parts += [f"  • {u}" for u in urls[:URLS_IN_BODY]]
            if n > URLS_IN_BODY:
                parts.append(f"  … и ещё {n - URLS_IN_BODY} — полный список в отчёте")

        parts += [
            "",
            "КАК ПРОВЕРИТЬ",
            "Открыть исходный код страницы и убедиться, что правка видна "
            "ПОСЕТИТЕЛЮ, а не только в админке. Если на сайте есть кеш — "
            "сначала сбросить его.",
        ]

        task = Task(
            site=site_origin,
            rule=rule,
            subject=subject,
            layer=layer,
            severity=sev,
            effort=effort,
            title=title,
            body="\n".join(parts),
            urls=urls,
            count=n,
            due_days=DUE_DAYS.get(sev, 30),
            tags=[f"seo:{LAYER_NAMES.get(layer, layer).lower()}", f"seo:{sev}", host],
            autofixable=rule in AUTOFIXABLE,
        )
        # Метка в самом тексте задачи: по ней экспортёр опознаёт СВОЮ задачу в
        # трекере при повторном аудите и обновляет её вместо создания дубля.
        # Держим именно в теле, а не только в поле JSON: после выгрузки
        # единственный носитель смысла — сама карточка в трекере.
        task.body = task.body + f"\n\n—\nМетка аудита: {task.fingerprint}"
        tasks.append(task)

    # Порядок = порядок работы. Серьёзность ПЕРВЕЕ слоя: критичное и важное
    # должно быть сверху, даже если относится к «низкому» слою. Иначе high-
    # задача уезжает под шесть средних и агентство берётся не за то.
    # Слой остаётся вторым ключом — он разводит равные по важности задачи.
    tasks.sort(key=lambda t: (
        SEVERITY_ORDER.get(t.severity, 9),
        t.layer,
        -t.count,
    ))
    return tasks


def summarise_tasks(tasks: list[Task]) -> dict[str, Any]:
    """Сводка для отчёта агентству."""
    by_sev: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    pages = 0
    auto = 0
    for t in tasks:
        by_sev[t.severity] = by_sev.get(t.severity, 0) + 1
        ln = LAYER_NAMES.get(t.layer, str(t.layer))
        by_layer[ln] = by_layer.get(ln, 0) + 1
        pages += t.count
        if t.autofixable:
            auto += 1
    return {
        "tasks": len(tasks),
        "pages_touched": pages,
        "autofixable_tasks": auto,
        "by_severity": by_sev,
        "by_layer": by_layer,
    }
