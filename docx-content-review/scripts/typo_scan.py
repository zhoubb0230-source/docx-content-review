#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错别字候选扫描（spec §9.7，M8）。

**为什么必须是独立通道**：错别字是唯一一个存在确定性候选生成手段的类别，
让 LLM 扫描它先天不利——
  1. LLM 对错别字有鲁棒性，这恰是缺陷：读到「帐号」「布署」会自动理解为正确词，
     注意力不会停留，它在做语义理解而非逐字比对；
  2. 配额挤占：一片 20 页可能有 20–40 个错别字，max_issues_per_chunk 瞬间打满；
  3. 重要性排挤：模型倾向报告「看起来更重要」的问题，错别字最不起眼。

与 D1 同构的三段式：脚本扫描出候选 → LLM 批量裁定（封闭选择题）→ 过闸门②③。
**脚本候选不得直接生成修订**（混淆集方法误报率高，LLM 裁定不可省略）。

子命令
  scan   扫描候选 → work/typos/typos-<chunk>.json（含供 LLM 裁定的分批 payload）
  merge  合并裁定结果 → work/issues/issues-<chunk>.typos.jsonl，类别记为 A1
  lint   只自检词表，不需要 run 目录（扩表后必跑）

**召回率上限 = common-typos.txt 的覆盖范围。** 未登录词检测（切词后查不到的词即可疑）
能突破这个上限，但需 5 万词级词表才有信噪比，内置词表远未达到该规模，故不启用
（config: typo_check.oov_detection）。扩表是提升召回的唯一杠杆，且是可枚举、
可收敛的资产——与 never-flag 同性质。**不要靠放宽闸门②的 A1 阈值来提召回。**

退出码：0 成功。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_jsonl, emit, levenshtein, read_json, read_jsonl,
    run_cli, version_header,
)
from workspace import SKILL_ROOT, guard_write_path, load_run_config, resolve_path  # noqa: E402

DICT_DIR = SKILL_ROOT / "assets" / "dict"


def _load_pairs(name: str, override: str | None = None) -> list[tuple[str, str, str]]:
    p = Path(override) if override else DICT_DIR / name
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0].strip(), parts[1].strip(),
                        parts[2].strip() if len(parts) > 2 else ""))
    return out


def _load_list(name: str, override: str | None = None) -> set[str]:
    p = Path(override) if override else DICT_DIR / name
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def lint(typos_path: str | None = None, traps_path: str | None = None) -> dict:
    """词表自检。扩表是提召回的唯一手段，也是最容易引入误报的地方——
    下面每一项都是实际踩过的坑，扩表后必跑。

    最要紧的是 `false_positive`：拿每条左串去撞 `typo-traps.txt` 里的合法句子。
    **只验"能查出错"不算验，还要验"不会把对的判成错"**——
    「按全」会被「按全流程」拆出来，「以经」会被「以经验」拆出来，
    这类条目让"扩表不抬高误报率"这个前提失效，必须改写或删除。
    """
    rows = [(w, r) for w, r, _ in _load_pairs("common-typos.txt", typos_path)]
    whitelist = _load_list("typo-whitelist.txt")
    traps = [t for t in _load_list("typo-traps.txt", traps_path)]
    seen, dup = set(), []
    for w, _ in rows:
        (dup.append(w) if w in seen else seen.add(w))
    problems = {
        # 建议与原文相同 → 闸门②直接判「建议与原文相同」，白白消耗一次裁定
        "same": [w for w, r in rows if w == r],
        # 单字左串会把「度」「作」「帐」这类高频字全部拉成候选，噪音淹没一切
        "single_char": [w for w, _ in rows if len(w) < 2],
        # 左串同时躺在白名单里 → 自相矛盾，扫描时永远被跳过
        "in_whitelist": [w for w, _ in rows if w in whitelist],
        "duplicated": dup,
        # 差异超出 A1 闸门（长度差 ≤2 且编辑距离 ≤3）→ 永远落不了笔，只会降级成批注
        "over_a1_gate": [w for w, r in rows
                         if abs(len(w) - len(r)) > 2 or levenshtein(w, r) > 3],
        # 负向语料：命中即误报。白名单能拦下的不算。
        "false_positive": [f"{w} ← 「{t}」" for w, _ in rows for t in traps
                           if w in t and not (w in whitelist
                                              or any(x in t and w in x for x in whitelist))],
    }
    return {"entries": len(rows), "whitelist": len(whitelist), "traps": len(traps),
            "ok": not any(problems.values()), "problems": problems}


