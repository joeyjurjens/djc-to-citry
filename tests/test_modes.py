import pytest

from djc_to_citry.engines import ensure_django

ensure_django()

from djc_to_citry.emit import translate

STRADDLE = "{% load component_tags %}{% if b %}<div>{% endif %}x{% if b %}</div>{% endif %}"
FILTERED = "{% load component_tags %}<p>{{ name|upper }}</p>"
BALANCED = (
    '{% load component_tags %}<div>{% if b %}<span>x</span>{% endif %}{% slot "default" / %}</div>'
)


def test_pure_refuses_what_citry_cannot_express():
    assert [m.rule for m in translate(STRADDLE, mode="pure").markers] == ["T-STRADDLE"]


def test_compat_keeps_django_syntax():
    out = translate(STRADDLE, mode="compat")
    assert out.kept_django
    assert "{% if" in out.template


def test_a_filter_is_no_longer_a_refusal():
    out = translate(FILTERED, mode="pure")
    assert out.markers == []
    assert "{{ name.upper() }}" in out.template


def test_compat_prefers_citry_when_it_can():
    pure = translate(BALANCED, mode="pure")
    compat = translate(BALANCED, mode="compat")
    assert compat.template == pure.template
    assert not compat.kept_django
    assert "<c-if" in compat.template and "{% if" not in compat.template


def test_star_import_is_refused():
    from djc_to_citry.codemod import Unmigratable, migrate_source

    source = "from django_components import *\n\n\nclass A(Component):\n    template = ''\n"
    with pytest.raises(Unmigratable):
        migrate_source(source)


def test_dict_sequences_come_from_the_kwargs_annotation():
    from djc_to_citry.codemod import dict_sequences

    source = (
        "class A(Component):\n"
        "    class Kwargs:\n"
        "        items: list[dict]\n"
        "        name: str\n"
        "    def get_template_data(self, args, kwargs, slots, context):\n"
        "        return {'items': kwargs.items, 'name': kwargs.name}\n"
    )
    assert dict_sequences(source) == {"A": {"items"}}


def migrate(body: str) -> str:
    from djc_to_citry.codemod import migrate_source

    return migrate_source(body)[0]


COMPONENT = (
    "from django_components import Component\n"
    "class A(Component):\n"
    "    def get_template_data(self, args, kwargs, slots, context):\n"
    "        return {}\n"
    "    template = ''\n"
)


def test_an_ordinary_django_import_is_kept():
    out = migrate("from django.utils.text import slugify\n" + COMPONENT)
    assert "from django.utils.text import slugify" in out


def test_a_replaced_django_import_is_translated():
    out = migrate(
        COMPONENT.replace(
            "import Component\n", "import Component, merge_attributes\nx = merge_attributes\n"
        )
    )
    assert "merge_attrs" in out
    assert "merge_attributes" not in out


def test_a_config_base_is_dropped():
    source = (
        "from django_components import Component\n"
        "from django_components.extensions.cache import ComponentCache\n"
        "class A(Component):\n"
        "    class Cache(ComponentCache):\n"
        "        enabled = True\n"
        "    def get_template_data(self, args, kwargs, slots, context):\n"
        "        return {}\n"
        "    template = ''\n"
    )
    assert "class Cache:" in migrate(source)


def test_an_unmapped_name_is_refused_not_silently_dropped():
    from djc_to_citry.codemod import Unmigratable

    source = COMPONENT.replace("import Component\n", "import Component, register\n@register('a')\n")
    with pytest.raises(Unmigratable, match="register"):
        migrate(source)


DEFAULTS = (
    "from django_components import Component, Default\n"
    "from myapp.conf import setting\n"
    "class A(Component):\n"
    "    class Kwargs:\n"
    "        x: str\n"
    "        y: int\n"
    "    class Defaults:\n"
    "        x = Default(lambda: setting())\n"
    "        y = 3\n"
    "    def get_template_data(self, args, kwargs, slots, context):\n"
    "        return {}\n"
    "    template = ''\n"
)


def test_defaults_move_onto_the_kwargs_fields():
    out = migrate(DEFAULTS)
    assert "x: str = field(default_factory=lambda: setting())" in out
    assert "y: int = 3" in out
    assert "class Defaults" not in out


def test_a_lazy_default_makes_kwargs_a_dataclass():
    out = migrate(DEFAULTS)
    assert "from dataclasses import dataclass, field" in out
    assert "@dataclass\n    class Kwargs:" in out


def test_a_computed_config_class_is_refused():
    from djc_to_citry.codemod import Unmigratable

    source = (
        "from django_components import Component\n"
        "from django_components.extensions.cache import ComponentCache\n"
        "class A(Component):\n"
        "    class Cache(ComponentCache):\n"
        "        @property\n"
        "        def enabled(self):\n"
        "            return True\n"
        "    def get_template_data(self, args, kwargs, slots, context):\n"
        "        return {}\n"
        "    template = ''\n"
    )
    with pytest.raises(Unmigratable, match="enabled"):
        migrate(source)


def test_an_fstring_lookup_does_not_reuse_the_outer_quote():
    """Reusing it is only valid from Python 3.12; the output must run on 3.10."""
    import ast

    source = COMPONENT.replace(
        "        return {}\n",
        '        return {"cid": kwargs.cid, "target": f"#{kwargs.cid}"}\n',
    ).replace("    template = ''\n", "    template = '<a c-href=\"target\">x</a>'\n")
    out = migrate(source)
    assert "f\"#{data['" in out or 'f"#{kwargs.cid}"' in out
    ast.parse(out)


def test_on_render_binds_only_what_the_body_uses():
    source = (
        "from django_components import Component\n"
        "class A(Component):\n"
        "    def get_template_data(self, args, kwargs, slots, context):\n"
        "        return {'x': 1}\n"
        "    def on_render_after(self, context, template, content):\n"
        "        return context['x']\n"
        "    template = ''\n"
    )
    out = migrate(source)
    assert "context = self._render_context" in out
    assert "content = result" not in out


def test_a_data_method_unwraps_its_inputs_first():
    """Citry's constant proxy breaks `re` and `is True`; unwrap at the boundary."""
    import ast

    out = migrate(COMPONENT)
    assert "def _plain(kwargs):" in out
    assert "from citry import const_value" in out
    fn = next(
        node
        for node in ast.walk(ast.parse(out))
        if isinstance(node, ast.FunctionDef) and node.name == "template_data"
    )
    assert ast.unparse(fn.body[0]) == "kwargs = _plain(kwargs)"


def test_the_helper_is_emitted_once_for_a_module_of_components():
    source = COMPONENT + COMPONENT.replace("class A(", "class B(").replace(
        "from django_components import Component\n", ""
    )
    assert migrate(source).count("def _plain(kwargs):") == 1
