import importlib
import sys

from . import app as _app_module

_app_module = importlib.reload(_app_module)
sys.modules[__name__] = _app_module
