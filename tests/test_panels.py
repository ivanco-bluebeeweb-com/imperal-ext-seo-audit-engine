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


# --- главное действие: «Добавить сайт» ---------------------------------------

def _buttons(node) -> list[str]:
    """Подписи всех кнопок в дереве."""
    return [(getattr(n, "props", {}) or {}).get("label") or ""
            for n in _flatten(node) if n.type == "Button"]


def _fake_load(result: dict):
    """Подменить чтение портфеля — панель не должна ходить в хранилище."""
    async def loader(ctx, min_severity: str = "medium"):
        return result
    return loader


_STATES = {
    "первый запуск": {"first_run": True, "rows": []},
    "сбой чтения": {"problem": "Не удалось прочитать результаты", "rows": []},
    "есть данные": {
        "run_id": 2, "label": "", "tasks_by_site": {},
        "rows": [{"origin": "https://climtec.md", "state": "done", "score": 87,
                  "pages": 12, "tasks": 3, "top_issue": "Кеш устарел",
                  "by_severity": {"high": 1}, "rules": ["cache.serving_stale"]}],
    },
}


@pytest.mark.parametrize("state", list(_STATES))
async def test_add_site_button_is_always_in_the_sidebar(ctx, monkeypatch, state):
    """«Добавить сайт» обязана быть в сайдбаре в ЛЮБОМ состоянии.

    Раньше сайдбар предлагал только «Открыть портфель» и «Задачи» — добавить
    новый сайт было неоткуда, а при сбое чтения он вырождался в строку
    «Результаты недоступны» вообще без действий. То есть ровно в двух
    состояниях, где помощь нужнее всего (ничего ещё нет / всё сломалось),
    приложение не давало сделать главное.

    Поэтому кнопка собирается ДО чтения данных и вне всяких ветвлений, а этот
    тест перебирает все три состояния и падает, если она где-то исчезла.
    """
    monkeypatch.setattr(panels, "_load", _fake_load(_STATES[state]))

    labels = _buttons(await panels.seo_nav(ctx))

    assert any("Добавить сайт" in lbl for lbl in labels), \
        f"в состоянии «{state}» кнопки нет; есть только: {labels}"


@pytest.mark.parametrize("state", list(_STATES))
async def test_add_view_opens_a_real_form_in_every_state(ctx, monkeypatch, state):
    """Кнопка обязана открывать форму, а не проваливаться в «пусто».

    view=\"add\" обрабатывается ПЕРВЫМ, до проверок на пустоту и до баннера
    ошибки: форма ввода домена не зависит от прошлых результатов.
    """
    monkeypatch.setattr(panels, "_load", _fake_load(_STATES[state]))

    tree = await panels.seo_center(ctx, view="add")

    assert any(n.type == "Form" for n in _flatten(tree)), \
        f"в состоянии «{state}» экран добавления без формы"


def test_the_add_form_submits_to_a_function_this_extension_owns():
    """Форма должна звать функцию ЭТОГО расширения, с верным именем поля.

    `action=` панельной формы резолвится против функций РЕНДЕРЯЩЕГО расширения.
    Ссылка на чужую функцию падает уже на клике («Function not found»), а
    неверное имя поля тихо не донесёт значение до инструмента — оба случая
    видны только в живой панели, если не проверить здесь.
    """
    import main  # noqa: F401
    from app import chat

    tree = panels._add_view({"rows": []})
    forms = [n for n in _flatten(tree) if n.type == "Form"]
    assert forms, "экран добавления обязан содержать форму"

    action = (forms[0].props or {}).get("action")
    functions = getattr(chat, "_functions", None) or {}
    assert action in functions, \
        f"форма ссылается на '{action}', которой нет среди {sorted(functions)}"

    # Имя поля должно совпадать с параметром инструмента.
    names = {(n.props or {}).get("param_name")
             for n in _flatten(forms[0]) if n.type in ("Input", "Password")}
    from models import AuditSitesParams
    assert names <= set(AuditSitesParams.model_fields), \
        f"поля {names} не существуют в параметрах audit_sites"


# --- раскладка --------------------------------------------------------------

def test_stacks_use_the_direction_values_the_sdk_understands():
    """direction принимает только \"v\"/\"h\" — не \"vertical\".

    РЕАЛЬНЫЙ БАГ: все семь экранов передавали direction=\"vertical\". SDK
    объявляет значение по умолчанию \"v\", а Row/Column внутри SDK используют
    строго \"h\"/\"v\". Значение уходит во фронтенд без валидации — ни SDK, ни
    `imperal validate` не возражают, поэтому вертикальная раскладка ломалась
    молча, а поймать это можно только сверкой значений.
    """
    import main  # noqa: F401

    trees = [panels._first_run(), panels._banner("тест"),
             panels._add_view({"rows": []}),
             panels._portfolio_view(_STATES["есть данные"]),
             panels._findings_view(_STATES["есть данные"])]

    for tree in trees:
        for node in _flatten(tree):
            if node.type != "Stack":
                continue
            direction = (node.props or {}).get("direction", "v")
            assert direction in ("v", "h"), \
                f"недопустимое direction={direction!r} — SDK знает только 'v'/'h'"


def test_every_ui_call_matches_the_sdk_signature():
    """Ни один вызов ui.* не должен передавать несуществующий аргумент.

    Этот тест — обобщение ДВУХ уже случившихся багов:
      * ui.Header(title=...) — на самом деле первый аргумент называется `text`;
        панель падала с TypeError при рендере;
      * direction="vertical" — SDK знает только "v"/"h", значение уходило во
        фронтенд без валидации и раскладка ломалась молча.

    Общее у них то, что `imperal validate` их не видит: он проверяет контракт
    расширения, а не соответствие вызовов ui.* сигнатурам SDK. Поэтому сверяем
    сами — по фактической сигнатуре, а не по памяти.
    """
    import ast
    import inspect
    import pathlib

    from imperal_sdk import ui

    problems: list[str] = []
    source = pathlib.Path(__file__).resolve().parent.parent / "panels.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "ui"):
            continue

        fn = getattr(ui, node.func.attr, None)
        if fn is None:
            problems.append(f"строка {node.lineno}: ui.{node.func.attr} не существует")
            continue
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        if any(p.kind == p.VAR_KEYWORD for p in params.values()):
            continue
        for kw in node.keywords:
            if kw.arg and kw.arg not in params:
                problems.append(
                    f"строка {node.lineno}: ui.{node.func.attr}(...{kw.arg}=...) "
                    f"— такого аргумента нет")

    assert not problems, "вызовы ui.* расходятся с SDK:\n  " + "\n  ".join(problems)
