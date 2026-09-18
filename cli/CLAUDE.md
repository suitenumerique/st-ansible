# CLAUDE.md — st-cli

## 1. What it is

`st-cli` (package `st-cli`, version `0.3.1`) bootstraps and operates
`suitenumerique.st` Ansible deployments. It runs **from a deployment repo**
(its CWD), never from this collection repo. It writes a committed config tree,
generates throwaway Ansible scaffolding, and shells out to `ansible-playbook`,
`ansible-galaxy`, `ansible-vault` and `ssh`. It never runs Ansible in-process.

Version source: `st_cli/__init__.py` `__version__`. `pyproject.toml` must match.

## 2. Architecture

```text
st_cli/main.py          Typer app. Global callback = version warnings. _run() maps StCliError -> exit 1.
st_cli/cmd/*.py         One module per subcommand. Calls core/ only.
st_cli/core/*.py        Logic: recover, render, write, manifest, upgrades, pin, runner, vault.
st_cli/core/resources/  Read-only: apps/<app>.yml manifests, templates/, upgrades.yml.
```

Subcommands: `bootstrap`, `deploy`, `secrets`, `restart`, `ps`, `oneoff`,
`reset`, `logs`, `doctor`, `upgrade`, `version`.

Committed by the operator, per `(app, env, component)`:
`<app>/<env>/<component>/{vars.yml,vault.yml,hosts}`, `<app>/<env>/common.yml`,
`ssh/`, and `.st-cli.yml` (version pin, secret backend, unit list with
`bootstrapped_with` stamps). Hosts live only in the INI `hosts` file.
`.st-cli/` is regenerated scaffolding and is gitignored.

### Workflows

- **bootstrap** (`cmd/bootstrap.py`): questionnaire → `answers` → env blobs
  rendered from `templates/env/*.j2` → `writer.write_core` / `write_vault` →
  `manifest.upsert_unit`. Secrets go to `vault.yml` as `{{ vault_* }}` refs, or
  to OpenBao lookup refs with the hashi_vault backend. Dependencies (for example
  meet → livekit) get their own select: deploy now, skip, external, or reuse /
  modify when a provider unit exists.
- **rebootstrap** (same command over an existing unit): a 3-way select
  `ReplayAction.MODIFY` (default) / `REUSE` / `OVERRIDE`. Modify recovers the
  answers from the committed tree (`core/recover.py`), pre-fills every prompt,
  and merges the result back (`core/envblob.merge`). Reuse writes nothing.
  Override rebuilds from scratch and regenerates the core's own secrets.
- **upgrade** (`cmd/upgrade.py`): refuses when behind upstream or when the
  installed CLI is older than the pin (exit 1). Then realigns the pin, replays
  every unit that `core/upgrades.needed` flags (`ReplayAction.SILENT`, or
  `MODIFY` when a flag is full_replay), prints the manual steps, and cleans
  `.st-cli/`. A crashed run resumes on a plain re-run.
- **deploy** (`cmd/deploy.py`): pin and flag gate → `sshuser.ensure_ssh_user`
  → `drift.preflight` (generate + galaxy install) → `runner.play` per unit,
  `serial: 1`, two phases (`base` as root, `deploy` as the app user).
- **doctor** (`core/drift.py`): offline and warn-only. Lists pending flags and
  an env-key diff between a fresh render and the committed blob.
- **restart / ps / logs / oneoff / reset** (`cmd/remote.py`): direct `ssh`.
  `-H/--host` is the inventory alias. They never block on versions. `reset`
  also redeploys the unit through `runner.play` after the teardown.

## 3. What you must maintain

Two artefacts carry the upgrade contract. Every change to an app, a template,
or a questionnaire must keep both correct.

### 3.1 The bootstrap questionnaire (`cmd/bootstrap.py`)

The invariant, pinned by `tests/test_rebootstrap_flow.py`: **an Enter-through
rebootstrap of a committed unit leaves its tree byte-identical, and never
rotates a secret.** `st-cli upgrade` relies on it. Rules for every prompt:

- **Pre-fill from recovery.** Read a text default with `_recall(answers, KEY,
  fallback)`. It returns `prompts.Recovered(...)`. On a required prompt the
  silent replay auto-accepts only a `Recovered` default. A plain fallback
  string still prompts. A default you build by hand from recovered data (a
  parsed URL part, a reconstructed endpoint) must be wrapped in
  `Recovered(...)` too, or every silent upgrade stops on that prompt.
- **Secrets go through `_ask_secret(answers, backend, KEY, component,
  gen=...)`.** It is a no-op when `KEY` is already in `answers`. Never prompt or
  regenerate a secret another way.
- **A gate derives its default from recovered state.** Every `_confirm` or
  `_ask_select` that opens a block (SMTP, S3, blobs offload, DB mode, outbound
  mode, dependency mode) must default to the current committed choice. A wrong
  default silently drops the block on an Enter-through run.
- **A new decision must be visible.** A genuinely new boolean or select passes
  `auto=False`, or the release flag sets `full_replay: true`. Otherwise the
  silent replay answers it without the operator. An optional
  (`required=False`) prompt auto-accepts any default, so a new optional value
  that needs review also needs `full_replay: true`.
