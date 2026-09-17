"""SQLite's own net diff of a whole episode: the oracle `functional_spec.md` §3.6 was defined from.

The fold is a definition, and a definition needs something outside itself to be
checked against. That something is the session extension asked the question the
fold asks: one session, open for the whole episode, whose changeset is the net
difference between where the instance started and where it ended.

The framework used to keep such a session for `Instance.changes()`, and the fold
test read it from there. It does not any more -- a session lives for one call now
-- so the test opens its own, on the instance's own connection and attached to
the same tables. Opened after the instance is made, so the startup hooks are
starting state here as they are in the log.

Test code, deliberately not importable from `seahaven`: neither the fold nor its
oracle is public API in this release.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import apsw

from seahaven.changes import open_session, render_log
from seahaven.instances import Instance
from tests.support.fold import NetChange

__all__ = ["Oracle", "recording"]


@dataclass(frozen=True)
class Oracle:
    """A session recording one instance, and the net diff it has to say."""

    instance: Instance
    session: apsw.Session

    def net_diff(self) -> list[NetChange]:
        """Every row the episode changed, once each, as the fold's own shape.

        Rendered with `render_log`, which is the framework's renderer and not
        part of what is under test: what is under test is the fold's arithmetic
        against SQLite's, and both sides have to be in one vocabulary to be
        compared at all. `i=None` because a cumulative changeset belongs to no
        call, and `NetChange` has no ordinal to carry it into.
        """
        conn = self.instance.db.conn
        changeset = self.session.changeset()
        records = render_log(changeset, conn, {}, i=None) if changeset else []
        return [
            NetChange(
                subworld=record.subworld,
                table=record.table,
                op=record.op,
                key=record.key,
                before=record.before,
                after=record.after,
            )
            for record in records
        ]


@contextmanager
def recording(instance: Instance) -> Iterator[Oracle]:
    """Record every write to `instance` for the length of the block."""
    session = open_session(instance.db.conn, instance._tracked)
    try:
        yield Oracle(instance=instance, session=session)
    finally:
        session.close()
