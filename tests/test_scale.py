"""Портфель из 500 сайтов — проверки на настоящем масштабе.

Почему отдельный файл: остальные тесты работают на двух-трёх сайтах и такой
проблемы не видят в принципе. А ломается всё именно на объёме — до 500 доменов
любой наивный код выглядит прекрасно.

База строится один раз на весь модуль и лежит во временном файле: сеть не
задействована, движок не запускается, данные пишутся прямо в хранилище.
"""

from __future__ import annotations

import json
import random

import pytest

import bridge as br

SITES = 500
RUNS = 3


@pytest.fixture(scope="module")
def big_db(tmp_path_factory) -> str:
    """Портфель: 500 уникальных сайтов, каждый проверен трижды."""
    from seoaudit.store import Store

    path = str(tmp_path_factory.mktemp("scale") / "big.db")
    store = Store(path)
    random.seed(7)
    for r in range(RUNS):
        run_id = store.create_run(label=f"прогон {r}")
        for i in range(SITES):
            origin = f"https://site{i:03d}.example"
            site_id = store.add_site(run_id, origin)
            # каждый семнадцатый «не открылся» — нужно проверить сортировку
            store.set_site_state(site_id, "error" if i % 17 == 0 else "done")
            store.queue_urls(site_id, [f"{origin}/p{p}" for p in range(4)], "seed")
            for page in store.pending_pages(site_id):
                store.save_page_result(page["id"], {
                    "state": "done", "status": 200, "final_url": page["url"],
                    "content_type": "text/html", "bytes": 4000, "head": {},
                })
            store.add_findings(site_id, [{
                "rule": "meta.title_missing", "severity": "high", "layer": 3,
                "message": "Нет заголовка", "url": origin, "evidence": {},
            }])
        store.finish_run(run_id)
    store.close()
    return path


def _payload_kb(node) -> float:
    """Во сколько килобайт обойдётся это дерево на проводе."""
    text = json.dumps(node, ensure_ascii=False,
                      default=lambda o: getattr(o, "props", None)
                      or getattr(o, "__dict__", str(o)))
    return len(text.encode("utf-8")) / 1024


def _walk(node):
    """Обход дерева UI.

    Дети лежат не только в `children`: у ui.List элементы живут в `items`, у
    ListItem раскрытое содержимое — в `expanded_content`. Обход только по
    `children` молча проходит мимо всего списка и «доказывает» пустоту там, где
    строки на самом деле есть — я на это уже попалась.
    """
    yield node
    props = getattr(node, "props", {}) or {}
    for key in ("children", "items", "expanded_content"):
        kids = props.get(key) or []
        if not isinstance(kids, (list, tuple)):
            kids = [kids]
        for kid in kids:
            if hasattr(kid, "type"):
                yield from _walk(kid)


# --- выборка -----------------------------------------------------------------

def test_one_row_per_site_not_one_per_run(big_db):
    """500 сайтов, проверенных трижды, — это 500 строк, а не 1500.

    В схеме `sites` привязана к run_id (UNIQUE(run_id, origin)), поэтому один
    домен, проверенный трижды, лежит ТРЕМЯ записями. Список подключённых сайтов
    обязан показывать его один раз — иначе портфель на глазах «размножается»
    с каждым аудитом.
    """
    store = br.open_store(big_db)
    try:
        rows, total = br.connected_sites(store, limit=200)
        assert total == SITES, f"ожидалось {SITES} уникальных сайтов, получено {total}"
        assert len({r["origin"] for r in rows}) == len(rows), "в странице есть дубли"
        assert all(r["runs"] == RUNS for r in rows), "число проверок посчитано неверно"
    finally:
        store.close()


def test_a_page_stays_a_page(big_db):
    """Сколько просили — столько и пришло, независимо от размера портфеля."""
    store = br.open_store(big_db)
    try:
        for limit in (1, 10, 50, 200):
            rows, total = br.connected_sites(store, limit=limit)
            assert len(rows) == limit
            assert total == SITES
    finally:
        store.close()


