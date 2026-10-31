#!/usr/bin/env python3
"""Run the upstream CLI with configurable pagination and explicit list fields.

The upstream source stays on disk unchanged.  A deliberately narrow AST check
recognizes its pagination guard, diagnostic and sleep before changing the two
uniform bounds in memory.  An unfamiliar implementation fails before upstream
module initialization, rather than silently keeping its original delay.
Company names come from explicit API fields or the known DOM company node;
the upstream's overloaded boss_name field remains unchanged for compatibility.
Experience comes from explicit API fields or complete DOM experience labels,
never search filters, recruiter text or a free-text job description.

Keep this module standard-library-only: the upstream Python executes this file
directly, including when the orchestrator is installed in another environment.
The CLI emits UTF-8 on stdout/stderr because the orchestrator decodes its pipes
as UTF-8.  Initialize that contract before argparse or upstream module loading;
neither the host locale nor PYTHONUTF8 (ignored by Python -I) establishes it.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from functools import wraps
import importlib.util
import inspect
import math
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Iterator


DEFAULT_PAGE_WAIT_MIN = 12.0
DEFAULT_PAGE_WAIT_MAX = 22.0
MAX_PAGE_WAIT = 300.0
_UPSTREAM_MODULE = "_boss_architecture_collection_upstream"
_DOM_EXPERIENCE_PATTERN = (
    r"/^(?:[0-9]{1,2}\s*[-–—~～至]\s*[0-9]{1,2}\s*年"
    r"|[0-9]{1,2}\s*年(?:以上|以内|以下|\s*[+＋])|经验不限"
    r"|(?:在校(?:生)?|应届(?:生)?|无经验)"
    r"(?:\s*[\/／]\s*(?:在校(?:生)?|应届(?:生)?|无经验))*)$/"
)


class RuntimeCompatibilityError(RuntimeError):
    """An upstream collection hook cannot be safely adapted."""


def configure_output_encoding() -> None:
    """Make CLI pipes UTF-8 without replacing or discarding Unicode output.

    In-process callers may redirect output to a text-only stream such as
    StringIO, which needs no byte encoding.  A real stream's reconfiguration
    errors are allowed to surface rather than silently retaining a locale
    encoding.  Importing this module alone does not alter application streams.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")


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


def adapt_company_dom(tree: ast.Module) -> ast.Module:
    """Add company/experience to the recognized upstream extraction template.

    This retains its job/recruiter/link extraction and adds no navigation or
    requests. Pagination-only fixtures without this optional template work as
    before; a changed template fails visibly instead of guessing at DOM nodes.
    """
    assignments = [
        node for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "EXTRACT_LIST_JS" for target in node.targets)
    ]
    if not assignments:
        return tree
    error = "不支持的上游公司名称 DOM 提取结构；已停止采集，未使用招聘者姓名代替公司名"
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Constant):
        raise RuntimeCompatibilityError(error)
    value = assignments[0].value
    source = value.value
    boss_node = "var bossEl = card.querySelector('.boss-name');"
    boss_field = "boss_name: bossEl ? bossEl.innerText.trim() : '',"
    if not isinstance(source, str) or source.count(boss_node) != 1 or source.count(boss_field) != 1:
        raise RuntimeCompatibilityError(error)
    source = source.replace(boss_node, (
        "var companyEl = card.querySelector('.company-name');\n        "
        "var companyName = companyEl && typeof companyEl.innerText === 'string' "
        "? companyEl.innerText.replace(/\\s+/g, ' ').trim() : '';\n        "
        "if (/[\\x00-\\x1f\\x7f]/.test(companyName)) companyName = '';\n        " + boss_node
    ))
    source = source.replace(boss_field, "company: companyName,\n            " + boss_field)
    tag_loop = "for (var j = 0; j < tagEls.length; j++) tags.push(tagEls[j].innerText.trim());"
    tag_field = "tags: tags.join(' | '),"
    if source.count(tag_loop) != 1 or source.count(tag_field) != 1:
        raise RuntimeCompatibilityError("不支持的上游招聘经验 DOM 标签结构；已停止采集，未猜测经验要求")
    source = source.replace(tag_loop, (
        "var experienceValues = [];\n        "
        f"var experiencePattern = {_DOM_EXPERIENCE_PATTERN};\n        "
        "for (var j = 0; j < tagEls.length; j++) {\n            "
        "if (typeof tagEls[j].innerText !== 'string') continue;\n            "
        "var tagText = tagEls[j].innerText.trim();\n            "
        "tags.push(tagText);\n            "
        "var pieces = tagText.split(/[|｜,，;；、\\r\\n]+/);\n            "
        "for (var k = 0; k < pieces.length; k++) {\n                "
        "var experience = pieces[k].trim();\n                "
        "if (experiencePattern.test(experience) && experienceValues.indexOf(experience) === -1) "
        "experienceValues.push(experience);\n            }\n        }"
    ))
    source = source.replace(tag_field, tag_field + "\n            experience: experienceValues.length === 1 ? experienceValues[0] : '',")
    assignments[0].value = ast.copy_location(ast.Constant(value=source), value)
    return ast.fix_missing_locations(tree)


