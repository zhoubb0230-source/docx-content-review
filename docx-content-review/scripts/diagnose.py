#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一份**不含任何正文**的诊断包，用于把审查现场反馈给技能维护者。

真实语料是优化这个技能的唯一有效输入，而语料通常不能外传。这个脚本抽出
「排障需要、但不泄露内容」的那一层：结构指纹 + 各环节的计数与丢弃分布。

**硬保证：输出里不含文档正文、标题文字、术语、批注内容。** 不是靠约定，
是靠 `_assert_no_text()` 逐个字符串去撞 paragraphs.jsonl —— 撞上就以退出码 8 终止。

它能回答的问题，正是靠计数才能回答的那几个：
  - 漏斗在哪一节收窄（模型没查出来 / 闸门丢了 / 复核淘汰了，处置完全不同）
  - 事实抽取的质量（各类计数 + 幻觉闸门丢了多少）
  - 这份文档的编号方案、表格形态、章节结构是否落在脚本的假设之内
  - 哪几条错词表条目、哪几条 L 规则贡献了绝大多数条目（误报通常极度集中）

用法
  diagnose.py --run-dir <run> [--out <路径>]        # 默认写 <run>/output/diagnose.json
  diagnose.py --run-dir <run> --format md          # 同时打印可直接粘贴的摘要
