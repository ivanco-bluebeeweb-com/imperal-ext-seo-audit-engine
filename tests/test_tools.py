"""Тесты обвязки: перевод состояния базы в ответ пользователю.

Сеть здесь не участвует — правила движка покрыты его собственными 28 тестами
на подготовленном HTML. Здесь проверяется ИМЕННО ОБВЯЗКА: что пустой результат
читается как «всё в порядке», а не как поломка; что «аудитов не было» и «база
не читается» — РАЗНЫЕ ответы (совет пользователю в этих случаях разный); что
долгий прогон уходит в фон, а без kernel-хука всё равно выполняется.
"""

from __future__ import annotations

import pytest

import bridge as br
import codes as c


# --- разбор ввода -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("climtec.md", ["https://climtec.md"]),
    ("climtec.md, ksrenovationgroup.com",
     ["https://climtec.md", "https://ksrenovationgroup.com"]),
    ("climtec.md\nksrenovationgroup.com\n",
     ["https://climtec.md", "https://ksrenovationgroup.com"]),
    ("https://climtec.md", ["https://climtec.md"]),   # уже со схемой — не трогаем
    ("http://climtec.md", ["http://climtec.md"]),     # http сохраняем как есть
    ("", []),
])
def test_sites_are_parsed_the_way_people_type_them(raw, expected):
    """Человек пишет домены как удобно, а движку нужен полный origin.

    РЕАЛЬНЫЙ БАГ, найденный на живом прогоне. `normalize_url` внутри движка
    опирается на `urlsplit`, а тот в строке "climtec.md" видит ПУТЬ, а не хост:
    hostname пустой, функция возвращает ввод как есть. Движок затем склеивал
    схему с таким origin и получалось `https:///climtec.md` — три слэша. В
    отчёте появился неоткрываемый адрес.

    Схему дописываем на границе ВВОДА, а не внутри `normalize_url`: та же
    функция применяется к ссылкам, найденным на страницах, где строка без схемы
    — это законный относительный путь, и додумывать ей схему было бы ошибкой.
    """
    assert br.parse_sites(raw) == expected


def test_a_bare_domain_never_becomes_a_triple_slash_origin():
    """Ровно тот дефект, который испортил первый живой отчёт."""
    for raw in ("climtec.md", "www.climtec.md", "climtec.md/"):
        origin = br.parse_sites(raw)[0]
        assert not origin.startswith("https:///"), origin
        assert origin.startswith("https://")
        # и origin обязан быть открываемым: хост непустой
        from urllib.parse import urlsplit
        assert urlsplit(origin).hostname, origin


def test_a_site_is_not_audited_twice_in_one_run():
    """Дубль в списке — не два прогона одного сайта.

    Иначе пользователь, вставивший список с повтором, заплатил бы двойным
    временем обхода чужого сайта и получил бы удвоенные находки.
    """
    assert len(br.parse_sites("a.com, A.COM, a.com")) == 1


# --- «аудитов не было» — это не поломка -------------------------------------

async def test_reading_results_before_any_audit_explains_what_to_do(ctx):
    """До первого аудита инструмент обязан подсказать следующий шаг."""
    import handlers_read as hr
    from models import ListFindingsParams

    result = await hr.list_findings(ctx, ListFindingsParams())

    assert result.status == "error"
    assert result.error_code == c.SEO_NO_RUNS
    # В тексте должно быть действие, а не только констатация.
    assert "проверить" in (result.error or "").lower()


async def test_resume_without_any_audit_says_so_plainly(ctx):
    """«Продолжить» без прогонов — понятный отказ, а не трассировка."""
    import handlers_audit as ha
    from models import ResumeAuditParams

    result = await ha.resume_audit(ctx, ResumeAuditParams())

    assert result.status == "error"
    assert result.error_code == c.SEO_NO_RUNS


async def test_audit_without_sites_names_the_missing_thing(ctx):
    """Пустой список сайтов — отказ до всякой работы."""
    import handlers_audit as ha
    from models import AuditSitesParams

    result = await ha.audit_sites(ctx, AuditSitesParams(sites="   "))

    assert result.status == "error"
    assert result.error_code == c.SEO_NO_SITES


# --- порог важности проверяется до запуска ----------------------------------

def test_a_typo_in_severity_is_refused_not_silently_ignored():
    """«critcal» не должен молча отфильтровать всё и показать пустой список."""
    from pydantic import ValidationError
    from models import ListFindingsParams

    with pytest.raises(ValidationError):
        ListFindingsParams(min_severity="critcal")


