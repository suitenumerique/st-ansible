# CLAUDE.md — st-cli

## 1. Overview

`st-cli` is a Typer-based Python CLI (package `st-cli`, version `0.0.20`) that
bootstraps and operates `suitenumerique.st` Ansible deployments. It lives under
`cli/` inside the larger collection repo. It does **not** run Ansible in-process:
it shells out to `ansible-playbook`, `ansible-galaxy`, and `ansible-vault`,
resolving each binary **next to its own interpreter first** (co-installed
ansible-core is authoritative, pip or pipx, under the same Python that has `hvac`),
falling back to `PATH`. ansible-core and hvac are opt-in extras (`[ansible]`,
`[hashivault]`, `[full]`). Its job: scaffold a versionable config tree (with
`ansible-vault`-encrypted secrets), generate throwaway Ansible scaffolding, and
drive deploy (ansible) plus restart / ps / oneoff / reset / logs (ssh) and doctor.

## 2. Architecture

Layering is strict and one-directional:

```text
st_cli/main.py         Typer app, subcommand registration, error→exit(1) wrapper
        ↓
st_cli/cmd/*.py        One module per subcommand; calls into core/
        ↓
st_cli/core/*.py       Business logic: generation, rendering, running ansible,
                       vault/secrets, manifests, app metadata, paths, tree I/O
        ↓
st_cli/core/resources/ Bundled Jinja2 templates + app manifests (read-only)
```

`main.py` registers 10 subcommands. A global `@app.callback()` runs a best-effort
upstream-version check before every subcommand (`core/upstream.py`); warn-only,
swallows every exception. Each command body is wrapped by `_run(fn)`, which
catches `StCliError` → clean `typer.Exit(1)` (no traceback).

### Main workflows (data flow)

**bootstrap** (`cmd/bootstrap.py`): interactive `questionary` questionnaire →
answers (domain, DB, OIDC, S3, per-dependency 3-state deploy/reuse/external) →
writes committed config tree (`<app>/<env>/<component>/{vars.yml,vault.yml,hosts}`)
→ records units in `.st-cli.yml`. Secrets route to `vault.yml` (encrypted); env
blobs hold `{{ vault_* }}` refs. `-c/--component` narrows to one component (a
provider can be bootstrapped+deployed before the core; `-c <core>` wires deps
wire-only, REUSE/EXTERNAL, no "deploy it now").

**generate** (`core/generate.py`): reads `.st-cli.yml` + `apps/*.yml` → renders
`.st-cli/{ansible.cfg,galaxy-requirements.yml,playbooks/*.yml}` from
`templates/scaffold/*.j2`. One two-phase playbook per unit. Idempotent, fully
regeneratable — `.st-cli/` is gitignored.

**deploy** (`cmd/deploy.py`): `manifest.managed_units` (sorted by `deploy_order`)
→ `drift.preflight` (warn-only: generate → galaxy_install → drift check, AFTER the
pinned collection is installed) → `runner.play` per unit (base + deploy, or
deploy-only with `-d`).

## 3. Key modules

### `st_cli/cmd/` — subcommands

