---
status: complete
---

# Phase 3: OpenEnv

## Overview

Phases 1 and 2 built the change log and the state document; in process `inst.state()` already
answers the whole `functional_spec.md` §3.1 document. This phase puts that document on the wire.
`SeahavenState` stops being the three-field placeholder it was and becomes the document plus
OpenEnv's `step_count`; `reset` gains `state_format=` and hands the session's episode id to the
instance, so a document produced over OpenEnv names the episode the harness named.

**This phase is the project's gate** (`implementation_plan.md`): a document that does not arrive
whole over the WebSocket makes everything above it pointless, so the confirmation test
`functional_spec.md` §9 asks for is written here, against a real server, before anything else is
built on it.

Nothing is removed. `Instance.changes()`, `Change` and `controller_changes` stay until Phase 4;
the docs are Phase 5's.

## Steps

1. **`src/seahaven/openenv/env.py` — `SeahavenState` becomes the document.**

   Two small models for the two nested envelope fields, each field described, because
   `SeahavenState`'s fields are described for the reason the class already gives:

   ```python
   class WorldRef(BaseModel):
       name: str
       version: str

   class FixtureRef(BaseModel):
       id: str
       file_sha256: str
   ```

   `SeahavenState(State)` declares every envelope field of FS §3.1, in FS §3.1 order:

   ```python
   format: str
   seahaven_version: str
   world: WorldRef
   fixture: FixtureRef | None = None
   seed: int | None = None
   now: str | None = None
   startup: dict[str, Any] | None = None
   call_count: int
   state: dict[str, Any]
   ```

   `episode_id` and `step_count` are the base's and are not redeclared; `extra="allow"` is
   inherited and kept. The four fields the document may answer `null` for carry a `None` default
   so that reading one off a frame that left it out does not raise; the five the framework always
   writes are required. `state` is `dict[str, Any]` and untyped beyond that, because its shape is
   the format's; the built-in shapes are pinned by `tests/support/state_v1.schema.json`, not by
   the model. The old `world: str`, `fixture: str` and `now: str` are replaced, which is the
   breaking change FS §9 accepts.

2. **`src/seahaven/openenv/env.py` — `reset` gains `state_format=` and names the episode.**

   ```python
   def reset(self, seed=None, episode_id=None, *, fixture=None, now=None,
             state_format=None, **startup_kwargs) -> SeahavenObservation:
   ```

   The episode id is minted before the instance (`episode_id or str(uuid.uuid4())`) and passed to
   creation, so the instance's own `episode_id` — the one its documents report — is the session's.
   `world.instance(...)` has no `episode_id` parameter by design (ARCH §6: in process an instance
   *is* an episode), so this one caller goes through the instance manager,
   `self.world._instances().create(...)`, with a comment saying why. `self._episode_id` is then
   read back off the instance rather than kept in parallel. `state_format` is keyword-only, so it
   can never fall into `**startup_kwargs` and reach a hook.

3. **`src/seahaven/openenv/env.py` — the `state` property answers the document.**

   With an instance: `SeahavenState(step_count=self._steps, **instance.state())`. Without one, the
   world's pinned formatter runs with `None` (FS §3.5):
   `state.document(self.world, None, pin, self.world.resolve_state_format(pin))` with
   `pin = self.world.pinned_state_format` (ARCH §9 spells this `world.state_format`, which Phase 2
   gave to the registration decorator). `document` is imported by name, as `instances.py` imports
   it, because the property is called `state`.

4. **`src/seahaven/openenv/client.py` — the docstring example.**

   `env.state().now` becomes a read of the document: `.state` is the formatter's output and
   `.model_dump(exclude={"step_count"})` is the document, which is the sentence FS §9 ends on.

5. **`worlds/projecttracker/tests/test_openenv.py`** — the one assertion outside the framework
   suite that reads `state.world` and `state.fixture` as strings, moved to the nested models.
   Minimal: the world suite has to stay green, and the docs are Phase 5's.

## Tests

`tests/test_env.py` (the `state` section rewritten):

- `test_state_before_reset_is_the_worlds_pinned_format_with_no_instance` — every envelope field of
  FS §3.5: `format` is the world's pin, `world` is named and versioned, `fixture`, `seed`, `now`,
  `startup` and `episode_id` are `None`, `call_count` and `step_count` are 0, `state` is
  `{"db": {"log": []}}`.
- `test_state_before_reset_runs_a_custom_pinned_formatter_with_no_instance` — a world pinned to a
  format it registers itself, whose formatter is handed `None`.
- `test_state_after_reset_is_the_instances_document_and_the_step_count` — `model_dump(exclude=
  {"step_count"})` equals `instance.state()` after an episode with a write in it.
- `test_state_after_reset_carries_the_fixture_and_the_clock` — the fixture's id and hash, the
  frozen instant, and the episode id `reset` was given.
- `test_reset_selects_a_state_format` — `reset(state_format="seahaven.state+last_step/1")` and the
  document holds the last call's records alone.
- `test_an_unknown_state_format_refuses_the_reset_and_leaves_the_session_fresh`.
- `test_a_second_reset_starts_a_new_log`.
- `test_a_startup_keyword_never_receives_the_state_format` — a hook taking `**kwargs` sees none.

`tests/test_client.py`:

- `test_parse_state_answers_a_typed_state` — a whole document frame, nested models included.
- `test_state_answers_none_for_the_fields_a_document_leaves_null` — pre-reset shape.
- `test_state_refuses_a_frame_that_does_not_name_a_world`.

`tests/test_server.py` (FS §9's confirmation step, against a real server):

- `test_the_whole_document_arrives_over_the_websocket` — every FS §3.1 field, with the values the
  in-process document has, and `model_dump(exclude={"step_count"})` equal to it.
- `test_the_stock_client_sees_the_same_document` — no Seahaven on the client side: the same keys
  and the same nested values as plain dictionaries.
- `test_reset_selects_a_state_format_over_the_wire`.
- the two existing end-to-end flows' state assertions, moved to the document.
