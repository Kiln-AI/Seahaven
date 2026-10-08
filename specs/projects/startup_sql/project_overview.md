---
status: complete
---

# Startup SQL

Let a caller using OpenEnv run setup on a world when an instance starts, without writing code. Today
the only per-episode setup is a startup hook, and a hook is code the world author writes: an eval
can pass a hook data through `reset(startup={...})`, but it cannot change the world in a way the
author did not write a hook for.

The caller sends SQL with `reset`, and the framework runs it against the new instance before the
first tool call. One served world then covers any starting state a fixture plus some SQL can
express, without a new fixture or a new hook for each scenario.

This is a framework feature that every world gets for free, not something each world opts in to.

## Design decision

Same machinery as startup hooks, new key. The SQL runs inside the startup step of instance creation
(same transaction, same failure handling, not in the change log), but it is a framework parameter,
`setup_sql`, beside `fixture` and `startup`, and not a startup keyword or a default
`instance_startup` hook. `startup` stays the world's own namespace.
