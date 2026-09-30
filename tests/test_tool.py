"""Building a tool from a function: what is accepted, what is refused, and what reaches the wire."""

import decimal
import json
import threading
import uuid
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, date, datetime, time
from enum import Enum, StrEnum
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NewType, TypedDict

import pydantic
import pytest
from pydantic import BaseModel, ConfigDict, Field, with_config

from seahaven.ctx import Ctx
from seahaven.errors import ArgumentError, WorldBug
from seahaven.handles import WorldHandle, Worlds
from seahaven.tool import Tool, _blame, _build_schema
from tests.conftest import build_world

if TYPE_CHECKING:  # deliberately not importable at runtime: see the refusal below
    from decimal import Decimal


class Point(BaseModel):
    model_config = ConfigDict(strict=True)

    x: int
    y: int


class Lax(BaseModel):
    n: int


Loose = NewType("Loose", Lax)


class Relaxed(BaseModel):
    model_config = ConfigDict(strict=False)

    n: int


@dataclass
class Span:
    start: int
    end: int


class Window(TypedDict):
    start: str
    end: str


class Colour(Enum):
    RED = "red"


class Shade(StrEnum):
    DARK = "dark"


class Palette(TypedDict):
    colour: Literal[Colour.RED]


class Booking(TypedDict):
    room: str
    at: datetime


@dataclass
class Holder:
    inner: Lax


class Holding(TypedDict):
    inner: Lax


@pydantic.dataclasses.dataclass
class LaxPydanticDataclass:
    n: int


@with_config(ConfigDict(str_strip_whitespace=True))
class ConfiguredTypedDict(TypedDict):
    n: int


class Wrapper(BaseModel):
    model_config = ConfigDict(strict=True)

    inner: Lax


class Branch(BaseModel):
    model_config = ConfigDict(strict=True)

    children: list[Branch]


# Two names for a type that carry the type they stand for, and publish its schema.
type Timestamp = datetime
Stamp = NewType("Stamp", datetime)

# An alias to a symbol that exists only under TYPE_CHECKING, and one defined in
# terms of itself: the two shapes that make looking through an alias interesting.
type Price = Decimal
type Tree = list[Tree]


def build(fn: Any, **options: Any) -> Tool:
    return Tool.from_function(fn, **options)


def refusal(fn: Any, **options: Any) -> str:
    with pytest.raises(WorldBug) as raised:
        build(fn, **options)
    return str(raised.value)


def test_the_argument_model_covers_the_json_types() -> None:
    def search(
        ctx: Ctx,
        text: str,
        limit: int,
        ratio: float,
        exact: bool,
        project: str | None,
        tags: list[str],
        counts: dict[str, int],
        status: Literal["open", "closed"],
        origin: Point,
        window: Window,
    ) -> dict[str, Any]:
        """Search."""
        return {}

    tool = build(search)
    arguments = {
        "text": "a",
        "limit": 1,
        "ratio": 1.5,
        "exact": True,
        "project": None,
        "tags": ["x"],
        "counts": {"x": 1},
        "status": "open",
        "origin": {"x": 1, "y": 2},
        "window": {"start": "s", "end": "e"},
    }

    validated = tool.validate(arguments)

    assert validated["project"] is None
    assert validated["status"] == "open"
    # A nested model arrives as the model, a TypedDict as the dict it is.
    assert validated["origin"] == Point(x=1, y=2)
    assert validated["window"] == {"start": "s", "end": "e"}


def test_annotated_metadata_reaches_the_schema_and_is_enforced() -> None:
    def page(ctx: Ctx, limit: Annotated[int, Field(ge=1, le=50, description="how many")]) -> dict:
        """Page."""
        return {}

    tool = build(page)

    assert tool.schema["properties"]["limit"]["description"] == "how many"
    assert tool.schema["properties"]["limit"]["minimum"] == 1
    with pytest.raises(ArgumentError):
        tool.validate({"limit": 99})


STAMP = "2026-06-01T09:00:00Z"
AT = datetime(2026, 6, 1, 9, tzinfo=UTC)
UID = uuid.UUID(int=5)

