"""Bootstrapping the two engines the tool talks to."""

from __future__ import annotations

import os
from pathlib import Path

urlpatterns: list = []  # ROOT_URLCONF target for the standalone config


def ensure_django(settings_module: str | None = None, compat: bool = False) -> None:
    import django
    from django.apps import apps
    from django.conf import settings

    if settings.configured:
        if not apps.ready:
            django.setup()
        return

    if settings_module:
        os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    else:
        settings.configure(
            BASE_DIR=Path.cwd(),
            SECRET_KEY="djc_to_citry",
            ROOT_URLCONF="djc_to_citry.djsetup",
            INSTALLED_APPS=["django_components"],
            DATABASES={},
            USE_TZ=True,
            CITRY_APP="djc_to_citry.citryapp:app",
            TEMPLATES=[
                {
                    "BACKEND": (
                        "citry_django.backend.CitryTemplates"
                        if compat
                        else "django.template.backends.django.DjangoTemplates"
                    ),
                    "DIRS": [],
                    "APP_DIRS": False,
                    "OPTIONS": {"builtins": ["django_components.templatetags.component_tags"]},
                }
            ],
            COMPONENTS={"autodiscover": False},
        )
    django.setup()


_app = None


class CompatUnavailable(RuntimeError):
    pass


def build():
    from citry import Citry

    try:
        from citry_django import CitryDjangoExtension
    except ImportError as exc:
        raise CompatUnavailable("compat validation needs citry-django installed") from exc

    try:
        from citry_django_djc import tokenize
    except ImportError:
        # without it the two halves disagree on where a tag carrying template
        # source as an argument ends
        tokenize = None

    ext = CitryDjangoExtension(tokenizer=tokenize) if tokenize else CitryDjangoExtension()
    return Citry(extensions=[ext])


def __getattr__(name):
    global _app
    if name != "app":
        raise AttributeError(name)
    if _app is None:
        _app = build()
    return _app
