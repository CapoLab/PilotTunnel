import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pilottunnel import cli
from pilottunnel.binaries import binary_components, binary_filename_for_component, current_platform_id
from pilottunnel.config import AppConfig, BinaryResolutionSettings, Profile, ProfilePorts, build_node_settings, save_config
from pilottunnel.state import AppState, load_state, save_state
from testsupport import allocate_tcp_ports


class ManualSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_path = self.base / "config.json"
        self.state_path = self.base / "state.json"
        self.registry_path = self.base / "registry.json"
        self.audit_path = self.base / "audit.log"
        self.lock_dir = self.base / "locks"
        self.work_dir = self.base / "work"
        self.staging_root = self.base / "staging"
        self.runtime_dir = self.base / "runtime"
        self.service_dir = self.base / "service-staging"
        self.service_dir.mkdir(parents=True, exist_ok=True)
        ports, listeners = allocate_tcp_ports(15)
        self.ports = ports
        for listener in listeners:
            listener.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(
                [
                    "--config",
                    str(self.config_path),
                    "--state",
                    str(self.state_path),
                    "--registry",
                    str(self.registry_path),
                    "--audit-log",
                    str(self.audit_path),
                    "--lock-dir",
                    str(self.lock_dir),
                    "--work-dir",
                    str(self.work_dir),
                    "--staging-root",
                    str(self.staging_root),
                    *args,
                ]
            )
        return code, output.getvalue()

    def _profile(self, name: str, *, adapter: str, runtime_role: str, port_offset: int) -> Profile:
        main_port, target_port, control_port, service_port, check_port = self.ports[port_offset : port_offset + 5]
        return Profile(
            name=name,
            main_port=main_port,
            target_host="127.0.0.1",
            target_port=target_port,
            role="controller",
            active_adapter=adapter,
            active_transport="tcp",
            runtime_role=runtime_role,
            ports=ProfilePorts(
                main_port=main_port,
                control_port=control_port,
                service_port=service_port,
                check_port=check_port,
            ),
        )

    def _managed_install_dir(self, *adapters: str) -> Path:
        install_dir = self.base / "managed-install"
        platform_id = current_platform_id()
        for adapter in adapters:
            for component in binary_components(adapter):
                filename = binary_filename_for_component(adapter, component, platform_id=platform_id)
                binary_path = install_dir / adapter / platform_id / filename
                binary_path.parent.mkdir(parents=True, exist_ok=True)
                binary_path.write_bytes(f"{adapter}-{component}-binary".encode("utf-8"))
        return install_dir

    def _write_config(self, profiles: list[Profile], *, managed_install_dir: Path) -> None:
        config = AppConfig(
            node=build_node_settings("controller"),
            binary_resolution=BinaryResolutionSettings(
                managed_install_dir=str(managed_install_dir),
                allow_system_path=False,
                prefer_managed_install=True,
            ),
            profiles=profiles,
        )
        save_config(config, self.config_path)
        save_state(AppState(), self.state_path)
        self.registry_path.write_text("{}", encoding="utf-8")

    def _base_profiles(self) -> list[Profile]:
        return [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active", port_offset=0),
            self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby", port_offset=5),
        ]

    def _success_patches(self):
        return patch.multiple(
            "pilottunnel.manual_switch",
            build_profile_healthcheck_plan=lambda **kwargs: {"ok": True, "checks": ["target"]},
            run_profile_healthchecks=lambda **kwargs: [{"ok": True, "endpoint": "target"}],
            summarize_healthchecks=lambda results, profile, role: {
                "ok": True,
                "profile": profile,
                "role": role,
                "checks": results,
                "result": "ok",
            },
        )

    def test_switch_plan_is_read_only(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        before = self.state_path.read_text(encoding="utf-8")
        code, output = self.run_cli(
            "switch",
            "plan",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["plan_only"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertEqual(before, self.state_path.read_text(encoding="utf-8"))

    def test_worker_role_blocks_manual_switch_plan(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["node"] = {
            "node_id": "node-test-worker",
            "node_role": "worker",
            "initialized_at": "now",
            "role_alias_used": "worker",
            "normalized_role": "worker",
        }
        self.config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
        code, output = self.run_cli(
            "switch",
            "plan",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked for node role 'worker'", output)

    def test_switch_apply_requires_exact_confirm_token(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "switch",
            "apply",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
        )
        self.assertEqual(code, 1)
        self.assertIn("SWITCH_PILOTTUNNEL_TUNNEL", output)

    def test_switch_target_must_exist(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "switch",
            "plan",
            "--target",
            "missing-tunnel",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
        )
        self.assertEqual(code, 1)
        self.assertIn("missing-tunnel", output)

    def test_switch_to_config_only_target_is_rejected(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp", "gost")
        profiles = self._base_profiles() + [self._profile("profile-test-003", adapter="gost", runtime_role="config_only", port_offset=10)]
        self._write_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli(
            "switch",
            "plan",
            "--target",
            "profile-test-003",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
        )
        self.assertEqual(code, 1)
        self.assertIn("config_only", output)

    def test_switch_starts_target_before_stopping_previous(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        events: list[tuple[str, str]] = []

        def fake_start(*, service_dir: Path, service_name: str | None, confirm: str | None, audit_path: Path):
            events.append(("start", service_name or ""))
            return {"ok": True, "service_name": service_name}

        def fake_stop(*, service_dir: Path, service_name: str | None, confirm: str | None, audit_path: Path):
            events.append(("stop", service_name or ""))
            return {"ok": True, "service_name": service_name}

        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", side_effect=fake_start), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            side_effect=fake_stop,
        ):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(events[0][0], "start")
        self.assertEqual(events[1][0], "stop")
        self.assertTrue(events[0][1].startswith("pilottunnel-"))
        self.assertTrue(events[1][1].startswith("pilottunnel-"))

    def test_target_start_failure_leaves_previous_active_unchanged(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        calls: list[str] = []

        def fake_start(**kwargs):
            calls.append("start")
            return {"ok": False, "service_name": kwargs["service_name"]}

        def fake_stop(**kwargs):
            calls.append("stop")
            return {"ok": True}

        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", side_effect=fake_start), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            side_effect=fake_stop,
        ):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["start"])
        state = load_state(self.state_path)
        self.assertEqual(state.manual_active_tunnel, "")

    def test_target_healthcheck_failure_leaves_previous_active_unchanged(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        stop_calls: list[str] = []

        with patch("pilottunnel.manual_switch.apply_systemd_start", return_value={"ok": True}), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            side_effect=lambda **kwargs: stop_calls.append(kwargs["service_name"]) or {"ok": True},
        ), patch("pilottunnel.manual_switch.build_profile_healthcheck_plan", return_value={"ok": False, "checks": ["target"]}), patch(
            "pilottunnel.manual_switch.run_profile_healthchecks",
            return_value=[{"ok": False, "endpoint": "target"}],
        ), patch(
            "pilottunnel.manual_switch.summarize_healthchecks",
            return_value={"ok": False, "result": "failed", "checks": [{"ok": False}]},
        ):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 1)
        self.assertEqual(len(stop_calls), 1)
        self.assertTrue(stop_calls[0].startswith("pilottunnel-demo-l4-002"))
        state = load_state(self.state_path)
        self.assertEqual(state.manual_active_tunnel, "")

    def test_successful_switch_updates_state_file(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", return_value={"ok": True}), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            return_value={"ok": True},
        ):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 0, msg=output)
        state = load_state(self.state_path)
        self.assertEqual(state.manual_active_tunnel, "demo-l4-002")
        self.assertEqual(state.manual_previous_tunnel, "smoke-l4-001")
        self.assertEqual(state.last_manual_switch["target_tunnel"], "demo-l4-002")

    def test_failed_switch_does_not_update_state_file(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", return_value={"ok": False}), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            return_value={"ok": True},
        ):
            code, _output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 1)
        state = load_state(self.state_path)
        self.assertEqual(state.manual_active_tunnel, "")
        self.assertEqual(state.last_manual_switch, {})

    def test_rollback_attempted_if_failure_happens_after_previous_stop(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        events: list[tuple[str, str]] = []

        def fake_start(*, service_dir: Path, service_name: str | None, confirm: str | None, audit_path: Path):
            events.append(("start", service_name or ""))
            return {"ok": True}

        def fake_stop(*, service_dir: Path, service_name: str | None, confirm: str | None, audit_path: Path):
            events.append(("stop", service_name or ""))
            return {"ok": True}

        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", side_effect=fake_start), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            side_effect=fake_stop,
        ), patch("pilottunnel.manual_switch.save_state", side_effect=OSError("disk-full")):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["rollback_attempted"])
        self.assertTrue(payload["rollback_succeeded"])
        self.assertEqual(
            events,
            [
                ("start", "pilottunnel-demo-l4-002-frp-tcp.service"),
                ("stop", "pilottunnel-smoke-l4-001-rathole-tcp.service"),
                ("stop", "pilottunnel-demo-l4-002-frp-tcp.service"),
                ("start", "pilottunnel-smoke-l4-001-rathole-tcp.service"),
            ],
        )

    def test_rollback_failure_is_reported_as_critical(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        stop_calls = {"count": 0}

        def fake_start(**kwargs):
            service_name = kwargs["service_name"]
            if service_name == "pilottunnel-smoke-l4-001-rathole-tcp.service":
                return {"ok": False, "service_name": service_name}
            return {"ok": True, "service_name": service_name}

        def fake_stop(**kwargs):
            stop_calls["count"] += 1
            return {"ok": True, "service_name": kwargs["service_name"]}

        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", side_effect=fake_start), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            side_effect=fake_stop,
        ), patch("pilottunnel.manual_switch.save_state", side_effect=OSError("disk-full")):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["rollback_attempted"])
        self.assertFalse(payload["rollback_succeeded"])
        self.assertEqual(stop_calls["count"], 2)

    def test_lock_prevents_concurrent_switch_apply(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        (self.lock_dir / "manual-switch.lock").write_text("busy", encoding="utf-8")
        code, output = self.run_cli(
            "switch",
            "apply",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--confirm",
            "SWITCH_PILOTTUNNEL_TUNNEL",
        )
        self.assertEqual(code, 1)
        self.assertIn("Concurrent manual switch blocked", output)

    def test_audit_log_records_apply_and_rollback_with_redaction(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)

        def fake_start(**kwargs):
            return {"ok": True, "token": "super-secret", "service_name": kwargs["service_name"]}

        def fake_stop(**kwargs):
            return {"ok": True, "token": "super-secret", "service_name": kwargs["service_name"]}

        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", side_effect=fake_start), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            side_effect=fake_stop,
        ), patch("pilottunnel.manual_switch.save_state", side_effect=OSError("disk-full")):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 1)
        self.assertNotIn("super-secret", output)
        lines = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        actions = [line["action"] for line in lines]
        self.assertIn("manual-switch-apply", actions)
        self.assertIn("manual-switch-rollback", actions)
        self.assertNotIn("super-secret", self.audit_path.read_text(encoding="utf-8"))

    @patch("subprocess.run", side_effect=AssertionError("unexpected subprocess.run call"))
    def test_switch_apply_uses_managed_service_lifecycle_only(self, _mock_run) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._base_profiles(), managed_install_dir=install_dir)
        seen: list[str] = []

        def fake_start(**kwargs):
            seen.append(kwargs["service_name"])
            return {"ok": True}

        def fake_stop(**kwargs):
            seen.append(kwargs["service_name"])
            return {"ok": True}

        with self._success_patches(), patch("pilottunnel.manual_switch.apply_systemd_start", side_effect=fake_start), patch(
            "pilottunnel.manual_switch.apply_systemd_stop",
            side_effect=fake_stop,
        ):
            code, output = self.run_cli(
                "switch",
                "apply",
                "--target",
                "demo-l4-002",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "SWITCH_PILOTTUNNEL_TUNNEL",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(seen)
        self.assertTrue(all(name.startswith("pilottunnel-") for name in seen))
