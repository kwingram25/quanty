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

1. Every release is the next minor version (0.3.0, then 0.4.0; no patch releases). Set it in
   `src/quanty/__init__.py` (`__version__ = "0.4.0"`; it's the only place) and move `CHANGELOG.md`'s
   "Unreleased" notes under it, dated the day it's tagged, with what a project does to upgrade.
   Projects depend on one minor version (`quanty-py>=0.3.0,<0.4`), so they move on purpose (edit
   the bound, then `quanty upgrade`).
2. Try it on TestPyPI first: Actions → release → Run workflow. It builds, checks the packages
   (`twine check --strict`), installs the wheel in a clean environment and runs a demo, then
   publishes to TestPyPI. Check it as a user would:

   ```sh
   v=0.4.0   # the version being released
   whl=$(curl -s https://test.pypi.org/simple/quanty-py/ | grep -oE "https://[^\"#]*quanty_py-$v-py3-none-any.whl" | head -1)
   uv venv /tmp/try && uv pip install --python /tmp/try "quanty-py @ $whl"   # its dependencies from PyPI
   /tmp/try/bin/quanty demo --ui
   ```

   The wheel by its URL: with quanty-py on PyPI, uv takes it only from there (it won't mix
   indexes for one package), and TestPyPI's own copies of other packages can be junk.

3. Tag and push: `git tag v0.4.0 && git push origin v0.4.0`. The workflow checks the tag matches
   `__version__`, builds and checks again, and publishes to PyPI once `pypi` is approved.

Versions can't be reused on PyPI: a broken release is fixed by the next version (and the bad one
yanked on PyPI), never by re-uploading.

## The public demo

https://quanty-demo.fly.dev runs `quanty demo --live --public`, the demo built for hosting:
synthetic markets only, no keys and no account balances, a banner saying it's shared by everyone
viewing it, and every few hours (`--reset-hours`, 6 by default) it exits so the host starts a fresh
one. `demo.Dockerfile` and `fly.demo.toml` in this repo run it on Fly.io, on one shared-CPU machine kept
running (about $3 a month), with 6 hours of history so each fresh start is quick. To update it after a change:

```sh
fly apps create quanty-demo            # the name must be unique on Fly; change it in fly.demo.toml too
fly deploy --config fly.demo.toml --ha=false
```

`.dockerignore` lets only the source reach the builder, never `.env` or `data/`.