# Each annotation, a value as parsed JSON spells it, and what the tool receives.
JSON_SPELLINGS: list[tuple[Any, Any, Any]] = [
    (tuple[int, int], [1, 2], (1, 2)),
    (uuid.UUID, str(UID), UID),
    (decimal.Decimal, "1.50", decimal.Decimal("1.50")),
    (decimal.Decimal, 1.5, decimal.Decimal("1.5")),
    (set[int], [1, 2], {1, 2}),
    (frozenset[str], ["a"], frozenset({"a"})),
    (bytes, "abc", b"abc"),
    (Path, "/srv/data", Path("/srv/data")),
    (IPv4Address, "10.0.0.1", IPv4Address("10.0.0.1")),
    (Span, {"start": 1, "end": 2}, Span(1, 2)),
    (datetime, STAMP, AT),
    (date, "2026-06-01", date(2026, 6, 1)),
    (time, "09:00:00", time(9)),
    (Colour, "red", Colour.RED),
    (Shade, "dark", Shade.DARK),
    (Literal[Shade.DARK], "dark", Shade.DARK),
    (Annotated[datetime, Field()], STAMP, AT),
    (datetime | None, None, None),
    (Timestamp, STAMP, AT),
    (list[Stamp], [STAMP], [AT]),
    (Booking, {"room": "r1", "at": STAMP}, {"room": "r1", "at": AT}),
]


@pytest.mark.parametrize(("annotation", "sent", "received"), JSON_SPELLINGS)
def test_an_argument_is_accepted_as_parsed_json_spells_it(
    annotation: Any, sent: Any, received: Any
) -> None:
    """What the wire hands over is parsed JSON, and the published schema describes that."""

    def at(ctx: Ctx, value: annotation) -> dict:
        """At."""
        return {}

    tool = build(at)

    assert tool.validate(json.loads(json.dumps({"value": sent}))) == {"value": received}


@pytest.mark.parametrize(("annotation", "sent", "received"), JSON_SPELLINGS)
def test_an_in_process_caller_may_hand_over_the_python_object(
    annotation: Any, sent: Any, received: Any
) -> None:
    def at(ctx: Ctx, value: annotation) -> dict:
        """At."""
        return {}

    assert build(at).validate({"value": received}) == {"value": received}


def test_an_in_process_object_keeps_its_identity() -> None:
    """A value strict Python validation accepts is handed over as it was, not rebuilt."""

    def place(ctx: Ctx, origin: Point, raw: str | bytes) -> dict:
        """Place."""
        return {}

    origin = Point(x=1, y=2)

    validated = build(place).validate({"origin": origin, "raw": b"\x00\xff"})

    assert validated["origin"] is origin
    assert validated["raw"] == b"\x00\xff"


def test_a_call_holding_a_python_object_is_validated_as_python_throughout() -> None:
    """The mode is the whole call's: beside a real `UUID`, a list is not a `tuple`."""

    def book(ctx: Ctx, id: uuid.UUID, span: tuple[int, int]) -> dict:
        """Book."""
        return {}

    tool = build(book)

    assert tool.validate({"id": UID, "span": (1, 2)}) == {"id": UID, "span": (1, 2)}
    with pytest.raises(ArgumentError) as raised:
        tool.validate({"id": UID, "span": [1, 2]})

    assert [violation["path"] for violation in raised.value.violations] == ["span"]


@pytest.mark.parametrize(
    ("annotation", "sent"),
    [
        (str, AT),
        (str, UID),
        (str, decimal.Decimal("1.5")),
        (str, b"abc"),
        (str, Colour.RED),
        (dict[str, int], Point(x=1, y=2)),
        (list[int], (1, 2)),
    ],
)
def test_a_python_object_is_not_rendered_to_fit_another_annotation(
    annotation: Any, sent: Any
) -> None:
    """Its JSON form would fit, and strict Python validation refuses it all the same."""

    def at(ctx: Ctx, value: annotation) -> dict:
        """At."""
        return {}

    with pytest.raises(ArgumentError):
        build(at).validate({"value": sent})


@pytest.mark.parametrize(
    ("annotation", "sent", "received"),
    [
        (datetime | str, STAMP, AT),
        (str | datetime, STAMP, STAMP),
        (uuid.UUID | str, str(UID), UID),
    ],
)
def test_a_union_of_types_one_json_string_fits_takes_the_member_pydantic_picks(
    annotation: Any, sent: Any, received: Any
) -> None:
    """Pinned rather than chosen: pydantic's smart union decides, and order can matter."""

    def at(ctx: Ctx, value: annotation) -> dict:
        """At."""
        return {}

    assert build(at).validate({"value": sent}) == {"value": received}


