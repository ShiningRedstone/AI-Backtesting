"""PyInstaller entry script for EdgeLab.exe (all logic lives in edgelab.desktop)."""
import sys

from edgelab.desktop import main

if __name__ == "__main__":
    sys.exit(main())
