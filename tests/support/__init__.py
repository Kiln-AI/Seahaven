"""Test-only helpers that are deliberately not part of `seahaven`.

What lives here is a *reader's* half of a format the framework produces: the
fold of `functional_spec.md` §3.6 and the oracle it is checked against. Keeping
them out of the package is the point -- the spec says the net diff is a fold of
the log and that nobody computes one in production, so the fold exists here, as
the reference implementation a consumer would write.
"""
