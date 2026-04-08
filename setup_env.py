#!/usr/bin/env python3
"""Tune Server - Environment setup.

Creates a .env file from .env.example if it doesn't exist.
Non-interactive: just copies the template with defaults.
Run once after installation.
"""

import shutil
import sys
from pathlib import Path


def main() -> None:
    base = Path(__file__).resolve().parent
    example = base / ".env.example"
    target = base / ".env"

    if target.exists():
        print(f".env already exists at {target}")
        print("Delete it first if you want to regenerate from template.")
        sys.exit(0)

    if not example.exists():
        print(f"ERROR: {example} not found. Cannot create .env.")
        sys.exit(1)

    shutil.copy2(example, target)
    print(f"Created {target} from template.")
    print("Edit .env to configure your Tidal, Qobuz, Deezer credentials and music directories.")


if __name__ == "__main__":
    main()
