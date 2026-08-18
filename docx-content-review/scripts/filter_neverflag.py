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
    EX, atomic_write_json, atomic_write_jsonl, die, emit, normalize_key, normalize_width,
    normalize_ws, read_json, read_jsonl, run_cli,
)
from workspace import guard_write_path, load_run_config, resolve_path  # noqa: E402

OPEN_Q = "“「『\""      # “ 「 『 "
CLOSE_Q = "”」』\""     # ” 」 』 "
# N10：引用的法规/标准/合同原文。**这里只是"援引"的标志，不等于整段都是引文**——
# 引文本体要么被引号括住，要么跟在「规定：」「约定：」之后。
# 条号允许小数分节（「合同第 3.2 条」）与两侧空格——`never-flag.md` 自己举的
# 那个例子早先就匹配不上，负向语料把它撞出来了。
_ART = r"第\s*[〇零一二三四五六七八九十百千0-9]+(?:\.[0-9]+)*\s*"
CITATION_PATTERNS = [
    re.compile(rf"《[^》]{{2,60}}》\s*{_ART}[条款章节项]"),
    re.compile(rf"(?:法|条例|办法|规定|标准|规范|合同|协议)\s*{_ART}[条款项]"),
    re.compile(r"\bGB/?T?\s*\d{3,}"),
    re.compile(r"\bISO\s*\d{3,}"),
]
# 整段就是一句引文
WHOLE_QUOTE_RE = re.compile(f"^[{OPEN_Q}].{{4,}}[{CLOSE_Q}]$")
# 引号括住的引文本体
QUOTED_BODY_RE = re.compile(f"[{OPEN_Q}][^{CLOSE_Q}]{{4,400}}[{CLOSE_Q}]")
# 「…规定：」之后到段末是逐字引文（仅在同段出现了援引标志时才算）
QUOTE_LEAD_RE = re.compile(r"(?:规定|约定|要求|明确)\s*[：:]")
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


SENT_END = "。！？；!?;"


def _norm_map(text: str) -> tuple[str, list[int]]:
    """去空白串 + 「去空白位置 → 原串位置」的映射。"""
    buf, idx = [], []
    for i, ch in enumerate(text):
        if not ch.isspace():
            buf.append(ch)
            idx.append(i)
    return "".join(buf), idx


def _span_range(ptext: str, span: str) -> tuple[int, int] | None:
    """跨度在段落原串中的字符区间。定位不到返回 None。

    闸门② 已保证 `normalize_ws(跨度)` 是段落的子串，所以这里在去空白空间里
    定位再映射回原串，不受换行与全角空格的干扰。
    """
    nt, idx = _norm_map(ptext)
    ns = normalize_ws(span)
    if not ns:
        return None
    i = nt.find(ns)
    if i < 0:
        return None
    return idx[i], idx[i + len(ns) - 1] + 1


