"""SQLite-хранилище движка аудита.

Зачем БД, а не память: на 20-200 сайтах прогон идёт долго и когда-нибудь
оборвётся (сеть, Ctrl-C, перезагрузка). Всё состояние живёт здесь, поэтому
повторный запуск продолжает с места обрыва, а не начинает заново.

Только стандартная библиотека — на машине агентства не нужен pip install.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Прогон аудита. Один прогон может охватывать много сайтов.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    label       TEXT NOT NULL DEFAULT '',
    profile     TEXT NOT NULL DEFAULT 'default'
);

-- Сайт внутри прогона.
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    origin      TEXT NOT NULL,          -- https://example.com
    label       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'pending',  -- pending|discovering|fetching|done|error
    error       TEXT NOT NULL DEFAULT '',
    robots_txt  TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, origin)
);

-- Очередь URL и результат загрузки каждого.
CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id       INTEGER NOT NULL REFERENCES sites(id),
    url           TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'seed',   -- seed|sitemap|link
    state         TEXT NOT NULL DEFAULT 'queued', -- queued|done|error|skipped
    status        INTEGER,
    final_url     TEXT NOT NULL DEFAULT '',
    redirects     INTEGER NOT NULL DEFAULT 0,
    elapsed_ms    INTEGER,
    content_type  TEXT NOT NULL DEFAULT '',
    bytes         INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    head          TEXT NOT NULL DEFAULT '{}',     -- извлечённые поля (JSON)
    cache_state   TEXT NOT NULL DEFAULT '',       -- hit|miss|'' (по заголовкам)
    cache_layer   TEXT NOT NULL DEFAULT '',       -- какой кеш ответил
    cache_stale   INTEGER NOT NULL DEFAULT 0,     -- 1 = кеш отдаёт устаревшее
    fetched_at    REAL,
    UNIQUE (site_id, url)
);

-- Находки: (сайт, правило, url) с деталями.
CREATE TABLE IF NOT EXISTS findings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id   INTEGER NOT NULL REFERENCES sites(id),
    rule      TEXT NOT NULL,
    layer     INTEGER NOT NULL,
    severity  TEXT NOT NULL,
    effort    INTEGER NOT NULL DEFAULT 2,
    url       TEXT NOT NULL DEFAULT '',
    message   TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT '',
    fixable   INTEGER NOT NULL DEFAULT 0,
    evidence  TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_site_state ON pages(site_id, state);
CREATE INDEX IF NOT EXISTS idx_findings_site ON findings(site_id, rule);
CREATE INDEX IF NOT EXISTS idx_sites_run ON sites(run_id, state);
"""


