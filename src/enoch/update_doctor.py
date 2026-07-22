"""Compatibility entry point for updater processes started before the package move."""

from __future__ import annotations

from enoch.operations.update_doctor import main


if __name__ == "__main__":
    main()
