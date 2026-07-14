"""Skill 加载器。

Skill = 一块领域专长 markdown（方法论 + 工作流 + 解读框架），放在 ``apex/prompts/skills/``。
文件带 frontmatter（``name`` / ``description`` / ``applies_to``）。本模块按 agent 过滤后把正文
拼成一段字符串，供 analyze / chat 注入到各自的 system prompt。

设计原则（参见 ADR：先沉淀 skill 内容，再做渐进披露）：
- **无 model-invoked load_skill 工具。** phase 1 skill 数量少，全量拼接进 prompt。
  skill 变厚/变多后再上 ``load_skill`` 工具做按需加载。
- **纯文件拼接，零副作用。** 解析失败的单个文件跳过，不影响其它 skill。
- **applies_to 必填。** 没声明 applies_to 的文件被忽略（必须显式指定 analyze / chat）。
"""
from __future__ import annotations

import re
from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parent / "prompts" / "skills"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 ---  ---  围起来的 frontmatter，返回 (字段 dict, 正文)。

    仅支持扁平 key: value（含 applies_to: [a, b] 列表语法）。够用且不引依赖。
    无 frontmatter -> 返回 ({}, text)。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fields[key.strip()] = val.strip()
    return fields, m.group(2)


def _parse_applies_to(val: str) -> list[str]:
    """``[analyze, chat]`` -> ``["analyze", "chat"]``。"""
    cleaned = val.strip().strip("[]").replace('"', "").replace("'", "")
    return [x.strip() for x in cleaned.split(",") if x.strip()]


def load_skills_for(agent: str) -> str:
    """拼接所有 ``applies_to`` 含 ``agent`` 的 skill 正文。

    无 skill / 目录不存在 / 全部不匹配 -> 返回空串（调用方拼 prompt 时自然无影响）。
    """
    if not _SKILLS_DIR.is_dir():
        return ""

    chunks: list[str] = []
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fields, body = _parse_frontmatter(text)
        applies_to = _parse_applies_to(fields.get("applies_to", ""))
        if agent not in applies_to:
            continue
        name = fields.get("name", path.stem)
        chunks.append(f"\n\n## Skill: {name}\n{body.rstrip()}")

    return "".join(chunks)
