#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批注回写：w:comment 六文件联动（spec §6.4）。

批注是逻辑审查的**主要交付出口**——L01–L24 中绝大多数动作是批注，因为模型
无法判断冲突双方孰对孰错，只能提示人工确认。砍掉批注等同于砍掉逻辑审查。

核心层与增强层必须分开对待：
  核心层  comments.xml + document.xml 中的 commentRangeStart/End/Reference
          + 关系文件 + 内容类型覆盖项   →  缺失则批注不可见，这层必须成功
  增强层  commentsExtended / commentsIds / commentsExtensible
          →  仅失去回复线程、已解决标记等特性，批注照常可见；
             写入失败时降级为仅核心层并记录，不得导致整体失败

一次性处理全部批注（3000 页可能上千条），避免反复解包/打包。

用法
  apply_comments.py plan  --run-dir <run>      # 生成批注清单
  apply_comments.py apply --run-dir <run>      # 六文件联动写入
退出码：0 成功；1 核心层失败；9 令牌失效。
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ooxml as ox  # noqa: E402
from _common import (  # noqa: E402
    conflict_admitted, EX, SEVERITY_PREFIX, atomic_write_json, die, emit, read_json, read_jsonl, rule_label,
    run_cli, version_header, warn,
)
from workspace import (  # noqa: E402
    Heartbeat, guard_write_path, lease_verify, load_config, resolve_path,
)

W = ox.W
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# 两侧是对**同一事实的互斥表述**的规则——只有这些才适合问「以哪一处为准」。
# 其余规则（区间自相矛盾、条目数不符、时序倒置、覆盖性缺失…）两侧不是竞争关系，
# 套用同一句文案会让评审人不知道要确认什么。
RIVAL_RULES = {"L01", "L02", "L04", "L05", "L06", "L07", "L12", "L14",
               "L17", "L20", "L21", "L22", "L23", "L28"}

