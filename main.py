"""Точка входа для веб-ядра и CLI (`imperal validate` / `build`).

Готовит sys.path, чистит кеш модулей и импортирует все слои, чтобы их
декораторы зарегистрировались на ОДНОМ экземпляре Extension. Чистка нужна
потому, что валидатор может грузить несколько расширений в одном процессе:
устаревший модуль в кеше означал бы «инструменты не зарегистрировались».
"""

import os
import sys

_EXT_DIR = os.path.dirname(os.path.abspath(__file__))
if _EXT_DIR not in sys.path:
    sys.path.insert(0, _EXT_DIR)

_LOCAL = ("app", "models", "codes", "shared", "bridge",
          "schedule_settings",
          "handlers_audit", "handlers_read", "handlers_schedule", "panels")
for _mod in _LOCAL:
    sys.modules.pop(_mod, None)

from app import ext, chat  # noqa: E402,F401
import handlers_audit  # noqa: E402,F401
import handlers_read  # noqa: E402,F401
import handlers_schedule  # noqa: E402,F401
import panels  # noqa: E402,F401
