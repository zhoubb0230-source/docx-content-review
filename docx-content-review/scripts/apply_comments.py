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
    EX, atomic_write_json, die, emit, read_json, read_jsonl, run_cli, version_header, warn,
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

SEVERITY_PREFIX = {"Critical": "[严重]", "High": "[重要]", "Medium": "[提示]", "Low": "[提示]"}
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
def build_plan(run_dir: Path, cfg: dict) -> dict:
    """批注来源：降级为批注的局部问题 + 裁定成立的逻辑冲突。"""
    logic_cfg = cfg.get("logic") or {}
    to_comment = bool(logic_cfg.get("coverage_rules_to_comment"))
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    items = []

    demoted = {d["id"] for d in
               (read_json(resolve_path(run_dir, "patchlist"), {}) or {}).get("demoted_to_comment", [])
               if d.get("id")}
    revised = {p["patch_id"] for p in
               (read_json(resolve_path(run_dir, "patchlist"), {}) or {}).get("patches", [])}

    for rec in read_jsonl(resolve_path(run_dir, "issues_verified")):
        iid = rec.get("id")
        if iid in revised:
            continue                                  # 已落笔为修订，不再重复批注
        if rec.get("action") == "report_only" and iid not in demoted:
            continue                                  # 仅风格倾向：只进报告，不入文档
        body = rec.get("evidence") or ""
        items.append({
            "comment_id": None, "pid": rec["pid"], "anchor": rec.get("original_text") or "",
            "severity": rec.get("severity") or "Medium",
            "text": f"【{rec.get('category')}】{body}".strip(),
            "source": "issue", "ref": iid,
        })

    verdicts = {c.get("conflict_id"): c for c in
                read_jsonl(resolve_path(run_dir, "conflicts_verified"))}
    cdir = resolve_path(run_dir, "conflicts_candidate")
    for path in sorted(cdir.glob("conflicts-candidate.*.json")):
        for c in (read_json(path, {}) or {}).get("candidates", []):
            action = c.get("action")
            rule = c.get("rule")
            if action == "revision":
                continue                              # 由 apply_revisions 处理
            if action == "report_only" and not (rule in {"L29", "L30", "L31", "L32"} and to_comment):
                continue
            v = verdicts.get(c["conflict_id"])
            if v and v.get("verdict") == "NOT_CONFLICT":
                continue
            unsure = bool(v and v.get("verdict") == "UNSURE")
            sides = c.get("sides") or []
            if not sides:
                continue
            # 文案统一：不要求模型判断哪一处是对的——它无法知道，那必然是幻觉源
            other = sides[1] if len(sides) > 1 else None
            if other:
                where = " > ".join(other.get("heading_path") or []) or "文档其他位置"
                body = (f"{c['description']}：本处与「{where}」"
                        f"（第 {other.get('page_hint')} 页）的描述存在冲突。"
                        f"此处为：{sides[0].get('text','')[:60]}；"
                        f"彼处为：{other.get('text','')[:60]}。请确认以哪一处为准。")
            else:
                body = f"{c['description']}：{c.get('note','')}"
            if unsure:
                body += "（Pass 4 裁定为不确定，待人工确认）"
            items.append({
                "comment_id": None, "pid": sides[0]["pid"],
                "anchor": (paras.get(sides[0]["pid"], {}).get("text") or "")[:40],
                "severity": c.get("severity") or "Medium",
                "text": f"【{rule}】{body}", "source": "conflict", "ref": c["conflict_id"],
            })

    for i, it in enumerate(items, 1):
        it["comment_id"] = i
    path = resolve_path(run_dir, "work") / "commentlist.json"
    guard_write_path(path, run_dir)
    atomic_write_json(path, {**version_header(), "comments": items})
    return {"comments": len(items), "path": str(path)}


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


def _anchor_paragraph(para, anchor: str, cid: int) -> bool:
    """在段落中放置 commentRangeStart / End / Reference。

    锚定属于核心层，不可省略——只写 comments.xml 而不在 document.xml 中锚定，
    批注在 Word 里根本不可见。
    """
    from lxml import etree

    start = etree.Element(ox.q("commentRangeStart")); start.set(ox.q("id"), str(cid))
    end = etree.Element(ox.q("commentRangeEnd")); end.set(ox.q("id"), str(cid))
    ref_run = etree.Element(ox.q("r"))
    ref = etree.SubElement(ref_run, ox.q("commentReference")); ref.set(ox.q("id"), str(cid))

    runs = [r for r in ox.para_runs(para) if ox.run_is_plain(r)]
    first = last = None
    if anchor and runs:
        loc = ox.locate_span(para, anchor)
        if loc:
            s, e, rs = loc
            pos = 0
            for r in rs:
                t = ox.run_text(r)
                if first is None and pos + len(t) > s:
                    first = r
                if pos < e:
                    last = r
                pos += len(t)
    if first is None:
        first = runs[0] if runs else None
    if last is None:
        last = runs[-1] if runs else None
    if first is None or last is None:
        # 空段落：锚到段落本身（pPr 之后）
        ppr = para.find(ox.q("pPr"))
        at = 1 if ppr is not None else 0
        para.insert(at, start)
        para.insert(at + 1, end)
        para.insert(at + 2, ref_run)
        return True

    fp, lp = first.getparent(), last.getparent()
    fp.insert(list(fp).index(first), start)
    li = list(lp).index(last)
    lp.insert(li + 1, end)
    lp.insert(li + 2, ref_run)
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
        p = etree.SubElement(c, ox.q("p"))
        r = etree.SubElement(p, ox.q("r"))
        t = etree.SubElement(r, ox.q("t"))
        prefix = SEVERITY_PREFIX.get(it.get("severity") or "Medium", "[提示]")
        t.text = f"{prefix} {it['text']}"
        t.set(ox.XML_SPACE, "preserve")
        if _anchor_paragraph(para, it.get("anchor") or "", cid):
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
