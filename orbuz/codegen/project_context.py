"""
ProjectContext — 项目上下文扫描器
=====================================
扫描项目目录 → 提取结构化摘要 → 注入 LLM prompt。

通用设计:
  - 语言无关：通过 scanner 注册表支持多种语言
  - 默认内置: rust, python, cpp 三种 scanner
  - 可外部注册: add_scanner("go", my_scanner_fn)

用法:
    context = build_project_context("/path/to/project")
    # {"project_name": "...", "language": "rust",
    #  "files": [...], "types": [...], "traits": [...],
    #  "deps": [...], "summary": "..."}
"""

from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path
from typing import Callable


# ── Scanner 注册表 ──

ScannerFn = Callable[[Path], dict]
_scanners: dict[str, ScannerFn] = {}


def register_scanner(language: str, fn: ScannerFn):
    """注册一个语言扫描器。覆盖已有的。"""
    _scanners[language] = fn


def detect_language(project_dir: Path) -> str | None:
    """根据项目根目录的文件推测语言。"""
    checks = {
        "rust": lambda p: (p / "Cargo.toml").exists(),
        "python": lambda p: (
            (p / "pyproject.toml").exists()
            or (p / "setup.py").exists()
            or (p / "requirements.txt").exists()
        ),
        "cpp": lambda p: (
            list(p.rglob("*.cpp"))[:1] or list(p.rglob("*.hpp"))[:1]
        ) and not (p / "Cargo.toml").exists(),
    }
    for lang, check in checks.items():
        if check(project_dir):
            return lang
    return None


def build_project_context(
    project_dir: str,
    language: str | None = None,
    max_files: int = 30,
    include_unstaged: bool = True,
) -> dict:
    """
    扫描项目目录 → 返回结构化上下文。

    参数:
        project_dir: 项目根目录路径
        language: 强制指定语言（None = 自动检测）
        max_files: 最大扫描文件数
        include_unstaged: 是否包含 git unstaged 变更
    """
    root = Path(project_dir).expanduser().resolve()
    if not root.exists():
        return {"error": f"目录不存在: {project_dir}"}

    lang = language or detect_language(root)
    if not lang:
        return {
            "project_dir": str(root),
            "error": "无法自动检测语言，请通过 language= 参数指定",
        }

    scanner = _scanners.get(lang)
    if not scanner:
        return {
            "project_dir": str(root),
            "language": lang,
            "error": f"不支持的语言: {lang}（已注册: {list(_scanners.keys())}）",
        }

    result = scanner(root)
    result["project_dir"] = str(root)
    result["language"] = lang

    # 附加 git context
    if include_unstaged:
        result["git_status"] = _git_status(root)

    # 限制文件列表长度
    if "files" in result and len(result["files"]) > max_files:
        result["files"] = result["files"][:max_files]
        result["files_truncated"] = True

    # 生成简短摘要
    result["summary"] = _summarize(result)

    # 附加项目知识文档（架构规则/约定）
    result["knowledge"] = build_knowledge_context(root)
    if result["knowledge"]:
        result["summary"] += f" | 知识: {result['knowledge']['file_count']} 文件"

    return result


def _summarize(ctx: dict) -> str:
    """从扫描结果生成一行摘要。"""
    parts = [f"项目: {Path(ctx['project_dir']).name}"]
    parts.append(f"语言: {ctx.get('language', '?')}")
    n_files = len(ctx.get("files", []))
    parts.append(f"文件数: {n_files}")
    n_types = len(ctx.get("types", []))
    if n_types:
        parts.append(f"类型/结构体: {n_types}")
    n_traits = len(ctx.get("traits", []))
    if n_traits:
        parts.append(f"接口/Trait: {n_traits}")
    n_impls = len(ctx.get("impls", []))
    if n_impls:
        parts.append(f"Impl块: {n_impls}")
    deps = ctx.get("deps", [])
    if deps:
        parts.append(f"依赖: {len(deps)}")
    return " | ".join(parts)


def _git_status(root: Path) -> dict:
    """获取 git 状态摘要。"""
    try:
        # 检查是否有 git 仓库
        git_dir = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
            cwd=str(root),
        )
        if git_dir.returncode != 0:
            return {}
        repo_root = git_dir.stdout.strip()

        # 已修改的文件
        modified = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, timeout=5,
            cwd=repo_root,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=5,
            cwd=repo_root,
        )
        return {
            "modified_files": [f for f in modified.stdout.splitlines() if f],
            "untracked_files": [f for f in untracked.stdout.splitlines() if f],
        }
    except Exception:
        return {}


# ── 知识文档自动发现 ──

