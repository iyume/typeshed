#!/usr/bin/env python3
"""Run mypy on typeshed's stdlib and third-party stubs."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, NamedTuple, Tuple

if TYPE_CHECKING:
    from _typeshed import StrPath

from typing_extensions import Annotated, TypeAlias

import tomli

from utils import (
    VERSIONS_RE as VERSION_LINE_RE,
    PackageDependencies,
    VenvInfo,
    colored,
    get_gitignore_spec,
    get_mypy_req,
    get_recursive_requirements,
    make_venv,
    print_error,
    print_success_msg,
    spec_matches_path,
    strip_comments,
)

# Fail early if mypy isn't installed
try:
    import mypy  # noqa: F401
except ImportError:
    print_error("Cannot import mypy. Did you install it?")
    sys.exit(1)

SUPPORTED_VERSIONS = ["3.11", "3.10", "3.9", "3.8", "3.7"]
SUPPORTED_PLATFORMS = ("linux", "win32", "darwin")
DIRECTORIES_TO_TEST = [Path("stdlib"), Path("stubs")]

ReturnCode: TypeAlias = int
VersionString: TypeAlias = Annotated[str, "Must be one of the entries in SUPPORTED_VERSIONS"]
VersionTuple: TypeAlias = Tuple[int, int]
Platform: TypeAlias = Annotated[str, "Must be one of the entries in SUPPORTED_PLATFORMS"]


class CommandLineArgs(argparse.Namespace):
    verbose: int
    filter: list[Path]
    exclude: list[Path] | None
    python_version: list[VersionString] | None
    platform: list[Platform] | None


def valid_path(cmd_arg: str) -> Path:
    """Helper function for argument-parsing"""
    path = Path(cmd_arg)
    if not path.exists():
        raise argparse.ArgumentTypeError(f'"{path}" does not exist in typeshed!')
    if not (path in DIRECTORIES_TO_TEST or any(directory in path.parents for directory in DIRECTORIES_TO_TEST)):
        raise argparse.ArgumentTypeError('mypy_test.py only tests the stubs found in the "stdlib" and "stubs" directories')
    return path


parser = argparse.ArgumentParser(
    description="Typecheck typeshed's stubs with mypy. Patterns are unanchored regexps on the full path."
)
if sys.version_info < (3, 8):

    class ExtendAction(argparse.Action):
        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: Sequence[str],
            option_string: object = None,
        ) -> None:
            items = getattr(namespace, self.dest) or []
            items.extend(values)
            setattr(namespace, self.dest, items)

    parser.register("action", "extend", ExtendAction)
parser.add_argument(
    "filter",
    type=valid_path,
    nargs="*",
    help='Test these files and directories (defaults to all files in the "stdlib" and "stubs" directories)',
)
parser.add_argument("-x", "--exclude", type=valid_path, nargs="*", help="Exclude these files and directories")
parser.add_argument("-v", "--verbose", action="count", default=0, help="More output")
parser.add_argument(
    "-p",
    "--python-version",
    type=str,
    choices=SUPPORTED_VERSIONS,
    nargs="*",
    action="extend",
    help="These versions only (major[.minor])",
)
parser.add_argument(
    "--platform",
    choices=SUPPORTED_PLATFORMS,
    nargs="*",
    action="extend",
    help="Run mypy for certain OS platforms (defaults to sys.platform only)",
)


@dataclass
class TestConfig:
    """Configuration settings for a single run of the `test_typeshed` function."""

    verbose: int
    filter: list[Path]
    exclude: list[Path]
    version: VersionString
    platform: Platform


def log(args: TestConfig, *varargs: object) -> None:
    if args.verbose >= 2:
        print(colored(" ".join(map(str, varargs)), "blue"))


def match(path: Path, args: TestConfig) -> bool:
    for excluded_path in args.exclude:
        if path == excluded_path:
            log(args, path, "explicitly excluded")
            return False
        if excluded_path in path.parents:
            log(args, path, f'is in an explicitly excluded directory "{excluded_path}"')
            return False
    for included_path in args.filter:
        if path == included_path:
            log(args, path, "was explicitly included")
            return True
        if included_path in path.parents:
            log(args, path, f'is in an explicitly included directory "{included_path}"')
            return True
    log_msg = (
        f'is implicitly excluded: was not in any of the directories or paths specified on the command line: "{args.filter!r}"'
    )
    log(args, path, log_msg)
    return False


def parse_versions(fname: StrPath) -> dict[str, tuple[VersionTuple, VersionTuple]]:
    result = {}
    with open(fname, encoding="UTF-8") as f:
        for line in f:
            line = strip_comments(line)
            if line == "":
                continue
            m = VERSION_LINE_RE.match(line)
            assert m, f"invalid VERSIONS line: {line}"
            mod: str = m.group(1)
            min_version = parse_version(m.group(2))
            max_version = parse_version(m.group(3)) if m.group(3) else (99, 99)
            result[mod] = min_version, max_version
    return result


_VERSION_RE = re.compile(r"^([23])\.(\d+)$")


def parse_version(v_str: str) -> tuple[int, int]:
    m = _VERSION_RE.match(v_str)
    assert m, f"invalid version: {v_str}"
    return int(m.group(1)), int(m.group(2))


def add_files(files: list[Path], module: Path, args: TestConfig) -> None:
    """Add all files in package or module represented by 'name' located in 'root'."""
    if module.is_file() and module.suffix == ".pyi":
        if match(module, args):
            files.append(module)
    else:
        files.extend(sorted(file for file in module.rglob("*.pyi") if match(file, args)))


class MypyDistConf(NamedTuple):
    module_name: str
    values: dict[str, dict[str, Any]]


# The configuration section in the metadata file looks like the following, with multiple module sections possible
# [mypy-tests]
# [mypy-tests.yaml]
# module_name = "yaml"
# [mypy-tests.yaml.values]
# disallow_incomplete_defs = true
# disallow_untyped_defs = true


def add_configuration(configurations: list[MypyDistConf], distribution: str) -> None:
    with Path("stubs", distribution, "METADATA.toml").open("rb") as f:
        data = tomli.load(f)

    mypy_tests_conf = data.get("mypy-tests")
    if not mypy_tests_conf:
        return

    assert isinstance(mypy_tests_conf, dict), "mypy-tests should be a section"
    for section_name, mypy_section in mypy_tests_conf.items():
        assert isinstance(mypy_section, dict), f"{section_name} should be a section"
        module_name = mypy_section.get("module_name")

        assert module_name is not None, f"{section_name} should have a module_name key"
        assert isinstance(module_name, str), f"{section_name} should be a key-value pair"

        values = mypy_section.get("values")
        assert values is not None, f"{section_name} should have a values section"
        assert isinstance(values, dict), "values should be a section"

        configurations.append(MypyDistConf(module_name, values.copy()))


def run_mypy(
    args: TestConfig,
    configurations: list[MypyDistConf],
    files: list[Path],
    *,
    testing_stdlib: bool,
    non_types_dependencies: bool,
    venv_info: VenvInfo,
    mypypath: str | None = None,
) -> ReturnCode:
    env_vars = dict(os.environ)
    if mypypath is not None:
        env_vars["MYPYPATH"] = mypypath
    with tempfile.NamedTemporaryFile("w+") as temp:
        temp.write("[mypy]\n")
        for dist_conf in configurations:
            temp.write(f"[mypy-{dist_conf.module_name}]\n")
            for k, v in dist_conf.values.items():
                temp.write(f"{k} = {v}\n")
        temp.flush()

        flags = [
            "--python-version",
            args.version,
            "--show-traceback",
            "--warn-incomplete-stub",
            "--no-error-summary",
            "--platform",
            args.platform,
            "--custom-typeshed-dir",
            str(Path(__file__).parent.parent),
            "--strict",
            # Stub completion is checked by pyright (--allow-*-defs)
            "--allow-untyped-defs",
            "--allow-incomplete-defs",
            "--allow-subclassing-any",  # See #9491
            "--enable-error-code",
            "ignore-without-code",
            "--config-file",
            temp.name,
        ]
        if not testing_stdlib:
            flags.append("--explicit-package-bases")
        if not non_types_dependencies:
            flags.append("--no-site-packages")

        mypy_args = [*flags, *map(str, files)]
        mypy_command = [venv_info.python_exe, "-m", "mypy"] + mypy_args
        if args.verbose:
            print(colored(f"running {' '.join(mypy_command)}", "blue"))
        result = subprocess.run(mypy_command, capture_output=True, text=True, env=env_vars)
        if result.returncode:
            print_error("failure\n")
            if result.stdout:
                print_error(result.stdout)
            if result.stderr:
                print_error(result.stderr)
            if non_types_dependencies and args.verbose:
                print("Ran with the following environment:")
                subprocess.run([venv_info.pip_exe, "freeze", "--all"])
                print()
        else:
            print_success_msg()
        return result.returncode


def add_third_party_files(
    distribution: str, files: list[Path], args: TestConfig, configurations: list[MypyDistConf], seen_dists: set[str]
) -> None:
    if distribution in seen_dists:
        return
    seen_dists.add(distribution)
    seen_dists.update(get_recursive_requirements(distribution).typeshed_pkgs)
    root = Path("stubs", distribution)
    for name in os.listdir(root):
        if name.startswith("."):
            continue
        add_files(files, (root / name), args)
        add_configuration(configurations, distribution)


class TestResults(NamedTuple):
    exit_code: int
    files_checked: int


def test_third_party_distribution(
    distribution: str, args: TestConfig, venv_info: VenvInfo, *, non_types_dependencies: bool
) -> TestResults:
    """Test the stubs of a third-party distribution.

    Return a tuple, where the first element indicates mypy's return code
    and the second element is the number of checked files.
    """

    files: list[Path] = []
    configurations: list[MypyDistConf] = []
    seen_dists: set[str] = set()
    add_third_party_files(distribution, files, args, configurations, seen_dists)

    if not files and args.filter:
        return TestResults(0, 0)

    print(f"testing {distribution} ({len(files)} files)... ", end="", flush=True)

    if not files:
        print_error("no files found")
        sys.exit(1)

    mypypath = os.pathsep.join(str(Path("stubs", dist)) for dist in seen_dists)
    if args.verbose:
        print(colored(f"\nMYPYPATH={mypypath}", "blue"))
    code = run_mypy(
        args,
        configurations,
        files,
        venv_info=venv_info,
        mypypath=mypypath,
        testing_stdlib=False,
        non_types_dependencies=non_types_dependencies,
    )
    return TestResults(code, len(files))


def test_stdlib(code: int, args: TestConfig) -> TestResults:
    files: list[Path] = []
    stdlib = Path("stdlib")
    supported_versions = parse_versions(stdlib / "VERSIONS")
    for name in os.listdir(stdlib):
        if name == "VERSIONS" or name.startswith("."):
            continue
        module = Path(name).stem
        module_min_version, module_max_version = supported_versions[module]
        if module_min_version <= tuple(map(int, args.version.split("."))) <= module_max_version:
            add_files(files, (stdlib / name), args)

    if files:
        print(f"Testing stdlib ({len(files)} files)...", end="", flush=True)
        # We don't actually need pip for the stdlib testing
        venv_info = VenvInfo(pip_exe="", python_exe=sys.executable)
        this_code = run_mypy(args, [], files, venv_info=venv_info, testing_stdlib=True, non_types_dependencies=False)
        code = max(code, this_code)

    return TestResults(code, len(files))


_PRINT_LOCK = Lock()
_DISTRIBUTION_TO_VENV_MAPPING: dict[str, VenvInfo] = {}


def setup_venv_for_external_requirements_set(requirements_set: frozenset[str], tempdir: Path) -> tuple[frozenset[str], VenvInfo]:
    venv_dir = tempdir / f".venv-{hash(requirements_set)}"
    return requirements_set, make_venv(venv_dir)


def install_requirements_for_venv(venv_info: VenvInfo, args: TestConfig, external_requirements: frozenset[str]) -> None:
    # Use --no-cache-dir to avoid issues with concurrent read/writes to the cache
    pip_command = [venv_info.pip_exe, "install", get_mypy_req(), *sorted(external_requirements), "--no-cache-dir"]
    if args.verbose:
        with _PRINT_LOCK:
            print(colored(f"Running {pip_command}", "blue"))
    try:
        subprocess.run(pip_command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(e.stderr)
        raise


def setup_virtual_environments(distributions: dict[str, PackageDependencies], args: TestConfig, tempdir: Path) -> None:
    """Logic necessary for testing stubs with non-types dependencies in isolated environments."""
    # STAGE 1: Determine which (if any) stubs packages require virtual environments.
    # Group stubs packages according to their external-requirements sets

    # We don't actually need pip if there aren't any external dependencies
    no_external_dependencies_venv = VenvInfo(pip_exe="", python_exe=sys.executable)
    external_requirements_to_distributions: defaultdict[frozenset[str], list[str]] = defaultdict(list)
    num_pkgs_with_external_reqs = 0

    for distribution_name, requirements in distributions.items():
        if requirements.external_pkgs:
            num_pkgs_with_external_reqs += 1
            external_requirements = frozenset(requirements.external_pkgs)
            external_requirements_to_distributions[external_requirements].append(distribution_name)
        else:
            _DISTRIBUTION_TO_VENV_MAPPING[distribution_name] = no_external_dependencies_venv

    # Exit early if there are no stubs packages that have non-types dependencies
    if num_pkgs_with_external_reqs == 0:
        if args.verbose:
            print(colored("No additional venvs are required to be set up", "blue"))
        return

    # STAGE 2: Setup a virtual environment for each unique set of external requirements
    requirements_sets_to_venvs: dict[frozenset[str], VenvInfo] = {}

    if args.verbose:
        num_venvs = len(external_requirements_to_distributions)
        msg = (
            f"Setting up {num_venvs} venv{'s' if num_venvs != 1 else ''} "
            f"for {num_pkgs_with_external_reqs} "
            f"distribution{'s' if num_pkgs_with_external_reqs != 1 else ''}... "
        )
        print(colored(msg, "blue"), end="", flush=True)

    venv_start_time = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        venv_info_futures = [
            executor.submit(setup_venv_for_external_requirements_set, requirements_set, tempdir)
            for requirements_set in external_requirements_to_distributions
        ]
        for venv_info_future in concurrent.futures.as_completed(venv_info_futures):
            requirements_set, venv_info = venv_info_future.result()
            requirements_sets_to_venvs[requirements_set] = venv_info

    venv_elapsed_time = time.perf_counter() - venv_start_time

    if args.verbose:
        print(colored(f"took {venv_elapsed_time:.2f} seconds", "blue"))

    # STAGE 3: For each {virtual_environment: requirements_set} pairing,
    # `pip install` the requirements set into the virtual environment
    pip_start_time = time.perf_counter()

    # Limit workers to 10 at a time, since this makes network requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        pip_install_futures = [
            executor.submit(install_requirements_for_venv, venv_info, args, requirements_set)
            for requirements_set, venv_info in requirements_sets_to_venvs.items()
        ]
        concurrent.futures.wait(pip_install_futures)

    pip_elapsed_time = time.perf_counter() - pip_start_time

    if args.verbose:
        msg = f"Combined time for installing requirements across all venvs: {pip_elapsed_time:.2f} seconds"
        print(colored(msg, "blue"))

    # STAGE 4: Populate the _DISTRIBUTION_TO_VENV_MAPPING
    # so that we have a simple {distribution: venv_to_use} mapping to use for the rest of the test.
    for requirements_set, distribution_list in external_requirements_to_distributions.items():
        venv_to_use = requirements_sets_to_venvs[requirements_set]
        _DISTRIBUTION_TO_VENV_MAPPING.update(dict.fromkeys(distribution_list, venv_to_use))


def test_third_party_stubs(code: int, args: TestConfig, tempdir: Path) -> TestResults:
    print("Testing third-party packages...")
    files_checked = 0
    gitignore_spec = get_gitignore_spec()
    distributions_to_check: dict[str, PackageDependencies] = {}

    for distribution in sorted(os.listdir("stubs")):
        distribution_path = Path("stubs", distribution)

        if spec_matches_path(gitignore_spec, distribution_path):
            continue

        if (
            distribution_path in args.filter
            or Path("stubs") in args.filter
            or any(distribution_path in path.parents for path in args.filter)
        ):
            distributions_to_check[distribution] = get_recursive_requirements(distribution)

    # If it's the first time test_third_party_stubs() has been called during this session,
    # setup the necessary virtual environments for testing the third-party stubs.
    # It should only be necessary to call setup_virtual_environments() once per session.
    if not _DISTRIBUTION_TO_VENV_MAPPING:
        setup_virtual_environments(distributions_to_check, args, tempdir)

    assert _DISTRIBUTION_TO_VENV_MAPPING.keys() == distributions_to_check.keys()

    for distribution, venv_info in _DISTRIBUTION_TO_VENV_MAPPING.items():
        non_types_dependencies = venv_info.python_exe != sys.executable
        this_code, checked = test_third_party_distribution(
            distribution, args, venv_info=venv_info, non_types_dependencies=non_types_dependencies
        )
        code = max(code, this_code)
        files_checked += checked

    return TestResults(code, files_checked)


def test_typeshed(code: int, args: TestConfig, tempdir: Path) -> TestResults:
    print(f"*** Testing Python {args.version} on {args.platform}")
    files_checked_this_version = 0
    stdlib_dir, stubs_dir = Path("stdlib"), Path("stubs")
    if stdlib_dir in args.filter or any(stdlib_dir in path.parents for path in args.filter):
        code, stdlib_files_checked = test_stdlib(code, args)
        files_checked_this_version += stdlib_files_checked
        print()

    if stubs_dir in args.filter or any(stubs_dir in path.parents for path in args.filter):
        code, third_party_files_checked = test_third_party_stubs(code, args, tempdir)
        files_checked_this_version += third_party_files_checked
        print()

    return TestResults(code, files_checked_this_version)


def main() -> None:
    args = parser.parse_args(namespace=CommandLineArgs())
    versions = args.python_version or SUPPORTED_VERSIONS
    platforms = args.platform or [sys.platform]
    filter = args.filter or DIRECTORIES_TO_TEST
    exclude = args.exclude or []
    code = 0
    total_files_checked = 0
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for version, platform in product(versions, platforms):
            config = TestConfig(args.verbose, filter, exclude, version, platform)
            code, files_checked_this_version = test_typeshed(code, args=config, tempdir=td_path)
            total_files_checked += files_checked_this_version
    if code:
        print_error(f"--- exit status {code}, {total_files_checked} files checked ---")
        sys.exit(code)
    if not total_files_checked:
        print_error("--- nothing to do; exit 1 ---")
        sys.exit(1)
    print(colored(f"--- success, {total_files_checked} files checked ---", "green"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_error("\n\nTest aborted due to KeyboardInterrupt!")
        sys.exit(1)
