"""Параметры инструментов и сущности результата.

ИМЕНА, А НЕ НОМЕРА. Человек говорит «проверь climtec.md», а не «run_id=7».
Поэтому инструменты принимают домены и обычные слова, а `run_id` необязателен:
пусто = последний прогон. Иначе первый же вопрос «покажи находки» требовал бы
сначала где-то найти номер.

ПОРОГИ ВАЖНОСТИ ПРОВЕРЯЮТСЯ. Движок знает ровно пять уровней. Опечатка
«critcal» не должна молча отфильтровать всё и показать пустой список — модель
превращает её в понятный отказ ДО запуска аудита.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from imperal_sdk import sdl

# Уровни движка. Держим литералами, а не импортом из seoaudit.severity, чтобы
# модели грузились и проверялись независимо от движка.
SEVERITIES = ("critical", "high", "medium", "low", "info")


def _check_severity(v: str) -> str:
    """Общая проверка порога: пусто → medium, мусор → внятная ошибка."""
    low = (v or "").strip().lower()
    if low and low not in SEVERITIES:
        raise ValueError(
            f"неизвестный уровень важности '{v}'. "
            f"Допустимо: {', '.join(SEVERITIES)}"
        )
    return low or "medium"


# --------------------------- параметры ---------------------------

class AuditSitesParams(BaseModel):
    """Что проверять и насколько глубоко."""

    sites: str = Field(
        "",
        description=("Домены через запятую или с новой строки, например "
                     "'climtec.md, ksrenovationgroup.com'. Схему писать не нужно."),
    )
    label: str = Field(
        "", description="Название прогона, например 'июль, портфель агентства'")
    max_pages: int = Field(
        50, ge=1, le=500,
        description=("Сколько страниц смотреть на каждом сайте. 50 хватает, "
                     "чтобы увидеть системные дефекты."),
    )
    site_workers: int = Field(
        4, ge=1, le=16, description="Сколько САЙТОВ обходить одновременно")
    page_workers: int = Field(
        4, ge=1, le=8,
        description=("Сколько страниц одновременно внутри ОДНОГО сайта. "
                     "Держите небольшим: это нагрузка на чужой сервер."),
    )

    @field_validator("sites")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()


class ResumeAuditParams(BaseModel):
    """Продолжение прерванного прогона.

    Отдельная модель, а не поле в AuditSitesParams: продолжение НЕ принимает
    список сайтов — он уже записан в прогоне, и передать другой значило бы
    молча подменить предмет работы.
    """

    run_id: int = Field(
        0, ge=0,
        description="Номер прогона. 0 или пусто — последний незавершённый.")


class RunScoped(BaseModel):
    """База для чтения результатов: какой прогон смотрим."""

    run_id: int = Field(
        0, ge=0, description="Номер прогона. 0 или пусто — последний прогон.")


class ListRunsParams(BaseModel):
    limit: int = Field(10, ge=1, le=50, description="Сколько прогонов вернуть")


class ListConnectedParams(BaseModel):
    """Что показать в списке подключённых сайтов.

    Есть `query` и постраничность, потому что портфель бывает на сотни доменов:
    вываливать их в чат одним куском бессмысленно — читать это невозможно, а
    ответ раздувается. По умолчанию отдаём первую страницу и говорим, сколько
    всего.
    """

    query: str = Field(
        "", description="Часть домена для поиска, например 'climtec'. "
                        "Пусто — все сайты.")
    limit: int = Field(
        50, ge=1, le=200, description="Сколько сайтов показать за раз")
    offset: int = Field(
        0, ge=0, description="Сколько сайтов пропустить — для следующей страницы")


class ListFindingsParams(RunScoped):
    site: str = Field(
        "", description="Домен, например 'climtec.md'. Пусто — весь портфель.")
    min_severity: str = Field(
        "medium",
        description=("Порог важности: critical, high, medium, low, info. "
                     "Возвращаются находки этого уровня и выше."),
    )
    limit: int = Field(50, ge=1, le=200, description="Сколько находок вернуть")

    @field_validator("min_severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        return _check_severity(v)


class ListTasksParams(RunScoped):
    site: str = Field("", description="Домен. Пусто — задачи по всему портфелю.")
    min_severity: str = Field(
        "medium", description="Порог важности: critical, high, medium, low, info.")
    limit: int = Field(50, ge=1, le=200, description="Сколько задач вернуть")

    @field_validator("min_severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        return _check_severity(v)


class GetReportParams(RunScoped):
    site: str = Field(
        "",
        description=("Домен для отчёта по одному сайту. Пусто — сводный отчёт "
                     "по всему портфелю."),
    )
    min_severity: str = Field(
        "medium", description="Порог важности для задач в отчёте.")

    @field_validator("min_severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        return _check_severity(v)


class ExportPlanParams(RunScoped):
    site: str = Field("", description="Домен. Пусто — весь портфель.")
    project: str = Field(
        "SEO", description="Название проекта в трекере, например 'SEO climtec'")
    assignee: str = Field(
        "", description="Кому назначить задачи (имя в трекере). Можно не указывать.")
    min_severity: str = Field(
        "medium", description="Порог важности: какие задачи попадут в план.")

    @field_validator("min_severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        return _check_severity(v)


class GetScheduleParams(BaseModel):
    """Ничего не принимает — модель есть, потому что её требует контракт.

    Инструмент без параметров всё равно обязан объявить схему: платформа
    выводит форму вызова из модели, и `params: None` оставил бы её пустой не
    «намеренно», а «неизвестно».
    """


class ScheduleParams(BaseModel):
    """Что поменять в расписании. Не переданное — не трогаем.

    `enabled` именно Optional[bool], а не bool: у булева поля со значением по
    умолчанию нельзя отличить «выключи» от «не трогай». С обычным bool просьба
    «перенеси на 4 утра» тихо выключила бы расписание — правка одного поля
    сломала бы другое.
    """

    enabled: bool | None = Field(
        None, description="Включить (true) или выключить (false) автоаудит.")
    hour: int | None = Field(
        None, ge=0, le=23,
        description="Час запуска по UTC, 0-23. Ночь спокойнее для чужих сайтов.")
    days: str = Field(
        "", description=("Дни недели через запятую: 1=понедельник … 7=воскресенье. "
                         "Например '1' или '1,4'. Пусто — не менять."))
    sites: str = Field(
        "", description=("Домены через запятую. Пусто — проверять те же сайты, "
                         "что и в прошлый раз."))
    max_pages: int | None = Field(
        None, ge=1, le=500, description="Сколько страниц смотреть на каждом сайте.")


class CompareParams(BaseModel):
    """Что с чем сравнивать."""

    site: str = Field(
        ..., description="Домен, например 'climtec.md'. Сравнение всегда по одному сайту.")
    after_run: int = Field(
        0, ge=0,
        description="Свежий прогон. 0 или пусто — последний прогон этого сайта.")
    before_run: int = Field(
        0, ge=0,
        description="С чем сравнивать. 0 или пусто — предыдущий прогон этого сайта.")


class FixPlanParams(RunScoped):
    """Какие правки показать и насколько готовые."""

    site: str = Field(
        "", description="Домен, например 'climtec.md'. Пусто — весь портфель.")
    only_ready: bool = Field(
        False,
        description=("Только те правки, где значение выведено однозначно и "
                     "человеку нечего решать."))
    limit: int = Field(
        100, ge=1, le=500, description="Сколько правок вернуть")


# --------------------------- сущности результата ---------------------------

class RunStarted(sdl.Entity):
    """Подтверждение запуска: что именно началось и сколько это займёт.

    Оценка времени здесь не украшение: аудит идёт минуты или десятки минут, и
    без неё пользователь не понимает, ждать ему или заняться другим.
    """

    run_id: int = 0
    sites_count: int = 0
    sites: list[str] = []
    max_pages: int = 0
    estimate: str = ""
    resumed: bool = False


class RunSummary(sdl.Entity):
    """Итог прогона: оценка портфеля, задачи, что упало."""

    run_id: int = 0
    label: str = ""
    sites_total: int = 0
    sites_done: int = 0
    sites_failed: int = 0
    pages_checked: int = 0
    findings_total: int = 0
    tasks_total: int = 0
    critical: int = 0
    high: int = 0
    worst_site: str = ""
    worst_score: int = 0
    finished: bool = False


class ConnectedSite(sdl.Entity):
    """Подключённый сайт — строка списка портфеля.

    Отличается от SiteScore намеренно: тот показывает ОЦЕНКУ сайта в конкретном
    прогоне, а этот отвечает на вопрос «какие сайты у меня вообще подключены» —
    по всем прогонам, с датой последней проверки.
    """

    origin: str = ""
    host: str = ""
    state: str = ""
    state_label: str = ""
    pages: int = 0
    runs: int = 0
    last_checked: str = ""
    failure: str = ""


class SiteScore(sdl.Entity):
    """Одна строка портфеля — сайт и его состояние."""

    origin: str = ""
    score: int = 0
    pages: int = 0
    tasks: int = 0
    top_issue: str = ""
    state: str = ""
    failure: str = ""


class Finding(sdl.Entity):
    """Одна находка: что не так, где и насколько это важно."""

    rule: str = ""
    severity: str = ""
    layer: int = 0
    layer_name: str = ""
    site: str = ""
    url: str = ""
    message: str = ""
    detail: str = ""


class AuditTask(sdl.Entity):
    """Одна работа = один дефект на одном сайте, со списком страниц внутри."""

    site: str = ""
    rule: str = ""
    task_title: str = ""
    body: str = ""
    severity: str = ""
    layer_name: str = ""
    pages: int = 0
    urls: list[str] = []
    due_days: int = 30
    tags: list[str] = []
    autofixable: bool = False
    fingerprint: str = ""


class Report(sdl.Entity):
    """Готовый отчёт в Markdown — по сайту или по портфелю."""

    scope: str = ""
    markdown: str = ""
    sites_count: int = 0
    tasks_total: int = 0


class ScheduleState(sdl.Entity):
    """Как настроен автоматический аудит."""

    enabled: bool = False
    hour: int = 3
    days: str = ""
    days_label: str = ""
    sites: str = ""
    max_pages: int = 50
    last_run_id: int = 0


class AuditComparison(sdl.Entity):
    """Что изменилось между двумя прогонами одного сайта.

    Отдельная сущность, а не поле в отчёте: главный её смысл — ПОЯВИВШИЕСЯ
    находки. В общем списке регрессия неотличима от старой беды, и заметить
    её можно только сравнением.
    """

    site: str = ""
    before_run: int = 0
    after_run: int = 0
    before_score: int = 0
    after_score: int = 0
    score_delta: int = 0
    fixed_count: int = 0
    remains_count: int = 0
    appeared_count: int = 0
    reliable: bool = True
    caveat: str = ""
    fixed: list[dict] = []
    appeared: list[dict] = []
    remains: list[dict] = []


class FixPlan(sdl.Entity):
    """План правок: конкретные значения полей, а не описание беды.

    Почему план, а не применение: аудит ничего не меняет на чужих сайтах —
    точно так же, как не создаёт задач в чужом трекере. Правки применяет
    коннектор сайта по подтверждению.
    """

    total: int = 0
    ready: int = 0
    needs_review: int = 0
    pages: int = 0
    by_field: dict = {}
    fixes: list[dict] = []


class ExportPlan(sdl.Entity):
    """План выгрузки в трекер: готовые аргументы для создания задач.

    Отдаём именно план, а не создаём задачи сами: аудит не должен уметь писать
    в чужой трекер. План передаётся коннектору (Asana/Notion) по подтверждению.
    """

    tasks_total: int = 0
    sites_count: int = 0
    autofixable: int = 0
    pages_touched: int = 0
    by_section: dict = {}
    entries: list[dict] = []
