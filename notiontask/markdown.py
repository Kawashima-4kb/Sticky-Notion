"""本文テキストの簡易 Markdown を Notion ブロックに変換する。"""

import re

MAX_TEXT = 2000  # rich text 1 要素あたりの文字数上限

# 上から順に判定する（### を # より先に）
_RULES = [
    (re.compile(r"^###\s+(.*)$"), "heading_3"),
    (re.compile(r"^##\s+(.*)$"), "heading_2"),
    (re.compile(r"^#\s+(.*)$"), "heading_1"),
    (re.compile(r"^\[([ xX]?)\]\s+(.*)$"), "to_do"),
    (re.compile(r"^[-*]\s+(.*)$"), "bulleted_list_item"),
    (re.compile(r"^\d+[.)]\s+(.*)$"), "numbered_list_item"),
    (re.compile(r"^>\s?(.*)$"), "quote"),
]


def rich_text(text):
    return [{"type": "text", "text": {"content": text[i:i + MAX_TEXT]}}
            for i in range(0, len(text), MAX_TEXT)]


def _block(block_type, text, **extra):
    return {"object": "block", "type": block_type,
            block_type: {"rich_text": rich_text(text), **extra}}


def line_to_block(line):
    for pattern, block_type in _RULES:
        m = pattern.match(line)
        if not m:
            continue
        if block_type == "to_do":
            return _block("to_do", m.group(2), checked=m.group(1).lower() == "x")
        return _block(block_type, m.group(1))
    return _block("paragraph", line)


def text_to_blocks(text):
    """改行ごとに 1 ブロック。連続する空行は 1 つの空段落にまとめ、先頭・末尾の空行は捨てる。"""
    blocks = []
    prev_empty = False
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        if not line.strip():
            if not prev_empty:
                blocks.append(_block("paragraph", ""))
            prev_empty = True
            continue
        blocks.append(line_to_block(line))
        prev_empty = False
    return blocks


_MD_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
              "bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> "}
_LIST_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do")


def _escape(text):
    # Notion-flavored Markdown では < がタグとして解釈されるためエスケープする
    return text.replace("\\", "\\\\").replace("<", "\\<")


def text_to_notion_markdown(text):
    """MCP 方式用。text_to_blocks と同じ解釈で Notion-flavored Markdown を組み立てる。"""
    parts = []
    prev_type = None
    for block in text_to_blocks(text):
        btype = block["type"]
        body = block[btype]
        content = _escape("".join(t["text"]["content"] for t in body["rich_text"]))
        if btype == "to_do":
            line = f"- [{'x' if body['checked'] else ' '}] {content}"
        elif btype == "paragraph" and not content:
            line = "<p />"
        else:
            line = _MD_PREFIX.get(btype, "") + content
        # 同種のリストは詰めて 1 つのリストに、それ以外は空行で段落を分ける
        sep = "\n" if btype == prev_type and btype in _LIST_TYPES else "\n\n"
        parts.append((sep if parts else "") + line)
        prev_type = btype
    return "".join(parts)


def strip_markup(line):
    """タイトル用に行頭の Markdown 記法を取り除く。"""
    for pattern, block_type in _RULES:
        m = pattern.match(line)
        if m:
            return m.group(2) if block_type == "to_do" else m.group(1)
    return line


def split_title(title, body):
    """タイトル未入力なら本文 1 行目をタイトルにし、残りを本文として返す。"""
    title = title.strip()
    if title:
        return title, body
    lines = body.strip().splitlines()
    if not lines:
        return "", ""
    return strip_markup(lines[0].strip()).strip(), "\n".join(lines[1:])