| Module | Command | Responsibility |
|--------|---------|----------------|
| `cmd/bootstrap.py` | `bootstrap APP ENV` | Interactive questionnaire; writes versioned config tree + `.st-cli.yml`. Host validation, OIDC provider choice, dependency 3-state prompt. `-c/--component` scaffolds a single component (provider standalone, core with wire-only deps, or a worker). No flag = full behaviour. A full/core/workers run prints an architecture-docs pointer + a "Requirements" checklist gated behind a yes/no readiness confirmation (declining aborts via `StCliError`); each secret-backend choice carries an inline description. |
| `cmd/deploy.py` | `deploy APP ENV` | Preflight + run playbooks. Flags: `-c/--component` (**repeatable**; empty = all, sorted by `deploy_order`; unknown raises naming it), `-n/--dry-run` (`--check --diff`), `-d/--deploy-only` (app-user phase only), `-H/--host` (single host by inventory **alias**, resolved via `tree.component_inventory`/`find_host` → ansible `--limit`). Every play is `serial: 1`. |
| `cmd/remote.py` | `restart`/`ps`/`oneoff`/`reset`/`logs` | Direct `ssh` (no Ansible). Hosts come from the component's `hosts` file; `-H/--host` is the inventory **alias** (validated via `tree.find_host`), ssh connects to its `ansible_host` ip. `restart`/`ps` loop ssh over each host; their `-c` is **repeatable**, `oneoff`/`logs`/`reset` keep single-`-c`. `restart` bare restarts ALL components and **warns + confirms** (`-y/--yes` skips, non-TTY raises); `restart -p/--parallel` restarts components concurrently (each still rolls hosts one at a time), ignores `deploy_order`, aggregates failures. `restart` drives `ui.progress_reporter` (per-component spinner on TTY, plain lines off-TTY); `_ssh(quiet=True)` discards ssh stdout+stderr so chatter can't garble the spinner — failed host surfaces via aggregated error (`unit@alias (rc=…)`, `st-cli logs` hint). `ps` runs `podman ps -a` per host, **skips `is_worker`**, prints `ui.host_header`. `logs`/`oneoff`/`reset` hit exactly one host (`resolve_target` + `_select_host` prompt; no-TTY + no `-H` raises); run the app-user command via `_as_user` (`sudo -iu <user> …` login shell). `reset` is destructive (stop + `down -v` + `rm -rf` + redeploy). `logs`: `journalctl --user -u` (15 min default, `--since`, live `-f`). **ssh noise suppression**: non-interactive commands use `_ssh(capture_stderr=True)` (stdout live, stderr replayed via `ui.warn` on failure); every `_ssh` passes `-o LogLevel=ERROR`. `_ssh` modes: `quiet` (discard both, restart), `capture_stderr` (mutually exclusive, quiet wins), default (inherit both, interactive). |
| `cmd/upgrade.py` | `upgrade` | Only upgrade path: `pipx upgrade st-cli` → realign `.st-cli.yml` pin from freshly-installed `importlib.metadata` version → clean trashable scaffolding. Realign + clean ONLY on real version change; no-op leaves `.st-cli/` intact + informs (pip-upgrade hint if pipx absent). Does NOT generate/install/doctor. |
| `cmd/version.py` | `version` | Print installed CLI version + `.st-cli.yml` pin; warn on mismatch. |
| `cmd/secrets.py` | `secrets APP ENV` | Edit an (app,env)'s ansible-vault secrets in `$EDITOR` via `ansible-vault edit`; prompts for component when several have `vault.yml`; ansible-vault backend only (hashi_vault refuses, points to OpenBao). `-c/--component` narrows to one. |

_`main.py` also defines `restart`/`ps`/`oneoff`/`reset`/`logs` (→ `cmd/remote.py`)
and `doctor [APP] [ENV]` (→ `core/drift.py`). `deploy`/`restart`/`ps`/`doctor`
take a **repeatable** `-c`; `oneoff`/`logs`/`reset`/`bootstrap`/`secrets` stay
single-`-c`._

### `st_cli/core/` — business logic

