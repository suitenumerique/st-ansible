# Developing the cli / the collection

## From a clone (development / editable)

```bash
git clone https://github.com/suitenumerique/st-ansible.git
cd st-ansible/cli

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,full]'       # st-cli + test deps + ansible-core + hvac

st-cli --help
pytest -q                           # run the test suite
```

## Linting & formatting (ruff)

CI (`.github/workflows/cli-tests.yml`, scoped to `cli/**`) runs `ruff check`,
`ruff format --check`, and `pytest` on Python 3.13 — **all three must pass**. There
is no committed ruff config, so ruff's defaults apply. Before finishing any change
under `cli/`, run all three from `cli/` (pin `ruff` to `0.15.*`, matching CI, to
avoid style churn):

```bash
ruff check --fix .   # lint (auto-fix trivial issues, e.g. unused imports)
ruff format .        # apply formatting (CI enforces this via `ruff format --check`)
pytest -q            # tests
```

## Developing against a local collection build

To test a local change to the collection instead of the pinned git tag, build a
tarball in the collection root (the parent repo of this `cli/` dir):

```bash
ansible-galaxy collection build        # run in the collection root → suitenumerique-st-<v>.tar.gz
```

Then point st-cli at it via the `ST_CLI_COLLECTION_SOURCE` env var:

```bash
export ST_CLI_COLLECTION_SOURCE=/abs/path/suitenumerique-st-<v>.tar.gz
```

`st-cli deploy` then installs that tarball instead of the git tag. You can also
point at the collection **source directory** (`export ST_CLI_COLLECTION_SOURCE=/abs/path/to/dir`),
which is rendered with `type: dir` in the generated requirements. Either way the
version pin is ignored, and a warning is printed at generate time.
