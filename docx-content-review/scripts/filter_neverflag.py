#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸门③：不改清单硬过滤（spec §8 闸门③、references/never-flag.md）。

prompt 里已经用反例 few-shot 讲过一遍 N1–N14，但**不依赖模型自觉**：
N7–N14 中可脚本判定的部分在这里硬过滤（依据 is_code / in_table / glossary
别名组 / 引用样式 / 标题结构），命中即丢弃。

用法
  filter_neverflag.py --run-dir <run> (--chunk 0001 | --all) [--config]
  filter_neverflag.py --probe "文本" [--category A4]     # 调试用
输入/输出：work/issues/issues-<chunk>.jsonl（就地过滤，幂等）
        + issues-<chunk>.neverflag.json（各条命中计数）
退出码：0 成功。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_jsonl, emit, normalize_key, normalize_width,
    read_json, read_jsonl, run_cli,
)
from workspace import guard_write_path, load_config, resolve_path  # noqa: E402

# N10：引用的法规/标准/合同原文
QUOTE_PATTERNS = [
    re.compile(r"《[^》]{2,60}》\s*第[〇零一二三四五六七八九十百千0-9]+[条款章节项]"),
    re.compile(r"(?:法|条例|办法|规定|标准|规范|合同|协议)\s*第[〇零一二三四五六七八九十百千0-9]+[条款项]"),
    re.compile(r"(?:规定|约定|要求)[：:][""「『]"),
    re.compile(r"^[""「『].{4,}[""」』]$"),
    re.compile(r"\bGB/?T?\s*\d{3,}"),
    re.compile(r"\bISO\s*\d{3,}"),
]
# N12：图表标题、编号、页眉页脚的固定格式
CAPTION_PATTERNS = [
    re.compile(r"^\s*(?:图|表|附图|附表|Figure|Fig\.?|Table)\s*[0-9０-９]+\s*[-–—.－][0-9０-９]+"),
    re.compile(r"^\s*(?:图|表)\s*[0-9０-９]+\s"),
    re.compile(r"^\s*附录\s*[A-Za-z0-9一二三四五六七八九十]"),
    re.compile(r"^\s*(?:单位|注)\s*[：:]"),
    re.compile(r"^\s*第\s*[0-9０-９]+\s*页"),
]
# N9：代码/命令/配置/日志/路径
CODE_PATTERNS = [
    re.compile(r"https?://|ftp://|ssh://"),
    re.compile(r"^\s*[$#>]\s+\S"),
    re.compile(r"\b(?:sudo|apt-get|yum|npm|pip|git|docker|kubectl|curl|systemctl)\s+[a-z-]+"),
    re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\s*=\s*[^\s，。；]+"),
    re.compile(r"(?:^|\s)(?:[A-Za-z]:\\|/(?:usr|etc|var|opt|home|bin|tmp|dev|srv)/)"),
    re.compile(r"<[/?!a-zA-Z][^<>]{0,80}>"),
    re.compile(r"\{[^{}]*\}\s*$"),
]
# N8：行业惯用简称、产品代号、内部代号（可由 fallback 术语层扩充）
BUILTIN_ABBREV = {
    "sre", "qa", "qps", "tps", "api", "sdk", "cli", "gui", "ci", "cd", "cicd", "k8s",
    "poc", "mvp", "roi", "kpi", "okr", "sla", "slo", "sli", "rto", "rpo", "owner",
    "p0", "p1", "p2", "p3", "p4", "mec", "cdn", "vpc", "iam", "oss", "rds", "mq",
    "llm", "rag", "gpu", "cpu", "ssd", "hdd", "iops", "tco", "saas", "paas", "iaas",
}
# N13：数字用法（阿拉伯 vs 汉字）——无规范输入时不判
CN_NUM = "〇零一二三四五六七八九十百千万亿两"
ARAB_NUM = "0123456789０１２３４５６７８９"
# N14：标题的名词短语式表述
HEADING_LIKE = re.compile(
    r"^\s*(?:[第]?[〇零一二三四五六七八九十百0-9０-９]+\s*[、.．，,]?\s*)?"
    r"[^。！？；]{1,40}$")


def _alias_groups(glossary: dict) -> list[set[str]]:
    groups = []
    for e in (glossary or {}).get("entries", []):
        forms = {e.get("key"), e.get("preferred")} | set(e.get("variants") or [])
        forms = {normalize_key(f) for f in forms if f}
        if len(forms) > 1:
            groups.append(forms)
    return groups


def _fallback_terms(glossary: dict) -> set[str]:
    """fallback 层的价值在于 enforce=off：只作参照物，用于抑制误报。"""
    out = set()
    for e in (glossary or {}).get("entries", []):
        if e.get("source") == "fallback" or e.get("enforce") == "off":
            for f in [e.get("key"), e.get("preferred"), *(e.get("variants") or [])]:
                if f:
                    out.add(normalize_key(f))
    return out


