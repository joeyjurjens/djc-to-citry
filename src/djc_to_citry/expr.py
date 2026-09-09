"""Django template expressions to Python expressions.

Everything here reads a parsed value -- Django's `FilterExpression` or
django-components' `TagValue` -- and never the source text. Both carry the
variable, its lookups and its filter chain as data, so there is nothing to
re-parse.

Citry evaluates Python, so a lookup is already an expression. Filters are the
only real translation: Django resolves them through a registry at render time,
which citry has no equivalent for. A filter therefore becomes either the Python
it stood for, or a callable the component hands to the template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.template.base import Variable

# Django built-ins worth writing as plain Python. Anything absent is refused in
# pure mode rather than guessed at, because importing django.template to run it
# would defeat the point of a citry-only library.
FILTERS: dict[str, str] = {
    "add": "({v} + {0})",
    "capfirst": "({v}[:1].upper() + {v}[1:])",
    "default": "({v} or {0})",
    "default_if_none": "({v} if {v} is not None else {0})",
    "first": "{v}[0]",
    "join": "{0}.join({v})",
    "last": "{v}[-1]",
    "length": "len({v})",
    "lower": "{v}.lower()",
    "safe": "Markup({v})",
    "title": "{v}.title()",
    "upper": "{v}.upper()",
}

# Names the expression above needs in template scope, which citry does not
# provide: its expressions see the component's data, not Python's builtins.
FILTER_SCOPE: dict[str, tuple[str, str]] = {
    "length": ("len", "len"),
    "safe": ("Markup", "citry.Markup"),
}


@dataclass
class Needs:
    """What a component must provide for its translated template to work."""

    scope: dict[str, str] = field(default_factory=dict)
    dicts: frozenset[str] = frozenset()
    refusals: list[str] = field(default_factory=list)

    def refuse(self, why: str) -> None:
        if why not in self.refusals:
            self.refusals.append(why)


def literal(value) -> str:
    return repr(str(value)) if isinstance(value, str) else repr(value)


def _needs_slots(rendered: str, needs: Needs) -> str:
    """`slots` is a parameter of template_data, not a template variable."""
    if rendered.startswith("slots."):
        needs.scope["slots"] = "slots"
    return rendered


def lookup(path, dicts: frozenset[str] = frozenset()) -> str:
    """A dotted Django lookup as a Python expression.

    Django reads `a.b` as a dict key first; citry evaluates Python, where that
    is a subscript. Only names the component declares as dicts are read that
    way -- the rest stay attribute access.
    """
    parts = tuple(path.split(".")) if isinstance(path, str) else tuple(path)
    if (answer := filled(".".join(parts))) is not None:
        return answer
    if parts[:1] == ("forloop",):
        return FORLOOP.get(parts[1] if len(parts) > 1 else "", ".".join(parts))
    if parts[0] in dicts and len(parts) > 1:
        return parts[0] + "".join(f"[{p!r}]" for p in parts[1:])
    return ".".join(parts)


# django-components exposes "was this slot filled?" as a template variable.
# citry answers the same question from the slots the component was given.
FILLED = re.compile(r"^slot_(\w+)_filled$")


def filled(name: str) -> str | None:
    """`slot_x_filled` and `component_vars.is_filled.x` as a citry expression."""
    match = FILLED.match(name)
    if match:
        return f"slots.{match.group(1)} is not None"
    prefix = "component_vars.is_filled."
    if name.startswith(prefix):
        return f"slots.{name[len(prefix) :]} is not None"
    return None


FORLOOP = {
    "counter0": "forloop_index",
    "counter": "(forloop_index + 1)",
    "first": "(forloop_index == 0)",
}


def variable(var, dicts: frozenset[str] = frozenset()) -> str:
    """A Django lookup is already a Python expression."""
    if not isinstance(var, Variable):
        return literal(var)
    if var.literal is not None:
        return literal(var.literal)
    if var.translate:
        return literal(str(var.var))
    return lookup(var.lookups or (), dicts)


def apply_filter(name, func, args: list[str], value: str, needs: Needs) -> str:
    if name in FILTERS:
        if name in FILTER_SCOPE:
            key, source = FILTER_SCOPE[name]
            needs.scope[key] = source
        return FILTERS[name].format(*args, v=value)
    module = getattr(func, "__module__", "") or ""
    if module.startswith("django."):
        needs.refuse(f"Django filter |{name} has no citry equivalent")
        return value
    # The project's own filter is an ordinary function; the component can hand
    # it to the template, which keeps it working inside a loop.
    needs.scope[name] = f"{module}.{getattr(func, '__name__', name)}"
    return f"{name}({', '.join([value, *args])})"


def filter_expression(expression, needs: Needs) -> str:
    value = _needs_slots(variable(expression.var, needs.dicts), needs)
    for func, args in expression.filters:
        rendered = [variable(a, needs.dicts) if is_var else literal(a) for is_var, a in args]
        value = apply_filter(
            getattr(func, "_filter_name", func.__name__), func, rendered, value, needs
        )
    return value


def condition(node, needs: Needs) -> str:
    """Django's if-parser builds an operator tree; every operator is Python."""
    if node.id == "literal":
        return filter_expression(node.value, needs)
    if node.second is None:
        return f"({node.id} {condition(node.first, needs)})"
    return f"({condition(node.first, needs)} {node.id} {condition(node.second, needs)})"


def interpolated(text: str, needs: Needs) -> str:
    """`"a{{ x }}b"` is a template in its own right, so lex it as one."""
    from django.template.base import Parser, TokenType
    from django_components.util.template_parser import parse_template as lex

    parser = Parser([])
    pieces: list[tuple[bool, str]] = []
    for token in lex(text):
        if token.token_type is TokenType.TEXT:
            pieces.append((False, token.contents))
        elif token.token_type is TokenType.VAR:
            pieces.append((True, filter_expression(parser.compile_filter(token.contents), needs)))
    if not pieces:
        return "''"
    if len(pieces) == 1 and pieces[0][0]:
        return pieces[0][1]
    if not any(is_var for is_var, _ in pieces):
        return literal("".join(piece for _, piece in pieces))
    # An f-string keeps this to one expression without needing `str` in scope,
    # which citry's sandbox does not provide.
    body = "".join(
        "{" + piece + "}" if is_var else piece.replace("{", "{{").replace("}", "}}")
        for is_var, piece in pieces
    )
    return 'f"' + body.replace('"', '\\"') + '"'


def tag_value(value, needs: Needs) -> str:
    """django-components reports an argument's kind, so nothing is sniffed."""
    kind = str(value.kind)
    if kind == "string":
        rendered = literal(value.value.content[1:-1])
    elif kind == "template_string":
        rendered = interpolated(value.value.content[1:-1], needs)
    elif kind == "variable":
        rendered = _needs_slots(lookup(value.value.content, needs.dicts), needs)
    else:
        rendered = value.value.content
    for f in value.filters:
        arg = [f.arg.content] if f.arg is not None else []
        rendered = apply_filter(f.name.content, None, arg, rendered, needs)
    return rendered


def literal_text(param) -> str:
    """The static text of a string argument, for names citry spells inline."""
    content = param.value.value.content
    return content[1:-1] if str(param.value.kind) == "string" else content
