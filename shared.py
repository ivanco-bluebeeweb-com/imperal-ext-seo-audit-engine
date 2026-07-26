"""Хелперы, общие для всех слоёв инструментов.

Урок из Notion Connector: когда слой записи импортирует приватные имена из слоя
чтения, зависимость говорит «запись построена на чтении», хотя это равноправные
слои. Поэтому общее живёт здесь, а не в одном из них.
"""

from __future__ import annotations

from typing import Any

from imperal_sdk import ActionResult

import bridge as br
import codes as c


def error(message: str, code: str, retryable: bool = False) -> ActionResult:
    """Ошибка со структурным кодом.

    `code` — обязательный позиционный аргумент. Ядро штампует
    `EXT_UNSTRUCTURED_ERROR` на любую ошибку без кода, превращая точный сбой в
    прозу, по которой ничего нельзя сделать. Валидатор ловит только буквальные
    `ActionResult.error(`, поэтому хелпер скрыл бы приложение от проверки — если
    бы код не был обязателен здесь.
    """
    return ActionResult.error(message, retryable, code=code)


async def open_portfolio(ctx) -> tuple[Any, int, ActionResult | None]:
    """Открыть базу портфеля и определить прогон.

    Возвращает (store, run_id, ошибка). Ошибка не None — значит показать её и
    выйти. Отсутствие базы и нечитаемая база — РАЗНЫЕ случаи: первый значит
    «запусти аудит», второй «что-то не так у нас», и путать их нельзя, иначе
    совет будет неверный.
    """
    path = await br.download_db(ctx)
    if not path:
        return None, 0, error(
            "Аудитов ещё не было. Скажите, какие сайты проверить — например "
            "«проверь climtec.md» — и я начну.",
            c.SEO_NO_RUNS,
        )
    try:
        store = br.open_store(path)
    except Exception as exc:
        await ctx.log(f"portfolio db unreadable: {type(exc).__name__}: {exc}",
                      "error")
        return None, 0, error(
            "Не удалось прочитать результаты прошлых аудитов. "
            "Запустите аудит заново — новые данные это исправят.",
            c.SEO_DB_UNREADABLE,
        )
    return store, 0, None


async def store_run_summary(ctx, run_id: int, data: dict) -> None:
    """Продублировать сводку прогона в ctx.store.

    Чтобы «покажи прогоны» отвечало мгновенно, не скачивая базу на десятки
    мегабайт. Сбой кеширования никогда не должен ломать аудит — он уже прошёл.
    """
    try:
        page = await ctx.store.query(br.RUNS_COLLECTION,
                                     where={"run_id": run_id}, limit=1)
        if page.data:
            await ctx.store.update(br.RUNS_COLLECTION, page.data[0].id, data)
        else:
            await ctx.store.create(br.RUNS_COLLECTION, data)
    except Exception:
        pass
