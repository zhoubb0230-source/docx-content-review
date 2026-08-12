#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回写后校验（spec §6.3 四项检查、D9）。

四项检查任一失败即判定本次回写失败，回滚到原文档并报错。优先级排序中
「样式不变性」仅次于「源文档完整性」，因此本文件的判定一律 fail-closed。

  1. 结构校验     全部部件 XML 良构 + 本技能所涉元素的结构约束
                  （提供 --xsd <dir> 时额外做真正的 XSD 校验）
  2. 无未追踪变更 「拒绝全部修订」视图必须与原文档逐字相同
  3. 样式不变性   样式类部件字节哈希不变 + 无格式修订标记 + 逐 run rPr 一致
  4. 批注锚定完整 每个 commentReference 都有配对的 Start/End 且 id 存在于 comments.xml

用法
  validate_docx.py --run-dir <run> [--xsd <dir>] [--json]
退出码：0 全部通过；8 存在失败项。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ooxml as ox  # noqa: E402
from _common import EX, atomic_write_json, emit, read_json, run_cli, version_header  # noqa: E402
from unpack import STYLE_PARTS  # noqa: E402
from workspace import guard_write_path, resolve_path  # noqa: E402


def check_structure(unpacked: Path, xsd_dir: str | None) -> dict:
    from lxml import etree

    problems = []
    parsed = 0
    for p in sorted(unpacked.rglob("*.xml")) + sorted(unpacked.rglob("*.rels")):
        try:
            etree.parse(str(p))
            parsed += 1
        except etree.XMLSyntaxError as exc:
            problems.append(f"XML 不良构：{p.relative_to(unpacked)}（{exc}）")

    doc = unpacked / "word" / "document.xml"
    if doc.exists():
        root = etree.parse(str(doc)).getroot()
        # w:rPr 内 w:ins/w:del 必须排在其他子元素之前（schema 强制的元素顺序）
        for rpr in root.iter(ox.q("rPr")):
            kids = [c.tag.split("}")[-1] for c in rpr if isinstance(c.tag, str)]
            for marker in ("ins", "del"):
                if marker in kids and kids.index(marker) != 0:
                    problems.append(f"w:rPr 中的 w:{marker} 未排在首位，违反元素顺序约束")
        # w:r 若有 rPr 必须是第一个子元素
        for r in root.iter(ox.q("r")):
            kids = [c.tag.split("}")[-1] for c in r if isinstance(c.tag, str)]
            if "rPr" in kids and kids.index("rPr") != 0:
                problems.append("w:r 中的 w:rPr 未排在首位")
        # 修订 id 必须唯一
        ids = Counter()
        for tag in ("ins", "del"):
            for el in root.iter(ox.q(tag)):
                v = el.get(ox.q("id"))
                if v is None:
                    problems.append(f"w:{tag} 缺少 w:id")
                else:
                    ids[v] += 1
        dup = [k for k, v in ids.items() if v > 1]
        if dup:
            problems.append(f"修订 id 重复：{dup[:5]}")
        # 批注 id 必须唯一。撞号的后果是旧锚点指向新批注，而"配对齐全"这类
        # 检查完全看不出来——既有批注被顶掉时四项校验曾经全绿。
        cids = Counter()
        for el in root.iter(ox.q("commentRangeStart")):
            v = el.get(ox.q("id"))
            if v is not None:
                cids[v] += 1
        dupc = [k for k, v in cids.items() if v > 1]
        if dupc:
            problems.append(f"批注 id 重复：{dupc[:5]}（旧锚点会指向新批注）")
        cx = unpacked / "word" / "comments.xml"
        if cx.exists():
            cdup = Counter(c.get(ox.q("id")) for c in etree.parse(str(cx)).getroot()
                           .iter(ox.q("comment")))
            dupd = [k for k, v in cdup.items() if v > 1]
            if dupd:
                problems.append(f"comments.xml 中批注 id 重复：{dupd[:5]}")
        # w:del 内必须是 w:delText，不能是 w:t
        for d in root.iter(ox.q("del")):
            if d.getparent() is not None and d.getparent().tag == ox.q("rPr"):
                continue
            if d.find(f".//{ox.q('t')}") is not None:
                problems.append("w:del 内出现 w:t，应为 w:delText")

    xsd_note = "未提供 XSD，已执行结构约束校验"
    if xsd_dir:
        wml = Path(xsd_dir) / "wml.xsd"
        if not wml.exists():
            xsd_note = f"未找到 {wml}，退回结构约束校验"
        else:
            try:
                schema = etree.XMLSchema(etree.parse(str(wml)))
                if doc.exists() and not schema.validate(etree.parse(str(doc))):
                    problems.extend(str(e)[:200] for e in schema.error_log[:10])
                xsd_note = f"已按 {wml} 执行 XSD 校验"
            except etree.XMLSchemaParseError as exc:
                xsd_note = f"XSD 加载失败：{exc}"
    return {"name": "结构校验", "pass": not problems, "parsed_parts": parsed,
            "problems": problems[:20], "xsd": xsd_note}


