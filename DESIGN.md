# djc-to-citry - design

Why the tool is built this way, and what it refuses to do.

## The problem with translating a template

A django-components template is HTML with Django tags as islands in it. To translate one you need two things at once:

- **what a tag means** - `{% slot "x" required %}` is a required slot
- **where it sits** - inside a start tag, between elements, or inside a
`<textarea>` where citry parses nothing

Django's parser gives the first and treats the HTML as flat text. Citry's parser gives the second and does not know Django's syntax. An earlier version of this tool had its own parser and guessed at the second half with regular expressions; every hard case - a conditional attribute, a tag inside raw text, a computed element name - was a separate heuristic that could be silently wrong.

## Using both parsers

Citry accepts *foreign source spans*: byte ranges whose syntax belongs to another template language. Its own grammar never sees them. That is how citry-django embeds Django in a citry template, and it is exactly what the translation needs in reverse.

1. django-components' lexer reports every `{% %}` and `{{ }}` with its position.
It is string-aware where Django's own lexer is not, so `{% component "}" %}` lexes correctly.
2. Those ranges go to `citry_core.template_parser.parse_template` as foreign
spans. Citry parses the surrounding HTML and hands back a tree with each Django tag located inside it.
3. Django's `Parser.parse()` gives the meaning of the same bytes. Every node
carries `token.position`.
4. The two are joined on position (`locate.py`). The result: a Django node plus
the region it occupies.

Running that over a real library shows the shape of the problem:

```
136  SlotNode in body             19  IfNode in start_tag   <- conditional attributes
124  HtmlAttrsNode in start_tag   19  IfNode in body
 50  VariableNode in dynamic      16  ProvideNode in body
 37  VariableNode in attr          2  VariableNode in raw   <- citry parses nothing here
 31  ComponentNode in body         2  ForNode in attr
 25  HtmlAttrsNode in dynamic      1  SlotNode in raw
```

Each of those was a heuristic before. Now they are rows in a table that falls out of the parsers.

### One thing citry cannot parse

A computed element name, `<{{ tag }}>`, is not HTML, so citry rejects the whole template - 24 of 119 components in the test library. Before parsing, the tool replaces that expression with letters of the same length. Every other position stays valid, and the masked ranges come back as their own region kind, which the emitter turns into `<c-element c-is="tag">`.

## Dispatch on the node class

`emit.py` maps Django node classes to emitters. There is no registry of tag names to keep in step with django-components, because Django already produced a typed node: `ComponentNode`, `SlotNode`, `HtmlAttrsNode`, `DjcCacheNode`.

A class with no entry becomes a marker. **Missing a construct is structurally impossible rather than something to remember.** `tests/test_emit.py` closes the loop from the other side: it reads the dispatch table and fails if a handled class has no example.

## Expressions are already parsed

django-components reports an argument's `kind` (string, variable, int, template_string), its `filters`, and whether it is a spread. Django reports a `FilterExpression`'s variable and filter chain, and an `{% if %}`'s condition as an operator tree. None of that needs re-parsing, so `expr.py` never inspects source text:

- a quoted literal becomes a static attribute; anything else becomes `c-`
- `{{ x|upper }}` becomes `{{ x.upper() }}`
- `{% if a.b|length > 2 and not c %}` becomes `((len(a.b) > 2) and (not c))`
- `"#{{ id }}"` becomes an f-string, because citry's sandbox has no `str`

## What the component must provide

Citry evaluates Python in the template but puts only the component's data in scope - no builtins. Anything else is hoisted into `template_data`:

```python
data["svg_attrs"] = merge_attrs((data["attrs"] or {}), (data["default_attrs"] or {}))
data["len"] = len
```

Hoisted names are derived from the element they belong to (`button_attrs`, `h2_attrs`) rather than numbered, and everything contributing attributes to the same start tag is merged into one `merge_attrs` call. Citry does allow several `c-bind` attributes, but it merges `class` and `style` across them only on HTML elements; component inputs are last-one-wins. Merging in Python keeps `{% html_attrs %}` behaving the same on both.

## Where the tool refuses

A refusal is a marker carrying the rule, what it saw, and why. It is never a silent pass-through.

| rule | when |
|---|---|
| `T-RAWTEXT` | a construct inside `<textarea>`/`<script>` that cannot become one expression |
| `T-STRADDLE` | a branch that opens an element it does not close; citry's parser rejects it |
| `T-UNKNOWN` | a tag with no handler |
| `PY-ARGS` | `class Args`; citry has no positional inputs, so this changes the public API |
| `PY-PARADIGM` | `on_render_before` / `on_render`; citry has one generator, merging them is a redesign |
| `PY-TD-SIG` | a data method reads `args` or `context`, which citry does not pass |
| `PY-CONFIG` | a nested config class computes a value; citry reads literals |
| `PY-NO-EQUIVALENT` | a django-components name with no citry counterpart is still used |
| `PY-IMPORT` | `from django_components import *` hides which names came from where |
| `PY-UNDEFINED` | the translation left a name undefined - a bug in this tool, reported as one |

The last one is the safety net. Every name whose import the tool removes is recorded and checked against the output, so a module is never written that would fail at import. It caught two real bugs: a dropped `ComponentCache` base and a `dataclass` import the tool itself introduced.

## Semantics that do not carry over

Some Django behaviour has no single Python equivalent. The tool does not guess; it reads what the source declares.

**Dotted lookup.** Django resolves `item.active` as a dict key first and an
attribute second. Citry evaluates Python, where those differ. Rather than picking one, the tool reads the component's own annotation: a `Kwargs` field declared `list[dict]` that reaches the template means the loop items are dicts, and only then is the lookup written as a subscript.

**Implicit calls.** Django calls a callable it finds during lookup. Where the
expression is hoisted into `template_data`, that runs in ordinary Python and the test can be written out: `(x() if callable(x) else x)`.

**The loop variable.** Citry has none. A body that reads `forloop` gets its
index from an enumeration the component prepares.

## Modules

```
locate.py    the join: Django's AST against citry's HTML structure
expr.py      Django expressions to Python
emit.py      one emitter per node class; the replacements
codemod.py   the Python side, with libcst
engines.py   starting the two engines
cli.py       scan / migrate / residue / template
```

Emission is text. Citry's AST is source-anchored - every node carries a token with byte offsets - and there is no unparser, so building a citry AST is not a path. The output does not have to be pretty: `citry_core.template_formatter` formats it and `parse_template` says whether it is valid.

## Verification

The tool's own tests cover each construct and each refusal. Beyond that it is checked against two real libraries it knows nothing about:

- **django-components-bootstrap** - 119 components, 36 modules. Every one
translates, every output parses as citry, and the ported test suite runs.
- **djc-phosphor-icons** - a single component using `NamedTuple` kwargs,
`class Defaults`, a cache extension and `|safe`. It found four bugs that Bootstrap never exercised.

Neither library is referenced anywhere in the source. Bootstrap is a fixture, not a specification.
