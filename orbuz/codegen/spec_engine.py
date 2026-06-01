"""
SpecEngine — Spec 驱动的多文件生成
==========================================
YAML spec → 自动推导受影响文件列表 → 逐个生成/更新。

通用设计:
  - Spec 格式语言无关（描述变更意图，而非代码）
  - 内置模板渲染（简单字符串替换 + 可选的 Jinja2）
  - 依赖项目上下文（ProjectContext）避免重复生成

用法:
    engine = SpecEngine(project_dir="/path/to/project")
    plan = engine.parse_spec("spec.yaml")
    # plan = {
    #   "action": "add_method",
    #   "target": "IborIndex",
    #   "files": ["src/types/ibor.rs", "src/pricers/swap.rs", ...],
    #   "prompts": {file: "生成的 prompt 上下文", ...},
    # }
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orbuz.codegen.project_context import build_project_context


# ── Spec 结构 ──

@dataclass
class SpecAction:
    """单个 spec 动作。"""
    action: str  # "add_type", "add_method", "modify", "add_test", "refactor"
    target: str  # struct/trait/module 名
    name: str  # 新方法/类型名
    signature: str = ""  # 方法签名/类型定义
    description: str = ""  # 自然语言描述
    files: list[str] = field(default_factory=list)  # 显式指定的文件（可选）
    affected_traits: list[str] = field(default_factory=list)
    affected_types: list[str] = field(default_factory=list)
    test_count: int = 1  # 测试用例数


@dataclass
class SpecPlan:
    """解析后的执行计划。"""
    title: str
    description: str
    actions: list[SpecAction] = field(default_factory=list)
    all_files: list[str] = field(default_factory=list)
    per_file_prompts: dict[str, str] = field(default_factory=dict)
    requires_oracle: bool = False
    oracle_command: str = ""


def parse_spec(spec_path: str) -> SpecPlan:
    """
    解析 YAML spec 文件 → SpecPlan。

    YAML 格式示例:
        title: "IborIndex 添加 fixing_date 方法"
        description: "在 IborIndex trait 加新方法并补齐所有 impl"
        actions:
          - action: add_method
            target: IborIndex
            name: fixing_date
            signature: "fn fixing_date(&self, spot: Date) -> Date"
            description: "计算给定起息日的 fixing date"
            affected_traits: []
            affected_types: [BuiltinIndex, CustomIndex]
        requires_oracle: true
        oracle_command: "cargo test --test ibor_tests -- --exact fixing_date"
    """
    path = Path(spec_path).expanduser()
    if not path.exists():
        return SpecPlan(title="ERROR", description=f"Spec 文件不存在: {spec_path}")

    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        return SpecPlan(title="ERROR", description=f"Spec 解析失败: {e}")

    if not data or "actions" not in data:
        return SpecPlan(title="ERROR", description="Spec 缺少 'actions' 字段")

    actions = []
    for a in data["actions"]:
        actions.append(SpecAction(
            action=a.get("action", "modify"),
            target=a.get("target", ""),
            name=a.get("name", ""),
            signature=a.get("signature", ""),
            description=a.get("description", ""),
            files=a.get("files", []),
            affected_traits=a.get("affected_traits", []),
            affected_types=a.get("affected_types", []),
            test_count=a.get("test_count", 1),
        ))

    plan = SpecPlan(
        title=data.get("title", "无标题"),
        description=data.get("description", ""),
        actions=actions,
        requires_oracle=data.get("requires_oracle", False),
        oracle_command=data.get("oracle_command", ""),
    )

    return plan


# ── 从 spec → 文件列表 + 生成 prompt ──

class SpecEngine:
    """
    Spec 驱动的生成引擎。

    用法:
        engine = SpecEngine(
            project_dir="/path/to/rustpricer",
            context=build_project_context("/path/to/rustpricer"),
        )
        plan = engine.parse_spec("specs/add_fixing_date.yaml")
        for file_path, prompt in plan.per_file_prompts.items():
            code = llm_call(prompt)
            write_file(file_path, code)
    """

    def __init__(
        self,
        project_dir: str,
        context: dict | None = None,
    ):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.context = context or build_project_context(str(self.project_dir))

    def parse_spec(self, spec_path: str) -> SpecPlan:
        """加载 spec → 推导文件列表 → 生成 per-file prompt。"""
        plan = parse_spec(spec_path)
        if plan.title == "ERROR":
            return plan

        # 合并所有文件
        all_files_set: set[str] = set()
        for action in plan.actions:
            for f in action.files:
                all_files_set.add(f)

            # 根据 action 类型推导额外的文件
            inferred = self._infer_files(action)
            for f in inferred:
                all_files_set.add(f)

        plan.all_files = sorted(all_files_set)

        # 生成每个文件的生成 prompt
        for file_path in plan.all_files:
            prompt = self._build_file_prompt(file_path, plan)
            plan.per_file_prompts[file_path] = prompt

        return plan

    def _infer_files(self, action: SpecAction) -> list[str]:
        """从 action 推导可能受影响的文件（使用 ProjectContext）。"""
        inferred = []
        files = self.context.get("files", [])

        if action.action == "add_type":
            # 新类型通常在 types/ 命名约定下
            type_name = action.name.lower()
            for f in files:
                if "types" in f or "schema" in f:
                    inferred.append(f)

        elif action.action == "add_method":
            # 向 trait 加方法，需要更新 impl 和 pricer
            for trait in action.affected_traits:
                trait_lower = trait.lower()
                for f in files:
                    if trait_lower in f.lower():
                        # 找到 trait 定义文件
                        inferred.append(f)
            for typ in action.affected_types:
                typ_lower = typ.lower()
                for f in files:
                    if typ_lower in f.lower():
                        inferred.append(f)

            # 更新 pricer 文件
            pricer_files = [f for f in files if "pricer" in f.lower()]
            inferred.extend(pricer_files[:3])

        elif action.action == "add_test":
            # 在 tests/ 或 *_test.rs 添加测试
            test_files = [f for f in files if "test" in f.lower() or f.endswith("_test.rs")]
            if test_files:
                inferred.append(test_files[0])
            else:
                # 看看有没有 tests/ 目录
                tests_dir = self.project_dir / "tests"
                if tests_dir.exists():
                    inferred.append("tests/" + action.name.replace(" ", "_").lower() + ".rs")

        return inferred[:5]  # 最多 5 个

    def _build_file_prompt(self, file_path: str, plan: SpecPlan) -> str:
        """为单个文件构建生成 prompt。"""
        project_summary = self.context.get("summary", "")
        project_files = "\n".join(self.context.get("files", []))

        # spec 摘要
        actions_desc = []
        for a in plan.actions:
            actions_desc.append(f"- [{a.action}] {a.name}: {a.description}")
        spec_desc = "\n".join(actions_desc)

        # 项目知识注入
        knowledge_block = ""
        knowledge = self.context.get("knowledge", {})
        if knowledge:
            knowledge_block = (
                f"\n\n### 项目架构规则（必须遵守）\n"
                f"{knowledge['content']}"
            )

        prompt = (
            f"## 目标\n"
            f"根据以下 spec 更新文件 `{file_path}`:\n\n"
            f"### Spec 描述\n"
            f"{plan.description}\n\n"
            f"### 具体动作\n"
            f"{spec_desc}\n\n"
            f"### 项目上下文\n"
            f"{project_summary}\n\n"
            f"### 项目文件结构\n"
            f"```\n{project_files}\n```"
            f"{knowledge_block}\n\n"
            f"## 要求\n"
            f"1. 只修改/生成 `{file_path}` 的内容\n"
            f"2. 保持项目中其他文件的兼容性\n"
            f"3. 遵循项目现有代码风格\n"
            f"4. 返回完整的文件内容（包含已存在的代码）\n"
            f"5. Rust 代码使用 pub 导出必要的类型和方法\n"
            f"6. 添加必要的 #[cfg(test)] 或 test module\n"
        )

        return prompt