- **An optional text prompt uses `_ask_optional`.** A blank answer pops the key
  and warns the operator to delete the committed line by hand, because
  `envblob.merge` never deletes a line.
- **A mode switch never deletes.** Switching `DATABASE_URL` ↔ `DB_*` or relay ↔
  direct keeps the old lines and warns which ones to remove.
- **Domain-derived keys** (`_ask_core`, the `derived` dict) are recomputed
  only when the domain changed; otherwise `setdefault` keeps hand edits.
- **A flagged dependency provider gets no reuse select.** `_handle_dependency`
  replays it directly, because Reuse would move the stamp without a replay
  and clear the flag silently.

When you add or change a prompt:

1. Add it with `_recall` / `_ask_secret` / `_ask_optional` as above.
2. Add the key to `core/recover.py` only when recovery cannot read it from the
   blob or the `{PLACEHOLDER}` component vars.
3. Extend the app's first-run script in `tests/helpers.py`
   (`<app>_first_run_script`) and the round-trip test in
   `tests/test_rebootstrap_flow.py` for that app. The replay leg uses
   `accept_defaults`, which presses Enter on every prompt. Add
   `assert not sq.asked(...)` when the test must prove a prompt did not fire.
4. Decide whether the release needs an entry in `upgrades.yml` (next section).

Per-app entry points: `_ask_core` (Django apps: meet, drive, messages, docs),
`_ask_keycloak`, `_ask_projects`. `_handle_dependency` runs the provider
questionnaires. Keycloak is not a Django app. Messages uses
`STORAGE_MESSAGE_*`, never `AWS_S3_*`.

### 3.2 Release flags (`resources/upgrades.yml`)

```yaml
baseline: "0.0.0"          # stamps below this get one full replay
flags:
  - version: "next"        # "X.Y.Z", or "next" on a branch (make version resolves it)
    apps: [drive]          # list of app names, or the string "all"
    components: [drive]    # optional: component keys of apps; not with "all"
    reason: "drive 3.0 needs the new S3 vars"
    link: "https://.../CHANGELOG.md#v0-4-0"  # optional: overrides the derived CHANGELOG anchor
    full_replay: true      # optional: full pre-filled replay, not a silent one
    new_components: [foo]  # optional: dependency keys to offer once; not with "all"
    warnings: ["..."]      # optional: manual steps the replay cannot do; a list
```

- A flag applies to every managed unit of `apps` whose stamp is below
  `version`. `components` narrows it to the listed units, so a change to
  the core does not force a provider replay. One replay clears every flag
  of a unit. One item is one need, not
  one version: write one item per app when reason, link, or components differ.
- **Add an entry only when operators must act.** A new env key or a new
  manifest var needs no entry: a replay picks it up. Add an entry, with
  `full_replay: true`, when a new required answer has no default or a new
  decision must be reviewed. Add `new_components` when an app gains a
  dependency. Add `warnings` for what a replay cannot do: a removal, a rename, a
  value change, a secret rotation, a data migration, or a secret to create in
  OpenBao.
- **Write `version: "next"` for an unreleased change.** A `next` flag applies
  to every unit, for every stamp. `st-cli upgrade` replays it on every run.
  `deploy` never blocks on it. Its `new_components` are offered on every run.
  `make version` turns `next` into the release version.
- **Prune only by raising `baseline`.** Never delete an entry above it.
- `st-cli upgrade` prints the warnings of every flag between the stamp and the
  newest one, in a final "Manual steps" block, one line per app/env. `doctor`
  and `deploy` print only the version and the reason. Write a warning as one
  short instruction: the line is the action list of the operator.
- CI lints the file (`tests/test_upgrades.py::TestUpgradeFlagFileLint`):
  versions are `X.Y.Z` or `next`; `apps` names real apps, `components` names
  real components of every listed app, `reason` is present, `link` is
  optional, `new_components` names real `dependencies[].on` targets,
  `warnings` is a list of strings. The "not above the shipped CLI" and
  "outranks the baseline" checks apply to `X.Y.Z` entries only.

### 3.3 Change checklist

| You change | Also do |
|---|---|
| A template key or a manifest var | Nothing else. Verify the round-trip test still passes. |
| A prompt, a gate, or a secret | Rules of 3.1, first-run script, round-trip test. |
| A new required answer without a default | Flag with `full_replay: true`. |
| A new dependency component | Manifest `dependencies[]`, `_handle_dependency`, flag with `new_components`. |
| A removed or renamed key, a changed format | Flag with `warnings`. The replay cannot do it. |
| A new app | Manifest in `resources/apps/`, `_ask_<app>` or `_ask_core` branch, templates, first-run script, round-trip test. |
| The version | `make version version=X.Y.Z` at the repo root: bumps `__init__.py`, `pyproject.toml` and `galaxy.yml`; also resolves `next` flags in `upgrades.yml`. |

## 4. Versions, pin and gates

