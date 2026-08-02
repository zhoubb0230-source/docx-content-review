#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pass 0 脚本预筛 + 概念族候选聚类（spec §9.1.3、§9.1.4）。

性能关键：**不得让 LLM 通读全文来抽术语**。2000 页全文过一遍约需 60 次调用，
而术语抽取的信息密度极低。脚本先筛出 200–500 个候选，只把候选所在段落及其
前后各一段交给 LLM 确认，聚合后约 10 次调用即可完成。

四路候选来源：词频未登录词 / 缩略语正则 / 定义句式正则 / 标题名词短语。
概念族聚类只产生建议（cluster_confirmed 恒为 false），不产生任何判定力。

用法
  glossary_scan.py --run-dir <run> [--config]
输出：work/glossary-candidates.json（含供 LLM 确认的分批 payload）
退出码：0 成功。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tokenizer as tk  # noqa: E402
from _common import (  # noqa: E402
    EX, atomic_write_json, emit, levenshtein, normalize_key, read_jsonl, run_cli, version_header,
)
from workspace import guard_write_path, load_config, resolve_path  # noqa: E402

CJK = r"㐀-鿿"
SEG_RE = re.compile(f"[{CJK}]+")
# 不能作为术语首尾的虚词/高频字，否则新词发现会切出大量垃圾
BAD_EDGE = set("的了和与及在是为对从把被给让使其此该这那些个并且或者等如则将已未也都还很最更就才不无有到于以之而")
BAD_INSIDE = set("。，、；：？！“”‘’（）《》")
ACRONYM_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})\b")
ACRONYM_MIX_RE = re.compile(r"\b([A-Za-z]{2,}[-_]?[0-9]{1,3}(?:\.[0-9]+)*)\b")
DEFINITION_PATTERNS = [
    re.compile(f"([{CJK}A-Za-z0-9]{{2,20}})\\s*(?:是指|指的是|定义为|是一种|系指)"),
    re.compile(f"所谓\\s*([{CJK}A-Za-z0-9]{{2,20}})"),
    re.compile(f"([{CJK}A-Za-z0-9]{{2,20}})\\s*[（(][^）)]*?(?:以下简称|简称|下称)\\s*"
               f"[""'']?([{CJK}A-Za-z0-9]{{1,12}})"),
    re.compile(f"([{CJK}A-Za-z0-9]{{2,20}})\\s*[（(]\\s*([A-Za-z][A-Za-z0-9 \\-]{{1,30}})\\s*[)）]"),
]
HEADING_NOUN_RE = re.compile(f"^[0-9０-９.、\\s]*([{CJK}A-Za-z0-9]{{2,20}})\\s*$")


def ngram_candidates(texts: list[str], min_freq: int, max_n: int = 8) -> Counter:
    """新词发现：段内 n-gram 频次 + 首尾虚词过滤 + 最长优先去冗。"""
    counts: Counter = Counter()
    for t in texts:
        for seg in SEG_RE.findall(t):
            L = len(seg)
            for n in range(2, min(max_n, L) + 1):
                for i in range(L - n + 1):
                    g = seg[i:i + n]
                    if g[0] in BAD_EDGE or g[-1] in BAD_EDGE:
                        continue
                    if any(ch in BAD_INSIDE for ch in g):
                        continue
                    counts[g] += 1
    counts = Counter({g: c for g, c in counts.items() if c >= min_freq})
    # 去冗：若某候选被更长候选包含且频次接近，保留更长的那个
    by_len = sorted(counts, key=len, reverse=True)
    drop = set()
    for long in by_len:
        for n in range(2, len(long)):
            for i in range(len(long) - n + 1):
                sub = long[i:i + n]
                if sub in counts and sub not in drop and counts[sub] <= counts[long] * 1.25:
                    drop.add(sub)
    return Counter({g: c for g, c in counts.items() if g not in drop})


def cluster(terms: list[str], max_dist: int) -> dict:
    """概念族候选聚类。只作建议，不作判据（cluster_confirmed 恒 false）。"""
    def initials(s: str) -> str:
        return "".join(re.findall(r"[A-Za-z]", s)).upper()

    cid: dict[str, str] = {}
    groups: list[list[str]] = []
    for t in terms:
        placed = False
        for g in groups:
            for other in g:
                a, b = normalize_key(t), normalize_key(other)
                close = levenshtein(a, b, cap=max_dist) <= max_dist and abs(len(a) - len(b)) <= max_dist
                # 英文缩写展开匹配：EN ↔ edge node
                acro = bool(initials(t)) and initials(t) == initials(other)
                # 共享 ≥2 字的核心词根
                shared = len(set(a) & set(b)) >= 2 and min(len(a), len(b)) >= 3 \
                    and len(set(a) & set(b)) / min(len(a), len(b)) >= 0.6
                if close or acro or shared:
                    g.append(t)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            groups.append([t])
    for i, g in enumerate(groups, 1):
        if len(g) < 2:
            continue
        for t in g:
            cid[t] = f"c{i:03d}"
    return cid


