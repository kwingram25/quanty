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
3. In this repository's GitHub settings → Environments, `testpypi` and `pypi` (both exist). `pypi`
   accepts only `v*` tags. Once the repository is public, also give `pypi` a required reviewer (you),
   so a stray tag can't publish without a click (private repositories need a paid plan for that).

### A release

1. Set the version in `src/quanty/__init__.py` (`__version__ = "0.2.0"`; it's the only place) and
   note the changes in `CHANGELOG.md` under that version, dated the day it's tagged (the first,
   0.1.0, waits there as "unreleased"). While at 0.x, a new minor version may break things: projects
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
