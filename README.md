# PilotTunnel

Safety-first multi-layer tunnel orchestration CLI with guarded service workflows.

PilotTunnel gives you one installer and one menu for controller/worker tunnel operations, with plans and confirmation gates before host-affecting steps.

## One-line install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh)
```

Run the same installer on every server. It prepares PilotTunnel, verifies the managed binaries, and opens the terminal menu. Choose its role later from the menu under **Setup / Configure this server**.

<details>
<summary>Non-interactive examples</summary>

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --no-menu --role controller
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --no-menu --role worker
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --no-menu --no-binaries --dry-run
```

</details>

## Common commands

```bash
python -m pilottunnel.cli version
python -m pilottunnel.cli node status
python -m pilottunnel.cli readiness report --json
```

## Safety by default

- No service start or stop during bootstrap
- No firewall, route, or interface mutation
- No adapter execution during bootstrap
- Explicit confirmation tokens for apply operations

No auto-switch. No background monitoring.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Operations guide](docs/OPERATIONS.md)
- [Security policy](SECURITY.md)
- [Contributing guide](CONTRIBUTING.md)
- [Release notes](RELEASE_NOTES.md)
- [Persian documentation](README_FA.md)

## License

[MIT](LICENSE)
