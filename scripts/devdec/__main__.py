"""Module entry: `python3 -m scripts.devdec` runs the CLI."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
