# Architecture

PilotTunnel is a server-only Python CLI for safety-first Layer 4 tunnel management. The current development line focuses on guarded operator workflows, not background automation.

## Core Model

- `controller` and `worker` are the only public node roles.
- profiles define operator intent for ports, target endpoints, adapters, and transport candidates.
- Layer 4 is the only runnable layer in `v0.1`; other layers remain planned-only metadata.
- adapters remain pluggable so each protocol family can evolve without rewriting the whole CLI.

## Runtime Files

- config stores node settings, profile definitions, and binary resolution preferences.
- state stores runtime selections such as active adapter, active transport, and binary import metadata.
- registry tracks declared ownership of ports and related managed resources.
- audit logs record guarded actions and dry-run planning details.

## Adapter Model

- adapters live under `pilottunnel/adapters/`
- each adapter exposes metadata, config rendering, and service/unit planning hooks
- runtime planning resolves adapter binaries and renders configs before any host-affecting step is allowed

## Binary Provider Model

- upstream source review is an admin-side workflow
- provider manifests pin exact versions, filenames, URLs, and SHA256 values
- server-side bootstrap and binary download use only a provider manifest, not dynamic upstream releases
- managed binary resolution supports local cache and managed install directories without executing binaries during bootstrap

## Bootstrap Flow

1. initialize node role
2. record preferred layer
3. inspect or download provider-managed binaries
4. run readiness checks
5. render runtime and service plans
6. apply guarded install or lifecycle commands only when explicitly confirmed

## Safety Boundaries

- no UI, dashboard, or frontend
- no implicit firewall, route, or interface mutations
- no uncontrolled service lifecycle changes
- no hidden background monitoring or auto-switching in `v0.1`
