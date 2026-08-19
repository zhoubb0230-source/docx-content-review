#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""段落级抽取：paragraphs.jsonl + 标题树（spec §6.2、§7.2.1）。

每个段落分配稳定 ID（p-000412），并记录 heading_path / in_table / is_code / page_hint。
heading_path 是逻辑审查的核心上下文，必须准确；page_hint 只用于报告定位，
绝不参与任何判定，也绝不作为切分判据。

用法
  extract.py --run-dir <run> [--config <yaml>]
退出码：0 成功；1 失败。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import EX, atomic_write_json, atomic_write_jsonl, die, emit, run_cli, version_header  # noqa: E402
from workspace import guard_write_path, load_run_config, resolve_path  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

HEADING_NAME_RE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.I)
CODE_STYLE_RE = re.compile(r"(code|preformatted|html|源代码|代码)", re.I)
CODE_TEXT_PATTERNS = [
    re.compile(r"^\s*[$#>]\s+\S"),                       # shell 提示符
    re.compile(r"^\s*(?:sudo|apt-get|yum|npm|pip|git|docker|kubectl|curl|ssh)\s"),
    re.compile(r"^\s*<[?!/a-zA-Z][^>]*>\s*$"),           # 单行标签
    re.compile(r"^\s*[\w.\-]+\s*[:=]\s*[^\s，。；：]+\s*$"),  # key=value / key: value 配置行
    re.compile(r"^\s*(?:[A-Za-z]:\\|/(?:usr|etc|var|opt|home|bin|tmp)/)"),  # 文件路径
    re.compile(r"[{};]\s*$"),                            # 以 { } ; 结尾
    re.compile(r"^\s*(?:def|class|function|public|private|import|from|SELECT|INSERT|UPDATE)\s", re.I),
]
QUOTE_STYLE_RE = re.compile(r"(quote|引用|citation)", re.I)


# --------------------------------------------------------------------------
def load_style_map(unpacked: Path) -> dict:
    """styleId → {name, heading_level, is_code, is_quote}"""
    from lxml import etree

    p = unpacked / "word" / "styles.xml"
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    root = etree.parse(str(p)).getroot()
    for st in root.findall(f"{{{W}}}style"):
        sid = st.get(f"{{{W}}}styleId")
        if not sid:
            continue
        nm = st.find(f"{{{W}}}name")
        name = (nm.get(f"{{{W}}}val") if nm is not None else "") or ""
        lvl = None
        m = HEADING_NAME_RE.match(name.strip())
        if m:
            lvl = int(m.group(1))
        else:
            m2 = re.match(r"^heading\s*([1-9])$", sid.strip(), re.I)
            if m2:
                lvl = int(m2.group(1))
        out[sid] = {
            "name": name,
            "heading_level": lvl,
            "is_code": bool(CODE_STYLE_RE.search(name) or CODE_STYLE_RE.search(sid)),
            "is_quote": bool(QUOTE_STYLE_RE.search(name) or QUOTE_STYLE_RE.search(sid)),
        }
    return out


def para_text(p) -> str:
    """段落可见文本。已删除态（w:del 内的 w:delText）不计入。"""
    parts = []
    for node in p.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag == f"{{{W}}}t":
            anc = node.getparent()
            skip = False
            while anc is not None:
                if anc.tag in (f"{{{W}}}del", f"{{{W}}}moveFrom"):
                    skip = True
                    break
                anc = anc.getparent()
            if not skip:
                parts.append(node.text or "")
        elif node.tag == f"{{{W}}}tab":
            parts.append("\t")
        elif node.tag == f"{{{W}}}br":
            parts.append("\n")
    return "".join(parts)


def para_style(p) -> str | None:
    ppr = p.find(f"{{{W}}}pPr")
    if ppr is None:
        return None
    st = ppr.find(f"{{{W}}}pStyle")
    return st.get(f"{{{W}}}val") if st is not None else None


def outline_level(p) -> int | None:
    ppr = p.find(f"{{{W}}}pPr")
    if ppr is None:
        return None
    ol = ppr.find(f"{{{W}}}outlineLvl")
    if ol is None:
        return None
    try:
        return int(ol.get(f"{{{W}}}val")) + 1
    except (TypeError, ValueError):
        return None


def has_numbering(p) -> bool:
    ppr = p.find(f"{{{W}}}pPr")
    return ppr is not None and ppr.find(f"{{{W}}}numPr") is not None


