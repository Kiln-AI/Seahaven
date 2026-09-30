"""A registered tool: the function, the argument model built from its signature, and its schema.

A world author writes a plain Python function with typed keyword parameters; the
framework derives the argument model and the JSON schema from the signature, so
the tool list an agent reads and the validation a call passes cannot drift apart.
Everything that can be wrong with a tool is found here, at registration, with a
message naming the tool and the parameter.

`Tool` and `Tool.from_function` are public: a tool factory -- `seahaven.helpers`,
or an extension's -- builds its tool with them and hands the result to
`world.tool(...)`.
"""

import dataclasses
import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import (
    Any,
    Concatenate,
    Literal,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

import pydantic
import pydantic_core
from pydantic import ConfigDict, create_model

from seahaven.ctx import Ctx
from seahaven.errors import ArgumentError, WorldBug

__all__ = ["Tool"]

# Strict, so the tool list is the contract: `"5"` is not an int and `1` is not a
# bool. A world mimicking a product that coerces relaxes one argument with
# `Annotated[int, Field(strict=False)]`, which works because strictness lives in
# the config: a `strict=` passed to `model_validate` or `model_validate_json`
# would override every field's own setting.
_ARGUMENT_CONFIG = ConfigDict(extra="forbid", strict=True)

_POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
_VARIADIC = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)

# The scalars JSON carries. A `Literal` value must be one for JSON to send it: a
# `StrEnum` or `IntEnum` member is, and equal to its own value, so pydantic
# accepts that value.
_JSON_SCALARS = (str, int, float, bool, type(None))

# Defaults Python itself warns about: a shared mutable bound to the function.
# Refused here for a second reason, that the schema publishes `default` and a
# value the tool can edit makes that publication a lie.
_MUTABLE_DEFAULTS = (list, dict, set, bytearray)


