"""Тесты трёх новых способностей: правки, сравнение прогонов, расписание.

ЧТО ИМЕННО ЗДЕСЬ СТОРОЖИТСЯ. Не «функция возвращает список» — такое ломается
шумно и заметно. Сторожатся решения, поломка которых ТИХАЯ и выглядит как
нормальная работа:

* правка, собранная из мусора, выглядит как сделанная работа;
* «починено 12» после урезанного обхода выглядит как победа;
* расписание, потерявшее час при выключении, выглядит как выключенное — и
  включается потом не тогда, когда человек ожидает.

Каждый из этих случаев не падает и не пишет в журнал. Поэтому проверяются они.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seoaudit.compare import COVERAGE_TOLERANCE, compare_findings, summarise
from seoaudit.fixes import (_desc_fix, _trim_to, build_fixes,
                            summarise_fixes)


def page(url, **over):
    d = {"final_url": url, "title": "", "description": "", "canonical": "",
         "h1": [], "html_lang": "ru"}
    d.update(over)
    return d


def finding(rule, url, **over):
    d = {"rule": rule, "url": url, "severity": "medium", "layer": 4,
         "message": f"{rule} на {url}"}
    d.update(over)
    return d


class Fixes(unittest.TestCase):
    """Находка -> конкретное значение поля."""

    def test_title_is_built_from_h1_not_invented(self):
        """Заголовок собирается из H1 — текста, написанного человеком.

        H1 берётся ДЛИННЫЙ намеренно: короткий заголовок правило само
        пометит «стоит дописать», и тест проверял бы длину, а не источник.
        """
        pages = {"https://s.md/a": page(
            "https://s.md/a",
            h1=["Кондиционеры для дома и офиса с установкой"])}
        out = build_fixes([finding("title.missing", "https://s.md/a")],
                          pages, "https://s.md")
        self.assertEqual(len(out), 1)
        self.assertIn("Кондиционеры для дома и офиса", out[0].proposed)
        self.assertTrue(out[0].ready)

    def test_too_short_title_from_h1_asks_for_a_human(self):
        """Собрать смог, но вышло коротко — это признаётся, а не выдаётся за готовое.

        Тихая поломка, от которой сторожит тест: пометить такую правку
        готовой значило бы наштамповать заголовков в три слова и отчитаться
        об успехе.
        """
        pages = {"https://s.md/a": page("https://s.md/a", h1=["Контакты"])}
        out = build_fixes([finding("title.missing", "https://s.md/a")],
                          pages, "https://s.md")
        self.assertFalse(out[0].ready)
        self.assertIn("дописать", out[0].note)

    def test_missing_description_is_never_invented(self):
        """Описание — рекламный текст в выдаче. Машина его не сочиняет.

        Главный инвариант всего модуля: правдоподобное описание, поставленное
        автоматически, ХУЖЕ отсутствующего. Отсутствие видно в следующем
        аудите; сочинённый текст выглядит сделанной работой и остаётся
        в выдаче навсегда.
        """
        pages = {"https://s.md/a": page("https://s.md/a", h1=["Услуги"])}
        out = build_fixes([finding("description.missing", "https://s.md/a")],
                          pages, "https://s.md")
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].ready, "описание не должно применяться само")
        self.assertEqual(out[0].proposed, "")

    def test_long_title_is_trimmed_not_cut_mid_word(self):
        """Обрезка по словам: «Кондиционе…» в выдаче выглядит как поломка."""
        long_title = ("Кондиционеры вентиляция отопление тепловые насосы "
                      "монтаж обслуживание проектирование Кишинёв Молдова")
        pages = {"https://s.md/a": page("https://s.md/a", title=long_title)}
        out = build_fixes([finding("title.too_long", "https://s.md/a")],
                          pages, "https://s.md")
        self.assertTrue(out and out[0].ready)
        self.assertFalse(out[0].proposed.endswith(" "))
        # Ни одно слово не разорвано: каждое слово результата есть в исходнике.
        for word in out[0].proposed.replace("…", "").split():
            self.assertIn(word, long_title + " —")

    def test_canonical_points_at_the_page_itself(self):
        pages = {"https://s.md/ru/a": page("https://s.md/ru/a",
                                           canonical="https://s.md/ro/a")}
        out = build_fixes([finding("canonical.cross_language", "https://s.md/ru/a")],
                          pages, "https://s.md")
        self.assertTrue(out and out[0].ready)
        self.assertEqual(out[0].proposed, "https://s.md/ru/a")

    def test_unknown_rule_produces_no_fix(self):
        """Правило вне списка чинимых не превращается в правку.

        Список чинимого сознательно короткий. Молчание здесь — не пробел,
        а отказ трогать то, где значение нельзя вывести однозначно.
        """
        pages = {"https://s.md/a": page("https://s.md/a")}
        out = build_fixes([finding("structure.orphan_page", "https://s.md/a")],
                          pages, "https://s.md")
        self.assertEqual(out, [])

    def test_fix_for_unknown_page_admits_it_needs_a_human(self):
        """Страницы в корпусе нет — правка есть, но помечена как ручная.

        Могло показаться, что такую находку правильнее пропустить молча. Нет:
        человек видит дефект в отчёте, и правка, исчезнувшая без объяснения,
        читается как «уже починено». Честнее строка «нужен человек».
        """
        out = build_fixes([finding("title.missing", "https://s.md/ghost")],
                          {}, "https://s.md")
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].ready)
        self.assertEqual(out[0].proposed, "")

    def test_summary_counts_ready_separately(self):
        pages = {
            "https://s.md/a": page(
                "https://s.md/a",
                h1=["Услуги компании по монтажу и обслуживанию"]),
            "https://s.md/b": page("https://s.md/b"),
        }
        out = build_fixes(
            [finding("title.missing", "https://s.md/a"),
             finding("description.missing", "https://s.md/b")],
            pages, "https://s.md")
        s = summarise_fixes(out)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["ready"], 1)
        self.assertEqual(s["needs_review"], 1)


class Compare(unittest.TestCase):
    """Два прогона: что починилось, что осталось, что появилось."""

    BEFORE = [
        finding("title.missing", "https://s.md/a"),
        finding("h1.missing", "https://s.md/b"),
    ]

    def test_fixed_and_appeared_are_separated(self):
        after = [
            finding("h1.missing", "https://s.md/b"),
            finding("robots.noindex", "https://s.md/c", severity="critical"),
        ]
        c = compare_findings(self.BEFORE, after, origin="https://s.md",
                             before_pages=10, after_pages=10)
        self.assertEqual([f.rule for f in c.fixed], ["title.missing"])
        self.assertEqual([f.rule for f in c.remains], ["h1.missing"])
        self.assertEqual([f.rule for f in c.appeared], ["robots.noindex"])

    def test_same_rule_on_another_page_is_a_new_finding(self):
        """Правило то же, страница другая — это НЕ «осталось».

        Сопоставление идёт по паре правило+адрес. Иначе починенная страница
        и сломавшаяся соседняя схлопнулись бы в «ничего не изменилось».
        """
        after = [finding("title.missing", "https://s.md/OTHER")]
        c = compare_findings([finding("title.missing", "https://s.md/a")],
                             after, origin="https://s.md",
                             before_pages=10, after_pages=10)
        self.assertEqual(len(c.fixed), 1)
        self.assertEqual(len(c.appeared), 1)
        self.assertEqual(c.remains, [])

    def test_changed_message_is_not_a_new_finding(self):
        """Сообщение содержит числа («11 слов») — сравнивать по нему нельзя.

        Иначе каждый прогон объявлял бы починку и регрессию одновременно
        просто потому, что счётчик в тексте изменился.
        """
        before = [finding("content.thin", "https://s.md/a", message="Мало текста (11 слов)")]
        after = [finding("content.thin", "https://s.md/a", message="Мало текста (14 слов)")]
        c = compare_findings(before, after, origin="https://s.md",
                             before_pages=10, after_pages=10)
        self.assertEqual(len(c.remains), 1)
        self.assertEqual(c.fixed, [])
        self.assertEqual(c.appeared, [])

    def test_shrunken_crawl_is_not_called_a_victory(self):
        """Обошли вчетверо меньше страниц — «починено» может быть непроверенным.

        Самая опасная ложь этого модуля: отчёт, который хвалит за работу,
        которой не было. Поэтому сравнение помечается как ненадёжное.
        """
        c = compare_findings(self.BEFORE, [], origin="https://s.md",
                             before_pages=50, after_pages=12,
                             before_score=60, after_score=95)
        self.assertFalse(c.reliable)
        self.assertTrue(c.caveat)
        self.assertIn("12", c.caveat)
        self.assertIn("50", c.caveat)

    def test_comparable_crawl_stays_reliable(self):
        """Небольшая разница в охвате — норма живого сайта, не повод для оговорки."""
        after_pages = int(50 * (1 - COVERAGE_TOLERANCE / 2))
        c = compare_findings(self.BEFORE, self.BEFORE, origin="https://s.md",
                             before_pages=50, after_pages=after_pages)
        self.assertTrue(c.reliable)
        self.assertEqual(c.caveat, "")

    def test_summary_leads_with_regressions(self):
        """В однострочном итоге появившееся должно быть ВИДНО.

        Ради него сравнение и существует: в общем списке новая беда ничем
        не выделяется среди старых.
        """
        after = self.BEFORE + [finding("robots.noindex", "https://s.md/c",
                                       severity="critical")]
        c = compare_findings(self.BEFORE, after, origin="https://s.md",
                             before_pages=10, after_pages=10)
        text = summarise(c)
        self.assertIn("ПОЯВИЛОСЬ", text.upper())


class Schedule(unittest.IsolatedAsyncioTestCase):
    """Ночной аудит: когда просыпаться и когда молчать."""

    async def asyncSetUp(self):
        from imperal_sdk.testing import MockContext
        self.ctx = MockContext()
        import schedule_settings as sched
        self.sched = sched

    # Понедельник 2026-07-27, 03:00 UTC — время по умолчанию.
    # 27 июля 2026, 03:00 UTC — это ПОНЕДЕЛЬНИК (isoweekday=1). Дату
    # приходится держать точной: правило смотрит на день недели, и метка
    # «понедельник», оказавшаяся четвергом, ломает тест не там, где кажется.
    MONDAY_3AM = 1785121200.0

    async def test_disabled_by_default(self):
        """Аудит ходит по ЧУЖИМ серверам. Сам по себе он не начинается."""
        ok, why = await self.sched.due(self.ctx, ts=self.MONDAY_3AM)
        self.assertFalse(ok)
        self.assertEqual(why, "disabled")

    async def test_runs_at_the_chosen_hour(self):
        await self.sched.set_settings(self.ctx, enabled=True, hour=3, days="1")
        ok, why = await self.sched.due(self.ctx, ts=self.MONDAY_3AM)
        self.assertTrue(ok, f"не запустился: {why}")

    async def test_silent_before_the_hour(self):
        await self.sched.set_settings(self.ctx, enabled=True, hour=3, days="1")
        ok, why = await self.sched.due(self.ctx, ts=self.MONDAY_3AM - 3600)
        self.assertFalse(ok)
        self.assertEqual(why, "too_early")

    async def test_silent_on_other_days(self):
        await self.sched.set_settings(self.ctx, enabled=True, hour=3, days="4")
        ok, why = await self.sched.due(self.ctx, ts=self.MONDAY_3AM)
        self.assertFalse(ok)
        self.assertEqual(why, "other_day")

    async def test_never_twice_in_one_day(self):
        """Тик срабатывает каждый час — прогон должен быть один.

        Без этого ежедневный аудит стал бы ежечасным обстрелом чужих сайтов.
        """
        await self.sched.set_settings(self.ctx, enabled=True, hour=3, days="1")
        await self.sched.mark_ran(self.ctx, run_id=7, ts=self.MONDAY_3AM)
        ok, why = await self.sched.due(self.ctx, ts=self.MONDAY_3AM + 3600)
        self.assertFalse(ok)
        self.assertEqual(why, "already_today")

    async def test_late_wakeup_still_runs(self):
        """Платформа разбудила в 9 вместо 3 — пропущенная ночь хуже сдвига."""
        await self.sched.set_settings(self.ctx, enabled=True, hour=3, days="1")
        ok, why = await self.sched.due(self.ctx, ts=self.MONDAY_3AM + 6 * 3600)
        self.assertTrue(ok)
        self.assertEqual(why, "catching_up")

    async def test_partial_update_keeps_other_fields(self):
        """«Перенеси на 4 утра» не должно стирать список сайтов.

        Правка одного поля, молча сбрасывающая другое, — тихая потеря
        настройки: человек узнает о ней, только когда аудит уйдёт не туда.
        """
        await self.sched.set_settings(self.ctx, enabled=True, hour=3,
                                      days="1", sites="climtec.md",
                                      max_pages=120)
        d = await self.sched.set_settings(self.ctx, hour=4)
        self.assertEqual(d["hour"], 4)
        self.assertEqual(d["sites"], "climtec.md")
        self.assertEqual(d["max_pages"], 120)
        self.assertTrue(d["enabled"])

    async def test_disabling_keeps_the_settings(self):
        """Выключение не стирает час и дни — включат обратно тем же составом."""
        await self.sched.set_settings(self.ctx, enabled=True, hour=5, days="2,5")
        d = await self.sched.set_settings(self.ctx, enabled=False)
        self.assertFalse(d["enabled"])
        self.assertEqual(d["hour"], 5)
        self.assertEqual(d["days"], "2,5")

    async def test_tick_is_not_coarser_than_an_hour(self):
        """Будильник задаёт ПОТОЛОК точности: реже часа — и «в 3 ночи» соврёт."""
        self.assertLessEqual(self.sched.TICK_MINUTES, 60)

    async def test_hour_is_clamped(self):
        """Час вне суток не должен превращаться в «никогда не запустится»."""
        d = await self.sched.set_settings(self.ctx, hour=99)
        self.assertLessEqual(d["hour"], 23)
        d = await self.sched.set_settings(self.ctx, hour=-5)
        self.assertGreaterEqual(d["hour"], 0)


if __name__ == "__main__":
    unittest.main()


class TrimQuality(unittest.TestCase):
    """Как именно режется длинный текст — это видно человеку в выдаче.

    Обрезка «формально в рамке» и обрезка «читается как текст» — разные вещи,
    и разница не ловится ни одной проверкой длины. Живой пример, с которого
    начались эти тесты: описание резалось как «…Analiza parametrilor și a
    liniei» — фраза обрывается на предлоге, длина при этом идеальная.
    """

    def test_description_is_cut_at_the_end_of_a_sentence(self):
        long_desc = (
            "Cum alegi recuperatorul pentru apartament: debitul de aer după "
            "suprafață, numărul de persoane, nivelul de zgomot și montajul. "
            "Analiza parametrilor și a liniei Quattro.")
        out, conf, _note = _desc_fix({"description": long_desc})
        self.assertEqual(conf, "high")
        self.assertTrue(out.endswith("."), f"обрывок вместо фразы: {out!r}")
        self.assertNotIn("și a liniei", out)

    def test_an_early_full_stop_does_not_collapse_the_text(self):
        """«Коротко.» в начале не должно съесть всё описание.

        Обрезка по первому же предложению формально красива и по сути хуже
        исходного: в выдачу уйдёт огрызок из одного слова. Нижняя граница
        существует ровно против этого.
        """
        text = ("Коротко. Дальше идёт основной длинный текст описания "
                "страницы, который несёт весь смысл для человека в поисковой "
                "выдаче и обязан сохраниться целиком без потерь и обрывов.")
        out, _conf, _note = _desc_fix({"description": text})
        self.assertGreater(len(out), 100, f"описание схлопнулось: {out!r}")

    def test_text_without_any_full_stop_still_gets_trimmed(self):
        """Нет точек — режем по словам, но никогда посреди слова."""
        text = ("Одно длинное предложение без единой точки в котором смысл "
                "тянется до самого конца и обрывать его придётся по словам "
                "потому что резать больше решительно негде совершенно")
        out = _trim_to(text, 160, keep_at_least=120)
        self.assertLessEqual(len(out), 160)
        self.assertFalse(out.endswith(" "))
        # Последнее слово должно быть целым словом исходника.
        self.assertIn(out.split()[-1], text.split())
