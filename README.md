# PilotTunnel

PilotTunnel is a safety-first Python CLI for server-side Layer 4 tunnel operations. It helps operators manage controller and worker roles, profile-driven planning, provider-managed binaries, runtime rendering, and guarded service workflows without adding any UI, dashboard, or hidden automation.

## Current Status

- Layer-first, adapter-based, and profile/port-based architecture is in place.
- Only `layer4` is active in `v0.1`; other layers remain listed as metadata and are intentionally blocked.
- Backhaul and Rathole now have richer dry-run planning for `controller` and `worker` roles.
- Real remote coordination, real systemd changes, firewall rules, and host networking changes are still not implemented.

## What PilotTunnel Does

- Plans and renders Layer 4 tunnel workflows through a single CLI
- Keeps controller and worker behavior explicit and role-aware
- Tracks config, state, registry, and audit data separately
- Uses pinned provider manifests for managed binary download workflows
- Preserves guarded confirmation steps for host-affecting operations

## What PilotTunnel Does Not Do

- No UI, dashboard, or frontend
- No automatic failover or background monitoring in `v0.1`
- No hidden firewall, route, or interface changes
- No uncontrolled service lifecycle changes
- No dynamic upstream `latest` binary fetching during install or bootstrap

## Architecture Overview

- Roles: `controller` and `worker`
- Layers: `layer4` runnable now, other known layers planned-only
- Profiles: operator-defined ports, target endpoint placeholders, and adapter candidates
- Binary provider: pinned manifest, exact versions, exact SHA256
- Runtime flow: plan first, inspect readiness, then apply only guarded steps

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/OPERATIONS.md](docs/OPERATIONS.md), [SECURITY.md](SECURITY.md), and [CONTRIBUTING.md](CONTRIBUTING.md) for the public project layout and operating model.

## Current Development Version

- Current project version: `0.1.1-dev`
- Release phase: `post-v0.1-dev`
- CLI only, with config-file driven workflows
- No auto-switch or background monitoring is included in the current development stage
- Release notes are available in [RELEASE_NOTES.md](RELEASE_NOTES.md)
- Change history is summarized in [CHANGELOG.md](CHANGELOG.md)
- The tagged `v0.1.0` release remains the stable reference point while the installer/bootstrap workflow evolves on `main`.

## v0.1.0 Supported Scope

- CLI only
- Config-file driven
- Layer 4 TCP only
- Selected adapters only
- One active tunnel
- Up to two hot-standby tunnels
- Config-only for remaining tunnels
- Guarded manual switch with rollback support
- No full auto-switch
- No background daemon
- No UI

## Version Metadata

```bash
python -m pilottunnel.cli version
```

- Prints the project name, version, release phase, supported scope, and safety notes.
- Confirms that auto-switch and background monitoring are not part of the current development stage.

## Quick Start

```bash
bash scripts/install.sh --role controller --layer layer4 --dry-run
bash scripts/install.sh --role controller --layer layer4 --confirm INSTALL_PILOTTUNNEL
bash scripts/install.sh --role worker --layer layer4 --confirm INSTALL_PILOTTUNNEL
curl -fsSL <INSTALLER_URL> | sh -s -- --role controller --layer layer4 --dry-run
curl -fsSL <INSTALLER_URL> | sh -s -- --role worker --layer layer4 --confirm INSTALL_PILOTTUNNEL
```

- The bootstrap helper is Linux-focused and safety-first.
- It defaults to the public source repository and the public provider manifest.
- It does not start services, perform daemon reloads, modify firewall rules, modify routes, or execute adapter binaries during bootstrap.

## Public Safety Guarantees

- Dry-run first where planning output already exists
- Exact confirm tokens for host-affecting apply paths
- Provider-managed binaries must pass manifest and checksum checks
- Public examples use placeholders and generic `controller`/`worker` roles only

## Development Commands

```bash
python -m compileall pilottunnel
python -m unittest discover -s tests -v
git diff --check
```

## Dry-Run Safety Model

- Default behavior stays dry-run unless `--apply` is passed.
- Dry-run planning renders deterministic config and systemd unit content without executing binaries.
- The switch engine still enforces lock, stop-old, cleanup, registry validation, start-new plan, healthcheck stub, commit, and rollback behavior.
- No real systemd, iptables, nftables, routes, or interfaces are changed in `v0.1`.

## v0.1 Operator Workflow