@dataclass(frozen=True, eq=False)
class Tool[**P = ..., R = Any]:
    """One registered tool.

    Hashable by name, which is what the registry keys on, and a tool's name is
    unique in a world by construction. Equality is identity: a `Tool` carries a
    function, a model class and a schema dict, and there is no useful sense in
    which two separately built ones are the same tool.

    Generic in the function's own parameters and return type, so a tool built by
    a factory carries what a tool built by the decorator carries: the decorator
    hands the function itself back and `inst.call(fn, ...)` reads its signature,
    while a factory hands back a `Tool` and this is where those types survive.
    Annotation-only -- nothing in the runtime reads `P` or `R` -- and both
    parameters default, so a bare `Tool` is the annotation it always was.
    """

    name: str
    description: str
    fn: Callable[Concatenate[Ctx[Any], P], R]
    params: type[pydantic.BaseModel]
    schema: dict[str, Any]
    transaction: bool = True
    # The framework's own flag, for `controller_run_sql`: a control tool bypasses
    # the middleware chain, the transaction and the gate, and is never listed.
    # `from_function` cannot set it.
    control: bool = False

    def __hash__(self) -> int:
        return hash(self.name)

    @classmethod
    def from_function[**Q, S](
        cls,
        fn: Callable[Concatenate[Ctx[Any], Q], S],
        *,
        name: str | None = None,
        description: str | None = None,
        transaction: bool = True,
    ) -> Tool[Q, S]:
        """Build a tool from a function, refusing anything that cannot be one.

        `Tool(...)`, not `cls(...)`: the function's parameters and result have to
        reach the returned type, and `Self` cannot carry them. Nothing subclasses
        `Tool`, and the class is public to be *built* from rather than derived.
        """
        tool_name = name or getattr(fn, "__name__", "")
        if not tool_name:
            raise WorldBug(f"a tool needs a name: {fn!r} has no __name__, so pass name=")
        _refuse_unsupported_callable(tool_name, fn)

        parameters = list(_signature(tool_name, fn).parameters.values())
        if not parameters:
            raise WorldBug(f"tool {tool_name!r} takes the context as its first parameter")
        _check_context_parameter(tool_name, parameters[0])
        fields = {p.name: _field(tool_name, p) for p in parameters[1:]}

        params = _build_model(tool_name, fields)
        return Tool(
            name=tool_name,
            description=description if description is not None else (inspect.getdoc(fn) or ""),
            fn=fn,
            params=params,
            schema=_build_schema(tool_name, params, fields),
            transaction=transaction,
        )

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The arguments as the function's parameters, or `ArgumentError`.

        Strict either way, in one of two modes chosen for the whole call. A call
        made of plain JSON values at every depth -- which is all the wire can hand
        over -- is validated in pydantic's JSON mode, which accepts the JSON form
        of every type the published schema describes: an array for a `tuple`, a
        string for a `UUID` or a `datetime`. Strict *Python* mode would demand the
        real `tuple` or `UUID`, which parsed JSON never holds. A call holding any
        other Python object is validated in strict Python mode, so a `datetime`
        handed to a `str` parameter is refused rather than rendered to text.

        Every violation is reported, not just the first: an agent that has to
        make one round trip per mistake makes several.
        """
        given = dict(arguments)
        text = _json_text(given)
        try:
            if text is None:
                model = self.params.model_validate(given)
            else:
                model = self.params.model_validate_json(text)
        except pydantic.ValidationError as error:
            raise _argument_error(self.name, error) from error
        # By field name, not by alias: the wire carries the alias and the
        # function is called with the name Python can spell.
        return {name: getattr(model, name) for name in type(model).model_fields}

    def arguments(self, positional: Sequence[Any], keywords: Mapping[str, Any]) -> dict[str, Any]:
        """This call's arguments spelled the way Python spells them, as the wire spells them.

        The typed call path names arguments by the function's own *parameters*,
        and its `ParamSpec` (`*args: P.args, **kwargs: P.kwargs`) makes a
        parameter the function declares positionally legal to pass positionally --
        so a call that type-checks has to run. Validation, on the other side of
        this, takes the names the tool list publishes, and the two differ wherever
        an argument declares an alias: the agent sends `from` and the function
        receives `from_`. Positional arguments therefore bind in the argument
        model's field order -- the signature's order after the context -- and
        every name is then translated.

        A `WorldBug` either way it can go wrong: only in-process host code can
        reach this, and both mistakes are ones a type checker has already refused.
        """
        names = list(self.params.model_fields)
        if len(positional) > len(names):
            raise WorldBug(
                f"tool {self.name!r} takes {len(names)} argument(s) and was passed "
                f"{len(positional)} positionally"
            )
        twice = sorted(set(names[: len(positional)]) & set(keywords))
        if twice:
            raise WorldBug(
                f"tool {self.name!r}: {', '.join(twice)} given both positionally and by name"
            )
        wire = self._wire_names(names)
        given = {**dict(zip(names, positional, strict=False)), **keywords}
        # A name that is not a parameter at all passes through under its own
        # spelling, for `validate` to refuse in the words it refuses an agent's.
        return {wire.get(name, name): value for name, value in given.items()}

    def _wire_names(self, names: Sequence[str]) -> dict[str, str]:
        """Each parameter's name to the name a call sends it under.

        Read from the published schema, whose properties are the argument model's
        fields in order, rather than from each `FieldInfo`: the tool list is the
        contract, so whatever the list says an argument is called is what a typed
        call sends it as -- an `alias`, a `validation_alias` and the first of an
        `AliasChoices` alike, with no rule here to keep in step with pydantic's.

        A schema that does not describe these fields one for one is not one this
        can read, and every parameter then keeps its own name, which is what it
        had before aliases were translated at all. That is a hand-built tool, or
        one whose aliases collide -- two parameters publishing under one name
        collapse `properties` to fewer entries than there are fields -- and such
        a tool has already published a schema an argument is missing from, so
        the identity fallback is no worse than the list it came from.
        """
        published = tuple(self.schema.get("properties", ()))
        if len(published) != len(names):
            return {}
        return dict(zip(names, published, strict=True))

    def listing(self) -> dict[str, Any]:
        """The tool as a tool list carries it, which is OpenEnv's own `Tool` shape."""
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def _json_text(arguments: dict[str, Any]) -> bytes | None:
    """The arguments as JSON text, or `None` when they are not plain JSON.

    Plain JSON can still have no UTF-8 text: `json.loads` keeps a lone surrogate
    such as `"\\ud800"`, which an HTTP body can carry. Strict Python validation
    takes a `str` as it is, so that call is validated as Python.
    """
    if not _is_json(arguments):
        return None
    try:
        return pydantic_core.to_json(arguments)
    except pydantic_core.PydanticSerializationError:
        return None


