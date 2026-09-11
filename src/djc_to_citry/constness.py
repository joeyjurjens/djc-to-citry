"""Find component code that citry's constant proxy would break.

The proxy behaves like the value it wraps until something asks what it really
is. Two shapes are decidable from the code alone: an identity test, which is
silently False, and a call into an API that demands the exact built-in type,
which raises. A call into a helper that does either of those inside is not
decidable here, so the marker says what it found, not that it found everything.
"""

from __future__ import annotations

import libcst as cst

from .emit import Marker

#: Callables that reject a proxy even though `isinstance` says it is a str.
#: `re` and `os.path` are the ones migrated code reaches for; the rest are the
#: cases citry's own constness docs name.
HOSTILE_ROOTS = frozenset({"re", "json", "os", "shutil", "codecs"})
HOSTILE_NAMES = frozenset({"getattr", "setattr", "hasattr", "open", "Path", "fspath"})

WHY = (
    "citry marks a template constant with a transparent proxy, which passes "
    "isinstance() but is not the value itself, so an identity test is silently "
    "False and a C-level API rejects it. Install the PlainInputs extension "
    "(djc-to-citry extension --out yourapp/plain_inputs.py) until citry hands "
    "component code plain values itself."
)


class _Reads(cst.CSTVisitor):
    """Does this expression read the component's inputs?"""

    def __init__(self) -> None:
        self.seen = False

    def visit_Name(self, node: cst.Name) -> None:
        if node.value == "kwargs":
            self.seen = True


def _reads_inputs(node: cst.CSTNode) -> bool:
    visitor = _Reads()
    node.visit(visitor)
    return visitor.seen


def _root(node: cst.BaseExpression) -> str:
    while isinstance(node, cst.Attribute):
        node = node.value
    return node.value if isinstance(node, cst.Name) else ""


def _leaf(node: cst.BaseExpression) -> str:
    if isinstance(node, cst.Attribute):
        return node.attr.value
    return node.value if isinstance(node, cst.Name) else ""


def _source(node: cst.CSTNode) -> str:
    return cst.Module([]).code_for_node(node).strip()


class _Finder(cst.CSTVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []
        self._classes: list[str] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._classes.append(node.name.value)

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self._classes.pop()

    def _record(self, node: cst.CSTNode) -> None:
        # the outermost class is the component; a nested one is its Kwargs
        self.hits.append((self._classes[0] if self._classes else "", _source(node)))

    def visit_Comparison(self, node: cst.Comparison) -> None:
        for target in node.comparisons:
            if not isinstance(target.operator, (cst.Is, cst.IsNot)):
                continue
            if _reads_inputs(node.left) or _reads_inputs(target.comparator):
                self._record(node)
                return

    def visit_Call(self, node: cst.Call) -> None:
        if _root(node.func) not in HOSTILE_ROOTS and _leaf(node.func) not in HOSTILE_NAMES:
            return
        if any(_reads_inputs(arg.value) for arg in node.args):
            self._record(node)


def advise(source: str) -> list[Marker]:
    """One marker per component whose code the proxy would break."""
    finder = _Finder()
    cst.parse_module(source).visit(finder)
    first: dict[str, str] = {}
    for component, snippet in finder.hits:
        first.setdefault(component, snippet)
    return [
        Marker("PY-CONST", "reads an input the constant proxy breaks", WHY, snippet).at(component)
        for component, snippet in first.items()
    ]
