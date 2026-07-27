"""Тесты правил, добавленных к исходным восемнадцати.

ПОЧЕМУ ЭТИ ТЕСТЫ ВЫГЛЯДЯТ ПАРАНОИДАЛЬНО. `run_site_rules` и `run_page_rules`
оборачивают каждое правило в `except Exception: continue`. Это разумно в бою —
одно кривое правило не должно валить весь аудит, — но означает, что СЛОМАННОЕ
правило не падает, а молча исчезает из выдачи. Тест, который проверяет только
«находок нет», такую поломку не заметит: пустой список выглядит одинаково и
когда сайт здоров, и когда правило умерло на первой строке.

Поэтому здесь всюду проверяется ПРИСУТСТВИЕ конкретной находки на заведомо
больном образце, а не отсутствие находок на здоровом.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seoaudit.extract import parse_head
from seoaudit.rules import run_page_rules, run_site_rules

# Заголовок и описание нужной длины: иначе сработают правила title/description
# и зашумят выдачу, а тест начнёт падать по причине, к нему не относящейся.
GOOD_HEAD = (
    "<title>Кондиционеры и вентиляция в Кишинёве — монтаж под ключ</title>"
    '<meta name="description" content="Продажа, монтаж и обслуживание систем '
    'кондиционирования и вентиляции в Кишинёве. Гарантия на работы, выезд '
    'замерщика, сервис круглый год.">'
)


def build(url: str, html: str, **over):
    head = parse_head(html, url)
    item = {
        "url": url, "final_url": url, "status": 200, "redirects": 0,
        "chain": [], "elapsed_ms": 300, "bytes": len(html),
        "content_type": "text/html", "error": "", "state": "done",
        "title": head.title, "description": head.description,
        "canonical": head.canonical, "lang": head.html_lang, "h1": head.h1,
        "noindex": head.is_noindex, "word_count": head.word_count,
        "images_no_alt": head.images_no_alt, "hreflang": head.hreflang,
        "links_internal": head.links_internal,
        "links_internal_total": head.links_internal_total,
        "json_ld_types": head.json_ld_types,
        "heading_levels": head.heading_levels,
    }
    item.update(over)
    return item, head


def keys(findings) -> set[str]:
    return {f.key for f in findings}


class PageRules(unittest.TestCase):
    """Шесть правил, читающих данные, которые раньше собирались впустую."""

    def test_viewport_missing_is_caught(self):
        """Сайт без viewport непригоден на телефоне — а телефон это трафик."""
        item, head = build("https://s.md/p", f"<html><head>{GOOD_HEAD}</head>"
                                             "<body><h1>Т</h1></body></html>")
        self.assertIn("mobile.viewport_missing", keys(run_page_rules(item, head)))

    def test_viewport_present_is_silent(self):
        html = (f'<html><head>{GOOD_HEAD}'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "</head><body><h1>Т</h1></body></html>")
        item, head = build("https://s.md/p", html)
        self.assertNotIn("mobile.viewport_missing", keys(run_page_rules(item, head)))

    def test_robots_conflict_is_caught(self):
        """index и noindex одновременно — Google выберет худшее для владельца."""
        html = (f'<html><head>{GOOD_HEAD}'
                '<meta name="robots" content="index,follow">'
                '<meta name="robots" content="noindex">'
                "</head><body><h1>Т</h1></body></html>")
        item, head = build("https://s.md/p", html)
        self.assertIn("robots.conflicting", keys(run_page_rules(item, head)))

    def test_heading_skip_is_caught(self):
        """H1 → H3: пропущенный уровень ломает структуру документа."""
        html = (f"<html><head>{GOOD_HEAD}</head>"
                "<body><h1>А</h1><h3>Б</h3></body></html>")
        item, head = build("https://s.md/p", html)
        self.assertIn("structure.heading_skip", keys(run_page_rules(item, head)))

    def test_correct_heading_order_is_silent(self):
        html = (f"<html><head>{GOOD_HEAD}</head>"
                "<body><h1>А</h1><h2>Б</h2><h3>В</h3></body></html>")
        item, head = build("https://s.md/p", html)
        self.assertNotIn("structure.heading_skip", keys(run_page_rules(item, head)))

    def test_charset_missing_is_caught(self):
        item, head = build("https://s.md/p",
                           f"<html><head>{GOOD_HEAD}</head><body><h1>Т</h1></body></html>",
                           content_type="text/html")
        self.assertIn("encoding.charset_missing", keys(run_page_rules(item, head)))

    def test_charset_in_http_header_counts(self):
        """Кодировка в заголовке ответа — тоже объявленная кодировка.

        Без этого правило требовало бы мета-тег там, где сервер уже всё сказал
        правильно: совет «почините» на исправной странице.
        """
        item, head = build("https://s.md/p",
                           f"<html><head>{GOOD_HEAD}</head><body><h1>Т</h1></body></html>",
                           content_type="text/html; charset=utf-8")
        self.assertNotIn("encoding.charset_missing", keys(run_page_rules(item, head)))

    def test_noindex_page_is_not_nagged_about_schema(self):
        """Страница, закрытая от индексации, не нуждается в разметке для выдачи.

        Инвариант «не советовать бессмысленное»: разметка и OG нужны ради
        поиска и соцсетей, а закрытую страницу там не покажут.
        """
        html = (f'<html><head>{GOOD_HEAD}'
                '<meta name="robots" content="noindex">'
                "</head><body><h1>Т</h1></body></html>")
        item, head = build("https://s.md/p", html)
        found = keys(run_page_rules(item, head))
        self.assertNotIn("schema.missing", found)
        self.assertNotIn("social.og_missing", found)


class SiteRules(unittest.TestCase):
    """Слой «Структура»: до этой работы в нём не было ни одного правила.

    ПОЧЕМУ ГРАФЫ ЗДЕСЬ НЕ КРОШЕЧНЫЕ. У правил структуры разные пороги размера:
    сироты обсуждаются от 4 страниц, тупики от 6, глубина клика от 8. Это не
    придирка реализации, а её суть — на сайте из трёх страниц «страница без
    входящих ссылок» не дефект, и правило обязано молчать. Поэтому тесты
    строят граф ВЫШЕ порога и добавляют фон из связанных страниц: иначе они
    проверяли бы не поиск дефекта, а срабатывание порога.
    """

    @staticmethod
    def filler(count: int, start: int = 0) -> dict[str, list[str]]:
        """Здоровые страницы «главная ↔ страница» — только чтобы набрать размер.

        Они связаны в обе стороны, поэтому сами не становятся ни сиротами,
        ни тупиками и не подмешивают лишних находок в проверяемый случай.
        """
        return {f"https://s.md/f{i}": ["https://s.md/"]
                for i in range(start, start + count)}

    @staticmethod
    def graph(spec: dict[str, list[str]], statuses: dict[str, int] | None = None):
        statuses = statuses or {}
        return [
            {"url": u, "final_url": u, "state": "done",
             "status": statuses.get(u, 200),
             "links_internal": links, "links_internal_total": len(links)}
            for u, links in spec.items()
        ]

    def test_orphan_page_is_caught(self):
        """На страницу никто не ссылается — посетитель до неё не дойдёт."""
        spec = {
            "https://s.md/": ["https://s.md/a", "https://s.md/f0",
                              "https://s.md/f1", "https://s.md/f2"],
            "https://s.md/a": ["https://s.md/"],
            "https://s.md/orphan": ["https://s.md/"],
        }
        spec.update(self.filler(3))
        pages = self.graph(spec)
        found = run_site_rules(pages, {"origin": "https://s.md"})
        orphan = [f for f in found if f.key == "structure.orphan_page"]
        self.assertTrue(orphan, "сирота не найдена")
        self.assertEqual(orphan[0].evidence["urls"], ["https://s.md/orphan"])

    def test_home_page_is_never_an_orphan(self):
        """На главную часто не ссылаются текстом — это не дефект.

        Инвариант: правило, объявляющее главную сиротой, обесценивает себя —
        такая находка появлялась бы почти на каждом сайте.
        """
        spec = {
            "https://s.md/": ["https://s.md/a", "https://s.md/f0",
                              "https://s.md/f1", "https://s.md/f2"],
            "https://s.md/a": ["https://s.md/"],
        }
        spec.update(self.filler(3))
        pages = self.graph(spec)
        found = run_site_rules(pages, {"origin": "https://s.md"})
        orphans = [f for f in found if f.key == "structure.orphan_page"]
        urls = orphans[0].evidence["urls"] if orphans else []
        self.assertNotIn("https://s.md/", urls)

    def test_mass_orphanage_is_reported_once_not_per_page(self):
        """Если «сирот» больше 80% — сломан обход, а не сайт.

        Без этой оговорки неудачный краул выдавал бы сотню находок «страница
        осиротела» вместо одной честной «я не смогла обойти сайт».
        """
        # Ссылки ДОЛЖНЫ быть хоть где-то: при полном их отсутствии правило
        # молчит намеренно — это «ссылок не собрано», а не «сайт из сирот».
        # Здесь же обход частично удался (главная ссылается на одну страницу),
        # и именно так выглядит подлом краула на живом сайте.
        spec = {f"https://s.md/p{i}": [] for i in range(20)}
        spec["https://s.md/"] = ["https://s.md/p0"]
        pages = self.graph(spec)
        found = run_site_rules(pages, {"origin": "https://s.md"})
        self.assertIn("structure.orphan_suspect", keys(found))
        self.assertNotIn("structure.orphan_page", keys(found))

    def test_broken_internal_link_names_the_source(self):
        """Битая ссылка бесполезна без ответа «а где она стоит»."""
        spec = {
            "https://s.md/": ["https://s.md/a", "https://s.md/f0",
                              "https://s.md/f1", "https://s.md/f2"],
            "https://s.md/a": ["https://s.md/gone", "https://s.md/"],
            "https://s.md/gone": [],
        }
        spec.update(self.filler(3))
        pages = self.graph(spec, statuses={"https://s.md/gone": 404})
        found = run_site_rules(pages, {"origin": "https://s.md"})
        broken = [f for f in found if f.key == "structure.broken_internal_link"]
        self.assertTrue(broken, "битая ссылка не найдена")
        self.assertEqual(broken[0].evidence["examples"]["https://s.md/gone"],
                         ["https://s.md/a"])

    def test_click_depth_is_measured_from_home(self):
        """Глубина считается обходом графа, а не длиной адреса."""
        spec = {
            "https://s.md/": ["https://s.md/a", "https://s.md/f0",
                              "https://s.md/f1", "https://s.md/f2",
                              "https://s.md/f3"],
            "https://s.md/a": ["https://s.md/b"],
            "https://s.md/b": ["https://s.md/c"],
            "https://s.md/c": ["https://s.md/d"],
            "https://s.md/d": ["https://s.md/"],
        }
        spec.update(self.filler(4))
        pages = self.graph(spec)
        found = run_site_rules(pages, {"origin": "https://s.md"})
        deep = [f for f in found if f.key == "structure.deep_page"]
        self.assertTrue(deep, "глубокая страница не найдена")
        self.assertEqual(deep[0].evidence["max_depth"], 4)

    def test_short_url_can_still_be_deep(self):
        """Короткий адрес не значит близкую страницу — и наоборот.

        Проверка того, что правило смотрит на ССЫЛКИ, а не на слэши: адрес
        /x короткий, но добраться до него можно только через четыре перехода.
        """
        spec = {
            "https://s.md/": ["https://s.md/a", "https://s.md/f0",
                              "https://s.md/f1", "https://s.md/f2",
                              "https://s.md/f3"],
            "https://s.md/a": ["https://s.md/b"],
            "https://s.md/b": ["https://s.md/c"],
            "https://s.md/c": ["https://s.md/x"],
            "https://s.md/x": ["https://s.md/"],
        }
        spec.update(self.filler(4))
        pages = self.graph(spec)
        found = run_site_rules(pages, {"origin": "https://s.md"})
        deep = [f for f in found if f.key == "structure.deep_page"]
        self.assertTrue(deep)
        self.assertIn("https://s.md/x",
                      [p["url"] for p in deep[0].evidence["pages"]])

    def test_dead_end_page_is_caught(self):
        """Страница, из которой некуда пойти дальше."""
        spec = {
            "https://s.md/": ["https://s.md/a", "https://s.md/f0",
                              "https://s.md/f1", "https://s.md/f2",
                              "https://s.md/f3"],
            "https://s.md/a": [],
        }
        spec.update(self.filler(4))
        pages = self.graph(spec)
        found = run_site_rules(pages, {"origin": "https://s.md"})
        self.assertIn("structure.dead_end", keys(found))

    def test_structure_layer_is_no_longer_empty(self):
        """Слой 3 существовал в оценке, но не имел ни одного правила.

        Тест сторожит именно это: если правила слоя когда-нибудь исчезнут,
        оценка снова начнёт молча считать пустой слой безупречным.
        """
        spec = {
            "https://s.md/": ["https://s.md/a", "https://s.md/f0",
                              "https://s.md/f1", "https://s.md/f2"],
            "https://s.md/a": ["https://s.md/"],
            "https://s.md/orphan": ["https://s.md/"],
        }
        spec.update(self.filler(3))
        pages = self.graph(spec)
        found = run_site_rules(pages, {"origin": "https://s.md"})
        self.assertTrue([f for f in found if f.layer == 3],
                        "в слое «Структура» снова нет находок")


if __name__ == "__main__":
    unittest.main()
