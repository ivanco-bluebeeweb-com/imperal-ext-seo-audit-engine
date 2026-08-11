"""Тесты best-effort интеграции с page-speed-insights.

Однонаправленная зависимость: SEO Audit Engine знает про page-speed-insights,
обратного знания нет. Проверяем ТРИ сценария из PREPARATION.md/бридж-докстрин
enrich_with_page_speed: приложение не установлено (IPC бросает), приложение
установлено но метрики хорошие (нет находки), и метрики плохие (находка
добавляется с правильным слоем/серьёзностью).
"""

from __future__ import annotations

import pytest

import bridge as br


def _site_row(store, run_id: int) -> dict:
    rows = store.sites(run_id)
    assert rows
    return dict(rows[0])


@pytest.mark.asyncio
async def test_enrich_skips_silently_when_extension_not_installed(ctx, tmp_path):
    """Нет регистрации page-speed-insights в MockExtensions -> call() бросает
    ExtensionError -- функция должна проглотить это и не добавить находок."""
    from tests.conftest import make_db
    from seoaudit.store import Store

    path = make_db(tmp_path)
    store = Store(path)
    run_id = 1
    site = _site_row(store, run_id)

    await br.enrich_with_page_speed(ctx, store, run_id, [site])

    findings = store.findings(site["id"])
    assert not any(f["rule"] == "performance.core_web_vitals_poor" for f in findings)
    store.close()


@pytest.mark.asyncio
async def test_enrich_adds_no_finding_when_metrics_are_good(ctx, tmp_path):
    from tests.conftest import make_db
    from seoaudit.store import Store

    async def fake_check(url: str, strategy: str = "mobile", **kwargs):
        return {
            "ok": True,
            "field_metrics": [{"name": "LCP", "category": "good"}],
            "lab_metrics": [{"name": "CLS", "category": "good"}],
        }

    ctx.extensions.register("page-speed-insights", "check_site_speed_ipc", fake_check)

    path = make_db(tmp_path)
    store = Store(path)
    run_id = 1
    site = _site_row(store, run_id)

    await br.enrich_with_page_speed(ctx, store, run_id, [site])

    findings = store.findings(site["id"])
    assert not any(f["rule"] == "performance.core_web_vitals_poor" for f in findings)
    store.close()


@pytest.mark.asyncio
async def test_enrich_adds_finding_when_metrics_are_poor(ctx, tmp_path):
    from tests.conftest import make_db
    from seoaudit.store import Store
    from seoaudit.severity import HIGH, LAYER_TECHNICAL

    async def fake_check(url: str, strategy: str = "mobile", **kwargs):
        return {
            "ok": True,
            "field_metrics": [{"name": "LCP", "category": "poor", "value": 5200, "unit": "ms"}],
            "lab_metrics": [],
        }

    ctx.extensions.register("page-speed-insights", "check_site_speed_ipc", fake_check)

    path = make_db(tmp_path)
    store = Store(path)
    run_id = 1
    site = _site_row(store, run_id)

    await br.enrich_with_page_speed(ctx, store, run_id, [site])

    findings = [dict(f) for f in store.findings(site["id"])]
    cwv = [f for f in findings if f["rule"] == "performance.core_web_vitals_poor"]
    assert len(cwv) == 1
    assert cwv[0]["severity"] == HIGH
    assert cwv[0]["layer"] == LAYER_TECHNICAL
    assert "LCP" in cwv[0]["message"] or "LCP" in cwv[0]["detail"]
    store.close()


@pytest.mark.asyncio
async def test_enrich_skips_sites_without_id_or_origin(ctx):
    """Защита от мусорных строк -- не должно даже пытаться звать IPC."""
    called = {"n": 0}

    async def fake_check(**kwargs):
        called["n"] += 1
        return {"ok": True}

    ctx.extensions.register("page-speed-insights", "check_site_speed_ipc", fake_check)

    await br.enrich_with_page_speed(ctx, None, 1, [{"origin": "", "id": 1}, {"origin": "https://x.com", "id": None}])

    assert called["n"] == 0
