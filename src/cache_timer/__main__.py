"""Entry point for ``python -m cache_timer``.

Used as the status line command when the console script is not on PATH, and by
the tests, which exercise the package without installing it.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
