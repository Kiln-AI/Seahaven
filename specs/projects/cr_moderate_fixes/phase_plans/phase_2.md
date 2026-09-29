---
status: complete
---

# Phase 2: The `bulk()` deadlock (2.M1)

## Overview

An `inst.call(...)` made inside `with inst.bulk():` queues for the process-wide concurrency gate
while its thread holds the instance lock, so a second thread holding the last slot and waiting for
that lock deadlocks both. A tool that calls another instance at a gate of 1 hangs one thread the
same way. This phase makes the gate re-entrant per thread and has `bulk()` mark the thread without
taking a slot, per [components/phase_2_bulk_gate.md](../components/phase_2_bulk_gate.md) (variant
B). No public signature changes, and HTTP requests (which run inside `bulk()`) stay ungated.

## Steps

1. `src/seahaven/instances.py`, beside `gate()`: a module-level `_gated = threading.local()`, with
   a comment giving the external constraint (Starlette's `run_in_threadpool` and
   `asyncio.to_thread` copy context variables into worker threads, so a `ContextVar` would mark a
   thread that holds none of the locks).
2. Rewrite `gate(*, bypass: bool = False)`:
   - return straight through when `_gated.active` is already set on this thread;
   - otherwise read `_gate` once (`None` when `bypass`), acquire it if there is one, then set
     `_gated.active = True`; in `finally` clear the flag, then release the semaphore it took.
   - Docstring gains: "Re-entrant per thread: a block opened inside another takes nothing, because
     by then the thread may hold an instance lock."
3. `Instance._bulk`: after `_refuse_if_formatting()`, enter `gate(bypass=True)` before
   `self._held()` in the same `with`. A one-line comment: it marks the thread and takes no slot,
   because HTTP requests run inside `bulk()` and the gate does not apply to them
   (`docs/http_apis.md`).
4. Module docstring, rule 2 ("The gate before the lock"): rewrite to the text in component doc §5.
5. `src/seahaven/docs/serving_and_openenv.md`, "The concurrency gate": after "Instance creation,
   tool listing and the control tool bypass it entirely." add "`inst.bulk()` bypasses it too, and
   so does any call made inside a call or a `bulk()` block on the same thread."

## Tests

All in `tests/test_instances.py`, after `test_the_gate_is_released_after_a_call_that_raised`. A
helper `TimedGate(instances._Gate)` whose `acquire` waits at most `WAIT` and raises
`AssertionError` when it runs out, counts `taken`, and sets a `contender_has_slot` event when a
thread named `contender` takes a slot; installed with `monkeypatch.setattr(instances, "_gate",
...)`. Each flag-checking body runs on a fresh `Caller` thread, because the docs-examples
`receivers` fixture leaves the main thread inside a `bulk()`.

- `test_a_call_inside_bulk_does_not_queue_behind_a_caller_waiting_for_the_lock[same|other]`: the
  author thread enters `x.bulk()`; a `contender` thread calls `x.call` and takes the only slot,
  then waits for X's lock; the author calls `x.call` (or `y.call`). Both threads finish, and both
  rows are present.
- `test_a_tool_calling_another_instance_does_not_wait_for_its_own_slot`: at gate 1, a tool on X
  calls a captured `y.call`; the call returns and Y has the row.
- `test_nested_bulk_blocks_take_no_slot`: `x.bulk()` containing `x.bulk()` and `y.bulk()` with a
  call in each: `taken == 0`; a later call gives `taken == 1`.
- `test_bulk_that_raised_leaves_the_thread_gated_again`: a call then a raise inside `x.bulk()`: the
  row is rolled back, `taken == 0`, and the next call gives `taken == 1`.
- `test_bulk_does_not_wait_for_a_full_gate`: a slot holder on X at gate 1; `y.bulk()` with a write
  on another thread finishes within `WAIT / 5` (passes on today's code on purpose: the no-change
  guard).
- `test_a_resize_inside_bulk_does_not_make_its_calls_queue`: `set_concurrency(0)`, enter
  `x.bulk()`, install `TimedGate(1)`, occupy its slot from a thread calling Y, then `x.call`
  returns.
- `test_a_control_tool_calling_its_instance_passes_a_full_gate`: a control tool calls
  `live.call(...)` on its own instance while another thread holds the only slot; it returns
  within `WAIT / 5`.

Confirm the component doc's mutations: without the flag check in `gate()`, without `gate()` in
`_bulk`, and without the reset in `finally`, the named tests fail.
