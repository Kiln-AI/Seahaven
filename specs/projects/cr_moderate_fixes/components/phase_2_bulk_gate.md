# Component: Phase 2, the `bulk()` deadlock (2.M1)

A per-thread re-entrant gate. Prototyped on a scratch copy of the tree at `38c6f3c`, with scripts
that reproduce each hang below; line numbers are at that commit.

## 1. Defect, and one more case the fix covers

Rule broken: a thread that holds an instance lock queues for the gate. There are three confirmed
ways to do this on current `main`, and all three hang for good at `set_concurrency(1)`:

| Case | Current code | Prototype |
|---|---|---|
| A in `X.bulk()`; B calls `X.call` (gets the slot, then waits for X's lock); A calls `X.call` | hangs | ok |
| The same, but A calls `Y.call` inside `X.bulk()` | hangs | ok |
| **New:** a tool on X calls `Y.call(...)` on an instance it captured. **One thread** holds the slot and X's lock, then queues for a second slot. | hangs | ok |

(`World.instance` is refused inside a call; calling an existing instance is not.)

## 2. Change 1: `gate()` becomes re-entrant per thread (instances.py, replaces lines 213-230)

```py
# Whether this thread is inside a `gate()` block, whether or not that block took
# a slot. Per thread, not a ContextVar: Starlette's `run_in_threadpool` and
# `asyncio.to_thread` copy contextvars into the worker thread, and that worker
# holds none of the locks the flag stands for.
_gated = threading.local()

@contextmanager
def gate(*, bypass: bool = False) -> Iterator[None]:
    """...(existing text)... Re-entrant per thread: a block opened inside another
    takes nothing, because by then the thread may hold an instance lock."""
    if getattr(_gated, "active", False):
        yield
        return
    semaphore = None if bypass else _gate      # read once, as now
    if semaphore is not None:
        semaphore.acquire()
    _gated.active = True                       # set only after the acquire returns
    try:
        yield
    finally:
        _gated.active = False
        if semaphore is not None:
            semaphore.release()
```

Decisions:
- **A bool, not a depth counter or the semaphore.** Only the outermost block writes it, so it
  resets to `False` without a saved value. The acquired semaphore stays in the outer generator's
  local, as now. A mid-block `set_concurrency` therefore works: nested calls bypass whichever gate
  is in place, and the outer block releases the one it took.
- **Set even when no slot is taken** (`bypass=True`, or `_gate is None`). This covers (a) a
  `set_concurrency(1)` inside a block that started with no gate (test 6), and (b) a control tool
  (given the `Instance`) calling `live.call(...)` under the lock. Neither changes un-nested
  behaviour.
- An exception inside the block reaches the `finally`, and an interrupted `acquire()` leaves the
  flag unset.
- **Nothing moves a held slot to another thread.** Every `gate()` is a `with` inside one function
  (`_dispatch`, `_bulk`), so it is entered and exited on the same thread. OpenEnv runs
  `instance.call` inside one executor job for each session (`openenv/env.py:743`). The HTTP server
  runs `dispatch` inside one `run_in_threadpool` job (`http/server.py:182`, `http/runtime.py:279`).
  A `bulk()` entered on one thread and exited on another already fails at the RLock release.

`_dispatch` and `handles.py` do not change (a handle's activation is on the thread that opened it).

## 3. Change 2: `_bulk` enters the gate before the lock

```py
with gate(bypass=True), self._held() as frame, self._recording(None), ExitStack() as stack:
```

`_refuse_if_formatting()` stays first. In a nested `bulk()`, or `Y.bulk()` inside `X.bulk()`, the
inner `gate()` returns early and `_held()` takes the lock.

**Decision: `bypass=True` (variant B, "mark but do not take"), not a slot-taking `gate()`
(variant A).** Variant A was the first proposal; B is what the maintainer's requirement of no change
for default callers requires. The code shape is the one decided: the gate before the lock,
a thread-local, and `gate()` bypassing on a marked thread. The difference is one keyword. Both
variants remove all three hangs , and both pass every existing test
on 4 CPUs. Variant A fails the hard requirement "same behaviour when not nested":

1. **HTTP requests would become gated.** `http/runtime.dispatch` runs every request inside
   `instance.bulk()`. `docs/http_apis.md:264` says the concurrency gate does not apply there, and
   `:277` says requests to different instances run in parallel. `seahaven http` has no
   `--concurrency` flag and never calls `set_concurrency`. Under A, HTTP throughput would be
   limited to `min(cpus, 16)` on every `seahaven http` server, and it would also get the gate's
   documented starvation defect.
2. **A `bulk()` that stays open holds a slot while it is open.** The repo's own
   `tests/test_docs_examples.py::receivers` (session scope) yields from inside `inst.bulk()` on the
   main thread. On 1 CPU (`taskset -c 0`, default gate 1), A made `test_http_server.py::
   test_an_id_of_64_characters_is_taken_and_65_is_not` time out after test_docs_examples.py (it
   passes alone). B passes the same run (480 passed). 1-CPU CI and eval containers are common.
3. A harness loading data with `bulk()` while other threads call would queue behind them, unfairly.

What B does not do: it does not count bulk work, or calls made inside `bulk()`, against the bound.
Today the `bulk()` block is already outside the bound. Only the calls nested in it took slots, and
that is the source of the deadlock. The rule "the gate before the lock" does not require `bulk()` to
take a slot. The rule is that a thread holding a lock never queues, and B keeps it. A `destroy` or a
`freeze` still never waits on a queued call, because under B a `bulk()` never queues.


## 4. Other paths that hold the instance lock

| Path | Takes the gate? | Can it reach `gate()` under the lock? | Action |
|---|---|---|---|
| `_dispatch` (call) | yes, first | nested `inst.call` | covered by the flag |
| control tool | `bypass=True`, first | a control fn calling `live.call` | covered (flag set on bypass) |
| `_bulk` | now yes (marks) | `inst.call` inside the block | covered |
| `handle.call` | no | only inside an activation that is already marked, or inside creation | none |
| creation `_held` (startup hooks, `instances.py:1162`) | no | a hook calling some `Y.call` | none: the new instance is not published yet, so no other thread can wait on its lock |
| `state()` formatter | no | a formatter calling **another** instance's `call` (own instance is refused) | none: formatters are documented as read-only (see Decisions) |
| `freeze`, `change_log`, `call_log`, `inspect`, `_control_db`, `destroy` | no | no: no user code runs under the lock | none |

## 5. Text changes

- **Module docstring, rule 2**, rewritten: "*The gate before the lock, and never under it.* The
  concurrency gate bounds how many tool calls run at once across the process. A call takes it
  before the instance lock, so a call queued behind it holds nothing and can never delay a
  `destroy` or a `freeze`. The gate is re-entrant per thread: once a thread is inside a call or a
  `bulk()` block, nothing more it does takes the gate. That covers a nested call through a handle,
  an `inst.call(...)` inside `bulk()`, and a call on another instance from inside either, because
  by then the thread may hold an instance lock that a queued caller is waiting for. `bulk()`,
  instance creation and the control tool take no slot."
- The `gate()` docstring gets the last sentence from §2. `_bulk` gets a one-line comment: it marks
  the thread and takes no slot, because HTTP requests run in it and the gate does not apply to them
  (`http_apis.md`).
- `docs/serving_and_openenv.md` §"The concurrency gate", line 492-493: add "`inst.bulk()` bypasses
  it too, and so does any call made inside a call or a `bulk()` block on the same thread."
  `composition.md:320-321` and `http_apis.md` stay correct under B.

## 6. Tests (in `tests/test_instances.py`, after `test_the_gate_is_released_after_a_call_that_raised`)

Helper `TimedGate(instances._Gate)`: `acquire` waits at most `WAIT` and **raises** `AssertionError`
("queued for the gate while holding an instance lock") when the wait runs out. Its `acquire` also
counts `taken` and sets `contender_has_slot` when a thread named `contender` gets a slot. Install it
with `monkeypatch.setattr`. The timeout is required: on current code a deadlocked test blocks the
`with world.instance()` teardown in `destroy()` for ever, and hung pytest in the first prototype.
The autouse `_process_wide_runtime_state` already restores `_gate`.

**Run the body of every flag-checking test on a fresh `Caller` thread.** The `receivers` fixture
leaves the main thread marked; the first prototype passed alone and failed in the full suite.

1. `test_a_call_inside_bulk_does_not_queue_behind_a_caller_waiting_for_the_lock[same|other]`: the
   author thread enters `x.bulk()`. The `contender` thread calls `x.call` and gets the only slot.
   The author then calls `x.call` or `y.call`. Both `finish()`, and both rows are present.
2. `test_a_tool_calling_another_instance_does_not_wait_for_its_own_slot`: at gate 1, a tool on X
   calls captured `y.call`.
3. `test_nested_bulk_blocks_take_no_slot`: `x.bulk()` containing `x.bulk(), y.bulk()` with a call
   on each. `taken == 0`, and a later call gives `taken == 1`.
4. `test_bulk_that_raised_leaves_the_thread_gated_again`: raise inside `x.bulk()` after a call. The
   row is rolled back, `taken` stays 0, and the next call takes a slot (`taken == 1`), so the flag
   was cleared.
5. `test_bulk_does_not_wait_for_a_full_gate`: the no-change guard. A slot holder on X at gate 1.
   `y.bulk()` on another thread finishes within `WAIT/5`.
6. `test_a_resize_inside_bulk_does_not_make_its_calls_queue`: `set_concurrency(0)`, enter
   `x.bulk()`, install `TimedGate(1)`, occupy it from a thread calling Y, then `x.call` returns.
7. Optional: `test_a_control_tool_calling_its_instance_passes_a_full_gate`, which extends the
   `peek` control tool in `test_a_control_tool_passes_an_exhausted_gate`.

Results: the prototype passes all of these. On current code 6 of the 7 fail, each within its
timeout. Test 5 passes on current code on purpose. Mutation checks:
- without the flag check in `gate()`, tests 1, 2, 3, 4 and 6 fail;
- without `gate()` in `_bulk`, tests 1, 3, 4 and 6 fail;
- without the reset in `finally`, tests 3 and 4 fail.

Full framework suite with the patch: 2393 passed, 1 failed (`test_typed_call::test_ty_resolves...`,
which also fails unpatched in the scratch copy). The reference world and xmlrpc pass on 1 CPU.

## 7. Decisions and known limits

- **`state()` does not enter the gate.** A formatter that calls another instance could still queue
  while it holds X's lock, but formatters are documented as read-only. Not changed.
- **The `receivers` fixture** in `tests/test_docs_examples.py` keeps a `bulk()` open on the main
  thread for the session, as it does today. Under variant B this only means main-thread tests are
  not gated; tests that check the flag run their bodies on a fresh thread (§6).
- **Out of scope:** two instances can still deadlock on lock order (A in `X.bulk()` calls Y while B
  in `Y.bulk()` calls X). This exists today and does not involve the gate.
- **HTTP** (`src/seahaven/http/`) is unchanged: requests run inside `bulk()`, which marks the thread
  and takes no slot, exactly as the gate does not apply to them today.