def exempt_regions(ptext: str, para: dict | None, issue: dict, skip: dict) -> list[tuple]:
    """段落内被位置类规则豁免的字符区间 [(起, 止, 规则号)]，整段豁免为 (0, len)。

    **N9/N10/N12 判的是区域，不是段落。** 三条规则的原文都是"这个区域内的内容
    不审查"：代码块、引文、图表标题各自是一块区域。早先的实现把它们写成了
    "段落里出现该特征就整段免检"，而技术文档里一句话带一条 URL、一个
    `cpu_usage=80%`、一句「按照本办法第五条执行」都极常见——按整段判，
    含这些特征的段落里所有语病都被静默吞掉，输出里没有任何痕迹。

    这与 ADR-030 修 N7 时定下的判据是同一条：**凡是"某个条件成立就跳过检查"
    的判据，都要问一句：这个条件在真实语料上会有多大比例成立？若接近全集，
    它就不是过滤器，是开关。** 负向语料见 `tests/fixtures/neverflag-traps.json`。
    """
    n = len(ptext)
    out: list[tuple] = []
    stripped = ptext.strip()
    off = ptext.find(stripped) if stripped else 0

    # N9 代码块、命令行、配置示例、日志片段、文件路径
    if skip.get("code_blocks", True):
        if (para or {}).get("is_code") or issue.get("is_code"):
            # 整段就是代码。这个判定来自 extract.py，那里已按中文字符占比排除了散文。
            out.append((0, n, "N9"))
        else:
            for rx in CODE_PATTERNS:
                for m in rx.finditer(ptext):
                    out.append((m.start(), m.end(), "N9"))

    # N10 引用的法规/标准/合同原文
    if skip.get("quoted_regulations", True):
        if (para or {}).get("is_quote") or WHOLE_QUOTE_RE.match(stripped):
            out.append((0, n, "N10"))
        else:
            cited = False
            for rx in CITATION_PATTERNS:
                for m in rx.finditer(ptext):
                    cited = True
                    out.append((m.start(), m.end(), "N10"))   # 条号本身不得改动
            for m in QUOTED_BODY_RE.finditer(ptext):
                out.append((m.start(), m.end(), "N10"))
            # 「《X 法》第 N 条规定：」之后到段末是逐字引文——引文常常不带引号，
            # 只靠这个冒号分界。没有援引标志时不适用，否则任何「要求：」都会免检。
            if cited:
                lead = QUOTE_LEAD_RE.search(ptext)
                if lead:
                    out.append((lead.end(), n, "N10"))

    # N12 图表标题、编号、页眉页脚的固定格式
    for rx in CAPTION_PATTERNS:
        m = rx.search(stripped)
        if not m:
            continue
        # 整行就是标签 → 整段免检；标签后面还跟着成句的正文（「表 3 中列出的各项
        # 指标改善了…问题。」）→ 只免检标签本身，正文照常审查。
        if not any(ch in stripped for ch in SENT_END):
            out.append((0, n, "N12"))
        else:
            out.append((off + m.start(), off + m.end(), "N12"))
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

    # N11 表格单元格内的省略式表述——单元格本身就是区域，整体不做语病审查
    if skip.get("tables_language_check", True) and ((para or {}).get("in_table") or issue.get("in_table")):
        return "N11"

    # N9 / N10 / N12：位置类规则。命中的条件是**跨度落进了被豁免的区域**，
    # 而不是"段落里出现过该特征"（见 exempt_regions 的说明）。
    regions = exempt_regions(ptext, para, issue, skip)
    if regions:
        rng = _span_range(ptext, text)
        if rng is None:
            # 跨度在段落里定位不到（闸门②本应保证能定位）。此时无从判断落在哪，
            # 退回旧的整段语义：宁可压制，也不要把可能落在代码/引文里的改动放出去。
            return regions[0][2]
        s, e = rng
        for a, b, rule in regions:
            if s < b and a < e:                # 有重叠即压制
                return rule

    # P 类到此为止：N7/N8/N13/N14 判的是「改动本身该不该做」，而 P 类不改任何字，
    # 它的 original_text 是整个段落。拿整段去撞 fallback 术语表必然命中，
    # 会把所有范式条目误杀——位置类规则（N9/N10/N11/N12）已在上面判过，够了。
    if cat in ("P1",):
        return None

    # N7 术语的合法别名
    if sugg:
        a, b = normalize_key(text), normalize_key(sugg)
        for g in groups:
            if any(f in a for f in g) and any(f in b for f in g):
                return "N7"
    # fallback 层的意义是「别改它」，所以只有当这条问题**确实要动这个术语**时才压制：
    # 建议里该术语没了，说明修改落在术语本身上。
    #
    # 早先的条件是「跨度里出现该术语就压制」。这在真实文档里等于关掉大半个审查——
    # 用户的 fallback 表里只要有「系统」「平台」「数据」这类两字词，
    # 每一条含这两个字的发现都会被静默吞掉，而输出里没有任何痕迹。
    # 压制方向的 fail-open 比放行方向更危险：它表现为"什么都没查出来"。
    #
    # B 类没有建议，不改任何字，"别改它"对它不适用，因此不参与本条压制。
    if sugg:
        nt, ns = normalize_key(text), normalize_key(sugg)
        for term in fallback:
            if term and len(term) >= 2 and term in nt and term not in ns:
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
                  glossary: dict, groups, fallback, path: Path | None = None) -> dict:
    # path 供侧通道（错别字/范式）使用：它们的产物是 issues-<chunk>.<通道>.jsonl，
    # 就地过滤，与主通道互不覆盖。
    path = Path(path) if path else resolve_path(run_dir, "issues") / f"issues-{chunk_id}.jsonl"
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
    stem = path.name[:-len(".jsonl")] if path.name.endswith(".jsonl") else path.name
    meta = path.parent / f"{stem}.neverflag.json"
    guard_write_path(meta, run_dir)
    total = sum(hits.values())
    atomic_write_json(meta, {"chunk_id": chunk_id, "dropped": total, "by_rule": hits,
                             "kept": len(kept)})
    return {"chunk_id": chunk_id, "dropped": total, "kept": len(kept), "by_rule": hits}


