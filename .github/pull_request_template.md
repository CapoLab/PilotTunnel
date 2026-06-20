## Summary

Describe the change at a high level.

## Checklist

- [ ] `python -m compileall pilottunnel`
- [ ] `python -m unittest discover -s tests -v`
- [ ] `git diff --check`
- [ ] No secrets, private hosts, private ports, or production identifiers were added
- [ ] No country or location role labels were added
- [ ] No generated binaries, provider artifacts, caches, or local debug folders were committed
- [ ] Safety gates for systemd, firewall, routes, interfaces, and binary execution remain intact

## Operator Impact

Note any CLI, bootstrap, documentation, or confirmation-token changes.
