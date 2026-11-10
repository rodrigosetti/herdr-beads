"""Isolated launchd packaging tests: no real launchctl, pip, or service process."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "bin"))
import beads_service_install as installer
from beads_contract import Refusal
from beads_identity import machine_id


class ServiceInstallTests(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        self.temporary = tempfile.TemporaryDirectory(prefix="beads service tests ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source repo"
        (self.source / "bin").mkdir(parents=True)
        (self.source / "schemas/v1").mkdir(parents=True)
        (self.source / "bin/beads_service.py").write_text("# packaged service\n")
        (self.source / "bin/beads_contract.py").write_text("# packaged dependency\n")
        (self.source / "bin/beads_ui.py").write_text("# packaged UI bridge\n")
        (self.source / "bin/beads_omp_session.mjs").write_text("export default function () {}\n")
        for name in ("beads_epics.py", "beads_presentation.py"):
            (self.source / "bin" / name).write_text(f"# packaged {name}\n")
        for mod in installer.MODS:
            folder = self.source / "mods" / mod
            folder.mkdir(parents=True)
            (folder / "mod.json").write_text(json.dumps({"name": mod}))
            (folder / "main.js").write_text(f"// controller mod {mod}\n")
        (self.source / "layouts").mkdir()
        (self.source / "layouts/beads-workspace.json").write_text('{"controller":true}\n')
        (self.source / "layouts/beads-workspace.svg").write_text("<svg/>\n")
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
            "HERDR_BEADS_BIN_HOME": str(self.base / "user bin"),
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

    def identity_file(self, *, legacy=False, value="12345678-1234-4234-8234-123456789abc"):
        path = self.paths.state_home / ("go-bd/machine-id" if legacy else "herdr-beads/machine-id")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        installer.private_write(path, (value + "\n").encode())
        return path

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
        identity = self.paths.state / "machine-id"
        self.assertEqual(stat.S_IMODE(identity.stat().st_mode), 0o600)
        self.assertFalse((self.paths.state_home / "go-bd").exists())
        self.assertEqual(machine_id(state_home=self.paths.state_home), identity.read_text().strip())
        extension = self.paths.root / "bin/beads_omp_session.mjs"
        self.assertEqual(extension.read_bytes(), (self.source / "bin/beads_omp_session.mjs").read_bytes())
        self.assertFalse((self.paths.root / "bin/go-bd-session.mjs").exists())
        self.assertFalse(self.paths.bin_home.exists())
        for mod in installer.MODS:
            self.assertEqual((self.paths.app / "mods" / mod / "main.js").read_bytes(),
                             (self.source / "mods" / mod / "main.js").read_bytes())
        self.assertEqual(json.loads(self.paths.journal.read_text())["phase"], "provisioned-no-start")

    def test_reinstall_preserves_projects_identity_logs_and_state(self):
        first = self.project("first repository")
        second = self.project("second repository")
        self.install([first])
        identity = self.paths.state / "machine-id"
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
        identity = self.paths.state / "machine-id"
        identity.write_text("bad identity\n")
        with self.assertRaisesRegex(installer.InstallError, "never replace"):
            self.install()
        self.assertEqual(identity.read_text(), "bad identity\n")
        with self.assertRaisesRegex(installer.InstallError, "never replace"):
            installer.manage("restart", self.paths)
        self.assertEqual(self.launch_calls(), [])

    def test_legacy_machine_identity_is_copied_without_regeneration_or_retirement(self):
        legacy = self.identity_file(legacy=True)
        original = legacy.read_bytes()
        self.install()
        canonical = self.paths.state / "machine-id"
        self.assertEqual(canonical.read_bytes(), original)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(machine_id(state_home=self.paths.state_home), original.decode().strip())
        legacy.unlink()
        self.install()
        self.assertEqual(machine_id(state_home=self.paths.state_home), original.decode().strip())
        self.assertEqual(canonical.read_bytes(), original)

    def test_runtime_imports_legacy_machine_identity_and_survives_legacy_removal(self):
        legacy = self.identity_file(legacy=True)
        original = legacy.read_bytes()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.paths.state_home)}):
            self.assertEqual(machine_id(), original.decode().strip())
            canonical = self.paths.state / "machine-id"
            self.assertEqual(canonical.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)
            self.assertEqual(legacy.read_bytes(), original)
            legacy.unlink()
            self.assertEqual(machine_id(), original.decode().strip())

    def test_interrupted_machine_import_never_publishes_an_incomplete_identity(self):
        legacy = self.identity_file(legacy=True)
        original = legacy.read_bytes()
        canonical = self.paths.state / "machine-id"
        actions = ((lambda: machine_id(state_home=self.paths.state_home), Refusal),
                   (lambda: installer.ensure_machine(self.paths), OSError))
        for action, error_type in actions:
            for operation in ("fsync", "link"):
                with self.subTest(action=action, operation=operation):
                    with patch.object(installer.os, operation, side_effect=OSError("interrupted publication")):
                        with self.assertRaises(error_type):
                            action()
                    self.assertFalse(canonical.exists())
                    self.assertEqual(legacy.read_bytes(), original)
                    self.assertEqual(list(self.paths.state.iterdir()), [])
        installer.ensure_machine(self.paths)
        self.assertEqual(canonical.read_bytes(), original)
        self.assertEqual(machine_id(state_home=self.paths.state_home), original.decode().strip())

    def test_machine_identity_conflict_refuses_both_records_untouched(self):
        legacy = self.identity_file(legacy=True)
        canonical = self.identity_file(value="87654321-4321-4321-8321-cba987654321")
        original = {path: path.read_bytes() for path in (legacy, canonical)}
        with self.assertRaisesRegex(installer.InstallError, "conflict"):
            self.install(no_start=False)
        with self.assertRaisesRegex(Refusal, "conflict"):
            machine_id(state_home=self.paths.state_home)
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertFalse(self.paths.config.exists())
        self.assertEqual(self.launch_calls(), [])

    def test_invalid_machine_identity_on_either_side_never_imports_or_replaces(self):
        legacy = self.identity_file(legacy=True)
        canonical = self.identity_file()
        valid = legacy.read_bytes()
        for invalid in (legacy, canonical):
            with self.subTest(path=invalid):
                installer.private_write(legacy, valid)
                installer.private_write(canonical, valid)
                invalid.write_text("invalid established identity\n")
                original = {path: path.read_bytes() for path in (legacy, canonical)}
                with self.assertRaisesRegex(installer.InstallError, "never replace"):
                    self.install()
                with self.assertRaisesRegex(Refusal, "never replace"):
                    machine_id(state_home=self.paths.state_home)
                self.assertEqual({path: path.read_bytes() for path in original}, original)
        canonical.unlink()
        legacy.write_text("invalid legacy identity\n")
        with self.assertRaises(installer.InstallError):
            self.install()
        with self.assertRaises(Refusal):
            machine_id(state_home=self.paths.state_home)
        self.assertFalse(canonical.exists())

    def test_missing_established_machine_identity_is_never_regenerated_on_install_or_restart(self):
        self.install()
        canonical = self.paths.state / "machine-id"
        canonical.unlink()
        with patch.object(installer.uuid, "uuid4") as generate:
            with self.assertRaisesRegex(installer.InstallError, "missing.*never replace"):
                self.install(no_start=False)
            with self.assertRaisesRegex(installer.InstallError, "missing.*never replace"):
                installer.manage("restart", self.paths)
        generate.assert_not_called()
        with self.assertRaisesRegex(Refusal, "restore"):
            machine_id(state_home=self.paths.state_home)
        self.assertFalse(canonical.exists())
        self.assertEqual(self.launch_calls(), [])

    def test_runtime_never_creates_a_fresh_machine_identity(self):
        with self.assertRaisesRegex(Refusal, "restore"):
            machine_id(state_home=self.paths.state_home)
        self.assertFalse(self.paths.state.exists())

    def test_legacy_state_without_identity_is_not_a_fresh_install(self):
        (self.paths.state_home / "go-bd").mkdir(parents=True)
        with patch.object(installer.uuid, "uuid4") as generate:
            with self.assertRaisesRegex(installer.InstallError, "missing.*never replace"):
                self.install()
        generate.assert_not_called()
        self.assertFalse((self.paths.state / "machine-id").exists())

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


    def legacy_copy(self, name="go-bd", *, packaged=False):
        path = (self.paths.root / "bin" if packaged else self.paths.bin_home) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"integration-owned fixture {name}\n".encode()
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        hashes = {key: set(value) for key, value in installer.LEGACY_HASHES.items()}
        hashes[name].add(digest)
        allowance = patch.object(installer, "LEGACY_HASHES", hashes)
        allowance.start()
        self.addCleanup(allowance.stop)
        return path, data

    def test_installed_dashboard_module_uses_isolated_controller_paths(self):
        if not self.node:
            self.skipTest("Node is required to exercise the installed ES module consumer")
        self.install()
        module = self.paths.app / "mods/beads-dashboard/installation.js"
        result = subprocess.run([self.node, "--input-type=module", "--eval",
                                 "import {readFileSync} from 'node:fs'; "
                                 "const code=readFileSync(process.argv[1]).toString('base64'); "
                                 "const {default: config}=await import('data:text/javascript;base64,'+code); "
                                 "console.log(JSON.stringify(config));", str(module)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "lifecycleBridge": str(self.paths.root / "bin/beads_ui.py"),
            "lifecyclePython": str(self.paths.python), "lifecycleConfig": str(self.paths.config)})

    def test_unknown_legacy_copy_refuses_before_service_or_ui_changes(self):
        self.paths.bin_home.mkdir()
        for name in installer.LEGACY_HASHES:
            with self.subTest(name=name):
                path = self.paths.bin_home / name
                path.write_text("unrelated personal tool\n")
                with self.assertRaisesRegex(installer.InstallError, "unrelated legacy executable"):
                    self.install(no_start=False)
                self.assertEqual(path.read_text(), "unrelated personal tool\n")
                self.assertEqual(self.launch_calls(), [])
                self.assertFalse(self.paths.config.exists())
                self.assertFalse(self.paths.app.exists())
                path.unlink()

    def test_symlink_and_foreign_legacy_copies_are_retained(self):
        path, data = self.legacy_copy()
        target = self.base / "foreign executable target"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(installer.InstallError, "symlink"):
            self.install(no_start=False)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), data)
        path.unlink()
        path.write_bytes(data)
        account = installer.pwd.getpwuid(os.getuid())
        with patch.object(installer.os, "getuid", return_value=os.getuid() + 1), patch.object(installer.pwd, "getpwuid", return_value=account):
            with self.assertRaisesRegex(installer.InstallError, "foreign-owned"):
                self.install(no_start=False)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.launch_calls(), [])

    def test_owned_legacy_retirement_has_durable_bytes_and_preserves_history_layouts(self):
        identity = self.identity_file(legacy=True)
        history = identity.parent / "historical receipt.json"
        history.write_text("unchanged history")
        copies = dict([self.legacy_copy(), self.legacy_copy("go-bd-session.mjs"),
                       self.legacy_copy("beads_epics.py"), self.legacy_copy("beads_presentation.py"),
                       self.legacy_copy("go-bd-session.mjs", packaged=True)])
        layouts = self.paths.app / "layouts"
        layouts.mkdir(parents=True)
        for name in ("beads-workspace.json", "beads-workspace.svg"):
            (layouts / name).write_text("personal layout")
        self.install()
        journal = json.loads(self.paths.journal.read_text())
        retired = {Path(record["path"]): record for record in journal["retired"]}
        self.assertEqual(set(retired), set(copies))
        for path, data in copies.items():
            self.assertFalse(path.exists())
            self.assertEqual(Path(retired[path]["backup"]).read_bytes(), data)
            self.assertEqual(retired[path]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(history.read_text(), "unchanged history")
        self.assertEqual(identity.read_bytes(), (self.paths.state / "machine-id").read_bytes())
        for name in ("beads-workspace.json", "beads-workspace.svg"):
            self.assertEqual((layouts / name).read_text(), "personal layout")
        first_receipt = Path(journal["receipt"])
        self.install()
        next_journal = json.loads(self.paths.journal.read_text())
        self.assertEqual(next_journal["previousReceipt"], str(first_receipt))
        self.assertTrue(first_receipt.is_file())
        self.assertEqual(self.launch_calls(), [])

    def test_live_controller_blocks_no_start_without_replacing_consumed_files(self):
        self.install()
        service = self.paths.root / "bin/beads_service.py"
        original = service.read_bytes()
        (self.source / "bin/beads_service.py").write_text("replacement service")
        config = self.paths.config.read_bytes()
        with (self.paths.state / "controller.sock.lock").open("r+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(installer.InstallError, "Live controller"):
                self.install()
        self.assertEqual(service.read_bytes(), original)
        self.assertEqual(self.paths.config.read_bytes(), config)
        self.assertEqual(self.launch_calls(), [])

    def test_live_legacy_launcher_blocks_cutover_and_retains_executable(self):
        self.identity_file(legacy=True)
        path, data = self.legacy_copy()
        lock = self.paths.state_home / "go-bd/project/issue-task.lock"
        lock.parent.mkdir()
        installer.private_write(lock, b"")
        lock.chmod(0o644)
        with lock.open("r+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(installer.InstallError, "Live legacy launch"):
                self.install(no_start=False)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.launch_calls(), [])
        self.assertFalse(self.paths.app.exists())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)
        self.install()
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)

    def test_group_writable_legacy_lock_is_refused_without_chmod(self):
        self.identity_file(legacy=True)
        lock = self.paths.state_home / "go-bd/project/issue-task.lock"
        lock.parent.mkdir()
        installer.private_write(lock, b"")
        lock.chmod(0o664)
        with self.assertRaisesRegex(installer.InstallError, "Unsafe legacy launch"):
            self.install(no_start=False)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o664)
        self.assertEqual(self.launch_calls(), [])

    def test_unconfirmed_startup_retains_extension_and_acknowledged_startup_survives(self):
        self.identity_file(legacy=True)
        extension, data = self.legacy_copy("go-bd-session.mjs")
        config_path = self.paths.state_home / "go-bd/project/launch-startup.json"
        config_path.parent.mkdir()
        ack = config_path.with_name("launch-startup-ack.json")
        session = {"session_id": "saved-session", "session_file": str(self.base / "retained transcript.jsonl"),
                   "machine": "machine-id", "worktree": str(self.base / "retained worktree")}
        config = {"ack": str(ack), "machine": session["machine"], "worktree": session["worktree"], "previous": None}
        installer.private_write(config_path, json.dumps(config).encode())
        for acknowledgement in (None, {"error": "not loaded"}, {"session": {**session, "machine": "another"}}):
            if acknowledgement is not None:
                installer.private_write(ack, json.dumps(acknowledgement).encode())
            with self.assertRaisesRegex(installer.InstallError, "Unconfirmed legacy startup"):
                self.install(no_start=False)
            self.assertEqual(extension.read_bytes(), data)
            self.assertEqual(self.launch_calls(), [])
        installer.private_write(ack, json.dumps({"session": session}).encode())
        original_config = config_path.read_bytes()
        original_ack = ack.read_bytes()
        self.install()
        self.assertFalse(extension.exists())
        self.assertEqual(config_path.read_bytes(), original_config)
        self.assertEqual(ack.read_bytes(), original_ack)

    def test_failed_historical_startup_is_retained_but_only_live_creation_blocks_retirement(self):
        self.identity_file(legacy=True)
        extension, data = self.legacy_copy("go-bd-session.mjs")
        config = self.paths.state_home / "go-bd/project/launch-ended.json"
        config.parent.mkdir()
        ack = config.with_name("launch-ended-ack.json")
        installer.private_write(config, json.dumps({"ack": str(ack)}).encode())
        installer.private_write(ack, b'{"error":"historical workspace missing"}\n')
        before = {path: path.read_bytes() for path in (config, ack)}
        hc = self.tools / "hc"
        hc.write_text("#!/bin/sh\nexit 0\n")
        hc.chmod(0o700)
        live = True

        def observed(argv, **kwargs):
            if Path(argv[0]).name != "hc":
                return self.fake_run(argv, **kwargs)
            if argv[1] == "ls":
                value = {"terminals": [{"address": "t1", "state": "live" if live else "finished"}]}
            else:
                value = {"state": "live", "argv": ["env", f"GO_BD_SESSION_CONFIG={config}", "omp"]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

        with patch.object(installer, "run", side_effect=observed):
            with self.assertRaisesRegex(installer.InstallError, "Unconfirmed legacy startup"):
                self.install()
            self.assertEqual(extension.read_bytes(), data)
            live = False
            self.install()
        self.assertFalse(extension.exists())
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_old_controller_startup_requires_exact_fence_ack_before_module_retirement(self):
        self.install()
        extension, data = self.legacy_copy("go-bd-session.mjs", packaged=True)
        config_path = self.paths.state / "omp/retained-run/1.json"
        config_path.parent.mkdir(parents=True)
        ack = config_path.with_suffix(".ack.json")
        session = {"session_id": "exact", "session_file": str(self.base / "transcript.jsonl"),
                   "machine": "this-machine", "worktree": str(self.base / "workspace")}
        fence = {"runId": "retained-run", "generation": 1}
        config = {"ack": str(ack), "machine": session["machine"], "worktree": session["worktree"],
                  "previous": None, "fence": fence, "bindingId": "exact-binding"}
        installer.private_write(config_path, json.dumps(config).encode())
        installer.private_write(ack, json.dumps({"session": session, "observation": {
            "fence": fence, "bindingId": "another-binding"}}).encode())
        with self.assertRaisesRegex(installer.InstallError, "another controller binding"):
            self.install()
        self.assertEqual(extension.read_bytes(), data)
        installer.private_write(ack, json.dumps({"session": session, "observation": {
            "fence": fence, "bindingId": "exact-binding"}}).encode())
        self.install()
        self.assertFalse(extension.exists())
        self.assertTrue(config_path.is_file())
        self.assertTrue(ack.is_file())

    def test_second_installer_cannot_replace_consumer_files(self):
        self.install()
        original = self.paths.config.read_bytes()
        with (self.paths.state / "install.lock").open("r+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(installer.InstallError, "Live installation"):
                self.install()
        self.assertEqual(self.paths.config.read_bytes(), original)
        self.assertEqual(self.launch_calls(), [])

    def test_service_replacement_follows_stop_and_ui_switch_follows_health(self):
        self.install()
        mod = self.paths.app / "mods/beads-dashboard/main.js"
        original_mod = mod.read_bytes()
        (self.source / "mods/beads-dashboard/main.js").write_text("// new controller UI")
        (self.source / "bin/beads_service.py").write_text("# new service")
        self.calls.clear()
        publish = installer.publish_files
        order = []

        def observe_publish(files):
            if self.paths.root / "bin/beads_service.py" in files:
                self.assertEqual([argv[1] for argv in self.launch_calls()], ["bootout"])
                order.append("package")
            else:
                self.assertEqual(order, ["package", "healthy"])
                order.append("ui")
            return publish(files)

        def healthy(paths, environment):
            self.assertEqual(mod.read_bytes(), original_mod)
            self.assertEqual((paths.root / "bin/beads_service.py").read_text(), "# new service")
            order.append("healthy")
            return '{"state":"running"}'

        with patch.object(installer, "publish_files", side_effect=observe_publish), patch.object(installer, "runtime_status", side_effect=healthy):
            self.install(no_start=False)
        self.assertEqual(order, ["package", "healthy", "ui"])
        self.assertEqual(mod.read_text(), "// new controller UI")
        self.assertEqual(json.loads(self.paths.journal.read_text())["phase"], "active")

    def test_failed_provisioning_preserves_ui_and_forward_recovery_receipt(self):
        self.install()
        mod = self.paths.app / "mods/beads-dashboard/main.js"
        original = mod.read_bytes()
        (self.source / "mods/beads-dashboard/main.js").write_text("// upgraded UI")
        with patch.object(installer, "provision_python", side_effect=installer.InstallError("dependency unavailable")):
            with self.assertRaisesRegex(installer.InstallError, "dependency unavailable"):
                self.install(no_start=False)
        self.assertEqual(mod.read_bytes(), original)
        journal = json.loads(self.paths.journal.read_text())
        self.assertEqual(journal["phase"], "failed-service-stopped")
        with self.assertRaisesRegex(installer.InstallError, "Incomplete cutover"):
            installer.manage("restart", self.paths)
        self.install()
        self.assertEqual(mod.read_text(), "// upgraded UI")
        self.assertEqual(json.loads(self.paths.journal.read_text())["previousReceipt"], journal["receipt"])

    def test_ui_publication_failure_rolls_back_existing_mods(self):
        self.install()
        old = {self.paths.app / "mods" / mod / "main.js":
               (self.paths.app / "mods" / mod / "main.js").read_bytes() for mod in installer.MODS}
        for mod in installer.MODS:
            (self.source / "mods" / mod / "main.js").write_text("// new controller UI")
        publish = installer.publish_files

        def partial_publish(files):
            if next(iter(files)).is_relative_to(self.paths.app):
                target = next(iter(old))
                installer.private_write(target, b"partial upgrade")
                raise OSError("interrupted UI publication")
            return publish(files)

        with patch.object(installer, "publish_files", side_effect=partial_publish):
            with self.assertRaisesRegex(OSError, "interrupted UI publication"):
                self.install()
        self.assertEqual({path: path.read_bytes() for path in old}, old)
        self.assertEqual(json.loads(self.paths.journal.read_text())["phase"], "failed-ui-not-activated")


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