| Module | Responsibility |
|--------|----------------|
| `core/appmeta.py` | Loads bundled `resources/apps/<app>.yml` manifests. `AppMeta` + `Dependency` dataclasses (lazy fallback `Component` if `models.py` absent). Accessors: `core()`, `worker()`, `component(key)`, `files_component(key)` (workers → core), `env_render_spec()`, `component_vars()`. |
| `core/drift.py` | Materialize the pinned collection + warn-only drift check. `preflight` (single pair, `deploy`) and `preflight_all` (sweep, `doctor` command) render scaffolding (`generate.generate_all`) + install pinned collection (`runner.galaxy_install`), then check committed `st_*` vars against role `meta/argument_specs.yml` (`check_app`/`check_unit`, difflib hints). Never touches committed tree. |
| `core/envrender.py` | Renders per-component env blobs from `templates/env/*.j2` + bootstrap answers. `render_env(app, component, answers) -> {blob_var: text}`. `oidc_endpoints(provider, base_url, realm)` derives OIDC OP URLs. Tolerant `_EmptyUndefined` → missing keys render as `""`. |
| `core/generate.py` | Renders trashable `.st-cli/` scaffolding: `ansible.cfg`, `galaxy-requirements.yml`, one `playbooks/<app>-<env>-<component>.yml` per unit. Appends `.st-cli/`, `.vault-pass` to `.gitignore`. `ST_CLI_COLLECTION_SOURCE` → installs local tarball/dir instead of the pinned git tag. |
| `core/manifest.py` | Reads/writes `.st-cli.yml` (committed: version pins + units). `upsert_unit` replaces by `(app,env,component)`. `managed_units` returns non-external units sorted by `deploy_order`. `ssh_user()`: `ST_CLI_SSH_USER` env (if set) else `None` (defers to ssh config chain). No `root` default; old `ansible_user`/`ST_CLI_ANSIBLE_USER` no longer read. |
| `core/models.py` | Pure dataclasses, no I/O: `Component` (frozen), `UnitState`, `StCliManifest`. |
| `core/paths.py` | Filesystem helpers anchored at `Path.cwd()` (deployment repo root, **not** this collection repo). Owns **all** path computation: `.st-cli/` scaffolding paths + committed config-tree paths + `ssh/` paths. No path building elsewhere. |
| `core/prompts.py` | Shared questionary input primitives (`_ask`, `_text_question`, `_password`, `_confirm`, `_ask_select`, `_ask_hosts`, `_is_valid_host`). In `core` so `secretbackend.py` can prompt without importing up into `cmd`. `cmd/bootstrap.py` re-exports these. Input counterpart to `core/ui.py`. |
| `core/runner.py` | Subprocess wrappers: `galaxy_install(version)`, `play(app,env,component,check,tags,limit)`, `syntax_check`. Sets `ANSIBLE_CONFIG`. Workers reuse the core unit's inventory via `appmeta.files_component`. `play`'s `limit` = ansible `--limit`, fed by `deploy -H`. |
| `core/secrets.py` | `gen_secret` (Django `SECRET_KEY` alphabet), `gen_token` (`token_urlsafe`), `gen_password`. Used by bootstrap. |
| `core/secretbackend.py` | Per-`(app,env)` secret-backend strategy. `SecretBackend` base + `AnsibleVaultBackend` (historical split, byte-for-byte identical) and `HashiVaultBackend` (OpenBao KV-v2, **reference-only**: env blobs carry `{{ lookup('community.hashi_vault.hashi_vault', '<term>') }}` refs, no `vault.yml`, no generation, no writes). `setup_backend` (bootstrap) + `load_backend` (generate) from `manifest.secret_config_for`; `write_common_connection` merges `ansible_hashi_vault_*` into `common.yml`; `hashi_lookup_ref` builds refs. |
| `core/tree.py` | Reads/writes committed config tree (path computation lives in `paths.py`; this is I/O only). Round-trip ruamel `YAML(typ="rt")` preserving comments + `!vault` scalars (`VaultString`). `read_hosts` parses INI inventory → ips (single source of truth); `read_inventory` returns `(alias,ip)` pairs; `find_host` matches `-H` against the **alias** only; `component_inventory` is worker→core-aware. `ensure_common`/`ensure_ssh_scaffold` seed committed `common.yml` + `ssh/` idempotently — never overwrite. |
| `core/ui.py` | Rich console helpers: `info`/`warn`/`error`/`success` (warn/error → stderr). All user-facing output goes through here. `progress_reporter()` yields a thread-safe `_Reporter` (transient live spinner on TTY, plain lines off-TTY) — used by `restart`. `host_header(name, host)` — used by `ps`. |
| `core/upstream.py` | Best-effort "newer version available" check via `@app.callback()`. Highest semver git tag via anonymous `git ls-remote --tags` (3s timeout, cached 6h under `$XDG_CACHE_HOME/st-cli/upstream.json`); if behind, **warns** to run `upgrade` (never prompts/auto-runs/exits). Any failure swallowed. Skipped for `upgrade`/help and when `ST_CLI_NO_UPSTREAM_CHECK` is set. |
| `core/vault.py` | `ansible-vault` wrappers: `ensure_vault_password` (prompts + writes `.vault-pass` chmod 600 + loud "back this up + share with every operator" warning), `is_encrypted`, `encrypt_file`, `decrypt_to_dict`, `edit_file` (interactive `$EDITOR`, inherits terminal). |
| `core/writer.py` | Pure writers for the committed tree, extracted from `cmd/bootstrap.py` (no prompting, no manifest mutation). `vars_header`, `apply_component_vars`, `write_vault`, `write_core`. Shared-rule helpers: `gen_value`, `rule_is_secret`, `rule_label`, `inject_consumer`. |
| `core/errors.py` | `StCliError` — base for all expected failures. `main._run` catches → `ui.error` + `exit(1)`. `runner.RunnerError` subclasses it. |
| `core/sshuser.py` | `ensure_ssh_user` — once-per-process ssh-user guard (module `_checked` flag), called by `deploy` + direct-ssh ops before connecting. No-op when `ST_CLI_SSH_USER` set or ssh config resolves non-local `User` (offline `ssh -G`); else on TTY prompts once, persists `User <x>` to `ssh/config.local`, applies via `ST_CLI_SSH_USER`; off-TTY warns + proceeds. |

