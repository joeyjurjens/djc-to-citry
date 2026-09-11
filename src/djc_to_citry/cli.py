"""djc-to-citry command line.

Each command is a function plus the arguments it takes; `main` builds the
parser from that list and nothing else.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass

from . import CITRY_VERIFIED_AGAINST, __version__
from .emit import Marker
from .engines import ensure_django

RESIDUE = ".djc-to-citry/residue.json"


def arg(*flags, **kw):
    return flags, kw


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    run: Callable
    args: tuple = ()


MODE = arg(
    "--mode",
    default="pure",
    choices=["pure", "compat"],
    help="pure = citry only; compat = keep Django syntax (needs citry-django)",
)


def migrate(args) -> int:
    from .codemod import migrate_module

    source = pathlib.Path(args.path).read_text()
    out, markers, ok = migrate_module(
        source,
        base=args.base,
        unwrap=args.unwrap,
        mode=args.mode,
        renames=dict(args.rename),
        app=args.app,
        tag_prefix=args.tag_prefix,
    )

    if ok == 0 and markers:
        # Writing here would replace the input with a comment.
        print(f"\nnothing migrated, {len(markers)} marker(s)", file=sys.stderr)
        for m in markers:
            print(f"  [{m.rule}] {m.component}: {m.what}", file=sys.stderr)
        return 1

    if args.out:
        target = pathlib.Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        if markers:
            out += "\n\n" + "\n\n".join(m.as_comment("") for m in markers) + "\n"
        target.write_text(out)
        residue = target.parent / RESIDUE
        residue.parent.mkdir(parents=True, exist_ok=True)
        residue.write_text(json.dumps([m.to_dict() for m in markers], indent=2))
    else:
        print(out)

    print(f"\n{ok} migrated, {len(markers)} marker(s)", file=sys.stderr)
    for m in markers:
        print(f"  [{m.rule}] {m.component}: {m.what}", file=sys.stderr)
    return 1 if markers else 0


def scan(args) -> int:
    from .codemod import scan_module

    rows = scan_module(pathlib.Path(args.path).read_text(), mode=args.mode)
    print(json.dumps(rows, indent=2) if args.json else _fmt_scan(rows))
    return 0 if all(r["status"] == "ok" for r in rows) else 1


def _fmt_scan(rows) -> str:
    mark = {"ok": "ok ", "marked": "?? ", "unsupported": "!! ", "error": "XX "}
    out = []
    for r in rows:
        detail = (
            f"slots={r.get('slots', [])} renders={r.get('renders', [])} hoisted={r.get('hoisted', 0)}"
            if r["status"] in ("ok", "marked")
            else f"[{','.join(r['rules'])}] {r['detail']}"
        )
        out.append(f"{mark[r['status']]}{r['component']:<16} {detail}")
    ok = sum(r["status"] == "ok" for r in rows)
    out.append(f"\n{ok}/{len(rows)} translatable")
    if errors := sum(r["status"] == "error" for r in rows):
        out.append(f"{errors} INTERNAL error(s) -- tool bug, not a migration finding")
    return "\n".join(out)


def residue(args) -> int:
    path = pathlib.Path(args.path)
    data = json.loads(path.read_text()) if path.exists() else []
    if args.rule:
        data = [d for d in data if d["rule"] == args.rule]
    for d in data:
        print(
            Marker(**d).as_comment("")
            if args.full
            else f"[{d['rule']}] {d['component']}: {d['what']}"
        )
    print(f"\n{len(data)} marker(s)", file=sys.stderr)
    return 0


def template(args) -> int:
    """Translate template text: a template file, or the literals in a .py file."""
    from .emit import rewrite

    total, failed = 0, 0
    for raw in args.paths:
        path = pathlib.Path(raw)
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for f in files:
            result = rewrite(f.read_text(), args.mode, args.tag_prefix)
            total += result.translated
            failed += len(result.markers)
            if args.write:
                f.write_text(result.text)
            else:
                print(result.text if len(files) == 1 else f"# --- {f}\n{result.text}")
            for marker in result.markers:
                print(f"!! [{marker.rule}] {f}: {marker.what}", file=sys.stderr)
    print(f"\n{total} template(s) translated, {failed} refused", file=sys.stderr)
    return 1 if failed else 0


def extension(args) -> int:
    from . import extension as module

    source = pathlib.Path(module.__file__).read_text()
    if args.out:
        target = pathlib.Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        print(f"wrote {target}", file=sys.stderr)
    else:
        print(source, end="")
    return 0


COMMANDS = (
    Command(
        "scan",
        "classify every component against the rule table",
        scan,
        (arg("path"), arg("--json", action="store_true"), MODE),
    ),
    Command(
        "migrate",
        "translate a component module",
        migrate,
        (
            arg("path"),
            arg("--out"),
            MODE,
            arg(
                "--base",
                default="Component",
                metavar="CLASS",
                help="Component, LibraryComponent, or a dotted path to your own base class",
            ),
            arg(
                "--unwrap",
                default="inline",
                choices=["inline", "none", "mark"],
                help="what to do about citry's constant markers: unwrap them with a "
                "helper in each module, leave it to something you install "
                "yourself, or leave it and mark the code that depends on it",
            ),
            arg(
                "--tag-prefix",
                default="",
                metavar="bs-",
                help="publish every component under this tag prefix, internal references included",
            ),
            arg(
                "--app",
                default="",
                metavar="module.attr",
                help="bind each Component to this Citry instance, e.g. myproject.components.app",
            ),
            arg(
                "--rename",
                action="append",
                default=[],
                type=lambda v: tuple(v.split("=", 1)),
                metavar="OLD=NEW",
                help="rewrite an import prefix, e.g. old_pkg.components=new_pkg",
            ),
        ),
    ),
    Command(
        "residue",
        "list markers from a migration",
        residue,
        (
            arg("path", nargs="?", default=RESIDUE),
            arg("--rule"),
            arg("--full", action="store_true"),
        ),
    ),
    Command(
        "template",
        "translate template text outside a component",
        template,
        (
            arg("paths", nargs="+"),
            MODE,
            arg(
                "--tag-prefix",
                default="",
                metavar="bs-",
                help="the prefix the converted library publishes its components under",
            ),
            arg("-w", "--write", action="store_true"),
        ),
    ),
    Command(
        "extension",
        "write the PlainInputs extension into a project",
        extension,
        (arg("--out"),),
    ),
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="djc-to-citry", description="Migrate django-components libraries to citry"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"djc-to-citry {__version__} (verified against citry {CITRY_VERIFIED_AGAINST})",
    )
    parser.add_argument("--settings", help="DJANGO_SETTINGS_MODULE for the source project")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for command in COMMANDS:
        p = sub.add_parser(command.name, help=command.help)
        for flags, kw in command.args:
            p.add_argument(*flags, **kw)
        p.set_defaults(run=command.run)

    args = parser.parse_args(argv)
    ensure_django(args.settings, compat=getattr(args, "mode", "") == "compat")
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
