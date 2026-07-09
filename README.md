# PilotTunnel

Safety-first CLI for planning and managing Layer 4 tunnel workflows between a controller and worker.

PilotTunnel keeps setup conservative: prepare the repo, verify managed binaries, open the terminal menu, and keep host-affecting actions behind explicit guarded commands.

## One-line install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh)
```

The installer prepares PilotTunnel, checks the required binaries, prints a short safety summary, and then opens the menu. Choose the server role later from `Setup / Configure this server`.

## Common commands

```bash
python -m pilottunnel.cli version
python -m pilottunnel.cli node status
python -m pilottunnel.cli readiness report --json
```

## Safety by default

- No service start or stop during base install
- No daemon reload during base install
- No firewall, route, or interface mutation during base install
- No adapter execution during base install
- Explicit confirmation tokens for apply operations

No auto-switch. No background monitoring. No UI.

## Reserved test ports

PilotTunnel reserves `27777` for its internal probe path and `27778` as the auxiliary test fallback. These are not the public tunnel port and not the target service port. Advanced or future multi-layer tests may use the reserved internal range `27777-27786`.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Operations guide](docs/OPERATIONS.md)
- [Security policy](SECURITY.md)
- [Contributing guide](CONTRIBUTING.md)
- [Release notes](RELEASE_NOTES.md)
- [Persian placeholder](README_FA.md)

## License

[MIT](LICENSE)
