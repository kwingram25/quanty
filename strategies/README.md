# Your strategies

Everything in this folder except this README and `_template.py` is ignored by git, so your
strategies never show up in `git status`, never conflict with `git pull`, and aren't published by
a fork.

```sh
cp strategies/_template.py strategies/my_idea.py   # then edit the placeholders
```

Every `*.py` file here whose name doesn't start with `_` is a strategy, named after its file. A
file with the same name as a bundled example (`src/quanty/strategies/`) replaces it. Run quanty
from the repo root so this folder is found.

Ignored also means unversioned and not backed up. To version your strategies, keep them in a
private repo of your own and either clone it into a subfolder here:

```sh
git clone git@github.com:you/my-strategies.git strategies/private
```

and its top-level `*.py` files are found too, or list its path in `strategy_paths` in
`~/.config/quanty/config.toml`.