def count_page_breaks(p) -> tuple[int, int]:
    """返回 (Word 渲染出的分页数, 手动分页符数)。

    **两者不能相加。** 手动分页处 Word 同样会写一个 `lastRenderedPageBreak`，
    加起来等于把那一页数了两遍。`lastRenderedPageBreak` 覆盖**全部**页边界
    （含手动分页导致的），所以它一旦存在就该单独用；没有它才退回手动分页符。
    """
    explicit = sum(1 for br in p.iter(f"{{{W}}}br")
                   if br.get(f"{{{W}}}type") == "page")
    rendered = sum(1 for _ in p.iter(f"{{{W}}}lastRenderedPageBreak"))
    return rendered, explicit


def detect_code(text: str, style_info: dict) -> bool:
    if style_info.get("is_code"):
        return True
    t = text.strip()
    if not t or len(t) > 400:
        return False
    # 中文字符占比高的不是代码
    han = sum(1 for ch in t if "一" <= ch <= "鿿")
    if han / max(len(t), 1) > 0.3:
        return False
    return any(rx.search(t) for rx in CODE_TEXT_PATTERNS)


# --------------------------------------------------------------------------
def page_density(unpacked: Path, total_chars: int, break_count: int, cfg: dict) -> dict:
    """spec §7.2.1：app.xml → 分页符 → 常数 400，三级降级。"""
    from lxml import etree

    pe = cfg.get("page_estimation") or {}
    mode = pe.get("source", "auto")
    lo, hi = (pe.get("app_xml_sanity_range") or [100, 1200])[:2]

    if mode in ("auto", "app_xml"):
        app = unpacked / "docProps" / "app.xml"
        if app.exists():
            try:
                root = etree.parse(str(app)).getroot()
                vals = {}
                for tag in ("Pages", "Characters", "Words"):
                    el = next((e for e in root.iter() if isinstance(e.tag, str) and e.tag.endswith("}" + tag)), None)
                    if el is not None and (el.text or "").strip().lstrip("-").isdigit():
                        vals[tag] = int(el.text.strip())
                pages, chars = vals.get("Pages", 0), vals.get("Characters", 0)
                if pages > 0 and chars > 0:
                    d = chars / pages
                    if lo <= d <= hi:
                        return {"source": "app_xml", "chars_per_page": d, "pages": pages,
                                "estimated": False}
            except (etree.XMLSyntaxError, OSError):
                pass
        if mode == "app_xml":
            pass

    if mode in ("auto", "pagebreak") and break_count >= 3:
        return {"source": "pagebreak", "chars_per_page": None, "pages": break_count + 1,
                "estimated": True}

    const = float(pe.get("fallback_chars_per_page") or 400)
    return {"source": "constant", "chars_per_page": const,
            "pages": max(1, round(total_chars / const)), "estimated": True}


