import pytest

from djc_to_citry.engines import ensure_django

ensure_django(None, compat=False)

from django.template import engines

from djc_to_citry.locate import region_at, regions


def where(source):
    found = regions(source)
    nodelist = engines["django"].engine.from_string(source).nodelist
    out = []
    for node in nodelist:
        token = getattr(node, "token", None)
        if token is None or type(node).__name__ == "TextNode":
            continue
        region = region_at(found, *token.position)
        out.append((source[slice(*token.position)], region.kind, region.element, region.attr))
    return out


@pytest.mark.parametrize(
    "source,construct,expected",
    [
        ('<div {% if x %}class="on"{% endif %}>y</div>', "{% if x %}", ("start_tag", "div", "")),
        ("<div {% if x %}a{% endif %}>{{ n }}</div>", "{{ n }}", ("body", "div", "")),
        ('<textarea>{% slot "d" / %}</textarea>', '{% slot "d" / %}', ("raw", "textarea", "")),
        ("<script>var x = {{ n }};</script>", "{{ n }}", ("raw", "script", "")),
        ('<p title="{{ t }}">y</p>', "{{ t }}", ("attr", "p", "title")),
        ("plain {{ x }}", "{{ x }}", ("body", "", "")),
        ("<p>café {{ n }}</p>", "{{ n }}", ("body", "p", "")),
        ("<{{ tag }} id='a'>y</{{ tag }}>", "{{ tag }}", ("dynamic", "", "")),
    ],
)
def test_region(source, construct, expected):
    assert [r[1:] for r in where(source) if r[0] == construct] == [expected] * len(
        [r for r in where(source) if r[0] == construct]
    )


def test_masking_preserves_positions():
    from djc_to_citry.locate import mask_dynamic_tags

    source = "<{{ tag }} id='a'>body</{{ tag }}>"
    masked, spans = mask_dynamic_tags(source)
    assert len(masked) == len(source)
    assert masked.count("e" * 9) == 2
    assert len(spans) == 2
