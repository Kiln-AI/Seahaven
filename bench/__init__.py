"""Seahaven's benchmark: two workloads over ProjectTracker `agency`, and a gate sweep.

Run by hand and before a release (`architecture.md` §10), never in CI and never
as a gate: nothing here fails a build, and no number here is a threshold. What it
is for is the two questions the framework cares about -- what a call costs, and
what the concurrency gate's size does to a process serving many sessions -- asked
the same way twice so that two runs on one machine can be compared.

Read the output the way the output asks to be read. A benchmark run on a shared
virtual machine measures that machine on that afternoon; `bench/results/latest.md`
opens with what its figures can and cannot be used for, and every table in it
carries the spread of its own repeats so a reader can refuse to believe a
difference smaller than the noise.

    uv run --no-sync python -m bench all --out bench/results/latest.md

`bench/README.md` has the rest of the commands and what they cost in wall time.
"""
