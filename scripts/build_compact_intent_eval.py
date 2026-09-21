#!/usr/bin/env python3
"""Build an evaluation copy that uses a compact intent-router system prompt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


COMPACT_SYSTEM_PROMPT = """你是意图路由器，只输出一个紧凑JSON，禁止解释、Markdown、思维过程或重复输出。
输入含query、history、visible_tools。decision仅允许direct_answer、clarify、tool_plan、abstain：普通问答用direct_answer；明确使用可见工具但缺required字段用clarify；可见工具能完成且required齐全用tool_plan；需要外部能力但无匹配工具或越权危险用abstain。
工具名必须来自visible_tools。arguments和missing_slots只能使用目标工具parameters.properties中的真实字段。clarify必须输出candidate_tool、缺少的required字段和空actions；其他decision的candidate_tool为null且missing_slots为空。可选字段仅在用户明确提供或Schema声明default时输出。依赖上一步真实返回值时只规划当前可执行步骤。
输出结构：{"decision":"tool_plan","candidate_tool":null,"actions":[{"tool":"工具名","arguments":{}}],"missing_slots":[]}。必须完整且只输出一次，96 token内。
/no_think"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.input.open(encoding="utf-8") as source, args.output.open(
        "w", encoding="utf-8"
    ) as destination:
        for line in source:
            row = json.loads(line)
            if not row.get("messages") or row["messages"][0].get("role") != "system":
                raise ValueError("each row must begin with a system message")
            row["messages"][0]["content"] = COMPACT_SYSTEM_PROMPT
            destination.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
