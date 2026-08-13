"""Tests for the register_known_site IPC surface Sites Registry calls into
whenever a site is registered there, per the platform-wide rule: a site
added to Sites Registry or WordPress Hub must show up as a known site here
without the user re-adding it by hand -- and, just as importantly, WITHOUT
ever triggering an audit or flipping the schedule on by itself.
"""
import pytest

import handlers_schedule as hs
import schedule_settings as sched


@pytest.mark.asyncio
async def test_register_known_site_adds_a_new_domain(ctx):
    result = await hs.expose_register_known_site(ctx, site_id="climtec.md", domain="climtec.md")
    assert result == {"ok": True, "site_id": "climtec.md", "created": True}

    settings = await sched.get_settings(ctx)
    assert "climtec.md" in settings["sites"]


@pytest.mark.asyncio
async def test_register_known_site_is_idempotent(ctx):
    await hs.expose_register_known_site(ctx, site_id="climtec.md", domain="climtec.md")
    result = await hs.expose_register_known_site(ctx, site_id="climtec.md", domain="climtec.md")
    assert result == {"ok": True, "site_id": "climtec.md", "created": False}

    settings = await sched.get_settings(ctx)
    assert settings["sites"].count("climtec.md") == 1


@pytest.mark.asyncio
async def test_register_known_site_normalizes_scheme_and_www(ctx):
    await hs.expose_register_known_site(ctx, domain="https://www.climtec.md/")
    result = await hs.expose_register_known_site(ctx, domain="climtec.md")
    assert result["created"] is False


@pytest.mark.asyncio
async def test_register_known_site_never_enables_the_schedule(ctx):
    """A site landing in the registry is not an explicit ask to start
    auditing it -- this app only ever touches someone else's site on an
    explicit ask."""
    await hs.expose_register_known_site(ctx, site_id="climtec.md")
    settings = await sched.get_settings(ctx)
    assert settings["enabled"] is False


@pytest.mark.asyncio
async def test_register_known_site_requires_site_id_or_domain(ctx):
    result = await hs.expose_register_known_site(ctx)
    assert result["ok"] is False
