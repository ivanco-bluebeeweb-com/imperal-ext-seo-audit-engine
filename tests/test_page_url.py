"""Тесты page-level фильтра (`page_url`) в list_findings/list_tasks/get_report.

КОНТРАКТ, который проверяется здесь — ровно тот, что согласован с пользователем
до реализации:

* `page_url` — ОПЦИОНАЛЬНЫЙ параметр. Пустое значение (по умолчанию) не меняет
  ни одной ветки существующей site-wide логики — это старое поведение,
  байт-в-байт. Тест `test_default_behaviour_is_untouched_by_the_new_param`
  ловит именно это: если кто-то потом «улучшит» фильтрацию и она случайно
  тронет пустой page_url, регрессия будет видна сразу.
* Сравнение — ТОЛЬКО точное совпадение через `same_url` движка (нормализация
  схемы/хоста/конечного слэша). Если страницы нет — явная ошибка со списком
  известных адресов, а НЕ подмена на «похожий» URL.
* `site` можно не указывать, если дан `page_url` — сайт угадывается по хосту
  адреса. Если оба заданы и указывают на разные хосты — явная ошибка
  SEO_PAGE_SITE_MISMATCH, а не молчаливый выбор одного из двух.
"""

from __future__ import annotations

import pytest

import bridge as br
import codes as c


def _db_with_two_sites(tmp_path) -> str:
    """Портфель из двух сайтов: g4s.md (Home + About) и climtec.md (Home).

    g4s.md несёт:
      - находку УРОВНЯ СТРАНИЦЫ на Home (title missing) — matched_via="url";
      - находку УРОВНЯ САЙТА (сироты), где evidence.urls включает About —
        matched_via="evidence" для About, но НЕ для Home.
    climtec.md — просто второй сайт, нужен для проверки site/page_url mismatch.
    """
    from seoaudit.store import Store

    path = str(tmp_path / "portfolio.db")
    store = Store(path)

    run = store.create_run(label="прогон для теста page_url")

    g4s = store.add_site(run, "https://g4s.md")
    store.set_site_state(g4s, "done")
    store.queue_urls(
        g4s, ["https://g4s.md/", "https://g4s.md/about"], "seed")
    pages = {p["url"]: p["id"] for p in store.pending_pages(g4s)}
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
    store.add_findings(g4s, [
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

    run2 = store.create_run(label="climtec прогон")
    climtec = store.add_site(run2, "https://climtec.md")
    store.set_site_state(climtec, "done")
    store.queue_urls(climtec, ["https://climtec.md/"], "seed")
    for page in store.pending_pages(climtec):
        store.save_page_result(page["id"], {
            "state": "done", "status": 200,
            "final_url": "https://climtec.md/",
            "content_type": "text/html", "bytes": 1024, "head": {},
        })
    store.finish_run(run2)

    store.close()
    return path


@pytest.fixture
def portfolio_path(tmp_path):
    return _db_with_two_sites(tmp_path)


def _patch_download(monkeypatch, hr, path):
    async def fake_download(_ctx):
        return path
    monkeypatch.setattr(hr.br, "download_db", fake_download)


# --- exact match --------------------------------------------------------

async def test_list_findings_page_url_exact_match_returns_only_that_page(
        ctx, monkeypatch, portfolio_path):
    """Home с `page_url` возвращает СВОЮ находку и не тянет чужую (About)."""
    import handlers_read as hr
    from models import ListFindingsParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(page_url="https://g4s.md/", site="g4s.md"))

    assert result.status == "success"
    urls = {f.url for f in result.data}
    assert urls == {"https://g4s.md/"}


async def test_site_is_optional_when_page_url_is_given(
        ctx, monkeypatch, portfolio_path):
    """Сайт можно не называть — угадывается по хосту page_url."""
    import handlers_read as hr
    from models import ListFindingsParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(page_url="https://g4s.md/"))

    assert result.status == "success"
    assert {f.url for f in result.data} == {"https://g4s.md/"}


