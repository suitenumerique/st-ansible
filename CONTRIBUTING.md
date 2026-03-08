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

This creates `changelogs/fragments/fix-rspamd-port.yml`. Open it and fill in the relevant section(s), then remove the placeholder. Valid sections (each value is a list of Markdown strings): `major_changes`, `minor_changes`, `breaking_changes`, `deprecated_features`, `removed_features`, `security_fixes`, `bugfixes`, `known_issues`, plus `release_summary` and `trivial`. For example :

```yaml
bugfixes:
  - Fixed the default rspamd controller port for single-host deployments.
```

The `st-cli` subproject shares the collection's version, so it shares this same changelog. Prefix cli entries so readers know the scope :

```yaml
minor_changes:
  - "st-cli: added a --dry-run flag to the deploy command."
```

For changes that should NOT appear in the released changelog (CI tweaks, chores, refactors with no user-facing effect), use a `trivial:` fragment. It still satisfies the PR check but is dropped from the released changelog.

Validate fragments locally before pushing :

```bash
make changelog.lint
```

A GitHub Actions "Changelog" workflow runs on every pull request: it lints the fragments and fails the PR if no fragment was added (a `trivial:` fragment counts).

Maintainers roll the fragments into `CHANGELOG.md` at release time with `make changelog.release` (this consumes and deletes the fragments) — contributors do not run this.

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
