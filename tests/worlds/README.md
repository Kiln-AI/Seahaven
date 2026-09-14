# Worlds the lints and composition are run against

Seven small packages, each a real world in the layout of functional spec §2.1, because the rules
that need a package -- import coverage, the world attribute, a `World` that refuses to be
constructed, one world adding another -- cannot be exercised by an object built in a test. They are
fixtures, not examples to copy: `messy` and `broken_ddl` are wrong on purpose.

| World | What it is for |
|---|---|
| `tidy` | A world with no findings at all: the negative case for every rule |
| `messy` | A wall clock, `random`, `uuid.uuid4()`, an undescribed tool and an orphan module |
| `broken_ddl` | DDL SQLite refuses, so the `World` cannot be constructed (SH104) |
| `no_world` | A package with no `world` attribute (SH501) |
| `payments` | The leaf of the composite: two tools, one table, one bindable startup keyword |
| `shop` | Adds `payments` with an empty allow list, and has one tool of its own |
| `emporium` | The composite host: `payments`, a second `payments` account, and `shop` |

The last three are one tree, and the tree the composition tests are written against: `emporium`
resolves to the four nodes `main`, `payments`, `payments_eu` and `shop`, with `shop/payments` an
alias of `payments` rather than a fifth store. `tests/conftest.py` puts their `src` directories on
`sys.path`, because a host adds another world by importing it.

The DDL rules are driven with an inline `World` instead (`test_lint_ddl.py`): they need neither an
import nor a package, and a committed world per malformed table would be a directory of them.
