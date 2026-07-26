"""Тесты панелей — прежде всего СТРУКТУРНЫЕ.

Почему они здесь с первого дня, а не после аварии: в Notion Connector две
панели были объявлены на одном слоте `center`. Каждая рендерилась корректно в
изоляции, `imperal validate` дубликат слота пропускал — а в живой панели одна
молча вытесняла другую, и кнопка выглядела сломанной. Ошибку показывает только
КАРТА слотов, поэтому её и проверяем.
"""

from __future__ import annotations

import pytest

import panels


def _flatten(node):
    """Обойти дерево UI. У компонентов дети лежат в props['children']."""
    yield node
    props = getattr(node, "props", None) or {}
    kids = props.get("children") or []
    if not isinstance(kids, (list, tuple)):
        kids = [kids]
    for kid in kids:
        if hasattr(kid, "type"):
            yield from _flatten(kid)


def _dump(node) -> str:
    return " ".join(str(getattr(n, "props", "")) for n in _flatten(node))


# --- владение слотами -------------------------------------------------------

def test_at_most_one_panel_per_slot():
    """Две панели на одном слоте — структурная ошибка, не косметика.

    Центральный слот держит РОВНО ОДНУ панель с семантикой замены: без стека и
    вкладок. Хост забирает слоты одним пакетом при инициализации сессии, поэтому
    две панели на одном слоте гарантированно конфликтуют, а вызов, адресованный
    вытесненной, выглядит как «ничего не произошло».
    """
    import main  # noqa: F401  — регистрирует все панели
    from app import ext

    seen: dict[str, list[str]] = {}
    for panel_id, spec in ext.panels.items():
        slot = spec["slot"] if isinstance(spec, dict) else getattr(spec, "slot")
        seen.setdefault(slot, []).append(panel_id)

    clashes = {slot: ids for slot, ids in seen.items() if len(ids) > 1}
    assert not clashes, f"на один слот претендует больше одной панели: {clashes}"


def test_every_dispatched_panel_id_exists():
    """`ui.Call` на несуществующую панель ломается только при клике.

    Переименование панели — ровно тот случай, когда это происходит. Поэтому
    сверяем ВСЕ вызовы с реестром, а не надеемся на grep глазами.
    """
    import re

    import main  # noqa: F401
    from app import ext

    source = open("panels.py", encoding="utf-8").read()
    dispatched = set(re.findall(r'ui\.Call\(\s*"__panel__(\w+)"', source))
    unknown = dispatched - set(ext.panels)
    assert not unknown, f"вызываются панели, которых нет: {unknown}"


def test_refresh_panels_name_real_panels():
    """`refresh_panels` принимает БАРЕ-идентификаторы — опечатка молча ничего не обновит."""
    import re

    import main  # noqa: F401
    from app import ext

    named: set[str] = set()
    for name in ("handlers_audit.py", "handlers_read.py"):
        src = open(name, encoding="utf-8").read()
        for block in re.findall(r"refresh_panels=\[([^\]]*)\]", src):
            named |= set(re.findall(r'"(\w+)"', block))

    unknown = named - set(ext.panels)
    assert not unknown, f"в refresh_panels названы несуществующие панели: {unknown}"


# --- поведение при пустоте и сбоях ------------------------------------------

async def test_first_run_screen_tells_the_user_what_to_do(ctx):
    """До первого аудита панель обязана объяснить следующий шаг."""
    tree = await panels.seo_center(ctx)
    body = _dump(tree)
    # Ровно тот сценарий, который человек может повторить дословно.
    assert "проверь" in body.lower()


async def test_panel_shows_a_banner_when_loading_blows_up(ctx, monkeypatch):
    """Пустой экран хуже баннера — и внутренности не утекают в UI."""
    async def boom(*_a, **_k):
        raise RuntimeError("store exploded: /internal/path/secret.db")

    monkeypatch.setattr(panels.br, "download_db", boom)

    tree = await panels.seo_center(ctx)
    body = _dump(tree)

    assert "store exploded" not in body
    assert "/internal/path" not in body
    alerts = [n for n in _flatten(tree) if n.type == "Alert"]
    assert alerts, "сбой чтения должен давать баннер"


