"""Write the citry form of each django-components construct.

The template is already HTML with Django tags as islands in it, so nothing is
rebuilt: every construct becomes one replacement over its own source range and
the text between them is copied through untouched. Whitespace survives, and
citry's parser is what says whether the result is valid.

Dispatch is on the node class Django itself produced. A construct with no entry
in `EMIT` is not silently passed through -- it becomes a marker, so an
unhandled tag is impossible to miss.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from itertools import pairwise

from citry_core.template_parser import parse_template
from django.template.base import TokenType

from . import expr
from .expr import Needs
from .locate import ATTR, DYNAMIC, RAW, START_TAG, region_at, regions


@dataclass
class Marker:
    """A construct that cannot be translated, for a human or an agent to fix."""

    rule: str
    what: str
    why: str
    source: str = ""
    component: str = ""

    def at(self, component: str) -> Marker:
        return Marker(self.rule, self.what, self.why, self.source, component)

    def to_dict(self) -> dict:
        return asdict(self)

    def as_comment(self, indent: str = "") -> str:
        return (
            f"{indent}# MIGRATE [{self.rule}] {self.what}\n"
            f"{indent}#   why: {self.why}\n"
            f"{indent}#   was: {self.source}"
        )


@dataclass
class Translation:
    template: str
    kept_django: bool = False
    needs: Needs = field(default_factory=Needs)
    markers: list[Marker] = field(default_factory=list)
    hoists: list[tuple[str, str]] = field(default_factory=list)
    imports: set[tuple[str, str]] = field(default_factory=set)
    slots: dict[str, dict] = field(default_factory=dict)
    components: set[str] = field(default_factory=set)


def quoted(value: str) -> str:
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', "'") + '"'


def tag_name(name: str) -> str:
    """A component name as citry spells it: hyphenated and lowercase.

    Citry registers both `card-body` and `cardbody` for a `CardBody` class, so
    either resolves; the hyphenated form is the one that reads as HTML.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", name)
    return spaced.replace("_", "-").replace(".", "-").lower()


class Writer:
    """Collects what the walk produces for one template."""

    def __init__(self, source: str, mode: str):
        self.source = source
        self.mode = mode
        self.dict_sequences: frozenset[str] = frozenset()
        self.result = Translation("")
        self.regions = regions(source)
        self.counters: dict[str, int] = {}

    @property
    def needs(self) -> Needs:
        return self.result.needs

    def region(self, at: int, to: int):
        return region_at(self.regions, at, to)

    def hoist(self, expression: str, stem: str) -> str:
        for name, existing in self.result.hoists:
            if existing == expression:
                return name
        self.counters[stem] = self.counters.get(stem, 0) + 1
        n = self.counters[stem]
        name = stem if n == 1 else f"{stem}{n}"
        self.result.hoists.append((name, expression))
        return name

    def refuse(self, rule: str, what: str, why: str, at: int, to: int) -> None:
        self.result.markers.append(Marker(rule, what, why, self.source[at:to]))

    def value(self, param) -> str:
        return expr.tag_value(param.value, self.needs)

    def keywords(self, node) -> str:
        """Keyword arguments of a django-components tag, as citry attributes.

        A quoted literal is a static attribute; anything else is a Python
        expression, which citry spells with the `c-` prefix. An empty literal
        is the exception: citry reads `x=""` as the boolean `True`, so the
        empty string has to be written as an expression.
        """
        out: list[str] = []
        # `attrs:foo=bar` collects into one dict named `attrs`; citry has no
        # such syntax, so the dict is built here instead.
        groups: dict[str, list[str]] = {}
        for param in node.params:
            if param.key is None:
                continue
            key = param.key.content
            if ":" in key:
                prefix, _, inner = key.partition(":")
                groups.setdefault(prefix, []).append(f"{inner!r}: {self.value(param)}")
            elif (
                str(param.value.kind) == "string"
                and not param.value.filters
                and expr.literal_text(param) != ""
            ):
                out.append(f" {key}={quoted(expr.literal_text(param))}")
            else:
                out.append(f" c-{key}={quoted(self.value(param))}")
        for prefix, pairs in groups.items():
            out.append(f" c-{prefix}={quoted('{' + ', '.join(pairs) + '}')}")
        return "".join(out)