def _digits_only_diff(a: str, b: str) -> bool:
    """差异是否只在「阿拉伯数字 ↔ 汉字数字」之间。"""
    def strip_nums(s: str) -> str:
        return "".join(ch for ch in s if ch not in CN_NUM and ch not in ARAB_NUM)
    return strip_nums(a) == strip_nums(b) and a != b


def check(issue: dict, para: dict | None, cfg: dict, glossary: dict,
          groups: list[set[str]], fallback: set[str]) -> str | None:
    """命中返回规则号（N7…N14），未命中返回 None。"""
    text = issue.get("original_text") or ""
    sugg = issue.get("suggested_text") or ""
    cat = issue.get("category") or ""
    ptext = (para or {}).get("text") or text
    skip = cfg.get("skip") or {}

    # N9 代码块、命令行、配置示例、日志片段、文件路径
    if skip.get("code_blocks", True):
        if (para or {}).get("is_code") or issue.get("is_code"):
            return "N9"
        if any(rx.search(ptext) for rx in CODE_PATTERNS):
            return "N9"

    # N11 表格单元格内的省略式表述
    if skip.get("tables_language_check", True) and ((para or {}).get("in_table") or issue.get("in_table")):
        return "N11"

    # N10 引用的法规/标准/合同原文
    if skip.get("quoted_regulations", True):
        if (para or {}).get("is_quote") or any(rx.search(ptext) for rx in QUOTE_PATTERNS):
            return "N10"

    # N12 图表标题、编号、页眉页脚
    if any(rx.search(ptext.strip()) for rx in CAPTION_PATTERNS):
        return "N12"

    # N7 术语的合法别名
    if sugg:
        a, b = normalize_key(text), normalize_key(sugg)
        for g in groups:
            if any(f in a for f in g) and any(f in b for f in g):
                return "N7"
    for term in fallback:
        if term and len(term) >= 2 and term in normalize_key(text):
            return "N7"

    # N8 行业惯用简称、产品代号、内部代号
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{0,9}", normalize_width(text))
    if tokens and all(t.lower() in BUILTIN_ABBREV for t in tokens) and \
            not re.search(r"[一-鿿]", text):
        return "N8"

    # N13 数字用法（阿拉伯数字 vs 汉字数字）
    if sugg and _digits_only_diff(text, sugg):
        return "N13"

    # N14 标题的名词短语式表述（无谓语不算成分残缺）
    if cat == "A4" and ((para or {}).get("is_heading") or HEADING_LIKE.match(ptext.strip())
                        and len(ptext.strip()) <= 40 and not ptext.strip().endswith("。")):
        return "N14"

    return None


def process_chunk(run_dir: Path, chunk_id: str, cfg: dict, paras: dict,
                  glossary: dict, groups, fallback) -> dict:
    path = resolve_path(run_dir, "issues") / f"issues-{chunk_id}.jsonl"
    if not path.exists():
        return {"chunk_id": chunk_id, "skipped": True}
    kept, hits = [], {}
    for rec in read_jsonl(path):
        rule = check(rec, paras.get(rec.get("pid")), cfg, glossary, groups, fallback)
        if rule:
            hits[rule] = hits.get(rule, 0) + 1
            continue
        kept.append(rec)
    guard_write_path(path, run_dir)
    atomic_write_jsonl(path, kept)
    meta = resolve_path(run_dir, "issues") / f"issues-{chunk_id}.neverflag.json"
    guard_write_path(meta, run_dir)
    total = sum(hits.values())
    atomic_write_json(meta, {"chunk_id": chunk_id, "dropped": total, "by_rule": hits,
                             "kept": len(kept)})
    return {"chunk_id": chunk_id, "dropped": total, "kept": len(kept), "by_rule": hits}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="filter_neverflag.py", description="闸门③不改清单过滤")
    ap.add_argument("--run-dir")
    ap.add_argument("--chunk")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--probe", help="直接检查一段文本（调试用）")
    ap.add_argument("--category", default="A5")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.probe:
        rule = check({"original_text": args.probe, "category": args.category},
                     {"text": args.probe}, cfg, {}, [], set())
        emit({"ok": True, "hit": rule, "kept": rule is None})
        return EX.OK

    run_dir = Path(args.run_dir).resolve()
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    glossary = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}
    groups, fallback = _alias_groups(glossary), _fallback_terms(glossary)

    ids = []
    if args.all:
        ids = sorted(p.name.split("-")[1].split(".")[0]
                     for p in resolve_path(run_dir, "issues").glob("issues-*.jsonl")
                     if p.name.count(".") == 1)
    elif args.chunk:
        ids = [args.chunk]
    results = [process_chunk(run_dir, c, cfg, paras, glossary, groups, fallback) for c in ids]
    emit({"ok": True, "chunks": len(results),
          "dropped": sum(r.get("dropped", 0) for r in results),
          "kept": sum(r.get("kept", 0) for r in results),
          "results": results})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
