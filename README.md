# djc-to-citry

Migrate a [django-components](https://github.com/django-components/django-components)
library to [citry](https://citry.dev).

The tool translates components and the way they are used. It does not scaffold a
package, port tests, or rearrange your project.

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

Citry's parser is asked to read a *Django* template, with the Django tags
declared foreign. That is how the tool knows a `{% if %}` sits inside a start
tag rather than between elements, or that a `{% slot %}` is inside a
`<textarea>` where citry parses nothing. Those were the cases that used to need
guessing.

Dispatch is on the node class Django itself produced, so a construct with no
handler cannot pass silently - it becomes a marker.

## Modes

| | `--mode pure` | `--mode compat` |
|---|---|---|
| Output runs on | citry alone | Django only |
| Requires | citry | citry + [citry-django](https://github.com/joeyjurjens/citry-django) |
| Django tags and filters | translated, or refused | kept where citry has no equivalent |
| For | a distributable component library | migrating an existing Django app |

`compat` translates to citry first and keeps the original only where citry's
parser rejects the result.

## Use

```bash
djc-to-citry scan     components.py --mode pure
djc-to-citry migrate  components.py --mode pure --out migrated.py
djc-to-citry migrate  components.py --rename old_pkg.components=new_pkg
djc-to-citry residue  .djc-to-citry/residue.json --full
djc-to-citry template templates/ --write
```

`template` translates django-components syntax wherever it appears - a template
file, or the template strings inside a Python module - without touching the code
around it.

Anything the tool will not guess at becomes a marker beside the code rather than
silently wrong output:

```python
# MIGRATE [PY-CONFIG] Icon.Cache computes enabled at runtime
#   why: citry reads a nested config class as literal values and rejects
#        anything else, so this setting has to move into the component.
```

A module is never written with a name whose import the tool removed, and
`migrate` refuses to write an output that would replace its input with a
comment.

## What it translates

| django-components | citry |
|---|---|
| `{% component %}` | `<c-name>`, kebab-case |
| `{% slot %}` / `{% fill %}` | `<c-slot>` / `<c-fill>` |
| `{% provide %}` | `<c-provide>` |
| `{% html_attrs %}` | one `c-bind` per element, merged in `template_data` |
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
