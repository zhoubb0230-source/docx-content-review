#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics.json：调优的唯一依据（spec §10.5）。

核心质量指标是「负样本误报率 ≤5%」，而调整 prompt 的唯一依据是各闸门的丢弃率。
**没有这份数据，调优是盲的**——它是 M2 的必需品，不是运维锦上添花。

怎么读这份数据：
  neverflag_drop 接近 0        → 不改清单没写对
  second_pass_drop 超过 50%    → 第一遍审查过于宽松
  hallucination_drop 居高不下  → 分片正文里的 pid 标注方式有问题
  edit_gate_drop 集中在某一类  → 该类的 prompt 示例需要收紧

子命令
  collect   汇总各闸门产物 → output/metrics.json
  bump      累加一次 LLM 调用及其 token 用量（Agent 每次调用后执行）
  show      打印摘要
退出码：0 成功。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, emit, read_json, read_jsonl, run_cli, version_header,
)
from workspace import deliver_path, guard_write_path, load_run_config, resolve_path  # noqa: E402

COUNTER_PATH = "work/llm-usage.json"


def _usage(run_dir: Path) -> dict:
    return read_json(run_dir / COUNTER_PATH, {"calls": {}, "tokens": {"input": 0, "output": 0},
                                              "retries": {}}) or {}


def bump(run_dir: Path, pass_name: str, input_tokens: int, output_tokens: int,
         retry: str | None) -> dict:
    u = _usage(run_dir)
    u.setdefault("calls", {})
    u["calls"][pass_name] = int(u["calls"].get(pass_name, 0)) + 1
    u.setdefault("tokens", {"input": 0, "output": 0})
    u["tokens"]["input"] = int(u["tokens"].get("input", 0)) + max(0, input_tokens)
    u["tokens"]["output"] = int(u["tokens"].get("output", 0)) + max(0, output_tokens)
    if retry:
        u.setdefault("retries", {})
        u["retries"][retry] = int(u["retries"].get(retry, 0)) + 1
    u["updated_at"] = time.time()
    path = run_dir / COUNTER_PATH
    guard_write_path(path, run_dir)
    atomic_write_json(path, u)
    return u


