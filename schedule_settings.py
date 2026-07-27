"""Когда аудит запускается сам — как НАСТРОЙКА, а не как константа.

ПОЧЕМУ ТИК ПЛЮС СОХРАНЁННОЕ ВРЕМЯ, А НЕ ПРОСТО СТРОКА CRON
-----------------------------------------------------------
Очевидное решение — «пусть человек правит строку расписания» — не работает:
платформа читает `@ext.schedule(cron=...)` при РЕГИСТРАЦИИ приложения, то есть
строка фиксируется на момент выкладки. Менять расписание так значит править
исходник и передеплоивать — ровно то, чего этот модуль позволяет избежать.

Поэтому расписание становится БУДИЛЬНИКОМ: срабатывает часто, спрашивает у
этого модуля «уже пора?» и молча уходит, когда нет. Выбранное человеком время
живёт в хранилище и меняется в любой момент без выкладки.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ ОПРОСА ЧАТА
----------------------------------
Аудит — не опрос. Один прогон идёт минуты, а по портфелю — десятки минут, и всё
это время он ходит по ЧУЖИМ серверам. Поэтому здесь настраивается не «раз в N
минут», а ЧАС СУТОК и ДНИ НЕДЕЛИ: работа тяжёлая, её место — ночь, когда на
сайтах клиентов нет посетителей.

Отсюда же тик в час, а не в пять минут: точность до часа — всё, что нужно
ночному прогону, а более частый тик означал бы двенадцать лишних пробуждений
в час ради ответа «ещё не пора».

ПОЧЕМУ ЗАЩИТА ОТ ПОВТОРА — ПО ДАТЕ, А НЕ ПО ИНТЕРВАЛУ
------------------------------------------------------
«Прошло ли 24 часа» — неверный вопрос для ежедневного запуска в 3 ночи: прогон,
затянувшийся до 4:10, сдвинет следующий на 4:10, потом на 5:20, и через неделю
ночной аудит поедет в утренний час пик. Поэтому запоминается ДАТА последнего
запуска: сегодня уже был — сегодня больше не запускаем, независимо от того,
сколько он длился.
"""

from __future__ import annotations

import time
from typing import Any

#: Где живёт настройка. Отдельная коллекция: это то, что правит человек, а не
#: то, что машина переписывает при каждом прогоне.
SETTINGS_COLLECTION = "seo_schedule"
SETTINGS_KEY = "schedule"

#: Расписание платформы — БУДИЛЬНИК, а не аудит. Срабатывает в начале каждого
#: часа; каждое срабатывание спрашивает due(), пора ли.
TICK_CRON = "5 * * * *"

#: Держится рядом со строкой, которую описывает, чтобы они не разошлись.
TICK_MINUTES = 60

#: Дни недели в том виде, в каком их называет человек.
DAY_NAMES = {
    0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
    4: "пятница", 5: "суббота", 6: "воскресенье",
}

#: Те же дни в форме для «по …»: «по понедельникам», а не «по понедельник».
#: Отдельный список, а не дописывание окончания: у среды и субботы меняется
#: не только хвост, и правило «прибавить -ам» сломалось бы на них.
DAY_NAMES_BY = {
    0: "понедельникам", 1: "вторникам", 2: "средам", 3: "четвергам",
    4: "пятницам", 5: "субботам", 6: "воскресеньям",
}

DEFAULT_HOUR = 3          # ночь: на клиентских сайтах нет посетителей
DEFAULT_DAYS = "1"        # раз в неделю, по понедельникам
MAX_SITES_PER_RUN = 25    # потолок автопрогона — см. ниже


def _now_parts(ts: float | None = None) -> tuple[str, int, int]:
    """(дата ГГГГ-ММ-ДД, час 0-23, день недели 0-6) по UTC.

    UTC, а не местное время: у платформы нет часового пояса пользователя, а
    молча подставить свой значило бы, что «в три ночи» окажется тремя часами
    дня для половины портфеля.
    """
    t = time.gmtime(ts if ts is not None else time.time())
    return (time.strftime("%Y-%m-%d", t), t.tm_hour, t.tm_wday)


def parse_days(raw: str) -> list[int]:
    """Дни недели из строки: '1,4' -> [0, 3]. Пусто -> все дни.

    Человек считает дни с единицы (понедельник = 1), Python — с нуля.
    Расхождение на единицу здесь означало бы аудит не в тот день недели —
    ошибку, которую замечают через неделю.
    """
    raw = (raw or "").strip()
    if not raw or raw in ("*", "все", "ежедневно", "daily"):
        return list(range(7))
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part.isdigit():
            continue
        n = int(part)
        if 1 <= n <= 7:
            out.append(n - 1)
    return sorted(set(out)) or list(range(7))


def days_label(days: list[int]) -> str:
    if len(days) >= 7:
        return "каждый день"
    if days == [0, 1, 2, 3, 4]:
        return "по будням"
    return "по " + ", ".join(DAY_NAMES_BY[d] for d in days)


