#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台账合并 + SQLite 索引构建/重建（spec §9.2、§9.3）。

Pass 3 不得将全量 facts 载入内存：3000 页文档台账条目可达 20 万条，且
「同 subject 不同 value」的朴素实现是 O(n²)。规则改以 GROUP BY + HAVING 表达。

**硬约束：facts/*.json 是权威源，ledger.db 只是可随时重建的派生索引。**
rebuild 必须能从 facts 文件全量重建且结果幂等；db 损坏时删库重建，绝不能成为单点。

子命令
  build     增量导入尚未入库的分片
  rebuild   删库并全量重建（幂等）
  stats     打印各类条目计数
  query     调试用：按 kind/subject 查询
退出码：0 成功；1 失败；9 令牌失效。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, die, emit, normalize_width, read_json, read_jsonl, run_cli, sha256_file,
)
from workspace import guard_write_path, resolve_path  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id        INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL,
  chunk_id  TEXT NOT NULL,
  pid       TEXT,
  subject   TEXT,
  value     TEXT,
  unit      TEXT,
  qualifier TEXT,
  scope     TEXT,
  meta      TEXT
);
CREATE INDEX IF NOT EXISTS ix_kind_subject ON facts(kind, subject);
CREATE INDEX IF NOT EXISTS ix_kind_scope   ON facts(kind, scope);
CREATE INDEX IF NOT EXISTS ix_pid          ON facts(pid);
CREATE INDEX IF NOT EXISTS ix_chunk        ON facts(chunk_id);
CREATE TABLE IF NOT EXISTS ingested (chunk_id TEXT PRIMARY KEY, rows INTEGER, sha TEXT);
"""

# facts-<chunk>.json 的 17 个字段 → 统一的扁平行。schema 固定，不允许自由发挥。
MAPPING = {
    "terms":        ("term", "term", "definition"),
    "acronyms":     ("acronym", "acronym", "expansion"),
    "entities":     ("entity", "name", None),
    "metrics":      ("metric", "subject", "value"),
    "positions":    ("position", "subject", "stance"),
    "objectives":   ("objective", "obj_id", "statement"),
    "initiatives":  ("initiative", "init_id", "statement"),
    "acceptance":   ("acceptance", "target", "criterion"),
    "dates":        ("date", "event", "value"),
    "versions":     ("version", "subject", "value"),
    "roles":        ("role", "role", "duty"),
    "xrefs":        ("xref", "type", "target"),
    "numbering":    ("numbering", "type", "label"),
    "commitments":  ("commitment", "subject", "statement"),
    "statuses":     ("status", "subject", "status"),
    "enumerations": ("enumeration", "claim", "count_declared"),
    "conclusions":  ("conclusion", "scope", "statement"),
}


# --------------------------------------------------------------------------
# 事实的幻觉闸门
#
# 审查通道早就有这道闸门（verify_span 的 hallucination_drop：pid 必须存在、
# original_text 必须在那个 pid 的正文里逐字找得到）。**事实通道一条都没有**——
# 模型写什么就入库什么，而全部 L 规则都建立在这份台账上。
# 后果是现场看到的样子：报告指着某一段说"目标与实测相差 N 倍"，
# 评审人翻到那一段，那句话根本不在那里。
#
# 两道判据，都只用确定性信息：
#   1. pid 必须在 paragraphs.jsonl 里（编造的 pid 一律丢）
#   2. 数值类事实的 value，其数字部分必须在该段正文里出现
# 第 2 条只对 metric 生效：statement / definition 这类值本来就是转述，不能逐字比。
DIGITS_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _norm_num_text(t: str) -> str:
    """比数字用的归一化：去千分位与空白，全角转半角。"""
    return normalize_width(t or "").replace(",", "").replace("，", "").replace(" ", "")


def value_grounded(kind: str, value: str | None, ptext: str) -> bool:
    """这条事实的数值在正文里找得到吗。找不到即丢。

    **只在能确定的时候判否**：value 里没有数字（纯文字的指标值）一律放行，
    否则会把"高/中/低""是/否"这类合法取值全部误杀。
    """
    if kind != "metric" or not value:
        return True
    nums = DIGITS_RE.findall(_norm_num_text(str(value)))
    if not nums:
        return True
    body = _norm_num_text(ptext)
    # 任意一个数字对得上就算落地：模型可能把「不超过 200」整串写进 value
    return any(n in body for n in nums)


def connect(run_dir: Path) -> sqlite3.Connection:
    db = resolve_path(run_dir, "ledger_db")
    guard_write_path(db, run_dir)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    return con


def flatten(chunk_id: str, data: dict, paras: dict | None = None,
            dropped: dict | None = None) -> list[tuple]:
    """展平成台账行。`paras` 给出时同时过幻觉闸门（见上）。"""
    rows = []
    for section, (kind, subj_key, val_key) in MAPPING.items():
        for item in (data.get(section) or []):
            if not isinstance(item, dict):
                continue
            subject = item.get(subj_key)
            value = item.get(val_key) if val_key else None
            if paras is not None:
                pid = item.get("pid")
                if not pid or pid not in paras:
                    if dropped is not None:
                        dropped["bad_pid"] = dropped.get("bad_pid", 0) + 1
                    continue
                if not value_grounded(kind, value, paras[pid]):
                    if dropped is not None:
                        dropped["value_not_found"] = dropped.get("value_not_found", 0) + 1
                    continue
            rows.append((
                kind, chunk_id, item.get("pid"),
                str(subject).strip() if subject is not None else None,
                str(value).strip() if value is not None else None,
                (str(item.get("unit")).strip() if item.get("unit") else None),
                (str(item.get("qualifier")).strip() if item.get("qualifier") else None),
                (str(item.get("scope")).strip() if item.get("scope") else None),
                json.dumps(item, ensure_ascii=False),
            ))
    return rows


def ingest(run_dir: Path, con: sqlite3.Connection, only: list[str] | None = None) -> dict:
    """增量导入。**判据是 facts 文件的 sha256，不是"这个分片入过库没有"。**

    分片失败重跑时 `facts-<chunk>.json` 会被整个改写。只按 chunk_id 判重的话，
    库里留的是上一轮的旧事实——而 Pass 3 的全部 L 规则都建立在这份台账上，
    结论会指向文档里已经不存在的内容。`ingested.sha` 这一列本来就是为此留的，
    早先写的是空串、也从不比对，等于没有。
    """
    fdir = resolve_path(run_dir, "facts")
    paras = {p["pid"]: (p.get("text") or "")
             for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    dropped: dict[str, int] = {}
    done = {r[0]: r[1] for r in con.execute("SELECT chunk_id, sha FROM ingested")}
    added, refreshed, total = 0, 0, 0
    for path in sorted(fdir.glob("facts-*.json")):
        cid = path.stem.split("-", 1)[1]
        if only and cid not in only:
            continue
        sha = sha256_file(path)
        if cid in done:
            if done[cid] == sha:
                continue
            # 内容变了：先清掉这一片的旧行，再重新导入（保持幂等）
            con.execute("DELETE FROM facts WHERE chunk_id=?", (cid,))
            refreshed += 1
        else:
            added += 1
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        rows = flatten(cid, data, paras, dropped)
        con.executemany(
            "INSERT INTO facts(kind,chunk_id,pid,subject,value,unit,qualifier,scope,meta) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO ingested(chunk_id,rows,sha) VALUES (?,?,?)",
                    (cid, len(rows), sha))
        total += len(rows)
    con.commit()
    # 丢弃数必须报出来：全部 L 规则都站在这份台账上，静默丢等于静默漏检
    return {"chunks_ingested": added, "chunks_refreshed": refreshed, "rows_added": total,
            "dropped": dropped}


def stats(con: sqlite3.Connection) -> dict:
    out = {k: c for k, c in con.execute("SELECT kind, COUNT(*) FROM facts GROUP BY kind")}
    out["_total"] = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    out["_chunks"] = con.execute("SELECT COUNT(*) FROM ingested").fetchone()[0]
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ledger.py", description="台账索引")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "rebuild", "stats"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--session")
        p.add_argument("--generation", type=int)
    p = sub.add_parser("query")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--subject")
    p.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    if args.cmd == "rebuild":
        db = resolve_path(run_dir, "ledger_db")
        guard_write_path(db, run_dir)
        db.unlink(missing_ok=True)
    con = connect(run_dir)
    try:
        if args.cmd in ("build", "rebuild"):
            res = ingest(run_dir, con)
            emit({"ok": True, **res, "stats": stats(con)})
        elif args.cmd == "stats":
            emit({"ok": True, "stats": stats(con)})
        else:
            q = "SELECT kind,pid,subject,value,unit,scope FROM facts WHERE kind=?"
            params: list = [args.kind]
            if args.subject:
                q += " AND subject=?"
                params.append(args.subject)
            q += f" LIMIT {int(args.limit)}"
            emit({"ok": True, "rows": [dict(zip(
                ["kind", "pid", "subject", "value", "unit", "scope"], r))
                for r in con.execute(q, params)]})
    finally:
        con.close()
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
