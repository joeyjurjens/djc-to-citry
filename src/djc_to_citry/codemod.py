"""Rewrite django-components component source into citry source."""

from __future__ import annotations

import ast
import builtins
import re
from typing import Any

import libcst as cst
from citry_core.template_parser import parse_template

from .emit import Marker, translate

# The Python side of the move. A django-components hook either has a citry
# method with the same job, or it has none and the component needs redesigning.
RENAMES = {
    "get_template_data": "template_data",
    "get_js_data": "js_data",
    "get_css_data": "css_data",
}

# What a name imported from django-components becomes in citry. A name that is
# absent from both has no counterpart, so dropping its import would leave it
# undefined -- that is a refusal, never a silent rewrite.
RENAMED = {
    "mark_safe": "Markup",
    "merge_attributes": "merge_attrs",
    "format_attributes": "format_attrs",
    "SlotInput": "SlotInput",
    "Slot": "Slot",
}

# Names that legitimately disappear: the base class is replaced, the typing
# helpers have no runtime effect, and citry reads a plain nested config class.
DISCARDED = {"Component", "types", "Context"}

# django-components configures a component through a subclass; citry reads the
# same settings off a plain nested class, so the base simply goes.
CONFIG_BASES = {"ComponentCache", "ComponentView", "ComponentDefaults"}

PARADIGM = {
    "on_render_before": (
        "citry has one on_render() generator: code before the yield replaces "
        "on_render_before, code after it replaces on_render_after."
    ),
    "on_render": "django-components' on_render takes (context, template); citry's yields.",
}


class Unmigratable(Exception):
    """A construct the tool refuses to translate rather than guess at."""

    def __init__(self, rule: str, what: str, why: str):
        super().__init__(what)
        self.marker = Marker(rule, what, why)


