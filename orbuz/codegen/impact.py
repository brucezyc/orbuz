"""
ImpactAnalyzer — 跨文件影响分析
======================================
给定一个修改的文件 → 分析其依赖关系 → 列出可能受影响的文件。

通用设计:
  - 对每个语言定义 import/include 模式
  - 构建文件级依赖图
  - 给定起点文件 → BFS 搜索上游依赖

用法:
    analyzer = ImpactAnalyzer("/path/to/project", language="rust")
    affected = analyzer.get_affected("src/pricers/swap.rs")
    # [{"file": "src/pricers/fra.rs", "reason": "uses SwapPricer"}, ...]
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ImpactResult:
    """影响分析结果。"""
    target_file: str
    directly_affected: list[dict] = field(default_factory=list)
    transitively_affected: list[dict] = field(default_factory=list)
    total_affected: int = 0
    dependency_graph_edges: int = 0

    @property
    def all_affected(self) -> list[dict]:
        return self.directly_affected + self.transitively_affected

    def summary(self) -> str:
        parts = [f"文件 {self.target_file} 的变更影响:"]
        if self.directly_affected:
            parts.append(f"  直接依赖者 ({len(self.directly_affected)}):")
            for af in self.directly_affected[:10]:
                parts.append(f"    - {af['file']} ({af['reason']})")
            if len(self.directly_affected) > 10:
                parts.append(f"    ... 还有 {len(self.directly_affected) - 10} 个")
        if self.transitively_affected:
            parts.append(f"  传递影响 ({len(self.transitively_affected)}):")
            for af in self.transitively_affected[:5]:
                parts.append(f"    - {af['file']} ({af['reason']})")
            if len(self.transitively_affected) > 5:
                parts.append(f"    ... 还有 {len(self.transitively_affected) - 5} 个")
        if not self.directly_affected and not self.transitively_affected:
            parts.append("  无直接影响（可能是孤立的模块）")
        return "\n".join(parts)


# ── Import 解析器注册表 ──

ImportParser = Callable[[str], list[tuple[str, str]]]
"""解析文件内容 → [(导入的模块名, 导入语句原文)]"""

_IMPORT_PARSERS: dict[str, ImportParser] = {}


def register_import_parser(language: str, parser: ImportParser):
    _IMPORT_PARSERS[language] = parser


# ── 语言 Import 解析器 ──

def _parse_imports_rust(content: str) -> list[tuple[str, str]]:
    """解析 Rust 的 use/extern crate 语句。"""
    imports = []

    # use crate::xxx::yyy
    for m in re.finditer(r'^\s*use\s+(crate|super|self)::([^;]+);', content, re.MULTILINE):
        imports.append((m.group(2).strip(), m.group(0).strip()))

    # use module::xxx
    for m in re.finditer(r'^\s*use\s+([a-zA-Z_][a-zA-Z0-9_:]*)(?:\s+as\s+\w+)?;', content, re.MULTILINE):
        imports.append((m.group(1).strip(), m.group(0).strip()))

    # mod xxx
    for m in re.finditer(r'^\s*(?:pub\s+)?mod\s+(\w+)\s*(?:;|\{)', content, re.MULTILINE):
        imports.append((m.group(1).strip(), m.group(0).strip()))

    return imports


def _parse_imports_python(content: str) -> list[tuple[str, str]]:
    """解析 Python 的 import/from 语句。"""
    imports = []
    # import xxx
    for m in re.finditer(r'^\s*import\s+(\S+)', content, re.MULTILINE):
        imports.append((m.group(1).strip(), m.group(0).strip()))

    # from xxx import yyy
    for m in re.finditer(r'^\s*from\s+(\S+)\s+import', content, re.MULTILINE):
        imports.append((m.group(1).strip(), m.group(0).strip()))

    return imports


def _parse_imports_cpp(content: str) -> list[tuple[str, str]]:
    """解析 C++ 的 #include 语句。"""
    imports = []
    for m in re.finditer(r'#include\s+[<"]([^>"]+)[>"]', content):
        imports.append((m.group(1).strip(), m.group(0).strip()))
    return imports


register_import_parser("rust", _parse_imports_rust)
register_import_parser("python", _parse_imports_python)
register_import_parser("cpp", _parse_imports_cpp)


# ── 主类 ──