def scan(run_dir: Path, cfg: dict) -> dict:
    paras = list(read_jsonl(resolve_path(run_dir, "paragraphs")))
    if not paras:
        from _common import die

        die(EX.ERROR, "paragraphs.jsonl 不存在，请先执行 extract.py")

    g = cfg.get("glossary") or {}
    min_freq = int(g.get("min_term_freq") or 3)
    max_cand = int(g.get("max_candidates") or 500)
    max_dist = int(g.get("cluster_distance") or 2)

    body = [p for p in paras if not p["is_code"]]
    texts = [p["text"] for p in body]
    where: dict[str, list[str]] = defaultdict(list)
    sources: dict[str, set] = defaultdict(set)
    hints: dict[str, str] = {}

    # ① 词频未登录词 / 名词短语
    freqs = ngram_candidates(texts, min_freq)
    for p in body:
        for term in freqs:
            if term in p["text"] and len(where[term]) < 8:
                where[term].append(p["pid"])
                sources[term].add("freq")

    # ② 缩略语正则
    for p in body:
        for m in ACRONYM_RE.finditer(p["text"]):
            a = m.group(1)
            if a.isdigit() or len(a) < 2:
                continue
            sources[a].add("acronym")
            if len(where[a]) < 8:
                where[a].append(p["pid"])
        for m in ACRONYM_MIX_RE.finditer(p["text"]):
            a = m.group(1)
            sources[a].add("acronym")
            if len(where[a]) < 8:
                where[a].append(p["pid"])

    # ③ 定义句式正则（高精度，优先级最高）
    for p in body:
        for rx in DEFINITION_PATTERNS:
            for m in rx.finditer(p["text"]):
                term = m.group(1).strip()
                if not term or len(term) < 2:
                    continue
                sources[term].add("definition")
                hints[term] = p["text"][:200]
                if p["pid"] not in where[term]:
                    where[term].append(p["pid"])
                if m.lastindex and m.lastindex >= 2 and m.group(2):
                    alias = m.group(2).strip()
                    if alias and alias != term:
                        sources[alias].add("alias_of:" + term)
                        if p["pid"] not in where[alias]:
                            where[alias].append(p["pid"])

    # ④ 标题名词短语（章节标题往往就是核心术语）
    for p in body:
        if not p["is_heading"]:
            continue
        m = HEADING_NOUN_RE.match(p["text"].strip())
        if m:
            term = m.group(1).strip()
            if len(term) >= 2:
                sources[term].add("heading")
                if p["pid"] not in where[term]:
                    where[term].append(p["pid"])

    scored = []
    for term, srcs in sources.items():
        if not where[term]:
            continue
        weight = (3 if "definition" in srcs else 0) + (2 if "heading" in srcs else 0) \
            + (2 if "acronym" in srcs else 0) + min(freqs.get(term, 0), 20) / 10
        scored.append({"term": term, "freq": freqs.get(term, len(where[term])),
                       "pids": where[term], "sources": sorted(srcs),
                       "definition_hint": hints.get(term, ""), "_w": weight})
    scored.sort(key=lambda x: (-x["_w"], -x["freq"], x["term"]))
    scored = scored[:max_cand]

    cids = cluster([c["term"] for c in scored], max_dist)
    for c in scored:
        c.pop("_w", None)
        c["cluster_id"] = cids.get(c["term"])
        c["cluster_confirmed"] = False       # 未经人工确认，不触发任何 L 规则

    # 分批：只把候选所在段落及其前后各一段交给 LLM
    pmap = {p["pid"]: i for i, p in enumerate(paras)}
    batches, cur, cur_tok = [], [], 0
    budget = 6000
    for c in scored:
        ctx_pids = []
        for pid in c["pids"][:3]:
            i = pmap.get(pid)
            if i is None:
                continue
            for j in range(max(0, i - 1), min(len(paras), i + 2)):
                if paras[j]["pid"] not in ctx_pids:
                    ctx_pids.append(paras[j]["pid"])
        ctx = "\n".join(f"[{paras[pmap[q]]['pid']}] {paras[pmap[q]]['text'].strip()}"
                        for q in ctx_pids if q in pmap)[:1200]
        item = {"term": c["term"], "sources": c["sources"], "context": ctx}
        t = tk.count(ctx) + 40
        if cur and cur_tok + t > budget:
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(item)
        cur_tok += t
    if cur:
        batches.append(cur)

    payload = {
        **version_header(),
        "candidates": scored,
        "clusters": {cid: [c["term"] for c in scored if c["cluster_id"] == cid]
                     for cid in sorted(set(cids.values()))},
        "batches": [{"batch_id": f"b{i:02d}", "items": b} for i, b in enumerate(batches, 1)],
    }
    out = resolve_path(run_dir, "glossary_candidates")
    guard_write_path(out, run_dir)
    atomic_write_json(out, payload)
    return {"candidates": len(scored), "clusters": len(payload["clusters"]),
            "batches": len(batches), "path": str(out)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="glossary_scan.py", description="Pass 0 候选术语预筛")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    emit({"ok": True, **scan(Path(args.run_dir).resolve(), load_config(args.config))})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
