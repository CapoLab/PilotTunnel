# PilotTunnel

PilotTunnel is a server-only Python CLI project for managing multiple tunnel adapters behind a stable public port. There is no UI, no dashboard, and no frontend. Version `v0.1` remains focused on safe Layer 4 orchestration with dry-run behavior by default.

## Current Status

- Layer-first, adapter-based, and profile/port-based architecture is in place.
- Only `layer4` is active in `v0.1`; other layers remain listed as metadata and are intentionally blocked.
- Backhaul and Rathole now have richer dry-run planning for `controller/iran` and `worker/foreign` roles.
- Real remote coordination, real systemd changes, firewall rules, and host networking changes are still not implemented.

## Dry-Run Safety Model

- Default behavior stays dry-run unless `--apply` is passed.
- Dry-run planning renders deterministic config and systemd unit content without executing binaries.
- The switch engine still enforces lock, stop-old, cleanup, registry validation, start-new plan, healthcheck stub, commit, and rollback behavior.
- No real systemd, iptables, nftables, routes, or interfaces are changed in `v0.1`.

## Layer 4 Scope

- Supported practical dry-run flows:
  - `backhaul tcp`
  - `backhaul tcpmux`
  - `backhaul ws`
  - `rathole tcp`
- Backhaul metadata also includes planned transports such as `utcpmux`, `uwsmux`, `tcptun`, and `faketcptun`.
- Experimental TUN-style Backhaul transports are blocked from active switching in `v0.1`.

## Example Commands

```bash
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --audit-log ./tmp/audit.log --lock-dir ./tmp/locks --work-dir ./tmp/work init
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json profile create --name turkey-6221 --main-port 6221 --target-port 5201 --role controller --control-port 7001 --service-port 7002 --check-port 7003 --candidate backhaul:tcp --candidate backhaul:tcpmux --candidate rathole:tcp
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json adapter list
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile turkey-6221 --adapter rathole --transport tcp
```

## What Is Implemented

- Role-aware profile config with `controller/iran` and `worker/foreign` normalization.
- Profile safety settings and explicit `main/control/service/check` port ownership.
- Port mapping parser and validation for common mapping shapes.
- Dry-run Backhaul and Rathole config/systemd generation with deterministic service names.
- Registry ownership tracking for ports, services, firewall tags, and routes.
- Audit entries that capture dry-run switch metadata while redacting secrets.

## What Is Still Dry-Run Only

- Real Backhaul or Rathole binary install/execution.
- Real systemd unit deployment outside explicit render targets.
- Real firewall, route, interface, or SSH/API-based worker coordination.
- Real tunnel health verification beyond local-only stubs.