def traps(path: Path, cfg: dict) -> dict:
    """负向语料自检：拿一组（段落, 跨度, 期望）去撞 check()。

    **只验"该压制的压住了"不算验，还要验"不该压制的没被压住"**——
    与 `typo_scan.py lint` 的 `typo-traps.txt`、`validate_docx.py` 的负向对照同性质。
    压制方向的 fail-open 比放行方向更危险：它表现为"什么都没查出来"，
    输出里没有任何痕迹（ADR-030）。改 N9–N14 前后必跑。
    """
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        die(EX.PARSE, f"负向语料格式不对（应为 {{\"cases\": [...]}}）：{path}")
    bad = []
    for i, c in enumerate(data["cases"], 1):
        para = {"text": c["para"], "is_code": bool(c.get("is_code")),
                "in_table": bool(c.get("in_table")), "is_quote": bool(c.get("is_quote")),
                "is_heading": bool(c.get("is_heading"))}
        issue = {"original_text": c["span"], "category": c.get("category") or "A5",
                 "suggested_text": c.get("suggested_text") or ""}
        got = check(issue, para, cfg, {}, [], set()) or ""
        want = c.get("expect") or ""
        if got != want:
            bad.append(f"#{i} {c.get('note') or c['para'][:20]}："
                       f"期望 {want or '保留'}，实际 {got or '保留'}")
    return {"cases": len(data["cases"]), "ok": not bad, "problems": bad}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="filter_neverflag.py", description="闸门③不改清单过滤")
    ap.add_argument("--run-dir")
    ap.add_argument("--chunk")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--channel", choices=["main", "typos", "patterns"], default="main",
                    help="--all 时决定扫哪一条通道的产物")
    ap.add_argument("--file", help="直接指定要过滤的 issues 文件；"
                                   "侧通道（错别字/范式）用它指向自己的产物")
    ap.add_argument("--config")
    ap.add_argument("--probe", help="直接检查一段文本（调试用）")
    ap.add_argument("--traps", help="负向语料自检（改 N9–N14 前后必跑），"
                                    "见 tests/fixtures/neverflag-traps.json")
    ap.add_argument("--category", default="A5")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve() if args.run_dir else None
    cfg = load_run_config(run_dir, args.config)

    if args.traps:
        res = traps(Path(args.traps), cfg)
        for p in res["problems"]:
            die_msg = f"   {p}"
            sys.stderr.write(die_msg + "\n")
        emit({"ok": res["ok"], **res})
        return EX.OK if res["ok"] else EX.PARSE

    if args.probe:
        rule = check({"original_text": args.probe, "category": args.category},
                     {"text": args.probe}, cfg, {}, [], set())
        emit({"ok": True, "hit": rule, "kept": rule is None})
        return EX.OK

    if run_dir is None:
        die(EX.USAGE, "缺少 --run-dir")
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    glossary = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}
    groups, fallback = _alias_groups(glossary), _fallback_terms(glossary)

    idir = resolve_path(run_dir, "issues")
    suffix = {"main": "", "typos": ".typos", "patterns": ".patterns"}[args.channel]
    ids = []
    if args.all:
        # 通道决定看哪一批文件：主通道 issues-<c>.jsonl，侧通道 issues-<c>.<通道>.jsonl
        ids = sorted(p.name.split("-")[1].split(".")[0]
                     for p in idir.glob(f"issues-*{suffix}.jsonl")
                     if p.name.count(".") == (1 if args.channel == "main" else 2)
                     and not p.name.endswith(".raw.jsonl"))
    elif args.chunk:
        ids = [args.chunk]
    target = Path(args.file) if args.file else None
    if target and not args.chunk:
        die(EX.USAGE, "--file 必须与 --chunk 一起使用")
    results = [process_chunk(run_dir, c, cfg, paras, glossary, groups, fallback,
                             target or (idir / f"issues-{c}{suffix}.jsonl"
                                        if suffix else None))
               for c in ids]
    emit({"ok": True, "chunks": len(results),
          "dropped": sum(r.get("dropped", 0) for r in results),
          "kept": sum(r.get("kept", 0) for r in results),
          "results": results})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
