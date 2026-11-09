#!/usr/bin/env python3
"""Install and manage the single per-user Herdr Beads LaunchAgent."""
import argparse
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


def ensure_machine(paths):
    directory = paths.state_home / "go-bd"
    private_directory(directory)
    path = directory / "machine-id"
    if path.exists() or path.is_symlink():
        try:
            value = private_read(path).decode().strip()
            if str(uuid.UUID(value)) != value:
                raise ValueError("noncanonical UUID")
        except (ValueError, UnicodeError) as error:
            raise InstallError(f"Invalid machine identity; restore original, never replace: {path}") from error
        return
    # Exclusive creation never replaces an identity created concurrently.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return ensure_machine(paths)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(str(uuid.uuid4()) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def package_sources(source, paths):
    if source == paths.root or paths.root.is_relative_to(source):
        raise InstallError("Service install root must be outside the source repository")
    if not (source / "bin/beads_service.py").is_file() or not any((source / "schemas/v1").glob("*.json")):
        raise InstallError(f"Service sources or version-1 schemas are missing: {source}")
    private_directory(paths.root)
    private_directory(paths.root / "bin")
    private_directory(paths.root / "schemas")
    private_directory(paths.root / "schemas/v1")
    files = [(item, paths.root / "bin" / item.name) for item in sorted((source / "bin").glob("*.py"))]
    files.extend((item, paths.root / "bin" / item.name) for item in sorted((source / "bin").glob("*.mjs")))
    files.append((source / "bin/herdr-beads", paths.root / "bin/herdr-beads"))
    files.append((source / "requirements.txt", paths.root / "requirements.txt"))
    for item in sorted((source / "schemas/v1").rglob("*")):
        target = paths.root / "schemas/v1" / item.relative_to(source / "schemas/v1")
        if item.is_dir():
            private_directory(target)
        else:
            files.append((item, target))
    for origin, target in files:
        private_write(target, origin.read_bytes())
        if origin.name == "herdr-beads":
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


def start(paths, environment):
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
    for directory in (paths.state, paths.state / "store", paths.logs):
        private_directory(directory)
    ensure_machine(paths)
    package_sources(source.resolve(), paths)
    provision_python(paths)
    private_write(paths.config, (json.dumps(config, indent=2) + "\n").encode())
    # LaunchAgents may contain unrelated jobs; preserve the directory's existing mode.
    if not paths.agents.exists():
        paths.agents.mkdir(parents=True, mode=0o700)
    if not stat.S_ISDIR(owned(paths.agents).st_mode):
        raise InstallError(f"Expected LaunchAgents directory: {paths.agents}")
    for name in ("stdout.log", "stderr.log"):
        log = paths.logs / name
        if log.exists() or log.is_symlink():
            info = owned(log)
            if not stat.S_ISREG(info.st_mode):
                raise InstallError(f"Expected regular log file: {log}")
            log.chmod(0o600)
        else:
            private_write(log, b"")
    plist = validate_plist(create_plist(paths, environment), paths)
    private_write(paths.plist, plistlib.dumps(plist, sort_keys=True))
    diagnostics(paths, config)
    if no_start:
        print("Provisioned only (--no-start); launchctl was not invoked.")
    else:
        start(paths, environment)


def manage(action, paths):
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