class ComponentTransformer(cst.CSTTransformer):
    def __init__(
        self,
        base: str = "Component",
        renames: dict[str, str] | None = None,
        app: str = "",
    ):
        self.base = base
        self.app = app
        self.needs_context_stash = False
        self.needs_markup = False
        self._in_render_hook = False
        self.renames = renames or {}
        self.dropped: set[str] = set()
        self.defaults: dict[str, dict[str, str]] = {}
        self.needs_dataclass = False
        self.reports: list[dict] = []
        self.needs_merge_attrs = False
        self._cls: list[str] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._cls.append(node.name.value)

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> Any:
        name = self._cls.pop()
        if name == "Defaults" and self._cls and self._cls[-1] in self.defaults:
            # citry reads defaults off the Kwargs field itself.
            return cst.RemoveFromParent()
        if name == "Kwargs" and self._cls:
            return self._with_defaults(updated_node, self.defaults.get(self._cls[-1], {}))
        if name == "Args" and self._cls:
            raise Unmigratable(
                "PY-ARGS",
                f"{self._cls[-1]} declares `class Args` (positional inputs)",
                "citry has no positional inputs at all: `Component` has no Args and a "
                "positional call raises TypeError. Every field must become a named "
                "Kwargs field, which changes this component's public API \u2014 a call "
                "the tool must not make on your behalf.",
            )
        if any(
            isinstance(b.value, cst.Name) and b.value.value == "Component"
            for b in original_node.bases
        ):
            updated_node = updated_node.with_changes(bases=[cst.Arg(value=cst.Name(self.base))])
            if self.app and self.base == "Component":
                # An ordinary component belongs to one engine; a library
                # definition may not name one at all.
                binding = cst.parse_statement(f"citry = {self.app.rpartition('.')[2]}")
                body = updated_node.body
                return updated_node.with_changes(body=body.with_changes(body=[binding, *body.body]))
            return updated_node
        kept = [
            b
            for b in updated_node.bases
            if not (isinstance(b.value, cst.Name) and b.value.value in CONFIG_BASES)
        ]
        if len(kept) != len(updated_node.bases):
            self._check_config_class(name, updated_node)
        if len(kept) != len(updated_node.bases):
            if kept:
                kept = kept[:-1] + [kept[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)]
                return updated_node.with_changes(bases=kept)
            return updated_node.with_changes(
                bases=[], lpar=cst.MaybeSentinel.DEFAULT, rpar=cst.MaybeSentinel.DEFAULT
            )
        return updated_node

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom | cst.RemovalSentinel:
        mod = getattr(updated_node.module, "value", None)
        names = original_node.names
        if isinstance(names, cst.ImportStar):
            raise Unmigratable(
                "PY-IMPORT",
                f"`from {mod} import *` cannot be rewritten",
                "The codemod maps names one by one; a star import hides which of "
                "them come from django-components.",
            )
        if mod == "django_components":
            self._account_for(names)
            wanted = [
                RENAMED[str(n.name.value)]
                for n in names
                if str(n.name.value) in RENAMED and str(n.name.value) != "mark_safe"
            ]
            wanted.append(self.base)
            if self.needs_merge_attrs:
                wanted.append("merge_attrs")
            if self.needs_markup:
                wanted.append("Markup")
            return updated_node.with_changes(
                module=cst.Name("citry"),
                names=[cst.ImportAlias(name=cst.Name(n)) for n in sorted(set(wanted))],
            )
        dotted = _dotted(updated_node.module) if updated_node.module else ""
        root = dotted.split(".")[0]
        if root == "django_components":
            self._account_for(names)
            return cst.RemoveFromParent()
        if root == "django":
            # An ordinary Django utility is just a function; only the names
            # citry replaces are taken out, and the rest are left importable.
            self._account_for(names)
            kept = [
                n
                for n in names
                if str(n.name.value) not in RENAMED and str(n.name.value) not in DISCARDED
            ]
            for alias in kept:
                self._keep(str(alias.name.value))
            if not kept:
                return cst.RemoveFromParent()
            kept = kept[:-1] + [kept[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)]
            return updated_node.with_changes(names=kept)
        for old, new in self.renames.items():
            if dotted == old or dotted.startswith(f"{old}."):
                return updated_node.with_changes(
                    module=cst.parse_expression(new + dotted[len(old) :])
                )
        return updated_node

    def _check_config_class(self, name: str, node: cst.ClassDef) -> None:
        """citry validates a config class field by field, as literal values.

        django-components computes the same settings on a subclass, so a
        property or method there has no citry counterpart.
        """
        for statement in node.body.body:
            if isinstance(statement, cst.FunctionDef):
                raise Unmigratable(
                    "PY-CONFIG",
                    f"{self._cls[-1] if self._cls else '?'}.{name} computes "
                    f"{statement.name.value} at runtime",
                    "citry reads a nested config class as literal values and rejects "
                    "anything else, so this setting has to move into the component.",
                )

    def _with_defaults(self, node: cst.ClassDef, defaults: dict[str, str]) -> cst.ClassDef:
        if not defaults:
            return node
        body = []
        factory = False
        for statement in node.body.body:
            field_name = _annotated_name(statement)
            if field_name is None or field_name not in defaults:
                body.append(statement)
                continue
            source = defaults[field_name]
            factory |= source.startswith("field(")
            assert isinstance(statement, cst.SimpleStatementLine)
            annotated = statement.body[0]
            body.append(
                statement.with_changes(
                    body=[annotated.with_changes(value=cst.parse_expression(source))]
                )
            )
        updated = node.with_changes(body=node.body.with_changes(body=body))
        self.needs_dataclass |= factory
        if not factory or _is_dataclass(node):
            return updated
        # Only a dataclass field can carry a factory, so declare it as one.
        return updated.with_changes(
            bases=[],
            lpar=cst.MaybeSentinel.DEFAULT,
            rpar=cst.MaybeSentinel.DEFAULT,
            decorators=[cst.Decorator(decorator=cst.Name("dataclass"))],
        )

    def _account_for(self, names) -> None:
        """Record every name whose import is about to disappear."""
        for alias in names:
            name = str(alias.name.value)
            if name == "mark_safe":
                self.needs_markup = True
            if name == "merge_attributes":
                self.needs_merge_attrs = True
            if name not in DISCARDED and name not in RENAMED and name not in CONFIG_BASES:
                self.dropped.add(name)

    def _keep(self, name: str) -> None:
        self.dropped.discard(name)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        if node.name.value == "on_render_after":
            self._in_render_hook = True

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
        replacement = RENAMED.get(updated_node.value)
        return cst.Name(replacement) if replacement else updated_node

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        func = updated_node.func
        if not (isinstance(func, cst.Attribute) and func.attr.value == "render"):
            return updated_node
        args = {a.keyword.value: a.value for a in updated_node.args if a.keyword}
        if "kwargs" not in args:
            return updated_node
        # django-components renders through a classmethod; citry renders an
        # instance, and str() serialises it
        call_args = [cst.Arg(value=args["kwargs"], star="**")]
        if "slots" in args:
            call_args.append(cst.Arg(keyword=cst.Name("slots"), value=args["slots"]))
        element = cst.Call(func=func.value, args=call_args)
        # on_render() accepts a composed element, and a library component has no
        # engine to serialise against on its own
        if self._in_render_hook:
            return element
        return cst.Call(func=cst.Name("str"), args=[cst.Arg(value=element)])

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        name = updated_node.name.value
        if name in PARADIGM:
            where = self._cls[-1] if self._cls else "?"
            raise Unmigratable("PY-PARADIGM", f"{where} defines {name}()", PARADIGM[name])
        if name == "on_render_after":
            self._in_render_hook = False
            self.needs_context_stash = True
            return _to_on_render(updated_node)
        if name not in RENAMES:
            return updated_node
        return self._data_method(updated_node, RENAMES[name])

    def _data_method(self, fn, new_name):
        body = _code(fn.body)
        for banned in ("args", "context"):
            # word-boundary: `kwargs.attrs` must not look like a read of `args`
            if re.search(rf"(?<![\w.]){banned}\s*[\[.]", body):
                raise Unmigratable(
                    "PY-TD-SIG",
                    f"{fn.name.value} reads `{banned}`, which citry does not pass",
                    "citry's data methods receive only (kwargs, slots).",
                )
        params = [p for p in fn.params.params if p.name.value not in {"args", "context"}]
        if params:  # drop the trailing comma libcst keeps on the removed tail
            params = params[:-1] + [params[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)]
        return fn.with_changes(
            name=cst.Name(new_name),
            params=fn.params.with_changes(params=params),
        )