async def test_sidebar_renders_without_any_data(ctx):
    """Левый нав не должен зависеть от наличия прогонов."""
    tree = await panels.seo_nav(ctx)
    buttons = [n for n in _flatten(tree) if n.type == "Button"]
    assert buttons, "в сайдбаре должен быть хотя бы один вход"


async def test_unknown_view_does_not_produce_a_blank_screen(ctx):
    """Неизвестный view — это не белый экран, а разумный вид по умолчанию."""
    tree = await panels.seo_center(ctx, view="нет-такого-экрана")
    assert list(_flatten(tree)), "панель обязана что-то отрисовать"


def test_handlers_and_panels_share_one_bridge_module():
    """Все слои обязаны видеть ОДИН объект модуля `bridge`.

    Ловушка, которая стоила отладки. `main.py` намеренно чистит `sys.modules`
    перед импортом слоёв — иначе валидатор, грузящий несколько расширений в
    одном процессе, получил бы устаревшие модули без зарегистрированных
    декораторов. Побочный эффект: если что-то импортировало `bridge` ДО
    `main`, в процессе живут ДВА разных объекта одного модуля.

    Чем это опасно: подмена (в тестах) или monkeypatch применяется к одному
    объекту, а обработчик держит другой — и подделка молча не действует. В
    тестах это выглядело так: прогон «с подделанным движком» шёл 26 секунд,
    потому что движок на самом деле обходил реальный домен. Утверждения
    проходили, проверяя не тот путь. Худший вид зелёного теста.

    Здесь мы фиксируем инвариант прямо: слои смотрят на один и тот же модуль.
    """
    import main  # noqa: F401  — приводит модули к единому состоянию
    import bridge
    import handlers_audit
    import handlers_read
    import panels as panels_mod

    assert handlers_audit.br is bridge
    assert handlers_read.br is bridge
    assert panels_mod.br is bridge


def test_no_handler_invents_a_field_the_entity_does_not_declare():
    """Поля сущностей и то, что передают обработчики, должны совпадать.

    РЕАЛЬНЫЙ БАГ, найденный на живом прогоне. Обработчик заполнял
    `site_count=` и `average_score=`, а сущность объявляла `sites_total`,
    `sites_done`, `pages_checked`. Pydantic лишние ключи проглатывал, объявленные
    оставались по умолчанию — и ответ выглядел так: текст «проверено 1 из 1,
    найдено 8 проблем» рядом с нулями во всех числовых полях.

    Почему это не поймали ни валидатор, ни остальные тесты: валидатор проверяет
    ОБЪЯВЛЕНИЕ data_model, а не то, чем его заполняют; тесты рендеринга смотрят
    на текст, который собирается из локальных переменных и потому верен. Ошибку
    видно только при сверке имён — этим и занят этот тест.
    """
    import ast
    import pathlib

    import main  # noqa: F401
    import models

    entities = {name: cls for name, cls in vars(models).items()
                if isinstance(cls, type) and hasattr(cls, "model_fields")}

    problems: list[str] = []
    root = pathlib.Path(__file__).resolve().parent.parent
    for fname in ("handlers_audit.py", "handlers_read.py", "panels.py"):
        tree = ast.parse((root / fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            entity = entities.get(node.func.id)
            if entity is None:
                continue
            declared = set(entity.model_fields)
            for kw in node.keywords:
                if kw.arg and kw.arg not in declared:
                    problems.append(
                        f"{fname}:{node.lineno} {node.func.id}(...{kw.arg}=...) "
                        f"— поле не объявлено")

    assert not problems, "обработчик заполняет несуществующие поля:\n" + "\n".join(problems)
