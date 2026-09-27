"""PyInstaller entry point (PyInstaller needs a script, not `-m savesync`)."""
import sys

from savesync.app import main

if __name__ == "__main__":
    sys.exit(main())
