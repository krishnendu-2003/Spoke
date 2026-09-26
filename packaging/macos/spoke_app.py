"""PyInstaller entry point for Spoke.app (see spoke/app.py)."""

import sys

from spoke.app import main

if __name__ == "__main__":
    sys.exit(main())
