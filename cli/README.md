# st-cli

`st-cli` is a Python wrapper around the `suitenumerique.st` Ansible collection. It
bootstraps a versionable config tree with `ansible-vault`-encrypted secrets. It
generates the throwaway Ansible scaffolding. It drives deploy over Ansible, and
restart, ps, one-off, reset and logs over ssh.

## Quick start

See [docs/00-getting-started/01-st-cli.md](../docs/00-getting-started/01-st-cli.md).

## Running via container (recommended)

We recommend the `st-cli` container. Add an alias to your `*shrc` file:

Podman (rootless, recommended):

```bash
alias st-cli='podman run --rm -ti --userns=keep-id \
  -v "$(pwd):/st-cli" \
  -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent \
  ghcr.io/suitenumerique/st-cli:latest'
```

Docker:

```bash
alias st-cli='docker run --rm -ti \
  -v "$(pwd):/st-cli" \
  -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent \
  ghcr.io/suitenumerique/st-cli:latest'
```

Reload your shell with `exec $SHELL`, or open a new terminal. Then check it works:

```bash
st-cli --help
```

> [!NOTE]
> `"$SSH_AUTH_SOCK"` needs a working `ssh-agent`. See this [GitHub documentation](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent#adding-your-ssh-key-to-the-ssh-agent)
> to add your keys. Run `ssh-add -l` to check that your keys are loaded.

## Running via `pipx`

`st-cli` ships three optional extras:

| Extra | Adds | When to use |
|-------|------|-------------|
| `[ansible]` | the pinned `ansible-core` | You want ansible bundled in the same venv as st-cli. |
| `[hashivault]` | the pinned `hvac` | You use the `hashi_vault` (OpenBao) secret backend. |
| `[full]` | the pinned `ansible-core` and `hvac` | Self-contained one-command install (recommended). |

### Self-contained (`[full]`)

One command installs st-cli together with ansible-core and hvac in the same
isolated venv. `st-cli` then resolves `ansible-playbook`, `ansible-galaxy` and
`ansible-vault` next to its own interpreter. The bundled ansible-core becomes
authoritative and runs under the same Python that has `hvac`. This fixes the
split-interpreter problem with `community.hashi_vault`:

```bash
pipx install "st-cli[full] @ git+https://github.com/suitenumerique/st-ansible.git#subdirectory=cli"

st-cli --help
```

Pin a specific release by adding a git ref before the `#`:

```bash
pipx install "st-cli[full] @ git+https://github.com/suitenumerique/st-ansible.git@<tag>#subdirectory=cli"
```

Replace `<tag>` with the release you want, for example `0.3.1`.

## SSH configuration (the `ssh/` directory)

> [!NOTE]
> The container install uses this by default. You can add whatever you need
> from your own `~/.ssh/config`.

`st-cli bootstrap` (and `st-cli deploy`, which regenerates the scaffolding) seeds a
committed `ssh/` directory next to your config tree:

- `ssh/config`: shared OpenSSH committed config for host and bastion
  (`ProxyJump`) definitions.
- `ssh/config.local`: the per-operator, gitignored companion to `ssh/config`.
  Put your ssh identity (`User`, `IdentityFile`, a personal `ProxyJump`) here.
- `ssh/known_hosts`: host keys for your target servers.

`ssh/config` seeds a bastion example, fully commented out:

```text
#   Host bastion
#       HostName bastion.example.org
#
#   Host 10.0.0.*
#       ProxyJump bastion
```

Uncomment and edit it to match your infrastructure.

> [!WARNING]
> Never commit private keys. `ssh/config` and `ssh/known_hosts` are committed on
> purpose. Do not put any secret in them.
> Keep your keys in your ssh-agent. Forward the agent into the container.

## Secret backends

`bootstrap` asks which secret backend to use for an `(app, env)`. It records the
choice in `.st-cli.yml`, and the choice applies to every component of that stack:

- ansible-vault (default): st-cli encrypts real values in a per-component
  `vault.yml`. The plaintext env blob carries `{{ vault_<key> }}` refs.
- hashi_vault (OpenBao / HashiCorp Vault KV-v2): st-cli writes no `vault.yml`.
  Instead, each secret in the env blob is a
  `{{ lookup('community.hashi_vault.hashi_vault', '<term>') }}` ref that Ansible
  resolves from OpenBao at deploy time.

  To set up a secret lookup, use the `@openbao` or `@vault` markers:

  ```text
  DATABASE_URL  →  postgres://app:@openbao(kv/data/db:pw)@db.host/app
  REDIS_URL  →  redis://@vault(kv/data/redis:user):@openbao(kv/data/redis:pw)@redis.host
  ```

## Upgrading

`st-cli` and the collection share one version number. The upgrade always happens
in two steps: first update the CLI itself, then let it realign your config.

Every command prints a short warning when your installed CLI and your
`.st-cli.yml` pin drift apart, or when a newer upstream release exists. You do
not have to check by hand:

- installed CLI == pin, and a newer release exists upstream: pull the new
  image, or run `pipx upgrade st-cli`. The next command then shows the
  `st-cli upgrade` hint below, once the pull makes the CLI newer than the pin.
- installed CLI < pin: someone else already ran `st-cli upgrade` and committed
  the newer pin. Pull the new image first.
- installed CLI > pin: you pulled the new image but did not run `st-cli
  upgrade` yet. Run `st-cli upgrade` to align the repo.

`st-cli deploy` and `st-cli upgrade` can refuse to run. See below.

### Step 1: update the CLI

Using the container:

```bash
podman pull ghcr.io/suitenumerique/st-cli:latest
# or docker pull ghcr.io/suitenumerique/st-cli:latest
```

Using pipx:

```bash
pipx upgrade st-cli
```

`st-cli upgrade` does not update the CLI itself. When a newer release is
available, it warns you with the exact command above and stops. It never
replays your questionnaire with an old version's templates. It also refuses to
run when your installed CLI is older than the `.st-cli.yml` pin: another
operator already ran `st-cli upgrade` and committed the newer pin. Pull the new
image first, then run `st-cli upgrade` again. Both refusals exit with a
non-zero status, so a script or CI job can detect them.

### Step 2: realign your config

```bash
st-cli upgrade
```

This does three things:

1. Realigns the `.st-cli.yml` version pin to the CLI you just installed.
2. Replays every unit a release flagged as needing attention, pre-filled from
   your current config, silently. An already-known answer stays without a
   prompt. Only a genuinely new required question stops to ask. st-cli answers
   a new optional question blank. If a flag declares a new optional component,
   for example a new `livekit` sidecar, the replay also offers to bootstrap it
   once. Declining leaves it alone, and the offer stays quiet afterwards.
3. Cleans the trashable `.st-cli/` scaffolding, but only when the pin actually
   changed.

A flag can also carry `warnings`: manual steps the replay cannot do, for
example a value that must change format. `st-cli upgrade` prints every such
warning between your current stamp and the newest flag, not only the newest
one. It prints them in a final "Manual steps for this upgrade:" block, one line
per app/env, before the "upgrade complete" line.

A crashed `st-cli upgrade` is safe to re-run. The pin is already aligned. Every
unit already replayed is skipped. Each replay is idempotent: it writes the same
files again and never rotates a secret.

Silent does not always mean silent. When recovery has a gap, a value st-cli
cannot reconstruct from the committed tree, it still stops and asks, the same
as a normal rebootstrap would. A flag can also mark itself `full_replay: true`.
This forces the full pre-filled questionnaire instead of the quiet replay, for
changes too broad for a silent pass.

We maintain the tags for all tools in the default values of the collection
variable. At this point, an upgrade of the actual LST applications is just a
deploy:

```bash
st-cli deploy meet prod # deploy the pinned image tags
```

### Keeping your config up to date (rebootstrap)

Some releases add configuration an app now requires: a new mandatory
environment variable, a new Ansible variable. When that happens, `st-cli
doctor` tells you which apps need attention, and `st-cli upgrade` replays them
for you (see above):

```bash
st-cli doctor           # e.g. "meet/prod/meet: rebootstrap needed (0.3.0 — …)"
st-cli upgrade
```

`st-cli deploy` blocks in only two cases, before it touches any server:

- your installed CLI is older than the `.st-cli.yml` pin. Pull the new image
  first, or run `pipx upgrade st-cli`.
- a pending rebootstrap flag sits at or below the pin. The pin was already
  bumped, but the replay for that unit is missing or crashed. Run `st-cli
  upgrade` to resume it.

Every other pending rebootstrap flag is a warning only. `deploy` continues. Run
`st-cli upgrade` when you get the chance.

`doctor` and `deploy` print only the version and the reason of a flag.
`st-cli upgrade` prints the link and the manual steps.

`doctor` also prints an offline advisory diff for every unit, separate from the
rebootstrap flags above. It is a "new env keys available" warning, shown when a
template offers a key your committed blob does not have yet. Run `bootstrap` to
add it. st-cli never reports your own keys in a blob. Neither the advisory, nor
a warning-only rebootstrap flag, ever blocks `deploy`. `deploy` prints them as
warnings once past its gate.

Re-running `bootstrap` on an existing deployment asks what to do. It offers a
3-way choice:

- Modify is the default. It replays the same questionnaire with answers
  pre-filled from your current config. Press Enter to keep a recovered value,
  or edit it inline. Recovery is best-effort. A value st-cli cannot recover
  falls back to a normal prompt, alongside genuinely new questions. An
  Enter-through run leaves your config byte-identical when every answer was
  recovered, so `git diff` shows exactly what changed and nothing else.
- Reuse keeps the unit exactly as it is. Nothing is written, and the
  `bootstrapped_with` stamp does not move. A pending rebootstrap flag stays
  pending. `deploy` only warns about it until the pin catches up to the flag's
  version, then it blocks until you Modify or Override for real.
- Override rebuilds the unit from scratch. This is destructive. It regenerates
  the core's own generated secrets, for example `DJANGO_SECRET_KEY`. It
  discards any hand-edits to `vars.yml` and `vault.yml`, and it breaks deployed
  services until you redeploy. A secret owned by a kept provider, for example
  the LiveKit API key/secret pair, is re-imported unchanged. Override never
  rotates it. A managed unit that mirrors a core-owned secret, for example
  messages' pymta copy of `MDA_API_SECRET`, is replayed in the same run and
  picks up the regenerated value. st-cli asks for a hard confirmation before it
  does any of this. Override needs the full `st-cli bootstrap <app> <env>` run.
  A wire-only `-c <core>` run does not offer it.

A gate that applies only sometimes reads accordingly. If SMTP is already
configured, the questionnaire asks "SMTP is configured — review its settings?"
instead of asking whether to set it up from scratch. Messages blobs offloading
follows the same pattern. Answering no keeps the current setting unchanged. It
never removes it.

If you deliberately switch mode, for example from a `DATABASE_URL` to discrete
`DB_*` vars, or messages outbound from relay to direct, st-cli warns you and
lists the exact committed lines to remove by hand. The merge never deletes a
committed line on its own, so the old lines stay until you remove them.

Dependencies get their own prompt. For an existing dependency, for example
`livekit` under `meet`, with no pending rebootstrap flag, `bootstrap` asks
whether to reuse it as-is or modify it. Reuse is the default and leaves it
untouched. Modify replays its questionnaire, pre-filled. When a dependency does
carry a pending flag, there is no prompt. st-cli prints the reason and replays
that dependency's questionnaire directly. This is the only way to clear the
flag. A `-c <core>`-only run, wiring only with no deploy, always reuses an
existing dependency automatically. If the dependency carries a pending flag,
st-cli warns that the flag stays pending, since a wire-only run never deploys a
dependency.

What survives a rebootstrap:

- your own `st_*` variables, and any comments you added to `vars.yml`;
- your own `KEY=value` lines inside the `*_env` blocks. New keys from the
  release are appended under an `# added by st-cli <version>` marker. Nothing
  is ever deleted;
- your secrets. st-cli never re-prompts or regenerates an already-answered
  secret, so `vault.yml` stays untouched unless a release genuinely introduces
  a new one. If the vault cannot be decrypted, the run aborts before the
  questionnaire runs, rather than failing at the end.

What st-cli cannot keep in sync. If you point a role at your own template, for
example `st_drive_backend_env_template`, a `*_compose_template`, or an
overridden `st_meet_livekit_files`, then the blob st-cli renders is no longer
what gets deployed. A rebootstrap keeps that blob correct but cannot touch your
file. Keeping it current is up to you. See the role's `REFERENCE.md` for what
upstream expects.

Since the questionnaire is interactive, a non-interactive or CI deploy needs
the rebootstrap done beforehand.

## Uninstall

### Using the container

```bash
podman rmi ghcr.io/suitenumerique/st-cli:latest
# or docker rmi ghcr.io/suitenumerique/st-cli:latest
```

### Using pipx

```bash
pipx uninstall st-cli
```