@pytest.mark.parametrize(
    ("annotation", "sent"),
    [
        (int, "5"),
        (bool, "true"),
        (float, "1.5"),
        (tuple[int, int], ["1", "2"]),
        (uuid.UUID, 5),
        (datetime, 1_700_000_000),
        (datetime, "2026-06-01"),
        (Colour, "RED"),
        (set[int], ["1"]),
        (Span, {"start": "1", "end": 2}),
        (Booking, {"room": 1, "at": STAMP}),
        (Point, {"x": "1", "y": 2}),
    ],
)
def test_the_json_spelling_is_held_strictly_at_every_depth(annotation: Any, sent: Any) -> None:
    def at(ctx: Ctx, value: annotation) -> dict:
        """At."""
        return {}

    with pytest.raises(ArgumentError) as raised:
        build(at).validate({"value": sent})

    assert raised.value.violations[0]["path"].startswith("value")


def test_a_value_with_no_json_form_is_refused_as_an_argument() -> None:
    """Only an in-process caller can send one, and the refusal is Python validation's."""

    def count(ctx: Ctx, n: int) -> dict:
        """Count."""
        return {}

    with pytest.raises(ArgumentError) as raised:
        build(count).validate({"n": threading.Lock()})

    assert [violation["path"] for violation in raised.value.violations] == ["n"]


@pytest.mark.parametrize(
    "annotation",
    [
        Lax,
        Lax | None,
        list[Lax],
        Annotated[Lax, Field(description="lax")],
        Loose,
        Wrapper,
        Holder,
        Holding,
        LaxPydanticDataclass,
        ConfiguredTypedDict,
    ],
)
def test_a_nested_type_with_its_own_lax_config_is_refused(annotation: Any) -> None:
    """A type with a pydantic config of its own validates under it, not the tool's."""

    def at(ctx: Ctx, value: annotation) -> dict:
        """At."""
        return {}

    message = refusal(at)

    assert message.startswith("tool 'at': argument 'value' uses ")
    assert "`strict=True`" in message


@pytest.mark.parametrize(
    "annotation", [Literal[Colour.RED], Literal[b"red"], list[Literal["a", Colour.RED]], Palette]
)
def test_a_literal_json_cannot_send_is_refused(annotation: Any) -> None:
    """The schema says `"const": "red"`, and validation would accept only the member."""

    def at(ctx: Ctx, value: annotation) -> dict:
        """At."""
        return {}

    message = refusal(at)

    assert message.startswith("tool 'at': argument 'value' is a `Literal` of ")
    assert "JSON cannot send" in message


def test_a_nested_model_that_chose_lax_input_gets_it() -> None:
    def at(ctx: Ctx, value: Relaxed) -> dict:
        """At."""
        return {}

    assert build(at).validate({"value": {"n": "5"}}) == {"value": Relaxed(n=5)}


def test_a_model_defined_in_terms_of_itself_still_registers() -> None:
    def grow(ctx: Ctx, root: Branch) -> dict:
        """Grow."""
        return {}

    tool = build(grow)

    assert tool.validate({"root": {"children": [{"children": []}]}}) == {
        "root": Branch(children=[Branch(children=[])])
    }


def test_json_spellings_reach_a_tool_called_by_name(tmp_path: Path) -> None:
    """The real entry point: the arguments an agent sends, through `world.instance(...)`."""
    world = build_world(tmp_path)

    received: list[tuple[Any, ...]] = []

    @world.tool
    def reserve(
        ctx: Ctx,
        span: tuple[int, int],
        id: uuid.UUID,
        amount: decimal.Decimal,
        tags: set[str],
        where: Span,
        at: datetime,
        colour: Colour,
    ) -> dict[str, bool]:
        """Reserve."""
        received.append((span, id, amount, tags, where, at, colour))
        return {"reserved": True}

    sent = json.loads(
        json.dumps(
            {
                "span": [1, 2],
                "id": str(UID),
                "amount": "9.99",
                "tags": ["a"],
                "where": {"start": 1, "end": 2},
                "at": STAMP,
                "colour": "red",
            }
        )
    )

    with world.instance(None) as instance:
        assert instance.call("reserve", **sent) == {"reserved": True}
        with pytest.raises(ArgumentError) as raised:
            instance.call("reserve", **{**sent, "span": ["1", "2"], "colour": "RED"})

    assert received == [((1, 2), UID, decimal.Decimal("9.99"), {"a"}, Span(1, 2), AT, Colour.RED)]
    assert {violation["path"] for violation in raised.value.violations} == {
        "span.0",
        "span.1",
        "colour",
    }