## 4. Resources & templating

Bundled under `st_cli/core/resources/`, packaged automatically by hatchling
(`packages = ["st_cli"]`).

### App manifests — `resources/apps/{meet,drive,messages,keycloak}.yml`

Single source of truth for the app/component map (loaded by `appmeta.load_app()`):
- `app`, `env_docs_url`
- `components[]`: `key`, `role` (FQCN), `user`, `app_name` (systemd unit +
  inventory group), `dir_var`, `enabled_var`, `deploy_order`, `is_core`, optional
  `is_worker`, `vars` (`{PLACEHOLDER}` templates), `env_render` (layer →
  `{blob_var, templates[]}`).
- `dependencies[]`: `of` (consumer), `on` (provider), `shared[]` rules
  (`consumer_env_key`, `var`, either `generate` (`token`/`secret`) or `prompt`,
  plus optional `answer_key`, `consumer_format`, `label`).

### Scaffold templates — `resources/templates/scaffold/*.j2`

- `ansible.cfg.j2` — `collections_path`, `vault_password_file`, `remote_user`
  (only when `ssh_user` set, else defers to ssh config chain).
- `galaxy-requirements.yml.j2` — pins collection git repo + version; when
  `ST_CLI_COLLECTION_SOURCE` set emits a local install entry (tarball: `name`
  only; dir: `name` + `type: dir`).
- `playbook.yml.j2` — two-phase playbook (always `serial: 1`): `base` task
  (`become_user: root`, `tags: ['base']`) + `deploy` task (`become_user: <user>`,
  `tags: ['deploy']`, sets `<enabled_var>: true`). `vars_files`: `vars.yml` + (if
  present) `vault.yml`.

### Env templates — `resources/templates/env/*.j2`

