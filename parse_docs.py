# -*- coding: utf-8 -*-
"""把企业知识库中的 .docx 切成带元数据的 JSON 块。

切分规则：
- 制度、红线、报销标准：按「第X条」切，块首保留章、节标题。
- 有编号小节（1.1、###）的手册和制度：按小节切。
- FAQ：一问一答一块，问题原文留在正文里。
- 操作指南：一个场景（一个标题下的连续步骤）一块。
- 员工手册：按「员工手册××：」分节，再按 2-1、2-1-1 这类条款切。
- 申请单：整张表一块。
- 表格不单独拆碎，挂在当前条款或小节上。
- 单块上限 800 字。只有超长块才按 720 字窗口继续切，重叠 60 字。

用法（在知识库根目录）：
    python parse_docs.py
    python parse_docs.py --root . --output chunks.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
except ImportError:
    print("缺少依赖 python-docx，请先执行：pip install python-docx", file=sys.stderr)
    raise

MAX_CHARS = 800
WINDOW_CHARS = 720
OVERLAP_CHARS = 60

DIR_CATEGORY = {
    "HR制度": "HR",
    "IT支持": "IT",
    "产品文档": "产品",
    "政策文档": "政策",
    "财务报销": "财务",
}

FAQ_RE = re.compile(r"^(?:Q\d*|问)\s*[:：]")
ARTICLE_RE = re.compile(
    r"^(第[0-9一二三四五六七八九十百零]+条)(?:[、.．]\s*|\s+)?(.*)$"
)
CHAPTER_RE = re.compile(r"^第[0-9一二三四五六七八九十百零]+章(?:\s+\S.*)?$")
NUM_HEAD_RE = re.compile(r"^(\d+(?:\.\d+)+)\s+(\S.*)$")
CLAUSE_RE = re.compile(r"^(\d+(?:-\d+)+)(.*)$")
HANDBOOK_RE = re.compile(r"^员工手册([^：:。\n]{1,20})[：:]\s*(.*)$")
HASH_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
LIST_MARK_RE = re.compile(r"^([-•*]|\d+[.、．])\s*")
BANNER_SPLIT_RE = re.compile(r"(?=员工手册[^：:。\n]{1,20}[：:])")


@dataclass
class Buffer:
    kind: str
    parts: list[str] = field(default_factory=list)
    leaf: str | None = None
    cid: str | None = None
    rest: str = ""

    @property
    def label_only(self) -> bool:
        """条款行只有短标题、后面还没跟上正文时，把它当成下级条款的路径，不单独成块。"""
        if self.kind != "clause" or len(self.parts) != 1:
            return False
        rest = self.rest.strip()
        if not rest:
            return True
        return len(rest) <= 24 and not re.search(r"[。！？]", rest)


def clean_inline(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ").replace("**", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def short_label(text: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def style_heading_level(paragraph: Paragraph) -> int | None:
    try:
        name = paragraph.style.name if paragraph.style is not None else ""
    except Exception:
        return None
    match = re.fullmatch(r"(?:Heading|标题)\s*(\d+)", name or "", flags=re.I)
    if not match:
        return None
    return int(match.group(1))


def is_list_item(paragraph: Paragraph) -> bool:
    p_pr = paragraph._p.pPr
    return p_pr is not None and p_pr.numPr is not None


def iter_block_items(document):
    """按 Word 正文顺序交替产出段落和表格。表格里的段落不会再单独产出。"""
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def render_table(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            value = clean_inline(cell.text.replace("\n", " "))
            if value and (not cells or cells[-1] != value):
                cells.append(value)
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def split_banners(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = BANNER_SPLIT_RE.split(text)
    return [part.strip() for part in parts if part and part.strip()]


def window_split(text: str, size: int, overlap: int) -> list[str]:
    """按字符窗口切开超长正文，尽量在换行处断开。"""
    text = text.strip()
    if not text:
        return []
    size = max(size, overlap + 1, 1)
    parts: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            floor = min(length, start + max(size // 2, 1))
            break_at = text.rfind("\n", floor, end)
            if break_at > start:
                end = break_at
        piece = text[start:end].strip()
        if piece:
            parts.append(piece)
        if end >= length:
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
    if len(parts) >= 2 and len(parts[-1]) < 40:
        parts[-2] = (parts[-2].rstrip() + "\n" + parts[-1]).strip()
        parts.pop()
    return parts


def split_with_banner(body: str, banner: str) -> list[str]:
    body = body.strip()
    if not body:
        return []
    prefix = f"【{banner}】\n" if banner else ""
    if len(prefix) + len(body) <= MAX_CHARS:
        return [prefix + body]
    budget = MAX_CHARS - len(prefix)
    if budget < 240:
        return window_split(prefix + body, WINDOW_CHARS, OVERLAP_CHARS)
    parts = window_split(body, budget, OVERLAP_CHARS)
    return [prefix + part for part in parts if part.strip()]


def category_of(path: Path, root: Path) -> str:
    for part in path.relative_to(root).parts[:-1]:
        if part in DIR_CATEGORY:
            return DIR_CATEGORY[part]
    return "未分类"


def risk_of(path: Path) -> str:
    stem = path.stem
    if "红线" in stem or "数据安全" in stem:
        return "high"
    return "normal"


def document_type(path: Path, title: str) -> str:
    name = f"{path.stem}{title}"
    if "申请单" in name or "申请表" in name:
        return "form"
    if re.search(r"API|接口文档", name, re.I):
        return "api"
    if any(key in name for key in ("管理办法", "制度", "规定", "规范", "标准", "红线")):
        return "policy"
    if "员工手册" in name:
        return "policy"
    if "FAQ" in name.upper():
        return "faq"
    return "sop"


def chunk_doc_type(doc_type: str, kind: str) -> str:
    if doc_type == "form":
        return "form"
    if kind == "faq":
        return "faq"
    if doc_type == "faq":
        return "sop"
    return doc_type


def find_title(blocks: list, path: Path) -> str:
    first_short = ""
    for block in blocks:
        if not isinstance(block, Paragraph):
            continue
        raw = block.text.replace("\u3000", " ").strip()
        if not raw:
            continue
        hash_match = HASH_HEAD_RE.match(raw)
        style_level = style_heading_level(block)
        if hash_match and len(hash_match.group(1)) == 1:
            return clean_inline(hash_match.group(2))
        if style_level == 1 and not raw.startswith("#"):
            return clean_inline(raw)
        if not first_short:
            text = clean_inline(HASH_HEAD_RE.sub("", raw, count=1))
            if text:
                first_short = text
    if first_short and len(first_short) <= 40 and "。" not in first_short:
        return first_short
    return path.stem


class Chunker:
    def __init__(self, title: str, doc_type: str, source: str, category: str, risk: str):
        self.title = title
        self.doc_type = doc_type
        self.source = source
        self.category = category
        self.risk = risk
        self.stack: list[tuple[int, str]] = []
        self.clause_context: list[tuple[str, str]] = []
        self.buf: Buffer | None = None
        self.chunks: list[dict] = []
        self.seq = 0
        self.title_consumed = False

    def parse(self, blocks: list) -> None:
        if self.doc_type == "form":
            self._parse_form(blocks)
            return
        for block in blocks:
            if isinstance(block, Paragraph):
                self.consume_paragraph(block)
            elif isinstance(block, Table):
                rendered = render_table(block)
                if rendered:
                    self.add_text(rendered)
        self.flush()

    def _parse_form(self, blocks: list) -> None:
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, Paragraph):
                text = clean_inline(block.text)
                if text and text != self.title:
                    parts.append(text)
            elif isinstance(block, Table):
                rendered = render_table(block)
                if rendered:
                    parts.append(rendered)
        body = "\n".join(parts).strip()
        if not body:
            return
        self._emit("body", body, "全文", "")

    def consume_paragraph(self, paragraph: Paragraph) -> None:
        raw_text = paragraph.text.replace("\r", "\n").replace("\u3000", " ")
        lines: list[str] = []
        for line in raw_text.splitlines():
            lines.extend(split_banners(line))
        if not lines:
            return
        style_level = style_heading_level(paragraph)
        if style_level and not lines[0].lstrip().startswith("#"):
            self.on_heading(style_level, clean_inline(lines[0]))
            for line in lines[1:]:
                self.consume_line(line)
            return
        list_item = is_list_item(paragraph)
        for index, line in enumerate(lines):
            if list_item and index == 0:
                stripped = clean_inline(line)
                if stripped and not LIST_MARK_RE.match(stripped):
                    line = "- " + line.lstrip()
            self.consume_line(line)

    def consume_line(self, raw: str) -> None:
        raw = raw.replace("\u3000", " ").replace("\xa0", " ").strip()
        if not raw:
            return
        hash_match = HASH_HEAD_RE.match(raw)
        if hash_match:
            self.on_heading(len(hash_match.group(1)), clean_inline(hash_match.group(2)))
            return
        text = clean_inline(raw)
        if not text:
            return
        banner = HANDBOOK_RE.match(text)
        if banner:
            self.on_heading(2, banner.group(1).strip())
            rest = banner.group(2).strip()
            if rest:
                self.add_text(rest)
            return
        if CHAPTER_RE.match(text):
            self.on_heading(2, text)
            return
        if FAQ_RE.match(text):
            self.start_faq(text)
            return
        article = ARTICLE_RE.match(text)
        if article:
            self.start_article(text, article.group(1), (article.group(2) or "").strip())
            return
        numbered = NUM_HEAD_RE.match(text)
        if numbered:
            level = min(2 + numbered.group(1).count("."), 6)
            self.on_heading(level, f"{numbered.group(1)} {numbered.group(2).strip()}")
            return
        clause = CLAUSE_RE.match(text)
        if clause:
            self.start_clause(text, clause.group(1), (clause.group(2) or "").strip())
            return
        self.add_text(text)

    def on_heading(self, level: int, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.title_consumed = True
        self.flush()
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, text))
        self.clause_context.clear()
        if FAQ_RE.match(text):
            self.buf = Buffer(kind="faq", parts=[text], leaf=None)

    def start_faq(self, text: str) -> None:
        self.flush()
        self.buf = Buffer(kind="faq", parts=[text], leaf=short_label(text))

    def start_article(self, text: str, number: str, rest: str) -> None:
        self.flush()
        leaf = number
        if rest and len(rest) <= 24 and not re.search(r"[。！？]", rest):
            leaf = f"{number} {rest}"
        self.buf = Buffer(kind="article", parts=[text], leaf=leaf, rest=rest)

    def start_clause(self, text: str, cid: str, rest: str) -> None:
        if (
            self.buf is not None
            and self.buf.kind == "clause"
            and self.buf.cid
            and cid.startswith(self.buf.cid + "-")
            and self.buf.label_only
        ):
            parent = (self.buf.cid, self.buf.leaf or self.buf.cid)
            self.buf = None
            self._retain_prefix(parent[0])
            self.clause_context.append(parent)
        else:
            self.flush()
        self._retain_prefix(cid)
        leaf = cid
        if rest and len(rest) <= 24 and not re.search(r"[。！？]", rest):
            leaf = f"{cid} {rest}"
        self.buf = Buffer(kind="clause", parts=[text], leaf=leaf, cid=cid, rest=rest)

    def _retain_prefix(self, cid: str) -> None:
        while self.clause_context and not cid.startswith(self.clause_context[-1][0] + "-"):
            self.clause_context.pop()

    def add_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if (
            not self.title_consumed
            and text == self.title
            and self.buf is None
            and not self.stack
            and not self.clause_context
        ):
            self.title_consumed = True
            return
        self.title_consumed = True
        if self.buf is None:
            self.buf = Buffer(kind="body", parts=[])
        self.buf.parts.append(text)

    def visible_nodes(self) -> list[str]:
        texts = [text for _, text in self.stack if text != self.title]
        return [short_label(text) for text in texts]

    def path_and_banner(self, buf: Buffer) -> tuple[str, str]:
        base = self.visible_nodes() + [label for _, label in self.clause_context]
        if buf.kind == "faq" and buf.leaf is None:
            path = " / ".join(base) if base else "前言"
            banner = " / ".join(base[:-1]) if len(base) > 1 else ""
            return path, banner
        if buf.leaf and buf.kind in {"article", "clause", "faq"}:
            path = " / ".join(base + [buf.leaf]) if base else buf.leaf
            banner = " / ".join(base)
            return path, banner
        path = " / ".join(base) if base else "前言"
        return path, path

    def flush(self) -> None:
        buf = self.buf
        self.buf = None
        if buf is None:
            return
        body = "\n".join(part.strip() for part in buf.parts if part and part.strip()).strip()
        if not body:
            return
        path, banner = self.path_and_banner(buf)
        self._emit(buf.kind, body, path, banner)

    def _emit(self, kind: str, body: str, path: str, banner: str) -> None:
        pieces = split_with_banner(body, banner)
        total = len(pieces)
        for index, piece in enumerate(pieces, start=1):
            self.seq += 1
            self.chunks.append(
                {
                    "text": piece,
                    "metadata": {
                        "chunk_id": f"{self.source}#{self.seq:04d}",
                        "source": self.source,
                        "category": self.category,
                        "title": self.title,
                        "section_path": path,
                        "doc_type": chunk_doc_type(self.doc_type, kind),
                        "risk_level": self.risk,
                        "part": index,
                        "part_count": total,
                    },
                }
            )


def parse_file(path: Path, root: Path) -> list[dict]:
    try:
        document = Document(str(path))
    except Exception as exc:
        raise RuntimeError(f"无法读取 {path}: {exc}") from exc
    blocks = list(iter_block_items(document))
    title = find_title(blocks, path)
    chunker = Chunker(
        title=title,
        doc_type=document_type(path, title),
        source=path.relative_to(root).as_posix(),
        category=category_of(path, root),
        risk=risk_of(path),
    )
    chunker.parse(blocks)
    return chunker.chunks


def iter_docx(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.docx"):
        if path.name.startswith("~$"):
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def parse_tree(root: Path) -> list[dict]:
    chunks: list[dict] = []
    for path in iter_docx(root):
        chunks.extend(parse_file(path, root))
    return chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按章节、条款、FAQ 和表格把知识库 Word 切成 JSON 块")
    parser.add_argument("--root", type=Path, default=Path("."), help="知识库根目录，默认当前目录")
    parser.add_argument("--output", type=Path, default=Path("chunks.json"), help="输出 JSON 文件")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 1

    chunks = parse_tree(root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    counts = Counter(item["metadata"]["category"] for item in chunks)
    order = ("HR", "IT", "产品", "政策", "财务", "未分类")
    detail = "，".join(f"{name} {counts[name]}" for name in order if counts[name])
    print(f"共 {len(chunks)} 块 -> {args.output}（{detail}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
