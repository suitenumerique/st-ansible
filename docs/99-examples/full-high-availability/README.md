# High-Availability Deployment

> [!WARNING]
> This setup is provided as an example to illustrate how the collection works.
> It is not meant to be used as-is in production. Adjust images, passwords, TLS settings,
> and backup strategies to your needs.

> [!IMPORTANT]
> This setup requires external Redis, external PostgreSQL, external OpenSearch and external S3 make sure they are
> provisioned and reachable before running the playbooks. The setup also requires an external reverse proxy,
> out of scope of this collection. If that seems too complicated for your needs, use PaaS platforms such as [Scalingo](https://scalingo.com).

Each service runs on two or more hosts behind an external load balancer, connected to
external databases and storage. The socks-proxies hold their own public IPs (one per host)
for outbound SMTP. LiveKit also has its own public IP for WebRTC traffic.

```text
  Internet
     │
     ├─── External LB (:80/:443, :25)
     │      │
     │      ├── keycloak (1+2)
     │      ├── drive (1+2)
     │      │     └── workers (1)
     │      ├── collabora (1)
     │      ├── messages (1+2)
     │      │     ├── workers (1)
     │      │     └── mpa (1)
     │      ├── mta-in (1+2)
     │      └── meet (1+2)
     │
     ├─── socks-proxy (1+2, public IPs)
     │
     └─── livekit (1, public IP)
```

## External Services

| Service | PostgreSQL | Redis | OpenSearch | S3 |
|---------|:----------:|:-----:|:----------:|:--:|
| keycloak | x | | | |
| messages | x | x | x | x |
| messages workers | x | x | x | x |
| mta-in | | | | |
| mpa | | | | |
| socks-proxy | | | | |
| drive | x | x | | x |
| drive workers | x | x | | x |
| collabora | | | | |
| meet | x | x | | x |
| livekit | | x | | |

> [!NOTE]
> In this example LiveKit is deployed as a single host with its own valkey (redis-compatible) as part of the
> compose stack and no external Redis is needed. However if you want high availability for livekit too, you should
> take a look at the [meet/multi-host](../meet/multi-host) example.

## Rolling Updates

All HA playbooks use `serial: 1` so hosts are updated one at a time. Rollback and database
migrations only run on the first host in the play:

```yaml
st_messages_rollback_enabled: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
st_messages_backend_run_migrations: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
```

If the first host fails, it rolls back to the previous version. If the second host fails, the
first host stays on the new version and the second can be debugged manually.

## Playbooks

| Playbook | Group | Hosts | Serial | Role |
|----------|-------|-------|--------|------|
| playbook_keycloak.yml | keycloak | 2 | 1 | keycloak |
| playbook_messages.yml | messages | 2 | 1 | messages |
| playbook_mtain.yml | mta_in | 2 | 1 | messages (mta-in) |
| playbook_messages_workers.yml | messages_workers | 1 | - | messages (workers) |
| playbook_socks_proxy.yml | socks_proxy | 2 | 1 | messages (socks-proxy) |
| playbook_mpa.yml | mpa | 1 | - | messages (mpa) |
| playbook_drive.yml | drive | 2 | 1 | drive |
| playbook_drive_workers.yml | drive_workers | 1 | - | drive (workers) |
| playbook_collabora.yml | collabora | 1 | - | drive (collabora) |
| playbook_meet.yml | meet | 2 | 1 | meet |
| playbook_livekit.yml | livekit | 1 | - | meet (livekit) |

## Running

```bash
# Install the collection
ansible-galaxy collection install -r galaxy-requirements.yml --force

# Run a single playbook
ansible-playbook -i hosts playbook_messages.yml

# Run all playbooks
ansible-playbook -i hosts playbook_*.yml
```