Dotenv-style bodies rendered into `st_<app>_*_env` literal-block vars.
`base.django.env.j2` is the shared base; per-app backend overlays
`{% include "base.django.env.j2" %}` then add app-specific keys (frontend overlays
don't include the base). `_EmptyUndefined` makes missing keys render as `""`.

## 5. Dev workflow

Install (editable, from `cli/`):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # st-cli + pytest, pytest-mock
pip install ansible-core         # ansible-playbook/galaxy/vault on PATH
```

Run: `st-cli --help` (entry point `st-cli = "st_cli.main:app"`). Tests (pytest,
`testpaths = ["tests"]`, offline & hermetic by default): `pytest -q` (the `network`
marker is deselected), `pytest -m network` opts into a real ansible-galaxy install.

**Every change MUST leave the code ruff-clean** — CI
(`.github/workflows/cli-tests.yml`, scoped to `cli/**`) runs `ruff check`,
`ruff format --check`, `pytest` on Python 3.13 (no committed ruff config, defaults
apply). Before finishing any `cli/` change, run all three from `cli/` (pin `ruff`
to `0.15.*` to match CI):

```bash
ruff check --fix .
ruff format .
pytest -q
```

Test layout: one `test_<module>.py` per source module. Shared setup in
`tests/conftest.py` (the `repo` tmp-cwd fixture + autouse upstream-check disabler)
and `tests/helpers.py` (config-tree seeders + `ScriptedQuestionary`). The lone
network test (`test_syntax_check_after_galaxy_install`) is `@pytest.mark.network`,
deselected via `addopts = "-m 'not network'"`. Deps: `typer`, `questionary`,
`ruamel.yaml`, `jinja2`, `rich`; dev `pytest`, `pytest-mock`; build hatchling;
Python ≥ 3.12.

### Developing against a local collection build

Run `ansible-galaxy collection build` in the collection root, then
`export ST_CLI_COLLECTION_SOURCE=/abs/path/to.tar.gz` (or a source dir): generate
overrides the git pin, `st-cli deploy` installs it (`galaxy_install` passes
`--force`). Unset to restore the pin. `st-cli` runs **from a deployment repo** (its
CWD), not this collection repo — `paths.py` anchors at `Path.cwd()`.

## 6. Conventions & gotchas

- **Error handling**: raise `StCliError` (or `RunnerError`) for expected failures.
  `main._run` → `ui.error` + `exit(1)`, no traceback. Never `sys.exit` from a command.
- **All output via `core/ui.py`**: `info`/`success` → stdout, `warn`/`error` →
  stderr. Avoid bare `print` (leaks secrets).
- **Secrets split** (two backends, chosen per `(app,env)` at bootstrap, recorded
  in `.st-cli.yml` `secrets:`):
  - **ansible-vault** (default): `vars.yml` plaintext/diffable with `{{ vault_<key> }}`
    refs; `vault.yml` a whole-file encrypted mapping of real values. Playbook loads
    both via `vars_files`. Routing in `AnsibleVaultBackend.env_secret` (buffered →
    `component_secrets` → `_write_vault`). Byte-for-byte identical to pre-0.0.20.
  - **hashi_vault** (OpenBao KV-v2, opt-in, **reference-only**): no `vault.yml`; env
    blob carries `{{ lookup('community.hashi_vault.hashi_vault', '<term>') }}` refs,
    real values in OpenBao. st-cli never generates/writes secrets — pre-create every
    secret yourself; one agnostic lookup-term prompt per secret (verbatim into the
    blob). Connection (`ansible_hashi_vault_url`, `_validate_certs`, `_auth_method`)
    in `common.yml`; token via `VAULT_TOKEN`/`ANSIBLE_HASHI_VAULT_TOKEN`. Generate
    installs `community.hashi_vault`, omits `vault_password_file` (needs `hvac`).
    Inline `@openbao(<path>)` / `@vault(<path>)` markers wrap only the marked segment
    in a lookup (rest stays literal); a value with no marker is kept literal (plain
    text) — only `@openbao(<path>)`/`@vault(<path>)` markers are wrapped in a lookup.
  - Only the backend *choice* lives in `.st-cli.yml`. No `secrets:` block ⇒ ansible-vault.
- **`vars.yml` is never encrypted** and **never carries the enabled flag** —
  `<enabled_var>: true` is injected inline on the `deploy` task so `base` stays base-only.
- **Workers own no files**: a `workers` component (`is_worker: true`) reuses the
  core unit's `vars.yml`/`vault.yml`/`hosts` verbatim — only flips
  `st_<app>_workers_enabled`. `appmeta.files_component("workers")` → the core (also
  followed by `generate`, `runner._playbook_cmd`, `remote.resolve_target`). No
  `<app>/<env>/workers/` dir is written.
- **`!vault` tagged scalars**: `tree.VaultString` (str subclass) + ruamel
  constructor/representer round-trip `!vault` literals untouched.
- **Hosts live only in the INI `hosts` file**, never in `.st-cli.yml`
  (`tree.read_hosts` parses `ansible_host=` per line). Each app role ships a distinct
  default uid/gid + host-port block (drive 1101/50100, keycloak 1102/50200, meet
  1103/50300, messages 1104/50400) so co-located stacks don't collide.
- **Two-phase deploy**: `base` task (root: podman + user install, idempotent) +
  `deploy` task (app-user: render config + start systemd unit); `-d`/`--deploy-only`
  runs only `--tags deploy` (no root, for routine updates).
- **OIDC providers**: `keycloak` (derive `/realms/<realm>/protocol/openid-connect/...`),
  `proconnect-prod`/`proconnect-integ` (bundled DINUM endpoints), `custom`
  (pass-through). `keycloak` is both an OIDC provider choice **and** a deployable app.
- **keycloak is not a Django app**: no core questionnaire — `bootstrap` dispatches to
  `_ask_keycloak` (DB + hostname + admin creds), renders one `st_keycloak_env` blob
  from `keycloak.env.j2` (no base include, no deps/workers/frontend).
- **messages uses `STORAGE_MESSAGE_*`, not `AWS_S3_*`**: `_ask_core` skips the
  `AWS_S3_*` questionnaire for `messages` (runs `_ask_messages_storage`), and
  `base.django.env.j2` only emits `AWS_S3_*` when `answers.AWS_S3_ENDPOINT_URL` is
  set. Don't re-add `AWS_S3_*` to the messages flow.
- **`doctor` is warn-only and best-effort**: materializes the pinned collection
  (generate + galaxy install) then checks `argument_specs.yml` drift (known
  incomplete — catches renames/typos, no guarantee). Never touches committed tree.
  `paths.py` anchors at `Path.cwd()`; `doctor` has a dev fallback
  (`repo_root().parent / "roles"`) when the collection isn't installed.
- **`upgrade` is the only upgrade path**: CLI + collection are one versioned unit.
  `pipx upgrade st-cli` → realign pin from `importlib.metadata` (not frozen
  `__version__`, lags one run) → clean scaffolding (`.st-cli/` only; repo-root
  `.vault-pass` preserved). Only on real version change; does NOT generate/install/
  doctor. **Version source**: `st_cli/__init__.py` `__version__`; `pyproject.toml`
  `version = "0.0.20"` must match.
- **`ST_CLI_SSH_USER` overrides the ssh user** for the `deploy` path (`ansible.cfg`
  `remote_user`) and direct-ssh ops (`remote._ssh_user`); when unset both defer to the
  ssh config chain — no `root` default. **Migration**: old `ST_CLI_ANSIBLE_USER` env +
  `.st-cli.local.yml` `ansible_user` key no longer read.
- **Host targeting is unified on `-H/--host` = inventory *alias*** (e.g. `meet1`),
  never a raw ip — validated against *this* app/env/component's `hosts`
  (`tree.find_host`). ssh ops map alias → `ansible_host` ip; `deploy` passes `--limit`.
- **Committed `ssh/` scaffold** (host-key + bastion config, not secret; anchored at
  `repo_root() / "ssh"`). `tree.ensure_ssh_scaffold` seeds `config` + `config.local`
  + `known_hosts` **idempotently** (never overwrites), from `bootstrap` and
  `generate`. `config` + `known_hosts` are **COMMITTED** (no active `Host *` seeded);
  `config.local` is **GITIGNORED**, per-operator, Included first, seeded fully
  commented (`Host *`: `User`/`IdentityFile`/`ProxyJump`). The container `Dockerfile`
  appends `Include …/config.local` + `Include …/config` + `UserKnownHostsFile
  …/known_hosts` + `StrictHostKeyChecking accept-new` to `/etc/ssh/ssh_config`
  (config.local first) so st-cli's ssh and ansible verify host keys + resolve
  bastions with no `~/.ssh` mount.
