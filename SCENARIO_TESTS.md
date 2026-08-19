# Scenario Tests (PST) — SEO Audit Engine

Метод: `Docs/session-notes/SCENARIO_TESTING_STANDARD.md`.

---

## Прогон 2026-08-19

**Существующее покрытие до PST:** 187 тестов в 13 файлах — глубокое
покрытие движка правил (28 собственных тестов на подготовленном HTML),
нормализации URL, page-level фильтрации, панелей, масштаба/WAL и
безопасной параллельной заливки (`upload_run_safely`). Аудит по точному
имени функции нашёл **8 функций, никогда не тестировавшихся через их
реальный хендлер**:

`list_runs`, `list_sites`, `export_plan`, `fix_plan`, `compare_audits`,
`set_schedule`, `get_schedule`, `register_known_site` (IPC-поверхность
для Sites Registry).

**Новый файл:** `tests/test_pst_scenarios.py` — 16 сценариев. Сидирование
следует точному паттерну из `test_pages_screen.py`/`test_tools.py`:
реальный sqlite `Store`, `bridge.download_db` подменён monkeypatch на
путь к этой базе — движок правил не подделывается нигде.

- `list_runs` — happy (реальный прогон возвращается), blocked (аудитов
  ещё не было → `SEO_NO_RUNS`, а не пустой список, выглядящий как «всё
  ок»).
- `list_sites` — happy (сайт с находками), adversarial (`min_severity`
  фильтрует так, что список сайтов не путается с списком задач).
- `export_plan` — happy (план не пуст, содержит assignee/project), error
  (несуществующий сайт → `SEO_SITE_NOT_FOUND`), happy-empty (порог
  строгости выше всех находок → «план пуст», не ошибка).
- `fix_plan` — happy (правки возвращаются), adversarial (`only_ready=True`
  сужает список, не ломает его).
- `compare_audits` — happy (различие между двумя прогонами: одна находка
  исчезла = fixed, одна появилась = new), error (сайт без второго прогона
  для сравнения).
- `set_schedule`/`get_schedule` — happy round-trip (частичное обновление
  `enabled=True, hour=4` не трогает остальные поля — критично: модель
  документирует именно этот риск), adversarial (`hour` вне 0-23 отклоняется
  Pydantic-валидацией, не тихо принимается).
- `register_known_site` (IPC) — happy (новый домен добавляется в список
  расписания), adversarial/idempotent (повторная регистрация уже известного
  домена не создаёт дубликат — `created: False`), error (пустой
  `site_id`/`domain`).

### Результат

203/203 тестов зелёные (187 существующих + 16 новых). **Реальных багов в
приложении не найдено.**

---
