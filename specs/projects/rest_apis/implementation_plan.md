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
- [ ] Phase 3: `command.py` (`main`), its tests and the process test, then the docs page, the API
      reference section and the links (architecture §6, §8.5, §8.7, §9)
