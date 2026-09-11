# djc-to-citry

Migrate a [django-components](https://github.com/django-components/django-components) library to [citry](https://citry.dev).

The tool translates components and the way they are used. It does not scaffold a package, port tests, or rearrange your project.

> Every line of this was written by an AI, working from the django-components
> and citry references. It was built to move two real libraries and is tested
> against them - [django-components-bootstrap](https://github.com/joeyjurjens/django-components-bootstrap)
> (119 components) became [citry-bootstrap](https://github.com/joeyjurjens/citry-bootstrap),
> and [djc-phosphor-icons](https://github.com/joeyjurjens/djc-phosphor-icons) became
> [citry-phosphor-icons](https://github.com/joeyjurjens/citry-phosphor-icons).
> Those two are the whole of its field experience, so expect a library that uses
> django-components differently to hit something neither of them does. It
> refuses rather than guesses when it can tell, but it cannot tell everything.

## How it works

Neither framework's parser is enough on its own, so the tool uses both.

```
django-components' lexer   ──▶  byte ranges Django owns
                                        │
citry's parser  ◀───────────────────────┘   (as foreign spans)
        │                                    gives the HTML structure
        ▼
Django's Parser  ──▶  what each tag means, with source positions
        │
        └──▶  joined on position  ──▶  one emitter per node class  ──▶  citry
                                                                          │
                                              citry's parser validates ◀───┘
```

Citry's parser is asked to read a *Django* template, with the Django tags declared foreign. That is how the tool knows a `{% if %}` sits inside a start tag rather than between elements, or that a `{% slot %}` is inside a `<textarea>` where citry parses nothing. Those were the cases that used to need guessing.

Dispatch is on the node class Django itself produced, so a construct with no handler cannot pass silently - it becomes a marker.

## Modes

| | `--mode pure` | `--mode compat` |
|---|---|---|
| Output runs on | citry alone | Django only |
| Requires | citry | citry + [citry-django](https://github.com/joeyjurjens/citry-django) |
| Django tags and filters | translated, or refused | kept where citry has no equivalent |
| For | a distributable component library | migrating an existing Django app |

`compat` translates to citry first and keeps the original only where citry's parser rejects the result.

## Use

```bash
djc-to-citry scan     components.py --mode pure
djc-to-citry migrate  components.py --mode pure --out migrated.py
djc-to-citry migrate  components.py --rename old_pkg.components=new_pkg
djc-to-citry migrate  components.py --base mylib.component.Base --unwrap none
djc-to-citry extension --out yourapp/plain_inputs.py
djc-to-citry residue  .djc-to-citry/residue.json --full
djc-to-citry template templates/ --write
```

`--base` names the class the components inherit from: `Component`, `LibraryComponent`, or a dotted path to your own.

Citry hands component code its constants wrapped in a transparent proxy. It passes `isinstance()`, but `x is True` and `x is None` are silently False and `re`, `str.join`, `os.fspath` and a validating `Kwargs` reject it. django-components has no such marker, so migrated code is written as if there were none. `--unwrap` says where that is dealt with:

| | |
|---|---|
| `inline` (default) | each module gets a small `_plain()` that unwraps the inputs |
| `none` | something you install yourself deals with it: the `PlainInputs` extension, or a base class of your own |
| `mark` | the same, and the tool marks the code that depends on whatever you installed |

`djc-to-citry extension` writes that extension into your project; install it with `Citry(extensions=[PlainInputs])`. It unwraps the inputs before citry builds the typed `Kwargs` and marks the pass-through values again before citry looks for its constants, so the engine optimizes exactly what it would have without it. It is a stopgap for [citry#107](https://github.com/citry-dev/citry/issues/107), which will hand component code plain values from the engine.

Detection is honest about its reach: it finds an identity test and a call into an API that rejects the proxy, both decidable from the code. A call into a helper that does either of those inside is not.

`template` translates django-components syntax wherever it appears - a template file, or the template strings inside a Python module - without touching the code around it.

Anything the tool will not guess at becomes a marker beside the code rather than silently wrong output:

```python
# MIGRATE [PY-CONFIG] Icon.Cache computes enabled at runtime
#   why: citry reads a nested config class as literal values and rejects
#        anything else, so this setting has to move into the component.
```

A module is never written with a name whose import the tool removed, and `migrate` refuses to write an output that would replace its input with a comment.

## What it translates

| django-components | citry |
|---|---|
| `{% component %}` | `<c-name>`, kebab-case |
| `{% slot %}` / `{% fill %}` | `<c-slot>` / `<c-fill>` |
| `{% provide %}` | `<c-provide>` |
| `{% html_attrs %}` | `merge_attrs()` in `template_data`, bound with one `c-bind` |
| `{% cache %}` | `<c-cache>` |
| `{% component_css_dependencies %}` | `<c-css>` |
| `{% if %}` / `{% for %}` | `c-if` / `c-for`, or a bound dict inside a start tag |
| `{{ x\|filter }}` | Python, or the project's own filter handed to the template |
| `<{{ tag }}>` | `<c-element c-is="tag">` |
| `{% load %}`, `{% comment %}` | dropped |
| `get_template_data` | `template_data` |
| `class Defaults` | defaults on the `Kwargs` fields |
| `Component` | `LibraryComponent` |

## Docs

`DESIGN.md` - why it is built this way, and what it refuses.

## Develop

```bash
uv sync
pytest
ruff check src tests && ruff format --check src tests
ty check src
```
