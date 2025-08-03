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
- [suitenumerique.st.podman](https://github.com/suitenumerique/st-ansible/-/tree/main/roles/podman/REFERENCE.md)
- [suitenumerique.st.messages](https://github.com/suitenumerique/st-ansible/-/tree/main/roles/messages/REFERENCE.md)
- [suitenumerique.st.alloy](https://github.com/suitenumerique/st-ansible/-/tree/main/roles/alloy/REFERENCE.md)
- [suitenumerique.st.restic](https://github.com/suitenumerique/st-ansible/-/tree/main/roles/restic/REFERENCE.md)
- [suitenumerique.st.valkey](https://github.com/suitenumerique/st-ansible/-/tree/main/roles/valkey/REFERENCE.md)

## Licensing

This codebase is under MIT License.

See [LICENSE](https://github.com/suitenumerique/st-ansible/blob/main/LICENSE) for full text.
