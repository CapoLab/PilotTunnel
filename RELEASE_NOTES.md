# PilotTunnel v0.1.0 Release Notes

## Release Summary

PilotTunnel `0.1.0` is a server-only, unified CLI release focused on safe operator-controlled Layer 4 tunnel planning and guarded service workflows. This release does not add automatic failover, background monitoring, or any UI surface.

## Supported Scope

- CLI only
- Config-file driven workflows
- Layer 4 TCP only
- Selected adapters only
- One active tunnel
- Up to two hot-standby tunnels
- Config-only for remaining tunnels
- Guarded manual switch with rollback support
- RC validation with `rc check` and `rc smoke`

## Operator Checklist

1. Clone or pull the repository into a local working directory.
2. Initialize the node role with `python -m pilottunnel.cli init --role controller` or `python -m pilottunnel.cli init --role worker`.
3. Inspect upstream binary sources with `python -m pilottunnel.cli binary source list`.
4. Fetch source binaries into `<SOURCE_DIR>` and prepare a provider manifest at `<MANIFEST_FILE>`.
5. Install managed binaries into `<INSTALL_DIR>`.
6. Render a runtime plan into `<RUNTIME_DIR>`.
7. Render staged service files into `<SERVICE_STAGING_DIR>`.
8. Review and apply a staged service install into `<SYSTEMD_TARGET_DIR>` only when ready.
9. Run guarded daemon reload and guarded start or stop commands only when the staged install is already in place.
10. Use guarded manual switch planning before any active tunnel change.
11. Run `rc check` and `rc smoke` before real deployment changes.
12. Keep backup and restore procedures available before any production rollout.

## Known Limitations

- No automatic failover or full auto-switch
- No background monitor or always-on daemon
- No UI
- Real deployment still requires explicit operator confirmation
- Production rollout should begin with a non-production smoke test

## Safety Notes

- No new runtime behavior is introduced by this release metadata update.
- The operator workflows remain dry-run or confirmation-gated by default.
- Real service lifecycle changes are still guarded and must be triggered explicitly by the operator.
