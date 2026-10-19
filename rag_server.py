# -*- coding: utf-8 -*-
"""企业知识库 RAG MCP Server。

读取同目录的 chunks.json，用 BAAI/bge-small-zh-v1.5 在本地做向量，
写入 chroma_db，并通过 MCP 工具 search_company_docs 返回最相近的 3 个文本块。

依赖：
    pip install mcp langchain-huggingface langchain-chroma
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

# 必须在加载模型和 Chroma 之前设置。MCP 走 stdout，日志只能进 stderr。
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "false")

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings
from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
CHUNKS_PATH = BASE_DIR / "chunks.json"
CHROMA_DIR = BASE_DIR / "chroma_db"
COLLECTION_NAME = "company_docs"
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
TOP_K = 3

mcp = FastMCP("星辰科技知识库")
_store: Chroma | None = None
_lock = threading.Lock()


class BgeZhEmbeddings(Embeddings):
    """文档原样编码；查询加上 bge-zh v1.5 要求的检索前缀。"""

    def __init__(self, inner: HuggingFaceEmbeddings, query_instruction: str):
        self.inner = inner
        self.query_instruction = query_instruction

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents(list(texts))

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(f"{self.query_instruction}{text}")


def build_embeddings() -> BgeZhEmbeddings:
    inner = HuggingFaceEmbeddings(
        model_name=MODEL_NAME,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
        query_encode_kwargs={"normalize_embeddings": True},
        show_progress=False,
    )
    return BgeZhEmbeddings(inner, QUERY_INSTRUCTION)


def chunks_digest() -> str:
    return hashlib.sha256(CHUNKS_PATH.read_bytes()).hexdigest()


def load_documents() -> list[Document]:
    payload = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    documents: list[Document] = []
    seen: set[str] = set()
    for index, item in enumerate(payload, start=1):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        metadata = dict(item.get("metadata") or {})
        clean_meta: dict[str, str | int | float | bool] = {}
        for key, value in metadata.items():
            if isinstance(value, bool) or isinstance(value, (int, float, str)):
                clean_meta[key] = value
            elif value is not None:
                clean_meta[key] = str(value)
        chunk_id = str(clean_meta.get("chunk_id") or f"chunk-{index}")
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        clean_meta["chunk_id"] = chunk_id
        documents.append(Document(page_content=text, metadata=clean_meta))
    return documents


def collection_state(store: Chroma) -> tuple[str, int]:
    collection = store._collection
    metadata = collection.metadata or {}
    digest = str(metadata.get("chunks_sha256") or "")
    return digest, int(collection.count())


def open_store() -> Chroma:
    if not CHUNKS_PATH.is_file():
        raise FileNotFoundError(f"找不到切分结果：{CHUNKS_PATH}")

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    digest = chunks_digest()
    embeddings = build_embeddings()
    metadata = {"hnsw:space": "cosine", "chunks_sha256": digest}

    def new_store() -> Chroma:
        return Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=embeddings,
            persist_directory=str(CHROMA_DIR),
            collection_metadata=metadata,
        )

    store = new_store()
    current_digest, count = collection_state(store)
    if current_digest == digest and count > 0:
        print(f"复用本地向量库 {CHROMA_DIR}，共 {count} 条", file=sys.stderr)
        return store

    documents = load_documents()
    if current_digest == digest and count == 0:
        if documents:
            print(f"正在写入向量库，共 {len(documents)} 条", file=sys.stderr)
            store.add_documents(documents, ids=[str(item.metadata["chunk_id"]) for item in documents])
        return store

    print("chunks.json 已变化，重建向量库", file=sys.stderr)
    store.delete_collection()
    store = new_store()
    if documents:
        store.add_documents(documents, ids=[str(item.metadata["chunk_id"]) for item in documents])
    return store


def get_store() -> Chroma:
    global _store
    if _store is not None:
        return _store
    with _lock:
        if _store is None:
            _store = open_store()
    return _store


def render_match(index: int, document: Document) -> str:
    section_path = str(document.metadata.get("section_path") or "").strip()
    source = str(document.metadata.get("source") or "").strip()
    body = document.page_content.strip()
    head = section_path or "（无章节路径）"
    lines = [f"[{index}] {head}"]
    if source:
        lines.append(f"来源：{source}")
    lines.append(body)
    return "\n".join(lines)


@mcp.tool()
def search_company_docs(query: str) -> str:
    """检索星辰科技企业内部知识库。

    覆盖 HR 制度、IT 支持、产品文档、合规政策和财务报销。
    用户询问请假、报销、差旅标准、VPN、打印机、电脑申领、CRM、数据安全或合规红线时使用。
    返回相似度最高的 3 个文本块，每块正文前面带章节路径和来源文件。
    """
    question = query.strip()
    if not question:
        return "请提供要检索的问题。"
    documents = get_store().similarity_search(question, k=TOP_K)
    if not documents:
        return "未在企业知识库中检索到相关内容。"
    return "\n\n".join(render_match(index, document) for index, document in enumerate(documents, start=1))


def main() -> None:
    if not CHUNKS_PATH.is_file():
        print(f"找不到切分结果：{CHUNKS_PATH}", file=sys.stderr)
        raise SystemExit(1)
    get_store()
    mcp.run()


if __name__ == "__main__":
    main()
