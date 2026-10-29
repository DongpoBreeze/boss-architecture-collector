import ast
from contextlib import redirect_stderr, redirect_stdout
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import collection_runtime as runtime  # noqa: E402


# All time/random objects are local fakes.  This fixture cannot contact a CDP
# endpoint or sleep, and exercises the same guarded pagination statement as the
# supported upstream while retaining distinct initial/scroll waits.
UPSTREAM_SOURCE = textwrap.dedent(r'''
    import argparse
    from dataclasses import dataclass
    import json
    from pathlib import Path
    import sys
    from types import SimpleNamespace

    events = []

    class FakeRandom:
        use_high = False

        def __init__(self):
            self.calls = []

        def uniform(self, minimum, maximum):
            self.calls.append((minimum, maximum))
            return maximum if self.use_high else minimum

    random = FakeRandom()
    time = SimpleNamespace(sleep=events.append)

    @dataclass
    class Example:
        value: str

    def scrape_list(keyword, city_input, max_pages, filters, output_path,
                    cdp_port=9222, fmt="json", allow_dom_fallback=False,
                    diff_base=None):
        time.sleep(random.uniform(2, 4))
        for pg in range(1, max_pages + 1):
            time.sleep(random.uniform(0.5, 1.5))
            if pg < max_pages:
                d = random.uniform(12, 22)
                print(f"  翻页等待 {d:.0f}s...\n")
                time.sleep(d)
        return events

    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--keyword")
        parser.add_argument("--city")
        parser.add_argument("--pages", type=int, default=2)
        parser.add_argument("--output", required=True)
        parser.add_argument("--merge")
        args, remaining = parser.parse_known_args()
        scrape_list(args.keyword, args.city, args.pages, {}, args.output)
        previous = json.loads(Path(args.merge).read_text(encoding="utf-8")) if args.merge else {"jobs": []}
        Path(args.output).write_text(json.dumps({
            "jobs": [*previous["jobs"], {"job_id": "fixture"}],
            "argv": sys.argv[1:], "events": events, "random_calls": random.calls,
        }, ensure_ascii=False), encoding="utf-8")
        return 0

    if __name__ == "__main__":
        raise AssertionError("the loader must delegate main explicitly")
''')