def _is_json(value: Any) -> bool:
    """Whether a value is plain parsed JSON at every depth.

    By exact type: a `StrEnum` member is a `str` and a `NamedTuple` a `tuple`, and
    each is a Python object its caller chose over the JSON form.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is list:
            pending.extend(item)
        elif type(item) is dict:
            if not all(type(key) is str for key in item):
                return False
            pending.extend(item.values())
        elif type(item) not in _JSON_SCALARS:
            return False
    return True


def _argument_error(tool_name: str, error: pydantic.ValidationError) -> ArgumentError:
    return ArgumentError(
        tool=tool_name,
        violations=[
            {
                "path": ".".join(str(part) for part in violation["loc"]),
                "message": violation["msg"],
                "type": violation["type"],
            }
            for violation in error.errors(include_url=False)
        ],
    )


def _refuse_unsupported_callable(tool_name: str, fn: Callable[..., Any]) -> None:
    if (
        inspect.iscoroutinefunction(fn)
        or inspect.isgeneratorfunction(fn)
        or inspect.isasyncgenfunction(fn)
    ):
        raise WorldBug(
            f"tool {tool_name!r} must be a plain synchronous function: an async def, a "
            f"generator and an async generator cannot be one"
        )


def _signature(tool_name: str, fn: Callable[..., Any]) -> inspect.Signature:
    try:
        # Annotations are evaluated here rather than left as strings, so a
        # `Literal` or a nested model reaches pydantic as the object it names.
        return inspect.signature(fn, eval_str=True)
    except NameError as error:
        raise WorldBug(
            f"tool {tool_name!r} is annotated with {error.name!r}, which does not exist at "
            f"runtime: an annotation imported only under TYPE_CHECKING cannot be a tool argument"
        ) from error


def _check_context_parameter(tool_name: str, parameter: inspect.Parameter) -> None:
    if parameter.kind not in _POSITIONAL or not _is_ctx(parameter.annotation):
        raise WorldBug(
            f"tool {tool_name!r}: the first parameter is the context, taken positionally and "
            f"annotated `Ctx`, `Ctx[X]` or not annotated at all; {parameter.name!r} is neither"
        )


def _is_ctx(annotation: Any) -> bool:
    """Whether an annotation is the context: bare, parameterised, or absent.

    `Ctx[CompanyWorlds]` is a world declaring the children it adds to a type
    checker (architecture section 8.3), and it is the same object at run time as
    a bare `Ctx`, so registration has no reason to know the difference.
    """
    return (
        annotation is inspect.Parameter.empty or annotation is Ctx or get_origin(annotation) is Ctx
    )


def _field(tool_name: str, parameter: inspect.Parameter) -> tuple[Any, Any]:
    if parameter.kind in _VARIADIC:
        raise WorldBug(
            f"tool {tool_name!r} takes named arguments only; *{parameter.name} cannot be one"
        )
    if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
        # Every argument is passed by name when the tool is called, so a
        # positional-only parameter would register cleanly and then fail on the
        # first call. Found here instead, which is the whole point of this module.
        raise WorldBug(
            f"tool {tool_name!r}: argument {parameter.name!r} is positional-only, and a tool's "
            f"arguments are passed by name; move it after the `/`"
        )
    if parameter.annotation is inspect.Parameter.empty:
        raise WorldBug(f"tool {tool_name!r}: argument {parameter.name!r} needs a type annotation")
    _refuse_unfaithful_annotation(tool_name, parameter)
    if isinstance(parameter.default, _MUTABLE_DEFAULTS):
        raise WorldBug(
            f"tool {tool_name!r}: argument {parameter.name!r} has a mutable default; use `None` "
            f"and default inside the tool"
        )
    default = ... if parameter.default is inspect.Parameter.empty else parameter.default
    return (parameter.annotation, default)


def _refuse_unfaithful_annotation(tool_name: str, parameter: inspect.Parameter) -> None:
    """Refuse an annotation whose validation would not match the schema it publishes.

    Two shapes do, anywhere in the annotation or in the fields of a type it
    reaches. A `Literal` of a value JSON cannot spell -- a plain `Enum` member, or
    `bytes` -- publishes `"const": "red"` and then accepts only the member, which
    is less than the schema says.

    And a type with a pydantic config of its own, which can accept more: a
    `BaseModel`, a pydantic dataclass, or anything given `@with_config`. The
    argument model's strict config reaches a nested stdlib dataclass or
    `TypedDict`, but such a type validates under its own config, which is lax
    unless it says otherwise, so `"5"` would be an `int` one level down. It must
    say `strict=` itself, `True` to follow the tool list or `False` to relax
    deliberately.
    """
    for atom in _reachable(parameter.annotation):
        if get_origin(atom) is Literal:
            for value in get_args(atom):
                if not isinstance(value, _JSON_SCALARS):
                    raise WorldBug(
                        f"tool {tool_name!r}: argument {parameter.name!r} is a `Literal` of "
                        f"{value!r}, which JSON cannot send; use a `Literal` of strings or "
                        f"numbers, or the `Enum` class itself"
                    )
        if not isinstance(atom, type):
            continue
        config = _own_config(atom)
        if config is not None and "strict" not in config:
            raise WorldBug(
                f"tool {tool_name!r}: argument {parameter.name!r} uses {atom.__name__}, which "
                f"has a pydantic config of its own that does not set `strict`; add "
                f"`strict=True` to it, or `strict=False` to accept lax input on purpose"
            )


def _own_config(atom: type) -> Mapping[str, Any] | None:
    if issubclass(atom, pydantic.BaseModel):
        return atom.model_config
    return getattr(atom, "__pydantic_config__", None)


def _reachable(annotation: Any) -> Iterator[Any]:
    """Every atom of an annotation, and of each model, dataclass and `TypedDict` it reaches."""
    seen: set[int] = set()
    # `seen` holds ids, and an id is unique only while its object is alive.
    walked: list[Any] = []
    pending = [annotation]
    while pending:
        walked.append(pending.pop())
        for atom in _atoms(walked[-1], seen):
            yield atom
            if isinstance(atom, type):
                pending.extend(_field_annotations(atom))


def _field_annotations(atom: type) -> list[Any]:
    if issubclass(atom, pydantic.BaseModel):
        return [field.annotation for field in atom.model_fields.values()]
    if not (dataclasses.is_dataclass(atom) or is_typeddict(atom)):
        return []
    try:
        return list(get_type_hints(atom).values())
    except Exception:
        # pydantic resolved these names from the frame the type was defined in,
        # which `get_type_hints` cannot see; the type is not looked into.
        return []


def _atoms(annotation: Any, seen: set[int] | None = None) -> Iterator[Any]:
    """The annotation and everything inside it.

    Looks through unions, `Annotated` metadata and containers, so `Point | None`
    and `list[Point]` reach `Point`. A name for a type is looked through too:
    `type Where = Point` (PEP 695) and `NewType("Where", Point)` both carry the
    annotation they stand for.

    `seen` guards the shapes that could otherwise not end: an alias, or a model
    walked by `_reachable`, defined in terms of itself.
    """
    seen = set() if seen is None else seen
    if id(annotation) in seen:
        return
    seen.add(id(annotation))
    yield annotation
    for attribute in ("__value__", "__supertype__"):
        try:
            carried = getattr(annotation, attribute, None)
        except NameError:  # an alias whose own annotation does not resolve
            carried = None
        if carried is not None:
            yield from _atoms(carried, seen)
    for argument in get_args(annotation):
        yield from _atoms(argument, seen)


def _build_model(tool_name: str, fields: dict[str, tuple[Any, Any]]) -> type[pydantic.BaseModel]:
    try:
        # ty cannot see that the field names unpacked here are never pydantic's
        # own `__config__` and friends, so the overload looks ambiguous to it.
        return create_model(  # ty: ignore[no-matching-overload]
            f"{tool_name}Arguments", __config__=_ARGUMENT_CONFIG, **fields
        )
    except Exception as error:
        raise WorldBug(f"{_blame(tool_name, fields, error)}: {error}") from error


def _blame(tool_name: str, fields: dict[str, tuple[Any, Any]], error: Exception) -> str:
    """Which argument pydantic refused, found by trying them one at a time.

    An annotation pydantic cannot build a schema for, or a name that shadows a
    `BaseModel` attribute, arrives as one error about the whole model. Rebuilding
    a field at a time is only ever done on the way to raising, so the cost of
    naming the argument is paid by the author who has a mistake to find.
    """
    for name, definition in fields.items():
        try:
            # Built *and* asked for its schema: pydantic defers some failures to
            # the schema, and both callers want the same answer to "which one?".
            create_model(  # ty: ignore[no-matching-overload]
                f"{tool_name}Argument", __config__=_ARGUMENT_CONFIG, **{name: definition}
            ).model_json_schema(mode="validation")
        except Exception:
            return f"tool {tool_name!r}, argument {name!r}"
    return f"tool {tool_name!r}"


def _build_schema(
    tool_name: str, params: type[pydantic.BaseModel], fields: dict[str, tuple[Any, Any]]
) -> dict[str, Any]:
    """The tool's JSON schema: the argument model's, with two adjustments.

    The top-level `title` is pydantic's model name (`get_issueArguments`), which
    is an implementation detail of this module and has no business on the wire.
    `$defs` are kept: nested models are ordinary, and tool list consumers resolve
    them. The description is not repeated here -- `listing()` carries it beside
    the schema, which is the shape OpenEnv consumes.

    An annotation pydantic accepted but cannot finish is found here rather than
    when the model was built: a `type Price = Decimal` naming a symbol that only
    exists under `TYPE_CHECKING` resolves to the alias object at signature time
    and fails when pydantic tries to look through it.
    """
    try:
        schema = params.model_json_schema(mode="validation")
    except Exception as error:
        raise WorldBug(f"{_blame(tool_name, fields, error)}: no JSON schema: {error}") from error
    schema.pop("title", None)
    # pydantic emits this for `extra="forbid"`. Set explicitly so the contract
    # holds whatever pydantic does, and refuse a schema that says otherwise.
    if schema.setdefault("additionalProperties", False) is not False:
        raise WorldBug(f"tool {tool_name!r}: the argument schema allows unknown arguments")
    return schema
