"""Командная строка движка аудита.

Рассчитано на работу агентства:

    # проверить портфель из файла (по домену на строку)
    python3 -m seoaudit audit --sites sites.txt --db portfolio.db

    # продолжить прогон после обрыва — НЕ начиная заново
    python3 -m seoaudit resume --db portfolio.db

    # отчёты и задачи
    python3 -m seoaudit report --db portfolio.db --out ./reports
    python3 -m seoaudit tasks --db portfolio.db --min-severity high --json tasks.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .discover import with_scheme
from .engine import AuditConfig, Engine
from .export_tracker import (
    DEFAULT_PROJECT,
    plan_for_tracker,
    summarise_plan,
    write_plan,
)
from .fetcher import FetchPolicy
from .reports import portfolio_json, portfolio_report_md, site_report_md
from .severity import CRITICAL, HIGH, site_health_score
from .store import Store
from .tasks import build_tasks, summarise_tasks


def _read_sites(path: str) -> list[str]:
    """Читает список сайтов: по одному на строку, # — комментарий."""
    out: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(with_scheme(line))
    return out


def _progress(kind: str, d: dict[str, Any]) -> None:
    """Живой вывод: на 200 сайтах молчащий процесс выглядит как зависший.

    Подпись должна совпадать с тем, как движок зовёт обработчик:
    on_event(kind, data) — словарь ПОЗИЦИОННО. С `**d` вызов молча падал
    внутри защищённого _emit, и прогресс не печатался вообще.
    """
    if kind == "run_start":
        print(f"Прогон #{d.get('run_id')}: сайтов {d.get('sites')}", flush=True)
    elif kind == "site_start":
        print(f"  → {d.get('origin')}", flush=True)
    elif kind == "site_done":
        print(f"  ✓ {d.get('origin')}: {d.get('pages')} стр., "
              f"{d.get('findings', '?')} находок, {d.get('seconds')}с", flush=True)
    elif kind == "site_error":
        print(f"  ✗ {d.get('origin')}: {str(d.get('error'))[:90]}", flush=True)


def _site_rows(store: Store, run_id: int,
               min_severity: str) -> tuple[list[dict[str, Any]], dict[str, list]]:
    """Собирает сводку по сайтам прогона и задачи для каждого."""
    rows: list[dict[str, Any]] = []
    tasks_by_site: dict[str, list] = {}

    for site in store.sites(run_id):
        findings = [dict(f) for f in store.findings(site["id"])]
        pages = store.count_pages(site["id"])
        tasks = build_tasks(site["origin"], findings, min_severity=min_severity)
        tasks_by_site[site["origin"]] = tasks

        by_sev: dict[str, int] = {}
        for f in findings:
            by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1

        top = ""
        for f in sorted(findings, key=lambda x: (x["layer"], x["severity"])):
            if f["severity"] in (CRITICAL, HIGH):
                top = f["message"]
                break

        rows.append({
            "origin": site["origin"],
            "state": site["state"],
            "error": site["error"],
            "pages": pages,
            "score": store.site_score(site["id"], pages),
            "tasks": len(tasks),
            "by_severity": by_sev,
            "top_issue": top,
            "rules": sorted({f["rule"] for f in findings}),
        })
    return rows, tasks_by_site


# ── команды ───────────────────────────────────────────────────────────────

