"""Compile the Polish gettext catalogue into a binary ``.mo`` file.

Usage::

    .\\.venv\\Scripts\\python.exe scripts/compile_translations.py
"""

from __future__ import annotations

from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
PO_PATH = ROOT / "locale" / "pl" / "LC_MESSAGES" / "django.po"
MO_PATH = PO_PATH.with_suffix(".mo")


def main() -> None:
    if not PO_PATH.exists():
        raise SystemExit(f"Missing catalogue: {PO_PATH}. Run update_translations.py first.")
    with PO_PATH.open("rb") as handle:
        catalog = read_po(handle)
    with MO_PATH.open("wb") as handle:
        write_mo(handle, catalog)
    translated = sum(1 for message in catalog if message.string)
    print(f"Compiled {translated} translated strings into {MO_PATH}")


if __name__ == "__main__":
    main()
