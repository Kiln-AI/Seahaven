---
status: draft
---

# Implementation Plan: REST APIs

## Phases

- [ ] Phase 1: `messages.py` and `runtime.py` (types, `Registry`, `dispatch`), the test world
      `tests/http_world.py`, and their tests, including the tool path through
      `world.instance(...)` (architecture §3, §4, §8.1–8.3, §8.6)
- [ ] Phase 2: `server.py` and the `__init__.py` wrappers (`app`, `serve`), with the real-HTTP
      tests (architecture §2.1, §5, §8.4)
- [ ] Phase 3: the `seahaven/cli/mcp.py` split, `command.py` (`main`), its tests and the process
      test (architecture §6, §8.5)
- [ ] Phase 4: the docs page `http_apis.md`, the `reference/api.md` section, the links from
      `index.md` and `serving_and_openenv.md`, and `tests/test_docs.py` (architecture §8.7, §9)
