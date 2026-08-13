# CLAUDE.md — st-cli

## 1. Overview

`st-cli` is a Typer-based Python CLI (package `st-cli`, version `0.2.0`) that
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

`main.py` registers 11 subcommands. A global `@app.callback()` runs a best-effort
upstream-version check before every subcommand (`core/upstream.py`); warn-only,
swallows every exception. Each command body is wrapped by `_run(fn)`, which
catches `StCliError` → clean `typer.Exit(1)` (no traceback).

### Main workflows (data flow)

**bootstrap** (`cmd/bootstrap.py`): interactive `questionary` questionnaire →
answers (domain, DB, OIDC, S3, per-dependency select) → writes committed config
tree (`<app>/<env>/<component>/{vars.yml,vault.yml,hosts}`) → records units in
`.st-cli.yml`. Secrets route to `vault.yml` (encrypted); env blobs hold
`{{ vault_* }}` refs. `-c/--component` narrows to one component (a provider can
be bootstrapped+deployed before the core; `-c <core>` wires deps wire-only: an
existing provider is reused automatically, a fresh one stays deploy-less
REUSE/EXTERNAL, no "deploy it now"). The per-dependency select depends on the
provider's state: a fresh provider offers deploy/skip/external; an existing
provider with no pending rebootstrap flag offers reuse (default, untouched) or
modify (replay, pre-filled); an existing provider with a pending flag skips the
select entirely and replays its questionnaire directly — see **rebootstrap**
below.

