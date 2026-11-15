"""Extract translatable strings and refresh the Polish gettext catalogue.

Django's ``makemessages`` needs the GNU gettext tools, which are not always
available on Windows. This script performs a small, dependency-light
extraction instead:

* ``{% trans "..." %}`` / ``{% translate "..." %}`` from templates
* ``{% blocktrans %}...{% endblocktrans %}`` bodies (``{{ var }}`` becomes
  ``%(var)s`` so the placeholders match Django's own extraction)
* ``_("...")`` calls in Python modules

Existing translations in ``locale/pl/LC_MESSAGES/django.po`` are preserved.

Usage::

    .\\.venv\\Scripts\\python.exe scripts/update_translations.py
    .\\.venv\\Scripts\\python.exe scripts/compile_translations.py
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.pofile import read_po, write_po
from pl_translations import TRANSLATIONS

ROOT = Path(__file__).resolve().parent.parent
LOCALE = ROOT / "locale" / "pl" / "LC_MESSAGES"
PO_PATH = LOCALE / "django.po"

TEMPLATE_DIRS = [ROOT / "templates"]
PYTHON_DIRS = [ROOT / "apps", ROOT / "config"]
SKIP_DIRS = {".venv", "staticfiles", "node_modules", "__pycache__", "locale"}

TRANS_RE = re.compile(r"{%\s*(?:trans|translate)\s+(?P<quote>['\"])(?P<text>.+?)(?P=quote)\s*%}")
BLOCKTRANS_RE = re.compile(
    r"{%\s*blocktrans(?P<args>[^%]*)%}(?P<body>.*?){%\s*endblocktrans\s*%}",
    re.DOTALL,
)
VAR_RE = re.compile(r"{{\s*(?P<name>\w+)\s*}}")


def _clean_blocktrans(body: str) -> str:
    body = body.strip()
    body = re.sub(r"\s+", " ", body)
    return VAR_RE.sub(r"%(\g<name>)s", body)


def extract_from_templates() -> set[str]:
    found: set[str] = set()
    for directory in TEMPLATE_DIRS:
        for path in directory.rglob("*"):
            if path.suffix not in {".html", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8")
            found.update(match.group("text") for match in TRANS_RE.finditer(text))
            found.update(_clean_blocktrans(match.group("body")) for match in BLOCKTRANS_RE.finditer(text))
    return found


def _is_gettext_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "_"
    if isinstance(func, ast.Attribute):
        return func.attr in {"gettext", "gettext_lazy"}
    return False


def extract_from_python() -> set[str]:
    found: set[str] = set()
    for directory in PYTHON_DIRS:
        for path in directory.rglob("*.py"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if not _is_gettext_call(node):
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
    return found


def load_existing() -> Catalog:
    if PO_PATH.exists():
        with PO_PATH.open("rb") as handle:
            return read_po(handle)
    return Catalog(locale="pl", project="Resort Neptun", charset="utf-8")


def main() -> None:
    existing = load_existing()
    msgids = extract_from_templates() | extract_from_python()

    catalog = Catalog(
        locale="pl",
        project="Resort Neptun",
        version="0.1.0",
        copyright_holder="Resort Neptun",
        charset="utf-8",
        fuzzy=False,
    )
    catalog.mime_headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Transfer-Encoding", "8bit"),
    ]

    preserved = 0
    for msgid in sorted(msgids):
        old = existing.get(msgid)
        msgstr = (old.string if old and old.string else "") or TRANSLATIONS.get(msgid, "")
        if msgstr:
            preserved += 1
        catalog.add(msgid, msgstr, auto_comments=[])

    LOCALE.mkdir(parents=True, exist_ok=True)
    with PO_PATH.open("wb") as handle:
        write_po(handle, catalog, width=88)

    print(f"Extracted {len(msgids)} strings ({preserved} existing translations kept).")
    print(f"Wrote {PO_PATH}")


if __name__ == "__main__":
    main()