DEFAULTS: dict[str, Any] = {
    "enabled": False,       # выключено по умолчанию: аудит ходит по чужим
                            # серверам, и начинать это без просьбы нельзя
    "hour": DEFAULT_HOUR,
    "days": DEFAULT_DAYS,
    "sites": "",            # пусто — сайты последнего прогона
    "max_pages": 50,
    "last_date": "",        # дата последнего автозапуска (UTC)
    "last_run_id": 0,
    "updated_at": 0.0,
    "updated_reason": "",
}


async def _read(ctx) -> dict[str, Any]:
    try:
        doc = await ctx.store.get(SETTINGS_COLLECTION, SETTINGS_KEY)
    except Exception:
        doc = None
    data = dict(DEFAULTS)
    if doc is not None:
        raw = getattr(doc, "data", None) or {}
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return data


async def _write(ctx, data: dict[str, Any]) -> None:
    payload = {k: data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    try:
        await ctx.store.update(SETTINGS_COLLECTION, SETTINGS_KEY, payload)
    except Exception:
        try:
            await ctx.store.create(SETTINGS_COLLECTION,
                                   {"id": SETTINGS_KEY, **payload})
        except Exception:
            pass


async def get_settings(ctx) -> dict[str, Any]:
    """Текущая настройка расписания, с разобранными днями."""
    d = await _read(ctx)
    d["days_list"] = parse_days(str(d.get("days", "")))
    d["days_label"] = days_label(d["days_list"])
    return d


async def set_settings(ctx, *, enabled: bool | None = None,
                       hour: int | None = None, days: str | None = None,
                       sites: str | None = None,
                       max_pages: int | None = None,
                       reason: str = "") -> dict[str, Any]:
    """Изменить расписание. Не переданное — не трогаем.

    Частичное обновление принципиально: «перенеси на 4 утра» не должно
    втихую сбрасывать список сайтов, а «выключи» — забывать выбранный час,
    иначе включение обратно пошло бы не по той настройке.
    """
    d = await _read(ctx)
    if enabled is not None:
        d["enabled"] = bool(enabled)
    if hour is not None:
        d["hour"] = max(0, min(23, int(hour)))
    if days is not None:
        d["days"] = (days or "").strip()
    if sites is not None:
        d["sites"] = (sites or "").strip()
    if max_pages is not None:
        d["max_pages"] = max(1, min(500, int(max_pages)))
    d["updated_at"] = time.time()
    d["updated_reason"] = reason or d.get("updated_reason", "")
    await _write(ctx, d)
    d["days_list"] = parse_days(str(d.get("days", "")))
    d["days_label"] = days_label(d["days_list"])
    return d


async def due(ctx, *, ts: float | None = None) -> tuple[bool, str]:
    """Пора ли запускать. Возвращает (да/нет, причина).

    Причина возвращается всегда, в том числе при отказе: без неё «почему
    ночью ничего не запустилось» невозможно объяснить, не влезая в код.
    """
    d = await _read(ctx)
    if not d.get("enabled"):
        return False, "disabled"

    today, hour, wday = _now_parts(ts)

    if wday not in parse_days(str(d.get("days", ""))):
        return False, "other_day"

    want = int(d.get("hour", DEFAULT_HOUR))
    if hour < want:
        return False, "too_early"

    # Уже запускались сегодня — второй раз не идём. Проверка ПО ДАТЕ, а не по
    # прошедшему времени: иначе долгий прогон сдвигал бы следующий запуск и
    # ночной аудит за неделю уполз бы в дневные часы.
    if str(d.get("last_date") or "") == today:
        return False, "already_today"

    # Час уже наступил, но мог и давно пройти — например, платформа не будила
    # приложение полдня. Запускаем: пропущенная ночь хуже сдвинутой на час.
    return True, ("on_time" if hour == want else "catching_up")


async def mark_ran(ctx, *, run_id: int = 0, ts: float | None = None) -> None:
    """Отметить, что сегодня уже запускались.

    Ставится ДО прогона, а не после: аудит идёт десятки минут и может упасть,
    а запись после успеха означала бы, что упавший прогон повторяется на
    каждом тике — сбой сети превратился бы в самый частый аудит в истории
    приложения, да ещё и по чужим серверам.
    """
    d = await _read(ctx)
    today, _hour, _wday = _now_parts(ts)
    d["last_date"] = today
    if run_id:
        d["last_run_id"] = int(run_id)
    await _write(ctx, d)


def describe(d: dict[str, Any]) -> str:
    """Человеческое описание настройки — для ответа и для панели."""
    if not d.get("enabled"):
        return "Автоматический аудит выключен."
    days = d.get("days_label") or days_label(parse_days(str(d.get("days", ""))))
    hour = int(d.get("hour", DEFAULT_HOUR))
    where = d.get("sites") or "сайты последнего прогона"
    return (f"Автоматический аудит: {days} в {hour:02d}:00 UTC, "
            f"{where}, до {int(d.get('max_pages', 50))} страниц на сайт.")
