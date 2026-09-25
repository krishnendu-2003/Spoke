"""Entrypoint: `python -m spoke [setup|test-mic|history|doctor]`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