def check_untracked(run_dir: Path, unpacked: Path) -> dict:
    """任何未被 w:ins/w:del 包裹的文本变更都要报告——这类改动在
    「接受修订后」视图里不可见，极易无意产生。"""
    from lxml import etree

    baseline = resolve_path(run_dir, "work") / "document.baseline.xml"
    doc = unpacked / "word" / "document.xml"
    if not baseline.exists() or not doc.exists():
        return {"name": "无未追踪变更", "pass": False,
                "problems": ["缺少基线或 document.xml，无法比对"]}
    before = ox.text_view(etree.parse(str(baseline)).getroot(), accept=False)
    after = ox.text_view(etree.parse(str(doc)).getroot(), accept=False)
    if before == after:
        return {"name": "无未追踪变更", "pass": True, "untracked_changes": 0}
    # 定位首个差异，便于排障
    i = 0
    while i < min(len(before), len(after)) and before[i] == after[i]:
        i += 1
    return {"name": "无未追踪变更", "pass": False, "untracked_changes": 1,
            "problems": [f"「拒绝全部修订」视图与原文档在第 {i} 字处开始不同："
                         f"原「{before[i:i+30]}」→ 现「{after[i:i+30]}」"]}


def check_style(run_dir: Path, unpacked: Path) -> dict:
    """D9 三项：样式部件字节哈希、无格式修订标记、逐 run rPr。"""
    from lxml import etree

    problems = []
    base = read_json(resolve_path(run_dir, "work") / "unpack-baseline.json", {}) or {}
    expect = base.get("style_parts") or {}
    for part, want in expect.items():
        p = unpacked / part
        if want is None:
            if p.exists():
                problems.append(f"{part} 原文档中不存在，回写后却出现了")
            continue
        if not p.exists():
            problems.append(f"{part} 丢失")
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != want:
            problems.append(f"{part} 字节哈希改变（{want[:8]}→{got[:8]}）")

    doc = unpacked / "word" / "document.xml"
    root = etree.parse(str(doc)).getroot()
    fmt = sorted(set(ox.iter_format_changes(root)))
    if fmt:
        problems.append(f"document.xml 中出现格式修订标记：{fmt}")

    # 逐 run rPr 比对。全部基于磁盘上的两份文档现场重算，不读回写时的溯源记录——
    # 溯源记录只描述"当时做了什么"，无法证明"现在的文档是什么样"。
    baseline_path = resolve_path(run_dir, "work") / "document.baseline.xml"
    rpr_added = []
    if baseline_path.exists():
        broot = etree.parse(str(baseline_path)).getroot()
        base_rpr = set(ox.rpr_sequence(broot))
        rpr_added = sorted(set(ox.rpr_sequence(root)) - base_rpr)
        if rpr_added:
            problems.append(f"回写引入了基线中不存在的 rPr 指纹 {len(rpr_added)} 种："
                            f"{rpr_added[:3]}（应为来源 run 的深拷贝）")

        # ① 拒绝全部修订后，逐字符的 (字, rPr) 必须与基线完全一致：
        #    这同时覆盖了文本与格式两方面的「可完全还原」要求。
        bview = ox.styled_char_view(broot, accept=False)
        nview = ox.styled_char_view(root, accept=False)
        if len(bview) != len(nview):
            problems.append(f"拒绝修订视图字符数不一致：基线 {len(bview)} vs 回写后 {len(nview)}")
        else:
            for i, (b, n) in enumerate(zip(bview, nview)):
                if b[1] != n[1]:
                    problems.append(f"拒绝修订视图第 {i} 字不同：「{b[1]}」→「{n[1]}」")
                    break
                if b[2] != n[2]:
                    problems.append(f"拒绝修订视图第 {i} 字（「{b[1]}」，段落 {b[0]}）的 rPr 改变："
                                    f"{b[2]}→{n[2]}")
                    break

        # ② 接受全部修订后，每个段落用到的 rPr 必须是该段落基线 rPr 的子集：
        #    新插入的文字只能沿用来源 run 的格式，不得引入任何新格式。
        base_by_para: dict[int, set] = {}
        for pi, _, k in bview:
            base_by_para.setdefault(pi, set()).add(k)
        new_by_para: dict[int, set] = {}
        for pi, _, k in ox.styled_char_view(root, accept=True):
            new_by_para.setdefault(pi, set()).add(k)
        for pi, keys in sorted(new_by_para.items()):
            extra = keys - base_by_para.get(pi, set())
            if extra:
                problems.append(f"段落 p-{pi:06d} 接受修订后出现该段落基线中不存在的 rPr："
                                f"{sorted(extra)[:2]}（新增 run 未深拷贝来源 rPr）")
                break

    return {"name": "样式不变性(D9)", "pass": not problems, "problems": problems[:20],
            "style_parts_checked": len(expect), "new_rpr_fingerprints": len(rpr_added)}


