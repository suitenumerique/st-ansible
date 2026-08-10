# Contributing

## Clone

The collection **must** be cloned into a valid collection root (e.g. it must contain `ansible_collections/namespace/collection_name`), for example:

```bash
git clone git@github.com:suitenumerique/st-ansible.git ~/git/ansible_collections/suitenumerique/st
```

## Linting

We use `ansible-lint` for linting (contained in `requirements.txt`), you can use the Makefile :

```bash
make lint
```

## Testing

You can use the Makefile to start the sanity tests, which uses `ansible-test` (bundled in `ansible-core`) :

```bash
make test.sanity
```

For more information about sanity tests, unit tests and integration tests, see [Testing Collections](https://docs.ansible.com/ansible/latest/dev_guide/developing_collections_testing.html#testing-collections).

## Molecule Tests

You need to install vagrant and libvirt to run molecule tests:

```bash
apt install -y vagrant libvirt-daemon-system
```

You can then run Molecule tests to verify roles:

```bash
make molecule
```

To run tests for a specific role:

```bash
make molecule role=restic
```

## Documentation

To generate the documentation you can install `aar-doc` (contained in `requirements.txt`), then fill in `meta/main.yml` if not already and `meta/argument_specs.yml` with your variables ([more info](https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_reuse_roles.html#role-argument-validation)).

Then use the Makefile :

```bash
make docs
make docs role=bla
```

## Changelog

This collection uses `antsibull-changelog` (contained in `requirements.txt`, configured in `changelogs/config.yaml`). The human-readable changelog is `CHANGELOG.md` at the repo root and is generated — never edit it by hand.

Every pull request must add a fragment: a small YAML file in `changelogs/fragments/`. Scaffold one with the Makefile :

```bash
make changelog.fragment name=fix-rspamd-port
```

This creates `changelogs/fragments/fix-rspamd-port.yml`. Open it and fill in the relevant section(s), then remove the placeholder. Valid sections (each value is a list of reStructuredText strings): `major_changes`, `minor_changes`, `breaking_changes`, `deprecated_features`, `removed_features`, `security_fixes`, `bugfixes`, `known_issues`, plus `release_summary` and `trivial`. For example :

```yaml
bugfixes:
  - Fixed the default rspamd controller port for single-host deployments.
```

An entry is **reStructuredText, not Markdown**. Two rules matter :

- Write a code span with **double** backticks: ` ``st-cli upgrade`` `. A single backtick is an RST title reference and renders as italics.
- Do not start an entry with `(`. RST reads `(cli)` as a list enumerator (`cli` is the roman numeral 151), drops it, and prints `1.` in its place.

The `st-cli` subproject shares the collection's version, so it shares this same changelog. Prefix an entry with its scope, followed by a colon, so readers know where the change applies :

```yaml
minor_changes:
  - "cli: added a ``--dry-run`` flag to the deploy command."
```

For changes that should NOT appear in the released changelog (CI tweaks, chores, refactors with no user-facing effect), use a `trivial:` fragment. It still satisfies the PR check but is dropped from the released changelog.

Validate fragments locally before pushing :

```bash
make changelog.lint
```

A GitHub Actions "Changelog" workflow runs on every pull request: it lints the fragments and fails the PR if no fragment was added (a `trivial:` fragment counts).

Maintainers roll the fragments into `CHANGELOG.md` at release time with `make changelog.release` (this consumes and deletes the fragments) — contributors do not run this.

## Dependency updates (Renovate)

Third-party versions are kept up to date by [Renovate](https://docs.renovatebot.com/) (configured in `renovate.json5`). It watches the container image tags and the restic release (annotated with `# renovate:` comments in each role's `meta/argument_specs.yml`), the Ansible collection dependencies in `galaxy.yml`, the `st-cli` / `molecule-lima` Python dependencies in the `pyproject.toml` files, and the base image in `cli/Dockerfile`. Every update is held for a **7-day minimum release age** before it is proposed (security fixes with advisory data are exempt); nothing is auto-merged.

Renovate only edits the **source of truth** — never the generated `roles/*/defaults/main.yml`. Its pull requests are therefore an *inbox*: you don't merge them directly, you integrate them onto your own branch, which regenerates the defaults and adds the changelog fragment. Branch off `main`, then :

```bash
make renovate pr=42            # one PR
make renovate pr=21,22,23      # several at once
```

For each PR this fetches its head (plain `git`, no `gh` CLI or token needed), cherry-picks it staged-but-uncommitted, scaffolds a `changelogs/fragments/renovate-pr-<N>.yml` fragment from the Renovate commit message, and regenerates the role defaults with `make docs`. Review `git status`, then commit, push, and open/merge your PR; Renovate closes its own PR once the dependency is up to date. (Renovate's inbox PRs skip the changelog-fragment check, since the fragment is created at integration time.)

To make Renovate track a **new** hand-pinned version, add an annotation comment directly above its `default:` in `meta/argument_specs.yml`, for example :

```yaml
        # renovate: datasource=docker depName=docker.io/caddy
        default: "2.11.4-alpine"
```

## Build

You can build the collection with the Makefile :
```bash
make build
```

And then install it in an Ansible repo elsewhere :
```bash
cd ~/bla
ansible-galaxy collection install ~/git/ansible_collections/suitenumerique/st/build/suitenumerique-st-1.0.0.tar.gz -p ./collections
```