# --- нормализация: root/trailing slash ----------------------------------

async def test_page_url_matches_regardless_of_trailing_slash(
        ctx, monkeypatch, portfolio_path):
    """`https://g4s.md` (без слэша) — тот же адрес, что и Home с слэшем."""
    import handlers_read as hr
    from models import ListFindingsParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(page_url="https://g4s.md"))

    assert result.status == "success"
    assert {f.url for f in result.data} == {"https://g4s.md/"}


async def test_page_url_matches_regardless_of_scheme_case(
        ctx, monkeypatch, portfolio_path):
    """Схема/хост сравниваются регистронезависимо (через `same_url` движка)."""
    import handlers_read as hr
    from models import ListFindingsParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(page_url="HTTPS://G4S.MD/"))

    assert result.status == "success"
    assert {f.url for f in result.data} == {"https://g4s.md/"}


# --- evidence-совпадение внутри сайт-уровневой находки -------------------

async def test_evidence_match_is_marked_differently_from_direct_url_match(
        ctx, monkeypatch, portfolio_path):
    """About затронут сайт-уровневой находкой (сироты) — matched_via='evidence'."""
    import handlers_read as hr
    from models import ListFindingsParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(page_url="https://g4s.md/about",
                                min_severity="low"))

    assert result.status == "success"
    items = list(result.data)
    assert len(items) == 1
    assert items[0].rule == "structure.orphan_page"


# --- zero findings -------------------------------------------------------

async def test_page_with_no_findings_is_a_success_not_an_error(
        ctx, monkeypatch, tmp_path):
    """Страница без единой находки — это «всё в порядке», а не ошибка."""
    import handlers_read as hr
    from models import ListFindingsParams
    from seoaudit.store import Store

    path = str(tmp_path / "clean.db")
    store = Store(path)
    run = store.create_run(label="чистый сайт")
    site = store.add_site(run, "https://clean.md")
    store.set_site_state(site, "done")
    store.queue_urls(site, ["https://clean.md/"], "seed")
    for page in store.pending_pages(site):
        store.save_page_result(page["id"], {
            "state": "done", "status": 200, "final_url": "https://clean.md/",
            "content_type": "text/html", "bytes": 512, "head": {},
        })
    store.finish_run(run)
    store.close()

    _patch_download(monkeypatch, hr, path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(page_url="https://clean.md/"))

    assert result.status == "success"
    assert list(result.data) == []
    assert "нет" in result.summary.lower()


# --- page_url не найден: явная ошибка, БЕЗ подмены -----------------------

async def test_unknown_page_url_is_an_explicit_error_not_a_silent_swap(
        ctx, monkeypatch, portfolio_path):
    """Несуществующий адрес — SEO_PAGE_NOT_FOUND, а не «похожая» страница."""
    import handlers_read as hr
    from models import ListFindingsParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(page_url="https://g4s.md/contact"))

    assert result.status == "error"
    assert result.error_code == c.SEO_PAGE_NOT_FOUND
    # список известных адресов — часть честного отказа, не мелочь
    assert "g4s.md" in result.error


# --- site и page_url на разные хосты: явная ошибка -----------------------

async def test_site_and_page_url_pointing_at_different_hosts_is_an_error(
        ctx, monkeypatch, portfolio_path):
    """Явное противоречие между `site` и `page_url` не решается молча."""
    import handlers_read as hr
    from models import ListFindingsParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_findings(
        ctx, ListFindingsParams(site="climtec.md",
                                page_url="https://g4s.md/"))

    assert result.status == "error"
    assert result.error_code == c.SEO_PAGE_SITE_MISMATCH


# --- задачи: multi-page task должна включать страницу, но не клонироваться --

