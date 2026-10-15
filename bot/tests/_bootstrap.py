"""Подготовка окружения тестов: путь к проекту и конфиг.

Если config/config.py нет (например, свежий клон без секретов), тесты берут
значения из config/config.py.example — боевой конфиг для них не нужен.
"""
import importlib.machinery
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import config.config  # noqa: F401
except ModuleNotFoundError:
    import config

    path = os.path.join(ROOT, "config", "config.py.example")
    loader = importlib.machinery.SourceFileLoader("config.config", path)
    spec = importlib.util.spec_from_loader("config.config", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    sys.modules["config.config"] = module
    config.config = module
