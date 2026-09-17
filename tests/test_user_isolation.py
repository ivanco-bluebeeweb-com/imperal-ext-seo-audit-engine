"""Comprehensive multi-user isolation tests for SEO Audit Engine.

Verifies:
1. Schedule settings for user_A and user_B are strictly isolated.
2. Sites Registry IPC register_known_site only updates the calling user's schedule.
3. System scheduler seo_auto_audit fans out per user and does not mix user contexts.
4. store_run_summary isolates runs per user_id.
"""
from copy import copy
import pytest
from imperal_sdk.testing import MockContext, MockSecretStore
from imperal_sdk.types.identity import UserContext

import schedule_settings as sched
import handlers_schedule as hs
import shared
import bridge as br


def make_user_ctx(user_id: str):
    ctx = MockContext(user_id=user_id)
    ctx.secrets = MockSecretStore({})
    ctx.user = UserContext(
        imperal_id=user_id,
        email=f"{user_id}@test.com",
        tenant_id="default",
        role="user",
    )
    return ctx


@pytest.mark.asyncio
async def test_schedule_settings_multi_user_isolation():
    """Two users in the same store have separate schedule settings."""
    ctx_a = make_user_ctx("user_alice")
    ctx_b = make_user_ctx("user_bob")

    # Share the underlying in-memory store (same tenant)
    ctx_b.store = ctx_a.store

    await sched.set_settings(
        ctx_a,
        enabled=True,
        hour=4,
        sites="alice-shop.com, alice-blog.com",
        reason="Alice setup",
    )

    await sched.set_settings(
        ctx_b,
        enabled=False,
        hour=8,
        sites="bob-security.md",
        reason="Bob setup",
    )

    settings_a = await sched.get_settings(ctx_a)
    settings_b = await sched.get_settings(ctx_b)

    assert settings_a["enabled"] is True
    assert settings_a["hour"] == 4
    assert "alice-shop.com" in settings_a["sites"]
    assert "bob-security.md" not in settings_a["sites"]

    assert settings_b["enabled"] is False
    assert settings_b["hour"] == 8
    assert "bob-security.md" in settings_b["sites"]
    assert "alice-shop.com" not in settings_b["sites"]


@pytest.mark.asyncio
async def test_register_known_site_ipc_isolation():
    """register_known_site only adds the domain to the context's user."""
    ctx_a = make_user_ctx("user_alice")
    ctx_b = make_user_ctx("user_bob")
    ctx_b.store = ctx_a.store

    await hs.expose_register_known_site(ctx_a, site_id="alice-newsite.com", domain="alice-newsite.com")

    settings_a = await sched.get_settings(ctx_a)
    settings_b = await sched.get_settings(ctx_b)

    assert "alice-newsite.com" in settings_a["sites"]
    assert "alice-newsite.com" not in settings_b["sites"]


@pytest.mark.asyncio
async def test_store_run_summary_user_isolation():
    """store_run_summary tags document with user_id and updates correctly."""
    ctx_a = make_user_ctx("user_alice")
    ctx_b = make_user_ctx("user_bob")
    ctx_b.store = ctx_a.store

    await shared.store_run_summary(ctx_a, 1, {"run_id": 1, "label": "Alice run 1", "score": 85})
    await shared.store_run_summary(ctx_b, 1, {"run_id": 1, "label": "Bob run 1", "score": 92})

    page_a = await ctx_a.store.query(br.RUNS_COLLECTION, where={"run_id": 1, "user_id": "user_alice"})
    page_b = await ctx_b.store.query(br.RUNS_COLLECTION, where={"run_id": 1, "user_id": "user_bob"})

    assert len(page_a.data) == 1
    assert page_a.data[0].data["label"] == "Alice run 1"
    assert page_a.data[0].data["user_id"] == "user_alice"

    assert len(page_b.data) == 1
    assert page_b.data[0].data["label"] == "Bob run 1"
    assert page_b.data[0].data["user_id"] == "user_bob"


@pytest.mark.asyncio
async def test_system_schedule_fanout(monkeypatch):
    """System schedule iterates users with settings and executes per user."""
    ctx_sys = MockContext(user_id="__system__")
    ctx_sys.user = UserContext(
        imperal_id="__system__",
        email="system@imperal.io",
        tenant_id="default",
        role="system",
    )

    # Set up settings for alice and bob in the shared store
    ctx_a = make_user_ctx("user_alice")
    ctx_b = make_user_ctx("user_bob")
    ctx_a.store = ctx_sys.store
    ctx_b.store = ctx_sys.store

    # Alice is due at hour 3 on Mondays
    await sched.set_settings(ctx_a, enabled=True, hour=3, days="1", sites="alice.com")
    # Bob is disabled
    await sched.set_settings(ctx_b, enabled=False, hour=3, days="1", sites="bob.com")

    audited_users = []

    real_run = hs._run_audit_for_user
    async def fake_run_audit_for_user(u_ctx):
        ok, reason = await sched.due(u_ctx)
        if ok:
            s = await sched.get_settings(u_ctx)
            audited_users.append((u_ctx.user.imperal_id, s.get("sites")))

    monkeypatch.setattr(hs, "_run_audit_for_user", fake_run_audit_for_user)
    monkeypatch.setattr(sched, "_now_parts", lambda ts=None: ("2026-09-21", 3, 0)) # Monday 3 AM

    await hs.seo_auto_audit(ctx_sys)

    # Only Alice should have been audited, with Alice's sites
    assert len(audited_users) == 1
    assert audited_users[0][0] == "user_alice"
    assert audited_users[0][1] == "alice.com"
    # validated
