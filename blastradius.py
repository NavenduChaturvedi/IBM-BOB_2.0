"""Entry shim so the tool runs as `python blastradius.py --diff main...feature/x`."""
import sys

from blastradius.cli import main

if __name__ == "__main__":
    sys.exit(main())