**rebootstrap** (same command, `cmd/bootstrap.py`): when a unit already exists,
`bootstrap` offers a 3-way select instead of overwriting — `ReplayAction.MODIFY`
(default) / `REUSE` / `OVERRIDE` (`_ask_rebootstrap_action`). **Modify** replays the
questionnaire: `core/recover.py` rebuilds `answers` from the committed tree (env
blobs parsed back to `KEY=value`, `{PLACEHOLDER}` component vars inverted,
hosts/cadvisor/dep-mode read back), every prompt pre-fills with it, and
`writer.write_core` MERGES via `core/envblob.merge`. Recovered secrets are never
re-prompted or regenerated; `writer.ensure_vault_readable` aborts up front if the
vault can't be decrypted. Gate confirms are context-aware: a recovered SMTP or
blobs-offload config reads "… is configured — review its settings?" instead of
asking whether to set it up, and declining means keep unchanged, never remove. A
mode switch (`DATABASE_URL` ↔ discrete `DB_*`, messages outbound relay ↔ direct) is
allowed but never silently drops the old lines (`envblob.merge` never deletes):
st-cli warns and lists the exact committed lines to remove by hand. **Reuse** keeps
the unit as-is: no `upsert_unit`, no `save_manifest`, `return`s before
`setup_backend` — the stamp structurally cannot move, so a pending flag stays
pending (warned per flag) and `deploy` keeps refusing. **Override** is a hard
destructive confirm (`auto=False`, decline raises `StCliError`; names the
regenerated secrets — the CORE's own generated ones such as
`DJANGO_SECRET_KEY`; a kept provider's secret, e.g. the LiveKit pair, is
re-imported unchanged and can never rotate here — and that a managed
dependency mirroring a core-owned secret, e.g. mta-in's copy of
`MDA_API_SECRET`, is replayed automatically in the same run so it picks up
the regenerated value), then runs `fresh=True`: recovery seeding is skipped
(`seed = {}`, empty host defaults, cadvisor default `True`) and
`writer.write_core`/`write_vault` start from an empty tree/wholesale write
(`fresh=`/`replace=`) instead of merging. The dependency loop still runs
normally, but an existing (non-external) dependency's reuse/modify select is
skipped under a core `OVERRIDE` — it is forced straight to the deploy branch,
the same one a flagged replay uses (`_handle_dependency`'s `override_core`
param): a plain Reuse would only re-inject a `shared` rule with a
`consumer_env_key`, missing values a provider-specific step constructs
directly (e.g. messages' `SPAM_CONFIG`/`MTA_OUT_DIRECT_PROXIES`), which would
otherwise vanish from the fresh core buffer entirely. Recovered provider
secrets are re-injected into that buffer, never rotated. Units are stamped
`bootstrapped_with`; `resources/upgrades.yml` declares which
releases require a rerun (`interactive: true` forces Modify; `new_components`
offers a newly declared component once). A dependency provider with a PENDING flag
skips the reuse/modify select entirely: st-cli prints the flag's reason and replays
that provider's questionnaire directly (pre-filled) — an actual replay is the only
thing that clears the flag. `st-cli upgrade` drives the same replay
programmatically via `ReplayAction.SILENT` (`core/prompts.py`'s silent-replay
machinery: recovered answers are kept without a prompt, only new REQUIRED
questions ask — a new optional question is answered blank; see the §6
silent-replay rule).
`doctor` reports outstanding flags plus an offline env-key diff (warn-only
advisory, never blocking); `deploy` hard-blocks on a pending flag (no override) and
prints the same advisories once the gate passes.

**generate** (`core/generate.py`): reads `.st-cli.yml` + `apps/*.yml` → renders
`.st-cli/{ansible.cfg,galaxy-requirements.yml,playbooks/*.yml}` from
`templates/scaffold/*.j2`. One two-phase playbook per unit. Idempotent, fully
regeneratable — `.st-cli/` is gitignored.

**deploy** (`cmd/deploy.py`): `manifest.managed_units` (sorted by `deploy_order`)
→ `drift.check_app` hard gate (before any ssh/network side effect) →
`sshuser.ensure_ssh_user` → `drift.preflight` (materialize-only: generate →
galaxy_install) → `runner.play` per unit (base + deploy, or deploy-only with
`-d`).

## 3. Key modules

### `st_cli/cmd/` — subcommands

| Module | Command | Responsibility |
|--------|---------|----------------|
| `cmd/bootstrap.py` | `bootstrap APP ENV` | Interactive questionnaire; writes versioned config tree + `.st-cli.yml`. **Re-running over an existing unit offers a 3-way select** — Modify (default, recoverable answers pre-fill from the tree, best-effort; merged back; byte-identical on an Enter-through run when recovery is complete) / Reuse (keep as-is, nothing written, stamp doesn't move) / Override (destructive from-scratch rebuild, regenerates secrets) — there is no overwrite confirm any more, see `ReplayAction`. The select only ever applies to the CORE target (`component is None` or the core key); a `-c <workers>` run keeps a plain Modify/Silent replay with no select — `REUSE`/`OVERRIDE` raise `StCliError` naming the core-only limitation if passed programmatically for it. Host validation, OIDC provider choice, per-dependency select: fresh provider → deploy/skip/external; existing provider with no pending flag → reuse (default)/modify; existing provider with a pending rebootstrap flag → no select, replays that provider's questionnaire directly; existing provider under a CORE `OVERRIDE` → no select either, forced straight to the same replay (an override wipes the core's own wiring — e.g. messages' `SPAM_CONFIG`/`MTA_OUT_DIRECT_PROXIES` — so every existing dependency must rebuild it in the same run; recovered provider secrets are re-injected, never rotated). `-c/--component` scaffolds a single component (provider standalone, core with wire-only deps, or a worker); in a wire-only core-only run an existing provider is reused automatically, warning that a pending flag stays pending — and Override is unavailable there (the select omits it; `replay=OVERRIDE` raises): a wire-only run never touches a provider, so it cannot force-replay the ones that rebuild the core-side wiring. No flag = full behaviour. A full/core/workers run prints an architecture-docs pointer + a "Requirements" checklist gated behind a yes/no readiness confirmation (declining aborts via `StCliError`); each secret-backend choice carries an inline description. `replay: ReplayAction = ASK` is the programmatic entry point (`st-cli upgrade`, tests) that skips the select — `MODIFY`/`REUSE`/`OVERRIDE` behave as above for a core/full target; for a `-c <provider>` target (not core/workers) `REUSE`/`OVERRIDE` raise `StCliError` (they apply to the core path only) — `MODIFY`/`SILENT` are unaffected; `SILENT` drives the questionnaire through `core/prompts.silent_replay()` (requires a committed unit to recover from). |
| `cmd/deploy.py` | `deploy APP ENV` | Preflight + run playbooks. Flags: `-c/--component` (**repeatable**; empty = all, sorted by `deploy_order`; unknown raises naming it), `-n/--dry-run` (`--check --diff`), `-d/--deploy-only` (app-user phase only), `-H/--host` (single host by inventory **alias**, resolved via `tree.component_inventory`/`find_host` → ansible `--limit`). Every play is `serial: 1`. |
| `cmd/remote.py` | `restart`/`ps`/`oneoff`/`reset`/`logs` | Direct `ssh` (no Ansible). Hosts come from the component's `hosts` file; `-H/--host` is the inventory **alias** (validated via `tree.find_host`), ssh connects to its `ansible_host` ip. `restart`/`ps` loop ssh over each host; their `-c` is **repeatable**, `oneoff`/`logs`/`reset` keep single-`-c`. `restart` bare restarts ALL components and **warns + confirms** (`-y/--yes` skips, non-TTY raises); `restart -p/--parallel` restarts components concurrently (each still rolls hosts one at a time), ignores `deploy_order`, aggregates failures. `restart` drives `ui.progress_reporter` (per-component spinner on TTY, plain lines off-TTY); `_ssh(quiet=True)` discards ssh stdout+stderr so chatter can't garble the spinner — failed host surfaces via aggregated error (`unit@alias (rc=…)`, `st-cli logs` hint). `ps` runs `podman ps -a` per host, **skips `is_worker`**, prints `ui.host_header`. `logs`/`oneoff`/`reset` hit exactly one host (`resolve_target` + `_select_host` prompt; no-TTY + no `-H` raises); run the app-user command via `_as_user` (`sudo -iu <user> …` login shell). `reset` is destructive (stop + `down -v` + `rm -rf` + redeploy). `logs`: `journalctl --user -u` (15 min default, `--since`, live `-f`). **ssh noise suppression**: non-interactive commands use `_ssh(capture_stderr=True)` (stdout live, stderr replayed via `ui.warn` on failure); every `_ssh` passes `-o LogLevel=ERROR`. `_ssh` modes: `quiet` (discard both, restart), `capture_stderr` (mutually exclusive, quiet wins), default (inherit both, interactive). |
| `cmd/upgrade.py` | `upgrade` | No longer self-upgrades — checks upstream first (`upstream.get_latest_cached` + `upstream.is_behind`; `ST_CLI_NO_UPSTREAM_CHECK` is a deliberate opt-out, tracked separately from a genuinely failed check so the "could not check" info is not printed for it): behind → warns with the concrete command (`pipx upgrade st-cli` if `upstream.owning_pipx()`, else `docker pull ghcr.io/suitenumerique/st-cli:latest`) then "re-run `st-cli upgrade`", and **returns** — old code must never replay a questionnaire against new release templates. Unknown upstream (and not opted out) → info, continues with the installed version. Then realigns `.st-cli.yml` pin from freshly-installed `importlib.metadata` version (before any replay, so a mid-replay crash leaves the pin correct and the stamp old — `deploy` still gates, a re-run resumes). `needs = upgrades.newest_per_unit(upgrades.needed(m))` grouped by `(app, env)`; `writer.ensure_vault_readable` runs for every group up front (one bad vault aborts the whole upgrade, not just its group). Per group: prints each need, picks `replay = MODIFY if any(n.interactive) else SILENT`; an `appmeta.load_app` failure (the manifest names an app this CLI version no longer ships) warns and skips just that group, rather than aborting the whole upgrade. Otherwise calls `bootstrap_mod.bootstrap(app, env, replay=...)` — or, when no core tree exists (a provider-only repo), one per-component `-c` call per need plus a warn that new-component offers are skipped. Replays run even when the pin was already aligned (a prior run may have realigned the pin but died before finishing every replay). Cleans trashable scaffolding ONLY on a real pin change. The closing "upgrade complete" success prints only when something actually happened (the pin changed or at least one replay ran); a no-op run's closing line is "No pending rebootstraps" instead. Does NOT generate/install/doctor. |
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
| `core/drift.py` | Rebootstrap-status report + offline env-key diff. `check_app` turns `upgrades.needed()` into warning strings (unit, flagged version, reason, link, exact command); only the NEWEST flag per unit is reported — this is the only thing that hard-gates `deploy`. A second, independent check renders each unit's templates against its `recover`ed answers and diffs the resulting KEY names against the committed blob: a key in the render but missing from the blob is a warn-only "new env keys available … run `st-cli bootstrap <app> <env>`" advisory; a key in the blob unknown to the templates is an info-only "custom vars or leftovers; remove only if you did not add them" note. Both are best-effort (conditional `{% if %}` keys are not detected) and never gate anything. `preflight` (single pair, `deploy`) is materialize-only now: renders scaffolding + installs the pinned collection (deploy needs it anyway) — the deploy hard gate calls `check_app` directly, before it, not through `preflight`; `preflight_all` (sweep, `doctor`) does **neither** — doctor is fully offline. Never touches the committed tree. The old `argument_specs.yml` unknown-var check is GONE (it flagged deliberate hand-edits in a tree we invite operators to edit). |
| `core/envblob.py` | Pure text merge primitive for the dotenv bodies inside `st_*_env` literal blocks. `parse`/`keys`/`merge(existing, rendered, marker)`. Existing lines (incl. comments + operator-only keys) are kept **verbatim, in place**; new rendered keys are appended under `marker`; **nothing is ever deleted**. `merge(x, x, m) == x` — the fixed point the rebootstrap round-trip depends on. No I/O, no st_cli imports. |
| `core/recover.py` | The inverse of `envrender.render_env` + `writer.apply_component_vars`: rebuilds a bootstrap `answers` dict from a committed unit. `recover`, `recover_cadvisor`, `recover_hosts`, `recover_oidc`, `recover_shared`. Values come back **verbatim** (`{{ vault_* }}` / lookup refs included) — that is what lets a rebootstrap skip re-prompting secrets. Blob-parsed values beat the `{PLACEHOLDER}` component-var inversion. Best-effort: never raises, a gap just means fewer pre-fills. Deliberately app-agnostic (no `if app == …`). |
| `core/upgrades.py` | Loads `resources/upgrades.yml` (release → apps needing a rebootstrap, plus per-flag `interactive`/`new_components`) and matches it against each unit's `bootstrapped_with` stamp. `parse_version` (tolerant `X.Y.Z`), `load_flags`, `load_baseline`, `needed(m, app, env)` → `UpgradeNeed` (carries `interactive`; a stamp below `baseline` gets one synthetic `interactive=True` flag). `newest_per_unit` collapses to the newest flag per `(app, env, component)`, OR-ing `interactive` across the collapsed set so an older interactive flag still forces a full replay. `offerable_components(app) -> set[str]`: the component keys a flag's `new_components` may legally name for `app` — every `dependencies[].on` target, minus `meet`'s `egress` (bundled into the `livekit` step, never its own dep-loop iteration). `new_component_offers(m, app=None, env=None) -> list[NewComponentOffer]`: offers `(app, env, comp)` from a flag's `new_components` when `comp` is one of `app`'s `offerable_components` with no unit yet for that triple and the pair's oldest non-external stamp is below the flag version — the same flag that triggers the offer also flags the pair's existing units, so the offer goes quiet once a replay stamps them past the flag version; a decline registers nothing. A missing stamp reads as `0.0.0`, so pre-feature repos get flagged once — no backfill code. |
| `core/envrender.py` | Renders per-component env blobs from `templates/env/*.j2` + bootstrap answers. `render_env(app, component, answers) -> {blob_var: text}`. `oidc_endpoints(provider, base_url, realm)` derives OIDC OP URLs. Tolerant `_EmptyUndefined` → missing keys render as `""`. |
| `core/generate.py` | Renders trashable `.st-cli/` scaffolding: `ansible.cfg`, `galaxy-requirements.yml`, one `playbooks/<app>-<env>-<component>.yml` per unit. Appends `.st-cli/`, `.vault-pass` to `.gitignore`. `ST_CLI_COLLECTION_SOURCE` → installs local tarball/dir instead of the pinned git tag. |
| `core/manifest.py` | Reads/writes `.st-cli.yml` (committed: version pins + units). `upsert_unit` replaces by `(app,env,component)`. `managed_units` returns non-external units sorted by `deploy_order`. `ssh_user()`: `ST_CLI_SSH_USER` env (if set) else `None` (defers to ssh config chain). No `root` default; old `ansible_user`/`ST_CLI_ANSIBLE_USER` no longer read. |
| `core/models.py` | Pure dataclasses, no I/O: `Component` (frozen), `UnitState`, `StCliManifest`. |
| `core/paths.py` | Filesystem helpers anchored at `Path.cwd()` (deployment repo root, **not** this collection repo). Owns **all** path computation: `.st-cli/` scaffolding paths + committed config-tree paths + `ssh/` paths. No path building elsewhere. |
| `core/prompts.py` | Shared questionary input primitives (`_ask`, `_text_question`, `_password`, `_confirm`, `_ask_select`, `_ask_hosts`, `_is_valid_host`). In `core` so `secretbackend.py` can prompt without importing up into `cmd`. `cmd/bootstrap.py` re-exports these. Input counterpart to `core/ui.py`. Also owns the **silent-replay machinery**: `Recovered(str)` marks a recovery-derived default; `silent_replay()` (context manager, yields `ReplayStats(auto, asked)`) and `suspend_silent()` (temporarily lifts silent mode, for a fresh provider's sub-questionnaire) toggle a module-level flag read by `in_silent_replay()`. Each primitive checks that flag: `_ask`/`_ask_hosts` auto-accept a non-empty `Recovered` default, and `_ask` also auto-accepts ANY `required=False` default (blank is always a safe answer for an optional field — without this a blank optional, never written to the blob, would be re-asked on every silent replay); a plain fallback default on a `required=True` prompt still prompts; `_confirm`/`_ask_select` take `auto: bool = True` and auto-accept `default` when silent+`auto` (an `_ask_select` default must also be a valid choice); `_password` never auto-accepts (a recovered secret never reaches it). `_announce_silent_question` prints a one-time "This release asks about new settings:" header before the first real prompt in a silent run. |
| `core/runner.py` | Subprocess wrappers: `galaxy_install(version)`, `play(app,env,component,check,tags,limit)`, `syntax_check`. Sets `ANSIBLE_CONFIG`. Workers reuse the core unit's inventory via `appmeta.files_component`. `play`'s `limit` = ansible `--limit`, fed by `deploy -H`. |
| `core/secrets.py` | `gen_secret` (Django `SECRET_KEY` alphabet), `gen_token` (`token_urlsafe`), `gen_password`. Used by bootstrap. |
| `core/secretbackend.py` | Per-`(app,env)` secret-backend strategy. `SecretBackend` base + `AnsibleVaultBackend` (historical split, byte-for-byte identical) and `HashiVaultBackend` (OpenBao KV-v2, **reference-only**: env blobs carry `{{ lookup('community.hashi_vault.hashi_vault', '<term>') }}` refs, no `vault.yml`, no generation, no writes). `setup_backend` (bootstrap) + `load_backend` (generate) from `manifest.secret_config_for`; `write_common_connection` merges `ansible_hashi_vault_*` into `common.yml`; `hashi_lookup_ref` builds refs. |
| `core/tree.py` | Reads/writes committed config tree (path computation lives in `paths.py`; this is I/O only). Round-trip ruamel `YAML(typ="rt")` preserving comments + `!vault` scalars (`VaultString`). `read_hosts` parses INI inventory → ips (single source of truth); `read_inventory` returns `(alias,ip)` pairs; `find_host` matches `-H` against the **alias** only; `component_inventory` is worker→core-aware. `ensure_common`/`ensure_ssh_scaffold` seed committed `common.yml` + `ssh/` idempotently — never overwrite. |
| `core/ui.py` | Rich console helpers: `info`/`warn`/`error`/`success` (warn/error → stderr). All user-facing output goes through here. `progress_reporter()` yields a thread-safe `_Reporter` (transient live spinner on TTY, plain lines off-TTY) — used by `restart`. `host_header(name, host)` — used by `ps`. |
| `core/upstream.py` | Best-effort "newer version available" check via `@app.callback()`. Highest semver git tag via anonymous `git ls-remote --tags` (3s timeout, cached 6h under `$XDG_CACHE_HOME/st-cli/upstream.json`, `get_latest_cached`); `is_behind(latest)` compares it against `__version__` (`True`/`False`/`None` when unparseable or unknown). If behind, **warns** to run `upgrade` — text branches on `owning_pipx()` (pipx-ownership probe: `pipx_metadata.json` in `sys.prefix`, then PATH lookup; shared with `cmd/upgrade.py`): pipx-owned → `st-cli upgrade`; not pipx-owned (container/pip installs) → `docker pull ghcr.io/suitenumerique/st-cli:latest` first. Never prompts/auto-runs/exits; any failure swallowed. Skipped for `upgrade`/help and when `ST_CLI_NO_UPSTREAM_CHECK` is set. |
| `core/vault.py` | `ansible-vault` wrappers: `ensure_vault_password` (prompts + writes `.vault-pass` chmod 600 + loud "back this up + share with every operator" warning), `is_encrypted`, `encrypt_file`, `decrypt_to_dict`, `edit_file` (interactive `$EDITOR`, inherits terminal). |
| `core/writer.py` | Pure writers for the committed tree, extracted from `cmd/bootstrap.py` (no prompting, no manifest mutation). `vars_header`, `apply_component_vars`, `write_vault`, `write_core`. Shared-rule helpers: `gen_value`, `rule_is_secret`, `rule_label`, `inject_consumer`. |
| `core/errors.py` | `StCliError` — base for all expected failures. `main._run` catches → `ui.error` + `exit(1)`. `runner.RunnerError` subclasses it. |
| `core/sshuser.py` | `ensure_ssh_user` — once-per-process ssh-user guard (module `_checked` flag), called by `deploy` + direct-ssh ops before connecting. No-op when `ST_CLI_SSH_USER` set or ssh config resolves non-local `User` (offline `ssh -G`); else on TTY prompts once, persists `User <x>` to `ssh/config.local`, applies via `ST_CLI_SSH_USER`; off-TTY warns + proceeds. |

## 4. Resources & templating

Bundled under `st_cli/core/resources/`, packaged automatically by hatchling
(`packages = ["st_cli"]`).

### Release flags — `resources/upgrades.yml`

Mapping of `baseline` (the oldest supported bootstrap stamp) + `flags`, the
releases that REQUIRE operators to rebootstrap: `version`, `apps` (list or
`all`), `reason`, `link`, plus two optional per-flag fields:

- `interactive: true` (default `false`) — forces a full pre-filled Modify
  replay instead of `upgrade`'s default silent one. The synthetic baseline
  flag always sets this: a unit that far behind deserves a full review.
- `new_components: [<key>, ...]` — component keys of `apps` that this release
  newly declares; `upgrade`'s replay offers to bootstrap each once
  (`core/upgrades.new_component_offers`). Requires `apps` to be an explicit
  list, never `"all"`. Each key must be one of `apps`'s
  `core/upgrades.offerable_components` — a real `dependencies[].on` target
  (e.g. `livekit`, `mta-in`), since only that menu ever actually asks the
  operator to bootstrap it. `meet`'s `egress` is a `dependencies[].on` entry
  too but is bundled into the `livekit` step and never its own iteration, so
  it is excluded; an `is_worker` component (never a `dependencies[].on`
  target at all) is excluded the same way.

Read by `core/upgrades.py`; surfaced by `doctor`/`upgrade` and enforced by
`deploy`. A unit stamped below `baseline` gets one synthetic flag ("no longer
supported"); a replay is cumulative, so that flag stands in for every pruned
entry. Adding an env var to a template or a var to `apps/*.yml` needs NO entry
— only add one when operators must act. Delete an entry ONLY when you raise
`baseline` to or above its version; never otherwise. CI lints the file
(`tests/test_upgrades.py::TestUpgradeFlagFileLint`): `baseline` and each
`version` must be valid `X.Y.Z` not greater than the shipped CLI version,
every `version` must outrank `baseline`, `apps` must be `"all"` or a non-empty
list of real app names, `reason` must be non-empty, `link` must be present,
`interactive` (when present) must be a bool, and `new_components` (when
present) requires an explicit non-`"all"` `apps` list and must itself be a
non-empty list of strings, each an `offerable_components` member of at least
one listed app.

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
- **Re-running `bootstrap` on an existing unit is a REBOOTSTRAP, not an overwrite**
  (the old "overwrite?" confirms are gone). `recover.recover` seeds `answers` from the
  committed tree, each recoverable prompt pre-fills with it (`_recall` /
  `prompts._ask(default=…)`), and `write_core` **merges** — so custom `st_*` vars,
  comments, `*_env_template` overrides and hand-added `KEY=value` lines all survive.
  **The invariant: an Enter-through rebootstrap leaves the tree byte-identical when
  recovery is complete** (pinned per-app in `tests/test_rebootstrap_flow.py`) —
  recovery is best-effort (`core/recover.py`); an unrecoverable answer falls back
  to a fresh prompt. Two traps to respect when touching the
  questionnaire: (1) any `_confirm`/`_ask_select` that GATES a block must derive its
  default from recovered state, or an Enter-through run silently drops the block (SMTP,
  blobs-offload, DB mode, outbound mode, dep mode); (2) `_ask_secret` must return early
  when the answer is already recovered — re-prompting or regenerating would rotate a
  live secret. `write_vault` merges and skips the write when nothing changed (ansible-vault
  re-salts, so an unconditional write churns `git diff` on every run).
- **Silent-replay rule**: `st-cli upgrade` drives the questionnaire through
  `core/prompts.silent_replay()`, and `_confirm`/`_ask_select` default to
  `auto=True` — a gating confirm/select whose default derives from recovered
  state is auto-accepted with no prompt, by design (trap 1 above is exactly
  what makes this safe). A genuinely NEW boolean/select question must pass
  `auto=False` (it still asks even in silent mode), or the flag that
  introduces it must set `interactive: true` in `resources/upgrades.yml`
  (forces a full Modify replay instead of a silent one). Getting this wrong
  either nags on every silent upgrade (a question replayed forever) or worse,
  silently answers a new decision the operator never saw. Corollary: a
  genuinely new OPTIONAL (`required=False`) question is silently answered
  blank in a silent replay — a release that needs an optional value reviewed
  must ship `interactive: true`.
- **The `Recovered` marker rule**: for a `required=True` prompt,
  `core/prompts.py`'s silent mode only auto-accepts a default wrapped in
  `prompts.Recovered` — a plain `str`/`bool` fallback (e.g. a hardcoded
  `"5432"`) still prompts, even when it happens to match what recovery would
  have produced. (`required=False` prompts auto-accept any default — blank is
  the safe answer for an optional field.) Every recovery-derived
  prompt default (from `_recall`, `_ask_oidc`, `_ask_keycloak`'s `KC_DB_URL`
  parts, drive's reconstructed S3 endpoint/bucket, the livekit egress
  pre-fills, `_ensure_meet_domain`, `_shared_default()`, …) must be wrapped in
  `Recovered(...)`, or a silent upgrade stops to ask a question that should
  have been answered automatically.
- **`doctor` is warn-only, offline and fast**: no collection install, no network. It
  reports which units need a rebootstrap (`upgrades.yml` flag vs the unit's
  `bootstrapped_with` stamp) and, per unit, an offline env-key diff (render the
  templates against the recovered answers, compare KEY names against the committed
  blob): a missing key warns "new env keys available … run `st-cli bootstrap <app>
  <env>`"; an unrecognized key gets an info-only "custom vars or leftovers" note.
  Both diff sides are advisory only and best-effort (conditional `{% if %}` keys
  aren't detected). Exit code stays 0 when clean. Never touches the committed tree.
  `deploy` turns only the rebootstrap flag into a **hard gate** (no override flag,
  no env var) and prints the same env-key advisories, as warnings, once the gate
  passes — the advisories alone never block a deploy.
- **`upgrade` is the only path that realigns the pin and replays flagged units** —
  but it no longer runs `pipx upgrade` itself. Behind upstream, it warns with the
  concrete command (`pipx upgrade st-cli` or `docker pull
  ghcr.io/suitenumerique/st-cli:latest`) and **stops**; the operator re-runs
  `st-cli upgrade` afterwards. This closes the stale-process problem: `upgrade`
  now always runs installed code, never an old process replaying with old
  templates. Once past that gate: realign pin from `importlib.metadata` (not
  frozen `__version__`, lags one run) → silently replay every unit
  `core/upgrades.needed()` flags (`ReplayAction.SILENT`, or `MODIFY` when any
  collapsed flag is `interactive`) → clean scaffolding (`.st-cli/` only;
  repo-root `.vault-pass` preserved). Realign + clean only on a real version
  change; replays still run even when the pin was already aligned (a prior run
  may have realigned but died mid-replay). Does NOT generate/install/doctor.
  **Version source**: `st_cli/__init__.py` `__version__`; `pyproject.toml`
  `version = "0.2.0"` must match.
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
