---
status: draft
---

# State Format

Design what `state()` returns: a really high-quality state system. This is what a client uses to
judge whether an episode was successful or not (we don't have internal rewards), so a lot rests on
this.

## What state carries

- **Database state**
  - A diff of all database state changes.
  - In a format that's excellent for consumers (see below).
  - P2: allow multiple formats.
- **Other environment state**
  - Tool counts: total, and total by tool name.
  - Tool calls: a list of tool calls and their params.
  - Tool runtime? No: we're mocked.
  - Step count.
  - Tool error counts.
- **Filtering**
  - Does `state` take options to filter, or always return everything? Things like tool counts are
    derivable if we return everything, so options only earn their place if they are needed for
    something else.
  - The tool-call list or the DB diff could be huge.
  - That said, filtering fights "stable and reusable" (below), so maybe we drop it.

## Stable and reusable

We want to design state to be pretty stable. Frameworks will run an episode and save `final_state`.
They may create new judges later that need to run against `final_state`, long after the episode
ran.

### Setup for versioning

This output is essential. It also will have to improve over time.

Can we call `state(format="v1")`, and set ourselves up to evolve the state? You can still keep
running existing evals against it using an older format key, but adopt newer, better ones for new
evals. Basically have a `StateFormatter`: a pluggable interface. Each World can specify its default
state format, for data consistency across Seahaven upgrades. Callers can request whatever they
want.

## Control tools vs state

We previously spec'ed control tools as the way an external judge can judge final state. We gave it
read-only SQL access (`controller_run_sql`) and the changeset (`controller_changes`).

This fails the "stable and reusable" bar above. We want `state` to replace them. We don't need to
pull them out right away, but we should update the docs to push `state()` as the primary surface.

Note: `src/seahaven/docs/openenv.md` is being added on another branch and will be in `main` soon.
It describes the OpenEnv surface, including `state`, and will conflict with this design. Updating
it for the new design is part of this project's docs work.

## DB format

This is a key design decision. Our design is that one world/fixture might have hundreds of
goals/judges in an external RL/eval system. Read the OpenEnv doc for some insights.

We could write a code judge for each, but that's expensive and error-prone. We could use an LLM
judge for each, but that's also expensive and error-prone.

We should ideate on a judge system that can work well in this world. Even though it's not part of
Seahaven, it needs to work on the `state()` output, so its design is coupled.

**Current thinking:** a judge that uses something like Jinja2 on the final state, paired with an
expected result.

- A list of expectations, each with a Jinja expression to evaluate on `final_state`, an expected
  value, and a comparison.
  - Example 1
    - jinja: select the tool count for the tool `update_issue_state`
    - expected value: 12
    - comparison: less_than
  - Example 2
    - jinja: select the number of changed records from the database, filtered to table `issues`
    - expected value: 24
    - comparison: equal

## Start wide

Write the project overview now. But before a functional spec that says the actual format, discuss
what the right format should be, thinking through the consumers/judges. JSON and Jinja? Does that
scale to DB diffs?

Prior art: a research phase before the functional spec, then bring back some ideas to discuss
before we build the functional spec.