def test_empty_severity_falls_back_to_a_sane_default():
    from models import ListFindingsParams

    assert ListFindingsParams(min_severity="").min_severity == "medium"


# --- фоновый прогон ---------------------------------------------------------

async def test_a_long_audit_is_handed_to_the_background(ctx, monkeypatch):
    """Чат не должен блокироваться на минуты.

    Федеральный предел одного вызова — 180 с, а аудит 200 сайтов идёт до 22
    минут. Поэтому инструмент обязан вернуть подтверждение сразу и уйти в фон
    с long_running=True (предел 1800 с).
    """
    import handlers_audit as ha
    from models import AuditSitesParams

    def fake_run(db_path, origins, **kw):
        return 1   # прогон «выполнился», в сеть не ходим

    monkeypatch.setattr(ha.br, "run_audit_blocking", fake_run)
    monkeypatch.setattr(ha.br, "site_rows", lambda *a, **k: ([], {}))
    monkeypatch.setattr(ha.br, "upload_db", _noop_upload)

    result = await ha.audit_sites(ctx, AuditSitesParams(sites="example.com"))

    assert result.status == "success"
    assert ctx.spawned, "аудит должен уходить в фоновую задачу"
    assert ctx.spawned[0]["long_running"] is True


async def test_the_ack_promises_a_second_message(ctx, monkeypatch):
    """Пользователь должен знать, что итог придёт отдельно."""
    import handlers_audit as ha
    from models import AuditSitesParams

    monkeypatch.setattr(ha.br, "run_audit_blocking", lambda *a, **k: 1)
    monkeypatch.setattr(ha.br, "site_rows", lambda *a, **k: ([], {}))
    monkeypatch.setattr(ha.br, "upload_db", _noop_upload)

    result = await ha.audit_sites(ctx, AuditSitesParams(sites="example.com"))

    text = (result.summary or "") + " " + str(getattr(result, "description", "") or "")
    assert "итог" in text.lower() or "пришлю" in text.lower()


async def test_audit_still_runs_when_the_platform_has_no_background_hook(
        no_background, monkeypatch):
    """Без kernel-хука инструмент обязан работать, а не падать из-за среды.

    Это локальный прогон и dev-режим: SDK документирует RuntimeError, а у
    тестового контекста метода может не быть вовсе.
    """
    import handlers_audit as ha
    from models import AuditSitesParams

    monkeypatch.setattr(ha.br, "run_audit_blocking", lambda *a, **k: 1)
    monkeypatch.setattr(ha.br, "site_rows", lambda *a, **k: ([], {}))
    monkeypatch.setattr(ha.br, "upload_db", _noop_upload)

    result = await ha.audit_sites(no_background, AuditSitesParams(sites="example.com"))

    assert result.status == "success"


async def _noop_upload(ctx, path):
    """Выгрузку базы в тестах не делаем — проверяется не она."""
    return None


# --- оценка времени ---------------------------------------------------------

def test_the_estimate_grows_with_the_portfolio():
    """Оценка нужна, чтобы управлять ожиданием, а не предсказать секунды."""
    one = br.estimate_minutes(1, 50)
    many = br.estimate_minutes(200, 50)
    assert one != many
    assert "мин" in many


def test_the_estimate_survives_an_empty_portfolio():
    assert br.estimate_minutes(0, 50)


# --- ошибки всегда со структурным кодом -------------------------------------

def test_every_error_path_carries_a_code():
    """Ошибка без кода получает от ядра штамп EXT_UNSTRUCTURED_ERROR.

    Это прямой урок из WP Publisher: точный сбой превращался в прозу, по
    которой пользователь ничего не мог сделать. Валидатор ловит только
    буквальные `ActionResult.error(`, поэтому приложение со своим хелпером для
    правила невидимо — и проверять это приходится тестом.
    """
    import ast
    import pathlib

    offenders: list[str] = []
    for path in pathlib.Path(".").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # прямой ActionResult.error(...) без code=
            if (isinstance(fn, ast.Attribute) and fn.attr == "error"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "ActionResult"):
                if not any(k.arg == "code" for k in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno} ActionResult.error без code")
            # наш хелпер: код обязателен позиционно, значит нужно >= 2 аргумента
            if isinstance(fn, ast.Name) and fn.id == "_error":
                if len(node.args) < 2 and not any(k.arg == "code" for k in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno} _error без кода")

    assert not offenders, "пути ошибок без структурного кода: " + "; ".join(offenders)
