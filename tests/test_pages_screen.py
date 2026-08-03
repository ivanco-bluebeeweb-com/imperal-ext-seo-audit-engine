"""Тесты новых экранов «Страницы сайта» и «Карточка страницы».

Строятся на том же page-level слое движка, что уже покрыт
`tests/test_page_url.py` (`find_page`, `filter_findings_by_page`,
`filter_tasks_by_page`, `same_url`) — экран и чат обязаны отвечать на один
вопрос про страницу одинаково, поэтому тесты здесь про ПАНЕЛЬ, а не про
повторную проверку самого фильтра.
"""

from __future__ import annotations

import pytest

import panels


def _flatten(node):
    """Обойти дерево UI — как в test_panels.py: дети не только в children."""
    yield node
    props = getattr(node, "props", None) or {}
    for key in ("children", "items", "expanded_content"):
        kids = props.get(key) or []
        if not isinstance(kids, (list, tuple)):
            kids = [kids]
        for kid in kids:
            if hasattr(kid, "type"):
                yield from _flatten(kid)


def _db_with_pages(tmp_path) -> str:
    """Сайт g4s.md с тремя страницами: Home (своя находка), About (через
    evidence сайт-уровневой находки), Contacts — по-настоящему чистая.

    Третья страница нужна, чтобы фильтр «только с находками» реально что-то
    прятал: без неё обе страницы несли бы находку, и тест на фильтр не
    отличал бы «фильтр работает» от «фильтра нет вовсе».
    """
    from seoaudit.store import Store

    path = str(tmp_path / "pages.db")
    store = Store(path)
    run = store.create_run(label="прогон для экрана страниц")

    site = store.add_site(run, "https://g4s.md")
    store.set_site_state(site, "done")
    store.queue_urls(site, [
        "https://g4s.md/", "https://g4s.md/about", "https://g4s.md/contacts",
    ], "seed")
    pages = {p["url"]: p["id"] for p in store.pending_pages(site)}
    store.save_page_result(pages["https://g4s.md/"], {
        "state": "done", "status": 200, "final_url": "https://g4s.md/",
        "content_type": "text/html", "bytes": 1024,
        "head": {"title": "Главная — G4S"},
    })
    store.save_page_result(pages["https://g4s.md/about"], {
        "state": "done", "status": 200, "final_url": "https://g4s.md/about",
        "content_type": "text/html", "bytes": 900,
        "head": {"title": "О нас"},
    })
    store.save_page_result(pages["https://g4s.md/contacts"], {
        "state": "done", "status": 200, "final_url": "https://g4s.md/contacts",
        "content_type": "text/html", "bytes": 800,
        "head": {"title": "Контакты"},
    })
    store.add_findings(site, [
        {
            "rule": "meta.title_missing", "severity": "high", "layer": 4,
            "effort": 1, "url": "https://g4s.md/", "message": "Нет заголовка",
            "detail": "", "evidence": {},
        },
        {
            "rule": "structure.orphan_page", "severity": "medium", "layer": 3,
            "effort": 2, "url": "https://g4s.md/about",
            "message": "Страница-сирота (1 шт.)", "detail": "",
            "evidence": {"orphans": ["https://g4s.md/about"], "pages": 2},
        },
    ])
    store.finish_run(run)
    store.close()
    return path


@pytest.fixture
def pages_db(tmp_path) -> str:
    return _db_with_pages(tmp_path)


def _patch_download(monkeypatch, path):
    async def fake_download(_ctx):
        return path
    monkeypatch.setattr(panels.br, "download_db", fake_download)


# --- экран «Страницы сайта» -------------------------------------------------

