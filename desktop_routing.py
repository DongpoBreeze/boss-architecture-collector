"""Read-only daily-folder routing and narrowly scoped checkpoint imports.

Planning never creates directories or runs the processor.  The desktop calls
``resolve_route`` on a worker for both previews and execution, then imports the
selected, single-source prerequisite chain into a fresh run on that worker.
Manifest-provided artifact paths are deliberately never followed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


AUTO_RUN_CHOICE = "自动选择（推荐）"
NEW_RUN_CHOICE = "新建运行目录"

MERGED = "prepared/merged_jobs_index.json"
SHORTLIST = "prepared/jd_shortlists/jd_input.json"
DETAILS = "prepared/jd_shortlists/details.json"
JD_MANIFEST = "prepared/jd_shortlists/details_manifest.json"
REVIEWS = "prepared/llm_reviews.json"
LLM_MANIFEST = "prepared/llm_manifest.json"
_LIST_FILES = ("raw_sample.json", MERGED, SHORTLIST)
_COPY_FILES = frozenset((*_LIST_FILES, DETAILS, JD_MANIFEST, REVIEWS, LLM_MANIFEST))
_BATCH_FILE = re.compile(r"prepared/jd_shortlists/detail_batches/details_\d{4,}\.json\Z")
_TRACKS = {"当前+成长": "all", "当前岗位": "current", "成长目标": "target",
           "all": "all", "current": "current", "target": "target"}
_CITIES = {"上海": "shanghai", "深圳": "shenzhen", "杭州": "hangzhou",
           "广州": "guangzhou", "成都": "chengdu", "北京": "beijing"}
_POST_STAGES = ("列表前处理", "JD补抓", "Codex语义复核", "生成报告")
_STAGES = {
    "仅采集": ("岗位采集",),
    **{name: (name,) for name in _POST_STAGES},
    "后处理全流程（已有数据）": _POST_STAGES,
    "完整流程（采集+后处理）": ("岗位采集", *_POST_STAGES),
}


def canonical_city(value: Any) -> str | None:
    if value in (None, "", "全部城市", "all"):
        return None
    return _CITIES.get(str(value), str(value).casefold())


def canonical_track(value: Any) -> str | None:
    return _TRACKS.get(str(value))


def same_path(first: Any, second: Any) -> bool:
    if not isinstance(first, (str, Path)) or not str(first).strip():
        return False
    if not isinstance(second, (str, Path)) or not str(second).strip():
        return False
    try:
        return os.path.normcase(str(Path(first).expanduser().resolve())) == os.path.normcase(str(Path(second).expanduser().resolve()))
    except (OSError, ValueError, RuntimeError):
        return False


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _safe_path(root: Path, relative: str) -> Path:
    """Reject both lexical escapes and symlinks/junctions in an artifact chain."""
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("输入文件路径越过所选日目录。")
    path = root / relative_path
    if not _contained(path, root):
        raise ValueError("输入文件链接指向所选日目录之外，已拒绝读取。")
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("输入文件使用符号链接或目录联接，已拒绝自动读取。")
    return path


def _json(root: Path, relative: str) -> Any:
    try:
        raw = _safe_path(root, relative).read_text(encoding="utf-8-sig")
        return json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _rows(value: Any, *keys: str) -> list[dict[str, Any]] | None:
    if isinstance(value, dict):
        value = next((value[key] for key in keys if key in value), None)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        return None
    return value


def _aliases(job: dict[str, Any]) -> set[str]:
    return {str(job[key]).strip() for key in ("job_id", "dedupe_key", "encrypt_job_id", "job_link", "link")
            if job.get(key) is not None and str(job[key]).strip()}


def _daily_marker(path: Path) -> bool:
    return (path / "raw").is_dir() or (path / "collection_manifest.json").is_file()


def resolve_daily_root(path: str | Path) -> tuple[Path, tuple[str, ...]]:
    """Recognize one day, including an accidentally selected raw/prepared/run.

    A parent containing daily folders is never resolved to the newest day.
    Nonexistent paths remain valid here so collection can create a new day.
    """
    if isinstance(path, str) and not path.strip():
        raise ValueError("请先选择某天的采集总文件夹，结果目录不能为空。")
    original = Path(path).expanduser().absolute()
    selected = original.resolve()
    if selected.exists() and not selected.is_dir():
        raise ValueError("请选择某天的采集总文件夹，不能选择文件。")
    if _daily_marker(selected):
        return selected, ()
    # Follow structure, not source_root strings from untrusted manifests.
    for parent in original.parents:
        if _daily_marker(parent):
            relative = original.relative_to(parent)
            if relative.parts and relative.parts[0] in ("raw", "prepared", "runs", "test-runs"):
                root = parent.resolve()
                if not _contained(original, root):
                    raise ValueError("所选子目录通过链接跳出了日目录；请直接选择某天的采集总文件夹。")
                return root, (f"已将所选子目录识别为日目录：{root}；城市与轨道仍按当前设置，不从目录名猜测。",)
            break
    if selected.is_dir():
        days = [child for child in selected.iterdir() if child.is_dir() and not child.is_symlink() and _daily_marker(child)]
        if days:
            examples = "、".join(sorted(child.name for child in days)[:3])
            raise ValueError(f"这里包含采集日目录（{examples}）。请选择其中某天的采集总文件夹，不要选择 job-result 总目录。")
    if original != selected:
        raise ValueError("所选目录通过链接指向其他位置；请直接选择某天的采集总文件夹。")
    return selected, ()


@dataclass(frozen=True)
class RoutePlan:
    requested_root: Path
    source_root: Path
    stage: str
    city: str
    track: str
    sample_size: int | None
    mode: str
    stages: tuple[str, ...]
    input_run: Path | None = None
    read_files: tuple[Path, ...] = ()
    copy_files: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    quality_incomplete: bool = False
    recovered_stages: tuple[str, ...] = ()
    fixed_sample: Path | None = None

    @property
    def summary(self) -> str:
        scope = f"范围：{self.city} · {self.track} · {self.sample_size or '全部'} 个去重岗位"
        if self.mode == "collection":
            source = f"写入：{self.source_root / 'raw'}（仅采集，不读取旧 prepared，不自动后处理）"
        elif self.input_run:
            names = "、".join(Path(item).name for item in self.copy_files if not _BATCH_FILE.fullmatch(item))
            source = f"读取：{self.input_run}" + (f"\n输入文件：{names}" if names else "（显式历史续跑）")
        else:
            source = f"读取：{self.source_root / 'raw'}（按当前城市与轨道读取原始列表）"
        target = (f"复用原运行目录：{self.input_run}" if self.mode == "resume" else
                  "采集输出保留在所选日目录" if self.mode == "collection" else
                  f"新建输出：{self.source_root / 'runs' / '<新时间戳>'}（不覆盖旧运行）")
        lines = [scope, source, "执行：" + " → ".join(self.stages), target]
        if self.missing:
            lines.append("缺失前置：" + "；".join(self.missing))
        if self.warnings:
            lines.append("提示：" + "；".join(self.warnings))
        if self.errors:
            lines.append("暂不能运行：" + "；".join(self.errors))
        return "\n".join(lines)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "requested_root": str(self.requested_root),
            "source_root": str(self.source_root),
            "input_run": str(self.input_run) if self.input_run else None,
            "read_files": [str(path) for path in self.read_files],
            "copy_files": list(self.copy_files), "missing_prerequisites": list(self.missing),
            "execution_plan": list(self.stages), "warnings": list(self.warnings),
            "errors": list(self.errors), "quality_incomplete": self.quality_incomplete,
            "recovered_stages": list(self.recovered_stages),
            "fixed_sample": str(self.fixed_sample) if self.fixed_sample else None,
            "preparation_complete": True,
            "output_policy": "reuse_explicit_run" if self.mode == "resume" else "collection_root" if self.mode == "collection" else "new_run",
        }


def _scope_matches(value: dict[str, Any], root: Path, city: str, track: str,
                   sample: int | None, *, metadata: bool = False) -> bool:
    track_key = "tracks" if metadata else "track"
    required = ("source_root", "city", track_key, "sample_size")
    if not all(key in value for key in required):
        return False
    saved_sample = value["sample_size"]
    return (same_path(value["source_root"], root)
            and canonical_city(value["city"]) == canonical_city(city)
            and canonical_track(value[track_key]) == canonical_track(track)
            and (saved_sample is None or type(saved_sample) is int)
            and saved_sample == sample)


def _stage_status(manifest: dict[str, Any], label: str) -> str | None:
    stages = manifest.get("stages")
    entry = stages.get(label) if isinstance(stages, dict) else None
    return entry.get("status") if isinstance(entry, dict) else None


@dataclass
class _Checkpoint:
    root: Path
    updated: float
    list_files: tuple[str, ...]
    jd_files: tuple[str, ...]
    model_files: tuple[str, ...]
    jd_count: int
    jd_total: int
    model_count: int
    warnings: tuple[str, ...]
    manifest_exists: bool


def _checkpoint(candidate: Path, daily: Path, city: str, track: str,
                sample: int | None, *, allow_unfinished_list: bool = False) -> tuple[_Checkpoint | None, str | None]:
    try:
        relative = candidate.relative_to(daily).as_posix()
        _safe_path(daily, relative)
    except (ValueError, OSError):
        return None, "目录链接或路径越界"
    manifest_path = candidate / "run_manifest.json"
    has_manifest = manifest_path.exists()
    manifest = _json(candidate, "run_manifest.json") if has_manifest else {}
    if not isinstance(manifest, dict) or (has_manifest and not _scope_matches(manifest, daily, city, track, sample)):
        return None, "运行清单无效或城市/轨道/样本/source_root 不一致"
    if manifest.get("status") == "running" and not allow_unfinished_list:
        return None, "运行尚未结束，避免读取正在改写的检查点"
    if not allow_unfinished_list and _stage_status(manifest, "列表前处理") not in (None, "success", "completed", "partial"):
        return None, "列表前处理检查点已失效或未完成（含 stale）"
    merged = _json(candidate, MERGED)
    jobs = _rows(merged, "jobs")
    meta = merged.get("metadata") if isinstance(merged, dict) else None
    if jobs is None or not isinstance(meta, dict) or not _scope_matches(meta, daily, city, track, sample, metadata=True):
        return None, "列表索引缺失、无效或范围不一致"
    if meta.get("sample_strategy") != "balanced-dedup":
        return None, "不是按去重岗位数量抽样的列表检查点"
    identifiers = [_aliases(job) for job in jobs]
    known = set().union(*identifiers) if identifiers else set()
    seen: set[str] = set()
    for aliases in identifiers:
        if not aliases or seen.intersection(aliases):
            return None, "列表索引缺少岗位身份或存在重复岗位"
        seen.update(aliases)
    if sample is not None and len(jobs) > sample:
        return None, "列表实际岗位数超出所选样本"
    shortlist = _json(candidate, SHORTLIST)
    eligible = _rows(shortlist, "jobs")
    if eligible is None or any(not _aliases(job) or not _aliases(job).intersection(known) for job in eligible):
        return None, "JD 初筛输入缺失、无效或不属于当前列表"
    eligible_keys = set().union(*(_aliases(job) for job in eligible)) if eligible else set()
    warnings: list[str] = []
    list_files = [MERGED, SHORTLIST]
    sample_rows = _rows(_json(candidate, "raw_sample.json"), "jobs")
    if sample_rows is not None and all(_aliases(job).intersection(known) for job in sample_rows):
        list_files.insert(0, "raw_sample.json")

    jd_files: list[str] = []
    detail_rows = _rows(_json(candidate, DETAILS), "jobs", "details", "records")
    if detail_rows is not None and all(_aliases(row).intersection(known) for row in detail_rows):
        jd_files.append(DETAILS)
    else:
        detail_rows = []
    jd_count = sum(any(_aliases(job).intersection(_aliases(row)) and str(row.get("jd") or "").strip()
                       for row in detail_rows) for job in eligible)
    jd_manifest = _json(candidate, JD_MANIFEST)
    if (isinstance(jd_manifest, dict) and type(jd_manifest.get("total_jobs")) is int
            and jd_manifest["total_jobs"] == len(eligible)
            and type(jd_manifest.get("batch_size")) is int and jd_manifest["batch_size"] > 0
            and isinstance(jd_manifest.get("records"), list)
            and all(isinstance(row, dict) and (not row.get("job_ids") or
                    isinstance(row["job_ids"], list) and set(map(str, row["job_ids"])).issubset(eligible_keys))
                    for row in jd_manifest["records"])):
        jd_files.append(JD_MANIFEST)
    try:
        batch_dir = _safe_path(candidate, "prepared/jd_shortlists/detail_batches")
        for path in sorted(batch_dir.glob("details_*.json")):
            relative = path.relative_to(candidate).as_posix()
            if _BATCH_FILE.fullmatch(relative):
                rows = _rows(_json(candidate, relative), "jobs", "details", "records")
                if rows is not None and all(_aliases(row).intersection(known) for row in rows):
                    jd_files.append(relative)
    except (OSError, ValueError):
        pass
    if _stage_status(manifest, "JD补抓") == "stale":
        warnings.append("来源 JD 阶段为 stale，不作为模型或报告的有效正文；仅补抓可恢复逐岗检查点")
        jd_count = 0

    model_files: list[str] = []
    reviews = _rows(_json(candidate, REVIEWS), "reviews")
    if (reviews is not None and all(_aliases(row).intersection(eligible_keys) for row in reviews)
            and _stage_status(manifest, "JD补抓") != "stale"
            and _stage_status(manifest, "Codex语义复核") != "stale"):
        model_files.append(REVIEWS)
        llm_manifest = _json(candidate, LLM_MANIFEST)
        if isinstance(llm_manifest, dict) and llm_manifest:
            model_files.append(LLM_MANIFEST)
    elif _stage_status(manifest, "Codex语义复核") == "stale":
        warnings.append("来源模型阶段为 stale，不导入旧模型结论")
    model_count = sum(row.get("status") == "success" for row in reviews or []) if model_files else 0
    updated = 0.0
    for relative in ("run_manifest.json", MERGED, SHORTLIST, *jd_files, *model_files):
        try:
            updated = max(updated, _safe_path(candidate, relative).stat().st_mtime)
        except (ValueError, OSError):
            continue
    for value in (manifest.get("updated_at"), manifest.get("finished_at"), meta.get("generated_at")):
        if isinstance(value, str):
            try:
                updated = max(updated, datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except (ValueError, OSError, OverflowError):
                pass
    return _Checkpoint(candidate, updated, tuple(list_files), tuple(jd_files), tuple(model_files),
                       jd_count, len(eligible), model_count, tuple(warnings), has_manifest), None


def load_fixed_sample(output: Path, daily: Path, city: str, track: str,
                      sample: int | None) -> dict[str, Any] | None:
    """Validate an already frozen balanced sample without consulting raw."""
    value = _json(output, "raw_sample.json")
    jobs = _rows(value, "jobs")
    metadata = value.get("metadata") if isinstance(value, dict) else None
    if (jobs is None or not isinstance(metadata, dict)
            or not _scope_matches(metadata, daily, city, track, sample, metadata=True)
            or metadata.get("sample_strategy") != "balanced-dedup"
            or sample is not None and len(jobs) > sample):
        return None
    seen: set[str] = set()
    for job in jobs:
        aliases = _aliases(job)
        if not aliases or aliases.intersection(seen):
            return None
        seen.update(aliases)
    return value


def _resume_plan(manifest: dict[str, Any], selected: Path, daily: Path,
                 stage: str, city: str, track: str, sample: int | None,
                 common: dict[str, Any], warnings: list[str]) -> RoutePlan:
    saved_routing = manifest.get("routing")
    saved_routing = saved_routing if isinstance(saved_routing, dict) else {}
    if saved_routing.get("preparation_complete") is False:
        return RoutePlan(**common, mode="resume", input_run=selected, warnings=tuple(warnings),
                         errors=("该运行的前置文件导入未完整完成，不能按已有目录继续。请改用“自动选择（推荐）”新建运行；原输入与旧结果未覆盖。",))
    recovered: tuple[str, ...] = ()
    fixed_sample: Path | None = None
    missing: tuple[str, ...] = ()
    incomplete = False
    # A saved report plan must never authorize BOSS or model work.  Only the
    # optional, local shortlist prefix that this router itself can add is
    # accepted; no arbitrary stage list is replayed from a manifest.
    allowed = {_STAGES[stage]}
    if stage in ("JD补抓", "生成报告"):
        allowed.add(("列表前处理", stage))
    saved = saved_routing.get("execution_plan")
    if manifest.get("stage_selection") == stage and isinstance(saved, list):
        saved_plan = tuple(saved) if all(isinstance(item, str) for item in saved) else ()
        if saved_plan in allowed:
            common["stages"] = saved_plan
            missing = tuple(item for item in saved_routing.get("missing_prerequisites", []) if isinstance(item, str)) if isinstance(saved_routing.get("missing_prerequisites"), list) else ()
            incomplete = saved_routing.get("quality_incomplete") is True
            if saved_plan == ("列表前处理", stage):
                checkpoint, _reason = _checkpoint(selected, daily, city, track, sample, allow_unfinished_list=True)
                if checkpoint and _stage_status(manifest, "列表前处理") != "stale":
                    common["stages"] = (stage,)
                    recovered = ("列表前处理",)
                    warnings.append("已有固定样本与完整列表索引，恢复检查点后继续，不重新抽样")
                elif load_fixed_sample(selected, daily, city, track, sample) is not None:
                    fixed_sample = selected / "raw_sample.json"
                    warnings.append("从本运行已固定 raw_sample.json 重建缺失列表索引，不回 raw 重新抽样")
        else:
            warnings.append("已忽略无效的历史路由执行链；不会扩展所选阶段的浏览器或模型权限")
    reads = [selected / "run_manifest.json"]
    for relative in _COPY_FILES:
        try:
            path = _safe_path(selected, relative)
            if path.is_file():
                reads.append(path)
        except (ValueError, OSError):
            continue
    if "列表前处理" in common["stages"] and fixed_sample is None:
        reads.extend(_raw_files(daily, city, track))
    return RoutePlan(**common, mode="resume", input_run=selected, read_files=tuple(reads),
                     warnings=tuple(warnings), missing=missing, quality_incomplete=incomplete,
                     recovered_stages=recovered, fixed_sample=fixed_sample)


def _raw_files(root: Path, city: str, track: str) -> tuple[Path, ...]:
    files: list[Path] = []
    tracks = ("current", "target") if canonical_track(track) == "all" else (canonical_track(track),)
    cities = (canonical_city(city),) if canonical_city(city) else tuple(_CITIES.values())
    for selected_track in tracks:
        for selected_city in cities:
            relative_dir = f"raw/{selected_track}/{selected_city}"
            try:
                directory = _safe_path(root, relative_dir)
                for path in sorted(directory.glob("*.json")):
                    parts = path.stem.split("__")
                    if len(parts) != 5 or parts[0] != selected_track or parts[1] != selected_city:
                        continue
                    relative = path.relative_to(root).as_posix()
                    value = _json(root, relative)
                    if isinstance(value, dict) and isinstance(value.get("filters"), dict) and _rows(value, "jobs") is not None:
                        files.append(path)
            except (OSError, ValueError):
                continue
    return tuple(files)


def resolve_route(root: str | Path, stage: str, city: str, track: str,
                  sample_size: int | None, run_choice: str = AUTO_RUN_CHOICE) -> RoutePlan:
    """Return a filesystem-read-only execution plan for exactly one scope.

    Errors about missing prerequisites are data on the plan so the preview can
    explain them; an ambiguous/unsafe selected directory raises ``ValueError``.
    Only automatic single stages import checkpoints, always from one run.
    """
    daily, normalization = resolve_daily_root(root)
    requested = Path(root).expanduser().absolute()
    if stage not in _STAGES or canonical_track(track) is None or (sample_size is not None and (type(sample_size) is not int or sample_size < 1)):
        raise ValueError("阶段、岗位轨道或去重岗位数量无效。")
    warnings = list(normalization)
    common = dict(requested_root=requested, source_root=daily, stage=stage, city=city,
                  track=track, sample_size=sample_size, stages=_STAGES[stage])
    if stage == "仅采集":
        _safe_path(daily, "raw")
        return RoutePlan(**common, mode="collection", warnings=tuple(warnings))
    _safe_path(daily, "runs")
    if run_choice not in (AUTO_RUN_CHOICE, NEW_RUN_CHOICE):
        selected = Path(run_choice).expanduser().absolute()
        if not _contained(selected, daily):
            raise ValueError("所选历史运行不在当前日目录内；请先选择对应的采集日目录。")
        _safe_path(daily, selected.relative_to(daily).as_posix())
        manifest = _json(selected, "run_manifest.json")
        errors = []
        if not selected.is_dir() or not isinstance(manifest, dict) or not manifest:
            errors.append("历史运行的 run_manifest.json 缺失或无效；不会覆盖未知范围的旧结果。")
        elif not _scope_matches(manifest, daily, city, track, sample_size):
            errors.append("历史运行的城市、轨道、岗位数量或 source_root 与当前设置不同，请载入原设置或新建运行。")
        if errors:
            return RoutePlan(**common, mode="resume", input_run=selected,
                             read_files=(selected / "run_manifest.json",), warnings=tuple(warnings), errors=tuple(errors))
        return _resume_plan(manifest, selected, daily, stage, city, track, sample_size, common, warnings)
    mode = "auto" if run_choice == AUTO_RUN_CHOICE else "new"
    if mode == "new" and stage in ("JD补抓", "Codex语义复核", "生成报告"):
        return RoutePlan(**common, mode=mode, errors=("该阶段需要已有检查点。请使用“自动选择（推荐）”，或显式选择历史运行续跑。",), warnings=tuple(warnings))
    # Full workflows deliberately start at raw.  Importing old downstream
    # conclusions before rerunning shortlist/JD would make them stale at once.
    if stage in ("列表前处理", "后处理全流程（已有数据）", "完整流程（采集+后处理）"):
        files = _raw_files(daily, city, track)
        errors = () if files or stage == "完整流程（采集+后处理）" else ("当前城市和轨道没有可读取的原始列表；请检查所选日目录或先运行采集。",)
        return RoutePlan(**common, mode=mode, read_files=files, warnings=tuple(warnings), errors=errors)

    candidates = [daily]
    for container in ("runs", "test-runs"):
        try:
            directory = _safe_path(daily, container)
            candidates.extend(path for path in directory.iterdir() if path.is_dir())
        except (OSError, ValueError):
            continue
    usable = []
    rejected = []
    for candidate in candidates:
        if not (candidate / "prepared").exists():
            continue
        checkpoint, reason = _checkpoint(candidate, daily, city, track, sample_size)
        if checkpoint:
            usable.append(checkpoint)
        elif reason:
            rejected.append(f"{candidate.name}：{reason}")
    if rejected:
        warnings.append(f"已忽略 {len(rejected)} 个范围不符或无效检查点（" + "；".join(rejected[:2]) + "）")
    if stage == "Codex语义复核":
        usable = [checkpoint for checkpoint in usable if checkpoint.jd_count > 0]
    selected = max(usable, key=lambda checkpoint: (checkpoint.updated, str(checkpoint.root))) if usable else None
    missing: list[str] = []
    if selected is None:
        if stage == "Codex语义复核":
            return RoutePlan(**common, mode=mode, missing=("相同范围的完整 JD 正文",), warnings=tuple(warnings),
                             errors=("未找到相同范围且含完整 JD 的可用检查点。请先运行“JD补抓”或“后处理全流程（已有数据）”；语义复核不会自行启动 BOSS 网络抓取。",))
        files = _raw_files(daily, city, track)
        missing.append("可用列表检查点（将先在本地执行列表前处理）")
        common["stages"] = ("列表前处理", stage)
        if stage == "生成报告":
            missing.extend(("JD 正文", "模型语义复核"))
            warnings.append("仅生成本地规则报告，缺 JD/模型项标记待核实；不调用浏览器或模型")
        errors = () if files else ("当前范围既没有可用检查点，也没有原始列表；请选择正确的采集日目录或先采集。",)
        return RoutePlan(**common, mode=mode, read_files=files, missing=tuple(missing), warnings=tuple(warnings),
                         errors=errors, quality_incomplete=stage == "生成报告")
    warnings.extend(selected.warnings)
    copies = list(selected.list_files)
    if stage == "JD补抓":
        copies.extend(selected.jd_files)
        warnings.append("只复用列表和逐岗 JD 检查点；本次补抓后需另行复核模型/报告，不导入旧模型结论")
    else:
        if selected.jd_count:
            copies.extend(selected.jd_files)
            copies.extend(selected.model_files)
        if selected.jd_count < selected.jd_total:
            missing.append(f"JD 正文 {selected.jd_total - selected.jd_count}/{selected.jd_total} 岗")
        if not selected.jd_count:
            missing.append("完整 JD 正文")
        if stage == "生成报告" and (not selected.model_files or selected.model_count < selected.jd_count):
            missing.append("模型语义复核" if not selected.model_count else f"模型语义复核 {selected.jd_count - selected.model_count} 岗")
        if stage == "生成报告":
            warnings.append("仅按所选检查点重新生成报告，不启动 BOSS 补抓或模型调用")
    reads = ([selected.root / "run_manifest.json"] if selected.manifest_exists else []) + [selected.root / relative for relative in copies]
    return RoutePlan(**common, mode=mode, input_run=selected.root, read_files=tuple(reads),
                     copy_files=tuple(copies), missing=tuple(missing), warnings=tuple(warnings),
                     quality_incomplete=bool(missing) and stage in ("生成报告", "Codex语义复核"))


def copy_route_inputs(plan: RoutePlan, output: str | Path,
                      *, cancelled: Callable[[], bool] | None = None) -> tuple[Path, ...]:
    """Import only the selected prerequisite chain, never a whole directory.

    The destination must be a fresh, contained run.  Old reports, auth/config,
    raw trees, arbitrary manifest.output_files, and symlink escapes are not
    copied.  Source artifacts are never modified.
    """
    target = Path(output).expanduser().absolute()
    if plan.errors:
        raise ValueError("；".join(plan.errors))
    if plan.mode not in ("auto", "new") or not plan.input_run or not plan.copy_files:
        return ()
    if (not _contained(target, plan.source_root / "runs")
            or same_path(target, plan.source_root / "runs") or same_path(target, plan.input_run)):
        raise ValueError("自动路由输出必须是当前日目录下的新 runs 子目录。")
    _safe_path(plan.source_root, target.relative_to(plan.source_root).as_posix())
    if any(target.iterdir()):
        raise ValueError("自动导入只允许空的新运行目录，不能覆盖已有文件。")
    imported: list[Path] = []
    for relative in plan.copy_files:
        if cancelled and cancelled():
            raise InterruptedError("已停止检查点导入。")
        if relative not in _COPY_FILES and not _BATCH_FILE.fullmatch(relative):
            raise ValueError("拒绝复制非白名单前置文件。")
        if not _contained(plan.input_run, plan.source_root):
            raise ValueError("输入运行不在所选日目录内。")
        _safe_path(plan.source_root, plan.input_run.relative_to(plan.source_root).as_posix())
        source = _safe_path(plan.input_run, relative)
        destination = _safe_path(target, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # report_stage can otherwise read JD text embedded by an old report
        # even when its separate JD checkpoint was explicitly rejected/stale.
        if relative in (MERGED, SHORTLIST) and DETAILS not in plan.copy_files and plan.stage != "JD补抓":
            value = _json(plan.input_run, relative)
            jobs = _rows(value, "jobs")
            if jobs is None:
                raise ValueError("输入检查点在预览后发生变化，请刷新并重试。")
            for job in jobs:
                job.pop("jd", None)
            with destination.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        else:
            # Exclusive destination prevents accidental overwrite if files
            # appeared between the empty-directory check and this copy.
            with source.open("rb") as source_handle, destination.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
        imported.append(destination)
    return tuple(imported)
