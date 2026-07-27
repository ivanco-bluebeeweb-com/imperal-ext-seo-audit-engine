"""Граница ввода: голый домен обязан стать рабочим адресом.

Регрессия, которая стоила целого прогона. Человек написал `climtec.md` —
`urlsplit` увидел в этом ПУТЬ, а не хост, схема приклеилась к пути, и получился
`https:climtec.md`. Прогон отработал «успешно»: одна страница, ошибка
«no host given», отчёт вида «сайт недоступен». То есть инструмент обвинил
чужой сервер в собственной ошибке разбора — худший вид бага.

Платформенный путь был защищён, CLI — нет. Поэтому тестов два: на саму
функцию и на то, что КАЖДАЯ точка ввода её применяет.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from urllib.parse import urlsplit

import bridge as br
from seoaudit.discover import normalize_url, with_scheme


class WithScheme(unittest.TestCase):

    def test_bare_domain_gets_a_scheme(self):
        self.assertEqual(with_scheme("climtec.md"), "https://climtec.md")

    def test_existing_scheme_is_left_alone(self):
        for url in ("http://a.md", "https://a.md/path"):
            self.assertEqual(with_scheme(url), url)

    def test_the_actual_regression_no_host_given(self):
        """Ровно та поломка, ради которой всё это написано.

        Утверждение здесь ОДНО: пройдя границу ввода, голый домен становится
        адресом с настоящим хостом. Раньше тест дополнительно фиксировал, что
        БЕЗ `with_scheme` получается битый `https:climtec.md` — и это оказалось
        привязкой к самой поломке: разбор адресов зависит от версии Python, и
        на другой среде проверка развалилась, хотя приложение исправно. Тест
        обязан держать требование, а не форму бага.
        """
        fixed = normalize_url(with_scheme("climtec.md"))
        self.assertTrue(fixed.startswith("https://"), fixed)
        # Хост действительно распознан, а не приклеен к пути.
        self.assertEqual(urlsplit(fixed).hostname, "climtec.md")

    def test_empty_input_stays_empty(self):
        """Пустая строка не должна превращаться в 'https://'."""
        self.assertEqual(with_scheme(""), "")
        self.assertEqual(with_scheme("   "), "")


class EveryEntryPointNormalises(unittest.TestCase):
    """Одна защищённая точка ввода и одна забытая — это и был баг."""

    def test_parse_sites_normalises_bare_domains(self):
        out = br.parse_sites("climtec.md, https://b.md, c.md")
        self.assertTrue(all(u.startswith("http") for u in out), out)

    def test_cli_read_sites_calls_with_scheme(self):
        """Проверяем ИСХОДНИК: файл со списком сайтов читается с диска.

        Дублировать здесь чтение файла значило бы тестировать копию логики,
        а не саму функцию. Поэтому смотрим, что вызов действительно стоит
        внутри `_read_sites` — именно его там когда-то не было.
        """
        source = pathlib.Path(__file__).resolve().parent.parent / "seoaudit" / "cli.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        checked: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in ("_read_sites", "cmd_audit"):
                continue
            calls = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            self.assertIn("with_scheme", calls,
                          f"{node.name} принимает домены, но не нормализует их")
            checked.append(node.name)

        self.assertEqual(sorted(checked), ["_read_sites", "cmd_audit"],
                         f"проверены не все точки ввода: {checked}")


if __name__ == "__main__":
    unittest.main()