async def test_pages_screen_lists_all_pages_by_default(ctx, monkeypatch, pages_db):
    """Без фильтра видны ВСЕ три страницы, включая по-настоящему чистую."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(ctx, view="pages", site="g4s.md")
    nodes = list(_flatten(page))
    items = [n for n in nodes if n.type == "ListItem"]
    urls = {i.props["id"] for i in items}

    assert urls == {
        "https://g4s.md/", "https://g4s.md/about", "https://g4s.md/contacts",
    }


async def test_pages_screen_only_issues_filter_hides_clean_pages(
        ctx, monkeypatch, pages_db):
    """`only_issues=1` прячет Contacts, но оставляет Home и About.

    About остаётся, хотя своей находки на URL не имеет — она попадает через
    evidence сайт-уровневой находки (сироты), и фильтр обязан это видеть,
    а не смотреть только на прямое совпадение по `url`.
    """
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(
        ctx, view="pages", site="g4s.md", only_issues="1")
    nodes = list(_flatten(page))
    items = [n for n in nodes if n.type == "ListItem"]
    urls = {i.props["id"] for i in items}

    assert urls == {"https://g4s.md/", "https://g4s.md/about"}
    assert "https://g4s.md/contacts" not in urls


async def test_pages_screen_toggle_buttons_are_present(ctx, monkeypatch, pages_db):
    """Переключатель «Все страницы» / «Только с находками» есть на экране."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(ctx, view="pages", site="g4s.md")
    labels = [n.props.get("label") for n in _flatten(page) if n.type == "Button"]

    assert "Все страницы" in labels
    assert "Только с находками" in labels


async def test_pages_screen_unknown_site_shows_info_not_error(
        ctx, monkeypatch, pages_db):
    """Домен не из портфеля — спокойное сообщение, путь назад, не «сбой»."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(ctx, view="pages", site="unknown-site.example")
    alerts = [n for n in _flatten(page) if n.type == "Alert"]

    assert alerts and alerts[0].props.get("type") == "info"


async def test_pages_screen_row_click_opens_page_card(ctx, monkeypatch, pages_db):
    """Строка страницы ведёт на её карточку с точным адресом."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(ctx, view="pages", site="g4s.md")
    items = [n for n in _flatten(page) if n.type == "ListItem"]
    home = next(i for i in items if i.props["id"] == "https://g4s.md/")

    click = home.props.get("on_click")
    assert click is not None
    assert click.params["params"] == {
        "view": "page", "site": "g4s.md", "page": "https://g4s.md/"}


# --- экран «Карточка страницы» ----------------------------------------------

async def test_page_screen_shows_only_that_pages_finding(
        ctx, monkeypatch, pages_db):
    """Home открывает СВОЮ находку и не тянет находку About."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(
        ctx, view="page", site="g4s.md", page="https://g4s.md/")
    text = " ".join(str(getattr(n, "props", "")) for n in _flatten(page))

    assert "Нет заголовка" in text
    assert "сирота" not in text.lower()


async def test_page_screen_shows_evidence_matched_finding_for_about(
        ctx, monkeypatch, pages_db):
    """About открывает находку уровня сайта, попавшую через evidence."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(
        ctx, view="page", site="g4s.md", page="https://g4s.md/about")
    text = " ".join(str(getattr(n, "props", "")) for n in _flatten(page))

    assert "сирота" in text.lower()


async def test_page_screen_unknown_url_does_not_silently_substitute(
        ctx, monkeypatch, pages_db):
    """Опечатка в адресе — честная ошибка со списком известных, не подмена."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(
        ctx, view="page", site="g4s.md", page="https://g4s.md/nope")
    alerts = [n for n in _flatten(page) if n.type == "Alert"]

    assert alerts
    assert "g4s.md" not in "".join(
        n.props.get("label", "") for n in _flatten(page) if n.type == "Header")


async def test_page_screen_missing_page_param_asks_instead_of_guessing(
        ctx, monkeypatch, pages_db):
    """Без адреса страницы экран не угадывает — просит указать обе части."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(ctx, view="page", site="g4s.md")
    alerts = [n for n in _flatten(page) if n.type == "Alert"]

    assert alerts and alerts[0].props.get("type") == "info"


async def test_site_card_links_to_pages_screen(ctx, monkeypatch, pages_db):
    """Карточка сайта показывает превью страниц и кнопку на полный список."""
    _patch_download(monkeypatch, pages_db)

    page = await panels.seo_center(ctx, view="site", site="g4s.md")
    labels = [n.props.get("label") for n in _flatten(page) if n.type == "Button"]
    items = [n for n in _flatten(page) if n.type == "ListItem"]
    page_items = [i for i in items if str(i.props.get("id", "")).startswith("page-")]

    assert "Все страницы" in labels
    assert page_items, "нет превью страниц на карточке сайта"
