# -*- coding: utf-8 -*-
"""星辰科技企业知识库客服 HTTP 接口。

进程启动时连接 RAG 并创建一次 Agent，之后所有请求复用该实例。

依赖：
    pip install fastapi uvicorn
    以及 main_graph.py 已有依赖

环境变量：
    DEEPSEEK_API_KEY   必填
    DEEPSEEK_MODEL     可选，默认 deepseek-chat

运行：
    python api_server.py
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

from main_graph import (
    SERVER_NAME,
    SERVER_PATH,
    SYSTEM_PROMPT,
    build_client,
    build_model,
    load_local_env,
    message_text,
    print_retrievals,
)

_agent = None
_client = None
# 全局 Agent 共用一条 MCP stdio 会话，同一时刻只跑一轮。
_invoke_lock = asyncio.Lock()


class ChatRequest(BaseModel):
    question: str
    thread_id: str = "cli"


class ChatResponse(BaseModel):
    answer: str
    status: str


def _payload(answer: str, status: str, code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=code,
        content={"answer": answer, "status": status},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _client
    load_local_env(ROOT / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not SERVER_PATH.is_file():
        raise RuntimeError(f"找不到 RAG 服务：{SERVER_PATH}")

    model = build_model()
    _client = build_client()
    print("正在连接企业知识库...")
    async with _client.session(SERVER_NAME) as session:
        tools = await load_mcp_tools(session)
        if not tools:
            raise RuntimeError("RAG 服务没有暴露任何工具。")
        _agent = create_react_agent(
            model,
            tools,
            prompt=SYSTEM_PROMPT,
            checkpointer=MemorySaver(),
        )
        names = "、".join(tool.name for tool in tools)
        print(f"已连接知识库，可用工具：{names}")
        try:
            yield
        finally:
            _agent = None


app = FastAPI(title="星辰科技知识库客服", lifespan=lifespan)


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> JSONResponse:
    question = body.question.strip()
    if not question:
        return _payload("question 不能为空", "error", 400)
    if _agent is None:
        return _payload("Agent 尚未就绪", "error", 503)

    thread_id = (body.thread_id or "cli").strip() or "cli"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 15}

    async with _invoke_lock:
        snapshot = await _agent.aget_state(config)
        start = len(snapshot.values.get("messages", [])) if snapshot.values else 0
        try:
            result = await _agent.ainvoke(
                {"messages": [("user", question)]},
                config=config,
            )
        except GraphRecursionError:
            return _payload("连续检索次数过多，请把问题说得更具体一些。", "success")
        except Exception as exc:
            return _payload(f"本轮出错：{exc}", "error", 500)

        messages = result["messages"]
        print_retrievals(messages, start)
        answer = message_text(messages[-1]) if messages else ""
        return _payload(answer or "这次没有生成回答，请换个说法再试一次。", "success")


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