def test_paging_covers_everything_without_repeats(big_db):
    """Пройдя все страницы, увидим каждый сайт ровно один раз.

    Тест на устойчивость сортировки: если порядок «плавает» между запросами,
    какие-то сайты покажутся дважды, а какие-то не покажутся никогда — и
    пользователь не сможет добраться до части своего портфеля.
    """
    store = br.open_store(big_db)
    try:
        seen: list[str] = []
        offset = 0
        while True:
            rows, total = br.connected_sites(store, offset=offset, limit=50)
            if not rows:
                break
            seen.extend(r["origin"] for r in rows)
            offset += len(rows)
            assert offset <= total + 50, "пагинация не заканчивается"
        assert len(seen) == SITES
        assert len(set(seen)) == SITES, "какой-то сайт попался дважды"
    finally:
        store.close()


def test_search_looks_at_the_whole_portfolio(big_db):
    """Поиск ищет по ВСЕМ сайтам, а не по текущей странице.

    Ровно поэтому в панели выключен `searchable=True` у ui.List: он фильтрует
    только переданные элементы, то есть на постраничной выдаче искал бы внутри
    50 строк. Человек набрал бы «site487», ничего не увидел и решил, что сайт
    не подключён. Здесь проверяем, что находится сайт с ПОСЛЕДНЕЙ страницы.
    """
    store = br.open_store(big_db)
    try:
        rows, total = br.connected_sites(store, query="site487")
        assert total == 1, "сайт с последней страницы не нашёлся"
        assert rows[0]["host"] == "site487.example"

        rows, total = br.connected_sites(store, query="SITE487")
        assert total == 1, "поиск обязан не зависеть от регистра"
    finally:
        store.close()


@pytest.mark.parametrize("needle", ["%", "_", "\\", "100%_"])
def test_like_metacharacters_are_not_wildcards(big_db, needle):
    """`%` в запросе — это символ, а не «покажи всё».

    Без экранирования поиск по «%» вернул бы весь портфель, а по «_» — любой
    домен: пользователь получил бы 500 «совпадений» и решил, что поиск сломан.
    """
    store = br.open_store(big_db)
    try:
        _rows, total = br.connected_sites(store, query=needle)
        assert total == 0, f"«{needle}» повёл себя как шаблон и вернул {total}"
    finally:
        store.close()


def test_broken_sites_come_first(big_db):
    """Сначала то, что не в порядке. Алфавит на 500 доменах бесполезен."""
    store = br.open_store(big_db)
    try:
        rows, _total = br.connected_sites(store, limit=20)
        assert rows[0]["state"] == "error", "проблемные сайты должны быть сверху"
    finally:
        store.close()


def test_limit_is_capped(big_db):
    """Огромный limit не превращается в выгрузку всего портфеля в UI."""
    store = br.open_store(big_db)
    try:
        rows, _total = br.connected_sites(store, limit=100_000)
        assert len(rows) <= br.SITES_PAGE_MAX
    finally:
        store.close()


# --- панель ------------------------------------------------------------------

async def test_the_sites_screen_stays_small_on_500_sites(ctx, monkeypatch, big_db):
    """Бюджет на объём: экран списка не должен раздуваться вместе с портфелем.

    До постраничности сводка на 500 сайтах весила ~460 КБ данных и отдавала все
    500 строк таблицей разом. Здесь фиксируем верхнюю границу: если кто-то снова
    начнёт отдавать портфель одним куском, тест это увидит.
    """
    import panels

    async def fake_download(_ctx):
        return big_db

    monkeypatch.setattr(panels.br, "download_db", fake_download)

    tree = await panels.seo_center(ctx, view="sites")
    lists = [n for n in _walk(tree) if n.type == "List"]
    assert lists, "на экране сайтов должен быть список"

    items = lists[0].props["items"]
    assert len(items) == br.SITES_PAGE, "на странице должно быть ровно page-size строк"
    assert lists[0].props["total_items"] == SITES, "нужно показывать общее число"

    size = _payload_kb(tree)
    assert size < 60, f"экран весит {size:.0f} КБ — портфель отдаётся целиком?"


async def test_search_on_the_screen_uses_the_whole_portfolio(ctx, monkeypatch, big_db):
    """Поиск на экране находит сайт, которого нет на первой странице."""
    import panels

    async def fake_download(_ctx):
        return big_db

    monkeypatch.setattr(panels.br, "download_db", fake_download)

    tree = await panels.seo_center(ctx, view="sites", q="site487")
    titles = [(getattr(n, "props", {}) or {}).get("title")
              for n in _walk(tree) if n.type == "ListItem"]
    assert titles == ["site487.example"]