def _to_on_render(fn: cst.FunctionDef) -> cst.FunctionDef:
    """`on_render_after(self, context, template, content)` -> citry's generator.

    citry yields once and hands back what the template rendered, so `content`
    is the yielded result rendered to text. The template context is whatever
    template_data() returned, stashed there under `_render_context`.
    """
    reads = _names_read(fn.body)
    prologue = [cst.parse_statement("result, error = yield\n")]
    # Only bind what the body goes on to use; the rest would be dead.
    if "content" in reads:
        # django-components hands `content` to the hook as a string; citry
        # yields a CitryRender, which has none of str's methods.
        prologue.append(cst.parse_statement("content = str(result)\n"))
    if "context" in reads:
        prologue.append(cst.parse_statement("context = self._render_context\n"))
    body = fn.body.with_changes(body=[*prologue, *fn.body.body])
    return fn.with_changes(
        name=cst.Name("on_render"),
        params=cst.Parameters(params=[cst.Param(name=cst.Name("self"))]),
        body=body,
    )


class _Names(cst.CSTVisitor):
    def __init__(self) -> None:
        self.found: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.found.add(node.value)


def _names_read(node) -> set[str]:
    """Names the body mentions, so nothing is bound that is never looked at."""
    visitor = _Names()
    node.visit(visitor)
    return visitor.found


def _dotted(node) -> str:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{_dotted(node.value)}.{node.attr.value}"
    return ""


def _code(node) -> str:
    return cst.Module(body=[]).code_for_node(node)


TEMPLATE_RE = re.compile(
    r'(?P<indent>[ \t]*)template(?:\s*:\s*types\.django_html)?\s*=\s*"""(?P<body>.*?)"""',
    re.DOTALL,
)


def rewrite_templates(source: str, classes: dict, mode: str = "pure"):
    """Translate every inline ``template`` block; return (source, per-class info)."""
    info: dict[str, object] = {}
    shapes = dict_sequences(source)
    order = list(classes)
    idx = [0]

    def one(m):
        i = idx[0]
        idx[0] += 1
        cls = order[i] if i < len(order) else f"<class#{i}>"
        res = translate(m.group("body"), mode=mode, dict_sequences=frozenset(shapes.get(cls, ())))
        parse_template(res.template)  # citry's parser is the authority
        info[cls] = res
        ind = m.group("indent")
        notes = "".join(marker.at(cls).as_comment(ind) + "\n" for marker in res.markers)
        body = "\n".join(
            (ind + "    " + ln.strip()) if ln.strip() else ""
            for ln in res.template.strip().splitlines()
        )
        # a backslash in the template must survive the Python literal
        prefix = "r" if "\\" in body else ""
        return f'{notes}{ind}template = {prefix}"""\n{body}\n{ind}"""'

    return TEMPLATE_RE.sub(one, source), info