def positional(node, index: int = 0):
    found = [p for p in node.params if p.key is None]
    return found[index] if index < len(found) else None


def _slot(node, w: Writer) -> tuple[str, str]:
    first = positional(node)
    name = expr.literal_text(first) if first is not None else "default"
    flags = set(node.active_flags or ())
    entry = w.result.slots.setdefault(name, {"required": False, "fallback": False})
    entry["required"] |= "required" in flags
    entry["fallback"] |= bool(node.nodelist)
    attrs = "" if name == "default" else f' name="{name}"'
    if "required" in flags:
        attrs += " required"
    return f"<c-slot{attrs}>", "</c-slot>"


def _fill(node, w: Writer) -> tuple[str, str]:
    first = positional(node)
    name = expr.literal_text(first) if first is not None else "default"
    return f'<c-fill name="{name}">', "</c-fill>"


def _component(node, w: Writer) -> tuple[str, str]:
    name = tag_name(node.name)
    w.result.components.add(node.name)
    return f"<c-{name}{w.keywords(node)}>", f"</c-{name}>"


def _provide(node, w: Writer) -> tuple[str, str]:
    first = positional(node)
    key = expr.literal_text(first) if first is not None else ""
    return f'<c-provide key="{key}"{w.keywords(node)}>', "</c-provide>"


def _dependencies(node, w: Writer) -> tuple[str, str]:
    return ("<c-css />", "") if "Css" in type(node).__name__ else ("<c-js />", "")


def _cache(node, w: Writer) -> tuple[str, str]:
    parts = ""
    if node.expire_time_var is not None:
        parts += f" ttl={quoted(expr.filter_expression(node.expire_time_var, w.needs))}"
    return f"<c-cache{parts}>", "</c-cache>"


def _html_attrs(node, w: Writer) -> tuple[str, str]:
    """The three positions of `{% html_attrs %}`: defaults, sources, literals."""
    defaults, literals, sources = [], [], []
    for param in node.params:
        rendered = w.value(param)
        if param.key is None:
            sources.append(f"({rendered} or {{}})")
        elif param.key.content.startswith("defaults:"):
            defaults.append((param.key.content.split(":", 1)[1], rendered))
        else:
            literals.append((param.key.content, rendered))

    parts = []
    if defaults:
        parts.append("{" + ", ".join(f"{k!r}: {v}" for k, v in defaults) + "}")
    parts.extend(sources)
    if literals:
        parts.append("{" + ", ".join(f"{k!r}: {v}" for k, v in literals) + "}")
    if not parts:
        return "", ""
    w.result.imports.add(("citry", "merge_attrs"))
    name = w.hoist(f"merge_attrs({', '.join(parts)})", "merged_attrs")
    return f'c-bind="{name}"', ""


EMIT = {
    "SlotNode": _slot,
    "FillNode": _fill,
    "ComponentNode": _component,
    "ProvideNode": _provide,
    "HtmlAttrsNode": _html_attrs,
    "ComponentCssDependenciesNode": _dependencies,
    "ComponentJsDependenciesNode": _dependencies,
    "DjcCacheNode": _cache,
}

# Tags that carry no meaning once the template is citry.
DROPPED = {"LoadNode", "CommentNode"}


def _start_tag(w: Writer, at: int, to: int):
    """Range of the start tag this span sits in, if any."""
    found = None
    for start, stop, region in w.regions:
        if region.kind in (START_TAG, DYNAMIC) and start <= at and to <= stop:
            found = (start, stop)
    return found


