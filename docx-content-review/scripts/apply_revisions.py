#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修订回写：w:ins / w:del（spec §6.3、D3、D9）。

能力刻意受限——**只做行内文本替换，不做段落删除、不做段落合并、不做段落新增**。
这是把回写风险降到最低的设计选择，不是未完成的功能。

两段式（spec §11.3.7b）：先生成完整补丁清单（可断点重建），再一次性应用。
  plan   读 issues-verified.jsonl + 冲突候选 → work/patchlist.json
  apply  应用补丁清单到 unpacked/word/document.xml，并落盘 rPr 溯源记录

退出码：0 成功；1 失败；8 定位失败超阈值；9 令牌失效。
"""
from __future__ import annotations

import argparse
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ooxml as ox  # noqa: E402
from _common import (  # noqa: E402
    conflict_admitted, EX, atomic_write_json, die, emit, read_json, read_jsonl, run_cli, version_header,
)
from workspace import (  # noqa: E402
    Heartbeat, guard_write_path, lease_verify, load_run_config, resolve_path,
)

A_CLASSES = {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"}


def build_plan(run_dir: Path, cfg: dict) -> dict:
    """落笔门槛固定为 conservative：只有 A 类 + 有 suggested_text + 过编辑距离
    + 二次复核通过，才生成修订。L25/L26 由权威术语表唯一确定，同样可落笔。"""
    out_cfg = cfg.get("output") or {}
    max_rev = int(out_cfg.get("max_revisions") or 2000)

    patches, demoted = [], []
    for rec in read_jsonl(resolve_path(run_dir, "issues_verified")):
        cat = rec.get("category") or ""
        sugg = (rec.get("suggested_text") or "").strip()
        verdict = (rec.get("verify") or {}).get("result") or rec.get("verify_result")
        if not sugg or cat not in A_CLASSES:
            continue
        if (cfg.get("verification") or {}).get("enable_second_pass", True) and verdict != "pass":
            demoted.append({"id": rec.get("id"), "reason": f"二次复核未通过（{verdict}）"})
            continue
        patches.append({
            "patch_id": rec.get("id") or f"P-{len(patches)+1:05d}",
            "pid": rec["pid"], "category": cat,
            "original_text": rec["original_text"], "suggested_text": sugg,
            # 错别字通道会给出"段内第几处"。带着它，跨度不必撑宽到唯一
            **({"occurrence": rec["occurrence"]} if isinstance(rec.get("occurrence"), int) else {}),
            "source": "issue",
        })

    cdir = resolve_path(run_dir, "conflicts_candidate")
    verified = {c.get("conflict_id"): c for c in read_jsonl(resolve_path(run_dir, "conflicts_verified"))}
    for path in sorted(cdir.glob("conflicts-candidate.L2[56].json")):
        for c in (read_json(path, {}) or {}).get("candidates", []):
            sug = c.get("suggest") or {}
            if c.get("action") != "revision" or not sug.get("suggested_text"):
                continue
            v = verified.get(c["conflict_id"])
            ok, why = conflict_admitted(c, v)
            if not ok:
                demoted.append({"id": c["conflict_id"], "reason": why})
                continue
            patches.append({
                "patch_id": c["conflict_id"], "pid": c["sides"][0]["pid"],
                "category": c["rule"], "original_text": sug["original_text"],
                "suggested_text": sug["suggested_text"], "source": "conflict",
                # 术语规范化的语义是"这一段里的这个写法全部换掉"，不是"改某一处"
                "all_occurrences": bool(sug.get("all_occurrences")),
            })

    # 修订总数超限时超出部分降级为批注（超大量修订会让 Word 打开缓慢甚至无响应）
    overflow = []
    if len(patches) > max_rev:
        overflow = patches[max_rev:]
        patches = patches[:max_rev]

    plan = {**version_header(), "patches": patches,
            "demoted_to_comment": demoted + [{"id": p["patch_id"], "reason": "超过 max_revisions 上限"}
                                             for p in overflow],
            "max_revisions": max_rev, "overflow": len(overflow)}
    path = resolve_path(run_dir, "patchlist")
    guard_write_path(path, run_dir)
    atomic_write_json(path, plan)
    return {"patches": len(patches), "demoted": len(plan["demoted_to_comment"]),
            "overflow": len(overflow), "path": str(path)}


# --------------------------------------------------------------------------
def apply_plan(run_dir: Path, cfg: dict) -> dict:
    from lxml import etree

    plan = read_json(resolve_path(run_dir, "patchlist"), {}) or {}
    patches = plan.get("patches") or []
    author = (cfg.get("output") or {}).get("author_name") or "内容审查"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    doc_xml = resolve_path(run_dir, "unpacked") / "word" / "document.xml"
    if not doc_xml.exists():
        die(EX.ERROR, f"未找到 {doc_xml}")
    tree = etree.parse(str(doc_xml))
    root = tree.getroot()

    paras = list(root.iter(ox.q("p")))
    by_pid = {f"p-{i:06d}": p for i, p in enumerate(paras, 1)}
    rid = ox.max_revision_id(root) + 1000

    applied, failed, provenance = [], [], []
    for patch in patches:
        para = by_pid.get(patch["pid"])
        if para is None:
            failed.append({**patch, "reason": "pid 不存在"})
            continue

        # 唯一性守卫：`locate_span` 取的是首个匹配，跨度在段内出现多次时
        # "改哪一处"没有依据——问题记录里只有 pid 与 original_text，没有偏移量。
        # 「本期指标目标为 200ms，实测值为 1200ms。」里改「200ms」，改中的是
        # 目标值还是实测值取决于 find 而不是取决于判定。**不确定即不落笔**：
        # 未落笔的条目会照常出普通批注（见 apply_comments），不会静默消失。
        #
        # 例外是术语规范化（L25/L26）：那里的语义本来就是"这一段里的这个写法
        # 全部换掉"，不存在"改哪一处"的问题。它带 all_occurrences 标记，
        # 走下面的循环逐处替换——顺带修掉一个存量缺陷：原实现只换首处，
        # 剩下的原样留着，文档会变成半规范化状态。
        every = bool(patch.get("all_occurrences"))
        occ = ox.span_count(para, patch["original_text"])
        # 第三条出路：候选自己说得清是第几处（错别字通道按段内序号给出）。
        # 有序号就不存在"改哪一处"的疑问，跨度因此可以缩到错字本身——
        # 撑宽跨度换唯一性的代价是修订与批注圈住一大片，评审人看不出改了什么。
        nth = patch.get("occurrence")
        nth = int(nth) if isinstance(nth, int) or (isinstance(nth, str) and nth.isdigit()) else None
        if occ > 1 and not every and nth is None:
            failed.append({**patch, "reason": f"原文在该段落中出现 {occ} 次，无法唯一定位，未落笔"})
            continue
        if nth is not None and nth >= occ:
            failed.append({**patch, "reason": f"指定的第 {nth + 1} 处不存在（该段共 {occ} 处），未落笔"})
            continue

        marks: list[tuple[str, str]] = []
        produced: list[str] = []
        src_rpr_key = ""
        while True:
            # 带序号时只处理指定的那一处；处理完 original 已进 w:del、
            # 不再参与定位，所以下一轮自然找不到，循环只跑一次
            loc = ox.locate_span(para, patch["original_text"], nth or 0)
            if loc is None:
                break
            start, end, runs = loc
            parts = ox.split_for_span(runs, start, end)
            if not parts or not parts["target"]:
                break

            anchor = parts["anchor"]
            parent = anchor.getparent()
            insert_at = list(parent).index(anchor)
            src_rpr_key = ox.rpr_key(anchor)

            # 1) 前段：保留，rPr 深拷贝自其来源 run
            new_nodes = []
            for src, text in parts["head"]:
                new_nodes.append(ox.new_run(ox.clone_rpr(src), text))
            # 2) 被删段：包进 w:del，w:t → w:delText，rPr 深拷贝自各自来源 run
            del_el = etree.Element(ox.q("del"))
            del_id = rid
            del_el.set(ox.q("id"), str(rid)); rid += 1
            del_el.set(ox.q("author"), author)
            del_el.set(ox.q("date"), stamp)
            for src, text in parts["target"]:
                del_el.append(ox.new_run(ox.clone_rpr(src), text, deleted=True))
            new_nodes.append(del_el)
            # 3) 新增段：包进 w:ins，继承首个受影响 run 的 rPr
            ins_el = etree.Element(ox.q("ins"))
            ins_id = rid
            ins_el.set(ox.q("id"), str(rid)); rid += 1
            ins_el.set(ox.q("author"), author)
            ins_el.set(ox.q("date"), stamp)
            ins_el.append(ox.new_run(copy.deepcopy(ox.clone_rpr(parts["target"][0][0])),
                                     patch["suggested_text"]))
            new_nodes.append(ins_el)
            # 4) 后段：保留
            for src, text in parts["tail"]:
                new_nodes.append(ox.new_run(ox.clone_rpr(src), text))

            for r in parts["affected"]:
                if r.getparent() is not None:
                    r.getparent().remove(r)
            for offset, node in enumerate(new_nodes):
                parent.insert(min(insert_at + offset, len(parent)), node)

            produced += [ox.rpr_key(r) for n in new_nodes
                         for r in ([n] if n.tag == ox.q("r") else list(n.iter(ox.q("r"))))]
            marks.append((str(del_id), str(ins_id)))
            # 换完一处后原文已进 w:del，而 para_runs 跳过修订标记内的 run，
            # 所以下一轮 locate_span 自然找到下一处，不需要额外的偏移记账。
            if not every:
                break

        if not marks:
            failed.append({**patch, "reason": "原文在段落中定位失败"})
            continue

        # del_id / ins_id 让 apply_comments 能把「修订原因」批注**精确**锚在这处改动上：
        # 改动落地后原文已进了 w:del，按原文再定位一次必然失败（见 ooxml.para_runs）。
        # 整段替换时锚在首处，批注正文说明的是这个写法本身，不针对某一处。
        provenance.append({"patch_id": patch["patch_id"], "pid": patch["pid"],
                           "del_id": marks[0][0], "ins_id": marks[0][1],
                           "occurrences": len(marks),
                           "source_rpr": src_rpr_key, "produced_rpr": produced,
                           "consistent": all(k == src_rpr_key for k in produced)})
        applied.append(patch["patch_id"])

    # 禁止输出任何格式修订标记（D9 第 2 条）——防御性检查，正常路径不会产生
    fmt = sorted(set(ox.iter_format_changes(root)))
    if fmt:
        die(EX.VALIDATE, f"回写产生了格式修订标记：{fmt}（违反 D9），已中止且未落盘")

    guard_write_path(doc_xml, run_dir)
    tree.write(str(doc_xml), xml_declaration=True, encoding="UTF-8", standalone=True)

    prov_path = resolve_path(run_dir, "work") / "revision-provenance.json"
    guard_write_path(prov_path, run_dir)
    atomic_write_json(prov_path, {**version_header(), "applied": applied,
                                  "failed": failed, "provenance": provenance,
                                  "author": author})
    inconsistent = [p for p in provenance if not p["consistent"]]
    return {"applied": len(applied), "failed": len(failed),
            "rpr_inconsistent": len(inconsistent),
            "failures": failed[:10], "provenance": str(prov_path)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="apply_revisions.py", description="修订回写")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--config")
        p.add_argument("--session")
        p.add_argument("--generation", type=int)
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    if args.session:
        lease_verify(run_dir.parent, args.session, args.generation)

    if args.cmd == "plan":
        emit({"ok": True, **build_plan(run_dir, cfg)})
        return EX.OK
    with Heartbeat(run_dir.parent, args.session, args.generation,
                   int((cfg.get("concurrency") or {}).get("lease_minutes") or 30), "writeback"):
        res = apply_plan(run_dir, cfg)
    emit({"ok": True, **res})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
