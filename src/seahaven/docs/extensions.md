# Extensions

The extension contract: an extension is an ordinary package that depends on `seahaven` and uses
its public API. It never monkeypatches the framework and never registers itself -- the world
registers what it wants. The XML-RPC example is the worked case.

The example is in the Seahaven repository at `extensions/seahaven-xmlrpc`: one tool factory, one
fault-mapping middleware, a DDL string and a startup hook, with its own README and a test suite that
drives it over a copy of ProjectTracker.

**This page is a stub.** Its prose is written in the documentation phase of Seahaven's
implementation plan. Until then: read `functional_spec.md` §21 in the Seahaven repository.
