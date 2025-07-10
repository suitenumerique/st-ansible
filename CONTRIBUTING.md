# Contributing

## Clone

The collection **must** be cloned into a valid collection root (e.g. it must contain `ansible_collections/namespace/collection_name`), for example:

```bash
git clone git@github.com:suitenumerique/st-ansible.git ~/git/ansible_collections/suitenumerique/st
```

## Linting

You can install `ansible-lint` for linting :

```bash
pipx install ansible-lint
```

Then you can use the Makefile :

```bash
make lint
```

## Testing

You can use the Makefile to start the sanity tests, which uses `ansible-test` (bundled in `ansible-core`) :

```bash
make test.sanity
```

For more information about sanity tests, unit tests and integration tests, see [Testing Collections](https://docs.ansible.com/ansible/latest/dev_guide/developing_collections_testing.html#testing-collections).

## Documentation

To generate the documentation you can install `aar-doc` :

```bash
pipx install aar-doc
```

Then fill in `meta/main.yml` if not already and `meta/argument_specs.yml` with your variables ([more info](https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_reuse_roles.html#role-argument-validation)).

Then use the Makefile :

```bash
make docs
make docs role=bla
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
