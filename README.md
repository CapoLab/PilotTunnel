# PilotTunnel

Safety-first multi-layer tunnel orchestration CLI with guarded service workflows.

PilotTunnel gives you one installer and one CLI for controller/worker tunnel operations, with plans and confirmation gates before host-affecting steps.

## One-line install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh)
```

Run the same installer on each server and choose the role interactively.

<details>
<summary>Non-interactive examples</summary>

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --role controller --layer layer4 --dry-run
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --role controller --layer layer4 --confirm INSTALL_PILOTTUNNEL
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --role worker --layer layer4 --confirm INSTALL_PILOTTUNNEL
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
