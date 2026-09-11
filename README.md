# Seahaven
A framework for building synthetic worlds/environments for AI agents. Useful for RL and evaluations.

## Status

Early development, and nothing here is stable yet. The runtime database layer exists today:
`Db`, `Clock`, `Ids`, the error hierarchy and the `seahaven.sandbox` module for running SQL an
agent wrote. The world, tool and instance API is being built on top of it; the specification is
in `specs/projects/seahaven_framework/`.

## Installation

```bash
pip install seahaven
```