def test_a_lone_surrogate_json_can_carry_reaches_a_tool_called_by_name(tmp_path: Path) -> None:
    """Parsed JSON with no UTF-8 text: validated as the `str` it is, not an internal error."""
    world = build_world(tmp_path)

    @world.tool
    def echo(ctx: Ctx, s: str) -> dict[str, str]:
        """Echo."""
        return {"s": s}

    with world.instance(None) as instance:
        assert instance.call("echo", **json.loads('{"s": "a\\ud800"}')) == {"s": "a\ud800"}
        with pytest.raises(ArgumentError):
            instance.call("echo", **json.loads('{"s": ["a\\ud800"]}'))


def test_deeply_nested_json_reaches_a_tool_called_by_name(tmp_path: Path) -> None:
    world = build_world(tmp_path)

    @world.tool
    def keep(ctx: Ctx, value: Any) -> dict[str, bool]:
        """Keep."""
        return {"kept": True}

    with world.instance(None) as instance:
        assert instance.call("keep", value=json.loads("[" * 600 + "]" * 600)) == {"kept": True}


def test_defaults_are_published_and_required_arguments_are_not() -> None:
    def get(ctx: Ctx, key: str, verbose: bool = False) -> dict:
        """Get."""
        return {}

    tool = build(get)

    assert tool.schema["required"] == ["key"]
    assert tool.schema["properties"]["verbose"]["default"] is False
    assert tool.validate({"key": "k"}) == {"key": "k", "verbose": False}


@pytest.mark.parametrize("default", [[], {}, set(), bytearray()])
def test_a_mutable_default_is_refused(default: Any) -> None:
    def get(ctx: Ctx, tags: Any = default) -> dict:
        """Get."""
        return {}

    assert "use `None` and default inside the tool" in refusal(get)


def test_an_unannotated_argument_is_refused() -> None:
    def get(ctx: Ctx, key) -> dict:
        """Get."""
        return {}

    assert "'key' needs a type annotation" in refusal(get)


def test_variadic_arguments_are_refused() -> None:
    def positional(ctx: Ctx, *args: int) -> dict:
        """Positional."""
        return {}

    def keyword(ctx: Ctx, **kwargs: int) -> dict:
        """Keyword."""
        return {}

    assert "named arguments only" in refusal(positional)
    assert "named arguments only" in refusal(keyword)


def test_a_positional_only_argument_is_refused() -> None:
    """Arguments are passed by name at call time, so one that cannot be would fail then."""

    def get(ctx: Ctx, key: str, /) -> dict:
        """Get."""
        return {}

    assert "positional-only" in refusal(get)


def test_a_tool_needs_a_context_parameter() -> None:
    def get() -> dict:
        """Get."""
        return {}

    assert "takes the context as its first parameter" in refusal(get)


def test_the_context_parameter_is_positional_and_unannotated_or_a_ctx() -> None:
    def annotated(ctx: Ctx, key: str) -> dict:
        """Annotated."""
        return {}

    def bare(ctx, key: str) -> dict:
        """Bare."""
        return {}

    assert build(annotated).name == "annotated"
    assert build(bare).name == "bare"

    def wrong_type(ctx: str, key: str) -> dict:
        """Wrong."""
        return {}

    def keyword_only(*, ctx: Ctx) -> dict:
        """Keyword only."""
        return {}

    assert "the first parameter is the context" in refusal(wrong_type)
    assert "the first parameter is the context" in refusal(keyword_only)


def test_a_parameterised_context_is_a_context() -> None:
    """`Ctx[CompanyWorlds]` is a world declaring its children to a type checker.

    It is the same object at run time as a bare `Ctx`, so registration has no
    reason to know the difference -- and the annotation is what the whole of
    architecture section 8.3 is (`seahaven check` binds the class to the
    registrations, not this).
    """

    class HostWorlds(Worlds):
        payments: WorldHandle

    def declared(ctx: Ctx[HostWorlds], key: str) -> dict:
        """Declared."""
        return {}

    def anything(ctx: Ctx[Any], key: str) -> dict:
        """Anything."""
        return {}

    assert build(declared).name == "declared"
    assert build(anything).name == "anything"

    def other(ctx: dict[str, Any], key: str) -> dict:
        """Some other generic altogether."""
        return {}

    assert "the first parameter is the context" in refusal(other)


