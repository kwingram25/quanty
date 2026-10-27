# Maintaining quanty

For whoever maintains quanty itself: releasing it, and running its public demo. Using quanty is
in [docs/](docs/).

## Releasing

quanty is published on PyPI as **quanty-py** (`pip install quanty-py`); it's imported and run as
`quanty`. The name `quanty` on PyPI belongs to an unrelated project. Releases are built and
published by `.github/workflows/release.yml` through trusted publishing: PyPI trusts that workflow,
so no API token is stored anywhere.

### Once: connect PyPI to this repository

1. Accounts on [pypi.org](https://pypi.org) and [test.pypi.org](https://test.pypi.org) (separate),
   with two-factor authentication.
2. On each, before the first release, add a **pending publisher** (Account → Publishing → Add a new
   pending publisher → GitHub): project `quanty-py`, owner `kwingram25`, repository `quanty`,
   workflow `release.yml`, environment `testpypi` on TestPyPI and `pypi` on PyPI.
3. In this repository's GitHub settings → Environments, create `testpypi` and `pypi`. Give `pypi` a
   required reviewer (you), so a stray tag can't publish without a click.

### A release

1. Set the version in `src/quanty/__init__.py` (`__version__ = "0.2.0"`; it's the only place) and
   note the changes in `CHANGELOG.md`. While at 0.x, a new minor version may break things: projects
   depend on `quanty-py>=0.1.0,<0.2`, so they move to 0.2 on purpose (edit the bound, then
   `quanty upgrade`).
2. Try it on TestPyPI first: Actions → release → Run workflow. It builds, checks the packages
   (`twine check --strict`), installs the wheel in a clean environment and runs a demo, then
   publishes to TestPyPI. Check it as a user would:

   ```sh
   uv venv /tmp/try && uv pip install --python /tmp/try \
     --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ quanty-py
   /tmp/try/bin/quanty demo --ui
   /tmp/try/bin/quanty init /tmp/try-project --yes    # depends on quanty-py from PyPI
   ```

3. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`. The workflow checks the tag matches
   `__version__`, builds and checks again, and publishes to PyPI once `pypi` is approved.

Versions can't be reused on PyPI: a broken release is fixed by the next version (and the bad one
yanked on PyPI), never by re-uploading.

### Before the first public release

Publishing makes the source public (a PyPI package ships it), the same step as making this
repository public. Before that:

- **README and getting started**: the install steps (today a `git clone`) become
  `pip install quanty-py` (or `uvx --from quanty-py quanty demo --ui`), with a line warning against
  the other `quanty`; image links become absolute
  (`https://raw.githubusercontent.com/kwingram25/quanty/main/docs/images/...`), since PyPI doesn't
  serve the repository's files.
- **A disclaimer** in the README and on PyPI: quanty places real-money orders; no warranty, not
  financial advice, use at your own risk (the MIT license says the legal part).
- **`CHANGELOG.md`**, starting at 0.1.0, and a note of what's stable at 0.x: the `Strategy` and `ctx`
  API, experiment files and the ledger schema change only with a minor version and a note.
- **The git history**, reviewed once more: it was started fresh, and gitleaks and the private-name
  guard run on every commit, but read it before the world does.
- **The repository made public**, so the PyPI page's links and the public demo's GitHub link work.

## The public demo

https://quanty-demo.fly.dev runs `quanty demo --live --public`, the demo built for hosting:
synthetic markets only, no keys and no account balances, a banner saying it's shared by everyone
viewing it, and every few hours (`--reset-hours`, 6 by default) it exits so the host starts a fresh
one. `demo.Dockerfile` and `fly.demo.toml` in this repo run it on Fly.io, where it stops when nobody
is visiting. To update it after a change:

```sh
fly apps create quanty-demo            # the name must be unique on Fly; change it in fly.demo.toml too
fly deploy --config fly.demo.toml --ha=false
```

`.dockerignore` lets only the source reach the builder, never `.env` or `data/`.
