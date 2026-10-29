"""结构化分块与出处（provenance）保留。

检索的价值不只在「找到相关段落」，更在「立刻知道这段话出自哪份文件、哪一页、
哪一节、哪几行」。因此每个 chunk 都携带完整出处，前端可以直接跳转定位。

支持的输入：
  .md / .markdown  —— 按标题层级分节，节内按段落聚合
  .pdf             —— 按页解析，页内启发式识别小节标题
  .txt / .log      —— 按段落聚合
  .csv / .tsv      —— 表头 + 行组，保留列语义
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config

log = logging.getLogger("seeklit.chunk")

_HEADING_MD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# PDF 小节标题启发式：编号型（1. / 2.3 / III. / A.）、全大写短行、中文短标题
_HEADING_PDF = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?)|(?:[IVXLC]+\.?)|(?:[A-Z]\.?)|(?:\[[^\]]{1,20}\]))\s+\S.{0,90}$"
)
_SENT_SPLIT = re.compile(r"(?<=[。！？；;!?])\s*|(?<=[a-z\)\]])\.\s+")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_WS = re.compile(r"[ \t\u3000]+")


@dataclass
class Chunk:
    text: str
    section_path: str = ""
    page_no: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    chunk_index: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def n_chars(self) -> int:
        return len(self.text)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """把超长段落按句子边界切开，尽量不破坏语义完整性。"""
    if len(paragraph) <= max_chars:
        return [paragraph]
    sentences = [s for s in _SENT_SPLIT.split(paragraph) if s and s.strip()]
    if not sentences:  # 没有句读（例如超长表格行），硬切
        return [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)]
    out: list[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) > max_chars and buf:
            out.append(buf.strip())
            buf = s
        else:
            buf += s
        while len(buf) > max_chars:  # 单句仍然过长
            out.append(buf[:max_chars].strip())
            buf = buf[max_chars:]
    if buf.strip():
        out.append(buf.strip())
    return out


def _pack(paragraphs: list[tuple[str, int, int]], target: int, max_chars: int,
          overlap: int, section_of) -> list[Chunk]:
    """把 (文本, 起始行, 结束行) 段落序列打包成大小合适的 chunk。

    section_of(start_line) 用于回查该段落所属的标题路径。
    """
    chunks: list[Chunk] = []
    buf_parts: list[str] = []
    buf_lines: list[int] = []
    buf_len = 0

    def flush(end_line: int) -> None:
        nonlocal buf_parts, buf_lines, buf_len
        if not buf_parts:
            return
        text = "\n\n".join(buf_parts).strip()
        if text:
            start_line = buf_lines[0] if buf_lines else None
            sec = section_of(start_line) if start_line is not None else ""
            for piece in _split_long(text, max_chars):
                chunks.append(Chunk(text=piece, section_path=sec,
                                    line_start=start_line, line_end=end_line))
        # 尾部重叠：把最后 overlap 个字符带到下一个 chunk，避免跨块语义断裂
        tail = text[-overlap:].strip() if overlap and text else ""
        buf_parts = [tail] if tail else []
        buf_lines = [end_line] if tail else []
        buf_len = len(tail)

    for para, ls, le in paragraphs:
        para = para.strip()
        if not para:
            continue
        if buf_len + len(para) > target and buf_len > 0:
            flush(le)
        buf_parts.append(para)
        buf_lines.append(ls)
        buf_len += len(para) + 2
    if buf_parts:
        flush(buf_lines[-1] if buf_lines else None)
    return [c for c in chunks if len(c.text.strip()) >= 12]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def chunk_markdown(text: str) -> tuple[list[Chunk], str]:
    """返回 (chunks, 推断出的文档标题)。"""
    text = _clean(text)
    lines = text.split("\n")

    # 标题栈：level -> title
    stack: dict[int, str] = {}
    line_section: list[str] = []
    title = ""
    in_code = False

    for raw in lines:
        if raw.lstrip().startswith("```"):
            in_code = not in_code
            line_section.append(_section_path(stack))
            continue
        m = None if in_code else _HEADING_MD.match(raw)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()
            if not title and level <= 2:
                title = heading
            stack[level] = heading
            for lv in list(stack):
                if lv > level:
                    del stack[lv]
        line_section.append(_section_path(stack))

    def section_of(line_no: int | None) -> str:
        if line_no is None:
            return ""
        idx = min(max(line_no - 1, 0), len(line_section) - 1)
        return line_section[idx]

    # 段落切分（保留行号，1-based）
    paragraphs: list[tuple[str, int, int]] = []
    buf: list[str] = []
    start = 0
    for i, raw in enumerate(lines, start=1):
        if not raw.strip():
            if buf:
                paragraphs.append(("\n".join(buf), start, i - 1))
                buf = []
            continue
        if not buf:
            start = i
        buf.append(raw.rstrip())
    if buf:
        paragraphs.append(("\n".join(buf), start, len(lines)))

    chunks = _pack(paragraphs, config.CHUNK_TARGET_CHARS, config.CHUNK_MAX_CHARS,
                   config.CHUNK_OVERLAP_CHARS, section_of)
    # 标题行本身也参与检索：把 section_path 前置到 chunk 文本，提升关键词命中
    for c in chunks:
        if c.section_path and c.section_path not in c.text[:120]:
            c.text = f"{c.section_path}\n{c.text}"
    for i, c in enumerate(chunks):
        c.chunk_index = i
    return chunks, title


def _section_path(stack: dict[int, str]) -> str:
    if not stack:
        return ""
    return " > ".join(stack[lv] for lv in sorted(stack))


# ---------------------------------------------------------------------------
# 纯文本 / 日志
# ---------------------------------------------------------------------------
def chunk_plain(text: str) -> tuple[list[Chunk], str]:
    text = _clean(text)
    lines = text.split("\n")
    title = ""
    for raw in lines[:10]:
        s = raw.strip().lstrip("#").strip()
        if s:
            title = s[:120]
            break
    paragraphs: list[tuple[str, int, int]] = []
    buf: list[str] = []
    start = 0
    for i, raw in enumerate(lines, start=1):
        if not raw.strip():
            if buf:
                paragraphs.append(("\n".join(buf), start, i - 1))
                buf = []
            continue
        if not buf:
            start = i
        buf.append(raw.rstrip())
    if buf:
        paragraphs.append(("\n".join(buf), start, len(lines)))
    chunks = _pack(paragraphs, config.CHUNK_TARGET_CHARS, config.CHUNK_MAX_CHARS,
                   config.CHUNK_OVERLAP_CHARS, lambda _l: "")
    for i, c in enumerate(chunks):
        c.chunk_index = i
    return chunks, title


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------
def chunk_csv(text: str, delim: str | None = None) -> tuple[list[Chunk], str]:
    text = _clean(text)
    delim = delim or ("," if text.count(",") >= text.count("\t") else "\t")
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return [], ""
    header = rows[0]
    title = " / ".join(h.strip() for h in header if h.strip())[:200]
    body = rows[1:]
    per_chunk = max(1, min(12, max(1, config.CHUNK_TARGET_CHARS // max(40, len(text) // max(1, len(body))))))
    chunks: list[Chunk] = []
    for i in range(0, len(body), per_chunk):
        group = body[i : i + per_chunk]
        lines = [delim.join(header)] + [delim.join(r) for r in group]
        chunks.append(Chunk(
            text="\n".join(lines),
            section_path=f"数据表 行 {i + 1}-{i + len(group)}",
            line_start=i + 1,
            line_end=i + len(group),
            chunk_index=len(chunks),
        ))
    return chunks, title


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
_PDFPLUMBER: bool | None = None


def _quiet_pypdf() -> None:
    """压掉 pypdf 的字体告警。

    没有 fontTools 时，pypdf 解析 Type1/CFF 字体会对**每一个字体对象**打一条
    WARNING。两篇论文能刷出上百行，把真正有用的日志淹掉。文本抽取本身不受影响
    （缺字体只影响少数特殊编码的字形还原），所以这里直接降噪。
    """
    for name in ("pypdf", "pypdf._reader", "pypdf._page", "pypdf.generic._data_structures"):
        lg = logging.getLogger(name)
        if lg.level < logging.ERROR:
            lg.setLevel(logging.ERROR)


def _pdf_page_texts(path: Path) -> list[str]:
    """优先 pdfplumber（版面还原更好），回退 pypdf。"""
    global _PDFPLUMBER
    if _PDFPLUMBER is None:
        try:
            import pdfplumber  # type: ignore  # noqa: F401
            _PDFPLUMBER = True
        except ImportError:
            # 只提示一次，不要每个文件都刷一条
            _PDFPLUMBER = False
            log.info("未安装 pdfplumber，PDF 解析使用 pypdf（功能等价，版面还原略弱）")

    if _PDFPLUMBER:
        try:
            import pdfplumber  # type: ignore

            pages: list[str] = []
            with pdfplumber.open(str(path)) as pdf:
                limit = config.PDF_MAX_PAGES or len(pdf.pages)
                for page in pdf.pages[:limit]:
                    pages.append(page.extract_text() or "")
            if any(p.strip() for p in pages):
                return pages
        except Exception as exc:  # noqa: BLE001
            log.warning("pdfplumber 解析 %s 失败，回退 pypdf：%s", path.name, exc)

    _quiet_pypdf()
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        limit = config.PDF_MAX_PAGES or len(reader.pages)
        return [(reader.pages[i].extract_text() or "") for i in range(min(limit, len(reader.pages)))]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"PDF 解析失败 {path.name}: {exc}") from exc


def _pdf_title(path: Path, pages: list[str]) -> str:
    try:
        from pypdf import PdfReader  # type: ignore

        meta = PdfReader(str(path)).metadata
        if meta and getattr(meta, "title", None):
            t = str(meta.title).strip()
            if len(t) > 3:
                return t[:300]
    except Exception:  # noqa: BLE001
        pass
    for page in pages[:2]:
        for raw in page.split("\n")[:12]:
            s = _WS.sub(" ", raw).strip()
            if len(s) >= 12:
                return s[:300]
    return path.stem


def chunk_pdf(path: Path) -> tuple[list[Chunk], str]:
    """按页解析：chunk 不跨页，保证 page_no 精确可用。"""
    pages = _pdf_page_texts(path)
    title = _pdf_title(path, pages)
    chunks: list[Chunk] = []
    current_section = ""

    for pno, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        lines = [_WS.sub(" ", ln).rstrip() for ln in page_text.split("\n")]
        paragraphs: list[tuple[str, int, int]] = []
        buf: list[str] = []
        start = 0
        for i, ln in enumerate(lines, start=1):
            stripped = ln.strip()
            is_heading = bool(stripped) and len(stripped) <= 90 and (
                _HEADING_PDF.match(stripped)
                or (stripped.isupper() and len(stripped.split()) >= 2)
            ) and not stripped.endswith((".", ",", "，", "。", ":", "："))
            if is_heading:
                if buf:
                    paragraphs.append(("\n".join(buf), start, i - 1))
                    buf = []
                current_section = stripped
                paragraphs.append((stripped, i, i))
                continue
            if not stripped:
                if buf:
                    paragraphs.append(("\n".join(buf), start, i - 1))
                    buf = []
                continue
            if not buf:
                start = i
            buf.append(ln)
        if buf:
            paragraphs.append(("\n".join(buf), start, len(lines)))

        page_chunks = _pack(
            paragraphs, config.CHUNK_TARGET_CHARS, config.CHUNK_MAX_CHARS,
            min(config.CHUNK_OVERLAP_CHARS, 60), lambda _l: "",
        )
        for c in page_chunks:
            c.page_no = pno
            # 页内若识别到小节标题，用它；否则沿用上一页的小节
            head = next((p[0].strip() for p in paragraphs
                         if p[0].strip() and len(p[0].strip()) <= 90
                         and _HEADING_PDF.match(p[0].strip())), "")
            c.section_path = head or current_section
            c.chunk_index = len(chunks)
            chunks.append(c)

    # 论文正文里的英文标题信息补进文本，便于关键词检索命中作者/方法名
    for c in chunks:
        if c.section_path and c.section_path not in c.text[:120]:
            c.text = f"{c.section_path}\n{c.text}"
    return chunks, title


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
KIND_BY_DIR = {"papers": "paper", "notes": "note", "records": "record"}
KIND_BY_EXT = {".pdf": "paper", ".md": "note", ".markdown": "note",
               ".txt": "record", ".log": "record", ".csv": "record", ".tsv": "record"}


def chunk_file(path: Path) -> tuple[list[Chunk], str, str]:
    """解析任意受支持的文件，返回 (chunks, title, doc_kind)。"""
    ext = path.suffix.lower()
    kind = KIND_BY_DIR.get(path.parent.name.lower()) or KIND_BY_EXT.get(ext, "note")

    if ext == ".pdf":
        chunks, title = chunk_pdf(path)
        # PDF 的标题来自文档元数据或首行大字号文本，通常已经是完整论文名，
        # 不要再和文件名拼接。
        title = _compose_title(path, title, prefer_heading=True)
    elif ext in (".csv", ".tsv"):
        chunks, _header = chunk_csv(path.read_text(encoding="utf-8", errors="replace"),
                                    "," if ext == ".csv" else "\t")
        # 表头当标题毫无信息量（method_id / name_cn / family / …），
        # 直接用文件名；表头本来就已经拼进每个分块的正文里了。
        title = _compose_title(path, "")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        if ext in (".md", ".markdown"):
            chunks, _heading = chunk_markdown(text)
            title = _compose_title(path, _doc_heading(text))
        else:
            chunks, _first_line = chunk_plain(text)
            # 日志/纯文本的第一行往往是数据而不是标题
            title = _compose_title(path, "")

    if not title:
        title = path.stem
    return chunks, title, kind


def _doc_heading(text: str) -> str:
    """取 Markdown 里最能代表整篇文档的那个标题。

    优先 H1；没有合格 H1 时退到第一个「不像大纲编号」的标题。
    两类标题不能当文档身份，直接跳过：
      1. `一、任务界定` / `1. 通信链路异常` 这种大纲编号 —— 它是章节而不是文档，
         拿它当标题会让检索结果里出现一堆同名文档（实测真的撞过名）。
      2. `ESA：` / `小结` 这种过短或以冒号结尾的 —— 拼进标题里只会添乱。
    """
    in_code = False
    h1 = ""
    fallback = ""
    for raw in text.split("\n"):
        if raw.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING_MD.match(raw)
        if not m:
            continue
        level, heading = len(m.group(1)), m.group(2).strip()
        if not heading or not _usable_heading(heading):
            continue
        if level == 1 and not h1:
            h1 = heading[:200]
        elif not fallback and not _OUTLINE_HEADING.match(heading):
            fallback = heading[:200]
    return h1 or fallback


# 「一、xxx」「1. xxx」「(二) xxx」这类大纲编号开头的小标题
_OUTLINE_HEADING = re.compile(r"^(?:[（(]?\s*(?:[一二三四五六七八九十百]+|\d{1,3})\s*[)）、.．:：])")


def _usable_heading(heading: str) -> bool:
    """标题本身要有信息量：不能太短、不能以冒号结尾、不能是纯符号。"""
    h = heading.strip().strip("#").strip()
    if len(h) < 3:
        return False
    if h.endswith(("：", ":")):
        return False
    return bool(re.search(r"[\w\u4e00-\u9fff]", h))


def _compose_title(path: Path, heading: str, *, prefer_heading: bool = False) -> str:
    """把文件名和文档内标题合成一个既可辨识又有信息量的 doc_title。

    实测过的反面教材（都在 26 篇语料里真实出现过）：
        规则汇总.md          → 标题被推断成「一、任务界定」，和别的文档撞名
        M06.md               → 标题只剩「M06」，看不出是 SPOT
        method_params.csv    → 标题变成整行表头，长到没法显示
    合成规则：
        文件名是身份（唯一、稳定、和 source_path 对得上），
        文档内标题是内容摘要；两者不重复时用「 · 」连起来。
    """
    stem = path.stem.strip()
    heading = (heading or "").strip()
    if prefer_heading and heading:
        return heading[:240]
    if not heading or heading == stem:
        return stem or heading
    if stem.lower() in heading.lower() or heading.lower() in stem.lower():
        return heading[:240] if len(heading) >= len(stem) else stem
    return f"{stem} · {heading}"[:240]


def detect_lang(text: str) -> str:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if cjk == 0 and ascii_letters == 0:
        return "und"
    return "zh" if cjk >= ascii_letters * 0.3 else "en"