def _merge_parts(node, w: Writer) -> list[str]:
    """The three positions of `{% html_attrs %}`: defaults, sources, literals."""
    defaults, literals, sources = [], [], []
    for param in node.params:
        rendered = w.value(param)
        if param.key is None:
            sources.append(f"({rendered} or {{}})")
        elif param.key.content.startswith("defaults:"):
            defaults.append((param.key.content.split(":", 1)[1], rendered))
        else:
            literals.append((param.key.content, rendered))
    parts = []
    if defaults:
        parts.append("{" + ", ".join(f"{k!r}: {v}" for k, v in defaults) + "}")
    parts.extend(sources)
    if literals:
        parts.append("{" + ", ".join(f"{k!r}: {v}" for k, v in literals) + "}")
    return parts


def _conditional_part(node, tokens, index: int, end: int, w: Writer) -> str | None:
    """A conditional group of attributes as one dict expression."""
    branches = _branch_ranges(tokens, index, end)
    spans = [(t.position[0], t.position[1]) for t in tokens]
    parts = []
    for branch, (first, last) in enumerate(branches):
        pairs = _attrs_between(w, first, last, spans)
        if pairs is None:
            return None
        body = "{" + ", ".join(f"{k!r}: {v}" for k, v in pairs) + "}"
        parts.append((_condition(node, branch, w), body))
    expression = "{}"
    for condition, body in reversed(parts):
        expression = body if not condition else f"({body} if {condition} else {expression})"
    return expression


def _whole_spans(source: str, tokens, w: Writer, nodes) -> dict[int, tuple[int, str]]:
    """Ranges citry cannot express piecemeal, replaced as a unit.

    Everything contributing attributes to the same start tag - every
    `{% html_attrs %}` and every conditional group - is merged into one
    `merge_attrs` call. Citry does allow several `c-bind` attributes, but it
    merges `class` and `style` across them only on HTML elements; component
    inputs are last-one-wins. Merging in Python keeps `{% html_attrs %}`
    behaving the same on both.
    """
    spans: dict[int, tuple[int, str]] = {}
    inside = [(t.position[0], t.position[1]) for t in tokens]
    groups: dict[tuple[int, int], list[tuple[int, int, list[str]]]] = {}

    for index, token in enumerate(tokens):
        at, to = token.position
        node = nodes.get((at, to))
        kind = type(node).__name__.split("_")[0] if node is not None else ""
        owner = _start_tag(w, at, to)
        if owner is None:
            continue
        if kind == "HtmlAttrsNode":
            groups.setdefault(owner, []).append((at, to, _merge_parts(node, w)))
        elif kind == "IfNode":
            end = _matching_end(tokens, index)
            if end is None:
                continue
            part = _conditional_part(node, tokens, index, end, w)
            if part is None:
                continue
            groups.setdefault(owner, []).append((at, tokens[end].position[1], [part]))

    for items in groups.values():
        items.sort()
        parts = [part for _, _, group in items for part in group]
        if not parts:
            continue
        w.result.imports.add(("citry", "merge_attrs"))
        expression = parts[0] if len(parts) == 1 else f"merge_attrs({', '.join(parts)})"
        element = w.region(*items[0][:2]).element or "element"
        name = w.hoist(expression, f"{element.replace('-', '_')}_attrs")
        for position, (at, to, _) in enumerate(items):
            spans[at] = (to, f'c-bind="{name}"' if position == 0 else "")

    for at, to, region in w.regions:
        if region.kind != ATTR or not any(at <= s and e <= to for s, e in inside):
            continue
        if any(start <= at and to <= stop for start, (stop, _) in spans.items()):
            continue
        rendered = value_expression(region.value, w)
        if rendered is None:
            continue
        spans[at] = (to, f"c-{region.attr}={quoted(rendered)}")
    return spans


def value_expression(text: str, w: Writer) -> str | None:
    """An attribute value as one Python expression.

    A value is a template in its own right, so a `{% for %}` in it is parsed
    like any other and folded into the expression citry needs.
    """
    if "{%" not in text:
        return expr.interpolated(text, w.needs)
    from django.template import engines

    nodelist = engines["django"].engine.from_string(text).nodelist
    return _as_expression(list(nodelist), w)


