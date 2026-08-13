# st-cli

A Python wrapper around the `suitenumerique.st` Ansible collection: it bootstraps a
versionable config tree (with `ansible-vault`-encrypted secrets), generates the throwaway
Ansible scaffolding, and drives deploy (ansible) plus restart / ps / one-off / reset /
logs (ssh).

## Quick start

See [getting-started/st-cli.md](../docs/00-getting-started/01-st-cli.md).

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

## Upgrading

`st-cli` and the collection use the same version number, and the upgrade always
happens in two steps: first update the CLI itself, then let it realign your config.

### Step 1 — update the CLI

Using the container:

```bash
podman pull ghcr.io/suitenumerique/st-cli:latest
# or docker pull ghcr.io/suitenumerique/st-cli:latest
```

Using pipx:

```bash
pipx upgrade st-cli
```

`st-cli upgrade` no longer updates the CLI itself. If a newer release is available,
it warns you with the exact command above and stops — it never replays your
questionnaire with an old version's templates.

### Step 2 — realign your config

```bash
st-cli upgrade
```

This does three things:

1. Realigns the `.st-cli.yml` version pin to the CLI you just installed.
2. Replays every unit a release flagged as needing attention (see below), pre-filled
   from your current config, **silently**: an already-known answer is kept without a
   prompt, and only a genuinely new *required* question stops to ask (a new
   optional question is answered blank). If a flag declares a new
   optional component (for example a new `livekit` sidecar), the replay also offers to
   bootstrap it once — declining leaves it alone and the offer stays quiet afterwards.
3. Cleans the trashable `.st-cli/` scaffolding, but only when the pin actually changed.

Silent does not always mean silent: when recovery has a gap — a value st-cli cannot
reconstruct from the committed tree — it still stops and asks, the same as a normal
rebootstrap would. A flag can also mark itself `interactive: true`, which forces the
full pre-filled questionnaire instead of the quiet replay (used for changes too broad
for a silent pass).

We maintain the tags for all tools in the default values of the collection variable,
which means at this point upgrading the actual LST applications is just a deploy:

```bash
st-cli deploy meet prod # roll out the new blessed image tags
```

### Keeping your config up to date (rebootstrap)

Some releases add configuration an app now requires — a new mandatory environment
variable, a new Ansible variable. When that happens, `st-cli doctor` tells you which
apps need attention, `st-cli upgrade` replays them for you (see above), and
`st-cli deploy` refuses to run until it is done:

```bash
st-cli doctor           # e.g. "meet/prod/meet: rebootstrap needed (0.3.0 — …)"
st-cli upgrade           # or: st-cli bootstrap meet prod
```

`doctor` also prints an offline advisory diff for every unit, separate from the
rebootstrap flag above: a "new env keys available" warning when a template offers a
key your committed blob does not have yet (run `bootstrap` to add it), and an info
note when your blob has a key no template recognizes (a custom var, or a leftover —
remove it only if you did not add it yourself). Neither advisory blocks `deploy`;
only a pending rebootstrap flag does, and there is no way around it — no override
flag, no environment variable. `deploy` prints the same advisories, as warnings,
once its gate passes.

**Re-running `bootstrap` on an existing deployment asks what to do.** It offers a
3-way choice:

- **Modify** (default) — replays the same questionnaire with **answers pre-filled
  from your current config**: press Enter to keep a recovered value, or edit it
  inline. Recovery is best-effort: a value st-cli cannot recover falls back to a
  normal prompt, alongside genuinely new questions. An Enter-through run leaves your
  config byte-identical when every answer was recovered, so `git diff` shows exactly
  what changed and nothing else.
- **Reuse** — keeps the unit exactly as it is. Nothing is written, and the
  `bootstrapped_with` stamp does not move: a pending rebootstrap flag stays pending,
  and `deploy` still refuses to run until you Modify or Override for real.
- **Override** — rebuilds the unit from scratch. This is destructive: it regenerates
  the core's own generated secrets (for example `DJANGO_SECRET_KEY`), discards any
  hand-edits to `vars.yml`/`vault.yml`, and breaks deployed services until you
  redeploy. A secret owned by a kept provider (for example the LiveKit API key/secret
  pair) is re-imported unchanged — Override never rotates it. A managed unit that
  mirrors a core-owned secret (for example messages' mta-in copy of `MDA_API_SECRET`)
  is replayed in the same run and picks up the regenerated value. st-cli asks for a
  hard confirmation before it does any of this. Override needs the full
  `st-cli bootstrap <app> <env>` run — a wire-only `-c <core>` run does not offer it.

A gate that applies only sometimes reads accordingly: if SMTP is already configured,
the questionnaire asks "SMTP is configured — review its settings?" instead of asking
whether to set it up from scratch (messages blobs offloading follows the same pattern).
Answering no keeps the current setting unchanged — it never removes it.

If you deliberately switch mode — for example from a `DATABASE_URL` to discrete
`DB_*` vars, or messages outbound from relay to direct — st-cli warns you and lists
the exact committed lines to remove by hand. The merge never deletes a committed line
on its own, so the old lines stay until you remove them.

**Dependencies get their own prompt.** For an existing dependency (for example
`livekit` under `meet`) with no pending rebootstrap flag, `bootstrap` asks whether to
reuse it as-is (default, untouched) or modify it (replay its questionnaire,
pre-filled). When a dependency DOES carry a pending flag, there is no prompt: st-cli
prints the reason and replays that dependency's questionnaire directly — this is the
only way to clear the flag. A `-c <core>`-only run (wiring, no deploy) always reuses
an existing dependency automatically; if it carries a pending flag, st-cli warns that
the flag stays pending, since a wire-only run never deploys a dependency.

What survives a rebootstrap:

- your own `st_*` variables, and any comments you added to `vars.yml`;
- your own `KEY=value` lines inside the `*_env` blocks (new keys from the release are
  appended under an `# added by st-cli <version>` marker — nothing is ever deleted);
- your secrets. Already-answered secrets are never re-prompted and never regenerated, so
  `vault.yml` is left alone unless a release genuinely introduces a new one. If the vault
  cannot be decrypted, the run aborts *before* the questionnaire rather than failing at
  the end.

**What st-cli cannot keep in sync.** If you point a role at your own template — for
example `st_drive_backend_env_template`, a `*_compose_template`, or an overridden
`st_meet_livekit_files` — then the blob st-cli renders is no longer what gets deployed.
A rebootstrap keeps that blob correct but cannot touch your file; keeping it current is
up to you. See the role's `REFERENCE.md` for what upstream expects.

Since the questionnaire is interactive, a non-interactive/CI deploy needs the rebootstrap
done beforehand.

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