# --------------------------------------------------------------------------
def extract(run_dir: Path, cfg: dict) -> dict:
    from lxml import etree

    unpacked = resolve_path(run_dir, "unpacked")
    doc_xml = unpacked / "word" / "document.xml"
    if not doc_xml.exists():
        die(EX.ERROR, f"未找到 {doc_xml}，请先执行 unpack.py run")

    tree = etree.parse(str(doc_xml))
    root = tree.getroot()
    body = root.find(f"{{{W}}}body")
    if body is None:
        die(EX.ERROR, "document.xml 缺少 w:body")

    styles = load_style_map(unpacked)
    rows = []
    heading_stack: list[tuple[int, str]] = []
    headings = []
    table_seq = 0
    table_ids: dict[int, int] = {}
    total_chars = 0
    total_breaks = 0

    paras = list(body.iter(f"{{{W}}}p"))
    for idx, p in enumerate(paras, 1):
        text = para_text(p)
        sid = para_style(p)
        sinfo = styles.get(sid or "", {})
        level = sinfo.get("heading_level") or outline_level(p)
        stripped = text.strip()

        # 表格定位
        in_table = False
        tbl_id = row_idx = cell_idx = None
        anc = p.getparent()
        cell = row = tbl = None
        while anc is not None:
            if anc.tag == f"{{{W}}}tc" and cell is None:
                cell = anc
            elif anc.tag == f"{{{W}}}tr" and row is None:
                row = anc
            elif anc.tag == f"{{{W}}}tbl":
                tbl = anc
                break
            anc = anc.getparent()
        if tbl is not None:
            in_table = True
            key = id(tbl)
            if key not in table_ids:
                table_seq += 1
                table_ids[key] = table_seq
            tbl_id = table_ids[key]
            if row is not None:
                trs = tbl.findall(f"{{{W}}}tr")
                row_idx = trs.index(row) if row in trs else None
                if cell is not None:
                    tcs = row.findall(f"{{{W}}}tc")
                    cell_idx = tcs.index(cell) if cell in tcs else None

        is_heading = bool(level) and bool(stripped) and not in_table
        if is_heading:
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, stripped))

        breaks = count_page_breaks(p)          # (渲染分页, 手动分页)
        total_breaks += max(breaks)            # 只给 page_density 估总页数用

        pid = f"p-{idx:06d}"
        rec = {
            "pid": pid,
            "xpath_hint": tree.getpath(p),
            "index": idx,
            "heading_path": [h[1] for h in heading_stack],
            "level": level if is_heading else (len(heading_stack) or 0),
            "is_heading": is_heading,
            "style": sid or "Normal",
            "style_name": sinfo.get("name") or (sid or "Normal"),
            "text": text,
            "char_len": len(stripped),
            "in_table": in_table,
            "table_id": tbl_id,
            "row_idx": row_idx,
            "cell_idx": cell_idx,
            "is_list": has_numbering(p),
            "is_code": detect_code(text, sinfo),
            "is_quote": bool(sinfo.get("is_quote")),
            "page_breaks": breaks,
            "char_offset": total_chars,
        }
        total_chars += len(stripped)
        rows.append(rec)
        if is_heading:
            headings.append({"pid": pid, "level": level, "text": stripped, "index": idx,
                             "path": list(rec["heading_path"])})

    # 页码。**「这份文档共几页」与「这一段在第几页」是两个问题，来源也不同。**
    #
    # 旧实现把两者绑在一起：app.xml 给得出总页数就走「字符偏移 ÷ 每页字符数」这条
    # 线性路径，哪怕文档里明明有 Word 写好的分页标记。结果是**文档信息越全，
    # 每段的页码反而越不准**——线性模型假设字符均匀分布，而带表格、图片、
    # 分节符的长文档远非如此，正文中段能偏出几十页。
    # 现场反馈就是这个：批注里引的那句话在所指页码上找不到。
    #
    # 现在分开取：总页数优先 app.xml；**每段的页码优先分页标记**（逐段精确），
    # 一个标记都没有才退回线性估算，并把 page_estimated 置为 true。
    dens = page_density(unpacked, total_chars, total_breaks, cfg)
    total_rendered = sum(r["page_breaks"][0] for r in rows)
    total_explicit = sum(r["page_breaks"][1] for r in rows)
    which = 0 if total_rendered >= 3 else (1 if total_explicit >= 3 else None)
    if which is not None:
        page, exact = 1, True
        for rec in rows:
            rec["page_hint"] = page
            page += rec["page_breaks"][which]
        dens = {**dens, "page_hint_source":
                "rendered_breaks" if which == 0 else "explicit_breaks"}
    else:
        exact = False
        cpp = dens["chars_per_page"] or 400
        for rec in rows:
            rec["page_hint"] = int(rec["char_offset"] // cpp) + 1
        dens = {**dens, "page_hint_source": "char_linear"}
    for rec in rows:
        # 逐段页码是否精确，与「总页数是否精确」是两件事，各记各的
        rec["page_estimated"] = not exact
        rec.pop("char_offset", None)
        rec.pop("page_breaks", None)

    out = resolve_path(run_dir, "paragraphs")
    guard_write_path(out, run_dir)
    atomic_write_jsonl(out, rows)

    hpath = resolve_path(run_dir, "headings")
    guard_write_path(hpath, run_dir)
    atomic_write_json(hpath, {**version_header(), "headings": headings,
                              "page_density": dens, "total_chars": total_chars})

    return {
        "paragraphs": len(rows),
        "headings": len(headings),
        "tables": table_seq,
        "chars": total_chars,
        "in_table_paragraphs": sum(1 for r in rows if r["in_table"]),
        "code_paragraphs": sum(1 for r in rows if r["is_code"]),
        "page_density": dens,
        "pages_est": max((r["page_hint"] for r in rows), default=1),
        "paragraphs_path": str(out),
        "headings_path": str(hpath),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="extract.py", description="段落级抽取")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    emit({"ok": True, **extract(run_dir, cfg)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