def _nodes_in(nodelist, at: int, to: int) -> list:
    """The run of sibling nodes covering a source range."""
    found = []
    for node in nodelist:
        token = getattr(node, "token", None)
        if token is None or token.position is None:
            continue
        start, stop = token.position
        if at <= start and stop <= to:
            found.append(node)
        elif start < to and at < stop:
            for name in getattr(node, "child_nodelists", ()):
                if (sub := getattr(node, name, None)) is not None:
                    found.extend(_nodes_in(sub, at, to))
    return found


def _as_expression(nodes, w: Writer) -> str | None:
    """Nodes as one Python expression, or None if they cannot be.

    Citry parses no tags inside a raw-text element but does interpolate, so
    the only way to keep the logic is to move it into a single expression.
    """
    parts = []
    for node in nodes:
        kind = type(node).__name__.split("_")[0]
        if kind == "TextNode":
            if node.s:
                parts.append(repr(node.s))
        elif kind == "VariableNode":
            parts.append(f"str({expr.filter_expression(node.filter_expression, w.needs)})")
        elif kind == "SlotNode":
            first = positional(node)
            name = expr.literal_text(first) if first is not None else "default"
            fallback = _as_expression(list(node.nodelist), w) if node.nodelist else "''"
            if fallback is None:
                return None
            parts.append(f"(str(slots.{name}) if slots.{name} is not None else {fallback})")
        elif kind == "ForNode":
            inner = _as_expression(list(node.nodelist_loop), w)
            if inner is None:
                return None
            target = ", ".join(node.loopvars)
            # Django resolves a lookup by calling it when it is callable; this
            # runs in template_data, where that test can be written out.
            sequence = expr.filter_expression(node.sequence, w.needs)
            called = f"({sequence}() if callable({sequence}) else {sequence})"
            parts.append(f"''.join({inner} for {target} in {called})")
        elif kind == "IfNode":
            rendered = "''"
            for branch in reversed(range(len(node.conditions_nodelists))):
                condition, nodelist = node.conditions_nodelists[branch]
                inner = _as_expression(list(nodelist), w)
                if inner is None:
                    return None
                if condition is None:
                    rendered = inner
                else:
                    rendered = f"({inner} if {expr.condition(condition, w.needs)} else {rendered})"
            parts.append(rendered)
        else:
            return None
    return " + ".join(parts) if parts else "''"


def _raw_spans(w: Writer, nodelist, tokens) -> dict[int, tuple[int, str]]:
    spans: dict[int, tuple[int, str]] = {}
    blocks = [t.position for t in tokens if t.token_type is TokenType.BLOCK]
    for at, to, region in w.regions:
        if region.kind != RAW or not any(at <= s and e <= to for s, e in blocks):
            continue
        rendered = _as_expression(_nodes_in(nodelist, at, to), w)
        if rendered is None:
            continue
        spans[at] = (to, "{{ " + w.hoist(rendered, f"{region.element}_content") + " }}")
    return spans


def _span_around(spans: dict[int, tuple[int, str]], at: int, to: int):
    for start, (end, replacement) in spans.items():
        if start <= at and to <= end:
            return start, end, replacement
    return None


def _matching_end(tokens, index: int, word: str = "if") -> int | None:
    """Index of the `{% end... %}` that closes the tag at `index`."""
    depth = 0
    for i in range(index, len(tokens)):
        this = (tokens[i].contents.split()[:1] or [""])[0]
        if this == word:
            depth += 1
        elif this == f"end{word}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _branch_ranges(tokens, index: int, end: int) -> list[tuple[int, int]]:
    """Source range of each branch body, in order."""
    marks = [index]
    depth = 0
    for i in range(index, end):
        word = (tokens[i].contents.split()[:1] or [""])[0]
        if word == "if":
            depth += 1
        elif word == "endif":
            depth -= 1
        elif depth == 1 and word in ("elif", "else"):
            marks.append(i)
    marks.append(end)
    return [(tokens[a].position[1], tokens[b].position[0]) for a, b in pairwise(marks)]