def cmd_audit(args: argparse.Namespace) -> int:
    # Схема дописывается на границе ВВОДА: без неё голый домен превращается в
    # адрес без хоста, и прогон возвращает «сайт недоступен» вместо аудита.
    origins = [with_scheme(s) for s in (args.site or [])]
    if args.sites:
        origins.extend(_read_sites(args.sites))
    origins = [o for o in origins if o]
    if not origins:
        print("Нечего проверять: укажите --site или --sites файл", file=sys.stderr)
        return 2

    store = Store(args.db)
    cfg = AuditConfig(
        max_pages_per_site=args.max_pages,
        site_workers=args.site_workers,
        page_workers=args.page_workers,
        follow_links=not args.no_follow,
        cache_probe=not args.no_cache_probe,
        respect_robots=not args.ignore_robots,
        policy=FetchPolicy(
            timeout=args.timeout,
            per_host_concurrency=args.per_host,
            per_host_delay=args.delay,
        ),
    )
    t0 = time.time()
    engine = Engine(store, cfg, on_event=_progress if not args.quiet else None)
    run_id = engine.run(origins, label=args.label)

    rows, tasks_by_site = _site_rows(store, run_id, args.min_severity)
    total_tasks = sum(len(t) for t in tasks_by_site.values())
    ok = [r for r in rows if r["state"] == "done"]
    crit = [r for r in ok if r["by_severity"].get(CRITICAL)]

    print()
    print(f"Готово за {round(time.time() - t0, 1)}с. "
          f"Сайтов {len(ok)}/{len(rows)} · задач {total_tasks}")
    if crit:
        print(f"ВНИМАНИЕ: критичные дефекты на {len(crit)} сайтах: "
              + ", ".join(r["origin"] for r in crit[:5])
              + (" …" if len(crit) > 5 else ""))
    print(f"База: {args.db}   (отчёты: python3 -m seoaudit report --db {args.db})")
    store.close()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Продолжить прерванный прогон.

    Смысл всей возни с БД: на 200 сайтах обрыв неизбежен, и повторять
    уже сделанную работу недопустимо.
    """
    store = Store(args.db)
    run = store.get_run(args.run) if args.run else store.latest_run()
    if not run:
        print("Прогонов в базе нет", file=sys.stderr)
        return 2

    pending = store.sites(run["id"], states=["pending", "discovering", "fetching"])
    if not pending:
        print(f"Прогон #{run['id']} уже завершён — продолжать нечего.")
        store.close()
        return 0

    print(f"Продолжаю прогон #{run['id']}: осталось сайтов {len(pending)}")
    cfg = AuditConfig(
        max_pages_per_site=args.max_pages,
        site_workers=args.site_workers,
        page_workers=args.page_workers,
    )
    engine = Engine(store, cfg, on_event=_progress if not args.quiet else None)
    engine.resume(run["id"])
    store.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = Store(args.db)
    run = store.get_run(args.run) if args.run else store.latest_run()
    if not run:
        print("Прогонов в базе нет", file=sys.stderr)
        return 2

    rows, tasks_by_site = _site_rows(store, run["id"], args.min_severity)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # сводка по портфелю
    portfolio = out / "portfolio.md"
    portfolio.write_text(
        portfolio_report_md(rows, label=run["label"] or ""), encoding="utf-8"
    )
    (out / "portfolio.json").write_text(
        portfolio_json(rows, tasks_by_site), encoding="utf-8"
    )

    # по каждому сайту
    for site in store.sites(run["id"]):
        findings = [dict(f) for f in store.findings(site["id"])]
        pages = store.count_pages(site["id"])
        name = site["origin"].replace("https://", "").replace("http://", "")
        name = name.strip("/").replace("/", "_") or "site"
        (out / f"{name}.md").write_text(
            site_report_md(
                dict(site), findings, tasks_by_site.get(site["origin"], []),
                pages=pages, score=store.site_score(site["id"], pages),
            ),
            encoding="utf-8",
        )

    print(f"Отчёты: {out}/portfolio.md  (+ по одному файлу на сайт)")
    store.close()
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """Выдать задачи — в консоль или JSON для загрузки в трекер."""
    store = Store(args.db)
    run = store.get_run(args.run) if args.run else store.latest_run()
    if not run:
        print("Прогонов в базе нет", file=sys.stderr)
        return 2

    rows, tasks_by_site = _site_rows(store, run["id"], args.min_severity)
    flat = [t for ts in tasks_by_site.values() for t in ts]

    if args.json:
        Path(args.json).write_text(
            json.dumps([t.to_dict() for t in flat], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Задач {len(flat)} → {args.json}")
    else:
        s = summarise_tasks(flat)
        print(f"Задач: {s['tasks']} · страниц: {s['pages_touched']} "
              f"· автоматически: {s['autofixable_tasks']}")
        print(f"По важности: {s['by_severity']}")
        print()
        for t in flat[: args.limit]:
            auto = " [авто]" if t.autofixable else ""
            print(f"  {t.severity[:4]:<4} {t.due_days:>3}д{auto}  {t.title}")
        if len(flat) > args.limit:
            print(f"  … и ещё {len(flat) - args.limit}")
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seoaudit",
        description="Аудит SEO по портфелю сайтов с формированием задач.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default="seoaudit.db", help="файл базы прогона")
        sp.add_argument("--quiet", action="store_true")

    a = sub.add_parser("audit", help="проверить сайты")
    common(a)
    a.add_argument("--site", action="append", help="домен (можно несколько раз)")
    a.add_argument("--sites", help="файл со списком доменов")
    a.add_argument("--label", default="", help="метка прогона")
    a.add_argument("--max-pages", type=int, default=150)
    a.add_argument("--site-workers", type=int, default=4,
                   help="сколько САЙТОВ одновременно")
    a.add_argument("--page-workers", type=int, default=4,
                   help="сколько страниц одновременно внутри сайта")
    a.add_argument("--per-host", type=int, default=2,
                   help="одновременных запросов к одному хосту (вежливость)")
    a.add_argument("--delay", type=float, default=0.35,
                   help="пауза между запросами к одному хосту, сек")
    a.add_argument("--timeout", type=float, default=20.0)
    a.add_argument("--min-severity", default="medium",
                   choices=["critical", "high", "medium", "low"])
    a.add_argument("--no-follow", action="store_true",
                   help="не искать страницы вне карты сайта")
    a.add_argument("--no-cache-probe", action="store_true",
                   help="не проверять, не отдаёт ли кеш устаревшее")
    a.add_argument("--ignore-robots", action="store_true")
    a.set_defaults(func=cmd_audit)

    r = sub.add_parser("resume", help="продолжить прерванный прогон")
    common(r)
    r.add_argument("--run", type=int)
    r.add_argument("--max-pages", type=int, default=150)
    r.add_argument("--site-workers", type=int, default=4)
    r.add_argument("--page-workers", type=int, default=4)
    r.set_defaults(func=cmd_resume)

    rep = sub.add_parser("report", help="сформировать отчёты")
    common(rep)
    rep.add_argument("--run", type=int)
    rep.add_argument("--out", default="./reports")
    rep.add_argument("--min-severity", default="medium",
                     choices=["critical", "high", "medium", "low"])
    rep.set_defaults(func=cmd_report)

    t = sub.add_parser("tasks", help="показать/выгрузить задачи")
    common(t)
    t.add_argument("--run", type=int)
    t.add_argument("--json", help="записать задачи в JSON")
    t.add_argument("--limit", type=int, default=40)
    t.add_argument("--min-severity", default="medium",
                   choices=["critical", "high", "medium", "low"])
    t.set_defaults(func=cmd_tasks)

    e = sub.add_parser("export", help="план выгрузки задач в трекер")
    common(e)
    e.add_argument("--run", type=int)
    e.add_argument("--json", default="tracker_plan.json",
                   help="куда записать план выгрузки")
    e.add_argument("--project", default=DEFAULT_PROJECT,
                   help="проект в трекере")
    e.add_argument("--assignee", default="", help="на кого назначить")
    e.add_argument("--min-severity", default="medium",
                   choices=["critical", "high", "medium", "low"])
    e.set_defaults(func=cmd_export)
    return p


def cmd_export(args: argparse.Namespace) -> int:
    """Подготовить план выгрузки задач в трекер.

    Сам в трекер не пишет: план сначала можно посмотреть глазами, а заносит
    его коннектор Imperal, у которого есть доступ. На 200 сайтах это разница
    между управляемым инструментом и стихией.
    """
    store = Store(args.db)
    run = store.get_run(args.run) if args.run else store.latest_run()
    if not run:
        print("Прогонов в базе нет", file=sys.stderr)
        return 2

    _, tasks_by_site = _site_rows(store, run["id"], args.min_severity)
    flat = [t for ts in tasks_by_site.values() for t in ts]
    plan = plan_for_tracker(flat, project=args.project, assignee=args.assignee)
    path = write_plan(args.json, plan)
    s = summarise_plan(plan)

    print(f"План выгрузки: {s['tasks']} задач по {s['sites']} сайтам → {path}")
    print(f"Проект: {args.project}")
    for section, n in s["by_section"].items():
        print(f"   {section:<22} {n}")
    print(f"Правится автоматически: {s['autofixable']} · страниц затронуто: {s['pages_touched']}")
    print()
    print("Занести в трекер: попросите Webbee «выгрузи план в Asana» —")
    print("или в панели Imperal, раздел Приложения → SEO Audit Engine.")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nПрервано. Прогон сохранён — продолжить: "
              "python3 -m seoaudit resume --db <файл>", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
