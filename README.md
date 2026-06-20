# PilotTunnel

Safety-first CLI for planning and managing Layer 4 tunnel workflows between a controller and worker.

PilotTunnel provides role-aware setup, managed binary verification, readiness checks, runtime planning, and guarded service workflows from one Python CLI. It is config-driven and designed to make every host-affecting action explicit.

## One-line install

Controller:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --role controller --layer layer4 --confirm INSTALL_PILOTTUNNEL
```

Worker:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --role worker --layer layer4 --confirm INSTALL_PILOTTUNNEL
```

Dry-run:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh) --role controller --layer layer4 --dry-run
```

Review downloaded scripts before running them. The installer prepares PilotTunnel but does not start tunnel services.

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

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Operations guide](docs/OPERATIONS.md)
- [Security policy](SECURITY.md)
- [Contributing guide](CONTRIBUTING.md)
- [Release notes](RELEASE_NOTES.md)
- [Persian documentation](README_FA.md)

## License

[MIT](LICENSE)