def _attrs_between(w: Writer, first: int, last: int, inside) -> list[tuple[str, str]] | None:
    pairs = []
    for at, to, region in w.regions:
        if region.kind != ATTR or not (first <= at and to <= last):
            continue
        rendered = value_expression(region.value, w)
        if rendered is None:
            return None
        pairs.append((region.attr, rendered))
    return pairs


@dataclass
class Frame:
    """An open construct, and how to close it."""

    close: str
    node: object = None
    branch: int = 0


def _index(nodelist, into: dict) -> dict:
    for node in nodelist:
        token = getattr(node, "token", None)
        if token is not None and token.position is not None:
            into.setdefault(tuple(token.position), node)
        for name in getattr(node, "child_nodelists", ()):
            if (sub := getattr(node, name, None)) is not None:
                _index(sub, into)
        for _, sub in getattr(node, "conditions_nodelists", None) or []:
            _index(sub, into)
    return into


def _condition(node, branch: int, w: Writer) -> str:
    condition = node.conditions_nodelists[branch][0]
    return expr.condition(condition, w.needs) if condition is not None else ""


def _open_if(node, w: Writer, branch: int = 0) -> tuple[str, Frame]:
    condition = _condition(node, branch, w)
    if not condition:
        return "<c-else>", Frame("</c-else>", node, branch)
    tag = "c-if" if branch == 0 else "c-elif"
    return f"<{tag} cond={quoted(condition)}>", Frame(f"</{tag}>", node, branch)


def _open_for(node, w: Writer, source: str) -> tuple[str, Frame]:
    target = ", ".join(node.loopvars)
    iterable = expr.filter_expression(node.sequence, w.needs)
    if iterable in w.dict_sequences:
        w.needs.dicts |= frozenset(node.loopvars)
    # citry has no loop variable, so a body that reads `forloop` gets the index
    # from an enumeration the component prepares.
    body = source[node.token.position[1] :]
    if "forloop." in body[: body.find("{% endfor")] if "{% endfor" in body else False:
        iterable = w.hoist(f"list(enumerate({iterable}))", "loop")
        target = f"forloop_index, {target}"
    return f"<c-for each={quoted(f'{target} in {iterable}')}>", Frame("</c-for>", node)