1. Inspect available upstream binary sources on an admin workstation.
2. Fetch known-good binaries into a local provider source directory.
3. Prepare a user-owned binary release directory and `provider-manifest.json`.
4. Upload release assets and `provider-manifest.json` to a separate binary repository release.
5. Install managed binaries into a local managed install directory from that pinned manifest.
6. Render and inspect a runtime plan.
7. Render staged service unit files.
8. Review a staged service install plan.
9. Optionally run guarded daemon-reload later if the operator intentionally installs units.
10. Start or stop managed services manually through the guarded lifecycle commands.
11. Use guarded manual switch planning and apply when changing the active tunnel.
12. Run `rc check` for a read-only release-candidate validation pass.
13. Run `rc smoke` for a safe local smoke pass that stages artifacts without touching real services by default.

## Final Operator Checklist

1. Clone or pull the repository into a local working directory.
2. Initialize the local node role with `python -m pilottunnel.cli init --role controller` or `python -m pilottunnel.cli init --role worker`.
3. Inspect and fetch upstream binary sources into `<SOURCE_DIR>` on an admin workstation.
4. Prepare a binary release directory for `<BINARY_REPO>` and `<BINARY_RELEASE_TAG>`.
5. Upload the release assets and `provider-manifest.json` to the user-owned binary repository release.
6. Install managed binaries into `<INSTALL_DIR>` from `<MANIFEST_URL>`.
7. Render a runtime plan into `<RUNTIME_DIR>`.
8. Render staged service files into `<SERVICE_STAGING_DIR>`.
9. Review and apply the staged service install into `<SYSTEMD_TARGET_DIR>` only when ready.
10. Run guarded `systemd reload` and guarded start or stop commands only after staged files are in place.
11. Use guarded manual switch planning before any active tunnel change.
12. Run `rc check` and `rc smoke` before any real deployment step.
13. Keep backup and restore steps available before production rollout.

## v0.1 Limitations

- No full auto-switch.
- No background monitoring daemon.
- No UI.
- Layer 4 TCP only.
- Only selected adapters are covered by the runtime planning workflow.
- Real deployment still requires careful operator confirmation.

## Known Limitations

