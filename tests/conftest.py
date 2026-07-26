"""Общие фикстуры тестов расширения.

ЧТО ИМЕННО ПОДДЕЛЫВАЕМ. Движок сам ходит в сеть через `urllib` в потоках —
подделывать `ctx.http` бессмысленно, аудит его не использует. Реальные точки
соприкосновения с платформой ровно три: `ctx.storage` (файл базы),
`ctx.store` (сводки) и `ctx.background_task` (долгий прогон). Первые две даёт
SDK, третьей у MockContext НЕТ — поэтому здесь есть фикстура, которая её
добавляет и записывает запуски.

Сеть в тестах не трогаем вообще: правила движка уже покрыты его собственными
28 тестами на подготовленном HTML. Здесь проверяется ОБВЯЗКА — что инструмент
корректно переводит состояние базы в ответ пользователю, что пустота не
выглядит ошибкой, и что панели не рассыпаются.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def ctx():
    """Контекст платформы с рабочими storage/store и подделанным фоном."""
    from imperal_sdk.testing import MockContext, MockSecretStore

    mock = MockContext()
    mock.secrets = MockSecretStore({})

    # MockContext не умеет background_task. Наша фикстура его добавляет и
    # ВЫПОЛНЯЕТ корутину сразу: в тесте важен результат, а не факт детача.
    spawned: list[dict] = []

    async def background_task(coro, *, long_running: bool = False, name: str = ""):
        spawned.append({"long_running": long_running, "name": name})
        # Корутину нужно исполнить — иначе Python предупредит о том, что она
        # никогда не ожидалась, а тест не увидит записанных результатов.
        mock.last_background_result = await coro
        return "task-test-1"

    mock.background_task = background_task
    mock.spawned = spawned
    mock.last_background_result = None
    return mock


@pytest.fixture
def no_background(ctx):
    """Контекст БЕЗ фонового хука — как локальный прогон или dev-режим.

    Проверяет обещание из handlers_audit: инструмент обязан работать и без
    kernel-хука, просто синхронно, а не падать из-за среды.
    """
    def boom(*_a, **_k):
        raise RuntimeError("no kernel spawn hook in this context")

    ctx.background_task = boom
    return ctx


# --- готовые базы аудита ----------------------------------------------------

def make_db(tmp_path, *, sites=(("https://example.com", "done"),),
            findings=None, label="тестовый прогон") -> str:
    """Собрать базу аудита БЕЗ обхода сети.

    Пишем прямо через Store движка: так тесты проверяют обвязку на реальной
    схеме (а не на выдуманной), но не делают ни одного сетевого запроса.
    """
    from seoaudit.store import Store

    path = str(tmp_path / "portfolio.db")
    store = Store(path)
    run_id = store.create_run(label=label)

    for origin, state in sites:
        site_id = store.add_site(run_id, origin)
        store.set_site_state(site_id, state)
        # Страница нужна, чтобы оценка считалась от реального количества.
        store.queue_urls(site_id, [origin + "/"], "seed")
        pages = store.pending_pages(site_id)
        if pages:
            store.save_page_result(pages[0]["id"], {
                "state": "done", "status": 200, "final_url": origin + "/",
                "redirects": 0, "elapsed_ms": 120, "content_type": "text/html",
                "bytes": 2048, "error": "",
                "head": {"title": "Главная", "description": "",
                         "canonical": origin + "/", "html_lang": "ru",
                         "noindex": False, "word_count": 400},
            })
        for f in (findings or []):
            store.add_findings(site_id, [f])

    store.finish_run(run_id)
    store.close()
    return path


def finding(rule="meta_description_missing", severity="high", layer=4,
            url="https://example.com/", message="Нет описания страницы",
            **extra) -> dict:
    """Одна находка в форме, которую пишет движок."""
    d = {
        "rule": rule,
        "severity": severity,
        "layer": layer,
        "effort": 1,
        "url": url,
        "message": message,
        "detail": extra.pop("detail", ""),
    }
    d.update(extra)
    return d


@pytest.fixture
async def loaded_ctx(ctx, tmp_path):
    """Контекст, в хранилище которого уже лежит база с находками."""
    import bridge as br

    path = make_db(tmp_path, findings=[
        finding(),
        finding(rule="h1_missing", severity="medium", layer=4,
                message="Нет заголовка H1"),
        finding(rule="canonical_missing", severity="critical", layer=1,
                message="Нет канонического адреса"),
    ])
    with open(path, "rb") as fh:
        await ctx.storage.upload(br.storage_key(ctx), fh.read())
    return ctx
