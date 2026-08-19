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


def locate_span(para, needle: str, occurrence: int = 0) -> tuple[int, int, list] | None:
    """在段落的可定位 run 序列中找到 needle，返回 (起始偏移, 结束偏移, runs)。

    合并 run 之后多数短语已落在单个 run 内，但跨 run 的情况仍然存在
    （中间夹着超链接、书签、域），因此定位必须在拼接文本上做。

    `occurrence` 指定要第几处（0 起）。**它存在的意义是让跨度可以缩到最小。**
    没有它时，"改哪一处"只能靠把跨度撑宽到段内唯一——错别字通道就是这么做的，
    结果一个两字的错字带出八十多字的 `original_text`，落笔时整句被删除重插，
    批注也跟着圈住一大片。有了序号，跨度可以就是那两个字。
    """
    runs = [r for r in para_runs(para) if run_is_plain(r)]
    if not runs or not needle:
        return None
    joined = "".join(run_text(r) for r in runs)
    idx = -1
    for _ in range(max(0, occurrence) + 1):
        idx = joined.find(needle, idx + 1)
        if idx < 0:
            return None
    return idx, idx + len(needle), runs


def span_count(para, needle: str) -> int:
    """跨度在段落可定位文本中出现的次数。

    `locate_span` 取的是**首个**匹配。跨度在同一段里出现多次时，"改哪一处"
    就成了一个没有依据的选择——问题记录里只有 pid 与 original_text，没有偏移量。
    「本期指标目标为 200ms，实测值为 1200ms。」里把「200ms」改掉，
    改中的是目标值还是实测值，取决于 `find` 而不是取决于判定。
    调用方据此决定：唯一才落笔，不唯一要么整段替换（术语规范化），要么不落笔。
    """
    runs = [r for r in para_runs(para) if run_is_plain(r)]
    if not runs or not needle:
        return 0
    joined = "".join(run_text(r) for r in runs)
    n, start = 0, 0
    while (i := joined.find(needle, start)) != -1:
        n += 1
        start = i + 1
    return n


# 段落中承载正文的直接子节点。批注范围必须以这一层为边界：
# w:commentRangeEnd 若插进 w:ins/w:del 内部，会被当成修订的一部分。
ANCHORABLE_TAGS = {"r", "hyperlink", "ins", "del", "moveFrom", "moveTo",
                   "smartTag", "sdt", "fldSimple", "subDoc", "customXml"}


def para_content_nodes(para) -> list:
    """段落里承载正文的直接子节点（w:pPr、书签、拼写标记等不算）。

    只含 commentReference / footnoteReference 的 run 也不算——它们是标记不是正文，
    否则同段落写第二条批注时，范围会把上一条批注的引用符也圈进去。
    """
    out = []
    for c in para:
        if not isinstance(c.tag, str) or not c.tag.startswith(f"{{{W}}}"):
            continue
        if c.tag[len(W) + 2:] not in ANCHORABLE_TAGS:
            continue
        if c.tag == q("r") and c.find(q("commentReference")) is not None:
            continue
        out.append(c)
    return out


def top_level_node(para, node):
    """把段落内任意后代节点抬到「w:p 的直接子节点」这一层。"""
    cur = node
    while cur is not None and cur.getparent() is not para:
        cur = cur.getparent()
    return cur


def comment_range_nodes(para, needle: str = "") -> tuple:
    """批注范围该插在哪两个节点的前后，返回 (起点节点, 终点节点)。

    **定位与锚定是两件事，不能共用同一份 run 集合。**
    定位只能在「可拆分 run」（不在修订标记内、只含 rPr 与 w:t）上做，
    但批注范围绝不能只覆盖这些 run——含换行 `w:br`、制表 `w:tab`、图形的 run，
    以及既有/新写入的 `w:ins`/`w:del`，同样是这段正文的一部分。
    早先的实现直接拿可拆分 run 的首尾当边界，于是：

      - 段落末尾的 run 含软换行时，范围止于换行之前 —— 表现为「只选中前面几行」；
      - 整段文字都在一个含 `w:br` 的 run 里时，一个可拆分 run 都没有，
        范围退化成段首的零长度点 —— 表现为「批注选不中任何正文」；
      - 段末刚写入修订时，范围止于修订之前。

    因此定位不到 needle（或压根没给）时一律退回**整段**，
    而不是退回「可拆分 run 的首尾」。
    """
    nodes = para_content_nodes(para)
    if not nodes:
        return None, None
    whole = (nodes[0], nodes[-1])
    if not needle:
        return whole
    loc = locate_span(para, needle)
    if not loc:
        return whole
    start, end, runs = loc
    first = last = None
    pos = 0
    for r in runs:
        t = run_text(r)
        if first is None and pos + len(t) > start:
            first = r
        if pos < end:
            last = r
        pos += len(t)
    if first is None or last is None:
        return whole
    a, b = top_level_node(para, first), top_level_node(para, last)
    if a is None or b is None:
        return whole
    kids = list(para)
    if kids.index(a) > kids.index(b):
        return whole
    return a, b


