# Security

PilotTunnel is designed as a safety-first server-side CLI for guarded Layer 4 tunnel operations. The project intentionally favors explicit operator confirmation, dry-run planning, and read-only inspection over convenience automation.

## Threat Model Summary

PilotTunnel assumes operators may be preparing or reviewing tunnel changes on real hosts. Because of that:

- host-affecting actions should be explicit, reviewable, and confirmation-gated
- read-only inspection should remain available without mutating services or network state
- binary acquisition should be pinned, verified, and provider-controlled
- public documentation should avoid private infrastructure examples

## Secret Handling

- Do not commit tokens, passwords, keys, cookies, or provider credentials.
- Do not add examples containing private hosts, private ports, or production identifiers.
- Audit and log output should stay generic and avoid leaking secret-looking values where redaction already exists.

## Public Repository Hygiene

- Keep examples limited to `controller` and `worker`.
- Use placeholders instead of real infrastructure values.
- Do not commit generated provider artifacts, local binary caches, or temporary diagnostics.
- If local debug files or machine-specific logs appear during development, remove or ignore them before committing.

## Reporting

If you find a security issue or public-hygiene problem, open a private security report through GitHub security advisories if available, or contact the maintainers without publishing exploit details or secrets in public issues.
