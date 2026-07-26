"""Движок SEO-аудита по портфелю сайтов.

Устройство (по слоям, снизу вверх):

    store.py     SQLite: состояние прогона, устойчивость к обрывам
    fetcher.py   загрузка: вежливость к хосту, повторы, чтение кеш-заголовков
    discover.py  robots.txt -> карты сайта -> список URL
    extract.py   разбор <head> настоящим парсером
    rules.py     нормы: 18 правил, выведенных из реальных дефектов
    severity.py  слои важности и приоритизация (выгода/трудозатраты)
    engine.py    связывает всё: обход -> разбор -> правила -> находки
    tasks.py     находки -> ЗАДАЧИ, сгруппированные (сайт × правило)
    reports.py   отчёты: по сайту (исполнителю) и по портфелю (руководителю)
    cli.py       точка входа

Принцип разделения ответственности: движок ТОЛЬКО ЧИТАЕТ сайты и судит.
Записывать правки в WordPress — дело wp-site-connector, единственной «руки».
Так у полей Rank Math остаётся один писатель, а доступы к сайтам не
размножаются по приложениям.
"""

__version__ = "0.1.0"

from .engine import AuditConfig, Engine
from .fetcher import FetchPolicy
from .store import Store
from .tasks import Task, build_tasks

__all__ = [
    "AuditConfig",
    "Engine",
    "FetchPolicy",
    "Store",
    "Task",
    "build_tasks",
    "__version__",
]
