# Operations

PilotTunnel is meant to be operated conservatively. Review plans first, prefer dry-run output, and only use guarded apply commands when you intentionally want host changes.

## Safe Operating Pattern

1. run read-only checks first
2. verify binary readiness from a pinned provider manifest
3. render runtime and service plans
4. inspect readiness and RC output
5. apply only the specific guarded step you intend to perform

## Bootstrap

The top-level bootstrap helper is:

```bash
bash scripts/install.sh
bash scripts/install.sh --role <ROLE> --layer layer4 --dry-run
bash scripts/install.sh --no-menu --role <ROLE>
```

- running the script with no arguments prepares PilotTunnel and opens the terminal menu
- role selection happens later under `Setup / Configure this server`
- use `--no-menu --role <ROLE>` for explicit non-interactive initialization
- the script defaults to the public source repository and public provider manifest
- it does not start services or modify firewall, routes, or interfaces
- it records unsupported known layers as planned-only preferences

## Role And Layer Setup

```bash
python -m pilottunnel.cli init --role controller
python -m pilottunnel.cli layer select --layer layer4
python -m pilottunnel.cli layer status
```

## Binary Provider Workflow

```bash
python -m pilottunnel.cli binary provider inspect --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST>
python -m pilottunnel.cli binary download-all --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --confirm DOWNLOAD_ALL_BINARIES
python -m pilottunnel.cli binary status --require-all --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST>
```

Provider releases use a pinned `provider-manifest.json`. Prepare release assets from
`<SOURCE_DIR>` into `<RELEASE_DIR>`, write the local manifest to `<MANIFEST_FILE>`,
and publish it under `<BINARY_REPO>` at `<BINARY_RELEASE_TAG>` before server setup.

## Readiness And Planning

```bash
python -m pilottunnel.cli readiness report --json
python -m pilottunnel.cli bootstrap command --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --bundle-output <BUNDLE_OUTPUT> --bundle-file <BUNDLE_FILE>
python -m pilottunnel.cli bootstrap plan --role controller --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --create-profile --target-host <TARGET_HOST> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST>
```

For explicit port planning, use placeholders rather than copied production values:
`<MAIN_PORT>`, `<TARGET_PORT>`, `<CONTROL_PORT>`, `<SERVICE_PORT>`, and
`<CHECK_PORT>`. Controller/worker handoff paths should likewise remain generic as
`<BUNDLE_OUTPUT>` and `<BUNDLE_FILE>`.

## Safety Notes

- keep examples placeholder-based
- do not paste secrets into config, issues, or logs
- do not treat `v0.1` as an auto-failover system
- prefer a non-production smoke run before any real deployment step
