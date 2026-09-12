"""How every fixture in `fixtures/` is made. Committed, because it is the recipe.

A fixture is a binary artifact, and a binary artifact with no source is one
nobody can change. This module is that source: one function per fixture, each
taking a live instance and filling it, which is exactly the shape
`seahaven fixture freeze <id> --run fixtures_src.generate:<id>` calls
once the CLI exists (Phase 7 of the implementation plan). Until then, running
this module does the same thing itself:

    uv run python worlds/projecttracker/fixtures_src/generate.py empty

Every fixture is frozen from a *blank* instance at `NOW`, so the tracker's clock
is the same instant in all of them and a scenario written against one reads the
same as a scenario written against another. Nothing here reads the wall clock,
and nothing here is random outside `ctx.ids`.
"""

import sys
from pathlib import Path

import seahaven

# The instant every fixture's clock is frozen at. Chosen once and never moved:
# it is baked into every sidecar, and moving it would invalidate every scenario
# written against a fixture's dates.
NOW = "2026-06-01T09:00:00.000Z"

# What an eval author reads when choosing a fixture. Prose about the data, not
# about the schema: what is in it, and what it is good for.
DESCRIPTIONS = {
    "empty": (
        "The tracker's schema with no rows. Start here to write a history, or to test setup flows."
    ),
}


def empty(inst: seahaven.Instance) -> None:
    """`empty`: the schema and nothing else.

    Deliberately does no work. It exists so that `empty` is made the same way
    every other fixture is -- freeze a blank instance through this module -- and
    so the fixture that later ones are forked from has a source like the rest.
    """


# Every fixture this module can build, by id. `seahaven fixture freeze` names one
# of these functions directly; `main` below looks it up here.
BUILDERS = {"empty": empty}


def build(fixture_id: str) -> seahaven.Fixture:
    """Freeze `fixture_id` into the world's fixtures directory, and return it.

    The instance is destroyed whether the build succeeds or not, and a failure
    leaves no fixture directory behind: `freeze` publishes by rename.
    """
    # Imported here, not at module import: `seahaven fixture freeze --run` imports
    # this module for one function, and a builder should not cost a world import
    # until it is called. Spelled through `world.py` rather than through the
    # package attribute, which is a `World` shadowing the module of that name.
    from projecttracker.world import world

    builder = BUILDERS[fixture_id]
    with world.instance(None, now=NOW) as inst:
        builder(inst)
        return inst.freeze(fixture_id, DESCRIPTIONS[fixture_id])


def main(argv: list[str]) -> int:
    """`python generate.py <fixture-id>...`, or with no arguments, every fixture.

    Refuses to overwrite: `freeze` fails if the directory exists, because a
    fixture is immutable once it is published. Rebuilding one means deleting it
    first, deliberately, and committing the new bytes.
    """
    ids = argv or sorted(BUILDERS)
    unknown = [fixture_id for fixture_id in ids if fixture_id not in BUILDERS]
    if unknown:
        print(f"no such fixture: {', '.join(unknown)}", file=sys.stderr)
        print(f"known fixtures: {', '.join(sorted(BUILDERS))}", file=sys.stderr)
        return 1
    for fixture_id in ids:
        fixture = build(fixture_id)
        print(f"froze {fixture.id} at {fixture.now} -> {fixture.dir}")
    return 0


if __name__ == "__main__":
    # Run as a script from a checkout, where the world may not be installed.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    raise SystemExit(main(sys.argv[1:]))