def test_only_plain_synchronous_functions_are_tools() -> None:
    async def coroutine(ctx: Ctx) -> dict:
        """Coroutine."""
        return {}

    def generator(ctx: Ctx) -> Any:
        """Generator."""
        yield {}

    async def async_generator(ctx: Ctx) -> Any:
        """Async generator."""
        yield {}

    for fn in (coroutine, generator, async_generator):
        assert "plain synchronous function" in refusal(fn)


def test_an_annotation_that_exists_only_under_type_checking_is_refused_by_name() -> None:
    def price(ctx: Ctx, amount: Decimal) -> dict:
        """Price."""
        return {}

    message = refusal(price)

    assert "'Decimal'" in message
    assert "TYPE_CHECKING" in message


def test_an_alias_to_a_type_checking_only_symbol_is_refused_as_a_world_bug() -> None:
    """The alias resolves at signature time; the symbol behind it never does."""

    def priced(ctx: Ctx, amount: Price, key: str) -> dict:
        """Priced."""
        return {}

    message = refusal(priced)

    assert message.startswith("tool 'priced', argument 'amount': ")
    assert "Decimal" in message


def test_an_alias_defined_in_terms_of_itself_still_registers() -> None:
    """Looking through aliases has to end: the walk remembers what it has seen."""

    def nested(ctx: Ctx, tree: Tree) -> dict:
        """Nested."""
        return {}

    assert "tree" in build(nested).schema["properties"]


def test_an_annotation_pydantic_cannot_build_names_the_tool_and_the_argument() -> None:
    class Opaque:
        pass

    def get(ctx: Ctx, thing: Opaque) -> dict:
        """Get."""
        return {}

    message = refusal(get)

    assert message.startswith("tool 'get', argument 'thing': ")
    assert "Unable to generate pydantic-core schema" in message


def test_an_argument_name_that_shadows_the_model_names_the_argument() -> None:
    def get(ctx: Ctx, model_dump: str) -> dict:
        """Get."""
        return {}

    assert refusal(get).startswith("tool 'get', argument 'model_dump': ")


def test_an_alias_is_the_wire_name_and_the_python_name_is_not() -> None:
    def copy_from(ctx: Ctx, from_: Annotated[str, Field(alias="from")]) -> dict:
        """Copy."""
        return {}

    tool = build(copy_from)

    assert list(tool.schema["properties"]) == ["from"]
    assert tool.validate({"from": "a"}) == {"from_": "a"}
    with pytest.raises(ArgumentError):
        tool.validate({"from_": "a"})


def test_the_schema_forbids_unknown_arguments_and_carries_no_model_title() -> None:
    def place(ctx: Ctx, origin: Point) -> dict:
        """Place."""
        return {}

    schema = build(place).schema

    assert schema["additionalProperties"] is False
    assert "title" not in schema
    assert "Point" in schema["$defs"]


def test_the_description_is_the_whole_docstring_unless_given() -> None:
    def get(ctx: Ctx) -> dict:
        """Fetch one issue.

        The whole docstring is what the agent reads.
        """
        return {}

    def bare(ctx: Ctx) -> dict:
        return {}

    assert build(get).description == (
        "Fetch one issue.\n\nThe whole docstring is what the agent reads."
    )
    assert build(get, description="Other").description == "Other"
    assert build(bare).description == ""


def test_the_name_and_the_transaction_flag_are_options() -> None:
    def get(ctx: Ctx) -> dict:
        """Get."""
        return {}

    tool = build(get, name="fetch", transaction=False)

    assert (tool.name, tool.transaction, tool.control) == ("fetch", False, False)
    assert build(get).transaction is True


def test_every_violation_of_one_call_is_reported() -> None:
    def create(ctx: Ctx, title: str, count: int) -> dict:
        """Create."""
        return {}

    with pytest.raises(ArgumentError) as raised:
        build(create).validate({"count": "1", "extra": True})

    assert {violation["path"] for violation in raised.value.violations} == {
        "title",
        "count",
        "extra",
    }
    assert raised.value.tool == "create"


