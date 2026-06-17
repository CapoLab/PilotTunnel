# PilotTunnel

PilotTunnel is a server-only Python CLI for managing multiple tunnel adapters behind a stable public port. Version `v0.1` focuses on safe local orchestration for `layer4`, with dry-run behavior by default and transactional switching semantics backed by state, registry, locks, and audit logs.

## File Tree Summary

```text
pilottunnel/
  __init__.py
  cli.py
  config.py
  state.py
  registry.py
  locks.py
  audit.py
  systemd.py
  healthcheck.py
  switch_engine.py
  adapters/
    __init__.py
    base.py
    common.py
    backhaul.py
    rathole.py
    frp.py
    gost.py
    chisel.py
    realm.py
    wstunnel.py
    bore.py
    ssh_reverse.py
    udp2raw.py
tests/
  test_registry.py
  test_switch_engine.py
  test_adapters_metadata.py
  test_safety.py
```

## What Was Implemented

- CLI skeleton for init, profile management, listing layers/adapters, install, switch, status, healthcheck, rollback, logs, registry checks, and cleanup.
- Config, runtime state, registry, audit redaction, and file-lock helpers using the Python standard library.
- Adapter registry for all requested tunnel candidates with metadata and safe dry-run/systemd-template behavior.
- Transactional switch flow with lock acquisition, backup state, precheck, render, stop old, cleanup, reclaim main port, start new, healthcheck, commit, and rollback.
- Local-only `v0.1` coordination model with dry-run default and explicit `--apply` opt-in for writing rendered unit files.

## Exact Commands To Test

```bash
python -m compileall pilottunnel
python -m unittest discover -s tests -v
git diff --check
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --audit-log ./tmp/audit.log --lock-dir ./tmp/locks --work-dir ./tmp/work init
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json profile create --name turkey-6221 --main-port 6221 --target-port 6221 --candidate backhaul:tcp
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile turkey-6221 --adapter backhaul --transport tcp
```

## Limitations

- Only `layer4` is usable in `v0.1`; other layers are discoverable metadata and intentionally blocked from real switching.
- Adapter actions are dry-run oriented; they generate safe unit templates and mocked lifecycle responses instead of touching live firewall, route, interface, or production systemd state.
- Remote worker coordination is represented only by data model concepts for now; Iran/foreign multi-host orchestration is not yet implemented.