KNOWLEDGE_CANDIDATES = [
    "CODEGEN.md",         # 最优先：显式的代码生成规则
    "CODEGEN.yaml",       # YAML 格式的代码生成规则
    "CODEGEN.yml",
    ".orbuz/knowledge.md",  # orbuz 专用知识目录
    ".orbuz/rules.md",
    "ARCHITECTURE.md",    # 架构文档（项目通用）
    "ARCHITECTURE.yaml",
    "DESIGN.md",          # 设计文档
    "AGENTS.md",          # Claude Code/Cursor 风格的 agent 指令
    "CLAUDE.md",
    ".cursorrules",
]


def find_knowledge_files(project_dir: Path) -> list[tuple[Path, str]]:
    """
    在项目根目录搜索知识文档文件。

    返回: [(文件路径, 文件名), ...]
    按 KNOWLEDGE_CANDIDATES 优先级排序。
    """
    found = []
    for name in KNOWLEDGE_CANDIDATES:
        fpath = project_dir / name
        if fpath.exists() and fpath.is_file():
            # 跳过空文件
            try:
                size = fpath.stat().st_size
                if size > 0:
                    found.append((fpath, name))
            except OSError:
                continue
    return found


def build_knowledge_context(project_dir: Path) -> dict:
    """
    扫描项目根目录 → 读取并拼接所有知识文档。

    返回:
    {
        "file_count": 2,
        "files": ["CODEGEN.md", ".orbuz/rules.md"],
        "content": "## Architecture Rules\\n...",
        "prompt_block": "# 项目知识\\n## 来自 CODEGEN.md\\n...",
    }
    如果没有发现知识文档，返回空 dict。
    """
    found = find_knowledge_files(project_dir)
    if not found:
        return {}

    files_info = []
    content_parts = []

    for fpath, fname in found:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                files_info.append(fname)
                content_parts.append(f"## 来自 {fname}\n{text}")
        except Exception:
            continue

    if not content_parts:
        return {}

    block = "# 项目知识\n" + "\n\n".join(content_parts)

    return {
        "file_count": len(files_info),
        "files": files_info,
        "content": block,
    }


def get_knowledge_prompt_block(project_dir: str | Path) -> str:
    """
    快速接口：给定项目路径 → 返回知识文档的 prompt 注入块。
    未发现时返回空字符串。
    """
    root = Path(project_dir).expanduser().resolve()
    ctx = build_knowledge_context(root)
    return ctx.get("content", "")


# ── Rust Scanner ──


def _scan_rust(root: Path) -> dict:
    """扫描 Rust 项目。"""
    result: dict = {
        "files": [],
        "crates": [],
        "types": [],
        "traits": [],
        "fns": [],
        "impls": [],
        "deps": [],
    }

    # Cargo.toml 解析
    cargo_toml = root / "Cargo.toml"
    if cargo_toml.exists():
        result["cargo_name"] = _parse_cargo_name(cargo_toml)
        result["deps"] = _parse_cargo_deps(cargo_toml)
        result["workspace"] = _check_workspace(cargo_toml)

    # 多 crate workspace: 扫描所有成员
    if result.get("workspace"):
        result["crates"] = []
        for member_dir in result["workspace"]:
            member_path = root / member_dir
            if member_path.exists():
                cargo = member_path / "Cargo.toml"
                if cargo.exists():
                    name = _parse_cargo_name(cargo)
                    result["crates"].append({
                        "name": name or member_dir,
                        "path": str(member_path.relative_to(root)),
                    })

    # 扫描 .rs 文件
    for rs_file in sorted(root.rglob("*.rs")):
        # 跳过 target/
        if "target" in rs_file.parts:
            continue
        rel = str(rs_file.relative_to(root))
        result["files"].append(rel)
        content = rs_file.read_text(encoding="utf-8", errors="replace")
        result["types"].extend(_extract_types(content, rel))
        result["traits"].extend(_extract_traits(content, rel))
        result["fns"].extend(_extract_fn_sigs(content, rel))
        result["impls"].extend(_extract_impls(content, rel))

    return result


def _parse_cargo_name(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _parse_cargo_deps(path: Path) -> list[dict]:
    deps = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'^\[dependencies\.([^\]]+)\]', content, re.MULTILINE):
            name = m.group(1).strip()
            deps.append({"name": name, "source": "table"})
        in_deps = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[dependencies]"):
                in_deps = True
                continue
            if in_deps:
                if stripped.startswith("["):
                    in_deps = False
                    continue
                m = re.match(r'^([a-zA-Z0-9_-]+)\s*=', stripped)
                if m:
                    deps.append({"name": m.group(1), "source": "inline"})
    except Exception:
        pass
    return deps