CORE_PART = "word/comments.xml"
ENHANCED = {
    "word/commentsExtended.xml": (
        f"{REL_BASE}/commentsExtended",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"),
    "word/commentsIds.xml": (
        f"{REL_BASE}/commentsIds",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml"),
    "word/commentsExtensible.xml": (
        f"{REL_BASE}/commentsExtensible",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml"),
}


# --------------------------------------------------------------------------
def _revision_reason(head: str, rid: str, evidence: str, patch: dict) -> str:
    """修订处的批注正文：只回答「为什么改」。

    改成了什么，修订标记本身已经显示；评审人缺的是改动理由——
    没有理由的修订只能整批接受或整批拒绝，等于把判断重新丢回给人。
    没有 evidence 时才退回「原文 → 改为」，避免与修订标记重复。

    抬头只有类别标签。**不写「已在此处标为修订，请确认后接受或拒绝」**——
    批注就锚在修订上，这句话没有增加任何信息，只是把正文撑长、让人抓不住重点。
    """
    lines = [head]
    if evidence:
        lines.append(evidence)
    else:
        lines.append(f"「{patch.get('original_text','')}」→「{patch.get('suggested_text','')}」")
    lines.append(f"（检测规则 {rid}）")
    return "\n".join(x for x in lines if x)


def _anchor_or_whole(anchor: str, para_text: str) -> str:
    """锚点在段内不唯一时置空 → 由 `_anchor_paragraph` 退回整段。

    圈错一处比圈住整段更糟：评审人会照着高亮去找问题，而问题不在那里。
    **判定必须放在 plan 里，依据是原始段落文本**——apply 的时候段落已经被修订
    改过（被替换的那一处进了 w:del，不再参与定位），在那时判会得到与计划
    不一致的结论：计划说"说不清是哪一处"，落笔却精确圈住了剩下的那一处。
    `commentlist.json` 由 plan 写，`validate_docx` 也拿它做预期，三者必须一致。
    """
    a = (anchor or "").strip()
    return a if a and para_text.count(a) == 1 else ""


def build_plan(run_dir: Path, cfg: dict) -> dict:
    """批注来源：降级为批注的局部问题 + 裁定成立的逻辑冲突 + **每一处已落笔的修订**。"""
    logic_cfg = cfg.get("logic") or {}
    to_comment = bool(logic_cfg.get("coverage_rules_to_comment"))
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    items = []

    patchlist = read_json(resolve_path(run_dir, "patchlist"), {}) or {}
    demoted = {d["id"] for d in patchlist.get("demoted_to_comment", []) if d.get("id")}
    patch_by_id = {p["patch_id"]: p for p in patchlist.get("patches", [])}

    # 本步在 apply_revisions.py apply 之后跑（SKILL.md 第 8 步），因此溯源记录已存在。
    # 缺失时（只跑了 plan 就来做批注）退回「按计划都落笔了」，保持旧行为。
    prov = read_json(resolve_path(run_dir, "work") / "revision-provenance.json", {}) or {}
    applied = set(prov.get("applied") or []) if prov else set(patch_by_id)
    rev_anchor = {p["patch_id"]: p for p in prov.get("provenance") or []}

    def _rev_item(pid: str, patch: dict, severity: str, text: str, ref: str) -> dict:
        a = rev_anchor.get(patch["patch_id"]) or {}
        return {
            "comment_id": None, "pid": pid, "anchor": patch.get("original_text") or "",
            "revision": {"del_id": a.get("del_id"), "ins_id": a.get("ins_id")},
            "severity": severity, "text": text, "kind": "revision",
            "source": patch.get("source") or "issue", "ref": ref,
        }

    for rec in read_jsonl(resolve_path(run_dir, "issues_verified")):
        iid = rec.get("id")
        patch = patch_by_id.get(iid)
        if patch is not None and iid in applied:
            # 已落笔为修订：不是「不用批注」，而是必须换一种批注——说明改动理由
            cat = rec.get("category") or ""
            items.append(_rev_item(
                patch["pid"], patch, rec.get("severity") or "Medium",
                _revision_reason(rule_label(cat), rec.get("rule_id") or cat,
                                 rec.get("evidence") or "", patch), iid))
            continue
        # 计划了修订却没落笔（定位失败）时不能静默消失，照常出普通批注
        if rec.get("action") == "report_only" and iid not in demoted:
            continue                                  # 仅风格倾向：只进报告，不入文档
        cat = rec.get("category") or ""
        # P 类的抬头用规则包里的范式名（「风险条目描述范式」），比通用标签具体得多；
        # 可追溯标记也用规则号本身（P-RISK-01），否则评审人无从查是哪条范式。
        rid = rec.get("rule_id") or cat
        head = rec["pattern_name"] if cat == "P1" and rec.get("pattern_name") else rule_label(cat)
        lines = [head]
        if rec.get("evidence"):
            lines.append(rec["evidence"])
        if rec.get("suggested_text"):
            lines.append(f"建议改为：{rec['suggested_text']}")
        lines.append(f"（检测规则 {rid}）")
        items.append({
            "comment_id": None, "pid": rec["pid"],
            "anchor": _anchor_or_whole(rec.get("original_text"),
                                       (paras.get(rec["pid"]) or {}).get("text") or ""),
            "severity": rec.get("severity") or "Medium",
            "text": "\n".join(x for x in lines if x),
            "source": "issue", "ref": iid,
        })

    verdicts = {c.get("conflict_id"): c for c in
                read_jsonl(resolve_path(run_dir, "conflicts_verified"))}
    withheld: dict[str, int] = {}      # 未准入交付物的候选，按原因计数
    cdir = resolve_path(run_dir, "conflicts_candidate")
    for path in sorted(cdir.glob("conflicts-candidate.*.json")):
        for c in (read_json(path, {}) or {}).get("candidates", []):
            action = c.get("action")
            rule = c.get("rule")
            if action == "revision":
                patch = patch_by_id.get(c["conflict_id"])
                if patch is None:
                    continue          # 未准入或未生成补丁：由报告承载
                if c["conflict_id"] in applied:
                    items.append(_rev_item(
                        patch["pid"], patch, c.get("severity") or "Medium",
                        _revision_reason(rule_label(rule), rule, c.get("note") or "", patch),
                        c["conflict_id"]))
                    continue
                # 补丁未落笔：往下走普通批注，不能静默丢失
            if action == "report_only" and not (rule in {"L29", "L30", "L31", "L32"} and to_comment):
                continue
            v = verdicts.get(c["conflict_id"])
            ok, why = conflict_admitted(c, v)
            if not ok:
                withheld[why] = withheld.get(why, 0) + 1
                continue
            unsure = why == "unsure_critical"
            sides = c.get("sides") or []
            if not sides:
                continue
            # 文案统一：不要求模型判断哪一处是对的——它无法知道，那必然是幻觉源
            other = sides[1] if len(sides) > 1 else None
            # 两侧落在同一段落时不构成"此处/彼处"，否则会出现自己跟自己冲突的怪文案
            if other is not None and other.get("pid") == sides[0].get("pid"):
                other = None
            # 抬头只有类别标签。规则的通用描述（「同一术语出现多个不同定义」）
            # 与标签（「术语不一致」）说的是同一件事，下一行的 note 才是本条的具体内容。
            lines = [rule_label(rule)]
            if other and rule in RIVAL_RULES:
                where = " > ".join(other.get("heading_path") or []) or "文档其他位置"
                # 只引另一处。本处那一句批注范围已经精确圈住了，再抄一遍是重复。
                lines.append(f"与「{where}」（第 {other.get('page_hint')} 页）的描述不一致："
                             f"{other.get('text','')[:80]}")
                # 不判断哪一处是对的——文档之外的事实不在模型视野里
                lines.append("请确认以哪一处为准。")
            else:
                note = c.get("note") or ""
                subj = (c.get("subject") or "").strip()
                # note 里已含 subject 就不重复
                if subj and subj not in note:
                    note = f"「{subj}」：{note}" if note else f"涉及「{subj}」"
                if note:
                    lines.append(note)
                if other:
                    where = " > ".join(other.get("heading_path") or []) or "文档其他位置"
                    lines.append(f"相关位置：「{where}」第 {other.get('page_hint')} 页——"
                                 f"{other.get('text','')[:60]}")
                lines.append("请核对后确认。")
            if unsure:
                lines.append("（自动裁定为不确定，需人工确认）")
            lines.append(f"（检测规则 {rule}，详见审查报告）")
            # 锚点取该侧句子；取不到才退回整段（anchor 为空 = 整段）。
            # 这里曾写 `paras[pid]["text"][:40]`——40 字截断在 Word 里就是
            # 「只选中了前面一两行」，评审人看不出批注究竟在说这一段的哪部分。
            side_text = (sides[0].get("text") or "").strip()
            para_text = paras.get(sides[0]["pid"], {}).get("text") or ""
            anchor = _anchor_or_whole(side_text, para_text)
            items.append({
                "comment_id": None, "pid": sides[0]["pid"],
                "anchor": anchor,
                "severity": c.get("severity") or "Medium",
                "text": "\n".join(lines), "source": "conflict", "ref": c["conflict_id"],
            })

    for i, it in enumerate(items, 1):
        it["comment_id"] = i
    path = resolve_path(run_dir, "work") / "commentlist.json"
    guard_write_path(path, run_dir)
    atomic_write_json(path, {**version_header(), "comments": items})
    return {"comments": len(items), "path": str(path), "withheld": withheld,
            "withheld_total": sum(withheld.values())}


# --------------------------------------------------------------------------
def _ensure_rel(unpacked: Path, target: str, rtype: str) -> None:
    from lxml import etree

    rels = unpacked / "word" / "_rels" / "document.xml.rels"
    tree = etree.parse(str(rels))
    root = tree.getroot()
    name = target.split("/")[-1]
    for r in root.findall(f"{{{REL_NS}}}Relationship"):
        if r.get("Target") in (name, target):
            return
    used = {r.get("Id") for r in root}
    n = 1
    while f"rId{n}" in used:
        n += 1
    etree.SubElement(root, f"{{{REL_NS}}}Relationship",
                     Id=f"rId{n}", Type=rtype, Target=name)
    tree.write(str(rels), xml_declaration=True, encoding="UTF-8", standalone=True)


def _ensure_content_type(unpacked: Path, part: str, ctype: str) -> None:
    from lxml import etree

    ct = unpacked / "[Content_Types].xml"
    tree = etree.parse(str(ct))
    root = tree.getroot()
    pname = "/" + part
    for o in root.findall(f"{{{CT_NS}}}Override"):
        if o.get("PartName") == pname:
            return
    etree.SubElement(root, f"{{{CT_NS}}}Override", PartName=pname, ContentType=ctype)
    tree.write(str(ct), xml_declaration=True, encoding="UTF-8", standalone=True)


def _revision_nodes(para, rev: dict | None) -> tuple:
    """按 w:id 找到本处修订的 w:del / w:ins，返回该段内的 (起点, 终点) 节点。

    修订落地后原文已进 w:del，用原文再定位必然失败；改动理由的批注必须
    精确圈住这处改动，只能按 id 找。
    """
    if not rev:
        return None, None
    found = []
    for tag, key in ((ox.q("del"), "del_id"), (ox.q("ins"), "ins_id")):
        want = rev.get(key)
        if not want:
            continue
        for el in para.iter(tag):
            if el.get(ox.q("id")) == str(want):
                node = ox.top_level_node(para, el)
                if node is not None:
                    found.append(node)
                break
    if not found:
        return None, None
    kids = list(para)
    found.sort(key=kids.index)
    return found[0], found[-1]


def _anchor_paragraph(para, anchor: str, cid: int, rev: dict | None = None) -> bool:
    """在段落中放置 commentRangeStart / End / Reference。

    锚定属于核心层，不可省略——只写 comments.xml 而不在 document.xml 中锚定，
    批注在 Word 里根本不可见。范围边界一律取 w:p 的直接子节点：
    把 commentRangeEnd 插进 w:ins/w:del 内部会让它变成修订的一部分。
    """
    from lxml import etree

    start = etree.Element(ox.q("commentRangeStart")); start.set(ox.q("id"), str(cid))
    end = etree.Element(ox.q("commentRangeEnd")); end.set(ox.q("id"), str(cid))
    ref_run = etree.Element(ox.q("r"))
    ref = etree.SubElement(ref_run, ox.q("commentReference")); ref.set(ox.q("id"), str(cid))

    first, last = _revision_nodes(para, rev)
    # 锚点在段内出现多次时不猜是哪一处，退回整段。
    # 圈错一处比圈住整段更糟：评审人会照着高亮去找问题，而问题不在那里。
    if first is None and anchor and ox.span_count(para, anchor) != 1:
        anchor = ""
    if first is None and anchor:
        # 精确路径：把锚点切成独立的 run，范围就能落在字符边界上。
        # 不切的话，合并后整段只有一只 run 时「一句话有语病」会圈住整段两百多字。
        spans = ox.isolate_span(para, anchor)
        if spans:
            fp = spans[0].getparent()
            fp.insert(list(fp).index(spans[0]), start)
            lp = spans[-1].getparent()
            lp.insert(list(lp).index(spans[-1]) + 1, end)
            # 引用符放回段落层：锚点若在超链接里，引用符不该被算进链接
            top = ox.top_level_node(para, end)
            para.insert(list(para).index(top if top is not None else end) + 1, ref_run)
            return True
    if first is None:
        first, last = ox.comment_range_nodes(para, anchor)
    if first is None or last is None:
        # 段落里没有任何正文节点（真空段）：锚到 pPr 之后，范围为空但批注仍可见
        ppr = para.find(ox.q("pPr"))
        at = 1 if ppr is not None else 0
        para.insert(at, start)
        para.insert(at + 1, end)
        para.insert(at + 2, ref_run)
        return True

    para.insert(list(para).index(first), start)
    li = list(para).index(last)
    para.insert(li + 1, end)
    para.insert(li + 2, ref_run)
    return True


def apply_comments(run_dir: Path, cfg: dict) -> dict:
    from lxml import etree

    plan = read_json(resolve_path(run_dir, "work") / "commentlist.json", {}) or {}
    items = plan.get("comments") or []
    unpacked = resolve_path(run_dir, "unpacked")
    doc_xml = unpacked / "word" / "document.xml"
    author = (cfg.get("output") or {}).get("author_name") or "内容审查"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not items:
        return {"comments": 0, "anchored": 0, "enhanced_layer": "skipped", "failed": 0}

    tree = etree.parse(str(doc_xml))
    root = tree.getroot()
    paras = list(root.iter(ox.q("p")))
    by_pid = {f"p-{i:06d}": p for i, p in enumerate(paras, 1)}

    # ---- 核心层：comments.xml ----
    nsmap = {"w": W}
    croot = etree.Element(ox.q("comments"), nsmap=nsmap)
    anchored, failed = 0, []
    written = []
    for it in items:
        para = by_pid.get(it["pid"])
        if para is None:
            failed.append({**it, "reason": "pid 不存在"})
            continue
        cid = it["comment_id"]
        c = etree.SubElement(croot, ox.q("comment"))
        c.set(ox.q("id"), str(cid))
        c.set(ox.q("author"), author)
        c.set(ox.q("date"), stamp)
        c.set(ox.q("initials"), "CR")
        prefix = SEVERITY_PREFIX.get(it.get("severity") or "Medium", "[提示]")
        body = f"{prefix} {it['text']}"
        # 每行一个 w:p——批注正文里的换行必须是真正的段落，否则 Word 会挤成一行
        for line in body.split("\n"):
            p = etree.SubElement(c, ox.q("p"))
            r = etree.SubElement(p, ox.q("r"))
            t = etree.SubElement(r, ox.q("t"))
            t.text = line
            t.set(ox.XML_SPACE, "preserve")
        if _anchor_paragraph(para, it.get("anchor") or "", cid, it.get("revision")):
            anchored += 1
            written.append(it)

    cpath = unpacked / CORE_PART
    guard_write_path(cpath, run_dir)
    etree.ElementTree(croot).write(str(cpath), xml_declaration=True, encoding="UTF-8",
                                   standalone=True)
    guard_write_path(doc_xml, run_dir)
    tree.write(str(doc_xml), xml_declaration=True, encoding="UTF-8", standalone=True)
    _ensure_rel(unpacked, CORE_PART, f"{REL_BASE}/comments")
    _ensure_content_type(
        unpacked, CORE_PART,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml")

    # ---- 增强层：失败即降级，不得导致整体失败 ----
    enhanced, enh_error = "written", None
    try:
        para_ids = {it["comment_id"]: f"{(uuid.uuid4().int >> 96):08X}" for it in written}
        ext = etree.Element(f"{{{W15}}}commentsEx", nsmap={"w15": W15, "w": W})
        for it in written:
            e = etree.SubElement(ext, f"{{{W15}}}commentEx")
            e.set(f"{{{W15}}}paraId", para_ids[it["comment_id"]])
            e.set(f"{{{W15}}}done", "0")
        p = unpacked / "word/commentsExtended.xml"
        guard_write_path(p, run_dir)
        etree.ElementTree(ext).write(str(p), xml_declaration=True, encoding="UTF-8", standalone=True)

        ids = etree.Element(f"{{{W16CID}}}commentsIds", nsmap={"w16cid": W16CID, "w": W})
        for it in written:
            e = etree.SubElement(ids, f"{{{W16CID}}}commentId")
            e.set(f"{{{W16CID}}}paraId", para_ids[it["comment_id"]])
            e.set(f"{{{W16CID}}}durableId", f"{(uuid.uuid4().int >> 96):08X}")
        p = unpacked / "word/commentsIds.xml"
        guard_write_path(p, run_dir)
        etree.ElementTree(ids).write(str(p), xml_declaration=True, encoding="UTF-8", standalone=True)

        cex = etree.Element(f"{{{W16CEX}}}commentsExtensible",
                            nsmap={"w16cex": W16CEX, "w": W})
        p = unpacked / "word/commentsExtensible.xml"
        guard_write_path(p, run_dir)
        etree.ElementTree(cex).write(str(p), xml_declaration=True, encoding="UTF-8", standalone=True)

        for part, (rtype, ctype) in ENHANCED.items():
            _ensure_rel(unpacked, part, rtype)
            _ensure_content_type(unpacked, part, ctype)
    except Exception as exc:  # noqa: BLE001 - 增强层随 Word 版本演进，失败必须可降级
        enhanced, enh_error = "degraded", str(exc)[:200]
        for part in ENHANCED:
            (unpacked / part).unlink(missing_ok=True)
        warn(f"批注增强层写入失败，已降级为仅核心层（批注仍可见）：{enh_error}")

    return {"comments": len(items), "anchored": anchored, "failed": len(failed),
            "enhanced_layer": enhanced, "enhanced_error": enh_error,
            "failures": failed[:10]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="apply_comments.py", description="批注回写")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--config")
        p.add_argument("--session")
        p.add_argument("--generation", type=int)
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    cfg = load_config(args.config)
    if args.session:
        lease_verify(run_dir.parent, args.session, args.generation)
    if args.cmd == "plan":
        emit({"ok": True, **build_plan(run_dir, cfg)})
        return EX.OK
    with Heartbeat(run_dir.parent, args.session, args.generation,
                   int((cfg.get("concurrency") or {}).get("lease_minutes") or 30), "writeback"):
        res = apply_comments(run_dir, cfg)
    if res.get("comments") and not res.get("anchored"):
        die(EX.ERROR, "批注核心层写入失败：无任何批注被锚定，批注将不可见")
    emit({"ok": True, **res})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
