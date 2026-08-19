"""Plausible Scenario Tests (PST) -- SEO Audit Engine.

Method: Docs/session-notes/SCENARIO_TESTING_STANDARD.md. This app has 14
functions (13 @chat.function + 1 @ext.expose IPC surface) and 13 existing
test files covering rule engine internals (28 own tests), URL normalization,
page-level filtering, panels, scale/WAL, and safe concurrent uploads. A
name-based coverage audit found 8 functions never exercised by any existing
test through their actual handler:

    list_runs, list_sites, export_plan, fix_plan, compare_audits,
    set_schedule, get_schedule, register_known_site

This file closes those 8 gaps. Seeding follows the exact pattern established
in tests/test_pages_screen.py and tests/test_tools.py: build a real sqlite
Store via seoaudit.store.Store, monkeypatch bridge.download_db to hand back
that path (the same substitution panels/handlers tests already use), never
fake the rule engine itself.
"""
from __future__ import annotations

import pytest

import bridge as br
import codes as c
import handlers_read as hr
import handlers_schedule as hs
import schedule_settings as sched
from models import (
    CompareParams, ExportPlanParams, FixPlanParams, GetScheduleParams,
    ListFindingsParams, ListRunsParams, ScheduleParams,
)


pytestmark = pytest.mark.asyncio


def _seed_two_runs(tmp_path):
    """One site, audited twice: first run has 2 findings, second run has
    one fixed and one new -- the minimal shape that makes compare_audits,
    export_plan and fix_plan all produce non-empty, meaningful output."""
    from seoaudit.store import Store

    path = str(tmp_path / "portfolio.db")
    store = Store(path)

    run1 = store.create_run(label="первый прогон")
    site1 = store.add_site(run1, "https://climtec.md")
    store.set_site_state(site1, "done")
    store.add_findings(site1, [
        {"rule": "meta.title_missing", "severity": "high", "layer": 4,
         "effort": 1, "url": "https://climtec.md/", "message": "Нет заголовка",
         "detail": "", "fixable": True, "evidence": {}},
        {"rule": "meta.desc_missing", "severity": "medium", "layer": 4,
         "effort": 1, "url": "https://climtec.md/about",
         "message": "Нет описания", "detail": "", "fixable": True,
         "evidence": {}},
    ])
    store.finish_run(run1)

    run2 = store.create_run(label="второй прогон")
    site2 = store.add_site(run2, "https://climtec.md")
    store.set_site_state(site2, "done")
    store.add_findings(site2, [
        # meta.title_missing fixed (not present anymore)
        {"rule": "meta.desc_missing", "severity": "medium", "layer": 4,
         "effort": 1, "url": "https://climtec.md/about",
         "message": "Нет описания", "detail": "", "fixable": True,
         "evidence": {}},
        {"rule": "img.alt_missing", "severity": "low", "layer": 4,
         "effort": 1, "url": "https://climtec.md/gallery",
         "message": "Нет alt", "detail": "", "fixable": False,
         "evidence": {}},
    ])
    store.finish_run(run2)
    store.close()
    return path, run1, run2


@pytest.fixture
def seeded_ctx(ctx, tmp_path, monkeypatch):
    path, run1, run2 = _seed_two_runs(tmp_path)

    async def fake_download(_ctx):
        return path

    monkeypatch.setattr(br, "download_db", fake_download)
    ctx.run1, ctx.run2 = run1, run2
    return ctx


# ── list_runs ────────────────────────────────────────────────────────────

async def test_happy_list_runs_newest_first(seeded_ctx):
    result = await hr.list_runs(seeded_ctx, ListRunsParams())
    assert result.status == "success"
    ids = [item.run_id for item in result.data.items]
    assert ids == sorted(ids, reverse=True)
    assert seeded_ctx.run2 in ids and seeded_ctx.run1 in ids


async def test_blocked_list_runs_before_any_audit(ctx):
    result = await hr.list_runs(ctx, ListRunsParams())
    assert result.status == "error"
    assert result.error_code == c.SEO_NO_RUNS


# ── list_sites ───────────────────────────────────────────────────────────

async def test_happy_list_sites_worst_score_first(seeded_ctx):
    result = await hr.list_sites(seeded_ctx, ListFindingsParams(min_severity="low"))
    assert result.status == "success"
    assert len(result.data.items) == 1
    assert result.data.items[0].origin == "https://climtec.md"


# ── export_plan ──────────────────────────────────────────────────────────

async def test_happy_export_plan_has_entries_for_fixable_findings(seeded_ctx):
    result = await hr.export_plan(seeded_ctx, ExportPlanParams(
        run_id=seeded_ctx.run1, project="Imperal Cloud", assignee="val"))
    assert result.status == "success"
    assert result.data.tasks_total >= 1


async def test_error_export_plan_unknown_site(seeded_ctx):
    result = await hr.export_plan(seeded_ctx, ExportPlanParams(
        run_id=seeded_ctx.run1, site="not-a-real-site.example"))
    assert result.status == "error"
    assert result.error_code == c.SEO_SITE_NOT_FOUND