- No automatic failover.
- No background monitor.
- No UI.
- Real deployment requires explicit operator confirmation.
- Production use should begin with a non-production smoke test.

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
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> --audit-log <AUDIT_LOG> --lock-dir <LOCK_DIR> --work-dir <WORK_DIR> init --role controller
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> layer select --layer layer4
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> profile create --name <PROFILE> --main-port <MAIN_PORT> --target-port <TARGET_PORT> --role controller --control-port <CONTROL_PORT> --service-port <SERVICE_PORT> --check-port <CHECK_PORT> --candidate backhaul:tcp --candidate backhaul:tcpmux --candidate rathole:tcp
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> adapter list
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> bootstrap command --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --bundle-output <BUNDLE_OUTPUT> --bundle-file <BUNDLE_FILE>
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> switch --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> switch --profile <PROFILE> --adapter rathole --transport tcp
```

## Dry-Run CLI Workflow

```bash
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> --audit-log <AUDIT_LOG> --lock-dir <LOCK_DIR> --work-dir <WORK_DIR> init --role controller
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> layer select --layer layer4
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> profile create --name <PROFILE> --main-port <MAIN_PORT> --target-host <TARGET_HOST> --target-port <TARGET_PORT> --role controller --control-port <CONTROL_PORT> --service-port <SERVICE_PORT> --check-port <CHECK_PORT>
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> adapter list
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> adapter show --name backhaul
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> switch --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> switch --profile <PROFILE> --adapter rathole --transport tcp
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> status --profile <PROFILE>
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> --audit-log <AUDIT_LOG> logs --profile <PROFILE> --limit 10
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> registry check
```

## Staged Apply Mode

- `dry-run`: no files are written.
- `staged apply`: `--apply` writes generated config and unit files into the staging root only.
- `real apply`: not implemented yet.

In this stage, `--apply` does not call `systemctl`, does not touch real systemd locations, does not modify firewall rules or routes, and does not download or execute tunnel binaries.

```bash
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> --staging-root <STAGING_ROOT> plan --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> --staging-root <STAGING_ROOT> --apply switch --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> --staging-root <STAGING_ROOT> staged list
python -m pilottunnel.cli --config <CONFIG_FILE> --state <STATE_FILE> --registry <REGISTRY_FILE> --staging-root <STAGING_ROOT> staged show --profile <PROFILE> --adapter backhaul --transport tcpmux
```

## Host Preflight And Binary Planning

- `preflight` is read-only.
- `binary plan` does not download anything yet.
- `real apply` is still not implemented.
- `staged apply` remains the safest review mode.

```bash
python -m pilottunnel.cli preflight
python -m pilottunnel.cli preflight --profile <PROFILE>
python -m pilottunnel.cli binary list
python -m pilottunnel.cli binary plan --adapter backhaul
python -m pilottunnel.cli binary plan --adapter rathole
```

## Binary Import And Verification

- Manual import is still supported for controlled local testing.
- Imported binaries are not executed unless `--run-version` is explicitly used.
- Services are not started.
- Real apply is still not implemented.

```bash
python -m pilottunnel.cli binary import --adapter backhaul --source ./backhaul --version manual-v0.0.0
python -m pilottunnel.cli binary import --adapter rathole --source ./rathole --version manual-v0.0.0
python -m pilottunnel.cli binary status
python -m pilottunnel.cli binary verify --adapter backhaul
python -m pilottunnel.cli binary verify --adapter rathole --run-version
```

## Preparing A Binary Provider Repository

- Binary provider manifests are required for managed downloads.
- SHA256 verification is mandatory before any binary is imported.
- The main `PilotTunnel` source repository remains source-only.
- Production installs must not fetch adapter binaries from upstream projects during server setup.
- A separate user-owned binary repository, such as `PilotTunnel-Binaries`, should hold release assets and `provider-manifest.json`.
- `binary source list` shows the built-in upstream catalog without contacting external hosts.
- `binary source fetch` downloads only from the known upstream release catalog, requires explicit pinned tags for every external adapter, and stores verified binaries under a local provider source tree.
- `binary provider prepare` combines explicit-tag upstream fetch, manifest generation, and manifest verification for one platform without uploading anything.
- `binary provider release-plan` builds a local upload plan for a user-owned GitHub release without writing files.
- `binary provider release-assets` writes normalized release asset filenames plus `provider-manifest.json` into a chosen local release directory.
- `binary install plan` shows how managed adapter binaries would be resolved from a provider manifest, the local cache, and optionally the system PATH.
- `binary install apply` copies verified adapter binaries into a chosen managed install directory without executing them.
- `binary install list` inspects the managed install directory and install summary file.
- `runtime plan` resolves managed binaries, renders adapter runtime configs, and prints dry-run argv plans without starting tunnel processes.
- `service render` converts the runtime plan into staged systemd unit files under a chosen output directory without touching real `systemd`.
- `service install plan` validates staged PilotTunnel unit files against the current service plan and shows what would be installed into a chosen target directory.
- `service install apply` copies only verified PilotTunnel unit files into the chosen target directory after exact confirmation, without calling `systemctl`.
- `systemd reload plan` reports whether a guarded `systemctl daemon-reload` would be needed after service install.
- `systemd reload apply` runs only `systemctl daemon-reload` after exact confirmation.
- `systemd status` inspects only PilotTunnel-managed service names discovered from staged unit files.
- Remote provider hosts must be allowlisted with `--allow-provider-host`.
- `binary download-all` prepares every required Layer 4 provider-managed adapter in one run.
- `binary status --require-all --json` fails until every required v0.1 Layer 4 adapter binary is imported and verified from the chosen manifest.
- `binary provider generate-manifest` scans a local provider source tree and writes a manifest without downloading anything.
- `binary provider verify-manifest` validates schema, URLs, checksums, and required adapter coverage without changing the host.
- Manifest entries pin exact versions, exact SHA256, exact filenames, and exact asset URLs.
- `bootstrap command` prints safe copy-paste prepare commands for controller and worker roles.
- `bootstrap` prepares role, profile, bundle, staging, backup, and readiness state without real deployment.
- Real deploy remains a separate gated workflow.

```bash
python -m pilottunnel.cli binary source list
python -m pilottunnel.cli binary source fetch --source-dir <SOURCE_DIR> --platform <PLATFORM> --version backhaul=<BACKHAUL_VERSION> --version rathole=<RATHOLE_VERSION> --version frp=<FRP_VERSION> --version gost=<GOST_VERSION> --version chisel=<CHISEL_VERSION> --version realm=<REALM_VERSION> --version bore=<BORE_VERSION> --dry-run
python -m pilottunnel.cli binary source fetch --source-dir <SOURCE_DIR> --platform <PLATFORM> --version backhaul=<BACKHAUL_VERSION> --version rathole=<RATHOLE_VERSION> --version frp=<FRP_VERSION> --version gost=<GOST_VERSION> --version chisel=<CHISEL_VERSION> --version realm=<REALM_VERSION> --version bore=<BORE_VERSION> --confirm FETCH_UPSTREAM_BINARIES
python -m pilottunnel.cli binary provider prepare --source-dir <SOURCE_DIR> --provider-name <PROVIDER_NAME> --base-url https://<PROVIDER_HOST>/<BASE_PATH> --platform <PLATFORM> --output <MANIFEST_FILE> --version backhaul=<BACKHAUL_VERSION> --version rathole=<RATHOLE_VERSION> --version frp=<FRP_VERSION> --version gost=<GOST_VERSION> --version chisel=<CHISEL_VERSION> --version realm=<REALM_VERSION> --version bore=<BORE_VERSION> --confirm PREPARE_PROVIDER_BINARIES
python -m pilottunnel.cli binary provider release-plan --source-dir <SOURCE_DIR> --provider-name <PROVIDER_NAME> --repo-slug <BINARY_REPO> --release-tag <BINARY_RELEASE_TAG> --output-dir <RELEASE_DIR> --version backhaul=<BACKHAUL_VERSION> --version rathole=<RATHOLE_VERSION> --version frp=<FRP_VERSION> --version gost=<GOST_VERSION> --version chisel=<CHISEL_VERSION> --version realm=<REALM_VERSION> --version bore=<BORE_VERSION>
python -m pilottunnel.cli binary provider release-assets --source-dir <SOURCE_DIR> --provider-name <PROVIDER_NAME> --repo-slug <BINARY_REPO> --release-tag <BINARY_RELEASE_TAG> --output-dir <RELEASE_DIR> --version backhaul=<BACKHAUL_VERSION> --version rathole=<RATHOLE_VERSION> --version frp=<FRP_VERSION> --version gost=<GOST_VERSION> --version chisel=<CHISEL_VERSION> --version realm=<REALM_VERSION> --version bore=<BORE_VERSION> --confirm PREPARE_PROVIDER_RELEASE_ASSETS
python -m pilottunnel.cli binary install plan --manifest <MANIFEST_FILE> --platform <PLATFORM>
python -m pilottunnel.cli binary install apply --manifest <MANIFEST_FILE> --platform <PLATFORM> --install-dir <INSTALL_DIR> --confirm INSTALL_PROVIDER_BINARIES
python -m pilottunnel.cli binary install list --install-dir <INSTALL_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> runtime plan --runtime-dir <RUNTIME_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> service render --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> service install plan --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <SYSTEMD_TARGET_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> service install apply --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <SYSTEMD_TARGET_DIR> --confirm INSTALL_PILOTTUNNEL_SERVICES
python -m pilottunnel.cli systemd reload plan --target-dir <SYSTEMD_TARGET_DIR>
python -m pilottunnel.cli systemd reload apply --target-dir <SYSTEMD_TARGET_DIR> --confirm SYSTEMD_DAEMON_RELOAD
python -m pilottunnel.cli systemd status --service-dir <SERVICE_STAGING_DIR>
python -m pilottunnel.cli binary provider generate-manifest --provider-name <PROVIDER_HOST> --base-url <MANIFEST_URL> --source-dir <SOURCE_DIR> --output <MANIFEST_FILE>
python -m pilottunnel.cli binary provider verify-manifest --manifest-file <MANIFEST_FILE>
python -m pilottunnel.cli binary provider inspect --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST>
python -m pilottunnel.cli binary provider inspect --manifest-file <MANIFEST_FILE>
python -m pilottunnel.cli binary download --adapter backhaul --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --confirm DOWNLOAD_BINARY
python -m pilottunnel.cli binary download-all --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --confirm DOWNLOAD_ALL_BINARIES
python -m pilottunnel.cli binary download-all --manifest-file <MANIFEST_FILE> --confirm DOWNLOAD_ALL_BINARIES
python -m pilottunnel.cli binary status --require-all --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --json
python -m pilottunnel.cli binary status --require-all --manifest-file <MANIFEST_FILE> --json
python -m pilottunnel.cli bootstrap command --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --bundle-output <BUNDLE_OUTPUT> --bundle-file <BUNDLE_FILE>
python -m pilottunnel.cli bootstrap plan --role controller --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --create-profile --target-host <TARGET_HOST> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST>
python -m pilottunnel.cli bootstrap apply --role controller --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --create-profile --target-host <TARGET_HOST> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --bundle-output <BUNDLE_OUTPUT> --confirm BOOTSTRAP_APPLY
python -m pilottunnel.cli bootstrap apply --role controller --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --create-profile --target-host <TARGET_HOST> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host github.com --confirm BOOTSTRAP_APPLY
```

- Upstream fetch uses only the built-in adapter catalog for `backhaul`, `rathole`, `frp`, `gost`, `chisel`, `realm`, and `bore`.
- The upstream catalog is for admin-side mirroring only, not for server install.
- `ssh_reverse` remains a host system dependency and is intentionally skipped by upstream fetch.
- Upstream fetch writes a local `pilottunnel-source-summary.json` file under `<SOURCE_DIR>` for audit-friendly review.
- `binary provider release-assets` writes normalized files and `provider-manifest.json` under `<RELEASE_DIR>` so they can be uploaded to a user-owned GitHub release.
- Admin-side upstream fetches must always use explicit pinned tags; PilotTunnel does not allow dynamic upstream release resolution.
- GitHub release downloads may require a comma-separated provider allowlist such as `github.com,release-assets.githubusercontent.com,objects.githubusercontent.com,github-releases.githubusercontent.com`.
- Managed install writes a local `pilottunnel-binary-install-summary.json` file under `<INSTALL_DIR>` and does not require root.
- Runtime planning writes adapter config files only under `<RUNTIME_DIR>` and keeps them in dry-run mode.
- No upstream fetch, provider prepare, managed install, runtime plan, service render, service install, guarded `systemd` status/reload, or bootstrap command in this stage starts services, changes firewall or routes, or executes downloaded binaries.

## Adapter Runtime Planning

- Recommended order in v0.1:
  1. fetch upstream sources
  2. prepare a provider manifest
  3. install managed binaries
  4. render and inspect a runtime plan
  5. render and inspect a staged service plan
  6. install staged service files into a chosen target directory
  7. optionally run guarded daemon-reload
  8. inspect managed service status
  9. use guarded start and stop for managed services
  10. inspect and apply a guarded manual switch workflow when needed
- `runtime plan` currently supports Layer 4 TCP planning for `rathole`, `frp`, and `gost`.
- It resolves binaries through the managed install layer, writes runtime config files under the chosen runtime directory, and reports active, hot-standby, and config-only tunnels.
- It does not start processes, bind ports, create `systemd` units, or execute adapter binaries.

```bash
python -m pilottunnel.cli binary source fetch --source-dir <SOURCE_DIR> --platform <PLATFORM> --version backhaul=<BACKHAUL_VERSION> --version rathole=<RATHOLE_VERSION> --version frp=<FRP_VERSION> --version gost=<GOST_VERSION> --version chisel=<CHISEL_VERSION> --version realm=<REALM_VERSION> --version bore=<BORE_VERSION> --confirm FETCH_UPSTREAM_BINARIES
python -m pilottunnel.cli binary provider prepare --source-dir <SOURCE_DIR> --provider-name <PROVIDER_NAME> --base-url https://<PROVIDER_HOST>/<BASE_PATH> --platform <PLATFORM> --output <MANIFEST_FILE> --version backhaul=<BACKHAUL_VERSION> --version rathole=<RATHOLE_VERSION> --version frp=<FRP_VERSION> --version gost=<GOST_VERSION> --version chisel=<CHISEL_VERSION> --version realm=<REALM_VERSION> --version bore=<BORE_VERSION> --confirm PREPARE_PROVIDER_BINARIES
python -m pilottunnel.cli binary install apply --manifest <MANIFEST_FILE> --platform <PLATFORM> --install-dir <INSTALL_DIR> --confirm INSTALL_PROVIDER_BINARIES
python -m pilottunnel.cli --config <CONFIG_FILE> runtime plan --runtime-dir <RUNTIME_DIR>
```

## Staged Service Planning

- `service render` reads the existing runtime plan, keeps `config_only` tunnels visible, and stages unit files only for `active` and `hot_standby` tunnels.
- It writes staged unit files only under the chosen service staging directory.
- It does not write to `/etc/systemd/system`.
- It does not call `systemctl`, run `daemon-reload`, or start, stop, enable, or restart services.

```bash
python -m pilottunnel.cli --config <CONFIG_FILE> service render --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR>
```

## Staged Service Install

- `service install plan` is read-only and verifies that the staged unit files still match the current runtime-backed service plan.
- `service install apply` requires exact confirmation with `--confirm INSTALL_PILOTTUNNEL_SERVICES`.
- It copies only verified PilotTunnel-generated unit files into the chosen target directory.
- It does not call `systemctl`, run `daemon-reload`, or start, stop, enable, or restart services.
- Real `/etc/systemd/system` targets require `--allow-system-dir`.

```bash
python -m pilottunnel.cli --config <CONFIG_FILE> service install plan --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <SYSTEMD_TARGET_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> service install apply --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <SYSTEMD_TARGET_DIR> --confirm INSTALL_PILOTTUNNEL_SERVICES
```

## Guarded Systemd Control

- `systemd reload plan` is read-only and reports whether a managed unit install likely requires `systemctl daemon-reload`.
- `systemd reload apply` requires exact confirmation with `--confirm SYSTEMD_DAEMON_RELOAD`.
- `systemd status` uses only read-only `systemctl show` calls for PilotTunnel-managed service names discovered from the staged service directory.
- `systemd start plan` and `systemd stop plan` are read-only.
- `systemd start apply` requires exact confirmation with `--confirm START_PILOTTUNNEL_SERVICES`.
- `systemd stop apply` requires exact confirmation with `--confirm STOP_PILOTTUNNEL_SERVICES`.
- Only PilotTunnel-managed active and hot-standby services discovered from the staged service directory are eligible.
- Restart, enable, and disable remain separate workflows. Guarded manual switch now has its own staged command path.

```bash
python -m pilottunnel.cli systemd reload plan --target-dir <SYSTEMD_TARGET_DIR>
python -m pilottunnel.cli systemd reload apply --target-dir <SYSTEMD_TARGET_DIR> --confirm SYSTEMD_DAEMON_RELOAD
python -m pilottunnel.cli systemd status --service-dir <SERVICE_STAGING_DIR>
python -m pilottunnel.cli systemd start plan --service-dir <SERVICE_STAGING_DIR>
python -m pilottunnel.cli systemd start apply --service-dir <SERVICE_STAGING_DIR> --confirm START_PILOTTUNNEL_SERVICES
python -m pilottunnel.cli systemd stop plan --service-dir <SERVICE_STAGING_DIR>
python -m pilottunnel.cli systemd stop apply --service-dir <SERVICE_STAGING_DIR> --confirm STOP_PILOTTUNNEL_SERVICES
```

## Guarded Manual Tunnel Switch

- `switch plan` is read-only and inspects only configured active or hot-standby tunnels.
- `switch apply` requires exact confirmation with `--confirm SWITCH_PILOTTUNNEL_TUNNEL`.
- The target service is started before the previous active service is stopped.
- A read-only healthcheck runs after target start.
- If target start or healthcheck fails, the previous active service is left in place.
- If a later failure happens after the previous service was stopped, PilotTunnel attempts rollback before reporting failure.
- This workflow uses only the managed service lifecycle abstraction and does not call adapter binaries directly.

```bash
python -m pilottunnel.cli --config <CONFIG_FILE> switch plan --target <TARGET_TUNNEL> --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> switch apply --target <TARGET_TUNNEL> --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --confirm SWITCH_PILOTTUNNEL_TUNNEL
```

## Release-Candidate Validation

- `rc check` validates the safe end-to-end v0.1 workflow in scratch space and keeps the operator's runtime and staged service directories untouched.
- `rc smoke` stages runtime and service artifacts under the chosen local directories, but it does not start services, run daemon-reload, install units into real system directories by default, or execute adapter binaries.
- Both commands report component checklist status, warnings, blockers, next safe commands, next real-apply hints, and explicit v0.1 limitations.
- Auto-switch and background monitoring are intentionally out of scope for `v0.1`.

```bash
python -m pilottunnel.cli --config <CONFIG_FILE> rc check --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <TARGET_SYSTEMD_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> rc smoke --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <TARGET_SYSTEMD_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> rc check --target <TARGET_TUNNEL> --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <TARGET_SYSTEMD_DIR>
```

## Real-Host Install Planning

- `install plan` is read-only.
- It does not copy to real system paths.
- It does not run `systemctl`.
- It does not start or stop services.
- Future real apply will require explicit confirmation and backups.

```bash
python -m pilottunnel.cli install plan --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli install plan --profile <PROFILE> --adapter rathole --transport tcp --install-root .var/pilottunnel/install-root
python -m pilottunnel.cli uninstall plan --profile <PROFILE> --adapter backhaul --transport tcpmux
```

## Controlled Install Apply Gate

- Plan-only remains the default.
- Staged apply still writes only staging files.
- `install apply` in this stage copies files only into `--install-root`.
- Services are still not started.
- `systemctl`, firewall, routes, and interfaces are untouched.
- Real-host file mode is Linux-only, requires `--real-host-files`, and still does not execute `systemctl`.

```bash
python -m pilottunnel.cli install apply --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm APPLY
python -m pilottunnel.cli install rollback --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm ROLLBACK
python -m pilottunnel.cli uninstall apply --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm UNINSTALL
```

## Service Lifecycle Planning

- `service render` is the new dry-run bridge from runtime plans to staged unit files.
- `service install plan` and `service install apply` are the guarded bridge from staged unit files to a chosen target systemd directory.
- `service plan` shows the service action that would be taken, but it does not run `systemctl`.
- `service status` and `service logs` are read-only inspection commands.
- Windows hosts remain safe: these commands return warnings instead of crashing.
- Real service lifecycle changes remain gated behind explicit `--real-systemd` confirmations.

```bash
python -m pilottunnel.cli --config <CONFIG_FILE> service render --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> service install plan --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <SYSTEMD_TARGET_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> service install apply --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <SYSTEMD_TARGET_DIR> --confirm INSTALL_PILOTTUNNEL_SERVICES
python -m pilottunnel.cli service plan --profile <PROFILE> --adapter backhaul --transport tcpmux --action start
python -m pilottunnel.cli service plan --profile <PROFILE> --adapter rathole --transport tcp --action stop
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --limit 50
```

## Real Systemd Read-Only And Daemon-Reload Gate

- `service status` and `service logs` can read from real systemd only with `--real-systemd`.
- `service daemon-reload` requires exact confirmation with `--confirm DAEMON_RELOAD`.
- `service start` remains gated and requires exact confirmation with `--confirm START_SERVICE`.
- `service stop` remains gated and requires exact confirmation with `--confirm STOP_SERVICE`.
- `service restart` remains gated and requires exact confirmation with `--confirm RESTART_SERVICE`.
- `service enable` remains gated and requires exact confirmation with `--confirm ENABLE_SERVICE`.
- `service disable` remains gated and requires exact confirmation with `--confirm DISABLE_SERVICE`.
- Only PilotTunnel-owned unit files are eligible for real start.
- Only PilotTunnel-owned unit files are eligible for real stop.
- Only PilotTunnel-owned unit files are eligible for real restart.
- Only PilotTunnel-owned unit files are eligible for real enable and disable.
- Firewall rules, routes, and interfaces remain untouched.

```bash
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --limit 50
python -m pilottunnel.cli service daemon-reload --real-systemd --confirm DAEMON_RELOAD
```

## Controlled Real Service Start Gate

- `service start` requires `--real-systemd` and exact `--confirm START_SERVICE`.
- Only PilotTunnel-owned unit files can be started.
- After start, PilotTunnel runs read-only `systemctl is-active` and `systemctl status`.
- Optional `--require-healthcheck` runs TCP healthchecks after start without stopping the service on failure.
- `service start` does not imply `enable`.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service start --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm START_SERVICE
python -m pilottunnel.cli service start --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm START_SERVICE --require-healthcheck
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --limit 50
```

