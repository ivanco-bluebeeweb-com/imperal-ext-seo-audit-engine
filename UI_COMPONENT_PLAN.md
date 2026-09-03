# SEO Audit Engine — UI component plan

Источники: `Docs/session-notes/UI_COMPONENT_VOCABULARY.md`, `UI_INTERFACE_STANDARD.md`,
`concepts/panels.md`. Основано на функционале `seo-audit-engine` (18+ проверок сайта,
без внешнего `connect_*`).

## 0. Разница с реализацией сейчас
Нет формы подключения аккаунта — первое реальное действие пользователя это ввод
URL(ов) для аудита. Стоит выровнять по стандарту:
- Первый экран — не generic `Empty`, а прямая форма запуска аудита (т.к. это и есть
  единственное "подключение", которое здесь есть).
- Форма ввода URL должна быть растянута на всю ширину контейнера, с лейблом и
  контекстным плейсхолдером ("https://example.com"), не голым полем.
- Прогресс аудита должен обновляться "живо" (через `refresh_panels`), не одним
  спиннером до конца прогона (может занимать минуты).

## 1. Компоненты

| Экран | Примитивы | Почему именно эти |
|---|---|---|
| Sidebar (left) | `ui.Column`(align="start") + `ui.Divider` + navigation `ui.ListItem`(Runs/Sites/Findings/Tasks/Schedule) + `ui.Button`("App settings") | Без карточек, по стандарту. |
| Empty (нет запусков) | `ui.EmptyState`(title="Запустите первый аудит", body, `ui.Form`(action="audit_sites") встроена прямо в EmptyState — `ui.Input`(label="URL сайта(ов)", placeholder="https://example.com, можно несколько через запятую") + `ui.Button`("Запустить аудит")) | Прямой CTA без надуманного шага подключения. |
| Audit Progress | `ui.ProgressBar`(checked/total pages) + `ui.List`(живой список найденных проблем по мере поступления) | Обновление через `refresh_panels`, не единый блокирующий спиннер. |
| Run List | `ui.DataTable`(date, sites count, findings count, health score Badge; sortable) | Обзор прошлых аудитов. |
| Site Health Detail | `ui.Stats`(health score/critical/warnings) + `ui.DataTable`(findings: page, issue, severity Badge; sortable) | Сводка по одному сайту с деталями находок. |
| Fix Plan | `ui.DataTable`(page, field, current value, suggested fix; sortable) + `ui.Button`("Экспортировать в задачи") | Готовые правки, actionable. |
| Compare Audits | `ui.Select`(run A) + `ui.Select`(run B) + `ui.DataTable`(fixed/remaining/new issues) | Сравнение прогресса между прогонами. |
| Resume Banner | `ui.Alert`(warning, "Аудит был прерван — продолжить?") + `ui.Button`("Продолжить") | Явное предложение возобновить, не тихая потеря прогресса. |
| Schedule Settings | `ui.Form`(action="set_schedule") + `ui.Switch`(enabled) + `ui.Select`(hour) + `ui.Select`(days) + `ui.Select`(sites, multi) | Настройка автоматического периодического аудита. |
| App Settings | `ui.Accordion`([Schedule, Connected sites source]) | Централизованные настройки по стандарту. |

## 2. User flow (валидно по panel lifecycle)

1. **SESSION INIT, нет запусков** → Empty со встроенной формой URL → `audit_sites` →
   Audit Progress (живое обновление).
2. Если прошлый аудит был прерван → при открытии сразу `Alert` Resume Banner ПОВЕРХ
   обычного списка запусков → "Продолжить" → `resume_audit`.
3. По завершении аудита → редирект в Site Health Detail (если 1 сайт) или Run List
   (если несколько сайтов проверялись разом).
4. Site Health Detail → клик на finding → Fix Plan (отфильтрован на этот сайт).
5. Fix Plan → "Экспортировать в задачи" → `export_plan` → подтверждение через
   `ui.Dialog` (создаёт задачи во внешнем трекере — необратимое побочное действие).
6. Run List → выбор двух прогонов → Compare Audits.
7. После первого ручного аудита — ненавязчивый `ui.Alert`(info) с CTA "Настроить
   автоматический аудит?" → Schedule Settings.
8. App Settings — доступен из sidebar в любой момент.

## 3. Экраны/карточки (конкретно для этого приложения)

- **Screen: Empty + Audit Form** — EmptyState со встроенным Input+Button.
- **Screen: Audit Progress** — ProgressBar + List(живой).
- **Screen: Run List** — DataTable(4 колонки).
- **Screen: Site Health Detail** — Stats(3) + DataTable(3 колонки).
- **Screen: Fix Plan** — DataTable(4 колонки) + Button.
- **Screen: Compare Audits** — 2×Select + DataTable(3 колонки).
- **Screen: Schedule Settings** — Form(4 поля).
- **Screen: App Settings** — Accordion(2 секции).

Ограничение SDK, учтённое в плане: длительный аудит (может занимать минуты на больших
сайтах) требует периодического `refresh_panels`, отдельного WebSocket-примитива для
живого прогресса в текущем инвентаре нет — используется поллинг.