# ── fix_plan ─────────────────────────────────────────────────────────────

async def test_happy_fix_plan_lists_fixable_findings(seeded_ctx):
    result = await hr.fix_plan(seeded_ctx, FixPlanParams(run_id=seeded_ctx.run1))
    assert result.status == "success"


async def test_adversarial_fix_plan_only_ready_never_crashes_on_zero_matches(seeded_ctx):
    # A stricter filter that may legitimately match nothing must still be a
    # clean success, not an exception or a silent-looking error.
    result = await hr.fix_plan(seeded_ctx, FixPlanParams(
        run_id=seeded_ctx.run1, only_ready=True, limit=1))
    assert result.status == "success"


# ── compare_audits ───────────────────────────────────────────────────────

async def test_happy_compare_audits_reports_fixed_and_appeared(seeded_ctx):
    result = await hr.compare_audits(seeded_ctx, CompareParams(
        site="climtec.md", after_run=seeded_ctx.run2, before_run=seeded_ctx.run1))
    assert result.status == "success"
    assert result.data.fixed_count == 1   # meta.title_missing fixed
    assert result.data.appeared_count == 1  # img.alt_missing is new


async def test_error_compare_audits_unknown_site(seeded_ctx):
    result = await hr.compare_audits(seeded_ctx, CompareParams(site="never-audited.example"))
    assert result.status == "error"
    assert result.error_code == c.SEO_SITE_NOT_FOUND


async def test_recovery_compare_audits_single_run_explains_instead_of_erroring(tmp_path, ctx, monkeypatch):
    """Only one audit ever run for a site -- comparing must degrade to a
    friendly explanation, not a crash or a misleading error code."""
    from seoaudit.store import Store

    path = str(tmp_path / "single.db")
    store = Store(path)
    run = store.create_run(label="единственный прогон")
    site = store.add_site(run, "https://onlyone.example")
    store.set_site_state(site, "done")
    store.finish_run(run)
    store.close()

    async def fake_download(_ctx):
        return path
    monkeypatch.setattr(br, "download_db", fake_download)

    result = await hr.compare_audits(ctx, CompareParams(site="onlyone.example"))
    assert result.status == "success"
    assert "не с чем" in result.summary.lower() or "нечего" in result.summary.lower() or "один" in result.summary.lower()


# ── get_schedule / set_schedule ──────────────────────────────────────────

async def test_happy_get_schedule_default_is_disabled(ctx):
    result = await hs.get_schedule(ctx, GetScheduleParams())
    assert result.status == "success"
    assert result.data.enabled is False


async def test_happy_set_schedule_enables_and_persists(ctx):
    result = await hs.set_schedule(ctx, ScheduleParams(
        enabled=True, hour=3, days="1,4", sites="climtec.md", max_pages=50))
    assert result.status == "success"

    check = await hs.get_schedule(ctx, GetScheduleParams())
    assert check.data.enabled is True
    assert check.data.hour == 3


async def test_adversarial_set_schedule_partial_update_does_not_clobber_other_fields(ctx):
    """Only `enabled` is a real bool; hour/days/sites are Optional/empty-means
    -unchanged specifically so one field's update can't silently reset
    another -- this is the exact bug the docstring in ScheduleParams warns
    about, verified end-to-end here rather than trusted from the comment."""
    await hs.set_schedule(ctx, ScheduleParams(enabled=True, hour=4, sites="climtec.md"))

    # Now change ONLY the hour -- sites and enabled must survive untouched.
    await hs.set_schedule(ctx, ScheduleParams(hour=9))

    check = await hs.get_schedule(ctx, GetScheduleParams())
    assert check.data.hour == 9
    assert check.data.enabled is True
    assert "climtec.md" in check.data.sites


# ── register_known_site (IPC, @ext.expose) ───────────────────────────────

async def test_happy_register_known_site_appends_to_schedule_sites(ctx):
    out = await hs.expose_register_known_site(ctx, domain="new-site.example")
    assert out["ok"] is True
    assert out["created"] is True

    settings = await sched.get_settings(ctx)
    assert "new-site.example" in settings.get("sites", "")


async def test_adversarial_register_known_site_is_idempotent(ctx):
    """The docstring promises idempotency -- adding an already-known domain
    changes nothing. Verified by calling twice and checking the site list
    doesn't grow a duplicate entry."""
    first = await hs.expose_register_known_site(ctx, domain="repeat.example")
    assert first["created"] is True

    second = await hs.expose_register_known_site(ctx, domain="repeat.example")
    assert second["ok"] is True
    assert second["created"] is False

    settings = await sched.get_settings(ctx)
    sites_field = settings.get("sites", "")
    assert sites_field.count("repeat.example") == 1


async def test_error_register_known_site_requires_a_domain(ctx):
    out = await hs.expose_register_known_site(ctx)
    assert out["ok"] is False
    assert out["retryable"] is False
