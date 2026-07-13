# Layer-4 Benchmark Matrix

PilotTunnel batch benchmarking is a local two-runner workflow: start the Worker batch first, then run the Controller batch with the printed non-secret token. Each server only manages its own PilotTunnel-owned files and services. The workflow does not use SSH, remote shell, firewall changes, route changes, interface changes, or automatic production switching.

## Batch status

| Adapter | Required binary | Role topology | Ports | Auth model | Batch eligibility | Expected live-test result |
| --- | --- | --- | --- | --- | --- | --- |
| rathole | `rathole` | Controller server, Worker client, Worker probe responder | Transport port, Controller user-facing port, Worker service port, private probe port | Derived per-link Rathole token | Ready for batch live test when binary and ports are available | Probe and real-service smoke should pass if both sides are started in the scheduled slot |
| frp | `frps` and `frpc` | Controller `frps`, Controller `frpc` STCP visitor, Worker `frpc`, Worker probe responder | Transport port, Controller user-facing port, Worker service port, private probe port | Shared per-link token with consistent `transport.tcpMux` and STCP secret | Ready for batch live test when both FRP components and ports are available | Probe and real-service smoke should pass after Worker slot is active |
| backhaul | `backhaul` | Controller server, Worker client, Worker probe responder | Transport port, Controller user-facing port, Worker service port, private probe port | Derived per-link token | Ready for batch live test when binary and ports are available | Probe and real-service smoke should pass if both sides are started in the scheduled slot |
| gost | `gost` | Controller tunnel server plus visitors, Worker tunnel client, Worker probe responder | Transport port, Controller user-facing port, Worker service port, private probe port | Per-link tunnel id and deterministic service/probe host filters | Ready for batch live test when binary and ports are available | Probe and real-service smoke should pass if the tunnel client registers both filtered targets |
| chisel | `chisel` | Controller reverse server, Worker reverse client, Worker probe responder | Transport port, Controller user-facing port, Worker service port, private probe port | Derived user/password auth file | Ready for batch live test when binary and ports are available | Probe and real-service smoke should pass when the Worker reverse client opens both reverse listeners |
| realm | `realm` | Direct Layer-4 baseline | Service path only | No tunnel auth model | Optional baseline only; not a winner candidate | Recorded as `baseline_only` for tunnel ranking |
| bore | `bore` | Two-sided tunnel plan with adapter fixed control port | Fixed Bore control port, user-facing/service ports | Derived `BORE_SECRET` environment | Protocol-limited: Bore v0.6 does not safely provide a separate private loopback probe listener alongside the public service without broad tunnel-port exposure | Recorded as `skipped_protocol_limited` |

## Minimal live procedure

Worker:

```bash
pilottunnel-test --link <LINK_LABEL> --batch-worker --adapters rathole,frp,backhaul,gost,chisel,realm,bore --attempts 3 --timeout 5
```

Controller:

```bash
pilottunnel-test --link <LINK_LABEL> --batch-controller --batch-token <TOKEN_FROM_WORKER> --attempts 3 --timeout 5
```

The Controller report ranks only candidates with successful probe and real-service smoke tests. The recommendation is informational only; it does not switch production automatically.
