#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分片（spec §7.3、§7.5）。

判据是 token，不是页数：同样 22k tokens，紧排版约 30 页，稀排版可达约 60 页。
以 context_tokens（正文 + 指令 + 术语摘要 + 上文衔接 + 输出预留）作为切分判据。

不可拆分的原子块：表格、代码块、连续列表。单个超长表格独立成片。
每片携带 heading_path 全路径 + 前一片末尾 2 段（明确标注为参考上文，不在审查范围）。

用法
  chunk.py --run-dir <run> [--config <yaml>]
退出码：0 成功；1 失败。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tokenizer as tk  # noqa: E402
from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_text, die, emit, read_json, read_jsonl,
    run_cli, sha256_text, version_header,
)
from workspace import (  # noqa: E402
    claim_path, config_hash, guard_write_path, load_run_config, resolve_path,
)

CTX_HEADER = "【以下为上文参考，仅供理解，不在本次审查范围】"
CTX_FOOTER = "【上文参考结束。以下为本次审查范围】"


# --------------------------------------------------------------------------
def build_blocks(paras: list[dict]) -> list[dict]:
    """把段落聚成不可拆分的原子块：表格 / 代码块 / 连续列表 / 单段。"""
    blocks: list[dict] = []
    i = 0
    n = len(paras)
    while i < n:
        p = paras[i]
        if p["in_table"] and p.get("table_id") is not None:
            tid = p["table_id"]
            j = i
            while j < n and paras[j].get("table_id") == tid:
                j += 1
            blocks.append({"kind": "table", "table_id": tid, "paras": paras[i:j]})
            i = j
            continue
        if p["is_code"]:
            j = i
            while j < n and paras[j]["is_code"] and not paras[j]["in_table"]:
                j += 1
            blocks.append({"kind": "code", "paras": paras[i:j]})
            i = j
            continue
        if p.get("is_list"):
            j = i
            while j < n and paras[j].get("is_list") and not paras[j]["in_table"]:
                j += 1
            blocks.append({"kind": "list", "paras": paras[i:j]})
            i = j
            continue
        blocks.append({"kind": "heading" if p["is_heading"] else "para", "paras": [p]})
        i += 1
    return blocks


def render_block(b: dict) -> str:
    """渲染为送入子 Agent 的文本。每段带 pid，便于模型回引且脚本可反查。"""
    if b["kind"] == "table":
        rows: dict[int, list[dict]] = {}
        for p in b["paras"]:
            rows.setdefault(p.get("row_idx") if p.get("row_idx") is not None else -1, []).append(p)
        lines = [f"[表格 T{b['table_id']}]"]
        for ridx in sorted(rows):
            cells = rows[ridx]
            cells.sort(key=lambda c: (c.get("cell_idx") if c.get("cell_idx") is not None else 0,
                                      c["index"]))
            merged: dict[int, list[dict]] = {}
            for c in cells:
                merged.setdefault(c.get("cell_idx") or 0, []).append(c)
            parts = []
            for cidx in sorted(merged):
                txt = " ".join(x["text"].strip() for x in merged[cidx] if x["text"].strip())
                pid = merged[cidx][0]["pid"]
                parts.append(f"{txt or '—'}({pid})")
            lines.append(f"行{ridx + 1}: " + " | ".join(parts))
        return "\n".join(lines)

    lines = []
    for p in b["paras"]:
        txt = p["text"].strip()
        if not txt:
            continue
        if p["is_heading"]:
            lines.append(f"[{p['pid']}] {'#' * min(p['level'] or 1, 6)} {txt}")
        elif b["kind"] == "code":
            lines.append(f"[{p['pid']}] «代码/命令/配置，不做语言审查» {txt}")
        else:
            lines.append(f"[{p['pid']}] {txt}")
    return "\n".join(lines)


def block_tokens(b: dict, ratio: float) -> int:
    if "_tok" not in b:
        b["_tok"] = tk.count(render_block(b), ratio)
    return b["_tok"]


def text_para_count(paras: list[dict]) -> int:
    return sum(1 for p in paras if not p["in_table"] and not p["is_code"] and p["text"].strip())


def chunk_type_of(paras: list[dict], threshold: float) -> str:
    real = [p for p in paras if p["text"].strip()]
    if not real:
        return "table_only"
    tbl = sum(1 for p in real if p["in_table"])
    txt = len(real) - tbl
    if tbl == 0:
        return "text"
    return "table_only" if txt / len(real) < threshold else "mixed"