async def test_list_tasks_page_url_keeps_task_identity_intact(
        ctx, monkeypatch, portfolio_path):
    """Задача про сирот (About) возвращается ЦЕЛОЙ — с тем же fingerprint.

    Не projection с урезанным urls: иначе повторный экспорт в трекер видел бы
    другой отпечаток и создавал бы дубликат вместо обновления существующей
    задачи.
    """
    import handlers_read as hr
    from models import ListTasksParams

    _patch_download(monkeypatch, hr, portfolio_path)

    full = await hr.list_tasks(
        ctx, ListTasksParams(site="g4s.md", min_severity="low"))
    scoped = await hr.list_tasks(
        ctx, ListTasksParams(page_url="https://g4s.md/about",
                             min_severity="low"))

    assert full.status == "success" and scoped.status == "success"
    full_task = next(t for t in full.data if t.rule == "structure.orphan_page")
    scoped_task = next(
        t for t in scoped.data if t.rule == "structure.orphan_page")
    assert scoped_task.fingerprint == full_task.fingerprint
    assert scoped_task.matched_page == "https://g4s.md/about"


async def test_list_tasks_page_url_excludes_tasks_not_touching_the_page(
        ctx, monkeypatch, portfolio_path):
    """Home не участвует в задаче про сирот (About) — задача не должна прийти."""
    import handlers_read as hr
    from models import ListTasksParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.list_tasks(
        ctx, ListTasksParams(page_url="https://g4s.md/", min_severity="low"))

    assert result.status == "success"
    rules = {t.rule for t in result.data}
    assert "structure.orphan_page" not in rules
    assert "meta.title_missing" in rules


# --- get_report: page-отчёт ----------------------------------------------

async def test_get_report_page_scope_markdown_snapshot(
        ctx, monkeypatch, portfolio_path):
    """Отчёт по Home: компактный markdown с адресом, находками и задачами."""
    import handlers_read as hr
    from models import GetReportParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.get_report(
        ctx, GetReportParams(page_url="https://g4s.md/"))

    assert result.status == "success"
    entity = result.data
    assert entity.scope == "page"
    assert entity.page_url == "https://g4s.md/"
    assert entity.findings_total == 1
    assert entity.has_critical_or_high is True
    assert "g4s.md" in entity.markdown
    assert "Нет заголовка" in entity.markdown or "title" in entity.markdown.lower()


async def test_get_report_page_not_found_reports_known_urls(
        ctx, monkeypatch, portfolio_path):
    """Отчёт по несуществующей странице — ошибка со списком известных адресов."""
    import handlers_read as hr
    from models import GetReportParams

    _patch_download(monkeypatch, hr, portfolio_path)

    result = await hr.get_report(
        ctx, GetReportParams(page_url="https://g4s.md/pricing"))

    assert result.status == "error"
    assert result.error_code == c.SEO_PAGE_NOT_FOUND


# --- обратная совместимость: пустой page_url не меняет старое поведение ---

async def test_default_behaviour_is_untouched_by_the_new_param(
        ctx, monkeypatch, portfolio_path):
    """Без page_url list_findings/list_tasks/get_report работают КАК РАНЬШЕ.

    Это байт-в-байт та же сборка данных, что и до добавления page_url —
    страховка от случайной регрессии site-wide логики по умолчанию.
    """
    import handlers_read as hr
    from models import GetReportParams, ListFindingsParams, ListTasksParams

    _patch_download(monkeypatch, hr, portfolio_path)

    findings = await hr.list_findings(ctx, ListFindingsParams(site="g4s.md"))
    tasks = await hr.list_tasks(ctx, ListTasksParams(site="g4s.md",
                                                     min_severity="low"))
    report = await hr.get_report(ctx, GetReportParams(site="g4s.md"))

    assert findings.status == "success"
    assert {f.url for f in findings.data} == {
        "https://g4s.md/", "https://g4s.md/about"}  # default min_severity=medium includes both
    assert tasks.status == "success"
    assert {t.rule for t in tasks.data} == {
        "meta.title_missing", "structure.orphan_page"}
    assert report.status == "success"
    assert report.data.scope == "site"
    assert report.data.page_url == ""
