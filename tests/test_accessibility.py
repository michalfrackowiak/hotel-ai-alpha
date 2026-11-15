"""Automated accessibility checks for the guest booking and voice pages.

The check parses the rendered HTML with the standard library and asserts the
WCAG 2.1 A/AA invariants that a static audit can verify: a language and title,
one main landmark, a skip link, labelled form controls, named buttons and links,
unique ids, a sane heading order, no positive tabindex and no focusable content
hidden from assistive technology.

Colour contrast is audited against the design tokens in ``static/css/app.css``:
the token pairs the stylesheet relies on are resolved, alpha colours are
composited over their real backdrop, and the WCAG 2.1 AA ratios are checked
(4.5:1 for text, 3:1 for focus rings and control borders).

It complements a manual axe-core run (see docs/PROJECT_MEMORY.md); it is not a
replacement for the live voice interaction.
"""

from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.test import Client

VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

NON_TEXT_INPUTS = {"hidden", "submit", "button", "reset", "image"}

FOCUSABLE = {"a", "button", "input", "select", "textarea"}

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class Node:
    """A small element tree built from :class:`html.parser.HTMLParser`."""

    __slots__ = ("tag", "attrs", "parent", "children", "text_parts")

    def __init__(self, tag: str, attrs: dict[str, str | None], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node] = []
        self.text_parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()

    def get(self, name: str) -> str:
        return (self.attrs.get(name) or "").strip()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.stack = [self.root]
        self.nodes: list[Node] = []

    def _add(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        for node in self.stack:
            node.text_parts.append(data)


def _ancestors(node: Node):
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def _aria_hidden(node: Node) -> bool:
    return node.get("aria-hidden").lower() == "true" or any(
        ancestor.get("aria-hidden").lower() == "true" for ancestor in _ancestors(node)
    )


def _labelled_by_text(node: Node, ids: dict[str, Node]) -> str:
    references = node.get("aria-labelledby")
    if not references:
        return ""
    return " ".join(ids[reference].text for reference in references.split() if reference in ids)


def _accessible_name(node: Node, label_for: dict[str, Node]) -> str:
    if node.get("aria-label"):
        return node.get("aria-label")
    labelled_by = node.get("aria-labelledby")
    if labelled_by:
        return labelled_by
    for ancestor in _ancestors(node):
        if ancestor.tag == "label":
            return ancestor.text
    if node.tag in {"input", "select", "textarea"} and node.get("id") in label_for:
        return label_for[node.get("id")].text
    if node.tag in {"button", "a"}:
        return node.text
    return node.get("title")


def _audit(html: str) -> list[str]:
    builder = _TreeBuilder()
    builder.feed(html)
    nodes = builder.nodes
    problems: list[str] = []

    ids = {node.get("id"): node for node in nodes if node.get("id")}
    duplicates = [value for value, count in Counter(node.get("id") for node in nodes if node.get("id")).items() if count > 1]
    for duplicate in duplicates:
        problems.append(f"duplicate id '{duplicate}'")

    html_nodes = [node for node in nodes if node.tag == "html"]
    if len(html_nodes) != 1:
        problems.append("expected exactly one <html> element")
    elif not html_nodes[0].get("lang"):
        problems.append("<html> is missing a lang attribute")

    titles = [node for node in nodes if node.tag == "title"]
    if not titles or not titles[0].text:
        problems.append("page is missing a non-empty <title>")

    viewport = next((node for node in nodes if node.tag == "meta" and node.get("name") == "viewport"), None)
    if viewport is None:
        problems.append("page is missing a viewport meta tag")
    elif "user-scalable=no" in viewport.get("content").lower():
        problems.append("viewport disables zoom")

    mains = [node for node in nodes if node.tag == "main"]
    if len(mains) != 1:
        problems.append("expected exactly one <main> landmark")
    skip_links = [node for node in nodes if node.tag == "a" and "skip-link" in node.get("class").split()]
    if not skip_links:
        problems.append("page is missing a skip link")
    elif not any(link.get("href").startswith("#") for link in skip_links):
        problems.append("skip link does not target an in-page anchor")

    label_for: dict[str, Node] = {}
    for label in (node for node in nodes if node.tag == "label"):
        target = label.get("for")
        if target:
            label_for[target] = label
            if target not in ids:
                problems.append(f"label points at missing id '{target}'")

    for reference in ("aria-labelledby", "aria-describedby"):
        for node in nodes:
            for value in node.get(reference).split():
                if value not in ids:
                    problems.append(f"{reference} references missing id '{value}'")

    for node in nodes:
        if node.tag == "img" and "alt" not in node.attrs:
            problems.append("image without an alt attribute")
        if node.tag == "svg" and not _aria_hidden(node):
            if node.get("role") != "img" or not (_accessible_name(node, label_for) or node.get("title")):
                problems.append("inline svg is neither decorative nor labelled")
        if node.tag in FOCUSABLE:
            tabindex = node.get("tabindex")
            if tabindex.isdigit() and int(tabindex) > 0:
                problems.append(f"positive tabindex on <{node.tag}>")
        if node.tag in FOCUSABLE and _aria_hidden(node):
            problems.append(f"focusable <{node.tag}> is inside an aria-hidden element")

    for node in nodes:
        if node.tag not in {"input", "select", "textarea"}:
            continue
        if node.tag == "input" and node.get("type").lower() in NON_TEXT_INPUTS:
            continue
        if not _accessible_name(node, label_for):
            problems.append(f"form control <{node.tag}> has no accessible name")

    for node in nodes:
        if node.tag in {"button", "a"} and not _accessible_name(node, label_for):
            problems.append(f"<{node.tag}> has no accessible name")

    for node in nodes:
        if node.tag == "input" and node.get("type").lower() == "radio":
            grouped = any(
                ancestor.tag == "fieldset" or ancestor.get("role") == "radiogroup"
                for ancestor in _ancestors(node)
            )
            if not grouped:
                problems.append("radio button is not inside a fieldset or radiogroup")
                break

    headings = [HEADINGS[node.tag] for node in nodes if node.tag in HEADINGS]
    if headings.count(1) != 1:
        problems.append("expected exactly one <h1>")
    if headings and headings[0] != 1:
        problems.append("first heading is not an <h1>")
    for previous, current in zip(headings, headings[1:], strict=False):
        if current > previous + 1:
            problems.append(f"heading level jumps from h{previous} to h{current}")
            break

# ---------------------------------------------------------------------------
# Colour contrast (static token audit)
# ---------------------------------------------------------------------------

TEXT_CONTRAST = 4.5
NON_TEXT_CONTRAST = 3.0

NAMED_COLOURS = {
    "white": (255, 255, 255, 1.0),
    "black": (0, 0, 0, 1.0),
    "transparent": (0, 0, 0, 0.0),
}

# Text pairs the stylesheet relies on. Foregrounds and backgrounds are token
# names or CSS literals; alpha foregrounds are composited over their backdrop.
TEXT_PAIRS = [
    ("body text on cream", "--ink", "--cream"),
    ("body text on paper", "--ink", "--paper"),
    ("body text on cream-deep", "--ink", "--cream-deep"),
    ("secondary text on cream", "--ink-soft", "--cream"),
    ("secondary text on paper", "--ink-soft", "--paper"),
    ("secondary text on cream-deep", "--ink-soft", "--cream-deep"),
    ("muted text on cream", "--muted", "--cream"),
    ("muted text on paper", "--muted", "--paper"),
    ("muted text on cream-deep", "--muted", "--cream-deep"),
    ("muted text on white", "--muted", "#ffffff"),
    ("link text on cream", "--forest", "--cream"),
    ("link text on paper", "--forest", "--paper"),
    ("eyebrow and step numbers on cream", "--gold-ink", "--cream"),
    ("eyebrow and step numbers on paper", "--gold-ink", "--paper"),
    ("eyebrow and step numbers on cream-deep", "--gold-ink", "--cream-deep"),
    ("working status", "--forest-soft", "--paper"),
    ("danger status on cream", "--danger", "--cream"),
    ("danger status on paper", "--danger", "--paper"),
    ("success status on cream", "--success", "--cream"),
    ("success status on paper", "--success", "--paper"),
    ("warning text on cream", "--warning", "--cream"),
    ("pending badge", "--warning", "--warning-soft"),
    ("confirmed badge", "--success", "--success-soft"),
    ("cancelled badge", "--danger", "--danger-soft"),
    ("primary button", "#ffffff", "--forest"),
    ("danger button hover", "#ffffff", "--danger"),
    ("footer links on green", "--gold-soft", "--forest"),
    ("footer links on deep green", "--gold-soft", "--forest-deep"),
    ("hero heading", "#fdfaf2", "--forest"),
    ("hero body", "#f4efe3", "--forest"),
    ("hero lede", "rgba(244, 239, 227, 0.82)", "--forest"),
    ("hero note", "rgba(244, 239, 227, 0.62)", "--forest"),
    ("cta body", "rgba(244, 239, 227, 0.72)", "--forest"),
    ("footer body", "rgba(244, 239, 227, 0.72)", "--forest-deep"),
    ("call status", "rgba(244, 239, 227, 0.8)", "--forest"),
    ("call hint", "rgba(244, 239, 227, 0.62)", "--forest"),
    ("call toggle", "rgba(244, 239, 227, 0.75)", "--forest"),
    ("accent card body", "rgba(244, 239, 227, 0.85)", "--forest"),
    ("user message", "#fdfaf2", "--forest"),
    ("skip link", "#ffffff", "--forest"),
]

# Non-text pairs: WCAG 1.4.11 wants 3:1 for focus indicators and the visual
# boundary of controls.
NON_TEXT_PAIRS = [
    ("focus ring on cream", "--focus-ring", "--cream"),
    ("focus ring on paper", "--focus-ring", "--paper"),
    ("focus ring on cream-deep", "--focus-ring", "--cream-deep"),
    ("focus ring on green", "--focus-ring", "--forest"),
    ("focus ring on deep green", "--focus-ring", "--forest-deep"),
    ("control border on cream", "--line-strong", "--cream"),
    ("control border on paper", "--line-strong", "--paper"),
    ("control border on white", "--line-strong", "#ffffff"),
    ("decorative gold on green", "--gold", "--forest"),
]


def _parse_colour(value: str) -> tuple[int, int, int, float]:
    """Return ``(r, g, b, alpha)`` for a CSS colour literal."""
    value = value.strip().lower()
    if value in NAMED_COLOURS:
        return NAMED_COLOURS[value]
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) not in {6, 8}:
            raise ValueError(f"unsupported hex colour {value!r}")
        r, g, b = (int(digits[index : index + 2], 16) for index in (0, 2, 4))
        alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
        return (r, g, b, alpha)
    match = re.fullmatch(r"rgba?\(([^)]+)\)", value)
    if not match:
        raise ValueError(f"unsupported colour {value!r}")
    parts = [part.strip() for part in match.group(1).split(",")]
    channels = [round(float(part.rstrip("%")) * (2.55 if part.endswith("%") else 1)) for part in parts[:3]]
    alpha = float(parts[3]) if len(parts) == 4 else 1.0
    return (channels[0], channels[1], channels[2], alpha)