def collect(run_dir: Path, cfg: dict) -> dict:
    idir = resolve_path(run_dir, "issues")
    gates = {"raw_candidates": 0, "hallucination_drop": 0, "neverflag_drop": 0,
             "edit_gate_drop": 0, "second_pass_drop": 0, "truncated_chunks": 0,
             "length_drop": 0, "unknown_category_drop": 0, "final_issues": 0}
    neverflag_by_rule: dict[str, int] = {}
    edit_by_category: dict[str, int] = {}

    for g in sorted(idir.glob("issues-*.gates.json")):
        d = read_json(g, {}) or {}
        gates["raw_candidates"] += int(d.get("raw") or 0)
        gates["hallucination_drop"] += int(d.get("hallucination_drop") or 0)
        gates["length_drop"] += int(d.get("length_drop") or 0)
        gates["unknown_category_drop"] += int(d.get("unknown_category") or 0)
        gates["edit_gate_drop"] += int(d.get("edit_gate_degrade") or 0)
        gates["neverflag_drop"] += int(d.get("neverflag_drop") or 0)
        if d.get("truncated"):
            gates["truncated_chunks"] += 1
        for k, v in (d.get("neverflag_by_rule") or {}).items():
            neverflag_by_rule[k] = neverflag_by_rule.get(k, 0) + int(v)

    for nf in sorted(idir.glob("issues-*.neverflag.json")):
        d = read_json(nf, {}) or {}
        gates["neverflag_drop"] += int(d.get("dropped") or 0)
        for k, v in (d.get("by_rule") or {}).items():
            neverflag_by_rule[k] = neverflag_by_rule.get(k, 0) + int(v)

    verified = list(read_jsonl(resolve_path(run_dir, "issues_verified")))
    gates["second_pass_drop"] = sum(1 for r in verified
                                    if (r.get("verify") or {}).get("result") == "drop")
    final = [r for r in verified if (r.get("verify") or {}).get("result") != "drop"]
    gates["final_issues"] = len(final)
    for r in verified:
        if r.get("gate_note") and not r.get("suggested_text"):
            edit_by_category[r.get("category", "?")] = edit_by_category.get(r.get("category", "?"), 0) + 1

    actions = {"revision": 0, "comment": 0, "report_only": 0}
    plan = read_json(resolve_path(run_dir, "patchlist"), {}) or {}
    actions["revision"] = len(plan.get("patches") or [])
    clist = read_json(resolve_path(run_dir, "work") / "commentlist.json", {}) or {}
    comments = clist.get("comments") or []
    # 说明修订理由的批注跟着修订走，不是另一个问题——算进 comment 会与 revision 重复计数
    actions["revision_comment"] = sum(1 for c in comments if c.get("kind") == "revision")
    actions["comment"] = len(comments) - actions["revision_comment"]
    actions["report_only"] = max(0, len(final) - actions["revision"] - actions["comment"])

    cdir = resolve_path(run_dir, "conflicts_candidate")
    conflicts = read_json(cdir / "index.json", {}) or {}
    verdicts = list(read_jsonl(resolve_path(run_dir, "conflicts_verified")))

    man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    u = _usage(run_dir)
    created = man.get("created_at")
    total_sec = None
    if created:
        from datetime import datetime

        try:
            total_sec = int((datetime.now().astimezone()
                             - datetime.fromisoformat(created)).total_seconds())
        except ValueError:
            total_sec = None

    payload = {
        **version_header(),
        "runid": man.get("runid"),
        "llm_calls": u.get("calls", {}),
        "tokens": u.get("tokens", {"input": 0, "output": 0}),
        "gates": gates,
        "gates_detail": {"neverflag_by_rule": neverflag_by_rule,
                         "edit_gate_by_category": edit_by_category},
        "actions": actions,
        "conflicts": {"candidates": conflicts.get("total", 0),
                      "by_rule": conflicts.get("by_rule", {}),
                      "adjudicated": len(verdicts),
                      "confirmed": sum(1 for v in verdicts if v.get("verdict") == "CONFLICT"),
                      "unsure": sum(1 for v in verdicts if v.get("verdict") == "UNSURE")},
        "retries": u.get("retries", {}),
        "chunking": {"chunks": len(idx.get("chunks") or []),
                     "single_pass": idx.get("single_pass"),
                     "token_estimated": idx.get("token_estimated"),
                     "tokenizer": idx.get("tokenizer"),
                     "total_text_tokens": idx.get("total_text_tokens")},
        "stats": man.get("stats", {}),
        "timing": {"total_sec": total_sec},
    }
    raw = gates["raw_candidates"] or 1
    payload["gate_rates"] = {
        k: round(gates[k] / raw, 4) for k in
        ("hallucination_drop", "neverflag_drop", "edit_gate_drop", "second_pass_drop")
    }
    path = deliver_path(run_dir, "metrics")
    guard_write_path(path, run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return payload


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="metrics.py", description="闸门丢弃率与调用量统计")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "show"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--config")
    p = sub.add_parser("bump")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--pass", dest="pass_name", required=True,
                   # 两条支线各有自己的计数：它们每片各多一次调用，
                   # 压测时要能单独算出耗时占比，混进主审查就看不出来了
                   choices=["pass0", "pass1_review", "pass1_extract", "pass2_verify",
                            "pass4", "typo", "pattern"])
    p.add_argument("--input-tokens", type=int, default=0)
    p.add_argument("--output-tokens", type=int, default=0)
    p.add_argument("--retry", choices=["json_parse", "chunk_failed", "chunk_skipped"])
    p.add_argument("--config")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    if args.cmd == "bump":
        u = bump(run_dir, args.pass_name, args.input_tokens, args.output_tokens, args.retry)
        emit({"ok": True, "calls": u["calls"], "tokens": u["tokens"]})
    elif args.cmd == "collect":
        m = collect(run_dir, load_run_config(run_dir, args.config))
        emit({"ok": True, "gates": m["gates"], "gate_rates": m["gate_rates"],
              "actions": m["actions"], "path": str(deliver_path(run_dir, "metrics"))})
    else:
        m = read_json(deliver_path(run_dir, "metrics"), {}) or {}
        emit({"ok": True, "gates": m.get("gates"), "gate_rates": m.get("gate_rates"),
              "llm_calls": m.get("llm_calls"), "actions": m.get("actions")})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