Three versions exist: the installed CLI (`st_cli.__version__`), the pin
(`.st-cli.yml` `versions.cli`), and upstream (latest git tag, cached 6h).
`core/pin.py` compares the first two: `ALIGNED`, `CLI_OLDER`, `CLI_NEWER`,
`UNKNOWN`. The global callback (`core/upstream.maybe_warn_upgrade`) warns on
every subcommand and never blocks:

| State | Warning |
|---|---|
| installed == pin, upstream newer | pull the image (or `pipx upgrade st-cli`) |
| installed < pin | pull the image |
| installed > pin, not behind upstream | run `st-cli upgrade` |

The pull makes the installed CLI newer than the pin. The `st-cli upgrade`
hint then appears on the next command.

`deploy` blocks in two cases only, before any ssh or network side effect:
installed < pin, or a pending flag whose version is at or below the pin (the
replay is missing or crashed; the fix is `st-cli upgrade`). Every other pending
flag is a warning. `upgrade` moves the pin. `docker pull` moves the installed
CLI. Nothing else writes the pin. `ST_CLI_NO_UPSTREAM_CHECK` disables the
callback only.

## 5. Module map

| Module | Role |
|---|---|
| `core/appmeta.py` | Loads `resources/apps/<app>.yml`: components, vars, `env_render`, `dependencies[]` with `shared[]` rules. |
| `core/recover.py` | Inverse of render: rebuilds `answers` from a committed unit. Best-effort, app-agnostic, values verbatim. |
| `core/envblob.py` | Text merge of dotenv blobs. Keeps existing lines in place, appends new keys, never deletes. `merge(x, x) == x`. |
| `core/envrender.py` | Renders env blobs from `templates/env/*.j2`. Missing keys render as `""`. `oidc_endpoints`. |
| `core/writer.py` | Writes `vars.yml` / `vault.yml` / hosts. Merges on rebootstrap, skips an unchanged vault write. |
| `core/prompts.py` | questionary primitives, `Recovered`, `silent_replay()`, `suspend_silent()`. `_password` never auto-accepts. |
| `core/secretbackend.py` | `AnsibleVaultBackend` (values in `vault.yml`) and `HashiVaultBackend` (reference-only lookup refs, mints nothing). |
| `core/upgrades.py` | `needed`, `newest_per_unit`, `pending_warnings`, `new_component_offers`, `parse_version`. |
| `core/drift.py` | `pending_needs`, `check_app`, `format_need`, env-key diff, `preflight`. |
| `core/pin.py` | `PinState`, `compare(m)`. |
| `core/upstream.py` | Upstream tag lookup, `is_behind`, `install_hint`, `maybe_warn_upgrade`. |
| `core/manifest.py` | `.st-cli.yml` I/O: pins, units, secret backend, `ssh_user`. |
| `core/tree.py` | Committed tree I/O with ruamel round-trip, `!vault` scalars, INI hosts, `find_host`. |
| `core/generate.py` | Renders `.st-cli/` scaffolding. `ST_CLI_COLLECTION_SOURCE` overrides the collection pin. |
| `core/runner.py` | `galaxy_install`, `play`, `syntax_check` subprocess wrappers. |
| `core/vault.py` | `ansible-vault` wrappers and `.vault-pass` handling. |
| `core/secrets.py` | `gen_secret`, `gen_token`, `gen_password` for `_ask_secret(gen=...)`. |
| `core/paths.py` | All path computation, anchored at `Path.cwd()`. |
| `core/ui.py` | All console output. `warn`/`error` go to stderr. No bare `print`. |
| `core/sshuser.py` | Once-per-process ssh user guard. `ST_CLI_SSH_USER` overrides. |
| `core/models.py`, `core/errors.py` | Dataclasses; `StCliError`. |

## 6. Conventions

- Raise `StCliError` for an expected failure. Never `sys.exit` in a command.
- Workers own no files: a `workers` component reuses the core unit's files and
  only flips `st_<app>_workers_enabled`.
- `vars.yml` is never encrypted and never carries the enabled flag.
- The secret backend is chosen per `(app, env)` and recorded in `.st-cli.yml`.
  With hashi_vault the operator pre-creates every secret in OpenBao; the
  prompt asks for the lookup term, pre-filled with `@openbao(kv/data/<app>:<KEY>)`.
- Each app role ships distinct uid/gid and host ports so co-located stacks do
  not collide.
- `ssh/config` and `ssh/known_hosts` are committed; `ssh/config.local` is
  per-operator and gitignored.

## 7. Dev workflow

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' pytest-xdist 'ruff==0.15.*'   # ruff pinned like CI
ruff check --fix . && ruff format . && pytest -q -n 6
```

CI (`.github/workflows/cli-tests.yml`) runs `ruff check`, `ruff format
--check` and `pytest` on Python 3.13. Every change must leave the tree
ruff-clean. Tests are offline. Each bootstrap test spawns `ansible-vault`, so
the full suite takes minutes without `-n`. One `test_<module>.py` per module.
`tests/conftest.py` provides the `repo` tmp-cwd fixture and disables the
upstream check. `tests/helpers.py` provides the seeders, the first-run script
builders, `script_questionary` (strict: an unscripted prompt fails) and
`accept_defaults` (Enter-through: use `sq.asked` to prove a prompt did not fire).