def _check_workspace(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        members = []
        in_workspace = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[workspace]"):
                in_workspace = True
                continue
            if in_workspace:
                if stripped.startswith("["):
                    break
                m = re.match(r'^members\s*=\s*\[(.+)\]', stripped)
                if m:
                    raw = m.group(1)
                    members = [
                        p.strip().strip('"')
                        for p in raw.split(",")
                        if p.strip()
                    ]
    except Exception:
        return []
    return members


_RE_STRUCT = re.compile(r'^\s*(?:pub\s+)?(?:struct|enum|union)\s+(\w+)')
_RE_TRAIT = re.compile(r'^\s*(?:pub\s+)?((?:unsafe\s+)?trait)\s+(\w+)')
_RE_PUB_FN = re.compile(r'^\s*pub\s+(?:unsafe\s+)?fn\s+(\w+)')
_RE_IMPL = re.compile(r'^\s*impl(\s*<[^>]+>)?\s+(\w+)')
# 完整函数签名（含参数和返回类型，忽略包含 async/unsafe）
_RE_FN_SIG = re.compile(
    r'^\s*(?:pub\s+)?(?:unsafe\s+)?(?:async\s+)?fn\s+(\w+)\s*\(([^)]*)\)\s*(->\s*(?:[^{;]+))?',
    re.MULTILINE,
)


def _extract_types(content: str, rel_path: str) -> list[dict]:
    types = []
    for m in _RE_STRUCT.finditer(content):
        types.append({"name": m.group(1), "kind": "struct", "file": rel_path})
    return types


def _extract_traits(content: str, rel_path: str) -> list[dict]:
    traits = []
    for m in _RE_TRAIT.finditer(content):
        traits.append({"name": m.group(2), "file": rel_path})
    return traits


def _extract_pub_fns(content: str, rel_path: str) -> list[dict]:
    fns = []
    for m in _RE_PUB_FN.finditer(content):
        fns.append({"name": m.group(1), "file": rel_path})
    return fns


def _extract_fn_sigs(content: str, rel_path: str) -> list[dict]:
    """提取完整函数签名（含参数列表和返回类型）。"""
    fns = []
    for m in _RE_FN_SIG.finditer(content):
        name = m.group(1)
        params = m.group(2).strip()
        ret = (m.group(3) or "").strip()
        sig = f"fn {name}({params})"
        if ret:
            sig += f" {ret}"
        fns.append({
            "name": name,
            "signature": sig,
            "file": rel_path,
        })
    return fns


def _extract_impls(content: str, rel_path: str) -> list[dict]:
    """提取 impl 块（含 trait 和关联函数名）。"""
    impls = []
    for m in _RE_IMPL.finditer(content):
        type_name = m.group(2)
        # 找关联函数
        inner_fns = []
        for fm in _RE_FN_SIG.finditer(content, m.end()):
            inner_fns.append(fm.group(1))
        impls.append({
            "type": type_name,
            "has_trait": bool(m.group(1) and " for " in content),
            "fns": inner_fns[:30],  # 限制数量
            "file": rel_path,
        })
    return impls


# ── Python Scanner ──


def _scan_python(root: Path) -> dict:
    result: dict = {
        "files": [],
        "classes": [],
        "fns": [],
        "deps": [],
    }

    # pyproject.toml
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
            result["project_name"] = data.get("project", {}).get("name", "")
            result["deps"] = [
                {"name": d} for d in data.get("project", {}).get("dependencies", [])
            ]
        except Exception:
            pass

    for py_file in sorted(root.rglob("*.py")):
        if any(p.startswith(".") or p in ("__pycache__",) for p in py_file.parts):
            continue
        rel = str(py_file.relative_to(root))
        result["files"].append(rel)
        content = py_file.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'^\s*(?:class)\s+(\w+)', content, re.MULTILINE):
            result["classes"].append({"name": m.group(1), "file": rel})
        for m in re.finditer(r'^\s*(?:async\s+)?def\s+(\w+)', content, re.MULTILINE):
            result["fns"].append({"name": m.group(1), "file": rel})

    return result


# ── C++ Scanner ──


def _scan_cpp(root: Path) -> dict:
    result: dict = {
        "files": [],
        "classes": [],
        "fns": [],
        "deps": [],
    }

    # CMakeLists.txt
    cmake = root / "CMakeLists.txt"
    if cmake.exists():
        result["build_system"] = "cmake"

    for ext in ("*.hpp", "*.h", "*.cpp", "*.cc", "*.cxx"):
        for cpp_file in sorted(root.rglob(ext)):
            if "target" in cpp_file.parts or "build" in cpp_file.parts:
                continue
            rel = str(cpp_file.relative_to(root))
            result["files"].append(rel)
            content = cpp_file.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r'^\s*(?:class)\s+(\w+)', content, re.MULTILINE):
                result["classes"].append({"name": m.group(1), "file": rel})

    return result


# ── 注册内置 scanner ──

register_scanner("rust", _scan_rust)
register_scanner("python", _scan_python)
register_scanner("cpp", _scan_cpp)
