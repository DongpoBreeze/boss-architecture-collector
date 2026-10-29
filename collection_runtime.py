#!/usr/bin/env python3
"""Run the upstream CLI with only its list-pagination delay made configurable.

The upstream source stays on disk unchanged.  A deliberately narrow AST check
recognizes its pagination guard, diagnostic and sleep before changing the two
uniform bounds in memory.  An unfamiliar implementation fails before upstream
module initialization, rather than silently keeping its original delay.

Keep this module standard-library-only: the upstream Python executes this file
directly, including when the orchestrator is installed in another environment.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import importlib.util
import math
from pathlib import Path
import sys
from types import ModuleType
from typing import Iterator


DEFAULT_PAGE_WAIT_MIN = 12.0
DEFAULT_PAGE_WAIT_MAX = 22.0
MAX_PAGE_WAIT = 300.0
_UPSTREAM_MODULE = "_boss_architecture_collection_upstream"


class RuntimeCompatibilityError(RuntimeError):
    """The upstream pagination implementation cannot be safely adapted."""


def validate_page_wait(minimum: float, maximum: float) -> tuple[float, float]:
    """Return finite delay bounds, rejecting invalid values consistently."""

    try:
        minimum, maximum = float(minimum), float(maximum)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("翻页等待时间必须为有限数字") from exc
    if not math.isfinite(minimum) or not math.isfinite(maximum):
        raise ValueError("翻页等待时间必须为有限数字")
    if not 0 <= minimum <= maximum <= MAX_PAGE_WAIT:
        raise ValueError(f"翻页等待时间须满足 0 <= 最短 <= 最长 <= {MAX_PAGE_WAIT:g} 秒")
    return minimum, maximum


def _same_ast(node: ast.AST, source: str) -> bool:
    return ast.dump(node, include_attributes=False) == ast.dump(
        ast.parse(source, mode="eval").body, include_attributes=False
    )


def _scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Walk one function's statements without entering nested function scopes."""

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        yield from _scope_nodes(child)


def adapt_page_wait(
    tree: ast.Module, minimum: float, maximum: float
) -> ast.Module:
    """Replace exactly the known upstream 12–22 second pagination expression."""

    minimum, maximum = validate_page_wait(minimum, maximum)
    error = (
        "不支持的上游列表翻页等待结构：需要 scrape_list 的 "
        "pg < max_pages / 翻页等待 / time.sleep(d) 结构；已停止采集"
    )
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "scrape_list"
    ]
    if len(functions) != 1:
        raise RuntimeCompatibilityError(error)
    loops = [
        node for node in _scope_nodes(functions[0])
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name) and node.target.id == "pg"
        and _same_ast(node.iter, "range(1, max_pages + 1)")
    ]
    if len(loops) != 1:
        raise RuntimeCompatibilityError(error)
    guards = [
        node for node in _scope_nodes(functions[0])
        if isinstance(node, ast.If) and _same_ast(node.test, "pg < max_pages")
    ]
    loop_guards = [node for node in _scope_nodes(loops[0]) if node in guards]
    if len(guards) != 1 or len(loop_guards) != 1:
        raise RuntimeCompatibilityError(error)
    guard = guards[0]
    if guard.orelse or len(guard.body) != 3:
        raise RuntimeCompatibilityError(error)
    assignment, diagnostic, sleep = guard.body
    if not (
        isinstance(assignment, ast.Assign)
        and len(assignment.targets) == 1
        and isinstance(assignment.targets[0], ast.Name) and assignment.targets[0].id == "d"
        and _same_ast(assignment.value, "random.uniform(12, 22)")
        and isinstance(diagnostic, ast.Expr)
        and _same_ast(diagnostic.value, 'print(f"  翻页等待 {d:.0f}s...\\n")')
        and isinstance(sleep, ast.Expr)
        and _same_ast(sleep.value, "time.sleep(d)")
    ):
        raise RuntimeCompatibilityError(error)
    uniform = assignment.value
    assert isinstance(uniform, ast.Call)
    uniform.args = [
        ast.copy_location(ast.Constant(value=minimum), uniform.args[0]),
        ast.copy_location(ast.Constant(value=maximum), uniform.args[1]),
    ]
    return ast.fix_missing_locations(tree)


@contextmanager
def _upstream_import_path(script: Path):
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    sys.path.insert(0, str(script.parent))
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode


def load_upstream(
    scraper_root: Path,
    page_wait_min: float = DEFAULT_PAGE_WAIT_MIN,
    page_wait_max: float = DEFAULT_PAGE_WAIT_MAX,
) -> ModuleType:
    """Load a checked, in-memory adaptation without invoking the upstream CLI."""

    minimum, maximum = validate_page_wait(page_wait_min, page_wait_max)
    script = Path(scraper_root).expanduser().resolve() / "scripts" / "boss_cdp_raw.py"
    source = script.read_text(encoding="utf-8-sig")
    tree = adapt_page_wait(ast.parse(source, filename=str(script)), minimum, maximum)
    spec = importlib.util.spec_from_file_location(_UPSTREAM_MODULE, script)
    if spec is None or spec.loader is None:
        raise RuntimeCompatibilityError(f"无法加载上游采集器：{script}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_UPSTREAM_MODULE)
    sys.modules[_UPSTREAM_MODULE] = module
    try:
        with _upstream_import_path(script):
            # Compiling explicitly avoids importing/writing an upstream pyc.
            exec(compile(tree, str(script), "exec", dont_inherit=True), module.__dict__)
        if not callable(getattr(module, "main", None)):
            raise RuntimeCompatibilityError("上游采集器缺少可调用的 main；已停止采集")
    except BaseException:
        if previous is None:
            sys.modules.pop(_UPSTREAM_MODULE, None)
        else:
            sys.modules[_UPSTREAM_MODULE] = previous
        raise
    return module


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为上游列表采集配置翻页等待，原参数放在 -- 后")
    parser.add_argument("--scraper-root", required=True, type=Path)
    parser.add_argument("--page-wait-min", type=float, default=DEFAULT_PAGE_WAIT_MIN)
    parser.add_argument("--page-wait-max", type=float, default=DEFAULT_PAGE_WAIT_MAX)
    parser.add_argument("upstream_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        args.page_wait_min, args.page_wait_max = validate_page_wait(
            args.page_wait_min, args.page_wait_max
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not args.upstream_args or args.upstream_args[0] != "--":
        parser.error("上游参数须放在 -- 分隔符之后")
    args.upstream_args = args.upstream_args[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    script = args.scraper_root.expanduser().resolve() / "scripts" / "boss_cdp_raw.py"
    try:
        upstream = load_upstream(args.scraper_root, args.page_wait_min, args.page_wait_max)
    except (OSError, UnicodeError, SyntaxError, RuntimeCompatibilityError) as exc:
        print(f"列表采集适配失败：{exc}", file=sys.stderr, flush=True)
        return 2
    original_argv = sys.argv
    try:
        sys.argv = [str(script), *args.upstream_args]
        with _upstream_import_path(script):
            result = upstream.main()
        return result if isinstance(result, int) else 0
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    raise SystemExit(main())
