"""Automated accessibility checks for the guest booking and voice pages.

The check parses the rendered HTML with the standard library and asserts the
WCAG 2.1 A/AA invariants that a static audit can verify: a language and title,
one main landmark, a skip link, labelled form controls, named buttons and links,
unique ids, a sane heading order, no positive tabindex and no focusable content
hidden from assistive technology.

It complements a manual axe-core run (see docs/PROJECT_MEMORY.md); it is not a
replacement for testing colour contrast or the live voice interaction.
"""

from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser

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

    return problems


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
