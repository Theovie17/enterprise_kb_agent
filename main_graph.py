# -*- coding: utf-8 -*-
"""星辰科技企业知识库客服。

ReAct 循环：用户提问 -> 调用 rag_server.search_company_docs -> 依据检索结果回答。

依赖：
    pip install langgraph langchain-mcp-adapters langchain-deepseek

环境变量：
    DEEPSEEK_API_KEY   必填
    DEEPSEEK_MODEL     可选，默认 deepseek-chat

运行：
    python main_graph.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from langchain_deepseek import ChatDeepSeek
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

SERVER_PATH = Path(__file__).resolve().parent / "rag_server.py"
SERVER_NAME = "company_kb"
THREAD_ID = "cli"

SYSTEM_PROMPT = """你是星辰科技的企业内部知识库客服。员工会咨询请假、考勤、报销、差旅、发票、VPN、打印机、电脑申领、CRM 和数据安全合规。

规则：
1. 涉及公司制度、流程、标准或产品用法的问题，必须先调用 search_company_docs，再根据工具返回的内容回答。
2. 只使用工具返回的信息。不要编造假期天数、报销金额、职级标准或审批流程。
3. 先给结论，再写依据。依据写成「章节路径（来源文件）」。
4. 几条结果不一致时，优先采用更具体的条款或 FAQ，并同时说明另一条来源。
5. 工具没有相关内容时，明确说知识库中没有找到，不要用常识补全公司规定。
6. 与公司知识无关的闲聊可以简短回应，并说明你主要负责企业内部制度咨询。
7. 使用简体中文。"""


def load_local_env(path: Path) -> None:
    """读取脚本目录下的 .env。已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def message_text(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip()


def print_retrievals(messages, start: int) -> None:
    for message in messages[start:]:
        for call in getattr(message, "tool_calls", None) or []:
            args = call.get("args") or {}
            query = args.get("query") if isinstance(args, dict) else args
            print(f"（检索知识库：{query}）")


def build_model() -> ChatDeepSeek:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("请先设置环境变量 DEEPSEEK_API_KEY。", file=sys.stderr)
        raise SystemExit(1)
    return ChatDeepSeek(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        api_key=api_key,
        temperature=0,
        max_tokens=2048,
        timeout=60,
    )


def build_client() -> MultiServerMCPClient:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return MultiServerMCPClient(
        {
            SERVER_NAME: {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(SERVER_PATH)],
                "cwd": str(SERVER_PATH.parent),
                "env": env,
            }
        }
    )


async def ask(agent, question: str) -> None:
    config = {"configurable": {"thread_id": THREAD_ID}, "recursion_limit": 15}
    snapshot = await agent.aget_state(config)
    start = len(snapshot.values.get("messages", [])) if snapshot.values else 0
    try:
        result = await agent.ainvoke({"messages": [("user", question)]}, config=config)
    except GraphRecursionError:
        print("客服：连续检索次数过多，请把问题说得更具体一些。")
        return
    messages = result["messages"]
    print_retrievals(messages, start)
    answer = message_text(messages[-1]) if messages else ""
    print("客服：" + (answer or "这次没有生成回答，请换个说法再试一次。"))


async def chat() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if not SERVER_PATH.is_file():
        print(f"找不到 RAG 服务：{SERVER_PATH}", file=sys.stderr)
        raise SystemExit(1)

    model = build_model()
    client = build_client()
    print("正在连接企业知识库...")
    async with client.session(SERVER_NAME) as session:
        tools = await load_mcp_tools(session)
        if not tools:
            print("RAG 服务没有暴露任何工具。", file=sys.stderr)
            raise SystemExit(1)
        agent = create_react_agent(
            model,
            tools,
            prompt=SYSTEM_PROMPT,
            checkpointer=MemorySaver(),
        )
        names = "、".join(tool.name for tool in tools)
        print(f"已连接知识库，可用工具：{names}")
        print("直接输入问题。输入 exit 或 退出 结束。")
        while True:
            try:
                question = (await asyncio.to_thread(input, "你：")).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                break
            if not question:
                continue
            if question.lower() in {"exit", "quit", "q"} or question in {"退出", "再见"}:
                print("再见。")
                break
            try:
                await ask(agent, question)
            except KeyboardInterrupt:
                print("\n本轮已取消。")
            except Exception as exc:
                print(f"客服：本轮出错：{exc}")


def main() -> None:
    load_local_env(Path(__file__).resolve().parent / ".env")
    asyncio.run(chat())


if __name__ == "__main__":
    main()
