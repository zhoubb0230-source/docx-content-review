#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸门②：可验证性（spec §8 闸门②）。

三件事，全部是确定性判断，不问模型：
  1. 原文逐字校验：original_text 必须在该分片正文中 exact match（仅允许空白归一化）。
     不匹配 = 幻觉 = 直接丢弃，不做修补。
  2. 修改幅度校验：按错误类型分别校验，不使用统一的编辑距离阈值。
     不通过 → 降级为批注并清空 suggested_text（不是丢弃）。
  3. 长度校验 + 单片上限（抑制模型凑数）。

用法
  verify_span.py --run-dir <run> --chunk 0001 [--in <raw.jsonl>] [--config]
输入：work/issues/issues-<chunk>.raw.jsonl（Pass 1 子 Agent 原始输出）
输出：work/issues/issues-<chunk>.jsonl（过闸后）+ 同名 .gates.json（计数）
退出码：0 成功；10 输入不可解析。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    SkillError,
    EX, atomic_write_json, atomic_write_jsonl, die, emit, is_subsequence, levenshtein,
    normalize_ws, read_json, read_jsonl, run_cli,
)
from workspace import guard_write_path, load_run_config, resolve_path  # noqa: E402

SEVERITIES = ["Critical", "High", "Medium", "Low"]
A_CLASSES = {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"}
B_CLASSES = {"B1", "B2", "B3", "B4", "B5"}
C_CLASSES = {"C1", "C2"}
# P 类只有一个 category（范式不符），具体是哪条范式由 rule_id 承载——
# 规则号由用户的规则包定义，是开放集合；category 保持封闭枚举不变。
P_CLASSES = {"P1"}
VALID_CATEGORIES = A_CLASSES | B_CLASSES | C_CLASSES | P_CLASSES
# P 类依赖场景定位的准确性，与 L29–L32 同理，不配 High（references/patterns.md）
P_SEVERITY_CAP = "Medium"

DE_SET = set("的地得")
CONJUNCTIONS = [
    "虽然", "但是", "然而", "不过", "因此", "所以", "由于", "因为", "既然", "不但", "不仅",
    "而且", "反而", "并且", "以及", "或者", "只有", "只要", "即使", "尽管", "无论", "不管",
    "如果", "那么", "否则", "从而", "进而", "于是", "何况", "况且", "才", "就", "却", "还",
    "仍然", "同时", "此外", "另外", "首先", "其次", "最后",
]
UNIT_CHARS = set("元万亿个项次人天月年秒分时台套件条张次%‰°℃")
NUMERIC_OK = set("0123456789., 、·%/:：-—~～±<>≤≥+")


def strip_punct(s: str) -> str:
    return "".join(ch for ch in s if not unicodedata.category(ch).startswith("P")
                   and ch not in "％±≤≥＜＞")


def diff_chars(a: str, b: str) -> tuple[str, str]:
    """去掉公共前后缀后剩余的差异片段。"""
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    j = 0
    while j < len(a) - i and j < len(b) - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
        j += 1
    return a[i:len(a) - j], b[i:len(b) - j]


def _strip_conjunctions(s: str) -> str:
    for c in sorted(CONJUNCTIONS, key=len, reverse=True):
        s = s.replace(c, "")
    return normalize_ws(s)


def _only_conjunction_diff(orig: str, sugg: str) -> bool:
    """整串剥离关联词后必须完全相同。

    不能只看 diff 片段：「只有…就能」→「只要…就能」的 diff 片段是「有」→「要」，
    两个单字都不在关联词表里，按片段判会误判为不通过。
    """
    return _strip_conjunctions(orig) == _strip_conjunctions(sugg)


def _glossary_mapping_ok(orig: str, sugg: str, glossary: dict) -> bool:
    """L25/L26：差异必须完全落在 forbidden/variants → preferred 的映射上。"""
    for e in (glossary or {}).get("entries", []):
        pref = e.get("preferred") or e.get("key")
        forms = [f.get("form") if isinstance(f, dict) else f for f in (e.get("forbidden") or [])]
        forms += list(e.get("variants") or [])
        for form in [f for f in forms if f]:
            if form in orig and orig.replace(form, pref) == sugg:
                return True
    return False


def edit_gate(category: str, orig: str, sugg: str, cfg: dict, glossary: dict) -> tuple[bool, str]:
    """按类别校验修改幅度。返回 (是否通过, 说明)。"""
    if orig == sugg:
        return False, "建议与原文相同"
    da, db = diff_chars(orig, sugg)
    dist = levenshtein(orig, sugg)

    if category == "A1":
        ok = abs(len(orig) - len(sugg)) <= 2 and dist <= 3
        return ok, "A1：长度差≤2 且差异字符≤3" if ok else f"A1 超限（len差 {abs(len(orig)-len(sugg))}, 距离 {dist}）"
    if category == "A2":
        rest_a = set(da) - DE_SET
        rest_b = set(db) - DE_SET
        ok = not rest_a and not rest_b and (set(da) | set(db)) & DE_SET
        return ok, "A2：差异仅在 的/地/得" if ok else f"A2 差异超出 的地得（{da!r}→{db!r}）"
    if category == "A3":
        ok = normalize_ws(strip_punct(orig)) == normalize_ws(strip_punct(sugg))
        return ok, "A3：剥离标点后一致" if ok else "A3 剥离标点后文字仍不一致"
    if category == "A4":
        ok = is_subsequence(orig, sugg) and len(sugg) > len(orig)
        return ok, "A4：只增不删" if ok else "A4 非纯增补（原文不是建议的子序列）"
    if category == "A5":
        limit = min(12, int(len(orig) * 0.3))
        ok = dist <= max(limit, 1)
        return ok, f"A5：距离 {dist} ≤ {max(limit,1)}" if ok else f"A5 距离 {dist} 超过 {max(limit,1)}"
    if category == "A6":
        ok = _only_conjunction_diff(orig, sugg)
        return ok, "A6：差异仅限关联词" if ok else f"A6 差异超出关联词表（{da!r}→{db!r}）"
    if category == "A7":
        ok = is_subsequence(sugg, orig) and len(sugg) < len(orig)
        return ok, "A7：只删不增" if ok else "A7 非纯删减（建议不是原文的子序列）"
    if category == "A8":
        bad = [ch for ch in (da + db) if ch not in NUMERIC_OK and ch not in UNIT_CHARS
               and not ch.isascii()]
        ok = not bad
        return ok, "A8：差异仅限数字/单位/分隔符" if ok else f"A8 差异含非数字单位字符（{''.join(bad)[:10]}）"
    if category in ("L25", "L26"):
        ok = _glossary_mapping_ok(orig, sugg, glossary)
        return ok, "术语映射命中" if ok else "差异未落在术语表登记的映射上"
    return False, f"类别 {category} 不允许携带建议文本"


def fallback_gate(orig: str, sugg: str, cfg: dict) -> tuple[bool, str]:
    """兜底：任何类别的编辑距离超过 min(30, len×0.4) 一律降级。"""
    v = cfg.get("verification") or {}
    cap = min(int(v.get("max_edit_distance_abs") or 30),
              int(len(orig) * float(v.get("max_edit_distance_ratio") or 0.4)))
    cap = max(cap, 1)
    dist = levenshtein(orig, sugg)
    return dist <= cap, f"兜底距离 {dist} ≤ {cap}" if dist <= cap else f"兜底距离 {dist} 超过 {cap}"


# --------------------------------------------------------------------------
def severity_rank(s: str) -> int:
    return SEVERITIES.index(s) if s in SEVERITIES else len(SEVERITIES)


def process(run_dir: Path, chunk_id: str, raw_path: Path, cfg: dict,
            out_path: Path | None = None, cap_override: int | None = None) -> dict:
    """out_path/cap_override 供侧通道（错别字、范式）使用：它们各有独立配额，
    产物也必须落在各自的文件里——写回同一个 issues-<chunk>.jsonl 会互相覆盖。"""
    chunk_text = (resolve_path(run_dir, "chunks") / f"chunk-{chunk_id}.txt")
    if not chunk_text.exists():
        die(EX.ERROR, f"分片文本不存在：{chunk_text}")
    body = chunk_text.read_text(encoding="utf-8")
    body_norm = normalize_ws(body)

    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    meta = next((c for c in idx.get("chunks", []) if c["chunk_id"] == chunk_id), {})
    ctx_pids = set(meta.get("context_pids") or [])
    paras = {}
    for p in read_jsonl(resolve_path(run_dir, "paragraphs")):
        paras[p["pid"]] = p

    glossary = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}
    v = cfg.get("verification") or {}
    min_len = int(v.get("min_span_chars") or 4)
    max_len = int(v.get("max_span_chars") or 120)
    # P 类的跨度上限必须与 A/B 类分开。
    # `max_span_chars` 的判据是"跨度太长说明模型在圈整段"——那是针对"这句话哪里写错了"
    # 的类别。P 类问的是"这一类段落该有的要件齐不齐"，它的 original_text **按设计就是
    # 整段**（scan_patterns.merge）。两种语义共用一个上限的后果是确定的：
    # 真实文档里的风险条目、接口描述普遍超过 120 字，实测 155 字即被 length_drop 丢弃，
    # 且这个丢弃在报告里不露面。0 = 不限。
    p_max = int((cfg.get("pattern_review") or {}).get("max_span_chars") or 0)
    cap = int(cap_override or (cfg.get("chunking") or {}).get("max_issues_per_chunk") or 20)

    counters = {"raw": 0, "bad_schema": 0, "unknown_category": 0, "context_pid": 0,
                "hallucination_drop": 0, "length_drop": 0, "edit_gate_degrade": 0,
                "truncated": 0, "kept": 0}
    kept = []
    for rec in read_jsonl(raw_path):
        counters["raw"] += 1
        if not isinstance(rec, dict):
            counters["bad_schema"] += 1
            continue
        cat = (rec.get("category") or rec.get("rule_id") or "").strip().upper()
        orig = (rec.get("original_text") or "").strip()
        pid = (rec.get("pid") or "").strip()
        if cat not in VALID_CATEGORIES:
            counters["unknown_category"] += 1     # 闸门①：无法归类的一律丢弃
            continue
        if not orig or not pid:
            counters["bad_schema"] += 1
            continue
        if pid in ctx_pids:
            counters["context_pid"] += 1          # 上文参考区不在审查范围
            continue

        # ① 原文逐字校验
        if normalize_ws(orig) not in body_norm:
            counters["hallucination_drop"] += 1
            continue
        para = paras.get(pid)
        if para is not None and normalize_ws(orig) not in normalize_ws(para["text"]):
            counters["hallucination_drop"] += 1   # pid 与原文对不上，同样按幻觉处理
            continue

        # ③ 长度校验（上限按类别取，见上面 p_max 的说明）
        upper = (p_max or 10 ** 9) if cat in P_CLASSES else max_len
        if not (min_len <= len(orig) <= upper):
            counters["length_drop"] += 1
            continue

        sugg = (rec.get("suggested_text") or "").strip()
        gate_note = ""
        if sugg:
            if cat not in A_CLASSES and cat not in ("L25", "L26"):
                sugg, gate_note = "", f"{cat} 类不生成修订，已清空建议"
                counters["edit_gate_degrade"] += 1
            else:
                ok, gate_note = edit_gate(cat, orig, sugg, cfg, glossary)
                if ok:
                    ok2, note2 = fallback_gate(orig, sugg, cfg)
                    if not ok2:
                        ok, gate_note = False, note2
                if not ok:
                    sugg = ""                      # 降级为批注，不是丢弃
                    counters["edit_gate_degrade"] += 1

        sev = (rec.get("severity") or "").strip().capitalize()
        if sev not in SEVERITIES:
            sev = "High" if cat in A_CLASSES else "Medium"
        if cat in P_CLASSES and severity_rank(sev) < severity_rank(P_SEVERITY_CAP):
            sev = P_SEVERITY_CAP           # 规则包写 High 也压回 Medium
        ev = (rec.get("evidence") or "").strip()[:25]   # 禁止长理由
        # P 类的 rule_id 是规则包定义的开放标识（P-RISK-01），必须原样带下去；
        # 其余类别的 rule_id 恒等于 category。
        rid = ((rec.get("rule_id") or "").strip() or cat) if cat in P_CLASSES else cat
        # 范式名要进批注正文（ADR-019：批注说人话），随记录带下去
        extra = {"pattern_name": (rec.get("pattern_name") or "").strip()} \
            if cat in P_CLASSES else {}

        # 动作：默认由有无建议决定，但两种情形闸门不得擅自升级——
        #   ① 上游明确声明了 report_only（范式规则包的 action、或只缺可选要件时的降级）。
        #      早先这里无条件重算，把 report_only 覆盖成 comment，
        #      于是 scan_patterns 的"只缺可选要件不打扰评审人"与规则包里的
        #      `action: report_only` 全都失效。
        #   ② C 类：taxonomy.md 写死"不生成修订也不生成批注"，只进报告。
        action = "revision" if sugg else "comment"
        if (rec.get("action") or "").strip() == "report_only" or cat in C_CLASSES:
            action, sugg = "report_only", ""

        kept.append({
            "chunk_id": chunk_id,
            "pid": pid,
            "category": cat,
            "rule_id": rid,
            **extra,
            "severity": sev,
            "original_text": orig,
            "suggested_text": sugg,
            "evidence": ev,
            "heading_path": (para or {}).get("heading_path", []),
            "page_hint": (para or {}).get("page_hint"),
            "in_table": bool((para or {}).get("in_table")),
            "is_code": bool((para or {}).get("is_code")),
            "gate_note": gate_note,
            "gates": {"exact": True, "edit": "pass" if sugg else "degraded_or_na"},
            "action": action,
        })

    # 闸门③ 必须在单片上限之前执行：否则不改清单里的噪音会先占满 20 条配额，
    # 真问题反被截断掉。filter_neverflag.py 是这些规则的唯一实现，此处复用它。
    import filter_neverflag as nf

    groups, fbterms = nf._alias_groups(glossary), nf._fallback_terms(glossary)
    survivors, nf_hits = [], {}
    for r in kept:
        rule = nf.check(r, paras.get(r["pid"]), cfg, glossary, groups, fbterms)
        if rule:
            nf_hits[rule] = nf_hits.get(rule, 0) + 1
            continue
        survivors.append(r)
    counters["neverflag_drop"] = len(kept) - len(survivors)
    counters["neverflag_by_rule"] = nf_hits
    kept = survivors

    # ④ 单片上限：超出按 severity 排序截断，但为 B 类保留下限席位。
    #
    # 纯按 severity 截断在这里会退化成按类别截断：A 类恒 High、B 类恒 Medium，
    # 于是一片里只要 A 类满 20 条，B 类就一条也出不来——指代不明、歧义、主客颠倒
    # 会被错别字和标点整组挤掉。这与 spec §9.7 论证错别字必须独立配额的机制相同
    # （配额挤占 + 重要性排挤），只是受害者换成了 B 类。
    # B 类与 A 类同出一次调用，拆不成独立通道，因此改为保底席位。
    kept.sort(key=lambda r: (severity_rank(r["severity"]), r["pid"]))
    truncated = False
    if len(kept) > cap:
        floor = int((cfg.get("chunking") or {}).get("min_b_class_slots") or 0)
        b_items = [r for r in kept if r["category"] in B_CLASSES]
        # 席位数不得超过配额的一半：保底是为了让少数类不被整组挤掉，
        # 不是为了反过来让它独占。cap 小于席位数时（如 --cap 1）必须让位给高严重度项。
        reserved = min(len(b_items), floor, cap // 2)
        head = [r for r in kept if r["category"] not in B_CLASSES][:cap - reserved]
        # 去重键必须带上原文，与下面的分片内去重同源。只用 (pid, category) 会把
        # 同一段落里同一类别的**不同**问题折叠成一条——回填时被当成"已选过"跳过，
        # 于是配额没用满、真问题却被丢掉（实测 cap=3 只保留 2 条）。
        def key(r):
            return (r["pid"], r["category"], normalize_ws(r["original_text"]))
        picked = head + b_items[:reserved]
        picked_keys = {key(r) for r in picked}
        for r in kept:
            if len(picked) >= cap:
                break
            if key(r) not in picked_keys:
                picked.append(r)
                picked_keys.add(key(r))
        picked.sort(key=lambda r: (severity_rank(r["severity"]), r["pid"]))
        counters["truncated"] = len(kept) - len(picked)
        counters["b_class_reserved"] = reserved
        kept = picked
        truncated = True
    counters["kept"] = len(kept)

    # 分片内按 pid 去重（重叠区去重，保留首次出现）
    seen = set()
    dedup = []
    for r in kept:
        key = (r["pid"], r["category"], normalize_ws(r["original_text"]))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    counters["dedup_drop"] = len(kept) - len(dedup)

    out = Path(out_path) if out_path else resolve_path(run_dir, "issues") / f"issues-{chunk_id}.jsonl"
    guard_write_path(out, run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_jsonl(out, dedup)
    # 闸门统计按输出文件命名，侧通道各自成档，不覆盖主通道的 gates
    stem = out.name[:-len(".jsonl")] if out.name.endswith(".jsonl") else out.name
    gates_path = out.parent / f"{stem}.gates.json"
    guard_write_path(gates_path, run_dir)
    atomic_write_json(gates_path, {"chunk_id": chunk_id, "truncated": truncated, **counters})

    return {"chunk_id": chunk_id, "output": str(out), "count": len(dedup),
            "truncated": truncated, **counters}


# 三条通道各自的文件与配额。侧通道的输入输出是同一个文件（原地过闸），
# 而主通道是 raw → 正式产物；混用会让侧通道覆盖主通道，这是踩过的坑。
CHANNELS = {
    "main":     ("issues-{c}.raw.jsonl", "issues-{c}.jsonl",
                 ("chunking", "max_issues_per_chunk", 40)),
    "typos":    ("issues-{c}.typos.jsonl", "issues-{c}.typos.jsonl",
                 ("typo_check", "max_typos_per_chunk", 200)),
    "patterns": ("issues-{c}.patterns.jsonl", "issues-{c}.patterns.jsonl",
                 ("pattern_review", "max_pattern_issues_per_chunk", 40)),
}


def sweep(run_dir: Path, cfg: dict, channel: str) -> dict:
    """整轮过闸：一条命令扫完全部分片。

    阶段化之后，闸门不再由「领了这一片的子 Agent」顺手跑——它是纯脚本，
    由主 Agent 在一波结束后统一收口。按片各发一次工具调用，
    在 20 片的文档上就是 20 轮往返，而这一轮往返什么判断都不做。
    """
    idir = resolve_path(run_dir, "issues")
    raw_pat, out_pat, (sect, key, dflt) = CHANNELS[channel]
    cap = int((cfg.get(sect) or {}).get(key) or dflt)
    results, unparsable = [], []
    for cid in [c["chunk_id"] for c in
                (read_json(resolve_path(run_dir, "chunk_index"), {}) or {}).get("chunks", [])]:
        raw = idir / raw_pat.format(c=cid)
        if not raw.exists():
            continue                    # 这一片没有这条通道的产出，不是错误
        try:
            results.append(process(run_dir, cid, raw, cfg, idir / out_pat.format(c=cid), cap))
        except SkillError:
            # 半写文件（子 Agent 写到一半断了）只作废这一片，不能拖垮整轮：
            # 一个坏文件让整轮 --all 退出，等于让一次环境抖动废掉全部分片的过闸。
            raw.unlink()
            unparsable.append(cid)
    return {"channel": channel, "chunks": len(results),
            "unparsable": len(unparsable), "unparsable_chunks": unparsable[:20],
            "count": sum(r["count"] for r in results),
            "truncated": sum(1 for r in results if r.get("truncated")),
            "dropped": sum(sum(v for k, v in r.items()
                               if k.startswith("drop_") and isinstance(v, int))
                           for r in results)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="verify_span.py", description="闸门②可验证性")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--chunk")
    ap.add_argument("--all", action="store_true", help="扫全部分片（与 --channel 配合）")
    ap.add_argument("--channel", choices=sorted(CHANNELS), default="main",
                    help="--all 时决定读写哪一条通道的文件与配额")
    ap.add_argument("--in", dest="raw", help="原始 JSONL；默认 issues-<chunk>.raw.jsonl")
    ap.add_argument("--out", help="过闸后的输出；默认 issues-<chunk>.jsonl。"
                                  "侧通道（错别字/范式）必须指定，否则会覆盖主通道产物")
    ap.add_argument("--cap", type=int, help="本次的单片上限；默认 max_issues_per_chunk。"
                                            "侧通道用各自的独立配额")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    if args.all:
        emit({"ok": True, **sweep(run_dir, cfg, args.channel)})
        return EX.OK
    if not args.chunk:
        die(EX.USAGE, "要么给 --chunk，要么给 --all")
    raw = Path(args.raw) if args.raw else resolve_path(run_dir, "issues") / f"issues-{args.chunk}.raw.jsonl"
    if not raw.exists():
        die(EX.PARSE, f"原始输出不存在：{raw}",
            "子 Agent 的 Pass 1 输出应先写入该路径（一行一条 JSON，无代码围栏）。")
    emit({"ok": True, **process(run_dir, args.chunk, raw, cfg,
                                Path(args.out) if args.out else None, args.cap)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