def _unique_window(text: str, s: int, e: int, pad: int = 6, limit: int = 40) -> tuple[int, int]:
    """取一段包含 [s,e) 且**在本段内唯一**的上下文窗口。

    候选的 `original_text` 就是这个窗口，回写时按它定位。窗口不唯一时
    `locate_span` 会落到首处——「本期指标目标为 200ms，实测值为 1200ms。」
    这类段落里就会改错地方，而 `apply_revisions` 的唯一性守卫会直接拒绝落笔。
    所以宁可把窗口撑宽一点，也不要产出一条注定落不了笔的候选。

    撑到 limit 仍不唯一（整段是重复内容）就返回最宽的那个，由回写侧兜底。
    """
    while pad <= limit:
        lo, hi = max(0, s - pad), min(len(text), e + pad)
        if text.count(text[lo:hi]) == 1:
            return lo, hi
        if lo == 0 and hi == len(text):
            break
        pad += 6
    return max(0, s - limit), min(len(text), e + limit)


def scan(run_dir: Path, cfg: dict, chunk_id: str | None) -> dict:
    tc = cfg.get("typo_check") or {}
    if not tc.get("enabled"):
        return {"enabled": False, "candidates": 0,
                "note": "typo_check.enabled=false"}

    typos = _load_pairs("common-typos.txt")
    whitelist = _load_list("typo-whitelist.txt")
    glossary = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}

    forbidden: list[tuple[str, str, str]] = []
    for e in glossary.get("entries", []):
        pref = e.get("preferred") or e.get("key")
        for f in (e.get("forbidden") or []):
            form = f.get("form") if isinstance(f, dict) else f
            if form and pref and levenshtein(form, pref, cap=2) <= 2:
                forbidden.append((form, pref, "术语表登记的禁用写法"))

    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    chunks = [c for c in idx.get("chunks", []) if not chunk_id or c["chunk_id"] == chunk_id]
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    tdir = resolve_path(run_dir, "typos")
    guard_write_path(tdir, run_dir)
    tdir.mkdir(parents=True, exist_ok=True)

    total = 0
    results = []
    for c in chunks:
        cands = []
        for pid in c.get("review_pids") or c.get("pids") or []:
            p = paras.get(pid)
            if not p or p["is_code"]:
                continue
            text = p["text"]
            for wrong, right, why in typos + forbidden:
                if wrong not in text:
                    continue
                # 白名单命中即跳过：「帐篷」不应被改为「账篷」
                if any(w in text and wrong in w for w in whitelist):
                    continue
                if wrong in whitelist:
                    continue
                for m in re.finditer(re.escape(wrong), text):
                    lo, hi = _unique_window(text, m.start(), m.end())
                    cands.append({"tid": f"{c['chunk_id']}-{len(cands) + 1:03d}",
                                  "pid": pid, "wrong": wrong, "right": right,
                                  "rule": "common-typos" if (wrong, right, why) in typos
                                          else "glossary-forbidden",
                                  "reason": why, "context": text[lo:hi],
                                  # 窗口里可能不止一个 `wrong`（撑宽之后更容易），
                                  # 所以记下本处的偏移，merge 时按位置替换而不是全局 replace
                                  "wrong_at": m.start() - lo,
                                  "original_text": text[lo:hi]})
            # 单字混淆集（shape / pinyin）**刻意不用于生成候选**：
            # 按单字命中会把「度」「作」「帐」这类高频字全部拉成候选，噪音淹没一切。
            # 它要有信噪比，前提是先做分词 + 未登录词检测——而未登录词检测需要
            # 5 万词级词表，内置词表远未达到该规模（config: typo_check.oov_detection）。
            # 这两份词表目前只作为写 common-typos.txt 时的人工参照，不参与运行期判定。
        cap = int(tc.get("max_typos_per_chunk") or 200)
        truncated = len(cands) > cap
        cands = cands[:cap]                    # 独立配额，不占用 max_issues_per_chunk
        batch = int(tc.get("batch_size") or 50)
        groups = [cands[i:i + batch] for i in range(0, len(cands), batch)]

        # 主文件只给 merge 用；**分批题面单独落盘，子 Agent 只读自己那一批**。
        # 以前主文件里 candidates 与 batches[].items 是同一批数据的两份拷贝，
        # 200 个候选就是 8 万字符——子 Agent 一读整份，上下文当场见底。
        for old_batch in tdir.glob(f"typos-{c['chunk_id']}.t*.json"):
            old_batch.unlink()
        batches = []
        for n, g in enumerate(groups, 1):
            bid = f"t{n:02d}"
            bpath = tdir / f"typos-{c['chunk_id']}.{bid}.json"
            guard_write_path(bpath, run_dir)
            atomic_write_json(bpath, {
                **version_header(), "chunk_id": c["chunk_id"], "batch_id": bid,
                # 题面只要这四项：定位靠 tid，判断靠 context + 两种写法
                "items": [{"tid": x["tid"], "context": x["context"],
                           "wrong": x["wrong"], "right": x["right"]} for x in g]})
            batches.append({"batch_id": bid, "count": len(g)})

        payload = {**version_header(), "chunk_id": c["chunk_id"], "candidates": cands,
                   "truncated": truncated, "batches": batches,
                   "require_llm_adjudication": bool(tc.get("require_llm_adjudication", True))}
        path = tdir / f"typos-{c['chunk_id']}.json"
        guard_write_path(path, run_dir)
        atomic_write_json(path, payload)
        total += len(cands)
        results.append({"chunk_id": c["chunk_id"], "candidates": len(cands),
                        "batches": len(batches), "truncated": truncated})
    return {"enabled": True, "candidates": total, "chunks": len(results), "results": results}


