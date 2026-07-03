# st-cli

A Python wrapper around the `suitenumerique.st` Ansible collection: it bootstraps a
versionable config tree (with `ansible-vault`-encrypted secrets), generates the throwaway
Ansible scaffolding, and drives deploy (ansible) plus restart / ps / one-off / reset /
logs (ssh).

## Running via container (recommended)

We recommend using `st-cli` container, you can safely make a *shrc alias
to make things smoother :

**Podman (rootless, recommended):**

```bash
alias st-cli='podman run --rm -ti --userns=keep-id \
  -v "$(pwd):/st-cli" \
  -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent \
  ghcr.io/suitenumerique/st-cli:latest'
```

**Docker:**

```bash
alias st-cli='docker run --rm -ti \
  -v "$(pwd):/st-cli" \
  -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent \
  ghcr.io/suitenumerique/st-cli:latest'
```

Reload your shell (`exec $SHELL`) or open a new terminal, then check it works:

```bash
st-cli --help
```

> [!NOTE]
> `"$SSH_AUTH_SOCK"` requires a working `ssh-agent`. You can take a look at this [github documentation](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent#adding-your-ssh-key-to-the-ssh-agent)
> for more information about how to add your keys, and use `ssh-add -l` to check if your keys are loaded.

## Running via `pipx`

`st-cli` ships three optional extras:

| Extra | Adds | When to use |
|-------|------|-------------|
| `[ansible]` | `ansible-core>=2.16` | You want ansible bundled in the same venv as st-cli. |
| `[hashivault]` | `hvac>=2.0` | You use the `hashi_vault` (OpenBao) secret backend. |
| `[full]` | `ansible-core>=2.16` + `hvac>=2.0` | Self-contained one-command install (recommended). |

### Self-contained (`[full]`)

One command installs st-cli together with ansible-core and hvac in the same isolated
venv — `st-cli` then resolves `ansible-playbook` / `ansible-galaxy` / `ansible-vault`
next to its own interpreter, so the bundled ansible-core is authoritative and runs under
the same Python that has `hvac` (fixing the split-interpreter problem with
`community.hashi_vault`):

```bash
pipx install "st-cli[full] @ git+https://github.com/suitenumerique/st-ansible.git#subdirectory=cli"

st-cli --help
```

Pin a specific release by adding a git ref before the `#`:

```bash
pipx install "st-cli[full] @ git+https://github.com/suitenumerique/st-ansible.git@0.0.20#subdirectory=cli"
```

## SSH configuration (the `ssh/` directory)

> [!NOTE]
> This is used by default the container install, but you can include
> whatever you need from your own `~/.ssh/config`.

`st-cli bootstrap` (and `st-cli deploy`, which regenerates the scaffolding) seeds a
committed `ssh/` directory next to your config tree:

- `ssh/config`: shared OpenSSH **committed** config for host / bastion (`ProxyJump`)
  definitions.
- `ssh/config.local`: the **per-operator** **gitignored** companion to `ssh/config`: put your ssh
  identity (`User`, `IdentityFile`, a personal `ProxyJump`) here.
- `ssh/known_hosts`: host keys for your target servers.

The bastion example seeded in `ssh/config`:

```text
Host bastion
    HostName bastion.example.org

Host 10.0.0.*
    ProxyJump bastion
```

> [!WARNING]
> **Never commit private keys.** `ssh/config` and `ssh/known_hosts` are committed on
> purpose, do not put any secret in them.
> Keep your keys in your ssh-agent (forwarded into the container).

## Secret backends

`bootstrap` asks which secret backend to use for an `(app, env)` (the choice is recorded in
`.st-cli.yml` and applies to every component of that stack):

- **ansible-vault** (default): Real values are encrypted
  in a per-component `vault.yml`; the plaintext env blob carries `{{ vault_<key> }}` refs.
- **hashi_vault** (OpenBao / HashiCorp Vault KV-v2): no `vault.yml` is written. Instead each
  secret in the env blob is a `{{ lookup('community.hashi_vault.hashi_vault', '<term>') }}` ref
  that Ansible resolves from OpenBao at deploy time.

  To setup a secret lookup, use the `@openbao` or `@vault` markers :

  ```text
  DATABASE_URL  →  postgres://app:@openbao(kv/data/db:pw)@db.host/app
  REDIS_URL  →  redis://@vault(kv/data/redis:user):@openbao(kv/data/redis:pw)@redis.host
  ```

## Quick start

See [getting-started/st-cli.md](../docs/00-getting-started/01-st-cli.md).

## Upgrading

### Using the container

`st-cli` and the collection use the same version number. To move to a newer release:

```bash
podman pull ghcr.io/suitenumerique/st-cli:latest
# or docker pull ghcr.io/suitenumerique/st-cli:latest
st-cli upgrade  # bumps the .st-cli.yml pin, regenerates the scaffoldings
```

We maintain the tags for all tools in the default values of the collection variable,
which means at this point upgrading the actual LST applications is just a deploy:

```bash
st-cli deploy meet prod # roll out the new blessed image tags
```

### Using pipx

```bash
st-cli upgrade          # automatically updates pipx package, bumps the .st-cli.yml pin,
                        # and regenerates the scaffoldings
st-cli deploy meet prod # roll out the new blessed image tags
```

## Uninstall

### Using the container

```bash
podman rm ghcr.io/suitenumerique/st-cli:latest
# or docker rm ghcr.io/suitenumerique/st-cli:latest
```

### Using pipx

```bash
pipx uninstall st-cli
```
