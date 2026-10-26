# Strategies in a clone of quanty

Your strategies belong in a project of your own: `uv run quanty init ~/code/my-quanty` makes one, a
private git repository with a strategy skeleton, its experiment and the files to deploy it. See
[Getting started](../docs/getting-started.md#2-start-your-project).

This folder is for trying things in a clone of quanty itself. Everything here except this README
and `_template.py` is ignored by git, so it never shows in `git status` or conflicts with
`git pull`, but it isn't versioned or backed up either.

```sh
cp strategies/_template.py strategies/my_idea.py   # then edit the placeholders
```

Every `*.py` file here whose name doesn't start with `_` is a strategy, named after its file. A file
with the same name as a bundled example (`src/quanty/strategies/`) replaces it. Run quanty from the
repo root so this folder is found. A shared strategy library elsewhere can be listed in
`strategy_paths` in `~/.config/quanty/config.toml` (note that `quanty deploy` copies only the
project folder).
