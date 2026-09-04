# ═══════════════════════════════════════════════════════════════════
# 提示词管理器 v2：YAML 结构化存储 + 字段字典自动注入 + 版本化
#
# 文件结构：
#   prompts/
#     field_dict.json        ← 枚举/字段字典（所有提示词共享）
#     v1/prompts.yaml        ← 该版本的所有提示词（结构化 YAML）
#     v2/prompts.yaml        ← 新版本覆盖/新增
#     v3/prompts.yaml
#
# YAML 中每个 prompt 条目字段：
#   name, version, description, system_prompt, user_prompt_template, variables
#
# 变量占位符格式：Python str.format() 的 {variable}
#   - 业务变量由调用者传入（如 {contract_text}, {contract_type}）
#   - field_dict.json 的枚举自动注入为 {field_dict.contract_type.values} 等
#
# 使用示例：
#   pm = PromptManager(prompts_dir="./prompts", current_version="v1")
#   pm.render("classify", contract_text="...", contract_title="xxx")
#     → {"system": "...", "user": "..."}   # 已完成变量替换
#   pm.load("classify")                    # 向后兼容
#     → "..."                               # 只返回 user_prompt_template 原始文本
# ═══════════════════════════════════════════════════════════════════
import json
import logging
import re
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("prompt-manager")


