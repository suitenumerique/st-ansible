# Getting started with st-cli

`st-cli` is a small command-line tool that scaffolds and drives `suitenumerique.st`
Ansible deployments: it writes a versioned config tree, generates the throwaway Ansible
scaffolding, installs the collection, and runs deploy / restart / ps / logs for you.

This guide takes you from **zero to a running `meet` PoC deployment** using the
**container image**, so you don't install Python, Ansible, or anything else on your host.
For the full command reference (all flags, secret backends, native pipx install), see
[`cli/README.md`](../../cli/README.md).

## Prerequisites

- **podman** (rootless, recommended) or **docker** on your host.
- 2 Servers (one for livekit, one for meet) with at least 2 cores, 2G of RAM, 10G of disk space and public IPs attached.
  This example will use [Scaleway Development Instances](https://www.scaleway.com/en/development-instances/).
- 3 Public Subdomains (one for livekit, one for livekit TURN, one for meet).
- A running **ssh-agent** loaded with the key that reaches your servers (`ssh-add -l` should list it).
- Optionally, Proconnect integration credentials. You can create one by connecting at
  [https://partenaires.proconnect.gouv.fr](https://partenaires.proconnect.gouv.fr) and create an application.
  The redirections URLs should be `https://\<your_domain\>/api/v1.0/callback/`
  and `https://\<your_domain\>/api/v1.0/logout-callback/` for logout.

## Step 0 — Spin up PoC infrastructure

> [!CAUTION]
> This is a throwaway proof-of-concept shortcut. **Do not use that in production**,
> use managed/external PostgreSQL, Redis, and S3 (and back them up).

`meet` needs a Reverse-Proxy, PostgreSQL, Redis, and an S3-compatible object store. For a PoC you can run all
four directly on the machine that will host the meet Django app.
LiveKit runs on its **own** host (it binds fixed ports with `network_mode: host` and wants a
dedicated public IP, see [../03-meet/02-livekit.md](../03-meet/02-livekit.md)).

Create 2 [Scaleway DEV1-S / Debian Trixie](https://console.scaleway.com/instance/servers/create/cpu?imageKey=193cdddd-0b0b-43b5-84a6-f0d88ebe7611&zone=fr-par-2&offerName=DEV1-S&distribution=debian) VMs, with public IPs and your ssh-key configured.
For this example the public domains will be `meet.poc.st.fr` pointing at the meet server,
`livekit.poc.st.fr` and `livekit-turn.poc.st.fr` both pointing at the livekit server.

Next, setup Postgresql, Redis, Rustfs and Caddy for the PoC on the `meet.poc.st.fr` VM :
```bash
ssh root@meet.poc.st.fr

export MEET_DOMAIN=meet.poc.st.fr
apt install podman

# PostgreSQL
podman run -d --name pg --network=host \
  -e POSTGRES_USER=meet -e POSTGRES_PASSWORD=meet -e POSTGRES_DB=meet \
  docker.io/library/postgres:18

# Redis
podman run -d --name redis --network=host docker.io/library/redis:8

# RustFS: S3-compatible object store
podman run -d --name rustfs --network=host \
  -e RUSTFS_ACCESS_KEY=meet -e RUSTFS_SECRET_KEY=meet \
  docker.io/rustfs/rustfs:latest
podman run --rm -i --entrypoint /bin/sh docker.io/rustfs/rc:latest <<'EOF'
rc alias set local http://host.containers.internal:9000 meet meet
rc mb local/meet
EOF

# Caddy: Load balancer + TLS offloading
cat > Caddyfile <<EOF
$MEET_DOMAIN {
    reverse_proxy 127.0.0.1:50300
}
EOF
podman run -d --name caddy --network host \
  -v $PWD/Caddyfile:/etc/caddy/Caddyfile:ro \
  docker.io/library/caddy:latest

# Ensure everyone is alright and exit
podman ps -a
# CONTAINER ID  IMAGE                           COMMAND               CREATED         STATUS         PORTS                               NAMES
# e1f983d9f603  docker.io/library/postgres:18   postgres              4 minutes ago   Up 4 minutes   5432/tcp                            pg
# 2f5fa68a5ca6  docker.io/library/redis:8       redis-server          4 minutes ago   Up 4 minutes   6379/tcp                            redis
# 601359a0012a  docker.io/rustfs/rustfs:latest  rustfs                4 minutes ago   Up 4 minutes   9000-9001/tcp                       rustfs
# 7a4b476733ab  docker.io/library/caddy:latest  caddy run --confi...  16 seconds ago  Up 16 seconds  80/tcp, 443/tcp, 2019/tcp, 443/udp  caddy

exit
```

## Step 1 — Add a shell alias

**On your computer**, add the alias to your `~/.bashrc` or `~/.zshrc`.

**Podman (recommended):**

```bash
alias st-cli='podman run --rm -ti --userns=keep-id \
  -v "$(pwd):/st-cli" \
  -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent \
  ghcr.io/suitenumerique/st-cli:latest'
```

or **Docker:**

```bash
alias st-cli='docker run --rm -ti \
  -v "$(pwd):/st-cli" \
  -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent \
  ghcr.io/suitenumerique/st-cli:latest'
```

The alias is single-quoted on purpose. Reload your shell (`exec $SHELL`) or open a new
terminal, then check it works:

```bash
st-cli --help
```

## Step 2 — Create your deployment repo

st-cli operates on the **current directory** (bind-mounted at `/st-cli` inside the
container), so always run it from the root of your deployment repo.

```bash
mkdir meet-poc && cd meet-poc
git init
```

## Step 3 — Bootstrap `meet` PoC

`bootstrap` is an interactive questionnaire to setup hosts, domain, database, OIDC provider, S3,
and each dependency. Run it from your repo root:

```bash
st-cli bootstrap meet poc
```

A typical run looks like this:

```text
╭───────────────────────────────────────────────────────────── Bootstrap ─────────────────────────────────────────────────────────────╮
│ Read how meet is architected before you start:                                                                                      │
│   https://github.com/suitenumerique/st-ansible/tree/main/docs/03-meet                                                               │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─────────────────────────────────────────────────────────── Requirements ────────────────────────────────────────────────────────────╮
│ Depending on the app, make sure you've prepared:                                                                                    │
│   • IP or hostname of the VM(s)                                                                                                     │
│   • PostgreSQL host and credentials                                                                                                 │
│   • Redis host and credentials                                                                                                      │
│   • S3 endpoint, bucket and credentials                                                                                             │
│   • Identity provider URLs and credentials                                                                                          │
│     (For ProConnect Integration environment: create an app at https://partenaires.proconnect.gouv.fr/)                              │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
? Do you have all of the above ready to continue? Yes
╭─────────────────────────────────────────────────────────────── Note ────────────────────────────────────────────────────────────────╮
│ This questionnaire only scaffolds your config files.                                                                                │
│ If you mistype an answer, don't start over: finish the questionnaire, then edit the generated files directly under                  │
│ <app>/<env>/<component>/.                                                                                                           │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
? Secret backend: ansible-vault — secrets encrypted locally with a generated password

st-cli generated a random ansible-vault password.
 ⚠  BACK UP YOUR VAULT PASSWORD  ⚠
Stored at ./.vault-pass (gitignored).
Every operator of this repo needs this exact file — share it securely
(password manager / secrets tool), never commit it.
If you lose it, every encrypted secret in this repo is unrecoverable.

› Bootstrapping meet/poc.
? meet host(s) — IP or hostname, comma-separated meet.poc.st.fr
? Public domain for meet meet.poc.st.fr
? Database configuration: discrete (DB_*)
? DB_HOST host.containers.internal
? DB_NAME meet
? DB_USER meet
? DB_PASSWORD ****
? DB_PORT 5432
? REDIS_URL (redis://[user:password@]host:port/db) redis://host.containers.internal:6379
? AWS_S3_ENDPOINT_URL http://host.containers.internal:9000
? AWS_S3_ACCESS_KEY_ID meet
? AWS_S3_SECRET_ACCESS_KEY ****
? AWS_STORAGE_BUCKET_NAME meet
? AWS_S3_REGION_NAME (optional) us-east-1
? Identity provider: proconnect-integ
? OIDC_RP_CLIENT_ID <REDACTED>
? OIDC_RP_CLIENT_SECRET ****************************************************************
? Configure transactional email (SMTP) settings? No
? Enable cadvisor container monitoring for meet? Yes

› Bootstrapping livekit/poc (dependency of meet).
? Bootstrap livekit now? Yes — bootstrap now
? livekit host(s) — IP or hostname, comma-separated livekit.poc.st.fr
? egress (leave blank to co-locate on the livekit hosts) host(s) — IP or hostname, comma-separated
› livekit: generated LIVEKIT_API_KEY.
› livekit: generated LIVEKIT_API_SECRET.
? LiveKit domain (e.g. livekit.example.org) livekit.poc.st.fr
? LiveKit TURN domain (e.g. turn.example.org) livekit-turn.poc.st.fr
? Enable cadvisor container monitoring for livekit? Yes
? Enable cadvisor container monitoring for egress? Yes
✓ livekit: managed — wrote vars.yml + vault.yml + hosts.
✓ meet: wrote vars.yml + vault.yml + hosts.
✓ Bootstrapped meet/poc.
›   domain: meet.poc.st.fr
›   OIDC provider: proconnect-integ
›   - livekit       hosts=livekit.poc.st.fr
›   - egress        hosts=livekit.poc.st.fr
›   - meet          hosts=meet.poc.st.fr
╭──────────────────────────────────────────────────────────── Next steps ─────────────────────────────────────────────────────────────╮
│ 1. Back up and share .vault-pass with the other operators.                                                                          │
│ 2. Review meet/poc/*/vars.yml and meet/poc/*/hosts.                                                                                 │
│ 3. Review secrets with `st-cli secrets meet poc`.                                                                                   │
│ 4. Deploy with `st-cli deploy meet poc`.                                                                                            │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Bootstrap writes, into your repo:

- `meet/poc/<component>/{vars.yml,vault.yml,hosts}` (**committed**) — `vars.yml`
  contains the plaintext variables, `vault.yml` is `ansible-vault`-encrypted,
  and `hosts` is the inventory.
- `.st-cli.yml` (**committed**) — collection/CLI version pin, the component list, and the
  secret-backend choice.
- `.vault-pass` (**gitignored**) — your vault password. **Back it up** —
  losing it makes every encrypted secret unrecoverable.
- `ssh/config` + `ssh/known_hosts` (**committed**) — the shared SSH client config, seeded
  with commented examples (see next step).

> [!CAUTION]
> At this point if you lose the `.vault-pass` password your repository
> is unusable and you have to bootstrap all over again.
>
> **Backup the password**.

## Step 4 — Configure `ssh/config`

`st-cli` populates a `ssh` directory with empty configuration files that will be used
by the `st-cli` container. Edit `ssh/config` to add any bastion / `ProxyJump` you need to reach your servers:

```text
Host bastion
    HostName bastion.example.org

Host 10.0.0.*
    ProxyJump bastion
```

If you have dedicated ssh users by operators, configure them in the dedicated
`ssh/config.local` which is gitignored:

```text
Host *
  User root
```

## Step 5 — Deploy

Install the pinned collection and run the playbooks. The first deploy provisions the base
(podman + the app user) and needs root on the target once:

```bash
st-cli deploy meet poc
```

Useful follow-ups:

```bash
st-cli deploy meet poc -n   # --dry-run: ansible --check --diff, changes nothing
st-cli deploy meet poc -d   # --deploy-only: app phase only, can be used for every future updates
```

At this point, if all the vars are correctly set and everything is wired correctly,
you should be able to go to [https://meet.poc.st.fr](https://meet.poc.st.fr), connect and create a room.

## Step 6 —  Troubleshooting

Confirm the containers came up (`podman ps -a` across every managed host):

```bash
st-cli ps meet poc
```

Tail the logs:

```bash
st-cli logs meet poc --since '3 min ago'  # Last 3 minutes logs of the meet django containers
st-cli logs meet poc -f                   # --follow
st-cli logs meet poc -c livekit           # Target the livekit containers
```
