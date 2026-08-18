#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pass 4 冲突裁定的脚手架（spec §10、prompts/pass4-adjudicate.md）。

与 `verify_pass2.py` 是同一个形状，解决的也是同一个问题：
**待判集拼装、分批、结果归并全部由脚本做，Agent 只负责答题。**

以前这一步是「Agent 自己读 conflicts-candidate.*.json，逐条发起调用，逐条 append」。
两个后果，在小文档上都看不出来：

1. 候选文件要整份读进主 Agent 的上下文，才能拼出题面。800 页文档的候选可达几百条，
   读完之后**后面每一轮工具往返都要把它重算一遍**。
2. 逐条裁定 = 每条 1 次调用 + 前后 2 次心跳。而 prompt 自己写着「一次可批量处理若干条」，
   是流程描述比 prompt 更保守。

分批之后每个子 Agent 只读自己那一批、只写自己那个裁定文件，各批之间没有依赖，
可以并行；`collect` 再把它们归并成下游唯一认的 `conflicts-verified.jsonl`。

**`collect` 会把「有候选但没裁定」的条目显式报出来。** 漏答不是小事：
`_common.conflict_admitted` 对未裁定一律不准入（fail-closed，这是对的），
于是漏答表现为「这条冲突凭空消失」，报告里也看不出来。数字摆出来才能补跑。