def _split_run_at(run, offset: int):
    """把一只可拆分 run 在第 offset 个字符处切成两只，返回 (前, 后)。

    文本逐字不变、rPr 深拷贝，因此「拒绝修订视图的逐字符 (字, rPr)」与
    「每段用到的 rPr 指纹集合」都不变——这正是 D9 校验的两个判据。
    """
    text = run_text(run)
    if offset <= 0:
        return None, run
    if offset >= len(text):
        return run, None
    rpr = clone_rpr(run)
    left, right = new_run(rpr, text[:offset]), new_run(rpr, text[offset:])
    parent = run.getparent()
    i = list(parent).index(run)
    parent.remove(run)
    parent.insert(i, left)
    parent.insert(i + 1, right)
    return left, right


def isolate_span(para, needle: str, occurrence: int = 0) -> list | None:
    """把 needle 精确切成独立的 run，返回构成该跨度的 run 列表（已在文档中就位）。

    批注范围的边界只能落在 run 之间，所以要让范围精确到字符，就得先让跨度
    自成 run。合并后的段落常常整段只有一只 run，不拆的话「一句话有语病」
    会圈住整段两百多字，评审人根本看不出问题在哪。

    只切边界的两只 run，中间的原样保留；只动可拆分 run（`locate_span` 已保证）。
    定位不到返回 None，由调用方退回整段。
    """
    loc = locate_span(para, needle, occurrence)
    if not loc:
        return None
    start, end, runs = loc
    covered = []
    pos = 0
    for r in runs:
        t = run_text(r)
        s, e = pos, pos + len(t)
        pos = e
        if not t or e <= start or s >= end:
            continue
        covered.append([r, max(0, start - s), min(len(t), end - s)])
    if not covered:
        return None
    # 先切尾再切头：先切头会让尾部那只 run 的偏移失效（单 run 跨度时是同一只）
    r, s0, e0 = covered[-1]
    if e0 < len(run_text(r)):
        covered[-1][0] = _split_run_at(r, e0)[0]
    r, s0, e0 = covered[0]
    if s0 > 0:
        covered[0][0] = _split_run_at(r, s0)[1]
    return [c[0] for c in covered]


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


def comment_coverage(root) -> dict:
    """每条批注实际圈住的正文，返回 {批注 id: {"reject": 串, "accept": 串}}。

    只看磁盘上的文档本身：按文档顺序走一遍，`commentRangeStart`/`End` 之间的
    文字就是 Word 里会被高亮的那一段。给出拒绝/接受两种修订视图，
    因为范围内若含刚写入的修订，原文只在拒绝视图里是连续的。
    """
    cover: dict = {}
    active: set = set()
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag == q("commentRangeStart"):
            cid = node.get(q("id"))
            if cid is not None:
                active.add(cid)
                cover.setdefault(cid, {"reject": [], "accept": []})
            continue
        if node.tag == q("commentRangeEnd"):
            active.discard(node.get(q("id")))
            continue
        if node.tag not in (q("t"), q("delText")) or not active:
            continue
        in_ins = in_del = False
        anc = node.getparent()
        while anc is not None:
            if anc.tag == q("ins"):
                in_ins = True
            elif anc.tag == q("del"):
                in_del = True
            anc = anc.getparent()
        text = node.text or ""
        for cid in active:
            if not (in_ins):
                cover[cid]["reject"].append(text)
            if not (in_del or node.tag == q("delText")):
                cover[cid]["accept"].append(text)
    return {cid: {k: "".join(v) for k, v in views.items()} for cid, views in cover.items()}


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