def class_spans(source, base=""):
    """Yield (name, start, end) per top-level class, decorators included."""
    lines = source.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    pattern = re.compile("class (\\w+)\\(" + base)
    heads = []
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        j = i
        while j and lines[j - 1].lstrip().startswith("@"):
            j -= 1
        heads.append((m.group(1), offsets[j]))
    for i, (name, at) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(source)
        yield name, at, end


_STR = re.compile(r"""(f?)("([^"\\\\]|\\\\.)*"|'([^'\\\\]|\\\\.)*')""")
_FSTRING_SLOT = re.compile(r"\{([^{}]+)\}")


def _rebind(text: str, keys: set, quote: str = '"') -> str:
    return re.sub(
        r"\b([A-Za-z_]\w*)\b",
        lambda k: f"data[{quote}{k.group(1)}{quote}]" if k.group(1) in keys else k.group(1),
        text,
    )


def _bind(expr: str, keys: set) -> str:
    """Rewrite bare template names in ``expr`` to ``data[...]`` lookups.

    String literals are masked first, so a dict key like ``{"href": href}``
    keeps its key while the value is rebound. An f-string is masked too, but
    the expressions inside its braces are names and must be rebound.
    """
    lits: list[str] = []

    def stash(m):
        prefix, body = m.group(1), m.group(2)
        if prefix:
            # Reusing the f-string's own quote inside a placeholder is only
            # valid from Python 3.12, so the lookup takes the other one.
            inner = "'" if body[:1] == '"' else '"'
            body = _FSTRING_SLOT.sub(lambda s: "{" + _rebind(s.group(1), keys, inner) + "}", body)
        lits.append(prefix + body)
        return f"\x00S{len(lits) - 1}\x00"

    masked = _rebind(_STR.sub(stash, expr), keys)
    return re.sub(r"\x00S(\d+)\x00", lambda m: lits[int(m.group(1))], masked)


class _Hoist(cst.CSTTransformer):
    """``return {...}`` -> build the dict, apply merge_attrs, return it."""

    def __init__(self, info: dict):
        self.info = info
        self.frames: list[list[Any]] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        res = self.info.get(node.name.value)
        stash = any(
            isinstance(s, cst.FunctionDef) and s.name.value == "on_render" for s in node.body.body
        )
        self.frames.append([getattr(res, "hoists", None) or [], stash, False])
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> Any:
        self.frames.pop()
        return updated_node

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> Any:
        if not self.frames:
            return updated_node
        hoisted, stash, done = self.frames[-1]
        if done or not (hoisted or stash) or len(updated_node.body) != 1:
            return updated_node
        ret = updated_node.body[0]
        if not isinstance(ret, cst.Return) or not isinstance(ret.value, cst.Dict):
            return updated_node
        self.frames[-1][2] = True

        keys = {
            e.key.evaluated_value
            for e in ret.value.elements
            if isinstance(e, cst.DictElement) and isinstance(e.key, cst.SimpleString)
        }
        lines = [
            cst.SimpleStatementLine(
                [cst.Assign([cst.AssignTarget(cst.Name("data"))], value=ret.value)],
                leading_lines=updated_node.leading_lines,
            )
        ]
        for var, expr in hoisted:
            lines.append(cst.parse_statement(f'data["{var}"] = {_bind(expr, keys)}'))
        if stash:
            # on_render() has no context parameter, so keep what the template got
            lines.append(cst.parse_statement("self._render_context = data"))
        lines.append(cst.parse_statement("return data"))
        return cst.FlattenSentinel(lines)


def inject_hoisted(source: str, info: dict) -> str:
    return cst.parse_module(source).visit(_Hoist(info)).code