class CollectionRuntimeTests(unittest.TestCase):
    def write_upstream(self, root, source=UPSTREAM_SOURCE):
        script = root / "scripts" / "boss_cdp_raw.py"
        script.parent.mkdir(parents=True)
        script.write_text(source, encoding="utf-8")
        return script

    def run_with_legacy_stdio(self, encoding, arguments, cwd):
        # -I ignores PYTHONUTF8/PYTHONIOENCODING.  Force the actual pipe
        # encodings as well, so this exercises English Windows/ASCII behavior
        # even on a development machine whose locale supports Chinese.
        bootstrap = (
            "import runpy, sys; "
            "sys.stdout.reconfigure(encoding=sys.argv.pop(1), errors='strict'); "
            "sys.stderr.reconfigure(encoding=sys.stdout.encoding, errors='strict'); "
            "print('INITIAL=' + sys.stdout.encoding + ';UTF8_MODE=' + str(sys.flags.utf8_mode), flush=True); "
            "runpy.run_path(sys.argv.pop(1), run_name='__main__')"
        )
        environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="ascii:strict")
        return subprocess.run([
            sys.executable, "-I", "-X", "utf8=0", "-c", bootstrap, encoding,
            str(Path(runtime.__file__).resolve()), *arguments,
        ], cwd=cwd, env=environment, capture_output=True, timeout=10)

    def test_isolated_cli_emits_strict_utf8_from_cp1252_and_ascii_before_loading_upstream(self):
        for encoding in ("cp1252", "ascii"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UPSTREAM_SOURCE.replace(
                    "import sys\n", 'import sys\nprint("上游初始化：中文 ✓")\nprint("上游诊断：中文 ✓", file=sys.stderr)\n'
                )
                script = self.write_upstream(root, source)
                output = root / "out.json"
                result = self.run_with_legacy_stdio(encoding, [
                    "--scraper-root", str(root), "--page-wait-min", "0.25", "--page-wait-max", "0.25",
                    "--", "--output", str(output),
                ], root)
                stdout, stderr = result.stdout.decode("utf-8", errors="strict"), result.stderr.decode("utf-8", errors="strict")
                self.assertEqual(result.returncode, 0, stdout + stderr)
                self.assertIn(f"INITIAL={encoding};UTF8_MODE=0", stdout)
                self.assertIn("上游初始化：中文 ✓", stdout)
                self.assertIn("上游诊断：中文 ✓", stderr)
                self.assertIn("翻页等待", stdout)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["events"], [2, 0.5, 0.25, 0.5])
                self.assertEqual(script.read_text(encoding="utf-8"), source)

    def test_isolated_help_and_argument_errors_use_utf8_before_upstream_is_loaded(self):
        for arguments, exit_code, diagnostic in (
            (["--help"], 0, "为上游列表采集配置翻页等待"),
            (["--scraper-root", "unused", "--page-wait-min", "nan", "--", "--help"], 2, "翻页等待时间必须为有限数字"),
        ):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                result = self.run_with_legacy_stdio("ascii", arguments, Path(directory))
                stdout, stderr = result.stdout.decode("utf-8", errors="strict"), result.stderr.decode("utf-8", errors="strict")
                self.assertEqual(result.returncode, exit_code, stdout + stderr)
                self.assertIn("INITIAL=ascii;UTF8_MODE=0", stdout)
                self.assertIn(diagnostic, stdout + stderr)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_actual_sleep_bounds_change_and_initial_scroll_waits_are_preserved(self):
        for minimum, maximum in ((0, 0), (1.25, 7.5), (300, 300)):
            for use_high in (False, True):
                with self.subTest(minimum=minimum, maximum=maximum, use_high=use_high), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.write_upstream(root)
                    module = runtime.load_upstream(root, minimum, maximum)
                    module.random.use_high = use_high
                    with redirect_stdout(io.StringIO()):
                        sleeps = module.scrape_list("架构", "上海", 3, {}, "unused")
                    pagination = maximum if use_high else minimum
                    initial, scroll = (4, 1.5) if use_high else (2, 0.5)
                    self.assertEqual(sleeps, [initial, scroll, pagination, scroll, pagination, scroll])
                    self.assertEqual(module.random.calls, [
                        (2, 4), (0.5, 1.5), (minimum, maximum),
                        (0.5, 1.5), (minimum, maximum), (0.5, 1.5),
                    ])

    def test_default_bounds_use_the_same_verified_adaptation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_upstream(root)
            module = runtime.load_upstream(root)
            with redirect_stdout(io.StringIO()):
                module.scrape_list("架构", "上海", 2, {}, "unused")
            self.assertEqual(module.events, [2, 0.5, 12.0, 0.5])
            self.assertEqual(module.random.calls[2], (12.0, 22.0))

    def test_single_page_does_not_add_any_pagination_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_upstream(root)
            module = runtime.load_upstream(root, 250, 300)
            module.scrape_list("架构", "上海", 1, {}, "unused")
            self.assertEqual(module.events, [2, 0.5])

    def test_only_two_pagination_constants_change_and_signature_stays_compatible(self):
        original = ast.parse(UPSTREAM_SOURCE)
        adapted = runtime.adapt_page_wait(ast.parse(UPSTREAM_SOURCE), 1.25, 3.75)
        original_nodes, adapted_nodes = list(ast.walk(original)), list(ast.walk(adapted))
        self.assertEqual(len(original_nodes), len(adapted_nodes))
        changes = [(left.value, right.value)
                   for left, right in zip(original_nodes, adapted_nodes)
                   if isinstance(left, ast.Constant) and left.value != right.value]
        self.assertEqual(changes, [(12, 1.25), (22, 3.75)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_upstream(root)
            module = runtime.load_upstream(root)
            self.assertEqual(str(inspect.signature(module.scrape_list)),
                             "(keyword, city_input, max_pages, filters, output_path, cdp_port=9222, fmt='json', allow_dom_fallback=False, diff_base=None)")

    def test_unknown_pagination_shapes_fail_before_module_initialization(self):
        changes = (
            ("def scrape_list(", "def renamed_scrape_list("),
            ("range(1, max_pages + 1)", "range(2, max_pages + 1)"),
            ("pg < max_pages", "pg <= max_pages"),
            ("random.uniform(12, 22)", "random.uniform(11, 22)"),
            ("翻页等待", "其它等待"),
            ("time.sleep(d)", "time.sleep(d + 1)"),
            ("time.sleep(d)", "time.sleep(d)\n            events.append(999)"),
        )
        for old, new in changes:
            with self.subTest(change=new), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                initialized = root / "initialized"
                source = "from pathlib import Path\nPath(" + repr(str(initialized)) + ").touch()\n" + UPSTREAM_SOURCE.replace(old, new)
                self.write_upstream(root, source)
                with self.assertRaises(runtime.RuntimeCompatibilityError):
                    runtime.load_upstream(root, 1, 2)
                self.assertFalse(initialized.exists())

    def test_ambiguous_pagination_guards_are_rejected(self):
        block = textwrap.dedent('''\
            if pg < max_pages:
                d = random.uniform(12, 22)
                print(f"  翻页等待 {d:.0f}s...\\n")
                time.sleep(d)
        ''')
        block = textwrap.indent(block, "        ")
        self.assertIn(block, UPSTREAM_SOURCE)
        with self.assertRaises(runtime.RuntimeCompatibilityError):
            runtime.adapt_page_wait(ast.parse(UPSTREAM_SOURCE.replace(block, block + block)), 1, 2)

    def test_loading_does_not_write_source_or_upstream_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = self.write_upstream(root)
            original = script.read_bytes()
            original_path, original_bytecode = list(sys.path), sys.dont_write_bytecode
            module = runtime.load_upstream(root, 0, 0)
            self.assertEqual(module.Example("works").value, "works")
            self.assertEqual(script.read_bytes(), original)
            self.assertEqual(list(root.rglob("*")), [script.parent, script])
            self.assertEqual(sys.path, original_path)
            self.assertEqual(sys.dont_write_bytecode, original_bytecode)

    def test_runtime_cli_forwards_all_original_arguments_and_merge_with_unicode_space_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "中文 空格"
            scraper = root / "上游 目录"
            self.write_upstream(scraper)
            copied_runtime = root / "执行 目录" / "collection_runtime.py"
            copied_runtime.parent.mkdir(parents=True)
            shutil.copyfile(runtime.__file__, copied_runtime)
            output, checkpoint = root / "结果 文件.json", root / "已有 结果.json"
            checkpoint.write_text(json.dumps({"jobs": [{"job_id": "checkpoint"}]}), encoding="utf-8")
            upstream_args = [
                "--keyword", "AI 架构", "--city", "北京", "--pages", "2",
                "--salary", "406", "--experience", "106", "--degree", "203",
                "--cdp-port", "9333", "--no-detail", "--output", str(output),
                "--merge", str(checkpoint), "--diff-base", "基准 文件.json",
                "--allow-dom-fallback", "--format", "json",
            ]
            result = subprocess.run([
                sys.executable, "-u", str(copied_runtime), "--scraper-root", str(scraper),
                "--page-wait-min", "0.125", "--page-wait-max", "0.625", "--", *upstream_args,
            ], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["argv"], upstream_args)
            self.assertEqual(payload["events"], [2, 0.5, 0.125, 0.5])
            self.assertEqual(payload["jobs"], [{"job_id": "checkpoint"}, {"job_id": "fixture"}])
            self.assertFalse((scraper / "scripts" / "__pycache__").exists())

    def test_main_restores_process_arguments_when_delegating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_upstream(root)
            original_argv = sys.argv
            original_path = list(sys.path)
            output = root / "out.json"
            with redirect_stdout(io.StringIO()):
                result = runtime.main(["--scraper-root", str(root), "--page-wait-min", "4.5",
                                       "--page-wait-max", "4.5", "--", "--output", str(output)])
            self.assertEqual(result, 0)
            self.assertIs(sys.argv, original_argv)
            self.assertEqual(sys.path, original_path)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["events"], [2, 0.5, 4.5, 0.5])

    def test_help_and_invalid_waits_do_not_load_upstream(self):
        with patch("collection_runtime.load_upstream") as load, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as help_exit:
                runtime.main(["--help"])
            self.assertEqual(help_exit.exception.code, 0)
            for minimum, maximum in (("nan", "2"), ("1", "inf"), ("-1", "2"), ("2", "1"), ("0", "301")):
                with self.subTest(minimum=minimum, maximum=maximum), self.assertRaises(SystemExit) as error:
                    runtime.main(["--scraper-root", "unused", "--page-wait-min", minimum,
                                  "--page-wait-max", maximum, "--", "--keyword", "架构"])
                self.assertEqual(error.exception.code, 2)
        load.assert_not_called()

    def test_unrecognized_source_returns_a_clear_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_upstream(root, "def main():\n    raise AssertionError('must not run')\n")
            error = io.StringIO()
            with redirect_stderr(error):
                result = runtime.main(["--scraper-root", str(root), "--", "--help"])
            self.assertEqual(result, 2)
            self.assertIn("不支持的上游列表翻页等待结构", error.getvalue())

    def test_original_cli_invocation_uses_real_adapter_and_writes_wait_manifest(self):
        from collect_architecture import invoke_spec

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "组合 输出"
            scraper = Path(directory) / "模拟 上游"
            self.write_upstream(scraper)
            spec = {"combination": "current__shanghai__kw__salary__exp", "track": "current",
                    "city": "上海", "city_id": "shanghai", "keyword": "AI 架构", "keyword_id": "kw",
                    "salary": "不限", "salary_code": "", "experience": "不限", "experience_code": "", "degree": "203"}
            checkpoint = root / "checkpoints" / f"{spec['combination']}.partial.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(json.dumps({"jobs": [{"job_id": "checkpoint"}]}), encoding="utf-8")
            manifest = {"records": []}
            with redirect_stdout(io.StringIO()):
                status = invoke_spec(spec, root=root, manifest=manifest,
                                     manifest_path=root / "collection_manifest.json", python=Path(sys.executable),
                                     scraper_root=scraper, pages=2, page_wait_min=0.75, page_wait_max=0.75)
            self.assertEqual(status, "success")
            record = manifest["records"][0]
            self.assertEqual((record["page_wait_min"], record["page_wait_max"]), (0.75, 0.75))
            payload = json.loads(Path(record["json"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["events"], [2, 0.5, 0.75, 0.5])
            self.assertEqual(len(payload["jobs"]), 2)
            self.assertIn(str(checkpoint), payload["argv"])


if __name__ == "__main__":
    unittest.main()
