# Changelog

## 0.1.1-dev - 2026-06-20

- Bumped project metadata to the post-`v0.1.0` development stage.
- Added a Linux `scripts/install.sh` helper for safe one-command non-production bootstrap and smoke-test preparation.
- Kept installer behavior confirmation-gated, with no default writes to live systemd targets, no service lifecycle changes, no firewall changes, and no tunnel binary execution.

## 0.1.0 - 2026-06-20

- First public v0.1.0 release of the unified `pilottunnel` CLI.
- Added safe workflows for upstream binary source review, provider manifest preparation, managed binary install, runtime planning, service rendering, service install planning, guarded systemd reload and status inspection, guarded service start and stop, guarded manual switch with rollback, and RC validation.
- Kept the release scoped to Layer 4 TCP planning and guarded operator workflows.
- Kept release behavior generic and CLI-only, without UI, automatic failover, or background monitoring.
- Preserved dry-run and guarded confirmation patterns for host-affecting workflows.