def migrate_source(
    source: str,
    base: str = "Component",
    mode: str = "pure",
    renames: dict[str, str] | None = None,
    known: frozenset[str] = frozenset(),
    app: str = "",
):
    classes = dict.fromkeys(n for n, _, _ in class_spans(source, "Component"))
    source, info = rewrite_templates(source, classes, mode=mode)
    wanted = {(module, name) for r in info.values() for module, name in r.imports}

    tree = cst.parse_module(source)
    tf = ComponentTransformer(base=base, renames=renames, app=app)
    tf.needs_merge_attrs = ("citry", "merge_attrs") in wanted
    tf.needs_markup = ("citry", "Markup") in wanted or "mark_safe" in source
    tf.defaults = declared_defaults(source)
    out = tree.visit(tf).code
    if tf.needs_dataclass:
        out = "from dataclasses import dataclass, field\n" + out
    out = inject_hoisted(out, info)
    # A filter the project defines is an ordinary function the component calls.
    extra = sorted(f"from {m} import {n}" for m, n in wanted if m != "citry")
    if app and base == "Component":
        module, _, name = app.rpartition(".")
        if module:
            extra.append(f"from {module} import {name}")
    if extra:
        out = "\n".join(extra) + "\n" + out
    _refuse_undefined(out, tf.dropped, known)
    return out, info


def split_classes(source: str):
    """Each component as a standalone snippet, so one bad class does not block
    the rest of the module."""
    spans = list(class_spans(source, "Component"))
    head = source[: spans[0][1]] if spans else source
    for name, at, end in spans:
        yield name, head + source[at:end]


def _markers(info: dict) -> list[Marker]:
    return [m.at(cls) for cls, res in info.items() for m in getattr(res, "markers", ())]


def migrate_module(
    source: str,
    base: str = "Component",
    mode: str = "pure",
    renames: dict[str, str] | None = None,
    app: str = "",
):
    """Whole module at once; per-class on refusal, collecting a Marker each."""
    try:
        out, info = migrate_source(source, base=base, mode=mode, renames=renames, app=app)
    except Unmigratable:
        pass
    else:
        return out, _markers(info), len(list(split_classes(source)))

    known = module_names(source)
    pieces, markers, ok = [], [], 0
    for i, (name, block) in enumerate(split_classes(source)):
        try:
            got, info = migrate_source(
                block, base=base, mode=mode, renames=renames, known=known, app=app
            )
        except Unmigratable as e:
            markers.append(e.marker.at(name))
        else:
            pieces.append(got if i == 0 else got[got.index(f"class {name}(") :])
            markers.extend(_markers(info))
            ok += 1
    return "\n\n".join(pieces), markers, ok


def scan_module(source: str, mode: str = "pure") -> list[dict]:
    """Classify every component by running the real migration on it, so scan
    cannot drift from what migrate does."""
    rows = []
    known = module_names(source)
    for name, block in split_classes(source):
        row = {"component": name, "status": "ok", "rules": [], "detail": ""}
        try:
            _, info = migrate_source(block, mode=mode, known=known)
        except Unmigratable as e:
            row.update(status="unsupported", rules=[e.marker.rule], detail=e.marker.what)
        except Exception as e:  # noqa: BLE001
            # a refusal is Unmigratable; anything else is a bug in the tool and
            # must not be reported as if the component were merely unsupported
            row.update(status="error", rules=["INTERNAL"], detail=f"{type(e).__name__}: {e}")
        else:
            res = info.get(name)
            if res is not None and res.markers:
                first = res.markers[0]
                row.update(status="marked", rules=[first.rule], detail=first.what)
            if res is not None:
                row |= {
                    "slots": sorted(res.slots),
                    "renders": sorted(res.components),
                    "hoisted": len(res.hoists),
                }
        rows.append(row)
    return rows


def _annotated_name(statement) -> str | None:
    if not isinstance(statement, cst.SimpleStatementLine) or len(statement.body) != 1:
        return None
    entry = statement.body[0]
    if isinstance(entry, cst.AnnAssign) and isinstance(entry.target, cst.Name):
        return entry.target.value
    return None


def _is_dataclass(node: cst.ClassDef) -> bool:
    return any(
        isinstance(d.decorator, cst.Name) and d.decorator.value == "dataclass"
        for d in node.decorators
    )


def module_names(source: str) -> frozenset[str]:
    """Everything the module defines at the top level."""
    return frozenset(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    )