class Store:
    """Тонкая обёртка над sqlite3 с готовыми операциями движка.

    ВАЖНО о потоках: sqlite3 запрещает использовать одно соединение из
    разных потоков. А аудит 200 сайтов без параллельности бессмыслен —
    поэтому соединение здесь СВОЁ У КАЖДОГО ПОТОКА (threading.local),
    а запись сериализуется одним замком.

    Почему так, а не `check_same_thread=False` на одном соединении: там
    пришлось бы вручную защищать каждый курсор, и любая пропущенная
    операция давала бы порчу данных под нагрузкой. Соединение на поток —
    штатный для sqlite способ, а WAL позволяет читателям не ждать писателя.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        p = Path(self.path)
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()

        con = self._connect()
        con.executescript(_SCHEMA)
        con.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        con.commit()

    # ---------------------------------------------------------------- служебное

    def _connect(self) -> sqlite3.Connection:
        """Соединение текущего потока (создаётся при первом обращении)."""
        con = getattr(self._local, "con", None)
        if con is not None:
            return con
        con = sqlite3.connect(self.path, timeout=30.0)
        con.row_factory = sqlite3.Row
        # WAL: параллельные читатели не блокируют писателя.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        self._local.con = con
        with self._conn_lock:
            self._connections.append(con)
        return con

    @property
    def db(self) -> sqlite3.Connection:
        """Совместимость: код обращается к store.db как к обычному соединению."""
        return self._connect()

    def close(self) -> None:
        """Закрывает соединения всех потоков."""
        with self._conn_lock:
            for con in self._connections:
                try:
                    con.close()
                except Exception:
                    pass
            self._connections.clear()
        self._local = threading.local()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Транзакция под замком записи — параллельные писатели не спорят."""
        con = self._connect()
        with self._write_lock:
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise

    # -------------------------------------------------------------------- runs

    def create_run(self, label: str = "", profile: str = "default") -> int:
        cur = self.db.execute(
            "INSERT INTO runs(started_at, label, profile) VALUES(?,?,?)",
            (time.time(), label, profile),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=? WHERE id=?", (time.time(), run_id)
        )
        self.db.commit()

    def latest_run(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    # ------------------------------------------------------------------- sites

    def add_site(self, run_id: int, origin: str, label: str = "") -> int:
        """Идемпотентно: повторный вызов возвращает существующий id."""
        self.db.execute(
            "INSERT OR IGNORE INTO sites(run_id, origin, label) VALUES(?,?,?)",
            (run_id, origin, label),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT id FROM sites WHERE run_id=? AND origin=?", (run_id, origin)
        ).fetchone()
        return int(row["id"])

    def set_site_state(
        self, site_id: int, state: str, error: str = "", robots_txt: str | None = None
    ) -> None:
        if robots_txt is None:
            self.db.execute(
                "UPDATE sites SET state=?, error=? WHERE id=?", (state, error, site_id)
            )
        else:
            self.db.execute(
                "UPDATE sites SET state=?, error=?, robots_txt=? WHERE id=?",
                (state, error, robots_txt, site_id),
            )
        self.db.commit()

    def set_site_notes(self, site_id: int, notes: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE sites SET notes=? WHERE id=?",
            (json.dumps(notes, ensure_ascii=False), site_id),
        )
        self.db.commit()

    def sites(self, run_id: int, states: Iterable[str] | None = None) -> list[sqlite3.Row]:
        if states:
            marks = ",".join("?" for _ in states)
            return list(
                self.db.execute(
                    f"SELECT * FROM sites WHERE run_id=? AND state IN ({marks}) ORDER BY id",
                    (run_id, *states),
                )
            )
        return list(
            self.db.execute("SELECT * FROM sites WHERE run_id=? ORDER BY id", (run_id,))
        )

    def get_site(self, site_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()

    # ------------------------------------------------------------------- pages

    def queue_urls(self, site_id: int, urls: Iterable[str], source: str) -> int:
        """Добавить URL в очередь. Дубли игнорируются, возвращает число новых."""
        rows = [(site_id, u, source) for u in urls]
        if not rows:
            return 0
        before = self.count_pages(site_id)
        self.db.executemany(
            "INSERT OR IGNORE INTO pages(site_id, url, source) VALUES(?,?,?)", rows
        )
        self.db.commit()
        return self.count_pages(site_id) - before

    def count_pages(self, site_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM pages WHERE site_id=?", (site_id,)
        ).fetchone()
        return int(row["n"])

    def pending_pages(self, site_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        """Незагруженные страницы — основа возобновления после обрыва."""
        sql = "SELECT * FROM pages WHERE site_id=? AND state='queued' ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.db.execute(sql, (site_id,)))

    def pages(self, site_id: int, only_done: bool = True) -> list[sqlite3.Row]:
        if only_done:
            return list(
                self.db.execute(
                    "SELECT * FROM pages WHERE site_id=? AND state='done' ORDER BY id",
                    (site_id,),
                )
            )
        return list(
            self.db.execute("SELECT * FROM pages WHERE site_id=? ORDER BY id", (site_id,))
        )

    def save_page_result(self, page_id: int, result: dict[str, Any]) -> None:
        self.db.execute(
            """UPDATE pages SET state=?, status=?, final_url=?, redirects=?,
                   elapsed_ms=?, content_type=?, bytes=?, error=?, head=?,
                   cache_state=?, cache_layer=?, fetched_at=?
               WHERE id=?""",
            (
                result.get("state", "done"),
                result.get("status"),
                result.get("final_url", ""),
                int(result.get("redirects", 0)),
                result.get("elapsed_ms"),
                result.get("content_type", ""),
                int(result.get("bytes", 0)),
                result.get("error", ""),
                json.dumps(result.get("head", {}), ensure_ascii=False),
                result.get("cache_state", ""),
                result.get("cache_layer", ""),
                time.time(),
                page_id,
            ),
        )
        self.db.commit()

    def save_page_results(self, results: list[tuple[int, dict[str, Any]]]) -> None:
        """Пакетная запись — на сотнях страниц заметно быстрее поштучной."""
        payload = [
            (
                r.get("state", "done"),
                r.get("status"),
                r.get("final_url", ""),
                int(r.get("redirects", 0)),
                r.get("elapsed_ms"),
                r.get("content_type", ""),
                int(r.get("bytes", 0)),
                r.get("error", ""),
                json.dumps(r.get("head", {}), ensure_ascii=False),
                r.get("cache_state", ""),
                r.get("cache_layer", ""),
                time.time(),
                pid,
            )
            for pid, r in results
        ]
        self.db.executemany(
            """UPDATE pages SET state=?, status=?, final_url=?, redirects=?,
                   elapsed_ms=?, content_type=?, bytes=?, error=?, head=?,
                   cache_state=?, cache_layer=?, fetched_at=?
               WHERE id=?""",
            payload,
        )
        self.db.commit()

    # ---------------------------------------------------------------- findings

    def clear_findings(self, site_id: int) -> None:
        """Правила прогоняются заново — старые находки сайта убираем."""
        self.db.execute("DELETE FROM findings WHERE site_id=?", (site_id,))
        self.db.commit()

    def add_findings(self, site_id: int, items: Iterable[dict[str, Any]]) -> int:
        rows = [
            (
                site_id,
                it.get("key") or it.get("rule") or "unknown",
                int(it.get("layer", 4)),
                it.get("severity", "medium"),
                int(it.get("effort", 2)),
                it.get("url", ""),
                it.get("title") or it.get("message", ""),
                it.get("detail", ""),
                1 if it.get("fixable") else 0,
                json.dumps(it.get("evidence", {}), ensure_ascii=False),
                time.time(),
            )
            for it in items
        ]
        if not rows:
            return 0
        self.db.executemany(
            """INSERT INTO findings(site_id, rule, layer, severity, effort, url,
                                    message, detail, fixable, evidence, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.db.commit()
        return len(rows)

    def findings(self, site_id: int) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM findings WHERE site_id=? ORDER BY layer, rule, url",
                (site_id,),
            )
        )

    def findings_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                """SELECT f.*, s.origin AS origin, s.label AS site_label
                   FROM findings f JOIN sites s ON s.id = f.site_id
                   WHERE s.run_id=? ORDER BY f.layer, s.origin, f.rule, f.url""",
                (run_id,),
            )
        )

    def site_score(self, site_id: int, page_count: int) -> int:
        """Оценка 0-100 по сохранённым находкам.

        Нужна агентству, чтобы СРАВНИВАТЬ сайты между собой и понимать,
        с какого начинать. Считается из БД, а не из объектов в памяти,
        поэтому доступна и после перезапуска.
        """
        if page_count <= 0:
            return 0
        weights = {"critical": 100, "high": 40, "medium": 12, "low": 4, "info": 0}
        penalty = 0.0
        for row in self.db.execute(
            "SELECT severity, url FROM findings WHERE site_id=?", (site_id,)
        ):
            w = weights.get(row["severity"], 0)
            if not w:
                continue
            # находки на весь сайт весят полностью, постраничные — с затуханием,
            # иначе большой сайт всегда выглядит хуже маленького
            penalty += w if not row["url"] else w / max(1.0, page_count ** 0.5)
        return max(0, min(100, int(round(100 - penalty / 3.0))))

    # ------------------------------------------------------------------ сводки

    def run_summary(self, run_id: int) -> dict[str, Any]:
        sites = self.sites(run_id)
        out: dict[str, Any] = {
            "run_id": run_id,
            "sites": len(sites),
            "by_state": {},
            "pages_done": 0,
            "pages_queued": 0,
            "findings": 0,
        }
        for s in sites:
            out["by_state"][s["state"]] = out["by_state"].get(s["state"], 0) + 1
        row = self.db.execute(
            """SELECT
                 SUM(CASE WHEN p.state='done' THEN 1 ELSE 0 END) AS done,
                 SUM(CASE WHEN p.state='queued' THEN 1 ELSE 0 END) AS queued
               FROM pages p JOIN sites s ON s.id=p.site_id WHERE s.run_id=?""",
            (run_id,),
        ).fetchone()
        out["pages_done"] = int(row["done"] or 0)
        out["pages_queued"] = int(row["queued"] or 0)
        frow = self.db.execute(
            """SELECT COUNT(*) AS n FROM findings f JOIN sites s ON s.id=f.site_id
               WHERE s.run_id=?""",
            (run_id,),
        ).fetchone()
        out["findings"] = int(frow["n"] or 0)
        return out