@pytest.mark.parametrize(
    ("arguments", "accepted"),
    [
        ({"n": 5, "flag": False, "ratio": 1.5}, True),
        ({"n": "5", "flag": False, "ratio": 1.5}, False),
        ({"n": 2.0, "flag": False, "ratio": 1.5}, False),
        ({"n": 5, "flag": 1, "ratio": 1.5}, False),
        # An int is a float: the one widening JSON itself makes.
        ({"n": 5, "flag": False, "ratio": 2}, True),
    ],
)
def test_validation_is_strict(arguments: dict[str, Any], accepted: bool) -> None:
    def measure(ctx: Ctx, n: int, flag: bool, ratio: float) -> dict:
        """Measure."""
        return {}

    tool = build(measure)

    if accepted:
        assert tool.validate(arguments)["n"] == 5
    else:
        with pytest.raises(ArgumentError):
            tool.validate(arguments)


def test_strict_is_relaxed_for_one_argument_at_a_time() -> None:
    def page(ctx: Ctx, limit: Annotated[int, Field(strict=False)], offset: int) -> dict:
        """Page."""
        return {}

    tool = build(page)

    assert tool.validate({"limit": "5", "offset": 0})["limit"] == 5
    with pytest.raises(ArgumentError):
        tool.validate({"limit": 5, "offset": "0"})


def test_a_listing_is_exactly_what_a_tool_list_carries() -> None:
    def get(ctx: Ctx, key: str) -> dict:
        """Get one."""
        return {}

    tool = build(get)

    assert tool.listing() == {
        "name": "get",
        "description": "Get one.",
        "input_schema": tool.schema,
    }


def test_a_tool_is_hashable_by_name_and_equal_only_to_itself() -> None:
    def get(ctx: Ctx) -> dict:
        """Get."""
        return {}

    tool = build(get)

    assert hash(tool) == hash(build(get, description="different"))
    assert tool != build(get)
    # Field by field this copy is indistinguishable, and it is still not this
    # tool: equality is identity, not a comparison of what a tool carries.
    assert replace(tool) != tool
    assert tool in {tool}


def test_a_tool_is_frozen() -> None:
    def get(ctx: Ctx) -> dict:
        """Get."""
        return {}

    with pytest.raises(FrozenInstanceError):
        build(get).name = "other"  # ty: ignore[invalid-assignment]


def test_the_schema_describes_the_arguments_a_call_sends() -> None:
    """The input schema, not the output one: a validation alias is a wire name."""

    def copy_from(ctx: Ctx, from_: Annotated[str, Field(validation_alias="from")]) -> dict:
        """Copy."""
        return {}

    assert list(build(copy_from).schema["properties"]) == ["from"]


def test_a_tool_built_by_a_factory_carries_its_own_flags() -> None:
    """What an extension's tool factory does: `Tool.from_function` plus its own options."""

    @dataclass
    class Row:
        id: str

    def run(ctx: Ctx, statement: str) -> Row:
        """Run."""
        return Row(id=statement)

    tool = Tool.from_function(run, name="run_sql", description="Run SQL.", transaction=False)

    assert (tool.name, tool.description, tool.transaction) == ("run_sql", "Run SQL.", False)
    assert tool.schema["properties"]["statement"]["type"] == "string"


def test_a_pydantic_failure_no_single_argument_reproduces_still_names_the_tool() -> None:
    """`_blame`'s fallback: every failure seen so far is one argument's, but not by construction."""
    assert _blame("get", {}, ValueError("something model-wide")) == "tool 'get'"


def test_a_schema_that_would_allow_unknown_arguments_is_refused() -> None:
    """The canary on `extra="forbid"`: pydantic emits `additionalProperties`, and it must be
    false. Nothing `from_function` builds can trip it; a change in pydantic could."""

    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")

        key: str

    with pytest.raises(WorldBug, match="allows unknown arguments"):
        _build_schema("get", Loose, {"key": (str, ...)})


def test_a_callable_without_a_name_needs_one_given() -> None:
    class Callable_:
        def __call__(self, ctx: Ctx, word: str) -> dict[str, str]:
            return {"word": word}

    factory_built = Callable_()

    assert "has no __name__" in refusal(factory_built)
    assert build(factory_built, name="echo").name == "echo"
