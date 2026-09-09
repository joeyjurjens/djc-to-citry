"""Locate every Django construct in the HTML that surrounds it.

Neither parser alone is enough. Django's AST knows what each tag means but
reads the HTML as flat text. Citry's parser knows the HTML structure but not
Django's syntax -- unless it is told which byte ranges are foreign, which is
exactly what django-components' lexer reports.

Both parsers report source positions, so running each over the same source and
joining on position gives one view: a Django node, plus where in the HTML it
sits. That is the whole of what the conversion needs to decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from citry_core import _rust
from citry_core.template_parser import parse_template

ForeignSpan = _rust.template_parser.ForeignSpan
ParseOptions = _rust.template_parser.ParseOptions

# Citry parses no tags inside these; a Django tag here has no citry equivalent.
RAW_TEXT = frozenset({"textarea", "script", "style", "title"})

BODY = "body"
DYNAMIC = "dynamic"
START_TAG = "start_tag"
ATTR = "attr"
RAW = "raw"


@dataclass(frozen=True)
class Region:
    """Where a span of source sits in the surrounding HTML."""

    kind: str
    element: str = ""
    attr: str = ""
    value: str = ""


@dataclass(frozen=True)
class Offsets:
    """Django counts characters, citry counts UTF-8 bytes."""

    to_byte: list[int]
    to_char: dict[int, int]

    @classmethod
    def of(cls, source: str) -> Offsets:
        to_byte, n = [0], 0
        for ch in source:
            n += len(ch.encode())
            to_byte.append(n)
        return cls(to_byte, {b: i for i, b in enumerate(to_byte)})


def foreign_spans(source: str, offsets: Offsets) -> list[ForeignSpan]:
    """Every byte range Django owns, per django-components' own lexer."""
    from django_components.util.template_parser import parse_template as lex

    spans: list[ForeignSpan] = []
    verbatim_from: int | None = None
    for token in lex(source):
        if token.token_type.name not in ("BLOCK", "VAR", "COMMENT") or token.position is None:
            continue
        at, to = token.position
        command = token.contents.split()[:1] if token.token_type.name == "BLOCK" else []
        # Claimed whole, body included: Django's lexer hands that body back as
        # plain text, which citry would otherwise parse.
        if command == ["verbatim"]:
            verbatim_from = at
            continue
        if verbatim_from is not None:
            if command != ["endverbatim"]:
                continue
            at, verbatim_from = verbatim_from, None
        spans.append(
            ForeignSpan(
                offsets.to_byte[at],
                offsets.to_byte[to],
                may_control_body=token.token_type.name == "BLOCK",
                provider="djc",
                ordinal=len(spans),
            )
        )
    return spans


def mask_dynamic_tags(source: str) -> tuple[str, set[tuple[int, int]]]:
    """Blank out `<{{ tag }}>` so citry can parse the rest of the template.

    A computed element name is not HTML, so citry's grammar rejects it outright
    and the whole template would be lost. Replacing the expression with letters
    of the same length keeps every other position valid; the ranges come back
    so the caller still knows the name was dynamic.
    """
    from django.template.base import TokenType
    from django_components.util.template_parser import parse_template as lex

    masked, spans = source, set()
    for token in lex(source):
        if token.token_type is not TokenType.VAR:
            continue
        if token.position is None:
            continue
        at, to = token.position
        if not (source[:at].endswith("<") or source[:at].endswith("</")):
            continue
        masked = masked[:at] + "e" * (to - at) + masked[to:]
        spans.add((at, to))
    return masked, spans


def _variant(element) -> tuple[str, Any]:
    return type(element).__name__.rsplit("_", 1)[-1], element._0


def regions(source: str) -> list[tuple[int, int, Region]]:
    """Character ranges of the HTML, innermost last.

    Citry is asked to parse the Django template with the Django parts declared
    foreign, so its own grammar never sees them.
    """
    masked, dynamic = mask_dynamic_tags(source)
    offsets = Offsets.of(masked)
    template = parse_template(
        masked, options=ParseOptions(foreign_spans=foreign_spans(masked, offsets))
    )
    found: list[tuple[int, int, Region]] = []

    def char(token) -> tuple[int, int]:
        return offsets.to_char[token.start_index], offsets.to_char[token.end_index]

    def walk(elements, enclosing: Region) -> None:
        for element in elements:
            kind, node = _variant(element)
            if kind != "Node":
                continue
            start_tag = node.start_tag
            name = start_tag.name.content.lower()
            at, to = char(start_tag.token)
            is_dynamic = any(at <= d[0] and d[1] <= to for d in dynamic)
            # A masked name is a placeholder, not the element's real name.
            found.append((at, to, Region(DYNAMIC, "") if is_dynamic else Region(START_TAG, name)))
            for attr in start_tag.attrs:
                found.append(
                    (
                        *char(attr.token),
                        Region(
                            ATTR,
                            name,
                            attr.key.content,
                            attr.inner_value.content if attr.inner_value else "",
                        ),
                    )
                )
            end_tag = getattr(node, "end_tag", None)
            if end_tag is None:
                continue
            if is_dynamic:
                found.append((*char(end_tag.token), Region(DYNAMIC, "")))
            inner = Region(RAW if name in RAW_TEXT else BODY, "" if is_dynamic else name)
            found.append((char(start_tag.token)[1], char(end_tag.token)[0], inner))
            walk(node.body.elements, inner)

    walk(template.elements, Region(BODY))
    found.sort(key=lambda r: (r[0], -(r[1] - r[0])))
    return found


def region_at(found: list[tuple[int, int, Region]], start: int, end: int) -> Region:
    """The innermost region containing this span."""
    best = Region(BODY)
    for at, to, region in found:
        if at <= start and end <= to:
            best = region
    return best