def merge(run_dir: Path, cfg: dict, chunk_id: str) -> dict:
    """把 LLM 裁定结果并入该片的 issues。裁定为 A（原字正确）或「都不对」的一律丢弃。"""
    tc = cfg.get("typo_check") or {}
    tdir = resolve_path(run_dir, "typos")
    # 分批裁定（每批一个文件，可并行）+ 旧的整片单文件写法，两种都收
    verdict_files = sorted(tdir.glob(f"typos-{chunk_id}.t*.verdicts.jsonl"))
    single = tdir / f"typos-{chunk_id}.verdicts.jsonl"
    if single.exists():
        verdict_files.insert(0, single)
    if not verdict_files:
        return {"merged": 0, "note": f"无裁定结果：{tdir}/typos-{chunk_id}[.tNN].verdicts.jsonl"}
    payload = read_json(tdir / f"typos-{chunk_id}.json", {}) or {}
    cands = {(c["pid"], c["wrong"], c["context"]): c for c in payload.get("candidates", [])}
    by_tid = {c["tid"]: c for c in payload.get("candidates", []) if c.get("tid")}

    rows, seen = [], set()
    for v in [x for f in verdict_files for x in read_jsonl(f)]:
        if str(v.get("verdict", "")).strip().upper() != "B":
            continue                      # 只有裁定「应为建议写法」才采纳
        # tid 是首选定位方式：回填 pid/wrong/context 三个字段容易抄错，
        # 抄错的结果是这一条静默消失（对不上候选就丢），没有任何痕迹。
        c = by_tid.get(v.get("tid")) or cands.get(
            (v.get("pid"), v.get("wrong"), v.get("context"))) or {}
        if not c or c.get("tid") in seen:
            continue
        if c.get("tid"):
            seen.add(c["tid"])
        # 只替换裁定针对的那一处：窗口里可能出现两次同一个错词
        # （「1200ms」里就含着「200ms」），全局 replace 会顺手改掉不该改的那个。
        orig, at = c["original_text"], c.get("wrong_at")
        if isinstance(at, int) and orig[at:at + len(c["wrong"])] == c["wrong"]:
            sugg = orig[:at] + c["right"] + orig[at + len(c["wrong"]):]
        else:
            sugg = orig.replace(c["wrong"], c["right"], 1)
        rows.append({
            "chunk_id": chunk_id, "pid": c["pid"], "category": "A1", "rule_id": "A1",
            "severity": "High", "original_text": orig,
            "suggested_text": sugg,
            "evidence": (c.get("reason") or "错别字")[:25],
            "source": "typo_channel", "action": "revision",
        })
    out = resolve_path(run_dir, "issues") / f"issues-{chunk_id}.typos.jsonl"
    guard_write_path(out, run_dir)
    atomic_write_jsonl(out, rows)
    return {"merged": len(rows), "path": str(out),
            "note": "需再过 verify_span.py（--in/--out 指向本文件、--cap 用独立配额）"
                    "与 filter_neverflag.py（--file 指向本文件）"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="typo_scan.py", description="错别字候选扫描（M8）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--chunk")
    p.add_argument("--config")
    p = sub.add_parser("merge")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--chunk", help="不给 = 合并全部分片（阶段化流程用这个）")
    p.add_argument("--config")
    p = sub.add_parser("lint", help="自检词表，不需要 run 目录")
    # 允许指向别处的词表：技能目录在运行期只读，负向对照不该去改它
    p.add_argument("--typos", help="错词表路径；默认用内置的")
    p.add_argument("--traps", help="负向语料路径；默认用内置的")
    args = ap.parse_args(argv)
    if args.cmd == "lint":
        res = lint(getattr(args, "typos", None), getattr(args, "traps", None))
        emit({"ok": res["ok"], **res})
        return EX.OK if res["ok"] else EX.PARSE
    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    if args.cmd == "scan":
        emit({"ok": True, **scan(run_dir, cfg, args.chunk)})
    elif args.chunk:
        emit({"ok": True, **merge(run_dir, cfg, args.chunk)})
    else:
        res = [merge(run_dir, cfg, c["chunk_id"]) for c in
               (read_json(resolve_path(run_dir, "chunk_index"), {}) or {}).get("chunks", [])]
        emit({"ok": True, "chunks": len(res), "merged": sum(r["merged"] for r in res)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