## Controlled Real Service Stop Gate

- `service stop` requires `--real-systemd` and exact `--confirm STOP_SERVICE`.
- Only PilotTunnel-owned unit files can be stopped.
- After stop, PilotTunnel runs read-only `systemctl is-active` and `systemctl status`.
- `service stop` does not imply `disable`.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service stop --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm STOP_SERVICE
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --limit 50
```

## Controlled Real Service Enable/Disable Gates

- `service enable` requires `--real-systemd` and exact `--confirm ENABLE_SERVICE`.
- `service disable` requires `--real-systemd` and exact `--confirm DISABLE_SERVICE`.
- Only PilotTunnel-owned unit files can be enabled or disabled.
- `service enable` does not start the service.
- `service disable` does not stop the service.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service enable --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm ENABLE_SERVICE
python -m pilottunnel.cli service disable --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm DISABLE_SERVICE
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
```

## Controlled Real Service Restart Gate

- `service restart` requires `--real-systemd` and exact `--confirm RESTART_SERVICE`.
- Only PilotTunnel-owned unit files can be restarted.
- Restart does not enable or disable the service.
- Optional `--require-healthcheck` runs TCP healthchecks after restart without another automatic restart on failure.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service restart --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm RESTART_SERVICE
python -m pilottunnel.cli service restart --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm RESTART_SERVICE --require-healthcheck
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
```

## Controlled Deployment Workflow

- `deploy plan` is read-only.
- `deploy apply` is an orchestrator around existing guarded readiness, file apply, daemon-reload, service start, healthcheck, and optional enable steps.
- `deploy apply` requires `--real-host` and exact `--confirm DEPLOY_APPLY`.
- It does not touch firewall rules, routes, or network interfaces.
- It does not download anything.
- It does not auto-stop on healthcheck failure.
- Enabling the service is optional through `--enable-after-start`.

```bash
python -m pilottunnel.cli deploy plan --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli deploy apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host --confirm DEPLOY_APPLY --require-healthcheck
python -m pilottunnel.cli deploy apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host --confirm DEPLOY_APPLY --require-healthcheck --enable-after-start
python -m pilottunnel.cli deploy status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
```

## Two-Sided Controller/Worker Bundles

- One unified CLI is used on both sides.
- The controller side exports a worker preparation bundle.
- The worker side inspects and imports that bundle to prepare local files.
- No real services are started.
- No firewall, routes, or systemd changes are performed on the host.

```bash
python -m pilottunnel.cli init --role controller
python -m pilottunnel.cli bundle export-worker --profile <PROFILE> --adapter backhaul --transport tcpmux --output .var/pilottunnel/bundles/<PROFILE>-worker.json
python -m pilottunnel.cli init --role worker
python -m pilottunnel.cli bundle inspect --input .var/pilottunnel/bundles/<PROFILE>-worker.json
python -m pilottunnel.cli bundle import --input .var/pilottunnel/bundles/<PROFILE>-worker.json --staging-root .var/pilottunnel/staging --confirm IMPORT
```

## End-to-End Local Simulation

- This simulates both controller and worker locally.
- It does not start services.
- It does not touch systemd, firewall rules, or routes.
- It is the recommended check before running on real servers.

```bash
python -m pilottunnel.cli simulate e2e --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli simulate e2e --profile <PROFILE> --adapter rathole --transport tcp --keep-files
python -m pilottunnel.cli simulate e2e --profile <PROFILE> --adapter backhaul --transport tcpmux --json
```

## Single Script, Two Roles

- The same `pilottunnel` CLI is used on both controller and worker servers.
- First `init` asks which role this server should use unless `--role` is provided.
- Controller nodes make switching and profile decisions.
- Worker nodes prepare passive-side tasks with the same CLI.
- No separate controller or worker scripts are needed.

```bash
python -m pilottunnel.cli init --role controller
python -m pilottunnel.cli init --role worker
python -m pilottunnel.cli node status
```

## TCP Healthchecks

- `healthcheck` is read-only.
- It only performs TCP connect probes.
- It does not start services.
- It does not modify firewall, routes, or systemd.

```bash
python -m pilottunnel.cli healthcheck --host <TARGET_HOST> --port <TARGET_PORT>
python -m pilottunnel.cli healthcheck --profile <PROFILE> --all
python -m pilottunnel.cli healthcheck --profile <PROFILE> --all --json
python -m pilottunnel.cli install apply --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm APPLY --require-healthcheck
```

## Server Readiness Report

- `readiness report` is read-only.
- It is the recommended check before any future real apply.
- It does not start services or modify system state.

```bash
python -m pilottunnel.cli readiness report
python -m pilottunnel.cli readiness report --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli readiness report --profile <PROFILE> --adapter rathole --transport tcp --json
```

## Controlled Real-Host File Apply

- This mode is Linux-only.
- It requires explicit `--real-host-files` and exact confirmation.
- It writes files only; it does not start, stop, enable, disable, or restart services.
- Systemd unit files may be copied, but `systemctl` is not executed.
- Firewall rules, routes, and interfaces remain untouched.
- Backups and a manifest are created before and during real-host file apply.
- Rollback restores backups and removes newly-created files from the manifest.

```bash
python -m pilottunnel.cli install apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_APPLY
python -m pilottunnel.cli install rollback --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_ROLLBACK
python -m pilottunnel.cli uninstall apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_UNINSTALL
```

## Backup and Restore Safety Layer

- Backups include PilotTunnel-owned files and metadata only.
- `restore apply` requires exact confirmation before writing anything.
- Restore verifies checksums before applying files.
- Restore creates a pre-restore safety backup before overwriting current files.
- No services are started, stopped, or restarted.
- No firewall rules, routes, or network interfaces are touched.
- It is recommended before first real Linux server testing.

```bash
python -m pilottunnel.cli backup plan
python -m pilottunnel.cli backup create --confirm BACKUP_CREATE
python -m pilottunnel.cli backup list
python -m pilottunnel.cli backup inspect --backup-id BACKUP_ID
python -m pilottunnel.cli backup verify --backup-id BACKUP_ID
python -m pilottunnel.cli restore plan --backup-id BACKUP_ID
python -m pilottunnel.cli restore apply --backup-id BACKUP_ID --confirm RESTORE_APPLY
```

## What Is Implemented

- Role-aware profile config with `controller` and `worker` normalization.
- Profile safety settings and explicit `main/control/service/check` port ownership.
- Port mapping parser and validation for common mapping shapes.
- Dry-run Backhaul and Rathole config/systemd generation with deterministic service names.
- Registry ownership tracking for ports, services, firewall tags, and routes.
- Audit entries that capture dry-run switch metadata while redacting secrets.

## What Is Still Dry-Run Only

- Real Backhaul or Rathole binary install/execution.
- Real service execution after file deployment.
- Real firewall, route, interface, or SSH/API-based worker coordination.
- Real tunnel health verification beyond local-only stubs.
