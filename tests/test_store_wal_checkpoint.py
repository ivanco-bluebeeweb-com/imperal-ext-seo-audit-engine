"""Store.close() должен сбрасывать WAL до того, как файл базы будет прочитан
как raw-байты (bridge.upload_db делает ровно это).

КОНТЕКСТ БАГА. `Store` держит по одному sqlite-соединению на поток (см.
докстринг класса) — это правильно для `site_workers > 1`, где каждый сайт
аудитится в своём потоке пула. Но `close()` вызывается из ОДНОГО потока
(там, где вызывающий код решил закончить работу), а sqlite3 запрещает
закрывать соединение не из того потока, где оно создано — попытка бросает
ProgrammingError, которую `close()` намеренно проглатывает построчно (один
сбой закрытия не должен маскировать успешный аудит). В WAL-режиме свежие
коммиты живут в файле `<path>-wal`, пока никто не сделает checkpoint;
если соединение воркера так и не закрылось штатно, эти данные остаются
ТОЛЬКО там. `bridge.upload_db` читает raw-байты одного файла `path` —
без явного checkpoint в `close()` находки сайтов, обработанных в
воркер-потоках, тихо не долетают до хранилища портфеля, хотя `run()`
отчитывается `finished=True`. Обнаружено на живом двухсайтовом прогоне
(audit_sites вернул валидный run_id, но ни list_runs, ни get_report его
после не находили).
"""

from __future__ import annotations

import os
import sqlite3
import threading

from seoaudit.store import Store


def _raw_row_count(db_path: str, table: str) -> int:
    """Прочитать raw-байты файла и посчитать строки — то же самое, что делает
    bridge.upload_db (Path(path).read_bytes()) с итоговым файлом."""
    copy_path = db_path + ".raw_check_copy"
    with open(db_path, "rb") as src, open(copy_path, "wb") as dst:
        dst.write(src.read())
    con = sqlite3.connect(copy_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()
        os.unlink(copy_path)


def test_close_checkpoints_wal_writes_made_from_worker_threads(tmp_path):
    """Пять «воркеров» (свои соединения, свои потоки) пишут по одному сайту,
    как это делает Engine.run() с ThreadPoolExecutor при site_workers>1.
    После close() все пять записей обязаны быть видны в raw-байтах файла,
    не только в WAL."""
    db_path = str(tmp_path / "portfolio.db")
    store = Store(db_path)
    run_id = store.create_run(label="parallel test")

    def worker(n: int) -> None:
        # add_site открывает СВОЁ соединение в этом потоке (threading.local).
        store.add_site(run_id, f"https://site{n}.example")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # До close() данные точно записаны (committed) — проверяем через тот же
    # Store, который видит и WAL. Это не тест поведения бага, а sanity-check
    # что мы действительно вставили 5 строк, прежде чем измерять раздельно.
    assert len(store.sites(run_id)) == 5

    store.close()

    # Теперь — то, что реально важно: raw-байты файла (что улетает в
    # ctx.storage.upload через bridge.upload_db) обязаны содержать все 5
    # записей, а не только те, что попали туда до этого теста.
    assert _raw_row_count(db_path, "sites") == 5


def test_close_checkpoint_is_best_effort_and_never_raises(tmp_path):
    """Даже если что-то пойдёт не так при попытке checkpoint (файл занят,
    права доступа, etc.), close() не должен ронять вызывающий код — заливка
    последней успешно закрытой версии всё равно лучше, чем необработанное
    исключение посреди аудита."""
    db_path = str(tmp_path / "portfolio.db")
    store = Store(db_path)
    store.create_run(label="best effort")

    # Удаляем файл базы из-под ног — checkpoint-соединение не сможет
    # открыться на несуществующем пути так, как ожидал бы штатный сценарий,
    # но close() обязан остаться тихим (задокументированное поведение).
    store.close()  # normal close still must not raise even in this edge case