# --------------------------------------------------------------------------
def overhead_tokens(cfg: dict, has_glossary: bool) -> int:
    """单次调用里正文之外的开销。

    **术语表摘要不存在时不占预算**：`term_rules` 默认关闭，此时既不做 Pass 0
    也没有术语摘要可塞，却照样从预算里扣掉 4000——每片白白少装 4000 token 正文，
    等价于凭空多切出一批分片，而分片数就是调用数与工具轮次数。
    """
    ch = cfg.get("chunking") or {}
    return (int(ch.get("instruction_tokens") or 9000)
            + (int(ch.get("glossary_tokens") or 4000) if has_glossary else 0)
            + int(ch.get("output_reserve_tokens") or 8000))


def clear_stale_products(run_dir: Path, stale: list[str]) -> int:
    """作废这些 chunk_id 的全部产物。

    **不清就是静默跳片**：续跑判定是「文件存在性即状态」（workspace.chunk_done），
    重新分片后 chunk-0001 的内容已经换了，但旧的 issues-0001.jsonl / facts-0001.json
    还在，于是新的第 1 片一上来就被判为「已完成」——查漏的那部分正文再也不会被看到，
    而输出里没有任何痕迹。压制方向的 fail-open，与闸门③那类是同一种危险。
    """
    removed = 0
    for cid in stale:
        targets = [claim_path(run_dir, cid),
                   resolve_path(run_dir, "facts") / f"facts-{cid}.json"]
        targets += sorted(resolve_path(run_dir, "issues").glob(f"issues-{cid}.*"))
        targets += sorted(resolve_path(run_dir, "typos").glob(f"typos-{cid}.*"))
        targets += sorted(resolve_path(run_dir, "patterns").glob(f"patterns-{cid}.*"))
        for t in targets:
            if t.exists() and t.is_file():
                guard_write_path(t, run_dir)
                t.unlink()
                removed += 1
    return removed


def split(blocks: list[dict], cfg: dict, overhead: int) -> list[list[dict]]:
    ch = cfg.get("chunking") or {}
    ratio = float(ch.get("token_fallback_ratio") or 1.0)
    max_text = int(ch.get("max_text_tokens") or 15000)
    max_ctx = int(ch.get("max_context_tokens") or 100000)
    fill = float(ch.get("split_point_fill_ratio") or 0.7)
    budget = min(max_text, max(1000, max_ctx - overhead - 1000))  # 1000 = 上文衔接预算

    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_tok = 0
    for b in blocks:
        bt = block_tokens(b, ratio)
        if bt > budget:
            # 超长块（通常是大表格）独立成片，不切断
            if cur:
                chunks.append(cur)
                cur, cur_tok = [], 0
            chunks.append([b])
            continue
        # 主切点 H2、次切点 H3：装到 split_point_fill_ratio 之后在标题处断开，
        # 让分片对齐章节。这个比例定得越低，片数越多——每片平均只装到这个比例的预算。
        is_split_point = b["kind"] == "heading" and (b["paras"][0]["level"] or 9) <= 3
        if cur and (cur_tok + bt > budget or (is_split_point and cur_tok > budget * fill)):
            chunks.append(cur)
            cur, cur_tok = [], 0
        cur.append(b)
        cur_tok += bt
    if cur:
        chunks.append(cur)
    return chunks


