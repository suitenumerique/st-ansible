# La Suite Territoriale Ansible Collection

## Install

To install the collection, add a `galaxy_requirements.yml` file to your ansible code containing :

```yaml
collections:
  - name: https://github.com/suitenumerique/st-ansible.git
    type: git
    version: "1"
```

Then use `ansible-galaxy install -r galaxy_requirements.yml`.

## Usage

### Roles

The collection contains the following roles:
- [suitenumerique.st.podman](https://github.com/suitenumerique/st-ansible/blob/main/roles/podman/REFERENCE.md)
- [suitenumerique.st.messages](https://github.com/suitenumerique/st-ansible/blob/main/roles/messages/REFERENCE.md)
- [suitenumerique.st.drive](https://github.com/suitenumerique/st-ansible/blob/main/roles/drive/REFERENCE.md)
- [suitenumerique.st.keycloak](https://github.com/suitenumerique/st-ansible/blob/main/roles/keycloak/REFERENCE.md)
- [suitenumerique.st.alloy](https://github.com/suitenumerique/st-ansible/blob/main/roles/alloy/REFERENCE.md)
- [suitenumerique.st.restic](https://github.com/suitenumerique/st-ansible/blob/main/roles/restic/REFERENCE.md)

## Development

### Dependencies
Create a virtualenv using:
```bash
python3 -m virtualenv ./venv
```
And activate it:
```bash
source ./venv/bin/activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

### Building locally

Before building, run:
```bash
make docs
```
to update the documentations and propagate the default values to roles/<role>/defaults from the argument_specs.yml files

Then build:
```bash
make build
```
This will output a line with the location of the built file: `Created collection for suitenumerique.st at <path>/suitenumerique-st-<version>.tar.gz`

### Using the locally built collection
In your consumer repository's `galaxy_requirements.yml`, overwrite the `collections` key:
```yaml
collections:
  - name: <path>/suitenumerique-st-<version>.tar.gz
    version: <version>
    type: file
```
and run
```bash
ansible-galaxy install -r galaxy_requirements.yml --force
```
to forcefully update the dependency. You can then repeat the `make docs`, `make build` and the previous update command to update the galaxy collection.

## Licensing

This codebase is under MIT License.

See [LICENSE](https://github.com/suitenumerique/st-ansible/blob/main/LICENSE) for full text.
