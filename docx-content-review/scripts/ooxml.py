#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OOXML 底层操作：run 定位、run 拆分、rPr 深拷贝（spec §6.3、D9）。

被 apply_revisions.py / apply_comments.py / validate_docx.py 共用。
本文件不做任何判定，只提供不会破坏样式的结构操作。

D9 的要害在这里：替换 run 内部的一段文字必须把原 run 拆成「前段/被删段/后段」，
**每一段都要深拷贝原 rPr**。漏拷即套用默认样式——三号字变五号、仿宋变等线，
而且只在用户接受修订后才显现，回写当时看不出来。
"""
from __future__ import annotations

import copy
import hashlib
from typing import Iterator

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
NS = {"w": W}

# D9 第 2 条：这些元素一旦出现，即表示本技能改动了格式
FORMAT_CHANGE_TAGS = [
    "rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "tcPrChange", "trPrChange",
]
# w:rPr 的子元素顺序受 schema 强制，w:del/w:ins 必须排在最前
RPR_ORDER = ["ins", "del", "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps",
             "strike", "dstrike", "outline", "shadow", "emboss", "imprint", "noProof",
             "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern", "position",
             "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
             "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath"]


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def rpr_of(run) -> object | None:
    return run.find(q("rPr"))


def clone_rpr(run):
    """完整深拷贝原 run 的 rPr（含 rFonts 的 ascii/eastAsia/hAnsi/cs 四个属性、
    sz/szCs、b/i/color/u/spacing 等全部子元素）。无 rPr 则返回 None。"""
    rpr = rpr_of(run)
    return copy.deepcopy(rpr) if rpr is not None else None


def canonical(el) -> str:
    """与命名空间前缀、属性书写顺序、文档作用域无关的规范化串。

    不能用 c14n2：它要求命名空间在作用域内声明，而 rPr 常常是刚深拷贝出来、
    尚未挂进文档的游离子树，序列化会直接抛异常。
    """
    parts = [el.tag]
    for k in sorted(el.attrib):
        parts.append(f"@{k}={el.attrib[k]}")
    for c in el:
        if isinstance(c.tag, str):
            parts.append("(" + canonical(c) + ")")
    if (el.text or "").strip():
        parts.append("#" + el.text.strip())
    return "|".join(parts)


def rpr_key(run_or_rpr) -> str:
    """rPr 的规范化指纹，用于逐 run 比对。"""
    rpr = run_or_rpr
    if rpr is not None and isinstance(getattr(rpr, "tag", None), str) and rpr.tag == q("r"):
        rpr = rpr_of(rpr)
    if rpr is None:
        return "∅"
    return hashlib.sha256(canonical(rpr).encode("utf-8")).hexdigest()[:16]


def new_run(rpr, text: str, *, deleted: bool = False):
    """新建 run。rPr 必须由调用方从来源 run 深拷贝而来，不得凭空构造。"""
    r = etree.SubElement(etree.Element(q("_tmp")), q("r"))
    r.getparent().remove(r)
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = etree.SubElement(r, q("delText") if deleted else q("t"))
    t.text = text
    if text != text.strip() or text == "":
        t.set(XML_SPACE, "preserve")
    return r


def para_runs(para) -> list:
    """段落中可供定位的 run：直接位于 w:p 或 w:hyperlink 下，且不在既有修订标记内。"""
    out = []
    for r in para.iter(q("r")):
        parent = r.getparent()
        skip = False
        anc = parent
        while anc is not None and anc is not para:
            if anc.tag in (q("ins"), q("del"), q("moveFrom"), q("moveTo")):
                skip = True
                break
            anc = anc.getparent()
        if not skip:
            out.append(r)
    return out


def run_text(run) -> str:
    return "".join(t.text or "" for t in run.findall(q("t")))


def run_is_plain(run) -> bool:
    """只含 rPr 与 w:t 的 run 才可安全拆分；含域、图形、换行的不动。"""
    for child in run:
        if not isinstance(child.tag, str):
            return False
        if child.tag not in (q("rPr"), q("t")):
            return False
    return run.find(q("t")) is not None


def locate_span(para, needle: str) -> tuple[int, int, list] | None:
    """在段落的可定位 run 序列中找到 needle，返回 (起始偏移, 结束偏移, runs)。

    合并 run 之后多数短语已落在单个 run 内，但跨 run 的情况仍然存在
    （中间夹着超链接、书签、域），因此定位必须在拼接文本上做。
    """
    runs = [r for r in para_runs(para) if run_is_plain(r)]
    if not runs:
        return None
    joined = "".join(run_text(r) for r in runs)
    idx = joined.find(needle)
    if idx < 0:
        # 退一步：允许空白差异
        compact = joined.replace(" ", "").replace("　", "")
        if needle.replace(" ", "").replace("　", "") not in compact:
            return None
        return None
    return idx, idx + len(needle), runs


def split_for_span(runs: list, start: int, end: int) -> dict:
    """把 [start, end) 覆盖的 run 拆为 前段 / 目标段 / 后段。

    返回 {"head": [(run, text)], "target": [(run, text)], "tail": [(run, text)],
          "anchor": 首个受影响 run, "first_index": 该 run 在父节点中的下标}
    每段文本都记录其来源 run，调用方据此深拷贝对应的 rPr。
    """
    head, target, tail = [], [], []
    pos = 0
    for r in runs:
        t = run_text(r)
        s, e = pos, pos + len(t)
        pos = e
        if e <= start or not t:
            continue
        if s >= end:
            continue
        if s < start:
            head.append((r, t[:start - s]))
        mid = t[max(0, start - s):min(len(t), end - s)]
        if mid:
            target.append((r, mid))
        if e > end:
            tail.append((r, t[end - s:]))
    affected = [r for r, _ in target] or [r for r, _ in head]
    if not affected:
        return {}
    anchor = affected[0]
    return {"head": head, "target": target, "tail": tail,
            "anchor": anchor, "affected": affected}


def iter_format_changes(root) -> Iterator[str]:
    for tag in FORMAT_CHANGE_TAGS:
        for _ in root.iter(q(tag)):
            yield tag


def rpr_sequence(root) -> list[str]:
    """全文所有 run 的 rPr 指纹序列，用于 D9 的逐 run 比对。"""
    return [rpr_key(r) for r in root.iter(q("r"))]


def paragraph_rpr_map(root) -> dict:
    """段落序号 → 该段落的 rPr 指纹序列。"""
    out = {}
    for i, p in enumerate(root.iter(q("p")), 1):
        out[i] = [rpr_key(r) for r in p.iter(q("r"))]
    return out


def text_view(root, *, accept: bool) -> str:
    """修订视图下的全文文本。

    accept=True  接受全部修订：取 w:ins 内的 w:t，丢弃 w:del
    accept=False 拒绝全部修订：丢弃 w:ins，取 w:del 内的 w:delText
    「拒绝全部修订」的结果必须与原文档逐字相同——否则就存在未被追踪的改动。
    """
    parts = []
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag not in (q("t"), q("delText")):
            continue
        in_ins = in_del = False
        anc = node.getparent()
        while anc is not None:
            if anc.tag == q("ins"):
                in_ins = True
            elif anc.tag == q("del"):
                in_del = True
            anc = anc.getparent()
        if accept:
            if in_del:
                continue
            if node.tag == q("delText"):
                continue
        else:
            if in_ins:
                continue
            # 拒绝视图里 delText 要还原为正文
        parts.append(node.text or "")
    return "".join(parts)


def styled_char_view(root, *, accept: bool) -> list[tuple[int, str, str]]:
    """逐字符的 (段落序号, 字符, rPr 指纹) 序列。

    D9 的自足校验基础：不依赖回写时记录的任何溯源信息，只看磁盘上的文档本身。
    存过的溯源记录会在文档被后续改动后失效，用它做判据等于让校验形同虚设。
    """
    out: list[tuple[int, str, str]] = []
    for pi, p in enumerate(root.iter(q("p")), 1):
        for node in p.iter():
            if not isinstance(node.tag, str) or node.tag not in (q("t"), q("delText")):
                continue
            in_ins = in_del = False
            anc = node.getparent()
            run = None
            while anc is not None:
                if anc.tag == q("r") and run is None:
                    run = anc
                elif anc.tag == q("ins"):
                    in_ins = True
                elif anc.tag == q("del"):
                    in_del = True
                anc = anc.getparent()
            if accept and (in_del or node.tag == q("delText")):
                continue
            if not accept and in_ins:
                continue
            key = rpr_key(run) if run is not None else "∅"
            for ch in (node.text or ""):
                out.append((pi, ch, key))
    return out


def max_revision_id(root) -> int:
    best = 0
    for tag in ("ins", "del", "comment", "commentRangeStart", "commentReference"):
        for el in root.iter(q(tag)):
            for attr in (q("id"),):
                v = el.get(attr)
                if v and str(v).lstrip("-").isdigit():
                    best = max(best, int(v))
    return best
