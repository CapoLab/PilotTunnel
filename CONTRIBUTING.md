# Contributing

PilotTunnel is a server-ops CLI project with a strong safety bias. Contributions are welcome, but changes should preserve the guarded workflow model and keep public examples generic.

## Local Development

```bash
python -m compileall pilottunnel
python -m unittest discover -s tests -v
git diff --check
```

## Safety Constraints

- Do not add UI, dashboard, or frontend code.
- Do not add bootstrap steps that start, stop, restart, enable, or disable services unless they remain behind explicit confirmation gates and tests.
- Do not add firewall, route, interface, or uncontrolled systemd mutations.
- Do not run adapter binaries during planning or bootstrap workflows unless a command is already explicitly designed and tested for that purpose.
- Keep dry-run and read-only paths available wherever practical.
- Preserve exact-confirmation behavior for host-affecting operations.

## Public Repository Hygiene

- Use only generic public examples with `controller` and `worker`.
- Do not add country names, private tunnel names, private hosts, private ports, secrets, tokens, or production values.
- Do not commit generated binary artifacts, provider release folders, local caches, or temporary debug output.
- Prefer placeholders such as `<PROFILE>`, `<TARGET_HOST>`, `<MANIFEST_URL>`, and `<INSTALL_DIR>` in docs.

## Pull Request Expectations

- Add or update tests for behavior changes.
- Keep documentation aligned with the CLI and safety model.
- Call out any changes to operator-facing commands or confirmation tokens.
