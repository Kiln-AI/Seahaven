# Concepts

A world is a declaration; a fixture is a frozen database; an instance is a private copy of one that
a run of an eval drives. Time comes from the instance's clock and ids from its seeded stream, which
is what makes a replay of the same fixture and seed the same run. A changeset is what the instance
recorded of what the agent did to it, and is what an eval is scored on.

**This page is a stub.** Its prose is written in the documentation phase of Seahaven's
implementation plan. Until then: read `functional_spec.md` §§3-7 in the Seahaven repository.