def _company_text(value: Any) -> str:
    """Accept text, never coerce JSON containers, IDs or recruiter names."""
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value if all(ord(character) >= 32 and ord(character) != 127 for character in value) else ""


def _api_company(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for field in ("brandName", "brand_name", "companyName", "company_name"):
        name = _company_text(raw.get(field))
        if name:
            return name
    company = raw.get("company")
    return _company_text(company.get("name")) if isinstance(company, dict) else _company_text(company)


def _experience_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return "" if value.isdigit() else value


def _api_experience(raw: Any) -> str:
    if isinstance(raw, dict):
        for field in ("jobExperience", "job_experience", "experience"):
            experience = _experience_text(raw.get(field))
            if experience:
                return experience
    return ""


def _install_company_capture(module: ModuleType) -> None:
    """Capture company/experience without altering upstream CLI behavior."""
    mapper = getattr(module, "map_api_job", None)
    merge = getattr(module, "merge_unique", None)
    if mapper is not None:
        try:
            inspect.signature(mapper).bind({})
        except (TypeError, ValueError):
            raise RuntimeCompatibilityError("不支持的上游 API 岗位映射接口；已停止采集，未猜测公司或经验") from None

        @wraps(mapper)
        def map_api_job(raw):
            mapping_input = raw
            if isinstance(raw, dict) and "jobExperience" in raw and not isinstance(raw["jobExperience"], str):
                # The known mapper concatenates this field into tags. Invalid
                # JSON types cannot be joined; omit only that unusable label,
                # retaining all valid source fields and the original raw dict.
                mapping_input = {**raw, "jobExperience": ""}
            mapped = mapper(mapping_input)
            return {**mapped, "company": _api_company(raw), "experience": _api_experience(raw)} if isinstance(mapped, dict) else mapped

        module.map_api_job = map_api_job

    if merge is None or (mapper is None and not hasattr(module, "EXTRACT_LIST_JS")):
        return
    try:
        inspect.signature(merge).bind([], [], key="job_id", new_overrides=False)
    except (TypeError, ValueError):
        raise RuntimeCompatibilityError("不支持的上游岗位合并接口；已停止采集，未丢弃已采集公司或经验") from None

    @wraps(merge)
    def merge_unique(existing, incoming, key="job_id", new_overrides=False):
        merged = merge(existing, incoming, key=key, new_overrides=new_overrides)
        # Preserve upstream membership, order and override rules. A duplicate
        # may only fill a missing company/experience, never overwrite another
        # field or a nonempty value on the row upstream chose to keep.
        readers = {"company": _company_text, "experience": _experience_text}
        captured = {}
        groups = (list(reversed(incoming)), list(reversed(existing))) if new_overrides else (existing, incoming)
        for rows in groups:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                identity = row.get(key)
                if not isinstance(identity, (str, int)) or isinstance(identity, bool) or not identity:
                    continue
                for field, read in readers.items():
                    value = read(row.get(field))
                    if value:
                        captured.setdefault(identity, {}).setdefault(field, value)
        result = []
        for row in merged:
            identity = row.get(key) if isinstance(row, dict) else None
            if isinstance(identity, (str, int)) and not isinstance(identity, bool) and identity in captured:
                missing = {field: value for field, value in captured[identity].items() if not readers[field](row.get(field))}
                if missing:
                    row = {**row, **missing}
            result.append(row)
        return result

    module.merge_unique = merge_unique


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
    tree = adapt_company_dom(adapt_page_wait(ast.parse(source, filename=str(script)), minimum, maximum))
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
        _install_company_capture(module)
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
    configure_output_encoding()
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
