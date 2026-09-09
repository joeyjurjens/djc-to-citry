"""One case per construct django-components can produce.

The last test is the safety net: it asserts these cases exercise every node
class the emitter claims to handle, so adding a handler without a case fails.
"""

import pytest

from djc_to_citry.engines import ensure_django

ensure_django()

from django.template import engines

from djc_to_citry.emit import DROPPED, EMIT, translate

LOAD = "{% load component_tags %}"

CASES = [
    ("variable", "<p>{{ user.name }}</p>", "<p>{{ user.name }}</p>", []),
    ("filter", "<p>{{ name|upper }}</p>", "<p>{{ name.upper() }}</p>", []),
    (
        "variable in an attribute",
        '<a href="/u/{{ id }}/">x</a>',
        "<a c-href='f\"/u/{id}/\"'>x</a>",
        [],
    ),
    (
        "if / elif / else",
        "{% if a %}<p>1</p>{% elif b %}<p>2</p>{% else %}<p>3</p>{% endif %}",
        '<c-if cond="a"><p>1</p></c-if><c-elif cond="b"><p>2</p></c-elif><c-else><p>3</p></c-else>',
        [],
    ),
    (
        "if around an attribute",
        '<div {% if on %}class="a"{% endif %}>x</div>',
        '<div c-bind="div_attrs">x</div>',
        [("div_attrs", "({'class': 'a'} if on else {})")],
    ),
    (
        "for / empty",
        "{% for i in xs %}<li>{{ i }}</li>{% empty %}<li>-</li>{% endfor %}",
        '<c-for each="i in xs"><li>{{ i }}</li></c-for><c-empty><li>-</li></c-empty>',
        [],
    ),
    (
        "forloop counter",
        "{% for i in xs %}<b>{{ forloop.counter }}</b>{% endfor %}",
        '<c-for each="forloop_index, i in loop"><b>{{ (forloop_index + 1) }}</b></c-for>',
        [("loop", "list(enumerate(xs))")],
    ),
    ("slot", LOAD + '<div>{% slot "default" / %}</div>', "<div><c-slot /></div>", []),
    (
        "named slot with fallback",
        LOAD + '{% slot "hdr" required %}fallback{% endslot %}',
        '<c-slot name="hdr" required>fallback</c-slot>',
        [],
    ),
    (
        "component",
        LOAD + '{% component "card" title="T" size=s %}body{% endcomponent %}',
        '<c-card title="T" c-size="s">body</c-card>',
        [],
    ),
    (
        "aggregate keyword",
        LOAD + '{% component "b" attrs:data-x="1" %}{% endcomponent %}',
        "<c-b c-attrs=\"{'data-x': '1'}\"></c-b>",
        [],
    ),
    (
        "fill",
        LOAD + '{% component "c" %}{% fill "hdr" %}h{% endfill %}{% endcomponent %}',
        '<c-c><c-fill name="hdr">h</c-fill></c-c>',
        [],
    ),
    (
        "provide",
        LOAD + '{% provide "k" v=1 %}<p>x</p>{% endprovide %}',
        '<c-provide key="k" c-v="1"><p>x</p></c-provide>',
        [],
    ),
    (
        "html_attrs",
        LOAD + '<p {% html_attrs attrs class="c" defaults:id=i %}>x</p>',
        '<p c-bind="p_attrs">x</p>',
        [("p_attrs", "merge_attrs({'id': i}, (attrs or {}), {'class': 'c'})")],
    ),
    (
        "cache",
        LOAD + '{% cache 60 "k" %}<p>x</p>{% endcache %}',
        '<c-cache ttl="60"><p>x</p></c-cache>',
        [],
    ),
    ("css dependencies", LOAD + "{% component_css_dependencies %}", "<c-css />", []),
    ("js dependencies", LOAD + "{% component_js_dependencies %}", "<c-js />", []),
    ("inline comment", "<p>{# gone #}x</p>", "<p>x</p>", []),
    (
        "block comment",
        "<p>{% comment %}gone{% endcomment %}x</p>",
        "<p>x</p>",
        [],
    ),
    (
        "raw text element",
        "<textarea>{% if v %}{{ v }}{% endif %}</textarea>",
        "<textarea>{{ textarea_content }}</textarea>",
        [("textarea_content", "(str(v) if v else '')")],
    ),
    (
        "computed tag name",
        "<{{ tag }} class='c'>x</{{ tag }}>",
        "<c-element c-is=\"tag\" class='c'>x</c-element>",
        [],
    ),
]


@pytest.mark.parametrize("label,source,expected,hoists", CASES, ids=[c[0] for c in CASES])
def test_construct(label, source, expected, hoists):
    result = translate(source)
    assert result.template.strip() == expected
    assert result.hoists == hoists
    assert result.markers == []


def node_classes(source):
    def walk(nodelist):
        for node in nodelist:
            yield type(node).__name__.split("_")[0]
            for name in getattr(node, "child_nodelists", ()):
                if (sub := getattr(node, name, None)) is not None:
                    yield from walk(sub)
            for _, sub in getattr(node, "conditions_nodelists", None) or []:
                yield from walk(sub)

    return set(walk(engines["django"].engine.from_string(source).nodelist))


def test_every_handled_node_class_has_a_case():
    covered = set().union(*(node_classes(source) for _, source, _, _ in CASES))
    handled = set(EMIT) | DROPPED | {"IfNode", "ForNode", "VariableNode", "TextNode"}
    assert handled - covered == set()


def test_forloop_reaches_a_component_argument():
    source = (
        LOAD
        + '{% for i in xs %}{% component "x" n=forloop.counter0 %}{% endcomponent %}{% endfor %}'
    )
    result = translate(source)
    assert 'c-n="forloop_index"' in result.template
    assert result.hoists == [("loop", "list(enumerate(xs))")]


def test_a_slot_in_a_raw_text_element_becomes_an_expression():
    source = LOAD + '<textarea>{% slot "default" / %}</textarea>'
    result = translate(source)
    assert result.template.strip() == "<textarea>{{ textarea_content }}</textarea>"
    assert result.hoists == [
        ("textarea_content", "(str(slots.default) if slots.default is not None else '')")
    ]
    assert result.markers == []


LOOKUP = "{% for item in items %}<b>{{ item.active }}</b>{% endfor %}"


def test_a_declared_dict_sequence_is_read_by_key():
    result = translate(LOOKUP, dict_sequences=frozenset({"items"}))
    assert "{{ item['active'] }}" in result.template


def test_an_undeclared_sequence_stays_attribute_access():
    assert "{{ item.active }}" in translate(LOOKUP).template