class ImpactAnalyzer:
    """
    跨文件影响分析。

    用法:
        analyzer = ImpactAnalyzer("/path/to/project", language="rust")
        result = analyzer.get_affected("src/pricers/swap.rs")
        print(result.summary())
    """

    def __init__(
        self,
        project_dir: str,
        language: str | None = None,
        file_extensions: list[str] | None = None,
    ):
        self.root = Path(project_dir).expanduser().resolve()
        self.language = language
        self._parser = _IMPORT_PARSERS.get(language or "") if language else None

        # 文件扩展名映射
        _ext_map = {
            "rust": [".rs"],
            "python": [".py"],
            "cpp": [".cpp", ".hpp", ".h", ".cc", ".cxx"],
        }
        self._extensions = file_extensions or _ext_map.get(language or "", [".py", ".rs", ".cpp", ".h"])

        # 缓存: 文件路径 → [依赖文件路径]
        self._dep_cache: dict[str, set[str]] = {}
        self._reverse_dep_cache: dict[str, set[str]] | None = None

    def get_affected(self, target_file: str) -> ImpactResult:
        """
        分析修改 target_file 会影响哪些文件。

        target_file: 相对于 project_dir 的文件路径
        """
        target = target_file.replace("\\", "/")
        # 构建依赖图（如果尚未缓存）
        self._build_dep_graph()

        # 查找直接反向依赖
        if not self._reverse_dep_cache:
            self._reverse_dep_cache = {}
            for dep_file, deps in self._dep_cache.items():
                for dep in deps:
                    if dep not in self._reverse_dep_cache:
                        self._reverse_dep_cache[dep] = set()
                    self._reverse_dep_cache[dep].add(dep_file)

        # 直接依赖者
        direct = list(self._reverse_dep_cache.get(target, set()))
        directly_affected = [
            {"file": f, "reason": _find_import_reason(f, target, self.root)}
            for f in sorted(direct)[:30]
        ]

        # BFS 找传递影响（最多 2 层）
        transitively_affected = []
        seen = set(direct)
        seen.add(target)
        frontier = list(direct)

        for _ in range(2):  # 2 层传递
            next_frontier = []
            for f in frontier:
                rdeps = self._reverse_dep_cache.get(f, set())
                for rdep in rdeps:
                    if rdep not in seen:
                        seen.add(rdep)
                        next_frontier.append(rdep)
                        transitively_affected.append({
                            "file": rdep,
                            "reason": f"通过 {Path(f).name} 传递依赖",
                        })
            frontier = next_frontier
            if not frontier:
                break

        return ImpactResult(
            target_file=target,
            directly_affected=directly_affected,
            transitively_affected=transitively_affected,
            total_affected=len(direct) + len(transitively_affected),
            dependency_graph_edges=sum(len(d) for d in self._dep_cache.values()),
        )

    def _build_dep_graph(self):
        """递归扫描所有源文件，构建依赖图。"""
        if self._dep_cache:
            return

        for fpath in self.root.rglob("*"):
            if fpath.suffix not in self._extensions:
                continue
            if any(p.startswith(".") or p in ("target", "node_modules", "__pycache__", "build")
                   for p in fpath.parts):
                continue

            rel = str(fpath.relative_to(self.root)).replace("\\", "/")
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            deps = set()
            if self._parser:
                for imported_name, _ in self._parser(content):
                    resolved = self._resolve_import(imported_name, fpath, rel)
                    if resolved:
                        deps.add(resolved)

            if deps:
                self._dep_cache[rel] = deps
            else:
                self._dep_cache[rel] = set()

    def _resolve_import(self, imported: str, abs_path: Path, rel_path: str) -> str | None:
        """
        解析 import 语句 → 项目内的具体文件路径。

        如 use crate::pricers::swap → src/pricers/swap.rs
        """
        # Rust crate 路径: crate::module::Item → src/module.rs 或 src/module/mod.rs
        if self.language == "rust":
            # 去掉最后的 ::Item 部分
            parts = imported.split("::")
            # 去掉泛型参数
            parts = [p.split("<")[0] for p in parts]

            # 尝试 crate::path
            if parts[0] == "crate":
                rel_parts = parts[1:]
            else:
                rel_parts = parts

            if not rel_parts:
                return None

            # 尝试 mod.rs 或 mod/mod.rs 或 mod_path.rs
            candidates = []
            mod_path = "/".join(rel_parts)
            candidates.append(self.root / "src" / f"{mod_path}.rs")
            candidates.append(self.root / "src" / mod_path / "mod.rs")

            # 如果不是从 src/ 开始，尝试直接路径
            if not mod_path.startswith("src/"):
                candidates.append(self.root / f"{mod_path}.rs")
                candidates.append(self.root / mod_path / "mod.rs")

            for cand in candidates:
                if cand.exists():
                    try:
                        return str(cand.relative_to(self.root)).replace("\\", "/")
                    except ValueError:
                        return str(cand)

        # Python: from module.submodule import X → module/submodule.py
        if self.language == "python":
            mod_path = imported.replace(".", "/")
            candidates = [
                self.root / f"{mod_path}.py",
                self.root / mod_path / "__init__.py",
            ]
            for cand in candidates:
                if cand.exists():
                    try:
                        return str(cand.relative_to(self.root)).replace("\\", "/")
                    except ValueError:
                        return str(cand)

        # C++: #include "path/to/file.hpp" → path/to/file.hpp
        if self.language == "cpp":
            # 优先项目内相对路径
            cand = self.root / imported
            if cand.exists():
                try:
                    return str(cand.relative_to(self.root)).replace("\\", "/")
                except ValueError:
                    return str(cand)
            # 模糊匹配文件名
            fname = Path(imported).name
            for found in self.root.rglob(fname):
                if found.is_file():
                    try:
                        return str(found.relative_to(self.root)).replace("\\", "/")
                    except ValueError:
                        return str(found)

        return None


def _find_import_reason(file: str, target: str, root: Path) -> str:
    """查找 file 为什么要依赖 target。"""
    try:
        path = root / file
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "import 依赖"

    target_name = Path(target).stem
    for parser_name, parser in _IMPORT_PARSERS.items():
        if file.endswith(tuple({
            "rust": ".rs",
            "python": ".py",
            "cpp": (".cpp", ".hpp", ".h"),
        }.get(parser_name, []))):
            for imported, line in parser(content):
                if target_name in imported or target_name in line:
                    return f"通过 `{line[:80]}`"
    return "依赖关系"
