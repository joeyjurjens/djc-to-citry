"""Give component code ordinary values without costing citry its constants.

Copy this into the project being migrated, or write it out with
`djc-to-citry extension --out yourapp/plain_inputs.py`, and install it:

    app = Citry(extensions=[PlainInputs])

Citry marks a value it knows at parse time with a transparent proxy so it can
precompute the template expressions that read it. Component code is ordinary
Python, where the proxy shows: `x is True` is False, and `re`, `str.join`,
`os.fspath` and a validating `Kwargs` reject it. django-components had no such
marker, so migrated code is written as if there were none.

Delete this once citry hands component code plain values itself, which
https://github.com/citry-dev/citry/issues/107 tracks.
"""

from citry import Const, Extension, const_value, is_const

_MARKED = "_plain_inputs_marked"


class PlainInputs(Extension):
    """Unwrap on the way in, mark again on the way out.

    The inputs are unwrapped before citry builds the typed `Kwargs`, so every
    callback, `self.kwargs` and a validating model see real values. A template
    variable that came straight back out of `template_data` is marked again
    before citry looks for its constants, so the engine optimizes exactly what
    it would have optimized without this extension.

    Two differences from citry's own behavior remain. `is_const()` on a
    component's inputs is always False; read `raw_kwargs`, which citry's docs
    already point at for this, to see what was marked. And a marked value that
    `template_data` puts inside a list or dict it builds loses its marker,
    where citry would have carried it into the child component.
    """

    name = "plain_inputs"

    def on_component_input(self, ctx) -> None:
        marked = {}
        for key, value in ctx.kwargs.items():
            if is_const(value):
                ctx.kwargs[key] = plain = const_value(value)
                marked[id(plain)] = plain
        if marked:
            # Held by value as well as by id, so the id cannot be reused by a
            # later object while this render is still running.
            setattr(ctx.component, _MARKED, marked)

    def on_component_data(self, ctx) -> None:
        marked = getattr(ctx.component, _MARKED, None)
        if not marked:
            return
        for name, value in ctx.template_data.items():
            if id(value) in marked:
                ctx.template_data[name] = Const(value)
