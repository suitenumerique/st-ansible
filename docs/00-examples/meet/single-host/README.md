# Single-Host LiveKit

> [!WARNING]
> This setup is provided as an example to illustrate how the collection works.
> It is not meant to be used as-is in production. Adjust images, passwords, TLS settings,
> and backup strategies to your needs.

> [!IMPORTANT]
> This example only deploys the **LiveKit** video conferencing server. The **Meet** application
> (frontend + backend) should not be deployed on a single host in production, so it's out of
> the current collection's scope. For production Meet deployments, use PaaS platforms such as [Scalingo](https://github.com/suitenumerique/meet/blob/main/docs/installation/scalingo.md)
> or see the [multi-host example](../multi-host/).

> [!IMPORTANT]
> LiveKit uses `network_mode: host` because of the wide range of UDP ports required.
> You should always deploy LiveKit on a **dedicated host**. As DNAT is unreliable with
> WebRTC, LiveKit must also be deployed **with a dedicated public IP** bound to the host.

This is the simplest way to get LiveKit running. It uses the role's default compose and
templates: embedded valkey, built-in caddy-l4, and all services on a single host.

## Running

```bash
# Install the collection
ansible-galaxy collection install -r galaxy-requirements.yml --force

# Deploy livekit
ansible-playbook -i hosts playbook_livekit_single.yml
```

## Key Variables

| Variable | Description |
|----------|-------------|
| `st_meet_livekit_domain` | LiveKit server domain (used for TLS/SNI) |
| `st_meet_livekit_turn_domain` | TURN server domain |
| `st_meet_livekit_api_key` | LiveKit API key |
| `st_meet_livekit_api_secret` | LiveKit API secret |
| `st_meet_livekit_tag` | LiveKit server image tag |
| `st_meet_livekit_egress_tag` | LiveKit egress image tag |
| `st_meet_livekit_caddyl4_tag` | caddy-l4 image tag |
| `st_meet_livekit_valkey_tag` | valkey image tag |
