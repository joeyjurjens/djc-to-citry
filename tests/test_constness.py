"""What the proxy breaks, that the tool can see, and that the extension fixes."""

import pytest
from citry import Citry, Component, is_const

from djc_to_citry.constness import advise
from djc_to_citry.extension import PlainInputs


@pytest.mark.parametrize(
    "code",
    [
        "kwargs.fluid is True",
        "kwargs.cols is not None",
        "re.sub('-', '', kwargs.name)",
        "json.dumps(kwargs.data)",
        "getattr(obj, kwargs.field)",
        "os.path.join(root, kwargs.name)",
    ],
)
def test_what_the_proxy_breaks_is_found(code):
    source = (
        f"class A(Component):\n    def template_data(self, kwargs, slots):\n        return {code}\n"
    )
    assert [m.component for m in advise(source)] == ["A"]


@pytest.mark.parametrize(
    "code",
    [
        "kwargs.cols == 3",
        "str(kwargs.name).upper()",
        "re.sub('-', '', other)",
        "self.id is None",
    ],
)
def test_what_the_proxy_survives_is_left_alone(code):
    source = (
        f"class A(Component):\n    def template_data(self, kwargs, slots):\n        return {code}\n"
    )
    assert advise(source) == []


def test_a_component_is_named_once_however_often_it_offends():
    source = (
        "class A(Component):\n"
        "    def template_data(self, kwargs, slots):\n"
        "        if kwargs.a is True:\n"
        "            return kwargs.b is None\n"
        "        return None\n"
    )
    markers = advise(source)
    assert [m.component for m in markers] == ["A"]
    assert markers[0].source == "kwargs.a is True"


def engine(**kwargs):
    app = Citry(autodiscover=False, **kwargs)

    class Probe(Component):
        citry = app
        name = "probe"

        class Kwargs:
            flag: object = None

        def template_data(self, kwargs, slots):
            app.seen = kwargs.flag
            return {"flag": kwargs.flag}

        template = "<div>{{ flag }}</div>"

    return app


@pytest.mark.parametrize("value", ["True", "None", "'x'"])
def test_the_extension_hands_component_code_the_real_value(value):
    app = engine(extensions=[PlainInputs])
    app.render_template(f'<c-probe c-flag="{value}" />').serialize()
    assert not is_const(app.seen)
    assert app.seen is eval(value)


def test_without_it_the_component_sees_a_proxy():
    """The bug the extension exists for, so the test above cannot pass by luck."""
    app = engine()
    app.render_template('<c-probe c-flag="True" />').serialize()
    assert is_const(app.seen)
    assert app.seen is not True


def test_citry_still_sees_the_same_constants():
    """Unwrapping must not cost the engine its precompute; that is the point."""
    import citry.component_render as render

    original = render.extract_const_vars
    captured = []

    def spy(variables, used_vars=None):
        const_vars, signature = original(variables, used_vars=used_vars)
        captured.append(sorted(const_vars))
        return const_vars, signature

    render.extract_const_vars = spy
    try:
        seen = []
        for extensions in ([], [PlainInputs]):
            captured.clear()
            engine(extensions=extensions).render_template('<c-probe c-flag="1" />').serialize()
            seen.append(list(captured))
    finally:
        render.extract_const_vars = original
    assert seen[0] == seen[1] == [[], ["flag"]]
