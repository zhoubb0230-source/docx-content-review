#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manifest 读写、断点续跑、租约心跳（spec §11.3.3、§11.5、§11.6）。

manifest.json **不是权威状态**，只是派生视图：多会话并发更新单一共享文件必然
互相覆盖。分片是否完成，一律由 issues-NNNN.jsonl / facts-NNNN.json 是否存在判定；
manifest 随时可由目录扫描重建，且重建结果幂等。

主 Agent 只读 stats 字段，不读 chunks 数组全文。

子命令
  rebuild    扫描目录重建 manifest（manifest 丢失或损坏时的恢复手段）
  stats      只输出统计数字（主 Agent 用）
  stage      设置当前阶段
  heartbeat  续租（Pass 4 等脚本不在运行的阶段，Agent 必须在每次 LLM 调用前后执行）
  doctor     按 §11.6 故障恢复矩阵体检并修复可自动修复项
退出码：0 成功；9 令牌失效。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, emit, now_iso, read_json, run_cli, version_header,
)
from workspace import (  # noqa: E402
    chunk_done, claim_path, guard_write_path, lease_heartbeat, lease_status, lease_verify,
    list_chunk_ids, load_config, resolve_path,
)


def rebuild(run_dir: Path, cfg: dict) -> dict:
    man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    metas = {c["chunk_id"]: c for c in idx.get("chunks", [])}
    prev = {c["id"]: c for c in man.get("chunks", [])}
    max_attempts = int((cfg.get("retry") or {}).get("max_attempts_per_chunk") or 3)

    chunks = []
    for cid in list_chunk_ids(run_dir):
        meta = metas.get(cid, {})
        need_issues = meta.get("chunk_type") != "table_only"
        old = prev.get(cid, {})
        attempts = int(old.get("attempts") or 0)
        if chunk_done(run_dir, cid, need_issues=need_issues):
            status = "done"
        elif attempts >= max_attempts:
            status = "skipped"      # 必须在最终报告的「未覆盖范围」中列出
        elif old.get("status") == "failed":
            status = "failed"
        else:
            status = "pending"
        issues_path = resolve_path(run_dir, "issues") / f"issues-{cid}.jsonl"
        facts_path = resolve_path(run_dir, "facts") / f"facts-{cid}.json"
        n_issues = sum(1 for _ in issues_path.open(encoding="utf-8")) if issues_path.exists() else 0
        facts = read_json(facts_path, {}) or {}
        n_facts = sum(len(v) for v in facts.values() if isinstance(v, list))
        chunks.append({"id": cid, "status": status, "issues": n_issues, "facts": n_facts,
                       "attempts": attempts, "last_error": old.get("last_error"),
                       "finished_at": old.get("finished_at") or (now_iso() if status == "done" else None),
                       "chunk_type": meta.get("chunk_type")})

    stats = {
        "total_chunks": len(chunks),
        "done": sum(1 for c in chunks if c["status"] == "done"),
        "failed": sum(1 for c in chunks if c["status"] == "failed"),
        "pending": sum(1 for c in chunks if c["status"] == "pending"),
        "skipped": sum(1 for c in chunks if c["status"] == "skipped"),
    }
    man.update({**version_header(), "chunks": chunks, "stats": stats,
                "updated_at": now_iso()})
    man.setdefault("stage", "pass1")
    path = resolve_path(run_dir, "manifest")
    guard_write_path(path, run_dir)
    atomic_write_json(path, man)
    return {"stats": stats, "stage": man.get("stage")}


def mark(run_dir: Path, cid: str, status: str, error: str | None) -> dict:
    man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
    chunks = man.setdefault("chunks", [])
    rec = next((c for c in chunks if c["id"] == cid), None)
    if rec is None:
        rec = {"id": cid, "attempts": 0}
        chunks.append(rec)
    rec["status"] = status
    if status == "failed":
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["last_error"] = (error or "")[:200]
    elif status == "done":
        rec["finished_at"] = now_iso()
    path = resolve_path(run_dir, "manifest")
    guard_write_path(path, run_dir)
    atomic_write_json(path, man)
    return {"chunk_id": cid, "status": status, "attempts": rec.get("attempts")}


