"""Isolated launchd packaging tests: no real launchctl, pip, or service process."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "bin"))
import beads_service_install as installer


class ServiceInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="beads service tests ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source repo"
        (self.source / "bin").mkdir(parents=True)
        (self.source / "schemas/v1").mkdir(parents=True)
        (self.source / "bin/beads_service.py").write_text("# packaged service\n")
        (self.source / "bin/beads_contract.py").write_text("# packaged dependency\n")
        (self.source / "bin/herdr-beads").write_text("#!/usr/bin/env python3\n")
        (self.source / "schemas/v1/run.json").write_text('{"type":"object"}')
        (self.source / "requirements.txt").write_text("jsonschema>=4.18,<5\nreferencing>=0.30\n")
        self.tools = self.base / "tools with spaces"
        self.tools.mkdir()
        for name in ("git", "bd"):
            tool = self.tools / name
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o700)
        self.env = patch.dict(os.environ, {
            "HERDR_BEADS_INSTALL_ROOT": str(self.base / "installed package"),
            "HERDR_BEADS_STATE_HOME": str(self.base / "state home"),
            "HERDR_BEADS_LAUNCH_AGENTS": str(self.base / "Launch Agents"),
            "XDG_CONFIG_HOME": str(self.base / "config home"),
            "HERDR_APP_HOME": str(self.base / "Herdr app"),
            "PATH": str(self.tools), "BEADS_DIR": "/must/not/inherit", "SECRET_TOKEN": "not-for-launchd",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.paths = installer.Paths()
        self.calls = []
        self.project_roots = []
        self.launch_responses = {}
        self.runner = patch.object(installer, "run", side_effect=self.fake_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.python = patch.object(installer, "provision_python", side_effect=self.fake_python)
        self.python_mock = self.python.start()
        self.addCleanup(self.python.stop)
        self.platform = patch.object(installer.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def fake_python(self, paths):
        (paths.root / "venv/bin").mkdir(parents=True, exist_ok=True)
        paths.python.write_text("#!/bin/sh\nexit 0\n")
        paths.python.chmod(0o700)

    def fake_run(self, argv, *, cwd=None, env=None):
        argv = [str(value) for value in argv]
        self.calls.append((argv, cwd, env))
        if argv[0] == installer.LAUNCHCTL:
            returncode, stdout, stderr = self.launch_responses.get(argv[1], (0, "state = running", ""))
        elif Path(argv[0]).name == "git":
            stdout = str(Path(cwd) / ".git") if "--git-common-dir" in argv else str(cwd)
            returncode, stderr = 0, ""
        elif Path(argv[0]).name == "bd":
            if "where" in argv:
                stdout = json.dumps({"path": str(Path(cwd) / ".beads")})
            else:
                stdout = json.dumps({"repo_root": str(cwd), "beads_dir": str(Path(cwd) / ".beads")})
            returncode, stderr = 0, ""
        else:
            returncode, stdout, stderr = 0, '{"state":"running"}', ""
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def project(self, name):
        root = self.base / name
        (root / ".git").mkdir(parents=True)
        (root / ".beads").mkdir()
        self.project_roots.append(root)
        return str(root)

    def install(self, projects=(), no_start=True):
        installer.install(self.source, self.paths, projects, no_start)

    def config(self):
        return json.loads(self.paths.config.read_text())

    def save_config(self, config):
        installer.private_write(self.paths.config, json.dumps(config).encode())

    def launch_calls(self):
        return [argv for argv, _, _ in self.calls if argv[0] == installer.LAUNCHCTL]

    def test_no_start_packages_absolute_paths_and_private_files_without_launchctl(self):
        root = self.project("project with spaces")
        self.install([root])
        config = self.config()
        plist = plistlib.loads(self.paths.plist.read_bytes())
        self.assertEqual(self.launch_calls(), [])
        self.assertEqual(plist["ProgramArguments"], [str(self.paths.python), str(self.paths.root / "bin/beads_service.py"), "--config", str(self.paths.config), "serve"])
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["Umask"], 0o077)
        self.assertEqual(config["projects"][0]["repository"]["root"], root)
        self.assertEqual(config["executables"]["bd"], str(self.tools / "bd"))
        self.assertIsNone(config["executables"]["omp"])
        self.assertIn("Unavailable optional capabilities: gh, omp, hc, herdr-app", self.output.getvalue())
        environment = plist["EnvironmentVariables"]
        self.assertEqual(set(environment), {"HOME", "USER", "LOGNAME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "HERDR_APP_HOME", "PATH"})
        self.assertNotIn("SECRET_TOKEN", environment)
        self.assertNotIn("BEADS_DIR", environment)
        expected_path = ":".join(dict.fromkeys([str(self.tools), *[str(Path(value).resolve()) for value in ("/usr/bin", "/bin", "/usr/sbin", "/sbin")]]))
        self.assertEqual(environment["PATH"], expected_path)
        for path in (self.paths.config, self.paths.plist, self.paths.logs / "stdout.log", self.paths.logs / "stderr.log"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for path in (self.paths.root, self.paths.state, self.paths.state / "store", self.paths.logs):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertTrue(os.access(self.paths.root / "bin/herdr-beads", os.X_OK))

    def test_reinstall_preserves_projects_identity_logs_and_state(self):
        first = self.project("first repository")
        second = self.project("second repository")
        self.install([first])
        identity = self.paths.state_home / "go-bd/machine-id"
        original_identity = identity.read_bytes()
        state = self.paths.state / "store/important.json"
        state.write_text("retained run state")
        log = self.paths.logs / "stderr.log"
        log.write_text("retained diagnostics")
        health = self.paths.state / "service-status.json"
        health.write_text("retained health")
        self.install([first, second, second])
        self.install()
        self.assertEqual(len(self.config()["projects"]), 2)
        self.assertEqual(identity.read_bytes(), original_identity)
        self.assertEqual(state.read_text(), "retained run state")
        self.assertEqual(log.read_text(), "retained diagnostics")
        self.assertEqual(health.read_text(), "retained health")
        self.assertEqual(self.launch_calls(), [])

    def test_invalid_existing_config_is_not_replaced_or_started(self):
        self.install()
        for mutate in (
            lambda config: config.update(schemaVersion=2),
            lambda config: config.update(projects="not a list"),
            lambda config: config.update(pollSeconds=True),
            lambda config: config.update(socketPath="relative.sock"),
            lambda config: config["executables"].update(git=None),
            lambda config: config.update(extra="unknown"),
        ):
            config = self.config()
            mutate(config)
            self.save_config(config)
            original = self.paths.config.read_bytes()
            with self.assertRaises(installer.InstallError):
                self.install(no_start=False)
            self.assertEqual(self.paths.config.read_bytes(), original)
            self.assertEqual(self.launch_calls(), [])
            self.paths.config.unlink()
            self.install()

    def test_malformed_json_is_visible_and_preserved(self):
        self.install()
        self.paths.config.write_text("{broken")
        with self.assertRaisesRegex(installer.InstallError, "Cannot read config"):
            self.install()
        self.assertEqual(self.paths.config.read_text(), "{broken")

    def test_invalid_machine_identity_never_replaced(self):
        self.install()
        identity = self.paths.state_home / "go-bd/machine-id"
        identity.write_text("bad identity\n")
        with self.assertRaisesRegex(installer.InstallError, "never replace"):
            self.install()
        self.assertEqual(identity.read_text(), "bad identity\n")

    def test_missing_required_tool_fails_before_provisioning(self):
        (self.tools / "bd").unlink()
        with self.assertRaisesRegex(installer.InstallError, "Required executable.*bd"):
            self.install()
        self.assertFalse(self.paths.config.exists())
        self.assertEqual(self.launch_calls(), [])

    def test_project_lookup_is_read_only_and_does_not_inherit_beads_dir(self):
        self.install([self.project("explicit sessionless project")])
        bd_calls = [(argv, env) for argv, _, env in self.calls if Path(argv[0]).name == "bd"]
        self.assertEqual([argv[2] for argv, _ in bd_calls], ["where", "context"])
        for argv, env in bd_calls:
            self.assertNotEqual(env.get("BEADS_DIR"), "/must/not/inherit")
            self.assertNotIn("SECRET_TOKEN", env)
            self.assertNotIn("close", argv)

    def test_noncanonical_project_is_refused(self):
        root = self.project("canonical project")
        alias = self.base / "project alias"
        alias.symlink_to(root, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "canonical"):
            self.install([str(alias)])

    def test_changed_registered_database_is_not_silently_rebound(self):
        root = self.project("registered project")
        self.install([root])
        old = self.config()
        old["projects"][0]["beadsDir"] = str(self.base / "other database")
        self.save_config(old)
        with self.assertRaisesRegex(installer.InstallError, "database changed"):
            self.install([root])
        self.assertEqual(self.config(), old)

    def test_unsafe_config_and_symlink_logs_are_refused(self):
        self.install()
        self.paths.config.chmod(0o644)
        with self.assertRaisesRegex(installer.InstallError, "owner-only"):
            self.install()
        self.paths.config.chmod(0o600)
        log = self.paths.logs / "stdout.log"
        log.unlink()
        target = self.base / "must not change"
        target.write_text("original")
        log.symlink_to(target)
        with self.assertRaisesRegex(installer.InstallError, "symlink"):
            self.install()
        self.assertEqual(target.read_text(), "original")

    def test_startup_is_scoped_to_one_gui_label(self):
        self.launch_responses["bootout"] = (3, "", "Boot-out failed: 3: No such process")
        self.install(no_start=False)
        self.assertEqual(self.launch_calls(), [
            [installer.LAUNCHCTL, "bootout", f"gui/{os.getuid()}/{installer.LABEL}"],
            [installer.LAUNCHCTL, "bootstrap", f"gui/{os.getuid()}", str(self.paths.plist)],
            [installer.LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/{installer.LABEL}"],
            [installer.LAUNCHCTL, "print", f"gui/{os.getuid()}/{installer.LABEL}"],
        ])

    def test_bootout_permission_error_is_not_swallowed(self):
        self.launch_responses["bootout"] = (3, "", "Operation not permitted")
        with self.assertRaisesRegex(installer.InstallError, "bootout failed.*Operation not permitted"):
            self.install(no_start=False)
        self.assertEqual(len(self.launch_calls()), 1)

    def test_bootstrap_and_kickstart_failures_remain_visible(self):
        for action in ("bootstrap", "kickstart"):
            with self.subTest(action=action):
                self.launch_responses = {action: (5, "", "deliberate startup failure")}
                self.calls.clear()
                with self.assertRaisesRegex(installer.InstallError, "deliberate startup failure"):
                    self.install(no_start=False)
                self.assertEqual(self.launch_calls()[-1][1], action)

    def test_unhealthy_startup_is_an_error_with_log_path(self):
        with patch.object(installer, "runtime_status", side_effect=installer.InstallError("dead service")), patch.object(installer.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(installer.InstallError, "did not become healthy.*dead service.*logs"):
                self.install(no_start=False)

    def test_status_reports_launchctl_failure_and_still_reads_health(self):
        self.install()
        self.calls.clear()
        self.launch_responses["print"] = (113, "", "Could not find service")
        with self.assertRaisesRegex(installer.InstallError, "Could not find service"):
            installer.manage("status", self.paths)
        self.assertTrue(any(argv[-1] == "status" for argv, _, _ in self.calls))
        self.assertIn(str(self.paths.state / "service-status.json"), self.output.getvalue())
        self.assertIn(str(self.paths.logs), self.output.getvalue())

    def test_restart_and_stop_do_not_provision_or_remove_state(self):
        self.install()
        self.python_mock.reset_mock()
        state = self.paths.state / "store/retained"
        state.write_text("keep")
        self.calls.clear()
        installer.manage("restart", self.paths)
        self.assertEqual([argv[1] for argv in self.launch_calls()], ["bootout", "bootstrap", "kickstart", "print"])
        self.calls.clear()
        installer.manage("stop", self.paths)
        self.assertEqual([argv[1] for argv in self.launch_calls()], ["bootout"])
        self.assertEqual(state.read_text(), "keep")
        self.assertTrue(self.paths.config.exists())
        self.python_mock.assert_not_called()

    def test_management_refuses_modified_plist_environment(self):
        self.install()
        plist = plistlib.loads(self.paths.plist.read_bytes())
        plist["EnvironmentVariables"]["BEADS_DIR"] = "/untrusted"
        installer.private_write(self.paths.plist, plistlib.dumps(plist))
        with self.assertRaisesRegex(installer.InstallError, "explicit service variables"):
            installer.manage("restart", self.paths)
        self.assertEqual(self.launch_calls(), [])

    def test_relative_isolation_override_is_refused(self):
        with patch.dict(os.environ, {"HERDR_BEADS_STATE_HOME": "relative/state"}):
            with self.assertRaisesRegex(installer.InstallError, "must be absolute"):
                installer.Paths()

    def test_cli_exposes_failure_as_nonzero(self):
        with patch.object(installer, "install", side_effect=installer.InstallError("bootstrap failure")), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(installer.main(["install", "--no-start"]), 1)
            self.assertIn("bootstrap failure", stderr.getvalue())


class PythonProvisionTests(unittest.TestCase):
    def test_dedicated_venv_requirements_are_idempotent_and_real_python_is_used(self):
        with tempfile.TemporaryDirectory(prefix="service python ") as directory:
            root = Path(directory).resolve()
            paths = type("Paths", (), {"root": root, "python": root / "venv/bin/python"})()
            (root / "requirements.txt").write_text("jsonschema>=4.18,<5\nreferencing>=0.30\n")
            calls = []

            def checked(argv, **kwargs):
                calls.append([str(value) for value in argv])
                if "venv" in argv:
                    paths.python.parent.mkdir(parents=True)
                    paths.python.write_text("python")
                return ""

            with patch.object(installer, "checked", side_effect=checked):
                installer.provision_python(paths)
                installer.provision_python(paths)
            self.assertEqual(calls[0], [str(Path(sys.executable).resolve()), "-m", "venv", str(root / "venv")])
            self.assertEqual(sum("pip" in call for call in calls), 1)
            self.assertEqual(sum("import jsonschema, referencing" in call for call in calls), 2)
            self.assertEqual(stat.S_IMODE((root / "dependencies.sha256").stat().st_mode), 0o600)

    def test_dependency_install_failure_is_not_marked_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = type("Paths", (), {"root": root, "python": root / "venv/bin/python"})()
            paths.python.parent.mkdir(parents=True)
            paths.python.write_text("python")
            (root / "requirements.txt").write_text("jsonschema\nreferencing\n")
            with patch.object(installer, "checked", side_effect=installer.InstallError("pip failed")):
                with self.assertRaisesRegex(installer.InstallError, "pip failed"):
                    installer.provision_python(paths)
            self.assertFalse((root / "dependencies.sha256").exists())


if __name__ == "__main__":
    unittest.main()