def _refuse_undefined(source: str, dropped: set[str], known: frozenset[str]) -> None:
    """Never emit a module that uses a name nothing defines.

    A migration that fails at import time is the worst kind, and there are two
    ways to get there: dropping the import of a name citry has no counterpart
    for, and introducing a name without its import. One check covers both.
    """
    tree = ast.parse(source)
    bound: set[str] = set(dir(builtins)) | set(known)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (used if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.Attribute | ast.Subscript):
            continue
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            bound.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            bound |= {a.asname or a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            bound |= set(node.names)

    missing = sorted(used - bound)
    if not missing:
        return
    from_djc = sorted(set(missing) & dropped)
    if from_djc:
        raise Unmigratable(
            "PY-NO-EQUIVALENT",
            f"uses {', '.join(from_djc)} from django-components",
            "citry has no counterpart, so the import cannot be carried over. "
            "Replace the construct or keep the component on django-components.",
        )
    raise Unmigratable(
        "PY-UNDEFINED",
        f"the translation left {', '.join(missing)} undefined",
        "This is a bug in djc-to-citry: it emitted a name without its import.",
    )


def dict_sequences(source: str) -> dict[str, set[str]]:
    """Per component, the data keys that hold a sequence of dicts.

    Django resolves `item.key` against a dict first and an attribute second;
    citry evaluates Python, where the two are different. Which one a template
    meant is not guessable, but the component declares it: a `Kwargs` field
    annotated `list[dict]` reaching the template says the loop items are dicts.
    """
    found: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        fields = {
            item.target.id: ast.unparse(item.annotation)
            for inner in node.body
            if isinstance(inner, ast.ClassDef) and inner.name == "Kwargs"
            for item in inner.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        keys: set[str] = set()
        for inner in node.body:
            if not isinstance(inner, ast.FunctionDef):
                continue
            for statement in ast.walk(inner):
                if not isinstance(statement, ast.Return) or not isinstance(
                    statement.value, ast.Dict
                ):
                    continue
                for key, value in zip(statement.value.keys, statement.value.values):
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        continue
                    annotation = _annotation_of(value, fields)
                    if annotation.replace(" ", "").startswith("list[dict"):
                        keys.add(key.value)
        if keys:
            found[node.name] = keys
    return found


def _annotation_of(value, fields: dict[str, str]) -> str:
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "kwargs"
    ):
        return fields.get(value.attr, "")
    if isinstance(value, ast.Name):
        return fields.get(value.id, "")
    return ""


def declared_defaults(source: str) -> dict[str, dict[str, str]]:
    """Per component, the default each `class Defaults` entry declares.

    django-components keeps defaults in their own class and wraps the lazy ones
    in `Default(...)`. citry reads defaults off the `Kwargs` field itself, and a
    dataclass field factory has the same render-time semantics, so the two
    forms map directly.

    The expression is taken as the author wrote it. Reconstructing it from an
    AST would reformat it, and `ast.unparse` does not even agree with itself
    across Python versions.
    """
    module = cst.parse_module(source)
    found: dict[str, dict[str, str]] = {}
    for node in module.body:
        if not isinstance(node, cst.ClassDef):
            continue
        for inner in node.body.body:
            if not isinstance(inner, cst.ClassDef) or inner.name.value != "Defaults":
                continue
            values = {
                name: _default_source(value, module)
                for statement in inner.body.body
                for name, value in _assignments(statement)
            }
            if values:
                found[node.name.value] = values
    return found


def _assignments(statement):
    """`x = expr` or `x: T = expr`, as (name, value)."""
    if not isinstance(statement, cst.SimpleStatementLine):
        return
    for entry in statement.body:
        if isinstance(entry, cst.AnnAssign) and isinstance(entry.target, cst.Name):
            if entry.value is not None:
                yield entry.target.value, entry.value
        elif isinstance(entry, cst.Assign) and len(entry.targets) == 1:
            target = entry.targets[0].target
            if isinstance(target, cst.Name):
                yield target.value, entry.value


def _default_source(value, module: cst.Module) -> str:
    """`Default(f)` defers to render time; a dataclass factory does the same."""
    code = module.code_for_node(value).strip()
    if (
        isinstance(value, cst.Call)
        and isinstance(value.func, cst.Name)
        and value.func.value == "Default"
        and len(value.args) == 1
    ):
        return f"field(default_factory={module.code_for_node(value.args[0].value).strip()})"
    return code
