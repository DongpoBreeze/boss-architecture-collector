"""Setuptools hooks for an explicitly allowlisted, offline-buildable release.

This build-only module is present in the source distribution, not in the wheel.
It never imports application modules or reads user/provider configuration.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy

from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

# Setuptools resolves a pyproject cmdclass by loading this file directly.  A
# PEP 517 frontend need not put the checkout on sys.path or import its parent
# package, so a relative import would fail during metadata preparation.
_SPEC = runpy.run_path(str(Path(__file__).with_name("__init__.py")))
DATA_DIRECTORY = _SPEC["DATA_DIRECTORY"]
MANIFEST_FILENAME = _SPEC["MANIFEST_FILENAME"]
MANIFEST_SCHEMA_VERSION = _SPEC["MANIFEST_SCHEMA_VERSION"]
PROJECT_NAME = _SPEC["PROJECT_NAME"]
RESOURCE_SOURCES = _SPEC["RESOURCE_SOURCES"]


def _source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resource_payloads() -> dict[str, bytes]:
    payloads = {}
    for name, relative in RESOURCE_SOURCES.items():
        path = _source_root() / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Release resource is missing or not a regular source file: {relative}")
        payload = path.read_bytes()
        try:
            text = payload.decode("utf-8")
            if not text.strip():
                raise ValueError("empty resource")
            if name.endswith(".json"):
                config = json.loads(text)
                required = {"schema_version", "degree", "cities", "salaries", "current", "target"}
                if not isinstance(config, dict) or not required.issubset(config):
                    raise ValueError("default collection configuration has an invalid schema")
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Release resource is invalid: {relative}: {exc}") from exc
        payloads[name] = payload
    return payloads


class BuildPy(build_py):
    """Keep flat modules and generate only the two release assets plus hashes."""

    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        if package == "boss_architecture_resources":
            # The setuptools hook is unnecessary at runtime (and would require
            # setuptools in an otherwise standard-library-only installation).
            modules = [module for module in modules if module[1] == "__init__"]
        return modules

    def run(self):
        payloads = _resource_payloads()
        for module in self.py_modules or ():
            path = _source_root() / (module.replace(".", "/") + ".py")
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"Release application module is missing or not a regular source file: {module}")
        super().run()
        if self.editable_mode:
            return
        destination = Path(self.build_lib) / "boss_architecture_resources" / DATA_DIRECTORY
        destination.mkdir(parents=True, exist_ok=True)
        for name, payload in payloads.items():
            (destination / name).write_bytes(payload)
            if (destination / name).read_bytes() != payload:
                raise RuntimeError(f"Release resource copy verification failed: {name}")
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "project": PROJECT_NAME,
            "version": self.distribution.get_version(),
            "resources": {
                name: hashlib.sha256(payload).hexdigest()
                for name, payload in sorted(payloads.items())
            },
        }
        (destination / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        expected = {
            *(module.replace(".", "/") + ".py" for module in self.py_modules or ()),
            "boss_architecture_resources/__init__.py",
            *(f"boss_architecture_resources/{DATA_DIRECTORY}/{name}"
              for name in (*RESOURCE_SOURCES, MANIFEST_FILENAME)),
        }
        unexpected = sorted(
            path.relative_to(self.build_lib).as_posix()
            for path in Path(self.build_lib).rglob("*")
            if path.is_file() and path.relative_to(self.build_lib).as_posix() not in expected
        )
        if unexpected:
            raise RuntimeError(
                "Unexpected files in the release build directory; use a clean build directory: "
                + ", ".join(unexpected)
            )

    def get_outputs(self, include_bytecode=True):
        outputs = super().get_outputs(include_bytecode)
        if self.editable_mode:
            return outputs
        destination = Path(self.build_lib) / "boss_architecture_resources" / DATA_DIRECTORY
        return outputs + [str(destination / name) for name in (*RESOURCE_SOURCES, MANIFEST_FILENAME)]


class Sdist(sdist):
    """A source archive must not inherit local plans or VCS-discovered secrets."""

    def make_distribution(self):
        _resource_payloads()
        required = {
            "pyproject.toml", "MANIFEST.in", "README.md", "LICENSE",
            "docs/development.md", "docs/desktop-review-workflow.md", "docs/command-line.md",
            "boss_architecture_resources/__init__.py",
            "boss_architecture_resources/_build.py",
            *RESOURCE_SOURCES.values(),
            *(module.replace(".", "/") + ".py" for module in self.distribution.py_modules or ()),
        }
        for relative in sorted(required):
            if not (_source_root() / relative).is_file():
                raise RuntimeError(f"Release source file is missing: {relative}")

        egg_info = Path(self.get_finalized_command("egg_info").egg_info)
        metadata_names = (
            "PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
            "requires.txt", "top_level.txt", "not-zip-safe", "zip-safe",
        )
        metadata = {
            (egg_info / name).as_posix() for name in metadata_names
            if (egg_info / name).is_file()
        }
        tests = {
            path.relative_to(_source_root()).as_posix()
            for path in (_source_root() / "tests").glob("test_*.py")
            if path.is_file() and not path.is_symlink()
        }
        self.filelist.files = sorted(required | metadata | tests)
        # Do not leak stale file names from an earlier egg_info/VCS discovery
        # into the otherwise allowlisted archive's SOURCES metadata.
        (egg_info / "SOURCES.txt").write_text("\n".join(self.filelist.files) + "\n", encoding="utf-8")
        super().make_distribution()
