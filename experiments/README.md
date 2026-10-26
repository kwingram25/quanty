# Experiments

An experiment is a TOML file: which strategies run, with which params, on which broker, under which
limits. `example.toml` is one to start from; see
[Getting started](../docs/getting-started.md#3-run-an-experiment) for the format.

Your own experiments belong in a project of your own (`quanty init` makes one, with
`experiments/main.toml`), not in this clone: here everything but this README and `example.toml` is
ignored by git.