def doctor(run_dir: Path, cfg: dict, fix: bool) -> dict:
    """§11.6 故障恢复矩阵。所有恢复动作幂等，且不触碰源文档。"""
    findings, fixed = [], []
    work = resolve_path(run_dir, "work")

    for tmp in work.rglob("*.tmp"):
        findings.append(f"分片半写残留：{tmp.name}")
        if fix:
            tmp.unlink(missing_ok=True)
            fixed.append(f"删除 {tmp.name}")

    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    metas = {c["chunk_id"]: c for c in idx.get("chunks", [])}
    for cid in list_chunk_ids(run_dir):
        cp = claim_path(run_dir, cid)
        if not cp.exists():
            continue
        done = chunk_done(run_dir, cid, need_issues=metas.get(cid, {}).get("chunk_type") != "table_only")
        if done:
            findings.append(f"claim 残留但产物已存在：{cid}")
            if fix:
                cp.unlink(missing_ok=True)
                fixed.append(f"回收 claim {cid}（标记完成，不重跑）")

    man_path = resolve_path(run_dir, "manifest")
    if not man_path.exists() or read_json(man_path) is None:
        findings.append("manifest.json 丢失或损坏")
        if fix:
            rebuild(run_dir, cfg)
            fixed.append("已由目录扫描重建 manifest")

    db = resolve_path(run_dir, "ledger_db")
    if db.exists():
        import sqlite3

        try:
            con = sqlite3.connect(str(db))
            con.execute("PRAGMA quick_check").fetchone()
            con.close()
        except sqlite3.DatabaseError:
            findings.append("ledger.db 损坏")
            if fix:
                db.unlink(missing_ok=True)
                fixed.append("已删库，可用 ledger.py rebuild 从 facts 全量重建")

    owner = read_json(resolve_path(run_dir, "owner"))
    if resolve_path(run_dir, "owner").exists() and owner is None:
        findings.append("owner.json 损坏")
        if fix:
            man = read_json(man_path, {}) or {}
            gen = int(man.get("generation") or 0) + 1
            atomic_write_json(resolve_path(run_dir, "owner"),
                              {"session_id": None, "generation": gen, "stage": None})
            fixed.append(f"视为无主，纪元重建为 {gen}")

    if not resolve_path(run_dir, "work").exists():
        findings.append("work/ 目录缺失")
    for name, kind in (("source-copy", None), ("paragraphs.jsonl", "paragraphs")):
        if kind and not resolve_path(run_dir, kind).exists():
            findings.append(f"{name} 缺失，需重跑对应 Pass")

    return {"findings": findings, "fixed": fixed, "healthy": not findings}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="state.py", description="状态与断点续跑")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("rebuild", "stats"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--config")
    p = sub.add_parser("stage")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--session")
    p.add_argument("--generation", type=int)
    p.add_argument("--config")
    p = sub.add_parser("mark")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--chunk", required=True)
    p.add_argument("--status", required=True, choices=["done", "failed", "pending", "skipped"])
    p.add_argument("--error")
    p.add_argument("--config")
    p = sub.add_parser("heartbeat")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--generation", type=int)
    p.add_argument("--stage")
    p.add_argument("--config")
    p = sub.add_parser("doctor")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--fix", action="store_true")
    p.add_argument("--config")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    cfg = load_config(getattr(args, "config", None))
    if args.cmd == "rebuild":
        emit({"ok": True, **rebuild(run_dir, cfg)})
    elif args.cmd == "stats":
        man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
        if not man.get("stats"):
            man = {**man, **rebuild(run_dir, cfg)}
        emit({"ok": True, "stage": man.get("stage"), "stats": man.get("stats", {}),
              "lease": lease_status(run_dir.parent)})
    elif args.cmd == "stage":
        if args.session:
            lease_verify(run_dir.parent, args.session, args.generation)
        man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
        man["stage"] = args.value
        man["updated_at"] = now_iso()
        guard_write_path(resolve_path(run_dir, "manifest"), run_dir)
        atomic_write_json(resolve_path(run_dir, "manifest"), man)
        emit({"ok": True, "stage": args.value})
    elif args.cmd == "mark":
        emit({"ok": True, **mark(run_dir, args.chunk, args.status, args.error)})
    elif args.cmd == "heartbeat":
        owner = lease_heartbeat(run_dir.parent, args.session, args.generation,
                                int((cfg.get("concurrency") or {}).get("lease_minutes") or 30),
                                args.stage)
        emit({"ok": True, "expires_at": owner["expires_at"], "generation": owner["generation"]})
    elif args.cmd == "doctor":
        emit({"ok": True, **doctor(run_dir, cfg, args.fix)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