def build(run_dir: Path, cfg: dict) -> dict:
    paras = list(read_jsonl(resolve_path(run_dir, "paragraphs")))
    if not paras:
        die(EX.ERROR, "paragraphs.jsonl 为空或不存在，请先执行 extract.py")

    ch = cfg.get("chunking") or {}
    ratio = float(ch.get("token_fallback_ratio") or 1.0)
    overlap_n = int(ch.get("overlap_paragraphs") or 2)
    single_limit = int(ch.get("single_pass_limit") or 22000)
    tbl_ratio = float(ch.get("table_only_text_ratio") or 0.10)
    has_glossary = bool((read_json(resolve_path(run_dir, "glossary_merged"), {}) or {})
                        .get("entries"))
    overhead = overhead_tokens(cfg, has_glossary)

    blocks = build_blocks(paras)
    body = "\n".join(render_block(b) for b in blocks)
    total_tokens = tk.count(body, ratio)

    # 单片模式判定：判据是 token，不是页数
    single_pass = total_tokens <= single_limit
    groups = [blocks] if single_pass else split(blocks, cfg, overhead)

    cdir = resolve_path(run_dir, "chunks")
    idx_path = resolve_path(run_dir, "chunk_index")
    guard_write_path(cdir, run_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    # 重新分片前先记下旧切法：内容变了的 chunk_id 必须连同它的产物一起作废，
    # 否则「文件存在性即状态」会让新的第 1 片顶着旧的 issues-0001 被判为已完成。
    prev_sig = {c["chunk_id"]: c.get("signature")
                for c in (read_json(idx_path, {}) or {}).get("chunks", [])}
    for old in cdir.glob("chunk-*.txt"):
        old.unlink()

    headings_doc = read_json(resolve_path(run_dir, "headings"), {}) or {}
    index = []
    prev_tail: list[dict] = []
    for i, grp in enumerate(groups, 1):
        cid = f"{i:04d}"
        gp = [p for b in grp for p in b["paras"]]
        hpath = gp[0]["heading_path"] if gp else []
        parts = []
        if prev_tail:
            parts.append(CTX_HEADER)
            parts.extend(f"[{p['pid']}] {p['text'].strip()}" for p in prev_tail if p["text"].strip())
            parts.append(CTX_FOOTER)
        parts.append("章节路径：" + (" > ".join(hpath) if hpath else "（文档开头）"))
        parts.extend(render_block(b) for b in grp)
        text = "\n".join(x for x in parts if x)

        ttok = tk.count("\n".join(render_block(b) for b in grp), ratio)
        ctok = ttok + overhead + tk.count("\n".join(p["text"] for p in prev_tail), ratio)
        ctype = chunk_type_of(gp, tbl_ratio)
        path = cdir / f"chunk-{cid}.txt"
        guard_write_path(path, run_dir)
        atomic_write_text(path, text)

        pages = [p["page_hint"] for p in gp if p.get("page_hint")]
        index.append({
            "chunk_id": cid,
            "path": str(path),
            "signature": sha256_text(text)[:16],
            "chunk_type": ctype,
            "text_tokens": ttok,
            "context_tokens": ctok,
            "pages_est": (max(pages) - min(pages) + 1) if pages else 0,
            "page_from": min(pages) if pages else None,
            "page_to": max(pages) if pages else None,
            "heading_path": hpath,
            "pids": [p["pid"] for p in gp],
            "review_pids": [p["pid"] for p in gp
                            if not p["is_code"] and not (p["in_table"] and (cfg.get("skip") or {})
                                                         .get("tables_language_check", True))],
            "context_pids": [p["pid"] for p in prev_tail],
            "para_count": len(gp),
            "table_ids": sorted({p["table_id"] for p in gp if p.get("table_id")}),
            "needs_review_call": ctype != "table_only",
            "oversized": ttok > int(ch.get("max_text_tokens") or 15000),
        })
        prev_tail = [p for p in gp if p["text"].strip() and not p["in_table"]][-overlap_n:]

    new_sig = {c["chunk_id"]: c["signature"] for c in index}
    stale = sorted(cid for cid, sig in prev_sig.items() if new_sig.get(cid) != sig)
    cleared = clear_stale_products(run_dir, stale)

    guard_write_path(idx_path, run_dir)
    payload = {
        **version_header(),
        "config_hash": config_hash(cfg),
        "max_issues_per_chunk": int(ch.get("max_issues_per_chunk") or 40),
        "overhead_tokens": overhead,
        "single_pass": single_pass,
        "total_text_tokens": total_tokens,
        "token_estimated": not tk.available(),
        "tokenizer": tk.mode(),
        "page_density": headings_doc.get("page_density"),
        "chunks": index,
    }
    atomic_write_json(idx_path, payload)

    for kind in ("issues", "facts"):
        resolve_path(run_dir, kind).mkdir(parents=True, exist_ok=True)

    return {
        "chunks": len(index),
        "single_pass": single_pass,
        "total_text_tokens": total_tokens,
        "token_estimated": not tk.available(),
        "tokenizer": tk.mode(),
        "review_calls": sum(1 for c in index if c["needs_review_call"]),
        "extract_calls": len(index),
        "table_only_chunks": sum(1 for c in index if c["chunk_type"] == "table_only"),
        "oversized_chunks": sum(1 for c in index if c["oversized"]),
        "max_context_tokens": max((c["context_tokens"] for c in index), default=0),
        "max_issues_per_chunk": int(ch.get("max_issues_per_chunk") or 40),
        "stale_chunks_cleared": len(stale),
        "stale_products_removed": cleared,
        "index": str(idx_path),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="chunk.py", description="分片")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    emit({"ok": True, **build(run_dir, load_run_config(run_dir, args.config))})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