def _css_variables() -> dict[str, str]:
    text = (Path(__file__).resolve().parent.parent / "static" / "css" / "app.css").read_text(encoding="utf-8")
    variables: dict[str, str] = {}
    for match in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", text):
        name, value = match.group(1), match.group(2).strip()
        if value.startswith(("#", "rgb")) or value in NAMED_COLOURS:
            variables[name] = value
    return variables


def _rgba(spec: str, variables: dict[str, str]) -> tuple[int, int, int, float]:
    if spec.startswith("--"):
        if spec not in variables:
            raise KeyError(f"CSS variable {spec} is not defined")
        spec = variables[spec]
    return _parse_colour(spec)


def _flatten(colour: tuple[int, int, int, float], backdrop: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b, alpha = colour
    br, bg, bb = backdrop
    return (
        round(r * alpha + br * (1 - alpha)),
        round(g * alpha + bg * (1 - alpha)),
        round(b * alpha + bb * (1 - alpha)),
    )


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for channel in rgb:
        srgb = channel / 255.0
        channels.append(srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = (l1, l2) if l1 >= l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)


def _pair_ratio(fg_spec: str, bg_spec: str, variables: dict[str, str]) -> float:
    background = _rgba(bg_spec, variables)
    if background[3] != 1.0:
        raise ValueError(f"background {bg_spec!r} must be opaque")
    bg = background[:3]
    fg = _flatten(_rgba(fg_spec, variables), bg)
    return _contrast_ratio(fg, bg)


def _contrast_failures(pairs, minimum: float, variables: dict[str, str]) -> list[str]:
    failures = []
    for label, fg_spec, bg_spec in pairs:
        try:
            ratio = _pair_ratio(fg_spec, bg_spec, variables)
        except (KeyError, ValueError) as error:
            failures.append(f"{label}: {error}")
            continue
        if ratio < minimum:
            failures.append(f"{label} ({fg_spec} on {bg_spec}): {ratio:.2f}:1 (needs {minimum}:1)")
    return failures


def test_text_colour_contrast_meets_wcag_aa():
    failures = _contrast_failures(TEXT_PAIRS, TEXT_CONTRAST, _css_variables())
    assert not failures, "Text contrast below WCAG AA:\n" + "\n".join(f"- {item}" for item in failures)


def test_non_text_colour_contrast_meets_wcag_aa():
    failures = _contrast_failures(NON_TEXT_PAIRS, NON_TEXT_CONTRAST, _css_variables())
    assert not failures, "Non-text contrast below WCAG AA:\n" + "\n".join(f"- {item}" for item in failures)


def _assert_accessible(html: str, label: str) -> None:
    problems = _audit(html)
    assert not problems, f"{label} has accessibility problems:\n" + "\n".join(f"- {problem}" for problem in problems)


@pytest.fixture
def client() -> Client:
    return Client()


def _stay_params(room_type, stay_dates) -> dict[str, str]:
    check_in, check_out = stay_dates
    return {
        "room_type": room_type.slug,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "adults": "2",
        "children": "0",
    }


@pytest.mark.django_db
def test_book_page_is_accessible(client):
    response = client.get("/book/")

    assert response.status_code == 200
    _assert_accessible(response.content.decode(), "/book/")


@pytest.mark.django_db
def test_book_page_is_accessible_in_polish(client):
    response = client.get("/book/", HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    _assert_accessible(response.content.decode(), "/book/ (pl)")


@pytest.mark.django_db
def test_book_quote_with_rate_plans_is_accessible(client, room_type, rooms, stay_dates):
    response = client.get("/book/", _stay_params(room_type, stay_dates))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'role="radiogroup"' in content
    _assert_accessible(content, "/book/ quote")


@pytest.mark.django_db
def test_voice_page_is_accessible(client):
    response = client.get("/voice/")

    assert response.status_code == 200
    _assert_accessible(response.content.decode(), "/voice/")


@pytest.mark.django_db
def test_voice_page_is_accessible_in_polish(client):
    response = client.get("/voice/", HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    _assert_accessible(response.content.decode(), "/voice/ (pl)")
