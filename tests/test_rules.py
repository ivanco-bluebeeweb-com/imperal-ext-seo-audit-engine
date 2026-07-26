"""Тесты правил аудита.

Эталоны — РЕАЛЬНЫЕ дефекты живого сайта, а не выдуманные случаи.
Главный: русская главная climtec.md объявляла каноническим адрес
румынской версии, то есть сама просила Google себя не индексировать.
Если правило перестанет это ловить — тест упадёт.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seoaudit.extract import parse_head
from seoaudit.rules import run_page_rules, run_site_rules
from seoaudit.tasks import build_tasks


def page(url: str, html: str, **over):
    """Собирает запись страницы так же, как её собирает движок."""
    head = parse_head(html, url)
    item = {
        "url": url, "final_url": url, "status": 200, "redirects": 0,
        "chain": [], "elapsed_ms": 300, "bytes": len(html),
        "content_type": "text/html", "error": "", "state": "done",
        "cache_state": "", "cache_layer": "", "cache_stale": 0,
        "title": head.title, "description": head.description,
        "canonical": head.canonical, "lang": head.html_lang, "h1": head.h1,
        "noindex": head.is_noindex, "word_count": head.word_count,
        "images_no_alt": head.images_no_alt,
        "hreflang": head.hreflang, "links_internal": head.links_internal,
    }
    item.update(over)
    return item, head


def keys(found):
    return {f.key for f in found}


HTML_RU_HOME_BROKEN = """<!doctype html>
<html lang="ru-RU"><head>
<title>Рекуператоры воздуха в Молдове — Climtec</title>
<meta name="description" content="Приточно-вытяжные установки с рекуперацией тепла для квартир и домов. Подбор по площади, монтаж под ключ, гарантия и сервис в Кишинёве.">
<link rel="canonical" href="https://climtec.md/">
<link rel="alternate" hreflang="ro-RO" href="https://climtec.md/">
<link rel="alternate" hreflang="ru-RU" href="https://climtec.md/ru/home-ru/">
</head><body><h1>Рекуператоры воздуха</h1>
<p>Текст страницы про рекуператоры и вентиляцию.</p></body></html>"""


class TestCanonicalCrossLanguage(unittest.TestCase):
    """Реальный дефект: RU-главная канонична на RO-главную."""

    def test_ловит_канонический_на_чужой_язык(self):
        item, head = page("https://climtec.md/ru/home-ru/", HTML_RU_HOME_BROKEN)
        found = run_page_rules(item, head, {})
        self.assertIn("canonical.cross_language", keys(found),
                      "не поймали canonical на другую языковую версию")

    def test_это_критично_и_чинится_автоматически(self):
        item, head = page("https://climtec.md/ru/home-ru/", HTML_RU_HOME_BROKEN)
        f = [x for x in run_page_rules(item, head, {})
             if x.key == "canonical.cross_language"][0]
        self.assertEqual(f.severity, "critical")
        self.assertTrue(f.fixable, "правится через коннектор — должно быть fixable")

    def test_исправленный_вариант_не_срабатывает(self):
        fixed = HTML_RU_HOME_BROKEN.replace(
            '<link rel="canonical" href="https://climtec.md/">',
            '<link rel="canonical" href="https://climtec.md/ru/home-ru/">')
        item, head = page("https://climtec.md/ru/home-ru/", fixed)
        self.assertNotIn("canonical.cross_language", keys(run_page_rules(item, head, {})),
                         "ложное срабатывание на корректной странице")


class TestCanonicalБазовые(unittest.TestCase):

    def test_нет_canonical(self):
        item, head = page("https://x.md/p/", "<html lang='ru'><head><title>Заголовок страницы достаточной длины тут</title></head><body><h1>Ок</h1></body></html>")
        self.assertIn("canonical.missing", keys(run_page_rules(item, head, {})))

    def test_canonical_на_чужой_домен(self):
        html = """<html lang="ru"><head><title>Нормальный заголовок страницы сайта тут</title>
        <link rel="canonical" href="https://other-domain.com/p/"></head><body><h1>Ок</h1></body></html>"""
        item, head = page("https://x.md/p/", html)
        found = keys(run_page_rules(item, head, {}))
        self.assertIn("canonical.cross_host", found)

    def test_два_тега_canonical(self):
        html = """<html lang="ru"><head><title>Нормальный заголовок страницы сайта тут</title>
        <link rel="canonical" href="https://x.md/p/">
        <link rel="canonical" href="https://x.md/other/"></head><body><h1>Ок</h1></body></html>"""
        item, head = page("https://x.md/p/", html)
        self.assertIn("canonical.duplicate_tag", keys(run_page_rules(item, head, {})))


class TestМетаданные(unittest.TestCase):

    def test_нет_заголовка(self):
        item, head = page("https://x.md/p/", "<html lang='ru'><head></head><body><h1>Ок</h1></body></html>")
        self.assertIn("title.missing", keys(run_page_rules(item, head, {})))

    def test_короткий_заголовок(self):
        item, head = page("https://x.md/p/", "<html lang='ru'><head><title>Коротко</title></head><body><h1>Ок</h1></body></html>")
        self.assertIn("title.too_short", keys(run_page_rules(item, head, {})))

    def test_нет_описания(self):
        item, head = page("https://x.md/p/", "<html lang='ru'><head><title>Достаточно длинный заголовок страницы вот</title></head><body><h1>Ок</h1></body></html>")
        self.assertIn("description.missing", keys(run_page_rules(item, head, {})))

    def test_нормальные_метаданные_молчат(self):
        html = """<html lang="ru"><head>
        <title>Рекуператоры воздуха в Молдове — подбор и монтаж</title>
        <meta name="description" content="Приточно-вытяжные установки с рекуперацией тепла для квартир и домов. Подбор по площади, монтаж под ключ, гарантия и сервис в Кишинёве всегда.">
        <link rel="canonical" href="https://x.md/p/"></head>
        <body><h1>Рекуператоры</h1><p>Текст.</p></body></html>"""
        item, head = page("https://x.md/p/", html)
        found = keys(run_page_rules(item, head, {}))
        for k in ("title.missing", "title.too_short", "title.too_long",
                  "description.missing", "canonical.missing", "h1.missing"):
            self.assertNotIn(k, found, f"ложное срабатывание {k}")


class TestH1(unittest.TestCase):
    """Реальный дефект climtec: на румынских постах H1 отсутствует."""

    def test_нет_h1(self):
        html = """<html lang="ro"><head><title>Face Zgomot Recuperatorul Noaptea Acasa</title>
        <link rel="canonical" href="https://x.md/p/"></head>
        <body><p>Text fara titlu principal.</p></body></html>"""
        item, head = page("https://x.md/p/", html)
        self.assertIn("h1.missing", keys(run_page_rules(item, head, {})))

    def test_несколько_h1(self):
        html = """<html lang="ru"><head><title>Достаточно длинный заголовок страницы вот</title>
        <link rel="canonical" href="https://x.md/p/"></head>
        <body><h1>Первый</h1><h1>Второй</h1></body></html>"""
        item, head = page("https://x.md/p/", html)
        self.assertIn("h1.multiple", keys(run_page_rules(item, head, {})))


class TestКеш(unittest.TestCase):
    """Реальная находка: LiteSpeed неделю отдавал устаревший HTML."""

    def test_устаревший_кеш_виден_как_находка_сайта(self):
        pages = []
        for i in range(3):
            item, _ = page(f"https://x.md/p{i}/", HTML_RU_HOME_BROKEN,
                           cache_stale=1, cache_state="hit", cache_layer="LiteSpeed")
            pages.append(item)
        found = run_site_rules(pages, {"origin": "https://x.md"})
        self.assertIn("cache.serving_stale", keys(found))

    def test_свежий_кеш_молчит(self):
        pages = []
        for i in range(3):
            item, _ = page(f"https://x.md/p{i}/", HTML_RU_HOME_BROKEN,
                           cache_stale=0, cache_state="hit", cache_layer="LiteSpeed")
            pages.append(item)
        self.assertNotIn("cache.serving_stale",
                         keys(run_site_rules(pages, {"origin": "https://x.md"})))


class TestДубли(unittest.TestCase):

    def test_одинаковый_заголовок_на_двух_страницах(self):
        html = """<html lang="ru"><head><title>Один и тот же заголовок на двух стр</title>
        <link rel="canonical" href="https://x.md/{n}/"></head><body><h1>Ок</h1></body></html>"""
        pages = [page(f"https://x.md/{n}/", html.replace("{n}", str(n)))[0]
                 for n in (1, 2)]
        self.assertIn("duplicate.title",
                      keys(run_site_rules(pages, {"origin": "https://x.md"})))


class TestГруппировкаЗадач(unittest.TestCase):
    """Ключевое требование: 200 сайтов не должны дать 6000 задач."""

    def _findings(self, n: int):
        return [{
            "rule": "description.missing", "layer": 4, "severity": "medium",
            "effort": 1, "url": f"https://x.md/p{i}/",
            "message": "Нет описания страницы", "detail": "", "fixable": 1,
            "evidence": "{}",
        } for i in range(n)]

    def test_двадцать_находок_одного_правила_дают_одну_задачу(self):
        tasks = build_tasks("https://x.md", self._findings(20))
        same = [t for t in tasks if t.rule == "description.missing"]
        self.assertEqual(len(same), 1, "правило должно давать ОДНУ задачу на сайт")
        self.assertEqual(same[0].count, 20, "все 20 страниц должны быть внутри задачи")
        self.assertEqual(len(same[0].urls), 20)

    def test_порог_важности_отсекает_мелочь(self):
        low = [{"rule": "images.missing_alt", "layer": 6, "severity": "low",
                "effort": 2, "url": "https://x.md/p/", "message": "Нет alt",
                "detail": "", "fixable": 0, "evidence": "{}"}]
        self.assertEqual(len(build_tasks("https://x.md", low, min_severity="medium")), 0)
        self.assertEqual(len(build_tasks("https://x.md", low, min_severity="low")), 1)

    def test_критичное_идёт_первым(self):
        items = self._findings(2) + [{
            "rule": "canonical.cross_language", "layer": 1, "severity": "critical",
            "effort": 1, "url": "https://x.md/ru/", "message": "Канонический на чужой язык",
            "detail": "", "fixable": 1, "evidence": "{}"}]
        tasks = build_tasks("https://x.md", items)
        self.assertEqual(tasks[0].severity, "critical",
                         "критичное обязано быть первым в списке работ")

    def test_у_задачи_есть_инструкция_и_проверка(self):
        t = build_tasks("https://x.md", self._findings(3))[0]
        self.assertIn("ЧТО СДЕЛАТЬ", t.body)
        self.assertIn("КАК ПРОВЕРИТЬ", t.body)
        self.assertTrue(t.due_days > 0)


class TestНеСудитьНезагруженное(unittest.TestCase):
    """Регресс: правила не должны судить страницы, которых не видели.

    Реальный случай: на icnli.org определитель кодировки склеивал имя
    кодировки с соседним тегом, ни одна страница не скачивалась, но аудит
    выдавал 35 находок «нет заголовка / нет описания / нет H1» по пустым
    записям. Уверенный приговор по неувиденному хуже отсутствия аудита.
    """

    def test_кодировка_не_склеивается_с_разметкой(self):
        from seoaudit.fetcher import _charset_from
        # тег без пробелов — именно так написан icnli.org
        got = _charset_from("text/html", b'<meta charset="utf-8"/><meta name=viewport content="x">')
        self.assertEqual(got, "utf-8")

    def test_неизвестная_кодировка_не_ломает_загрузку(self):
        from seoaudit.fetcher import _charset_from
        # кривое значение должно откатиться к utf-8, а не всплыть LookupError
        self.assertEqual(_charset_from("text/html", b'<meta charset="bogus-xyz">'), "utf-8")
        self.assertEqual(_charset_from("text/html; charset=nonsense99", b""), "utf-8")

    def test_пустая_страница_без_head_не_даёт_приговоров(self):
        """Запись без разобранного head не должна порождать находки о контенте."""
        from seoaudit.extract import HeadData
        from seoaudit.rules import run_page_rules
        item = {
            "url": "https://x.md/p/", "final_url": "https://x.md/p/",
            "status": None, "state": "queued", "redirects": 0, "chain": [],
            "elapsed_ms": 0, "bytes": 0, "content_type": "", "error": "",
            "cache_state": "", "cache_layer": "", "cache_stale": 0,
        }
        found = run_page_rules(item, HeadData(), {})
        # по незагруженной странице не должно быть претензий к её содержанию
        content_claims = {"title.missing", "description.missing", "h1.missing",
                          "canonical.missing", "content.thin"}
        self.assertEqual(keys(found) & content_claims, set())


if __name__ == "__main__":
    unittest.main()


class TestРазделениеЗадач(unittest.TestCase):
    """Регресс: разные предметы одного правила = РАЗНЫЕ задачи.

    Реальный случай на ksrenovationgroup.com: «раздел inspiration (6 стр.)
    не в карте сайта» и «раздел project (4 стр.)» склеились в одну задачу —
    заголовок от первой, ссылки от обеих. Исполнитель половину работы
    просто не видел в заголовке.
    """

    def _finding(self, rule, section, urls, sev="high"):
        return {
            "rule": rule, "severity": sev, "layer": 2, "effort": 2,
            "url": urls[0], "message": f"Раздел «{section}» ({len(urls)} стр.) отсутствует в карте сайта",
            "detail": "", "fixable": 0,
            "evidence": json.dumps({"section": section, "count": len(urls), "urls": urls}),
        }

    def test_разные_разделы_дают_разные_задачи(self):
        rows = [
            self._finding("sitemap.section_absent", "inspiration",
                          [f"https://x.com/inspiration/{i}/" for i in range(6)]),
            self._finding("sitemap.section_absent", "project",
                          [f"https://x.com/project/{i}/" for i in range(4)]),
        ]
        ts = build_tasks("https://x.com/", rows)
        self.assertEqual(len(ts), 2, "два раздела должны стать двумя задачами")
        # у каждой задачи свой набор страниц, число совпадает со списком
        for t in ts:
            self.assertEqual(t.count, len(t.urls))
        self.assertEqual({len(t.urls) for t in ts}, {6, 4})
        # метки различаются, иначе в трекере задачи затрут друг друга
        self.assertNotEqual(ts[0].fingerprint, ts[1].fingerprint)

    def test_одно_правило_много_страниц_остаётся_одной_задачей(self):
        """Обратная сторона: «нет H1 на 8 страницах» дробить НЕ нужно."""
        rows = [
            {"rule": "h1.missing", "severity": "medium", "layer": 4, "effort": 2,
             "url": f"https://x.com/p{i}/", "message": "Нет заголовка H1",
             "detail": "", "fixable": 0, "evidence": "{}"}
            for i in range(8)
        ]
        ts = build_tasks("https://x.com/", rows)
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0].count, 8)

    def test_метка_устойчива_между_прогонами(self):
        """Повторный аудит должен дать ТУ ЖЕ метку — иначе трекер зарастёт дублями."""
        def rows(n):
            return [
                {"rule": "h1.missing", "severity": "medium", "layer": 4, "effort": 2,
                 "url": f"https://x.com/p{i}/", "message": "Нет заголовка H1",
                 "detail": "", "fixable": 0, "evidence": "{}"}
                for i in range(n)
            ]
        # набор страниц изменился (8 -> 5), задача та же самая
        a = build_tasks("https://x.com/", rows(8))[0]
        b = build_tasks("https://x.com/", rows(5))[0]
        self.assertEqual(a.fingerprint, b.fingerprint)


if __name__ == "__main__":
    unittest.main()


class TestИзбирательноеДробление(unittest.TestCase):
    """Регресс: дробить задачи по предмету — только там, где это работа.

    Сначала я дробила по любому значению в доказательствах и получила две
    отдельные задачи «Заголовок не на языке страницы», отличавшиеся лишь
    текстом заголовка внутри. Для корректора это ОДНА работа со списком
    страниц. А вот два разных раздела карты сайта — действительно две
    разные настройки, их сливать нельзя.
    """

    def _f(self, rule, ev, url, sev="high"):
        return {
            "rule": rule, "severity": sev, "layer": 2, "effort": 1,
            "url": url, "message": "тест", "detail": "", "fixable": 0,
            "evidence": json.dumps(ev),
        }

    def test_язык_заголовка_не_дробится_по_тексту(self):
        rows = [
            self._f("i18n.title_language_mismatch",
                    {"html_lang": "ro-RO", "value": "Духота летом"},
                    "https://x.md/a/"),
            self._f("i18n.title_language_mismatch",
                    {"html_lang": "ro-RO", "value": "Как сохранить прохладу"},
                    "https://x.md/b/"),
        ]
        ts = build_tasks("https://x.md/", rows)
        self.assertEqual(len(ts), 1, "одна работа корректора = одна задача")
        self.assertEqual(ts[0].count, 2)

    def test_разделы_карты_сайта_дробятся(self):
        rows = [
            self._f("sitemap.section_absent",
                    {"section": "inspiration", "count": 2,
                     "urls": ["https://x.md/i/1/", "https://x.md/i/2/"]},
                    "https://x.md/i/1/"),
            self._f("sitemap.section_absent",
                    {"section": "project", "count": 1,
                     "urls": ["https://x.md/p/1/"]},
                    "https://x.md/p/1/"),
        ]
        ts = build_tasks("https://x.md/", rows)
        self.assertEqual(len(ts), 2, "два раздела = две настройки = две задачи")
        self.assertNotEqual(ts[0].fingerprint, ts[1].fingerprint)

    def test_отпечаток_не_зависит_от_числа_страниц(self):
        """Между прогонами набор страниц меняется — метка обязана уцелеть."""
        a = build_tasks("https://x.md/", [
            self._f("h1.missing", {}, f"https://x.md/{i}/") for i in range(3)])
        b = build_tasks("https://x.md/", [
            self._f("h1.missing", {}, f"https://x.md/{i}/") for i in range(9)])
        self.assertEqual(a[0].fingerprint, b[0].fingerprint)
