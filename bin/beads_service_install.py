#!/usr/bin/env python3
"""Install and manage the single per-user Herdr Beads LaunchAgent."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

LABEL = "com.herdr.beads"
LAUNCHCTL = "/bin/launchctl"
REQUIRED = ("git", "bd")
OPTIONAL = ("gh", "omp", "hc", "herdr-app")
LEGACY_HASHES = {
    "go-bd": {"0694d3e6aedd31f2a5c673b05d0781fff03ac6d5116cd0161a1a0103b1398622",
              "97c217d702f701efc2b82b5838179293fb1285cae49ddb3e2d932c706f666d8e"},
    "go-bd-session.mjs": {
        "5db42b644723f50ba16d0088548c7432f0934632ad4d8b682a0a724dd56fa05e",
        # Controller extension source recorded before the concrete-name cutover.
        "95767329f3ce684a424d310e3502240d030e62339c483e395dc8efe53bed7387",
    },
    "beads_epics.py": {"e356805a90580d1c360a0ee9be7b142ead67e40b6fe8aae319562cb92a366442",
                       "910fa25117dde84ebfc4400b0066e01af2cb22181d57933235f0b1569e3bbcd2"},
    "beads_presentation.py": {"33515fcfcd32e9ae0f2e2a03f7a464a04161af1d89aa29436301c4e272a2d2b5"},
}
MODS = ("beads-dashboard", "bead-detail", "beads-sidebar")


class InstallError(Exception):
    pass


def absolute(value, description):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise InstallError(f"{description} must be an absolute path: {value!r}")
    path = Path(value)
    if str(path.resolve()) != value:
        raise InstallError(f"{description} must be canonical (no aliases): {value}")
    return path


def owned(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise InstallError(f"Refusing symlink or foreign-owned path: {path}")
    return info


def private_directory(path):
    if not path.exists() and not path.is_symlink():
        path.mkdir(parents=True, mode=0o700)
    info = owned(path)
    if not stat.S_ISDIR(info.st_mode):
        raise InstallError(f"Expected directory: {path}")
    path.chmod(0o700)


def private_read(path):
    info = owned(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise InstallError(f"Expected owner-only regular file (chmod 600): {path}")
    return path.read_bytes()


def private_write(path, data):
    if path.exists() or path.is_symlink():
        info = owned(path)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError(f"Expected regular file: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(argv, *, cwd=None, env=None):
    try:
        result = subprocess.run([str(item) for item in argv], cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"Cannot execute {argv[0]}: {error}") from error
    return result


def checked(argv, **kwargs):
    result = run(argv, **kwargs)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise InstallError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result.stdout.strip()


class Paths:
    def __init__(self):
        self.home = Path.home().resolve()
        self.root = self.location("HERDR_BEADS_INSTALL_ROOT", self.home / ".local/share/herdr-beads-service")
        self.state_home = self.location("HERDR_BEADS_STATE_HOME", os.environ.get("XDG_STATE_HOME", str(self.home / ".local/state")))
        self.agents = self.location("HERDR_BEADS_LAUNCH_AGENTS", self.home / "Library/LaunchAgents")
        self.state = self.state_home / "herdr-beads"
        self.config = self.state / "service.json"
        self.plist = self.agents / f"{LABEL}.plist"
        self.logs = self.state / "logs"
        self.python = self.root / "venv/bin/python"
        self.app = self.location("HERDR_APP_HOME", Path(os.environ.get("XDG_CONFIG_HOME", str(self.home / ".config"))) / "herdr-app")
        self.bin_home = self.location("HERDR_BEADS_BIN_HOME", self.home / ".local/bin")
        self.journal = self.state / "install-cutover.json"

    @staticmethod
    def location(variable, default):
        value = os.environ.get(variable, str(default))
        if not Path(value).is_absolute():
            raise InstallError(f"{variable} must be absolute: {value}")
        # Parent symlinks (e.g. macOS /var) are resolved once at the boundary.
        path = Path(value)
        if path.is_symlink():
            raise InstallError(f"Refusing symlink for {variable}: {path}")
        return path.resolve()


def validate_config(config, paths):
    keys = {"schemaVersion", "pollSeconds", "socketPath", "storeRoot", "executables", "projects"}
    if not isinstance(config, dict) or set(config) != keys or type(config["schemaVersion"]) is not int or config["schemaVersion"] != 1:
        raise InstallError("Invalid service config: expected schemaVersion 1 and known fields only")
    poll = config["pollSeconds"]
    if type(poll) not in (int, float) or not 5 <= poll <= 300:
        raise InstallError("Invalid service config pollSeconds")
    for key, expected in (("socketPath", paths.state / "controller.sock"), ("storeRoot", paths.state / "store")):
        if absolute(config[key], key) != expected:
            raise InstallError(f"Invalid service config {key}: expected {expected}")
    executables = config["executables"]
    if not isinstance(executables, dict) or set(executables) != set(REQUIRED + OPTIONAL):
        raise InstallError("Invalid service config executables")
    for name, value in executables.items():
        if value is None and name in OPTIONAL:
            continue
        absolute(value, f"executable {name}")
    if not isinstance(config["projects"], list):
        raise InstallError("Invalid service config projects")
    for project in config["projects"]:
        if not isinstance(project, dict) or set(project) != {"repository", "beadsDir"}:
            raise InstallError("Invalid registered project")
        repository = project["repository"]
        if not isinstance(repository, dict) or set(repository) != {"root", "commonDir"}:
            raise InstallError("Invalid registered repository")
        for key, value in repository.items():
            absolute(value, f"repository {key}")
        absolute(project["beadsDir"], "beadsDir")
    return config


def load_config(paths):
    try:
        config = json.loads(private_read(paths.config))
    except (ValueError, OSError) as error:
        raise InstallError(f"Cannot read config {paths.config}: {error}") from error
    return validate_config(config, paths)


def discover_executables():
    result = {}
    for name in REQUIRED + OPTIONAL:
        value = shutil.which(name)
        if value:
            path = Path(value).resolve()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise InstallError(f"Executable is not runnable: {path}")
            result[name] = str(path)
        elif name in REQUIRED:
            raise InstallError(f"Required executable is missing from PATH: {name}")
        else:
            result[name] = None
    return result


def service_environment(paths, executables):
    account = pwd.getpwuid(os.getuid()).pw_name
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(paths.home / ".config"))).resolve()
    app_home = Path(os.environ.get("HERDR_APP_HOME", str(config_home / "herdr-app"))).resolve()
    directories = [str(Path(value).parent) for value in executables.values() if value]
    directories += [str(Path(value).resolve()) for value in ("/usr/bin", "/bin", "/usr/sbin", "/sbin")]
    return {"HOME": str(paths.home), "USER": account, "LOGNAME": account,
            "XDG_STATE_HOME": str(paths.state_home), "XDG_CONFIG_HOME": str(config_home),
            "HERDR_APP_HOME": str(app_home), "PATH": ":".join(dict.fromkeys(directories))}


def register_project(value, executables, environment):
    root = absolute(value, "--project")
    if not root.is_dir():
        raise InstallError(f"Project directory is missing: {root}")
    git = executables["git"]
    common = Path(checked([git, "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root, env=environment)).resolve()
    top = Path(checked([git, "rev-parse", "--show-toplevel"], cwd=root, env=environment)).resolve()
    canonical = common.parent if common.name == ".git" else top
    if root != canonical:
        raise InstallError(f"--project must name the canonical repository: {canonical}")
    env = dict(environment)
    if (root / ".beads").is_dir():
        env["BEADS_DIR"] = str(root / ".beads")
    try:
        location = json.loads(checked([executables["bd"], "--sandbox", "where", "--json"], cwd=root, env=env))
        beads = Path(location["path"]).resolve()
        context_env = {**environment, "BEADS_DIR": str(beads)}
        context = json.loads(checked([executables["bd"], "--sandbox", "context", "--json"], cwd=root, env=context_env))
        if not beads.is_dir() or str(Path(context["repo_root"]).resolve()) != str(root) or str(Path(context["beads_dir"]).resolve()) != str(beads):
            raise InstallError(f"Beads context does not match repository: {root}")
    except (ValueError, KeyError, TypeError) as error:
        raise InstallError(f"Invalid Beads project identity for {root}: {error}") from error
    return {"repository": {"root": str(root), "commonDir": str(common)}, "beadsDir": str(beads)}


def ensure_machine(paths, *, fresh=False):
    """Import the legacy UUID without replacing either established record."""
    canonical = paths.state / "machine-id"
    legacy = paths.state_home / "go-bd/machine-id"
    identities = {}
    for path in (canonical, legacy):
        if not path.exists() and not path.is_symlink():
            continue
        try:
            value = private_read(path).decode().strip()
            if str(uuid.UUID(value)) != value:
                raise ValueError("noncanonical UUID")
        except (ValueError, UnicodeError) as error:
            raise InstallError(f"Invalid machine identity; restore original, never replace: {path}") from error
        identities[path] = value
    if len(set(identities.values())) > 1:
        raise InstallError("Canonical and legacy machine identities conflict; restore original files, never replace either")
    if canonical in identities:
        return
    if not identities and not fresh:
        raise InstallError(f"Established machine identity is missing; restore original, never replace: {canonical}")
    value = next(iter(identities.values())) if identities else str(uuid.uuid4())
    private_directory(paths.state)
    descriptor, temporary = tempfile.mkstemp(prefix=".machine-id.", dir=canonical.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Publish only the complete file, without replacing a concurrent identity.
        try:
            os.link(temporary, canonical)
        except FileExistsError:
            return ensure_machine(paths)
        directory = os.open(canonical.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def package_plan(source, paths):
    """Read every payload before stopping the owner or changing installed files."""
    if source == paths.root or paths.root.is_relative_to(source):
        raise InstallError("Service install root must be outside the source repository")
    required = ("bin/beads_service.py", "bin/beads_ui.py", "bin/beads_omp_session.mjs",
                "bin/herdr-beads", "requirements.txt")
    if any(not (source / name).is_file() for name in required) or not any((source / "schemas/v1").glob("*.json")):
        raise InstallError(f"Controller sources, concrete omp extension or version-1 schemas are missing: {source}")
    origins = list(sorted((source / "bin").glob("*.py")))
    origins += [source / "bin/beads_omp_session.mjs", source / "bin/herdr-beads", source / "requirements.txt"]
    origins += [item for item in sorted((source / "schemas/v1").rglob("*")) if item.is_file()]
    package = {paths.root / item.relative_to(source): item.read_bytes() for item in origins}
    ui = {}
    for mod in MODS:
        folder = source / "mods" / mod
        files = [folder / "mod.json", *sorted(folder.glob("*.js"))]
        if len(files) < 2:
            raise InstallError(f"Missing mod sources: {folder}")
        for item in files:
            ui[paths.app / "mods" / mod / item.name] = item.read_bytes()
    installation = {"lifecycleBridge": str(paths.root / "bin/beads_ui.py"),
                    "lifecyclePython": str(paths.python), "lifecycleConfig": str(paths.config)}
    ui[paths.app / "mods/beads-dashboard/installation.js"] = ("export default " + json.dumps(installation) + ";\n").encode()
    for name in ("beads-workspace.json", "beads-workspace.svg"):
        target = paths.app / "layouts" / name
        if not target.exists() and not target.is_symlink():
            ui[target] = (source / "layouts" / name).read_bytes()
    return package, ui


def preflight_target(path):
    if path.exists() or path.is_symlink():
        if not stat.S_ISREG(owned(path).st_mode):
            raise InstallError(f"Expected regular installed file: {path}")
    for parent in path.parents:
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise InstallError(f"Refusing unsafe installed parent: {parent}")
            if info.st_uid not in (0, os.getuid()):
                raise InstallError(f"Refusing foreign-owned installed parent: {parent}")


def legacy_plan(source, paths):
    candidates = [paths.bin_home / name for name in LEGACY_HASHES]
    candidates += [paths.root / "bin/go-bd", paths.root / "bin/go-bd-session.mjs"]
    result = {}
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        preflight_target(path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        allowed = set(LEGACY_HASHES[path.name])
        if path.name in ("beads_epics.py", "beads_presentation.py"):
            allowed.add(hashlib.sha256((source / "bin" / path.name).read_bytes()).hexdigest())
        if digest not in allowed:
            raise InstallError(f"Refusing unrelated legacy executable; retain and review before cutover: {path} (sha256 {digest})")
        result[path] = data
    return result


def startup_terminal_active(path, executables, environment):
    """Under launch/controller locks, prove whether immutable creation argv is live."""
    hc = executables.get("hc")
    if not hc:
        raise InstallError("hc is unavailable; cannot establish whether the retained startup is live")
    try:
        listing = json.loads(checked([hc, "ls", "--all", "--json"], env=environment))
        rows = listing["terminals"]
        if not isinstance(rows, list):
            raise ValueError("terminal list is not an array")
        marker = f"GO_BD_SESSION_CONFIG={path}"
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("address"), str):
                raise ValueError("invalid terminal identity")
            if row.get("state") != "live":
                continue
            info = json.loads(checked([hc, "get", row["address"], "--json"], env=environment))
            if info.get("state") == "finished":
                continue
            argv = info.get("argv")
            if info.get("state") != "live" or not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
                raise ValueError("live terminal creation argv is unavailable")
            if marker in argv:
                return True
        return False
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise InstallError(f"Cannot validate retained terminal observations: {error}") from error


def preflight_startups(paths, executables, environment):
    """Live old startup argv needs an ack; dead failed history stays retained."""
    legacy = paths.state_home / "go-bd"
    configs = list(legacy.rglob("launch-*.json")) if legacy.exists() else []
    omp = paths.state / "omp"
    if omp.exists():
        configs += [path for path in omp.glob("*/*.json") if path.stem.isdecimal()]
    for path in configs:
        if path.name.endswith("-ack.json"):
            continue
        try:
            config = json.loads(private_read(path))
            if not isinstance(config, dict):
                raise ValueError("configuration is not an object")
            if config.get("sessionConfigEnv") == "BEADS_OMP_SESSION_CONFIG":
                continue
            ack_path = absolute(config["ack"], "startup acknowledgement")
            ack = json.loads(private_read(ack_path))
            session = ack.get("session") if isinstance(ack, dict) and not ack.get("error") else None
            if (not isinstance(session, dict) or not session.get("session_id")
                    or not session.get("session_file")
                    or session.get("machine") != config["machine"]
                    or session.get("worktree") != config["worktree"]
                    or (config.get("previous") and session != config["previous"])):
                raise ValueError("startup acknowledgement has no matching durable session")
            if "fence" in config:
                observation = ack.get("observation") or {}
                if not isinstance(observation, dict) or observation.get("fence") != config["fence"] or observation.get("bindingId") != config["bindingId"]:
                    raise ValueError("startup acknowledgement belongs to another controller binding")
        except (OSError, ValueError, KeyError, TypeError) as error:
            try:
                active = startup_terminal_active(path, executables, environment)
            except InstallError as inspection:
                raise InstallError(f"Unconfirmed legacy startup; retain extension and review/recover before cutover: {path}: {error}; {inspection}") from inspection
            if active:
                raise InstallError(f"Unconfirmed legacy startup; retain extension and review/recover before cutover: {path}: {error}") from error


@contextlib.contextmanager
def exclusive_lock(path, description, *, legacy=False):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & (0o022 if legacy else 0o077) or info.st_nlink != 1:
            raise InstallError(f"Unsafe {description}: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError(f"Live {description}; stop/retry before installing (no files replaced): {path}") from error
        yield
    finally:
        os.close(descriptor)


def save_cutover(paths, journal, phase):
    journal["phase"] = phase
    private_write(Path(journal["receipt"]), (json.dumps(journal, indent=2) + "\n").encode())
    private_write(paths.journal, (json.dumps(journal, indent=2) + "\n").encode())


def publish_files(files):
    for target, data in files.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        private_write(target, data)
        if target.name == "herdr-beads":
            target.chmod(0o700)


def provision_python(paths):
    python = Path(sys.executable).resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise InstallError(f"Real Python interpreter unavailable: {python}")
    venv = paths.root / "venv"
    if venv.exists() or venv.is_symlink():
        private_directory(venv)
    if not paths.python.exists():
        checked([python, "-m", "venv", str(paths.root / "venv")])
    private_directory(paths.root / "venv")
    requirements = paths.root / "requirements.txt"
    fingerprint = hashlib.sha256(requirements.read_bytes() + str(python).encode()).hexdigest()
    marker = paths.root / "dependencies.sha256"
    current = private_read(marker).decode().strip() if marker.exists() else None
    if current != fingerprint:
        checked([paths.python, "-m", "pip", "install", "--disable-pip-version-check", "-r", requirements])
    checked([paths.python, "-c", "import jsonschema, referencing"])
    private_write(marker, (fingerprint + "\n").encode())


def create_plist(paths, environment):
    return {"Label": LABEL,
            "ProgramArguments": [str(paths.python), str(paths.root / "bin/beads_service.py"), "--config", str(paths.config), "serve"],
            "EnvironmentVariables": environment, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 15, "ProcessType": "Background", "Umask": 0o077,
            "WorkingDirectory": str(paths.root),
            "StandardOutPath": str(paths.logs / "stdout.log"),
            "StandardErrorPath": str(paths.logs / "stderr.log")}


def validate_plist(plist, paths):
    if not isinstance(plist, dict):
        raise InstallError(f"Expected LaunchAgent dictionary: {paths.plist}")
    expected = create_plist(paths, plist.get("EnvironmentVariables", {}))
    if plist != expected:
        raise InstallError(f"Unexpected LaunchAgent configuration: {paths.plist}")
    environment = plist["EnvironmentVariables"]
    required = {"HOME", "USER", "LOGNAME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "HERDR_APP_HOME", "PATH"}
    if not isinstance(environment, dict) or set(environment) != required or any(not isinstance(value, str) or not value for value in environment.values()):
        raise InstallError("LaunchAgent environment must contain only the explicit service variables")
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "HERDR_APP_HOME"):
        absolute(environment[key], key)
    if environment["XDG_STATE_HOME"] != str(paths.state_home):
        raise InstallError("LaunchAgent state home differs from configured state home")
    for directory in environment["PATH"].split(":"):
        absolute(directory, "PATH entry")
    return plist


def runtime_status(paths, environment):
    return checked([paths.python, paths.root / "bin/beads_service.py", "--config", paths.config, "status"],
                   env=environment)


def start(paths, environment, *, stopped=False):
    if not stopped:
        launchctl("bootout", paths)
    launchctl("bootstrap", paths)
    launchctl("kickstart", paths)
    print(launchctl("print", paths))
    deadline = time.monotonic() + 10
    while True:
        try:
            print(runtime_status(paths, environment))
            return
        except InstallError as error:
            if time.monotonic() >= deadline:
                raise InstallError(f"Service did not become healthy: {error}; inspect {paths.logs}") from error
            time.sleep(0.2)


def launchctl(action, paths):
    if sys.platform != "darwin":
        raise InstallError("launchd management requires macOS; use install --no-start for provisioning")
    target = f"gui/{os.getuid()}/{LABEL}"
    if action == "bootout":
        result = run([LAUNCHCTL, "bootout", target])
        detail = result.stderr.strip() or result.stdout.strip()
        absent = result.returncode == 3 and ("no such process" in detail.lower() or "could not find service" in detail.lower())
        if result.returncode and not absent:
            raise InstallError(f"launchctl bootout failed ({result.returncode}): {detail}")
        return detail
    arguments = {"bootstrap": ["bootstrap", f"gui/{os.getuid()}", str(paths.plist)],
                 "kickstart": ["kickstart", "-k", target], "print": ["print", target]}[action]
    return checked([LAUNCHCTL, *arguments])


def diagnostics(paths, config):
    print(f"Service: {LABEL}\nConfig: {paths.config}\nLaunchAgent: {paths.plist}\nPython: {paths.python}\nHealth: {paths.state / 'service-status.json'}\nLogs: {paths.logs}\nSocket: {config['socketPath']}\nStore: {config['storeRoot']}\nRegistered projects: {len(config['projects'])}")
    missing = [name for name in OPTIONAL if config["executables"][name] is None]
    if missing:
        print("Unavailable optional capabilities: " + ", ".join(missing))


def install(source, paths, projects, no_start):
    established = any(path.exists() or path.is_symlink() for path in (
        paths.state, paths.root, paths.plist, paths.state_home / "go-bd"))
    previous = load_config(paths) if paths.config.exists() or paths.config.is_symlink() else None
    executables = discover_executables()
    environment = service_environment(paths, executables)
    registered = list(previous["projects"]) if previous else []
    for value in projects:
        project = register_project(value, executables, environment)
        matching = [item for item in registered if item["repository"] == project["repository"]]
        if matching and any(item != project for item in matching):
            raise InstallError(f"Registered Beads database changed; restore/review config explicitly: {value}")
        if project not in registered:
            registered.append(project)
    for project in registered:
        repository = Path(project["repository"]["root"])
        if paths.root == repository or paths.root.is_relative_to(repository):
            raise InstallError(f"Service install root must be outside registered repositories: {repository}")
    config = {"schemaVersion": 1, "pollSeconds": previous["pollSeconds"] if previous else 30,
              "socketPath": str(paths.state / "controller.sock"), "storeRoot": str(paths.state / "store"),
              "executables": executables, "projects": registered}
    validate_config(config, paths)
    package, ui = package_plan(source.resolve(), paths)
    legacy = legacy_plan(source.resolve(), paths)
    plist = validate_plist(create_plist(paths, environment), paths)
    targets = [*package, *ui, paths.config, paths.plist, paths.journal,
               paths.logs / "stdout.log", paths.logs / "stderr.log"]
    for target in targets:
        preflight_target(target)
    ensure_machine(paths, fresh=not established)
    with exclusive_lock(paths.state / "install.lock", "installation"):
        with contextlib.ExitStack() as legacy_locks:
            legacy_state = paths.state_home / "go-bd"
            if legacy_state.exists():
                if not stat.S_ISDIR(owned(legacy_state).st_mode):
                    raise InstallError(f"Expected legacy state directory: {legacy_state}")
                for lock in sorted(legacy_state.rglob("*.lock")):
                    legacy_locks.enter_context(exclusive_lock(lock, "legacy launch", legacy=True))
            preflight_startups(paths, executables, environment)
            if not no_start:
                launchctl("bootout", paths)
            # Share the transport's actual singleton inode: no replacement under
            # a live controller, and no foreground server may race provisioning.
            with exclusive_lock(paths.state / "controller.sock.lock", "controller"):
                preflight_startups(paths, executables, environment)
                journal, originals = perform_cutover(paths, config, plist, package, ui, legacy)
            try:
                if no_start:
                    print("Provisioned only (--no-start); launchctl was not invoked.")
                else:
                    start(paths, environment, stopped=True)
                save_cutover(paths, journal, "activating-ui")
                publish_files(ui)
                save_cutover(paths, journal, "provisioned-no-start" if no_start else "active")
            except BaseException as error:
                # Return all mods to their pre-cutover bytes; retain the stopped
                # legacy executable archives for explicit operator recovery.
                for path in ui:
                    original = originals[path]
                    if original is None:
                        if path.exists():
                            path.unlink()
                    else:
                        private_write(path, original[0])
                        path.chmod(original[1])
                journal["error"] = str(error)
                save_cutover(paths, journal, "failed-ui-not-activated")
                raise
        diagnostics(paths, config)
        print(f"Installed concrete omp extension: {paths.root / 'bin/beads_omp_session.mjs'}")
        print(f"Cutover ownership/recovery evidence: {paths.journal}")
        print("History retained. Review and explicitly adopt existing sessions in the dashboard before resume.")
        print("Open Beads in the Herdr sidebar; no standalone launcher or legacy fallback is installed.")


def perform_cutover(paths, config, plist, package, ui, legacy):
    transaction = paths.state / "installations" / uuid.uuid4().hex
    private_directory(paths.state / "installations")
    private_directory(transaction)
    journal = {"schemaVersion": 1, "receipt": str(transaction / "receipt.json"),
               "phase": "prepared", "previousReceipt": None, "retired": [], "uiOriginals": [],
               "files": {str(path): hashlib.sha256(data).hexdigest() for path, data in {**package, **ui}.items()}}
    if paths.journal.exists():
        previous = json.loads(private_read(paths.journal))
        journal["previousReceipt"] = previous["receipt"]
    originals = {}
    for index, path in enumerate([*ui, *legacy]):
        if path.exists():
            data = path.read_bytes()
            mode = stat.S_IMODE(owned(path).st_mode)
            backup = transaction / f"{index}.original"
            private_write(backup, data)
            originals[path] = (data, mode)
            record = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                      "backup": str(backup), "mode": mode}
        else:
            originals[path] = None
            record = {"path": str(path), "backup": None}
        journal["retired" if path in legacy else "uiOriginals"].append(record)
    save_cutover(paths, journal, "prepared")
    try:
        for path, data in legacy.items():
            preflight_target(path)
            if path.read_bytes() != data:
                raise InstallError(f"Legacy executable changed during cutover; retained: {path}")
            path.unlink()
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        save_cutover(paths, journal, "legacy-retired")
        for directory in (paths.root, paths.state / "store", paths.logs):
            private_directory(directory)
        for target in package:
            for directory in reversed(target.parent.relative_to(paths.root).parents):
                private_directory(paths.root / directory)
            private_directory(target.parent)
        publish_files(package)
        provision_python(paths)
        private_write(paths.config, (json.dumps(config, indent=2) + "\n").encode())
        paths.agents.mkdir(parents=True, exist_ok=True, mode=0o700)
        owned(paths.agents)
        for name in ("stdout.log", "stderr.log"):
            log = paths.logs / name
            if log.exists():
                log.chmod(0o600)
            else:
                private_write(log, b"")
        private_write(paths.plist, plistlib.dumps(plist, sort_keys=True))
        save_cutover(paths, journal, "provisioned")
    except BaseException as error:
        journal["error"] = str(error)
        save_cutover(paths, journal, "failed-service-stopped")
        raise
    return journal, originals


def manage(action, paths):
    with exclusive_lock(paths.state / "install.lock", "installation"):
        if action == "restart" and paths.journal.exists():
            journal = json.loads(private_read(paths.journal))
            if journal.get("phase") not in ("active", "provisioned-no-start"):
                raise InstallError(f"Incomplete cutover; rerun install before restart. Evidence retained: {paths.journal}")
        return manage_locked(action, paths)


def manage_locked(action, paths):
    config = load_config(paths)
    diagnostics(paths, config)
    try:
        plist = plistlib.loads(private_read(paths.plist))
    except (ValueError, OSError, plistlib.InvalidFileException) as error:
        raise InstallError(f"Cannot read LaunchAgent {paths.plist}: {error}") from error
    validate_plist(plist, paths)
    if action == "stop":
        launchctl("bootout", paths)
        print("Stopped. Run restart to load the service again.")
    elif action == "restart":
        ensure_machine(paths)
        start(paths, plist["EnvironmentVariables"])
    else:
        errors = []
        try:
            print(launchctl("print", paths))
        except InstallError as error:
            errors.append(str(error))
        try:
            print(runtime_status(paths, plist["EnvironmentVariables"]))
        except InstallError as error:
            errors.append(str(error))
        if errors:
            raise InstallError("; ".join(errors))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("install", "status", "restart", "stop"), default="install")
    parser.add_argument("--project", action="append", default=[], metavar="/canonical/repository")
    parser.add_argument("--no-start", action="store_true", help="provision without invoking launchctl")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command != "install" and (args.project or args.no_start):
        parser.error("--project and --no-start apply only to install")
    try:
        paths = Paths()
        if args.command == "install":
            install(args.source, paths, args.project, args.no_start)
        else:
            manage(args.command, paths)
    except (InstallError, OSError) as error:
        print(f"Herdr Beads service: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