class PromptManager:
    """提示词版本化管理器（YAML + 字段字典 + 变量替换）"""

    # 占位符匹配：{xxx}，支持点号路径 {field_dict.contract_type.values}
    _PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")

    def __init__(
        self,
        prompts_dir: str = "./prompts",
        current_version: Optional[str] = None,
        field_dict_filename: str = "field_dict.json",
    ):
        self.base_dir = Path(prompts_dir)
        self.field_dict_path = self.base_dir / field_dict_filename

        # 缓存：版本号 → 该版本所有 prompt dict
        self._cache: dict[str, list[dict]] = {}

        # 加载字段字典（供 {field_dict.xxx} 占位符使用）
        self.field_dict = self._load_field_dict()

        # 自动探测版本目录
        self.available_versions = self._detect_versions()
        self.current_version = current_version or (
            self.available_versions[-1] if self.available_versions else "v1"
        )

        logger.info(
            f"提示词管理器初始化 | 目录: {self.base_dir} | "
            f"版本: {self.current_version} | "
            f"可用版本: {self.available_versions} | "
            f"field_dict 键数: {len(self.field_dict)}"
        )

    # ═════════════════════════════════════════════════════════════
    # 内部辅助
    # ═════════════════════════════════════════════════════════════

    def _load_field_dict(self) -> dict:
        """加载字段字典 JSON"""
        if not self.field_dict_path.exists():
            logger.warning(f"field_dict.json 不存在（路径: {self.field_dict_path}），跳过字典注入")
            return {}
        try:
            data = json.loads(self.field_dict_path.read_text(encoding="utf-8"))
            logger.info(f"  ✅ 字段字典已加载 | 键: {list(data.keys())}")
            return data
        except Exception as e:
            logger.error(f"field_dict.json 解析失败: {e}")
            return {}

    def _detect_versions(self) -> list[str]:
        """扫描 base_dir 下哪些版本目录有 prompts.yaml"""
        if not self.base_dir.exists():
            return []
        versions = []
        for d in sorted(self.base_dir.iterdir()):
            if d.is_dir() and (d / "prompts.yaml").exists():
                versions.append(d.name)
        return versions

    def _get_version_prompts(self, version: str) -> list[dict]:
        """读取并缓存指定版本的 prompts.yaml"""
        if version in self._cache:
            return self._cache[version]

        yaml_path = self.base_dir / version / "prompts.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(
                f"提示词文件不存在: {yaml_path}（版本 {version}）"
            )

        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            logger.error(f"YAML 解析失败 [{version}]: {e}")
            raise

        prompts = raw.get("prompts", [])
        self._cache[version] = prompts
        logger.debug(f"加载版本 [{version}] 共 {len(prompts)} 个提示词")
        return prompts

    def _lookup_prompt(self, name: str, version: Optional[str] = None) -> dict:
        """在指定版本（默认为 current_version）中按 name 查找 prompt 条目"""
        ver = version or self.current_version
        prompts = self._get_version_prompts(ver)
        for p in prompts:
            if p.get("name") == name:
                return p
        raise KeyError(
            f"未找到 name='{name}' 的提示词（版本 {ver}）。"
            f"可用提示词: {[p['name'] for p in prompts]}"
        )

    def _resolve_placeholder(self, key: str) -> Optional[str]:
        """
        解析占位符 key → 实际值。
        支持点号路径：field_dict.contract_type.values → field_dict["contract_type"]["values"]
        找不到返回 None（跳过替换，保留原样）。
        """
        parts = key.split(".")
        obj: any = None
        if parts[0] == "field_dict":
            obj = self.field_dict
            parts = parts[1:]
        else:
            # 业务变量会在 render() 中先传入，这里不处理
            return None

        for p in parts:
            if isinstance(obj, dict) and p in obj:
                obj = obj[p]
            else:
                return None

        # dict/list → JSON 字符串，str → 原样
        if isinstance(obj, (dict, list)):
            return json.dumps(obj, ensure_ascii=False)
        return str(obj)

    def _resolve_variables(self, text: str, user_vars: dict) -> str:
        """
        将 text 中所有 {xxx} 占位符替换为实际值。
        优先级：user_vars（业务变量） > field_dict（枚举字典）。
        两种来源都找不到的占位符 → 保留原样（不抛异常）。
        """
        def _sub(match: re.Match) -> str:
            key = match.group(1)
            # 先查业务变量
            if key in user_vars and user_vars[key] is not None:
                return str(user_vars[key])
            # 再查 field_dict（支持点号路径）
            val = self._resolve_placeholder(key)
            if val is not None:
                return val
            # 都找不到 → 保留原样
            logger.warning(f"占位符未解析，保留原样: {{{key}}}")
            return match.group(0)

        return self._PLACEHOLDER_RE.sub(_sub, text)

    # ═════════════════════════════════════════════════════════════
    # Schema 工具（单一数据源：field_dict.json extract_schema.fields）
    # ═════════════════════════════════════════════════════════════

    def format_schema_text(self, schema_name: str = "extract_schema") -> str:
        """
        从 field_dict.json 的 schema 定义生成人类可读的 JSON 示例文本。

        单一数据源：field_dict[schema_name]["fields"]
        输出风格：{ "field_name": "中文描述（类型, 必填/可选）", ... }

        支持 enum refs：若字段的 "values" 是 "xxx.yyy" 点号路径，
        会自动解析为 field_dict["xxx"]["yyy"] 的实际值列表。

        Args:
            schema_name: field_dict.json 中的顶级键名，默认 "extract_schema"

        Returns:
            格式化好的 JSON 风格字符串块（含缩进），可直接注入 prompt
        """
        schema = self.field_dict.get(schema_name, {})
        fields = schema.get("fields", {})
        if not fields:
            logger.warning(f"format_schema_text: field_dict['{schema_name}']['fields'] 为空或不存在")
            return "{ }"

        lines = ["{"]
        field_items = list(fields.items())
        for i, (fname, fdef) in enumerate(field_items):
            desc = fdef.get("desc", "")
            ftype = fdef.get("type", "string")
            required = fdef.get("required", False)
            optional_str = "必填" if required else "可选"

            # 构造值描述（类型 + 枚举值 + required 标记）
            if ftype == "enum":
                # 解析 enum refs：field_dict.contract_type.values → ["采购合同", ...]
                # 用顿号 join 避免嵌套双引号（因为我们的输出是 JSON 风格字符串）
                values = fdef.get("values", [])
                if isinstance(values, str) and "." in values:
                    # 点号路径 → 手动解析成 list → 顿号 join
                    parts = values.split(".")
                    obj = self.field_dict
                    for p in parts:
                        obj = obj.get(p, []) if isinstance(obj, dict) else []
                    if isinstance(obj, list) and obj:
                        enum_str = "、".join(str(v) for v in obj)
                    else:
                        enum_str = values  # 解析失败 fallback
                elif isinstance(values, list):
                    enum_str = "、".join(str(v) for v in values)
                else:
                    enum_str = str(values)
                val_desc = f"{desc}（枚举: {enum_str}, {optional_str}）"
            elif ftype in ("string", "number", "integer", "boolean"):
                val_desc = f"{desc}（{ftype}, {optional_str}）"
            elif ftype == "string|null":
                val_desc = f"{desc}（string|null, {optional_str}）"
            elif ftype == "number|null":
                val_desc = f"{desc}（number|null, {optional_str}）"
            else:
                val_desc = f"{desc}（{ftype}, {optional_str}）"

            comma = "," if i < len(field_items) - 1 else ""
            lines.append(f'    "{fname}": "{val_desc}"{comma}')

        lines.append("}")
        return "\n".join(lines)

    # ═════════════════════════════════════════════════════════════
    # 公开 API
    # ═════════════════════════════════════════════════════════════

    def load(self, name: str, version: Optional[str] = None) -> str:
        """
        向后兼容接口：只返回 user_prompt_template 原始文本（不做变量替换）。
        新代码建议用 render()。
        """
        prompt = self._lookup_prompt(name, version)
        template = prompt.get("user_prompt_template", "")
        logger.debug(f"加载提示词 [{name}]（版本 {version or self.current_version}）")
        return template

    def get_prompt(self, name: str, version: Optional[str] = None) -> dict:
        """返回完整 prompt 条目 dict（包含 system_prompt + user_prompt_template）"""
        return self._lookup_prompt(name, version)

    def render(self, name: str, version: Optional[str] = None, **variables) -> dict:
        """
        加载并渲染一个提示词，完成变量替换。

        Args:
            name:       提示词 name（如 "classify", "extract"）
            version:    指定版本，None 用 current_version
            **variables: 业务变量（contract_text="...", contract_type="..." 等）

        Returns:
            dict: {"system": str, "user": str, "name": str, "version": str}
                  system_prompt 和 user_prompt_template 都已完成变量替换。
                  若 prompt 本身没有 system_prompt（如 extract 是复用 system_extract），
                  system 字段会从 system_extract 自动补齐。
        """
        prompt = self._lookup_prompt(name, version)
        ver = version or self.current_version

        # system_prompt：空的话自动继承 system_extract
        sys_text = prompt.get("system_prompt", "")
        if not sys_text and name != "system_extract":
            try:
                sys_text = self._lookup_prompt("system_extract", ver).get("system_prompt", "")
            except KeyError:
                logger.debug(f"[{name}] 无 system_prompt 且 system_extract 不存在，system 为空")

        user_text = prompt.get("user_prompt_template", "")

        # 替换变量（field_dict 枚举 + 用户传入的业务变量）
        sys_text = self._resolve_variables(sys_text, variables)
        user_text = self._resolve_variables(user_text, variables)

        logger.debug(f"render [{ver}/{name}] variables={list(variables.keys())}")

        return {
            "name": name,
            "version": ver,
            "system": sys_text,
            "user": user_text,
        }

    def list_versions(self) -> list[str]:
        """列出所有已有版本"""
        return self._detect_versions()

    def list_prompts(self, version: Optional[str] = None) -> list[dict]:
        """列出指定版本的所有提示词（不含完整模板，只含元信息）"""
        ver = version or self.current_version
        prompts = self._get_version_prompts(ver)
        return [
            {
                "name": p.get("name"),
                "version": p.get("version"),
                "description": p.get("description", ""),
                "variables": p.get("variables", []),
                "has_system": bool(p.get("system_prompt")),
                "has_user": bool(p.get("user_prompt_template")),
            }
            for p in prompts
        ]

    def rollback(self, version: str):
        """切换到指定版本"""
        if version not in self.available_versions and (self.base_dir / version / "prompts.yaml").exists():
            self.available_versions = self._detect_versions()
        if version not in self.available_versions:
            raise ValueError(
                f"无效版本: {version}。可用: {self.available_versions}"
            )
        self.current_version = version
        logger.info(f"🔄 提示词版本已切换到: {version}")

    def reload(self):
        """强制重新加载所有缓存（YAML 修改后热更新）"""
        self._cache.clear()
        self.field_dict = self._load_field_dict()
        self.available_versions = self._detect_versions()
        logger.info(f"🔄 PromptManager 已重新加载 | 可用版本: {self.available_versions}")