def _norm(s: str) -> str:
    return "".join(str(s or "").split())


def check_comments(run_dir: Path, unpacked: Path) -> dict:
    from lxml import etree

    problems = []
    doc = unpacked / "word" / "document.xml"
    root = etree.parse(str(doc)).getroot()
    starts = {e.get(ox.q("id")) for e in root.iter(ox.q("commentRangeStart"))}
    ends = {e.get(ox.q("id")) for e in root.iter(ox.q("commentRangeEnd"))}
    refs = {e.get(ox.q("id")) for e in root.iter(ox.q("commentReference"))}

    cpath = unpacked / "word" / "comments.xml"
    declared = set()
    if cpath.exists():
        croot = etree.parse(str(cpath)).getroot()
        declared = {c.get(ox.q("id")) for c in croot.iter(ox.q("comment"))}
    elif refs:
        problems.append("document.xml 中存在批注引用，但缺少 comments.xml")

    for cid in sorted(refs):
        if cid not in starts:
            problems.append(f"批注 {cid} 缺少 commentRangeStart")
        if cid not in ends:
            problems.append(f"批注 {cid} 缺少 commentRangeEnd")
        if declared and cid not in declared:
            problems.append(f"批注 {cid} 未在 comments.xml 中声明")
    for cid in sorted(declared - refs):
        problems.append(f"comments.xml 声明了批注 {cid}，但正文中无 commentReference（批注不可见）")

    # 锚定「存在」不等于锚定「圈对了」。Start/End 齐备但范围里没有正文，或者
    # 范围只盖住计划锚点的前半截，在 Word 里就是「批注选不中/只选中前面几行」。
    cover = ox.comment_coverage(root)
    empty = {cid for cid in refs
             if not _norm((cover.get(cid) or {}).get("reject") or "")
             and not _norm((cover.get(cid) or {}).get("accept") or "")}
    for cid in sorted(empty):
        problems.append(f"批注 {cid} 的锚定范围内没有任何正文（Word 中选不中文字）")

    # 计划里的锚点是**独立于回写过程**的预期：commentlist.json 由 plan 写，
    # 不是 apply 写的自述，因此可以拿来判定「文档是否实现了预期」。
    plan = read_json(resolve_path(run_dir, "work") / "commentlist.json", {}) or {}
    ptext: dict[str, str] = {}
    for i, p in enumerate(root.iter(ox.q("p")), 1):
        ptext[f"p-{i:06d}"] = _norm("".join(
            t.text or "" for t in p.iter(ox.q("t"), ox.q("delText"))))
    partial = 0
    for it in plan.get("comments") or []:
        cid = str(it.get("comment_id"))
        want = _norm(it.get("anchor"))
        got = cover.get(cid)
        if not want or got is None or cid in empty:
            continue
        # 范围内若含刚写入的修订，原文只在「拒绝修订」视图里连续，两视图取其一即可
        if want in _norm(got["reject"]) or want in _norm(got["accept"]):
            continue
        # 锚点在本段里根本不存在（跨段落、或原文已被另一处修订改写）不算锚定缺陷
        if want not in ptext.get(it.get("pid") or "", ""):
            continue
        partial += 1
        shown = got["reject"] or got["accept"]
        problems.append(f"批注 {cid} 的锚定范围未覆盖完整锚点："
                        f"计划「{str(it.get('anchor'))[:20]}…」，"
                        f"实际只圈住「{shown[:20]}…」")

    # 既有批注必须一条不少、一个字不变。送审文档常常已经带着别人的批注，
    # 而本技能往同一份 comments.xml 里追加——整份覆盖会把它们连人带话抹掉。
    # 判据取自**回写前的现场快照**（unpack 时存的 comments.baseline.xml），
    # 不是回写时的自述：溯源只描述"当时做了什么"，证明不了"现在的文档是什么样"。
    base = resolve_path(run_dir, "work") / "comments.baseline.xml"
    kept = 0
    if base.exists():
        def _texts(path: Path) -> dict:
            try:
                r = etree.parse(str(path)).getroot()
            except etree.XMLSyntaxError:
                return {}
            return {c.get(ox.q("id")): _norm("".join(t.text or "" for t in c.iter(ox.q("t"))))
                    for c in r.iter(ox.q("comment"))}
        was, now = _texts(base), (_texts(cpath) if cpath.exists() else {})
        for cid, text in was.items():
            if cid not in now:
                problems.append(f"源文档原有的批注 {cid} 在回写后消失了")
            elif now[cid] != text:
                problems.append(f"源文档原有的批注 {cid} 的正文被改写了："
                                f"原「{text[:20]}…」→ 现「{now[cid][:20]}…」")
            else:
                kept += 1

    return {"name": "批注锚定完整", "pass": not problems, "comments": len(declared),
            "existing_comments_kept": kept,
            "empty_ranges": len(empty), "partial_ranges": partial,
            "problems": problems[:20]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="validate_docx.py", description="回写后四项校验")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--xsd", help="含 wml.xsd 的目录；不提供则只做结构约束校验")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    unpacked = resolve_path(run_dir, "unpacked")
    checks = [
        check_structure(unpacked, args.xsd),
        check_untracked(run_dir, unpacked),
        check_style(run_dir, unpacked),
        check_comments(run_dir, unpacked),
    ]
    ok = all(c["pass"] for c in checks)
    report = {**version_header(), "pass": ok, "checks": checks}
    out = resolve_path(run_dir, "work") / "validation.json"
    guard_write_path(out, run_dir)
    atomic_write_json(out, report)
    emit({"ok": ok, "pass": ok, "report": str(out),
          "failed": [c["name"] for c in checks if not c["pass"]],
          "checks": {c["name"]: c["pass"] for c in checks},
          "problems": [p for c in checks for p in c.get("problems", [])][:10]})
    return EX.OK if ok else EX.VALIDATE


if __name__ == "__main__":
    run_cli(main)