@pytest.mark.parametrize("junk", ["мусор", "", "-5", None, "9999999"])
async def test_bad_paging_params_never_break_the_screen(ctx, monkeypatch, big_db, junk):
    """offset из UI приходит строкой и бывает мусором — экран обязан выжить."""
    import panels

    async def fake_download(_ctx):
        return big_db

    monkeypatch.setattr(panels.br, "download_db", fake_download)

    tree = await panels.seo_center(ctx, view="sites", offset=junk)
    assert tree.type == "Stack"


async def test_sidebar_does_not_pay_for_the_whole_portfolio(ctx, monkeypatch, big_db):
    """Сайдбар читает ЛЁГКО.

    Раньше он вызывал `_load`, который поднимает находки по всем сайтам и
    строит задачи — на 500 доменах это ~460 КБ ради двух чисел в подписи, и так
    при каждом обновлении панели.
    """
    import panels

    async def fake_download(_ctx):
        return big_db

    calls: list[str] = []
    real_site_rows = panels.br.site_rows

    def counting_site_rows(*args, **kwargs):
        calls.append("site_rows")
        return real_site_rows(*args, **kwargs)

    monkeypatch.setattr(panels.br, "download_db", fake_download)
    monkeypatch.setattr(panels.br, "site_rows", counting_site_rows)

    tree = await panels.seo_nav(ctx)
    labels = [(getattr(n, "props", {}) or {}).get("label") or ""
              for n in _walk(tree) if n.type == "Button"]

    assert any("Добавить сайт" in x for x in labels)
    assert any("Все сайты" in x for x in labels), "нужен вход в список сайтов"
    assert any(str(SITES) in x for x in labels), "в сайдбаре должно быть число сайтов"
    assert _payload_kb(tree) < 5
    assert calls == [], "сайдбар построил полный отчёт ради двух чисел в подписи"


async def test_the_sites_screen_avoids_the_heavy_path(ctx, monkeypatch, big_db):
    """Экран списка не должен готовить отчёт по всему портфелю.

    Проверяем ФАКТОМ вызова, а не запретом-исключением. Первая версия этого
    теста подменяла `site_rows` на падающую заглушку — и молча ничего не
    проверяла: `_load` ловит любое исключение и превращает его в мягкое
    «problem», так что экран всё равно возвращал Stack и тест был зелёным даже
    когда тяжёлый путь ВЫЗЫВАЛСЯ. Тест, который не умеет падать, хуже
    отсутствующего: он создаёт ложную уверенность.
    """
    import panels

    async def fake_download(_ctx):
        return big_db

    calls: list[str] = []
    real_site_rows = panels.br.site_rows

    def counting_site_rows(*args, **kwargs):
        calls.append("site_rows")
        return real_site_rows(*args, **kwargs)

    monkeypatch.setattr(panels.br, "download_db", fake_download)
    monkeypatch.setattr(panels.br, "site_rows", counting_site_rows)

    tree = await panels.seo_center(ctx, view="sites")

    assert tree.type == "Stack"
    assert calls == [], (
        "экран списка сайтов построил полный отчёт: на портфеле из 500 доменов "
        "это ~460 КБ и сотни лишних запросов ради списка имён"
    )


# --- инструмент чата ---------------------------------------------------------

async def test_the_chat_tool_pages_instead_of_dumping(ctx, monkeypatch, big_db):
    """В чат уходит страница и число «всего», а не 500 строк."""
    import handlers_read as hr
    import shared
    from models import ListConnectedParams

    async def fake_download(_ctx):
        return big_db

    monkeypatch.setattr(hr.br, "download_db", fake_download)
    monkeypatch.setattr(shared.br, "download_db", fake_download)

    result = await hr.list_connected_sites(ctx, ListConnectedParams())
    assert result.status == "success"
    assert len(result.data.items) == 50
    assert "500" in (result.summary or ""), "нужно сказать, сколько всего сайтов"
    assert "offset" in (result.summary or "").lower(), "нужно объяснить, как смотреть дальше"


async def test_empty_search_is_success_not_failure(ctx, monkeypatch, big_db):
    """«Ничего не нашлось» — это ответ, а не сбой."""
    import handlers_read as hr
    import shared
    from models import ListConnectedParams

    async def fake_download(_ctx):
        return big_db

    monkeypatch.setattr(hr.br, "download_db", fake_download)
    monkeypatch.setattr(shared.br, "download_db", fake_download)

    result = await hr.list_connected_sites(ctx, ListConnectedParams(query="нетакогосайта"))
    assert result.status == "success"
    assert result.error_code == ""