子命令
  build    候选 → work/conflicts/batches/adjudicate-bNN.json（每批一个文件）
  collect  work/conflicts/verdicts/*.jsonl → work/conflicts-verified.jsonl
  status   只报数：候选、已裁定、缺裁定
退出码：0 成功 / 2 参数错 / 10 输入不可解析。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_jsonl, die, emit, read_json, read_jsonl,
    run_cli, version_header,
)
from workspace import (  # noqa: E402
    guard_write_path, load_run_config, resolve_path,
)

VERDICTS = ("CONFLICT", "NOT_CONFLICT", "UNSURE")


def load_candidates(run_dir: Path) -> list[dict]:
    """按 conflict_id 排序，保证分批可复现（同一个 run 重跑 build 得到同样的分批）。"""
    cdir = resolve_path(run_dir, "conflicts_candidate")
    if not cdir.exists():
        die(EX.PARSE, f"候选目录不存在：{cdir}", "先跑 detect_conflicts.py。")
    out = []
    for path in sorted(cdir.glob("conflicts-candidate.*.json")):
        for c in (read_json(path, {}) or {}).get("candidates", []):
            if c.get("conflict_id"):
                out.append(c)
    out.sort(key=lambda c: c["conflict_id"])
    return out


def _group(c: dict) -> dict:
    """题面。**只给两侧原文与位置，不给任何倾向性信息。**

    `note` 是脚本比对得出的客观差异描述（「目标值 200ms vs 实测值 1200ms」），
    照给；但 severity/action 这类"这条有多要紧"的信息不进题面——
    模型看到 Critical 会倾向于答 CONFLICT。
    """
    return {
        "conflict_id": c["conflict_id"],
        "rule_description": c.get("description") or "",
        "subject": c.get("subject") or "",
        "note": c.get("note") or "",
        "sides": [{"heading_path": s.get("heading_path") or [],
                   "page": s.get("page_hint"),
                   "text": s.get("text") or ""}
                  for s in (c.get("sides") or [])],
    }


def build(run_dir: Path, cfg: dict) -> dict:
    cands = load_candidates(run_dir)
    size = int((cfg.get("logic") or {}).get("adjudicate_batch_size") or 10)
    size = max(1, size)

    bdir = resolve_path(run_dir, "conflicts_batches")
    vdir = resolve_path(run_dir, "conflicts_verdicts")
    for d in (bdir, vdir):
        guard_write_path(d, run_dir)
        d.mkdir(parents=True, exist_ok=True)
    for old in bdir.glob("adjudicate-b*.json"):
        old.unlink()                      # 批数可能变少，先清再写

    batches = []
    for i in range(0, len(cands), size):
        bid = f"b{i // size + 1:02d}"
        groups = [_group(c) for c in cands[i:i + size]]
        path = bdir / f"adjudicate-{bid}.json"
        guard_write_path(path, run_dir)
        atomic_write_json(path, {**version_header(), "batch_id": bid, "groups": groups})
        batches.append(bid)

    return {"candidates": len(cands), "batches": len(batches),
            "batch_dir": str(bdir), "verdict_dir": str(vdir),
            "batch_pattern": "adjudicate-b<NN>.json",
            "verdict_pattern": "adjudicate-b<NN>.verdicts.jsonl",
            "note": "每批一个文件，各批无依赖可并行；裁定写 verdicts/ 下的同名文件，"
                    "写完跑 collect 归并"}


def _read_verdicts(run_dir: Path) -> tuple[dict, list[Path], int]:
    """收三处：分批裁定文件、旧的单文件写法、以及已有的 conflicts-verified.jsonl。

    最后一处是断点续跑的前提：上一轮已经归并过的裁定不能因为重跑 collect 而丢失。
    """
    vdir = resolve_path(run_dir, "conflicts_verdicts")
    files = sorted(vdir.glob("*.jsonl")) if vdir.exists() else []
    verified = resolve_path(run_dir, "conflicts_verified")
    sources = ([verified] if verified.exists() else []) + files
    rows, invalid = {}, 0
    for path in sources:
        for v in read_jsonl(path):
            cid = v.get("conflict_id")
            verdict = str(v.get("verdict") or "").strip().upper()
            if not cid:
                invalid += 1
                continue
            if verdict not in VERDICTS:
                # 无法解析的裁定**不写入**：留空 = 未裁定 = 不进交付物（fail-closed）。
                # 写成 UNSURE 会让 Critical 级的候选走上「保留并标注待确认」那条路，
                # 那是"模型拿不准"的待遇，不该给"这行根本没答对格式"。
                invalid += 1
                continue
            rows[cid] = {"conflict_id": cid, "verdict": verdict,
                         "note": (v.get("note") or "")[:120]}
    return rows, files, invalid


def collect(run_dir: Path, *, write: bool = True) -> dict:
    cands = {c["conflict_id"]: c for c in load_candidates(run_dir)}
    rows, files, invalid = _read_verdicts(run_dir)

    known = {cid: r for cid, r in rows.items() if cid in cands}
    unknown = sorted(set(rows) - set(cands))
    missing = sorted(set(cands) - set(known))

    out = resolve_path(run_dir, "conflicts_verified")
    if write:                     # status 只报数，不落盘
        guard_write_path(out, run_dir)
        atomic_write_jsonl(out, [known[cid] for cid in sorted(known)])

    by_verdict = {v: sum(1 for r in known.values() if r["verdict"] == v) for v in VERDICTS}
    return {"candidates": len(cands), "verdicts": len(known), "verdict_files": len(files),
            "by_verdict": by_verdict, "invalid_lines": invalid,
            "unknown_ids": len(unknown), "missing": len(missing),
            # 漏答的前 20 条报出来，便于只补跑缺的那几批而不是整轮重来
            "missing_ids": missing[:20], "output": str(out),
            "note": "missing 一律不进交付物（未裁定 = 不确定 = 无问题）；"
                    "有缺就补跑对应批次再 collect" if missing else ""}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="adjudicate_pass4.py", description="Pass 4 裁定脚手架")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "collect", "status"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--config")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    if args.cmd == "build":
        emit({"ok": True, **build(run_dir, cfg)})
    elif args.cmd == "collect":
        emit({"ok": True, **collect(run_dir)})
    else:
        res = collect(run_dir, write=False)
        emit({"ok": True, "candidates": res["candidates"], "verdicts": res["verdicts"],
              "missing": res["missing"], "by_verdict": res["by_verdict"]})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
