# STE-26: NetFoundry and OpenZiti in the Home Lab

**Status:** Research complete — recommended for homelab POC  
**Linear issue:** [STE-26](https://linear.app)  
**Project:** Stephen Quinlan Homelab Research

## Executive Summary

**Yes — OpenZiti is a strong fit for a home lab.** NetFoundry is the commercial product built on top of the same technology. For learning, proof of concept, and non-production homelab use, **self-hosted OpenZiti (community edition) is the right starting point**. NetFoundry NaaS or licensed self-hosted options are worth evaluating later if you need SLAs, compliance certifications, or managed operations.

| Option | Best for homelab? | Cost | Notes |
|--------|-------------------|------|-------|
| **OpenZiti (self-hosted, OSS)** | **Yes — start here** | Free | Community support; full zero-trust overlay you control |
| **NetFoundry NaaS** | Optional later | Commercial trial | Managed controller/routers, SLAs, enterprise console |
| **NetFoundry self-hosted licensed** | Enterprise/regulated only | Commercial | Same infra you run, plus vendor support and FIPS |

## What Are NetFoundry and OpenZiti?

NetFoundry invented, open-sourced, and maintains **OpenZiti** — a zero-trust overlay network where **identity, not IP address**, controls access. Services stay "dark" (no open inbound ports); authorized clients dial out to reach them through encrypted tunnels.

- **OpenZiti** = open-source software (controller, routers, tunnelers, SDKs)
- **NetFoundry** = commercial products (managed NaaS, licensed self-hosted, enterprise support)

For a homelab thesis or CSA automation POC, OpenZiti delivers the core capabilities without commercial licensing.

## Core Architecture (What the Pieces Do)

```
┌─────────────────────────────────────────────────────────────────┐
│                     OpenZiti Overlay Network                    │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │  Controller  │───▶│    Router    │◀───│  Edge Router     │  │
│  │  (policy,    │    │  (fabric)    │    │  (optional, for  │  │
│  │   identities)│    │              │    │   remote sites)  │  │
│  └──────────────┘    └──────────────┘    └──────────────────┘  │
│         │                    ▲                    ▲           │
│         │ enroll/authorize   │ mTLS fabric       │              │
│         ▼                    │                   │              │
│  ┌──────────────┐    ┌──────┴───────┐    ┌──────┴───────┐      │
│  │   Identity   │    │   Tunneler   │    │   Tunneler   │      │
│  │  (client cert)│    │  (host svc)  │    │  (dial svc)  │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
└─────────────────────────────────────────────────────────────────┘
         │                    │                   │
    VLAN / Docker         Splunk UI           Admin laptop
    homelab hosts         on idx1:8000        dials Splunk
```

| Component | Role in homelab |
|-----------|-----------------|
| **Controller** | Central brain: identities, services, policies, PKI. One per overlay (can run in Docker on Proxmox/RPi). |
| **Router** | Data plane — routes encrypted traffic between identities. Quickstart bundles one with the controller. |
| **Tunneler (host)** | Runs where a service lives; advertises a service (e.g. Splunk `:8000`) into the overlay without opening firewall ports. |
| **Tunneler (client)** | Runs on admin devices; dials authorized services by name (e.g. `splunk-homelab.ziti`). |
| **ZAC** | Web UI at `https://<controller>:1280/zac/` for managing identities and policies. |

**Tunneler placement:** You do not need a tunneler sidecar per Docker container. One tunneler per **host** (or VM) that hosts services is usually enough. It can proxy multiple local services.

## Homelab Use Cases

### 1. Cross-VLAN access without flat firewall rules

Replace "allow VLAN X → homelab on all ports" with identity-based policies:

- Parents VLAN identity → no homelab services
- Personal VLAN identity → Jellyfin + Immich only
- Admin identity → Splunk, Proxmox, SSH

### 2. Secure remote access without port forwarding

Dial Splunk, Proxmox, or SSH through the overlay. No inbound ports on the MikroTik router.

### 3. Splunk lab integration (this repo)

The existing `docker-compose.yaml` Splunk cluster (`192.168.1.0/24` macvlan) can be exposed via a Ziti host tunneler on the Docker host:

- **Service:** `splunk-dp1` → `dp1:8000` (deployer UI)
- **Service:** `splunk-sh1` → `sh1:8000` (search head)
- **Policy:** only `admin` identity may dial

### 4. External IdP (Authentik, Keycloak)

Supported via **external JWT signer** (OIDC). Recommended learning path:

1. Get basic SSH or HTTP service working **without** IdP first
2. Add Authentik/OIDC as external JWT signer
3. Tunnelers use callback `http://localhost:20314/auth/callback`

Do not terminate TLS at nginx in front of Ziti tunnelers — it breaks mutual TLS.

## Recommended POC Path

### Phase 1: Throwaway quickstart (30 minutes)

```bash
# Install Ziti CLI (Linux amd64 example)
curl -sL https://get.openziti.io/install.bash | bash

# Ephemeral overlay — vanishes when you stop the process
ziti edge quickstart \
  --ctrl-address ziti.homelab.local \
  --router-address ziti.homelab.local
```

Add DNS (Pi-hole): `ziti.homelab.local` → controller VM IP.

### Phase 2: Persistent overlay (homelab default)

Use the Docker Compose POC in this repo:

```bash
cd openziti
cp .env.example .env
# Edit ZITI_CTRL_ADVERTISED_ADDRESS to your FQDN
docker compose up -d
```

- **ZAC:** `https://<ZITI_CTRL_ADVERTISED_ADDRESS>:1280/zac/`
- **Default password:** `admin` (change immediately)

### Phase 3: First service — SSH between VLANs

1. Create an identity for a client (`ziti edge create identity admin`)
2. Enroll the client (`ziti edge enroll ...`)
3. Create a service for SSH on a homelab host
4. Create dial/bind policies linking identity ↔ service
5. Run `ziti tunnel host` on server, `ziti tunnel run` on client

### Phase 4: Splunk over Ziti

1. Host tunneler on Docker host pointing at `192.168.1.10:8000` (dp1)
2. Dial policy for admin identity only
3. Access Splunk UI via Ziti DNS name — no macvlan route required from client VLAN

## OpenZiti vs Alternatives (homelab context)

| Solution | Zero-trust model | IdP support | Homelab complexity |
|----------|------------------|-------------|-------------------|
| **OpenZiti** | Identity + overlay, dark services | OIDC external JWT | Medium — worth learning |
| **Tailscale** | WireGuard mesh + ACLs | SSO on paid plans | Low — great for personal remote access |
| **Headscale** | Self-hosted Tailscale control plane | Limited | Low–medium |
| **VPN (WireGuard/OpenVPN)** | Network-level trust | Varies | Low — not true zero trust |

OpenZiti shines when you want **service-level policies**, **no inbound ports**, and a path to **enterprise zero-trust patterns** relevant to CSA customer engagements.

## CSA Business Relevance

Patterns learned in the homelab translate directly to customer conversations:

| Homelab learning | Customer value |
|------------------|----------------|
| Identity-based service policies | Replace VPN sprawl; least-privilege app access |
| Dark services (no public endpoints) | Reduce attack surface for Splunk, OT, internal APIs |
| OIDC / external IdP integration | Enterprise SSO (Okta, Entra ID, Keycloak) |
| Overlay across VLANs/sites | Hybrid cloud, branch, and IoT segmentation |
| Automation via Ziti CLI/API | IaC for zero-trust (Terraform, Ansible candidates) |

A homelab POC demonstrating Splunk access over OpenZiti — with Authentik OIDC — is a credible demo for CSA "secure observability platform access" narratives.

## When to Consider NetFoundry (Commercial)

Move from OpenZiti OSS to NetFoundry when you need:

- 99.95% SLA and 24×7 vendor support
- SOC 2 / FedRAMP / HIPAA compliance documentation
- Managed PKI, billing/metering, multi-tenant console
- Global PoP fabric without operating routers yourself

For homelab and internal POC, none of these are required.

## References

- [OpenZiti Get Started](https://netfoundry.io/docs/openziti/get-started/)
- [Set up a network (quickstarts)](https://netfoundry.io/docs/openziti/latest/get-started/network/)
- [Comparing NetFoundry and OpenZiti](https://netfoundry.io/ziti-openziti/comparing-netfoundry-and-openziti/)
- [Configuring OIDC / external auth](https://netfoundry.io/docs/openziti/how-to-guides/external-auth/)
- [Homelab discourse thread (Feb 2026)](https://openziti.discourse.group/t/openziti-overlay-for-simple-homelab/5541)
- [Docker all-in-one quickstart](https://github.com/openziti/ziti/blob/main/quickstart/docker/all-in-one/README.md)

## Recommendation

| Decision | Recommendation |
|----------|----------------|
| Use in homelab? | **Yes** — OpenZiti OSS |
| Start with NetFoundry NaaS? | No — overkill for learning |
| First milestone | Persistent Docker quickstart + SSH or Splunk service |
| IdP integration | Phase 2, after basic dial/host works |
| Repo artifact | `openziti/docker-compose.yml` + this document |
