# OpenZiti Homelab POC

Docker Compose deployment for [STE-26](../docs/research/STE-26-netfoundry-openziti-homelab.md) research.

## Prerequisites

- Docker and Docker Compose
- A local DNS name (e.g. via Pi-hole) pointing to this host: `ziti.homelab.local`

## Quick Start

```bash
cp .env.example .env
# Edit .env — set ZITI_CTRL_ADVERTISED_ADDRESS to your FQDN
docker compose pull
docker compose up -d
```

Open **ZAC** (Ziti Admin Console): `https://ziti.homelab.local:1280/zac/`  
Default login password: `admin` (change after first login).

## CLI inside the container

```bash
docker compose exec quickstart bash
ziti edge list identities
```

## Next steps

See the [research doc](../docs/research/STE-26-netfoundry-openziti-homelab.md) for:

- Architecture overview
- Splunk lab integration
- OIDC / Authentik setup
- Recommended learning phases