退出码：0 成功；1 失败；8 自检发现疑似正文泄漏。
"""
from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, die, emit, read_json, read_jsonl, run_cli, version_header,
)
from workspace import guard_write_path, resolve_path, load_run_config, stage_units, STAGES  # noqa: E402

# 允许出现在诊断包里的自由文本：规则号、类别号、词表条目、样式类别。
# 除此之外的字符串都要过 _assert_no_text。
SAFE_KEYS = {"rule", "category", "wrong", "right", "kind", "scheme", "style_class",
             "unit", "modality", "status", "type", "verdict", "action", "severity"}
LABEL_RE = re.compile(r"(图|表)\s*([0-9]+)(?:\s*[-–—.－]\s*([0-9]+))?")
SECTION_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)+)")


def _style_class(name: str | None) -> str:
    """样式只报**类别**，不报名字——自定义样式名常带单位或项目代号。"""
    n = (name or "").lower()
    for pat, cls in (("toc", "toc"), ("caption", "caption"), ("heading", "heading"),
                     ("标题", "heading"), ("题注", "caption"), ("目录", "toc"),
                     ("code", "code"), ("quote", "quote"), ("list", "list")):
        if pat in n:
            return cls
    return "other" if n else "none"


def _hist(values: list[int], buckets: list[int]) -> dict:
    out = {f"<={b}": 0 for b in buckets}
    out[f">{buckets[-1]}"] = 0
    for v in values:
        for b in buckets:
            if v <= b:
                out[f"<={b}"] += 1
                break
        else:
            out[f">{buckets[-1]}"] += 1
    return out


def structure(run_dir: Path) -> dict:
    """文档的结构指纹。脚本的假设全部落在这几项上——不符即是误报的来源。"""
    paras = list(read_jsonl(resolve_path(run_dir, "paragraphs")))
    heads = (read_json(resolve_path(run_dir, "headings"), {}) or {}).get("headings", [])

    # 编号方案：章-序（图3-7）还是全篇流水（图33）。ADR-047 的误报就出在这里
    chapter, flat = 0, 0
    for p in paras:
        for m in LABEL_RE.finditer(p.get("text") or ""):
            if m.group(3):
                chapter += 1
            else:
                flat += 1
    numbered_headings = sum(1 for h in heads if SECTION_RE.match(h.get("text") or ""))

    # 表格形态：平行表（表头相同的多张表）是 L06 误报的来源
    rows_per_table: dict[int, set] = collections.defaultdict(set)
    cols_per_table: dict[int, set] = collections.defaultdict(set)
    header_sig: dict[int, str] = {}
    for p in paras:
        tid = p.get("table_id")
        if tid is None:
            continue
        rows_per_table[tid].add(p.get("row_idx"))
        cols_per_table[tid].add(p.get("cell_idx"))
        if p.get("row_idx") == 0:
            # 表头**只留形状**：列数与各列字数，不留文字
            header_sig[tid] = header_sig.get(tid, "") + f"{len((p.get('text') or '').strip())},"
    sig_groups = collections.Counter(header_sig.values())
    parallel = sum(n for sig, n in sig_groups.items() if n >= 2 and sig)

    styles = collections.Counter(_style_class(p.get("style")) for p in paras)
    depth = collections.Counter(h.get("level") for h in heads)
    per_chapter = collections.Counter(tuple(p.get("heading_path") or [])[:1] for p in paras)

    return {
        "paragraphs": len(paras),
        "chars": sum(len(p.get("text") or "") for p in paras),
        "headings": len(heads),
        "heading_depth": {str(k): v for k, v in sorted(depth.items(), key=lambda x: (x[0] is None, x[0]))},
        "numbered_headings": numbered_headings,
        "paragraphs_per_chapter": _hist(list(per_chapter.values()), [10, 30, 80, 200]),
        "tables": len(rows_per_table),
        "table_rows": _hist([len(v) for v in rows_per_table.values()], [3, 8, 20, 50]),
        "table_cols": _hist([len(v) for v in cols_per_table.values()], [2, 4, 8, 16]),
        # 表头形状相同的表有几张 —— 这个数大，L06 的跨表判据就是关键
        "parallel_tables": parallel,
        "in_table_paragraphs": sum(1 for p in paras if p.get("in_table")),
        "code_paragraphs": sum(1 for p in paras if p.get("is_code")),
        "list_paragraphs": sum(1 for p in paras if p.get("is_list")),
        "quote_paragraphs": sum(1 for p in paras if p.get("is_quote")),
        "style_classes": dict(styles),
        "figure_table_labels": {"chapter_scheme": chapter, "flat_scheme": flat},
        "page_hint_max": max((p.get("page_hint") or 0) for p in paras) if paras else 0,
    }


def workload(run_dir: Path) -> dict:
    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    sizes = read_json(resolve_path(run_dir, "prompts") / "index.json", {}) or {}
    per_stage = {}
    for st in STAGES:
        try:
            units = stage_units(run_dir, st)
        except SystemExit:
            units = []
        n = [sizes.get(f"{st}/{u['unit']}", 0) for u in units]
        per_stage[st] = {"units": len(units),
                         "prompt_chars_max": max(n) if n else 0,
                         "prompt_chars_avg": round(sum(n) / len(n)) if n else 0}
    return {
        "chunks": len(idx.get("chunks", [])),
        "table_only_chunks": sum(1 for c in idx.get("chunks", [])
                                 if c.get("chunk_type") == "table_only"),
        "text_tokens_total": sum(int(c.get("text_tokens") or 0) for c in idx.get("chunks", [])),
        "stages": per_stage,
    }


def funnel(run_dir: Path) -> dict:
    """漏斗：候选进来多少、每一道闸门丢了多少、最后剩多少。

    **这是最该看的一张表。** "模型没查出来"与"查出来被闸门丢光"在最终报告里
    长得一模一样，处置却完全相反（前者调 prompt，后者调闸门或词表）。
    """
    idir = resolve_path(run_dir, "issues")
    agg: dict[str, int] = collections.Counter()
    per_rule: dict[str, int] = collections.Counter()
    for g in sorted(idir.glob("issues-*.gates.json")):
        d = read_json(g, {}) or {}
        for k, v in d.items():
            if isinstance(v, int):
                agg[k] += v
        for k, v in (d.get("neverflag_by_rule") or {}).items():
            per_rule[k] += int(v)
    verified = list(read_jsonl(resolve_path(run_dir, "issues_verified")))
    by_cat = collections.Counter(r.get("category") for r in verified)
    drop2 = sum(1 for r in verified if (r.get("verify") or {}).get("result") == "drop")
    return {"gates": dict(agg), "neverflag_by_rule": dict(per_rule),
            "second_pass_drop": drop2, "final_by_category": dict(by_cat)}


def facts(run_dir: Path) -> dict:
    """台账质量：各类抽了多少、幻觉闸门丢了多少。

    某一类计数为 0，往往不是"文档里没有"，而是"这一类没抽出来"——
    而覆盖性规则（L29/L30）正是建立在这些类之上的。
    """
    db = resolve_path(run_dir, "ledger_db")
    if not db.exists():
        return {"note": "ledger.db 尚未构建"}
    con = sqlite3.connect(str(db))
    by_kind = dict(con.execute("SELECT kind, COUNT(*) FROM facts GROUP BY kind"))
    scoped = dict(con.execute(
        "SELECT kind, SUM(CASE WHEN scope IS NOT NULL AND scope<>'' THEN 1 ELSE 0 END) "
        "FROM facts GROUP BY kind"))
    con.close()
    return {"by_kind": by_kind, "with_scope": scoped}


def conflicts(run_dir: Path) -> dict:
    cdir = resolve_path(run_dir, "conflicts_candidate")
    by_rule: dict[str, int] = collections.Counter()
    for f in sorted(cdir.glob("conflicts-candidate.*.json")):
        d = read_json(f, {}) or {}
        items = d.get("candidates") or d.get("items") or []
        for it in items:
            by_rule[it.get("rule") or f.stem.split(".")[-1]] += 1
    verd = collections.Counter(
        (r.get("verdict") or "?") for r in read_jsonl(resolve_path(run_dir, "conflicts_verified")))
    return {"candidates_by_rule": dict(by_rule), "verdicts": dict(verd)}


def typos(run_dir: Path) -> dict:
    """错别字候选按**词表条目**聚合。

    条目本身来自 `assets/dict/common-typos.txt`（我们自己的资产，不是文档内容），
    而误报通常极度集中在少数几条上——知道是哪几条，就知道该往
    `typo-whitelist.txt` 或术语表里加什么。
    """
    tdir = resolve_path(run_dir, "typos")
    pairs: dict[str, int] = collections.Counter()
    total = 0
    for f in sorted(tdir.glob("typos-????.json")):
        for c in (read_json(f, {}) or {}).get("candidates", []):
            pairs[f"{c.get('wrong')}→{c.get('right')}"] += 1
            total += 1
    adopted = 0
    for f in sorted(resolve_path(run_dir, "issues").glob("issues-*.typos.jsonl")):
        adopted += sum(1 for _ in read_jsonl(f))
    return {"candidates": total, "adopted": adopted,
            "top_entries": dict(pairs.most_common(25))}


def _assert_no_text(bundle: dict, run_dir: Path) -> list[str]:
    """自检：诊断包里不许出现文档正文。

    加校验必须同时加负向对照（CLAUDE.md）——所以这里不是"我保证不放正文"，
    而是拿 paragraphs.jsonl 逐条去撞。撞上就终止。
    """
    body = "\n".join((p.get("text") or "") for p in read_jsonl(resolve_path(run_dir, "paragraphs")))
    leaks: list[str] = []

    def walk(node, key=None):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(k, key)
                walk(v, k)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key)
        elif isinstance(node, str):
            if key in SAFE_KEYS or len(node.strip()) < 4:
                return
            s = node.strip()
            if s and s in body:
                leaks.append(s[:40])

    walk(bundle)
    return leaks


def build(run_dir: Path, cfg: dict) -> dict:
    b = {
        **version_header(),
        "note": "本文件不含文档正文。可直接提供给技能维护者。",
        "config": {k: (cfg.get(k) if not isinstance(cfg.get(k), dict) else cfg.get(k))
                   for k in ("strictness", "apply_threshold")},
        "chunking": cfg.get("chunking"),
        "concurrency": cfg.get("concurrency"),
        "logic": cfg.get("logic"),
        "typo_check": cfg.get("typo_check"),
        "pattern_review": cfg.get("pattern_review"),
        "structure": structure(run_dir),
        "workload": workload(run_dir),
        "funnel": funnel(run_dir),
        "facts": facts(run_dir),
        "conflicts": conflicts(run_dir),
        "typos": typos(run_dir),
    }
    return b


def render_md(b: dict) -> str:
    st, wl, fn = b["structure"], b["workload"], b["funnel"]
    lab = st["figure_table_labels"]
    scheme = ("章-序（图3-7）" if lab["chapter_scheme"] > lab["flat_scheme"] * 2
              else "全篇流水（图33）" if lab["flat_scheme"] > lab["chapter_scheme"] * 2
              else "两种混用" if lab["chapter_scheme"] or lab["flat_scheme"] else "未识别到任何图表编号")
    L = [
        "## 文档结构",
        f"- 段落 {st['paragraphs']}，字符 {st['chars']}，标题 {st['headings']}"
        f"（带编号的 {st['numbered_headings']}）",
        f"- 表格 {st['tables']} 张，其中表头形状相同的（疑似平行表）{st['parallel_tables']} 张",
        f"- 图表编号方案：**{scheme}**（章-序 {lab['chapter_scheme']} 处 / "
        f"流水 {lab['flat_scheme']} 处）",
        f"- 样式类别分布：{st['style_classes']}",
        "",
        "## 工作量",
        f"- 分片 {wl['chunks']}（纯表格片 {wl['table_only_chunks']}），"
        f"正文 token {wl['text_tokens_total']}",
        f"- 各波单元数与 prompt 体量：{ {k: v for k, v in wl['stages'].items()} }",
        "",
        "## 漏斗（候选 → 闸门 → 最终）",
        f"- 闸门计数：{fn['gates']}",
        f"- 不改清单按规则：{fn['neverflag_by_rule']}",
        f"- 复核淘汰 {fn['second_pass_drop']}，最终按类别：{fn['final_by_category']}",
        "",
        "## 事实台账",
        f"- 各类计数：{b['facts'].get('by_kind')}",
        f"- 其中填了 scope 的：{b['facts'].get('with_scope')}",
        "",
        "## 冲突",
        f"- 候选按规则：{b['conflicts']['candidates_by_rule']}",
        f"- 裁定结果：{b['conflicts']['verdicts']}",
        "",
        "## 错别字（按词表条目聚合，条目是我们的词表资产，不是文档内容）",
        f"- 候选 {b['typos']['candidates']}，采纳 {b['typos']['adopted']}",
        f"- 贡献最多的条目：{b['typos']['top_entries']}",
    ]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="diagnose.py",
                                 description="生成不含正文的诊断包")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out")
    ap.add_argument("--format", choices=["json", "md"], default="json")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    bundle = build(run_dir, cfg)

    leaks = _assert_no_text(bundle, run_dir)
    if leaks:
        die(EX.VERIFY, f"诊断包里出现了疑似正文（{len(leaks)} 处）：{leaks[:3]}",
            "这是自检失败，不是使用错误。请把这条报给技能维护者，不要手工删掉再发。")

    out = Path(args.out).resolve() if args.out else resolve_path(run_dir, "output") / "diagnose.json"
    guard_write_path(out, run_dir) if not args.out else None
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out, bundle)
    if args.format == "md":
        print(render_md(bundle))
        return EX.OK
    emit({"ok": True, "path": str(out), "leak_check": "passed",
          "chunks": bundle["workload"]["chunks"],
          "note": "不含正文，可直接外发"})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
