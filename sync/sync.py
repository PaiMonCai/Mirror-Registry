import importlib
import sys

from . import worker as _worker_module

_worker_module = importlib.reload(_worker_module)

if __name__ == "__main__":
    _worker_module.main()
else:
    sys.modules[__name__] = _worker_module
