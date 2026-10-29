"""Offline build/install regressions; all state and fake credentials stay temporary.

Run: python -m unittest discover -s tests -p test_packaging.py -v
The integration fixture uses the current interpreter's setuptools (>=70.1),
pip, and bundled ensurepip.  It never installs build tools or contacts an index.
"""

from __future__ import annotations

from email.parser import BytesParser
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock
import zipfile

try:
    import tomllib
except ImportError:  # Python 3.10's declared runtime dependency.
    import tomli as tomllib


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import boss_architecture_resources as resources
import project_resources


def project_config() -> dict:
    with (PROJECT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def isolated_environment(root: Path) -> dict[str, str]:
    # Do not let child applications discover real provider credentials, home
    # settings, pip config, cached login sessions, or project-specific overrides.
    environment = {
        name: value for name, value in os.environ.items()
        if not re.search(r"API_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD|SECRET", name, re.I)
        and not name.upper().startswith(("BOSS_ARCHITECTURE_", "CODEX_", "OPENAI_"))
    }
    environment.update({
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
        "LOCALAPPDATA": str(root / "local-app-data"),
        "APPDATA": str(root / "app-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "CODEX_HOME": str(root / "empty-codex-home"),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
    })
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return environment


class SourceResourceTests(unittest.TestCase):
    def test_explicit_module_list_matches_flat_application_modules(self):
        settings = project_config()["tool"]["setuptools"]
        self.assertEqual(settings["packages"], ["boss_architecture_resources"])
        self.assertFalse(settings["include-package-data"])
        self.assertIn("desktop_tk_lifecycle", settings["py-modules"])
        self.assertTrue({"delivery_export", "desktop_delivery"}.issubset(settings["py-modules"]))
        self.assertEqual(
            set(settings["py-modules"]),
            {path.stem for path in PROJECT.glob("*.py") if not path.stem.startswith("test_")},
        )

    def test_python_310_has_a_conditional_tomli_dependency_and_cli_entries(self):
        config = project_config()
        self.assertIn("tomli>=2; python_version < '3.11'", config["project"]["dependencies"])
        self.assertEqual(config["project"]["requires-python"], ">=3.10")
        self.assertEqual(config["project"]["scripts"], {
            "boss-architecture-collect": "collect_architecture:main",
            "boss-architecture-process": "process_architecture:main",
            "boss-architecture-desktop": "desktop_app:main",
            "boss-architecture-export": "delivery_export:main",
        })

    def test_source_resources_use_the_single_authoritative_files(self):
        self.assertTrue(project_resources.IS_SOURCE_CHECKOUT)
        self.assertEqual(project_resources.PROJECT_ROOT, PROJECT)
        self.assertEqual(project_resources.DEFAULT_CONFIG, PROJECT / resources.RESOURCE_SOURCES["architecture_collection.json"])
        self.assertEqual(project_resources.DEFAULT_PROMPT, PROJECT / resources.RESOURCE_SOURCES["jd_semantic_review.md"])
        self.assertEqual(
            project_resources.resource_path("delivery_jobs.v1.schema.json"),
            PROJECT / "schemas" / "delivery_jobs.v1.schema.json",
        )
        self.assertEqual(project_resources.default_state_root(), PROJECT)
        self.assertFalse((PROJECT / "boss_architecture_resources" / resources.DATA_DIRECTORY).exists())

    def test_source_resource_missing_and_unknown_names_fail_explicitly(self):
        with tempfile.TemporaryDirectory(prefix="boss-source-resource-") as temporary:
            with mock.patch.object(project_resources, "PROJECT_ROOT", Path(temporary)):
                with self.assertRaisesRegex(project_resources.ResourceError, "Source resource is missing"):
                    project_resources.resource_path("architecture_collection.json")
        with self.assertRaisesRegex(project_resources.ResourceError, "Unknown release resource"):
            project_resources.resource_path("../private.json")

    def test_installed_state_roots_are_platform_specific_and_never_created(self):
        with tempfile.TemporaryDirectory(prefix="boss-state-path-") as temporary:
            root = Path(temporary)
            with mock.patch.object(project_resources, "IS_SOURCE_CHECKOUT", False), mock.patch.object(
                project_resources.Path, "home", return_value=root / "home"
            ):
                for platform, environment, expected in (
                    ("win32", {"LOCALAPPDATA": str(root / "local")}, root / "local" / resources.PROJECT_NAME),
                    ("win32", {}, root / "home" / ".boss-architecture-collector"),
                    ("win32", {"LOCALAPPDATA": "relative-dir"}, root / "home" / ".boss-architecture-collector"),
                    ("linux", {"XDG_STATE_HOME": str(root / "state")}, root / "state" / resources.PROJECT_NAME),
                    ("linux", {}, root / "home" / ".local" / "state" / resources.PROJECT_NAME),
                    ("linux", {"XDG_STATE_HOME": "relative-dir"}, root / "home" / ".local" / "state" / resources.PROJECT_NAME),
                ):
                    with self.subTest(platform=platform, environment=environment), mock.patch.object(
                        project_resources.sys, "platform", platform
                    ), mock.patch.dict(project_resources.os.environ, environment, clear=True):
                        self.assertEqual(project_resources.default_state_root(), expected)
                        self.assertFalse(expected.exists())
            self.assertEqual(list(root.iterdir()), [])


class PackagingArtifactTests(unittest.TestCase):
    """Build real wheel/sdist artifacts and install the wheel in a fresh venv."""

    @classmethod
    def setUpClass(cls):
        try:
            installed_setuptools = metadata.version("setuptools")
            metadata.version("pip")
        except metadata.PackageNotFoundError:
            raise unittest.SkipTest("offline packaging tests require existing setuptools>=70.1 and pip")
        parsed = tuple(int(part) for part in re.findall(r"\d+", installed_setuptools)[:2])
        if parsed < (70, 1):
            raise unittest.SkipTest("offline packaging tests require an existing setuptools>=70.1")

        cls.temporary = tempfile.TemporaryDirectory(prefix="boss-packaging-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.environment = isolated_environment(cls.root)
        cls.config = project_config()
        cls.stage = cls.root / "source"
        cls._copy_source(cls.stage)
        cls.private_files = (
            "config/collection-plans/private-plan.json",
            "config/review-source-catalogs/private-source.json",
            "config/provider-credentials.json",
            "prompts/private-prompt.md",
            "job-result/private-jobs.json",
            "chrome-profile/Cookies",
            "edge-profile/Local State",
            "private-provider.log",
            ".env",
            "boss_architecture_resources/unpublished.json",
            "schemas/private-schema.json",
            "delivery-exports/private-export/delivery_jobs.json",
            "nested/delivery-exports/private-export/delivery_jobs.json",
        )
        for relative in cls.private_files:
            path = cls.stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("temporary-fixture-private-data", encoding="utf-8")

        cls.artifacts = cls.root / "artifacts"
        cls.artifacts.mkdir()
        cls._build("build_wheel", cls.stage, cls.artifacts)
        cls._build("build_sdist", cls.stage, cls.artifacts)
        cls.wheel = next(cls.artifacts.glob("*.whl"))
        cls.sdist = next(cls.artifacts.glob("*.tar.gz"))

        cls.extracted = cls.root / "extracted"
        cls.extracted.mkdir()
        with tarfile.open(cls.sdist, "r:gz") as archive:
            # Extract only our just-built regular files, without invoking the
            # pre-3.12 tarfile API's unsafe link/path handling.
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or not (member.isdir() or member.isfile()):
                    raise AssertionError(f"Unsafe sdist member: {member.name}")
                destination = cls.extracted.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
        cls.roundtrip_artifacts = cls.root / "roundtrip-artifacts"
        cls.roundtrip_artifacts.mkdir()
        cls._build("build_wheel", next(cls.extracted.iterdir()), cls.roundtrip_artifacts)
        cls.roundtrip_wheel = next(cls.roundtrip_artifacts.glob("*.whl"))

        cls.outside = cls.root / "outside-source"
        cls.outside.mkdir()
        cls.venv_root = cls.root / "venv"
        # Ensurepip is bundled with CPython and does not access a package index.
        cls._run([sys.executable, "-I", "-m", "venv", str(cls.venv_root)], cls.outside)
        bin_directory = cls.venv_root / ("Scripts" if os.name == "nt" else "bin")
        cls.python = bin_directory / ("python.exe" if os.name == "nt" else "python")
        cls.bin_directory = bin_directory
        cls._run([
            str(cls.python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
            "install", "--no-index", "--no-deps", "--no-cache-dir", str(cls.wheel),
        ], cls.outside)
        purelib = cls._run([
            str(cls.python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))",
        ], cls.outside)
        # A venv-local audit hook also applies to the real installed console
        # launchers.  Unexpected provider/network activity fails before a socket
        # can connect; no test needs credentials or a running local service.
        (Path(purelib.stdout.strip()) / "sitecustomize.py").write_text(
            "import sys\n"
            "def _deny_test_network(event, arguments):\n"
            "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
            "        raise RuntimeError('Network is disabled in offline packaging tests')\n"
            "sys.addaudithook(_deny_test_network)\n",
            encoding="utf-8",
        )
        probe = cls._run([
            str(cls.python), "-I", "-c",
            "import json, project_resources as r; print(json.dumps({'root': str(r.PROJECT_ROOT), 'source': r.IS_SOURCE_CHECKOUT}))",
        ], cls.outside)
        cls.installed = json.loads(probe.stdout)

    @classmethod
    def _copy_source(cls, destination):
        destination.mkdir(parents=True)
        files = {
            "pyproject.toml", "MANIFEST.in", "README.md", "LICENSE",
            "docs/development.md", "docs/desktop-review-workflow.md", "docs/command-line.md",
            "boss_architecture_resources/__init__.py", "boss_architecture_resources/_build.py",
            "tests/test_packaging.py", *resources.RESOURCE_SOURCES.values(),
            *(name + ".py" for name in cls.config["tool"]["setuptools"]["py-modules"]),
        }
        for relative in sorted(files):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT / relative, target)

    @classmethod
    def _run(cls, command, cwd, *, success=True):
        result = subprocess.run(
            command, cwd=cwd, env=cls.environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        if success and result.returncode:
            raise AssertionError(f"Command failed ({result.returncode}): {command!r}\n{result.stdout[-16000:]}")
        return result

    @classmethod
    def _build(cls, operation, source, destination, *, success=True):
        if operation == "build_wheel":
            return cls._run([
                sys.executable, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                "wheel", "--no-index", "--no-build-isolation", "--no-deps", "--no-cache-dir",
                "--wheel-dir", str(destination), ".",
            ], source, success=success)
        code = (
            "import sys; sys.path.insert(0, '.'); "
            f"from setuptools.build_meta import {operation}; "
            f"print({operation}(sys.argv[1]))"
        )
        return cls._run([sys.executable, "-I", "-c", code, str(destination)], source, success=success)

    @classmethod
    def _console(cls, name, *arguments):
        executable = cls.bin_directory / (name + (".exe" if os.name == "nt" else ""))
        return cls._run([str(executable), *arguments], cls.outside)

    def test_wheel_contains_exact_modules_and_validated_read_only_resources(self):
        with zipfile.ZipFile(self.wheel) as archive:
            actual = {name for name in archive.namelist() if ".dist-info/" not in name}
            prefix = "boss_architecture_resources/data/"
            expected = {
                *(name + ".py" for name in self.config["tool"]["setuptools"]["py-modules"]),
                "boss_architecture_resources/__init__.py",
                *(prefix + name for name in (*resources.RESOURCE_SOURCES, resources.MANIFEST_FILENAME)),
            }
            self.assertEqual(actual, expected)
            manifest = json.loads(archive.read(prefix + resources.MANIFEST_FILENAME))
            self.assertEqual(manifest["version"], self.config["project"]["version"])
            self.assertEqual(set(manifest["resources"]), set(resources.RESOURCE_SOURCES))
            for name, relative in resources.RESOURCE_SOURCES.items():
                payload = archive.read(prefix + name)
                self.assertEqual(payload, (PROJECT / relative).read_bytes())
                self.assertEqual(hashlib.sha256(payload).hexdigest(), manifest["resources"][name])
            wheel_metadata = BytesParser().parsebytes(archive.read(next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )))
            self.assertEqual(wheel_metadata["Requires-Python"], ">=3.10")
            self.assertTrue(any("tomli" in value and 'python_version < "3.11"' in value
                                for value in wheel_metadata.get_all("Requires-Dist", [])))

    def test_sdist_excludes_private_inputs_and_roundtrips_version_and_resources(self):
        with tarfile.open(self.sdist, "r:gz") as archive:
            paths = {PurePosixPath(member.name).relative_to(PurePosixPath(member.name).parts[0]).as_posix()
                     for member in archive.getmembers() if member.isfile()}
            self.assertEqual({name for name in paths if name.startswith("config/")},
                             {"config/architecture_collection.json"})
            self.assertEqual({name for name in paths if name.startswith("prompts/")},
                             {"prompts/jd_semantic_review.md"})
            self.assertEqual({name for name in paths if name.startswith("schemas/")},
                             {"schemas/delivery_jobs.v1.schema.json"})
            self.assertTrue(set(self.private_files).isdisjoint(paths))
            self.assertFalse(any(
                name.startswith(("job-result/", "chrome-profile/", "edge-profile/"))
                or "delivery-exports" in PurePosixPath(name).parts for name in paths
            ))
            self.assertNotIn("boss_architecture_resources/data/manifest.json", paths)
            self.assertEqual({name for name in paths if name.startswith("docs/")}, {
                "docs/development.md", "docs/desktop-review-workflow.md", "docs/command-line.md",
            })
            for module in self.config["tool"]["setuptools"]["py-modules"]:
                self.assertIn(module + ".py", paths)
        with zipfile.ZipFile(self.wheel) as original, zipfile.ZipFile(self.roundtrip_wheel) as rebuilt:
            for name in (*resources.RESOURCE_SOURCES, resources.MANIFEST_FILENAME):
                path = "boss_architecture_resources/data/" + name
                self.assertEqual(original.read(path), rebuilt.read(path))
            for archive in (original, rebuilt):
                info = BytesParser().parsebytes(archive.read(next(
                    name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
                )))
                self.assertEqual(info["Version"], self.config["project"]["version"])

    def test_installed_collect_dry_run_has_672_and_beijing_112_combinations(self):
        full = self._console("boss-architecture-collect", "--dry-run")
        self.assertIn("组合数: 672", full.stdout)
        self.assertEqual(len(re.findall(r"^\d{3}/672 ", full.stdout, re.M)), 672)
        beijing = self._console("boss-architecture-collect", "--dry-run", "--cities", "北京")
        self.assertIn("组合数: 112", beijing.stdout)
        self.assertEqual(len(re.findall(r"^\d{3}/112 ", beijing.stdout, re.M)), 112)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_installed_process_desktop_and_export_help_do_not_open_gui_or_network(self):
        for entry in ("boss-architecture-process", "boss-architecture-desktop", "boss-architecture-export"):
            with self.subTest(entry=entry):
                result = self._console(entry, "--help")
                self.assertIn("usage:", result.stdout)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_installed_collection_page_commands_expand_disjoint_bounded_shards(self):
        code = textwrap.dedent("""\
            import json
            from pathlib import Path
            import collect_architecture as collector
            from desktop_collection import CollectionRequest, CollectionSelection, make_collect_command
            from project_resources import DEFAULT_CONFIG
            request = CollectionRequest("unused", "", DEFAULT_CONFIG, CollectionSelection(city="北京", limit=5),
                                        page_wait_min=1.25, page_wait_max=2.75, browser_workers=3)
            commands = [make_collect_command(request, Path("not-created"), Path("missing-scraper"),
                                            browser=browser, shard=browser_index + 2 * worker, total_shards=6)
                        for worker in range(3) for browser_index, browser in enumerate(("chrome", "edge"))]
            expected = [spec["combination"] for spec in collector.build_specs(
                collector.load_config(DEFAULT_CONFIG), cities=["北京"], limit=5)]
            print(json.dumps({"commands": commands, "expected": expected}))
        """)
        data = json.loads(self._run([str(self.python), "-I", "-c", code], self.outside).stdout)
        targets = []
        profiles = []
        for index, command in enumerate(data["commands"]):
            with self.subTest(browser=("chrome", "edge")[index % 2], worker=index // 2):
                self.assertEqual(command[command.index("--cdp-port") + 1], str(9222 + index % 2))
                self.assertEqual(command[command.index("--page-wait-min") + 1], "1.25")
                self.assertEqual(command[command.index("--page-wait-max") + 1], "2.75")
                profiles.append(command[command.index("--profile-dir") + 1])
                result = self._run([*command, "--dry-run"], self.outside)
                targets.append(re.findall(r"^\d{3}/\d+ (\S+) \|", result.stdout, re.M))
                self.assertEqual(targets[index], data["expected"][index::6])
        self.assertEqual([len(items) for items in targets], [1, 1, 1, 1, 1, 0])
        flattened = [target for group in targets for target in group]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(set(flattened), set(data["expected"]))
        self.assertEqual(len(set(profiles[0::2])), 1)
        self.assertEqual(len(set(profiles[1::2])), 1)
        self.assertNotEqual(profiles[0], profiles[1])
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_installed_runtime_changes_only_pagination_waits_in_synthetic_upstream(self):
        with tempfile.TemporaryDirectory(dir=self.root, prefix="installed-list-runtime-") as temporary:
            base = Path(temporary)
            scraper = base / "中文 upstream"
            script = scraper / "scripts" / "boss_cdp_raw.py"
            script.parent.mkdir(parents=True)
            source = textwrap.dedent('''\
                import argparse
                import json
                from pathlib import Path
                from types import SimpleNamespace

                waits = []
                time = SimpleNamespace(sleep=waits.append)
                random = SimpleNamespace(uniform=lambda lower, upper: (lower + upper) / 2)

                def scrape_list(max_pages):
                    for pg in range(1, max_pages + 1):
                        time.sleep(3)
                        if pg < max_pages:
                            d = random.uniform(12, 22)
                            print(f"  翻页等待 {d:.0f}s...\\n")
                            time.sleep(d)

                def main():
                    parser = argparse.ArgumentParser()
                    parser.add_argument("--output", required=True)
                    parser.add_argument("--merge", required=True)
                    arguments = parser.parse_args()
                    scrape_list(3)
                    Path(arguments.output).write_text(json.dumps({"waits": waits, "merge": arguments.merge}), encoding="utf-8")
            ''')
            script.write_text(source, encoding="utf-8")
            output = base / "result.json"
            prior = str(base / "历史 检查点.json")
            runtime = self._run([str(self.python), "-I", "-c", "import collection_runtime; print(collection_runtime.__file__)"], self.outside)
            self._run([
                str(self.python), "-I", runtime.stdout.strip(),
                "--scraper-root", str(scraper), "--page-wait-min", "1.25", "--page-wait-max", "2.75",
                "--", "--output", str(output), "--merge", prior,
            ], self.outside)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {
                "waits": [3, 2.0, 3, 2.0, 3], "merge": prior,
            })
            self.assertEqual(script.read_text(encoding="utf-8"), source)
            self.assertEqual(list(script.parent.iterdir()), [script])
            self.assertEqual(list(self.outside.iterdir()), [])

    def test_installed_export_reads_schema_and_publishes_unique_confirmed_snapshots(self):
        with tempfile.TemporaryDirectory(dir=self.root, prefix="installed-export-") as temporary:
            fixture = Path(temporary)
            # Build the synthetic task through installed public storage APIs;
            # no processor, provider, browser or existing job data is involved.
            code = textwrap.dedent("""\
                import hashlib, json, sys
                from pathlib import Path
                from desktop_tasks import ReviewTask
                from process_architecture import model_input
                from project_resources import resource_path
                from review_cache import review_input_signature, review_signature
                from review_rules import compose_review_prompt

                root = Path(sys.argv[1])
                source = root / 'synthetic-source'
                (source / 'raw/current').mkdir(parents=True)
                (source / 'raw/target').mkdir(parents=True)
                key = 'c2f4a6b8d0e1133557799aabbccddeeff'
                job = {
                    'job_id': key, 'encrypt_job_id': key,
                    'job_link': f'https://www.zhipin.com/job_detail/{key}.html',
                    'title': 'Offline packaging fixture', 'company': 'Synthetic company',
                    'location': 'Shanghai', 'salary': '20-30K',
                    'api_key': 'temporary-fixture-private-data',
                    'raw_model_response': 'temporary-fixture-private-data',
                    'source_file': str(root / 'private-source.json'),
                }
                (source / 'raw/current/jobs.json').write_text(
                    json.dumps({'jobs': [job]}), encoding='utf-8')
                task = ReviewTask.create(source, root / 'tasks', 'Offline packaging fixture')
                task.begin('import')
                task.finish('import', [job])
                task.confirm('import')
                for stage in ('shortlist', 'fetch-jd', 'llm'):
                    jobs = task.input_jobs(stage)
                    task.begin(stage)
                    stats = {}
                    for item in jobs:
                        if stage == 'shortlist':
                            item['list_pass'] = True
                        elif stage == 'fetch-jd':
                            item.update(jd='岗位职责：分析业务需求，设计方案并实施交付。任职要求：具备系统集成经验。',
                                        jd_completeness='complete', recommended_keep=True)
                        else:
                            prompt = compose_review_prompt(task.get_review_rules())
                            digest = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
                            prompt_path = task.run_directory('llm') / 'review_prompts' / (digest + '.md')
                            prompt_path.parent.mkdir(parents=True, exist_ok=True)
                            prompt_path.write_text(prompt, encoding='utf-8')
                            stats['review_prompt_file'] = prompt_path.relative_to(task.path).as_posix()
                            payload = model_input(item)
                            item['llm_review'] = {
                                'job_id': key, 'status': 'success', 'review_format': 'simple-v1',
                                '判断': '符合', '理由': 'Synthetic offline review.',
                                'model': 'offline-fixture',
                                'cache_input_signature': review_input_signature(payload, prompt),
                                'cache_signature': review_signature(payload, prompt, 'offline-fixture-provider'),
                                'raw_response': 'temporary-fixture-private-data',
                            }
                    task.finish(stage, jobs, stats=stats)
                    task.confirm(stage)
                schema = resource_path('delivery_jobs.v1.schema.json')
                print(json.dumps({'task': str(task.path), 'schema': str(schema)}, ensure_ascii=False))
                """)
            result = self._run([str(self.python), "-I", "-c", code, str(fixture)], self.outside)
            paths = json.loads(result.stdout)
            task = Path(paths["task"])
            schema = Path(paths["schema"])
            self.assertTrue(schema.is_relative_to(Path(self.installed["root"]) / "boss_architecture_resources"))
            self.assertEqual(json.loads(schema.read_text(encoding="utf-8"))["properties"]["schema_version"]["const"], "1.0")
            manifest_before = (task / "task_manifest.json").read_bytes()
            self._console("boss-architecture-export", "--task", str(task))
            default_exports = list((task / "delivery-exports").glob("*/delivery_jobs.json"))
            self.assertEqual(len(default_exports), 1)
            original = default_exports[0].read_bytes()
            output_parent = fixture / "custom-exports"
            for _ in range(2):
                self._console("boss-architecture-export", "--task", str(task), "--output-dir", str(output_parent))
            custom_exports = list(output_parent.glob("*/delivery_jobs.json"))
            self.assertEqual(len(custom_exports), 2)
            export_ids = set()
            for path in (*default_exports, *custom_exports):
                payload = path.read_text(encoding="utf-8")
                data = json.loads(payload)
                self.assertEqual(data["schema_version"], "1.0")
                self.assertEqual(data["summary"], {"selected_count": 1, "ready_count": 1, "blocked_count": 0})
                self.assertEqual(len(data["jobs"]), 1)
                self.assertEqual(data["blocked_jobs"], [])
                self.assertNotIn("temporary-fixture-private-data", payload)
                self.assertNotIn(str(fixture), payload)
                export_ids.add(data["export_id"])
            self.assertEqual(len(export_ids), 3)
            self.assertEqual(default_exports[0].read_bytes(), original)
            self.assertEqual((task / "task_manifest.json").read_bytes(), manifest_before)
            self.assertFalse((task / "runtime/report").exists())
            self.assertEqual(list(self.outside.iterdir()), [])

    def test_fixture_blocks_network_before_any_connection(self):
        result = self._run([
            str(self.python), "-I", "-c",
            "import socket; socket.create_connection(('127.0.0.1', 9))",
        ], self.outside, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Network is disabled in offline packaging tests", result.stdout)

    def test_installed_defaults_are_outside_site_packages_and_have_no_state_side_effects(self):
        result = self._run([
            str(self.python), "-I", "-c",
            "import sys; sys.modules['setuptools'] = None; "
            "import json, collect_architecture as c, project_resources as r; "
            "print(json.dumps({'source': r.IS_SOURCE_CHECKOUT, 'state': str(r.default_state_root()), "
            "'output': str(c.default_root()), 'config': str(c.DEFAULT_CONFIG)}))",
        ], self.outside)
        values = json.loads(result.stdout)
        self.assertFalse(values["source"])
        self.assertFalse(self.installed["source"])
        site_packages = Path(self.installed["root"])
        state = Path(values["state"])
        self.assertNotIn(site_packages, state.parents)
        self.assertIn(state, Path(values["output"]).parents)
        self.assertTrue(Path(values["config"]).is_relative_to(site_packages / "boss_architecture_resources"))
        self.assertFalse(state.exists())
        self.assertFalse((site_packages / "job-result").exists())
        self.assertFalse((site_packages / "config").exists())

    def test_missing_tampered_or_mismatched_installed_resources_fail_without_fallback(self):
        for failure in ("missing-config", "tampered-prompt", "missing-schema", "tampered-schema",
                        "missing-manifest", "invalid-manifest", "wrong-version"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=self.root, prefix="resource-probe-") as temporary:
                probe = Path(temporary)
                with zipfile.ZipFile(self.wheel) as archive:
                    for name in archive.namelist():
                        if name == "project_resources.py" or name.startswith("boss_architecture_resources/"):
                            path = probe / name
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(archive.read(name))
                data = probe / "boss_architecture_resources" / "data"
                manifest_path = data / "manifest.json"
                if failure == "missing-config":
                    (data / "architecture_collection.json").unlink()
                    expected = "Packaged resource is missing"
                elif failure == "tampered-prompt":
                    (data / "jd_semantic_review.md").write_text("tampered", encoding="utf-8")
                    expected = "checksum mismatch"
                elif failure == "missing-schema":
                    (data / "delivery_jobs.v1.schema.json").unlink()
                    expected = "Packaged resource is missing"
                elif failure == "tampered-schema":
                    (data / "delivery_jobs.v1.schema.json").write_text("{}", encoding="utf-8")
                    expected = "checksum mismatch"
                elif failure == "missing-manifest":
                    manifest_path.unlink()
                    expected = "manifest is missing or invalid"
                elif failure == "invalid-manifest":
                    manifest_path.write_text("{}", encoding="utf-8")
                    expected = "manifest has an invalid schema"
                else:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["version"] = "999.0.0"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    expected = "resource version mismatch"
                # A plausible config in cwd must never hide a broken installation.
                cwd = probe / "unrelated-working-directory"
                (cwd / "config").mkdir(parents=True)
                shutil.copy2(PROJECT / "config" / "architecture_collection.json", cwd / "config" / "architecture_collection.json")
                result = self._run([
                    str(self.python), "-I", "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import project_resources as r; "
                    "r.resource_path('delivery_jobs.v1.schema.json')",
                    str(probe),
                ], cwd, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ResourceError", result.stdout)
                self.assertIn(expected, result.stdout)

    def test_missing_or_invalid_authoritative_source_fails_the_build(self):
        for relative in ("config/architecture_collection.json", "schemas/delivery_jobs.v1.schema.json"):
            for failure in ("missing", "invalid"):
                with self.subTest(resource=relative, failure=failure), tempfile.TemporaryDirectory(
                    dir=self.root, prefix="bad-source-"
                ) as temporary:
                    root = Path(temporary)
                    source = root / "source"
                    self._copy_source(source)
                    resource = source / relative
                    if failure == "missing":
                        resource.unlink()
                        expected = "Release resource is missing"
                    else:
                        resource.write_text("{}", encoding="utf-8")
                        expected = "Release resource is invalid"
                    artifacts = root / "artifacts"
                    artifacts.mkdir()
                    result = self._build("build_wheel", source, artifacts, success=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stdout)
                    self.assertEqual(list(artifacts.iterdir()), [])

    def test_stale_private_build_files_fail_closed_instead_of_entering_the_wheel(self):
        with tempfile.TemporaryDirectory(dir=self.root, prefix="stale-build-") as temporary:
            root = Path(temporary)
            source = root / "source"
            self._copy_source(source)
            stale = source / "build" / "lib" / "private-provider.log"
            stale.parent.mkdir(parents=True)
            stale.write_text("temporary-private-log", encoding="utf-8")
            artifacts = root / "artifacts"
            artifacts.mkdir()
            result = self._build("build_wheel", source, artifacts, success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unexpected files in the release build directory", result.stdout)
            self.assertEqual(list(artifacts.iterdir()), [])

    def test_missing_application_module_cannot_produce_an_incomplete_wheel(self):
        with tempfile.TemporaryDirectory(dir=self.root, prefix="missing-module-") as temporary:
            root = Path(temporary)
            source = root / "source"
            self._copy_source(source)
            (source / "collection_sources.py").unlink()
            artifacts = root / "artifacts"
            artifacts.mkdir()
            result = self._build("build_wheel", source, artifacts, success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Release application module is missing", result.stdout)
            self.assertEqual(list(artifacts.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
