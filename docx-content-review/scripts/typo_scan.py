#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错别字候选扫描（spec §9.7，M8；v1 默认关闭，接口已就位）。

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
  merge  合并裁定结果 → 追加到 issues-<chunk>.jsonl，类别记为 A1
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
from workspace import SKILL_ROOT, guard_write_path, load_config, resolve_path  # noqa: E402

DICT_DIR = SKILL_ROOT / "assets" / "dict"


def _load_pairs(name: str) -> list[tuple[str, str, str]]:
    p = DICT_DIR / name
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


def _load_list(name: str) -> set[str]:
    p = DICT_DIR / name
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def scan(run_dir: Path, cfg: dict, chunk_id: str | None) -> dict:
    tc = cfg.get("typo_check") or {}
    if not tc.get("enabled"):
        return {"enabled": False, "candidates": 0,
                "note": "typo_check.enabled=false（v1 默认关闭）"}

    typos = _load_pairs("common-typos.txt")
    shape = _load_pairs("confusion-shape.txt")
    pinyin = _load_pairs("confusion-pinyin.txt")
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
                    lo, hi = max(0, m.start() - 6), min(len(text), m.end() + 6)
                    cands.append({"pid": pid, "wrong": wrong, "right": right,
                                  "rule": "common-typos" if (wrong, right, why) in typos
                                          else "glossary-forbidden",
                                  "reason": why, "context": text[lo:hi],
                                  "original_text": text[lo:hi]})
            for a, b, _ in shape + pinyin:
                if len(a) != 1 or a not in text:
                    continue
                # 单字混淆集只在命中常见错词表之外时作为弱候选，交由 LLM 裁定
                continue
        cap = int(tc.get("max_typos_per_chunk") or 100)
        truncated = len(cands) > cap
        cands = cands[:cap]                    # 独立配额，不占用 max_issues_per_chunk
        batch = int(tc.get("batch_size") or 50)
        batches = [{"batch_id": f"t{i//batch + 1:02d}", "items": cands[i:i + batch]}
                   for i in range(0, len(cands), batch)]
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
    verdict_path = tdir / f"typos-{chunk_id}.verdicts.jsonl"
    if not verdict_path.exists():
        return {"merged": 0, "note": f"无裁定结果：{verdict_path}"}
    payload = read_json(tdir / f"typos-{chunk_id}.json", {}) or {}
    cands = {(c["pid"], c["wrong"], c["context"]): c for c in payload.get("candidates", [])}

    rows = []
    for v in read_jsonl(verdict_path):
        if str(v.get("verdict", "")).strip().upper() != "B":
            continue                      # 只有裁定「应为建议写法」才采纳
        c = cands.get((v.get("pid"), v.get("wrong"), v.get("context"))) or {}
        if not c:
            continue
        rows.append({
            "chunk_id": chunk_id, "pid": c["pid"], "category": "A1", "rule_id": "A1",
            "severity": "High", "original_text": c["original_text"],
            "suggested_text": c["original_text"].replace(c["wrong"], c["right"]),
            "evidence": (c.get("reason") or "错别字")[:25],
            "source": "typo_channel", "action": "revision",
        })
    out = resolve_path(run_dir, "issues") / f"issues-{chunk_id}.typos.jsonl"
    guard_write_path(out, run_dir)
    atomic_write_jsonl(out, rows)
    return {"merged": len(rows), "path": str(out),
            "note": "需再过 verify_span.py 与 filter_neverflag.py 两道闸门"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="typo_scan.py", description="错别字候选扫描（M8）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--chunk")
    p.add_argument("--config")
    p = sub.add_parser("merge")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--chunk", required=True)
    p.add_argument("--config")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    cfg = load_config(args.config)
    if args.cmd == "scan":
        emit({"ok": True, **scan(run_dir, cfg, args.chunk)})
    else:
        emit({"ok": True, **merge(run_dir, cfg, args.chunk)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