def translate(
    source: str, mode: str = "pure", dict_sequences: frozenset[str] = frozenset()
) -> Translation:
    from django.template import engines
    from django.template.base import Parser
    from django_components.util.template_parser import parse_template as lex

    engine = engines["django"].engine
    parser = Parser([], libraries=engine.template_libraries, builtins=engine.template_builtins)
    w = Writer(source, mode)
    w.result.needs.dicts = frozenset()
    w.dict_sequences = dict_sequences
    nodelist = engine.from_string(source).nodelist
    nodes = _index(nodelist, {})

    tokens = [t for t in lex(source) if t.token_type is not TokenType.TEXT and t.position]
    spans = _whole_spans(source, tokens, w, nodes)
    spans |= _raw_spans(w, nodelist, tokens)

    out: list[str] = []
    stack: list[Frame] = []
    cursor = 0

    for token in tokens:
        at, to = token.position or (0, 0)
        if at < cursor:
            continue
        span = _span_around(spans, at, to)
        if span is not None:
            start, end, replacement = span
            out.append(source[cursor:start])
            if replacement.startswith("c-") and not "".join(out[-2:])[-1:].isspace():
                out.append(" ")
            out.append(replacement)
            cursor = end
            continue
        out.append(source[cursor:at])
        cursor = to
        region = w.region(at, to)
        node = nodes.get((at, to))
        command = token.contents.split()[:1] if token.token_type is TokenType.BLOCK else []
        word = command[0] if command else ""

        if region.kind == RAW:
            w.refuse(
                "T-RAWTEXT",
                f"{token.contents!r} inside <{region.element}>",
                f"citry parses no tags inside <{region.element}>, so this would render literally",
                at,
                to,
            )
            out.append(source[at:to])
            continue

        if token.token_type is TokenType.COMMENT:
            continue

        if token.token_type is TokenType.VAR:
            rendered = expr.filter_expression(parser.compile_filter(token.contents), w.needs)
            if region.kind == DYNAMIC:
                closing = source[:at].endswith("</")
                out.append("c-element" if closing else f"c-element c-is={quoted(rendered)}")
            else:
                out.append("{{ " + rendered + " }}")
            continue

        if word.startswith("end") and stack:
            out.append(stack.pop().close)
            continue

        if word in ("else", "elif") and stack:
            frame = stack.pop()
            out.append(frame.close)
            text, frame = _open_if(frame.node, w, frame.branch + 1)
            out.append(text)
            stack.append(frame)
            continue

        if word == "empty" and stack:
            out.append(stack.pop().close)
            out.append("<c-empty>")
            stack.append(Frame("</c-empty>"))
            continue

        if node is not None and type(node).__name__ in DROPPED:
            # A dropped tag with a body takes the body with it.
            end = _matching_end(tokens, tokens.index(token), word)
            if end is not None:
                cursor = (tokens[end].position or (0, cursor))[1]
            continue

        if node is None:
            if word not in ("load",):
                w.refuse("T-UNKNOWN", f"{{% {word} %}}", "no citry equivalent is known", at, to)
                out.append(source[at:to])
            continue

        kind = type(node).__name__.split("_")[0]
        if kind == "IfNode":
            text, frame = _open_if(node, w)
            out.append(text)
            stack.append(frame)
            continue
        if kind == "ForNode":
            text, frame = _open_for(node, w, source)
            out.append(text)
            stack.append(frame)
            continue

        handler = EMIT.get(kind)
        if handler is None:
            w.refuse("T-UNKNOWN", f"{{% {word} %}}", f"{kind} has no citry equivalent", at, to)
            out.append(source[at:to])
            continue
        opening, closing = handler(node, w)
        # Only a tag written self-closing has no `{% end %}` to pair with.
        if closing and token.contents.rstrip().endswith("/"):
            opening = opening[:-1] + " />"
            closing = ""
        out.append(opening)
        if closing:
            stack.append(Frame(closing))

    out.append(source[cursor:])
    w.result.template = "".join(out)

    # Names the template asks for that citry does not put in scope itself.
    for name, source_expression in w.needs.scope.items():
        module, _, attribute = source_expression.rpartition(".")
        if module:
            w.result.imports.add((module, attribute))
        w.result.hoists.append((name, attribute))

    # Citry's own parser is the authority on whether the result is expressible.
    # A branch that opens an element it does not close is the usual reason it
    # is not: Django works on text, citry on structure.
    try:
        parse_template(w.result.template)
    except SyntaxError as exc:
        why = str(exc).split("\n")[0]
        if mode == "compat":
            return Translation(source, kept_django=True, needs=w.needs)
        w.result.markers.append(
            Marker("T-STRADDLE", "citry rejects the translated template", why, source.strip())
        )
    return w.result


@dataclass
class Rewrite:
    """The result of translating template text found in a file."""

    text: str
    translated: int = 0
    markers: list[Marker] = field(default_factory=list)


def rewrite(text: str, mode: str = "pure") -> Rewrite:
    """Translate a whole template file, or the template literals in a module."""
    if not text.lstrip().startswith(("import ", "from ")) and "class " not in text:
        result = translate(text, mode=mode)
        return Rewrite(result.template, 1, result.markers)

    from .codemod import TEMPLATE_RE

    seen = Rewrite("", 0, [])

    def one(match):
        result = translate(match.group("body"), mode=mode)
        seen.translated += 1
        seen.markers.extend(result.markers)
        indent = match.group("indent")
        body = "\n".join(
            (indent + "    " + line.strip()) if line.strip() else ""
            for line in result.template.strip().splitlines()
        )
        prefix = "r" if "\\" in body else ""
        return f'{indent}template = {prefix}"""\n{body}\n{indent}"""'

    seen.text = TEMPLATE_RE.sub(one, text)
    return seen
