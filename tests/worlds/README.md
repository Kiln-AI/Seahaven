# Worlds the lints are run against

Four small packages, each a real world in the layout of functional spec §2.1, because the rules that
need a package -- import coverage, the world attribute, a `World` that refuses to be constructed --
cannot be exercised by an object built in a test. They are fixtures for `seahaven check`, not
examples to copy: `messy` and `broken_ddl` are wrong on purpose.

| World | What it is for |
|---|---|
| `tidy` | A world with no findings at all: the negative case for every rule |
| `messy` | A wall clock, `random`, `uuid.uuid4()`, an undescribed tool and an orphan module |
| `broken_ddl` | DDL SQLite refuses, so the `World` cannot be constructed (SH104) |
| `no_world` | A package with no `world` attribute (SH501) |

The DDL rules are driven with an inline `World` instead (`test_lint_ddl.py`): they need neither an
import nor a package, and a committed world per malformed table would be a directory of them.
