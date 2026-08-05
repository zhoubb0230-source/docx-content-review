#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pass 3：全局台账冲突检测，L01–L32（spec §9.4）。纯脚本，零 LLM 调用。

D1 的核心：跨片比对是集合运算，不是语言理解任务。交给脚本可获得 100% 召回、
零幻觉，并规避中等能力模型的短板。所有 L 规则的输出都是**候选**，必须过 Pass 4 裁定。

L29–L32 是覆盖性检查（集合差集），不是论证强度判断——这是它们能进 v1 的原因。
但它们依赖抽取完整性：漏抽一条举措就会误报，因此严重度上限 Medium，措辞统一为
「未检索到与 X 对应的 Y，请确认」，默认只进报告不进批注。

用法
  detect_conflicts.py --run-dir <run> [--rules L06,L12] [--session <id> --generation <n>]
输出：work/conflicts/conflicts-candidate.<RULE>.json（按规则分组，已完成的规则不重算）
     + work/conflicts/index.json
退出码：0 成功；1 失败；9 令牌失效。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, emit, levenshtein, normalize_key, normalize_ws, read_json,
    read_jsonl, run_cli, version_header,
)
from ledger import connect  # noqa: E402
from workspace import (  # noqa: E402
    Heartbeat, guard_write_path, lease_verify, load_config, resolve_path,
)

# 规则元数据：严重度 + 默认动作。动作是候选值，Pass 4 裁定后才最终确定。
RULES = {
    "L01": ("High", "comment", "同一术语出现多个不同定义"),
    "L02": ("High", "comment", "同一缩略语有多个不同展开"),
    "L03": ("Low", "comment", "缩略语首次出现处未给出展开"),
    "L04": ("Medium", "comment", "同一实体存在多种写法且未登记为别名组"),
    "L05": ("Low", "report_only", "近义术语混用指代同一概念"),
    "L06": ("Critical", "comment", "同一指标在相同范围下出现不同数值"),
    "L07": ("High", "comment", "同一指标的单位不一致"),
    "L08": ("Critical", "comment", "区间矛盾（下限大于上限）"),
    "L09": ("High", "comment", "百分比构成合计不等于 100%"),
    "L10": ("High", "comment", "声明的条目数与实际列举数不符"),
    "L11": ("Medium", "comment", "数值与文字描述不符"),
    "L12": ("Critical", "comment", "同一事件出现不同日期"),
    "L13": ("High", "comment", "时序倒置"),
    "L14": ("High", "comment", "版本号倒退或不一致"),
    "L15": ("High", "comment", "交叉引用指向不存在的对象"),
    "L16": ("Medium", "comment", "图表编号跳号或重号"),
    "L17": ("Medium", "comment", "目录条目与正文标题不一致"),
    "L18": ("Low", "report_only", "图表存在但正文无引用"),
    "L19": ("Low", "report_only", "章节层级跳级"),
    "L20": ("Critical", "comment", "同一对象的状态前后矛盾"),
    "L21": ("High", "comment", "同一承诺的情态强度冲突"),
    "L22": ("High", "comment", "职责分配冲突"),
    "L23": ("High", "comment", "结论与其范围内的指标/状态矛盾"),
    "L24": ("Medium", "comment", "总结条目数与前文列举不符"),
    "L25": ("High", "revision", "使用了术语表中登记的禁用写法"),
    "L26": ("Medium", "revision", "使用了变体而非标准写法"),
    "L27": ("High", "comment", "目标值与实测值差距过大"),
    "L28": ("High", "comment", "同一议题的立场前后不一致"),
    "L29": ("Medium", "report_only", "目标无对应举措"),
    "L30": ("Medium", "report_only", "举措无验收指标"),
    "L31": ("Medium", "report_only", "量化承诺无度量方式"),
    "L32": ("Low", "report_only", "章节标题承诺的内容缺失"),
}
COVERAGE_RULES = {"L29", "L30", "L31", "L32"}

NUM_RE = re.compile(r"-?\d+(?:[,，]\d{3})*(?:\.\d+)?")
UNIT_ALIASES = {"毫秒": "ms", "秒": "s", "分钟": "min", "小时": "h", "天": "d",
                "兆": "MB", "吉": "GB", "g": "GB", "m": "MB", "k": "KB", "％": "%"}
TIME_UNITS = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}
SIZE_UNITS = {"KB": 1, "MB": 1024, "GB": 1024 ** 2, "TB": 1024 ** 3}
STATUS_POS = {"已完成", "已支持", "已建成", "已上线", "支持", "完成"}
STATUS_NEG = {"不支持", "未支持", "未完成", "规划中", "建设中", "待建设", "暂不支持"}
MODALITY = {"必须": 3, "应当": 3, "应": 3, "不低于": 3, "不得": 3, "将": 2, "计划": 2,
            "拟": 2, "宜": 1, "可": 1, "可选": 1, "建议": 1}
PHASE_RE = re.compile(r"(一|二|三|四|五|六|1|2|3|4|5|6)\s*期|阶段\s*([0-9一二三四五六])")
SECTION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
LABEL_RE = re.compile(r"(图|表)\s*([0-9]+)\s*[-–—.－]\s*([0-9]+)")
MEASURE_WORDS = ("度量", "测量", "监测", "统计口径", "采集", "验证", "考核", "评估方式",
                 "计算方式", "口径", "验收")
SECTION_PROMISE = {
    "风险": ("风险", "应对", "缓解", "隐患"),
    "验收": ("验收", "指标", "标准"),
    "预算": ("预算", "万元", "投入", "资金"),
    "里程碑": ("里程碑", "阶段", "上线", "启动"),
    "职责": ("负责", "职责", "owner", "由"),
}


# --------------------------------------------------------------------------
def parse_num(s) -> float | None:
    if s is None:
        return None
    m = NUM_RE.search(str(s).replace(",", "").replace("，", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "").replace("，", ""))
    except ValueError:
        return None


def norm_unit(u) -> str:
    if not u:
        return ""
    u = str(u).strip()
    return UNIT_ALIASES.get(u, UNIT_ALIASES.get(u.lower(), u))


def to_base(value, unit) -> tuple[float, str] | None:
    """把 (值, 单位) 折算到同一量纲基准，用于跨单位的数值比较。"""
    n = parse_num(value)
    if n is None:
        return None
    u = norm_unit(unit)
    if u in TIME_UNITS:
        return n * TIME_UNITS[u], "time"
    if u in SIZE_UNITS:
        return n * SIZE_UNITS[u], "size"
    return n, u or "raw"


def parse_date(s) -> tuple | None:
    if not s:
        return None
    t = str(s)
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})(?:\s*[-/月.]\s*(\d{1,2}))?", t)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    m = re.search(r"(\d{4})\s*年", t)
    if m:
        return (int(m.group(1)), 1, 1)
    return None


def parse_version(s) -> tuple | None:
    m = re.search(r"v?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(s or ""), re.I)
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def substantially_different(a: str, b: str) -> bool:
    """定义是否"实质不同"：短串按编辑距离，长串按相对差异比例。"""
    na, nb = normalize_ws(a), normalize_ws(b)
    if na == nb:
        return False
    d = levenshtein(na, nb)
    return d / max(len(na), len(nb), 1) > 0.4


# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, run_dir: Path, cfg: dict):
        self.run_dir = run_dir
        self.cfg = cfg
        self.paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
        self.order = {pid: i for i, pid in enumerate(self.paras)}
        self.headings = (read_json(resolve_path(run_dir, "headings"), {}) or {}).get("headings", [])
        self.glossary = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}
        self.con: sqlite3.Connection = connect(run_dir)

    def rows(self, kind: str) -> list[dict]:
        cur = self.con.execute(
            "SELECT kind,chunk_id,pid,subject,value,unit,qualifier,scope,meta "
            "FROM facts WHERE kind=?", (kind,))
        cols = ["kind", "chunk_id", "pid", "subject", "value", "unit", "qualifier", "scope", "meta"]
        out = []
        for r in cur:
            d = dict(zip(cols, r))
            try:
                d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
            except json.JSONDecodeError:
                d["meta"] = {}
            out.append(d)
        return out

    def side(self, pid: str, extra: dict | None = None) -> dict:
        p = self.paras.get(pid) or {}
        return {"pid": pid, "text": (p.get("text") or "").strip()[:300],
                "heading_path": p.get("heading_path", []), "page_hint": p.get("page_hint"),
                **(extra or {})}

    def sort_key(self, pid: str) -> int:
        return self.order.get(pid, 10 ** 9)


def make(rule: str, ctx: Ctx, sides: list[dict], subject: str, note: str,
         seq: dict) -> dict:
    sev, action, desc = RULES[rule]
    seq[rule] = seq.get(rule, 0) + 1
    if rule in COVERAGE_RULES and not (ctx.cfg.get("logic") or {}).get("coverage_rules_to_comment"):
        action = "report_only"
    return {
        "conflict_id": f"{rule}-{seq[rule]:04d}",
        "rule": rule, "severity": sev, "action": action, "description": desc,
        "subject": subject, "note": note, "sides": sides,
        "chapter_span": len({tuple(s.get("heading_path") or []) for s in sides}),
    }


# --------------------------------------------------------------------------
# 术语与命名
def r_L01(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("term"):
        if r["subject"] and r["value"]:
            groups[normalize_key(r["subject"])].append(r)
    for key, rs in groups.items():
        uniq = []
        for r in rs:
            if not any(not substantially_different(r["value"], u["value"]) for u in uniq):
                uniq.append(r)
        if len(uniq) >= 2:
            out.append(make("L01", ctx, [ctx.side(u["pid"], {"definition": u["value"]})
                                         for u in uniq[:4]],
                            rs[0]["subject"], f"检出 {len(uniq)} 个实质不同的定义", seq))
    return out


def r_L02(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("acronym"):
        if r["subject"] and r["value"]:
            groups[r["subject"].strip().upper()].append(r)
    for key, rs in groups.items():
        uniq = {}
        for r in rs:
            uniq.setdefault(normalize_key(r["value"]), r)
        if len(uniq) >= 2:
            out.append(make("L02", ctx, [ctx.side(r["pid"], {"expansion": r["value"]})
                                         for r in list(uniq.values())[:4]],
                            key, f"检出 {len(uniq)} 个不同展开", seq))
    return out


def r_L03(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("acronym"):
        if r["subject"]:
            groups[r["subject"].strip().upper()].append(r)
    for key, rs in groups.items():
        rs.sort(key=lambda r: ctx.sort_key(r["pid"] or ""))
        if not any((r["value"] or "").strip() for r in rs):
            out.append(make("L03", ctx, [ctx.side(rs[0]["pid"])], key,
                            "全文未检索到该缩略语的展开形式", seq))
    return out


def r_L04(ctx, seq):
    out = []
    dist = int((ctx.cfg.get("logic") or {}).get("entity_similar_distance") or 2)
    alias = []
    for e in ctx.glossary.get("entries", []):
        forms = {normalize_key(x) for x in
                 [e.get("key"), e.get("preferred"), *(e.get("variants") or []),
                  *[(f.get("form") if isinstance(f, dict) else f)
                    for f in (e.get("forbidden") or [])]] if x}
        if forms:
            alias.append(forms)
    names: dict[str, dict] = {}
    for r in ctx.rows("entity"):
        if not r["subject"]:
            continue
        names.setdefault(normalize_key(r["subject"]), r)
        for v in (r["meta"].get("variants_seen") or []):
            names.setdefault(normalize_key(str(v)), {**r, "subject": str(v)})
    keys = sorted(names)
    seen = set()
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if (a, b) in seen or a == b:
                continue
            if abs(len(a) - len(b)) > dist or levenshtein(a, b, cap=dist) > dist:
                continue
            if any(a in g and b in g for g in alias):
                continue                      # 已登记为别名组，属 N7，不报
            seen.add((a, b))
            out.append(make("L04", ctx,
                            [ctx.side(names[a]["pid"], {"form": names[a]["subject"]}),
                             ctx.side(names[b]["pid"], {"form": names[b]["subject"]})],
                            names[a]["subject"], "写法相近且未登记为别名组", seq))
    return out


def r_L05(ctx, seq):
    """仅对 cluster_confirmed=true 的概念族生效——未确认的族不产生判定力。"""
    out = []
    clusters = defaultdict(list)
    for e in ctx.glossary.get("entries", []):
        if e.get("cluster_id") and e.get("cluster_confirmed"):
            clusters[e["cluster_id"]].append(e)
    if not clusters:
        return out
    used = defaultdict(list)
    for r in ctx.rows("term") + ctx.rows("entity"):
        if r["subject"]:
            used[normalize_key(r["subject"])].append(r)
    for cid, entries in clusters.items():
        hits = []
        for e in entries:
            for form in {e.get("key"), e.get("preferred"), *(e.get("variants") or [])}:
                if form and normalize_key(form) in used:
                    hits.append((form, used[normalize_key(form)][0]))
        forms = {h[0] for h in hits}
        if len(forms) >= 2:
            out.append(make("L05", ctx, [ctx.side(r["pid"], {"form": f}) for f, r in hits[:4]],
                            cid, f"概念族内混用 {len(forms)} 种写法", seq))
    return out


# 数值与量纲
def r_L06(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("metric"):
        if not r["subject"] or r["value"] in (None, ""):
            continue
        # 按 (subject, scope, kind) 分组：目标 vs 实测 的差异由 L27 负责，不在此处误报
        groups[(normalize_key(r["subject"]), normalize_key(r["scope"] or ""),
                (r["meta"].get("kind") or "目标"))].append(r)
    for (subj, scope, kind), rs in groups.items():
        buckets: dict[str, dict] = {}
        for r in rs:
            base = to_base(r["value"], r["unit"])
            key = f"{base[0]}|{base[1]}" if base else normalize_key(str(r["value"]))
            buckets.setdefault(key, r)
        if len(buckets) >= 2:
            picks = list(buckets.values())[:4]
            # 批注是给评审人看的，note 里不能出现 kind= / scope= 这类字段名
            vals = "、".join(f"{r['value']}{r['unit'] or ''}" for r in picks)
            where = f"（适用范围：{rs[0]['scope']}）" if rs[0].get("scope") else ""
            out.append(make("L06", ctx,
                            [ctx.side(r["pid"], {"value": r["value"], "unit": r["unit"],
                                                 "qualifier": r["qualifier"]}) for r in picks],
                            rs[0]["subject"],
                            f"「{rs[0]['subject']}」的{kind}值出现 {len(buckets)} 种：{vals}{where}",
                            seq))
    return out


def r_L07(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("metric"):
        if r["subject"] and r["unit"]:
            groups[normalize_key(r["subject"])].append(r)
    for subj, rs in groups.items():
        units = {}
        for r in rs:
            units.setdefault(norm_unit(r["unit"]), r)
        if len(units) >= 2:
            out.append(make("L07", ctx, [ctx.side(r["pid"], {"unit": r["unit"], "value": r["value"]})
                                         for r in list(units.values())[:4]],
                            rs[0]["subject"], "单位不一致：" + "、".join(units), seq))
    return out


def r_L08(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("metric"):
        if r["subject"]:
            groups[(normalize_key(r["subject"]), normalize_key(r["scope"] or ""))].append(r)
    for key, rs in groups.items():
        lows = [r for r in rs if (r["qualifier"] or "") in ("≥", ">=", ">", "不低于", "下限", "最小")]
        highs = [r for r in rs if (r["qualifier"] or "") in ("≤", "<=", "<", "不超过", "上限", "最大")]
        for lo in lows:
            for hi in highs:
                a, b = to_base(lo["value"], lo["unit"]), to_base(hi["value"], hi["unit"])
                if a and b and a[1] == b[1] and a[0] > b[0]:
                    out.append(make("L08", ctx,
                                    [ctx.side(lo["pid"], {"bound": "下限", "value": lo["value"]}),
                                     ctx.side(hi["pid"], {"bound": "上限", "value": hi["value"]})],
                                    lo["subject"], f"下限 {lo['value']} 大于上限 {hi['value']}", seq))
    return out


def r_L09(ctx, seq):
    out = []
    tol = float((ctx.cfg.get("logic") or {}).get("percent_tolerance") or 0.5)
    groups = defaultdict(list)
    for r in ctx.rows("metric"):
        if norm_unit(r["unit"]) == "%" and parse_num(r["value"]) is not None:
            groups[normalize_key(r["scope"] or "")].append(r)
    for scope, rs in groups.items():
        if not (3 <= len(rs) <= 12):
            continue
        total = sum(parse_num(r["value"]) or 0 for r in rs)
        # 只在"看起来是一组构成比"时才报：接近 100 但不等于 100
        if 80 <= total <= 120 and abs(total - 100) > tol:
            out.append(make("L09", ctx, [ctx.side(r["pid"], {"value": r["value"]}) for r in rs[:6]],
                            scope or "全局", f"{len(rs)} 项百分比合计 {total:g}%，不等于 100%", seq))
    return out


def r_L10(ctx, seq):
    out = []
    for r in ctx.rows("enumeration"):
        dec = parse_num(r["value"])
        listed = parse_num(r["meta"].get("count_listed"))
        if dec is None or listed is None or dec == listed:
            continue
        rule = make("L10", ctx, [ctx.side(r["pid"], {"declared": dec, "listed": listed})],
                    r["subject"] or "", f"声明 {dec:g} 项，实际列举 {listed:g} 项", seq)
        # 能唯一确定正确数字时可生成修订，否则批注
        rule["action"] = "revision" if listed and listed == int(listed) else "comment"
        rule["suggest"] = {"from_count": dec, "to_count": listed}
        out.append(rule)
    return out


def r_L11(ctx, seq):
    out = []
    pat = re.compile(r"(翻[一]?番|翻倍|提升一倍|增长(\d+)倍|增加(\d+)倍)")
    seen_pids = set()
    for r in ctx.rows("commitment") + ctx.rows("conclusion") + ctx.rows("metric"):
        if r["pid"] in seen_pids:
            continue
        text = (ctx.paras.get(r["pid"] or "") or {}).get("text") or ""
        m = pat.search(text)
        if not m:
            continue
        seen_pids.add(r["pid"])
        nums = [float(x.replace(",", "")) for x in NUM_RE.findall(text.replace("，", ""))]
        nums = [n for n in nums if n > 0]
        if len(nums) < 2:
            continue
        want = 2.0
        if m.group(2) or m.group(3):
            want = float(m.group(2) or m.group(3)) + 1
        ratio = max(nums[:2]) / min(nums[:2])
        if not (want * 0.9 <= ratio <= want * 1.1):
            out.append(make("L11", ctx, [ctx.side(r["pid"], {"ratio": round(ratio, 2),
                                                             "expected": want})],
                            r["subject"] or "", f"文字称「{m.group(0)}」但数值比为 {ratio:.2f}", seq))
    return out


# 时间与版本
def r_L12(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("date"):
        if r["subject"] and r["value"]:
            groups[normalize_key(r["subject"])].append(r)
    for subj, rs in groups.items():
        uniq = {}
        for r in rs:
            d = parse_date(r["value"])
            uniq.setdefault(d or normalize_key(str(r["value"])), r)
        if len(uniq) >= 2:
            out.append(make("L12", ctx, [ctx.side(r["pid"], {"date": r["value"]})
                                         for r in list(uniq.values())[:4]],
                            rs[0]["subject"], f"同一事件出现 {len(uniq)} 个不同日期", seq))
    return out


def r_L13(ctx, seq):
    out = []
    CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
    phases = []
    for r in ctx.rows("date"):
        subj = r["subject"] or ""
        m = PHASE_RE.search(subj)
        if not m:
            continue
        tok = m.group(1) or m.group(2)
        idx = CN.get(tok, None) if tok in CN else (int(tok) if str(tok).isdigit() else None)
        d = parse_date(r["value"])
        if idx and d:
            phases.append((idx, d, r))
    phases.sort(key=lambda x: x[0])
    for i in range(1, len(phases)):
        if phases[i][1] < phases[i - 1][1]:
            out.append(make("L13", ctx,
                            [ctx.side(phases[i - 1][2]["pid"],
                                      {"phase": phases[i - 1][0], "date": phases[i - 1][2]["value"]}),
                             ctx.side(phases[i][2]["pid"],
                                      {"phase": phases[i][0], "date": phases[i][2]["value"]})],
                            phases[i][2]["subject"] or "",
                            f"第 {phases[i][0]} 阶段日期早于第 {phases[i-1][0]} 阶段", seq))
    return out


def r_L14(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("version"):
        if r["subject"] and r["value"]:
            groups[normalize_key(r["subject"])].append(r)
    for subj, rs in groups.items():
        rs.sort(key=lambda r: ctx.sort_key(r["pid"] or ""))
        uniq = {}
        for r in rs:
            uniq.setdefault(parse_version(r["value"]) or r["value"], r)
        if len(uniq) >= 2:
            vs = [(parse_version(r["value"]), r) for r in uniq.values()]
            regress = any(vs[i][0] and vs[i - 1][0] and vs[i][0] < vs[i - 1][0]
                          for i in range(1, len(vs)))
            out.append(make("L14", ctx, [ctx.side(r["pid"], {"version": r["value"]})
                                         for _, r in vs[:4]], rs[0]["subject"],
                            "版本号倒退" if regress else f"出现 {len(uniq)} 个不同版本号", seq))
    return out


# 引用与编号
def _known_targets(ctx) -> dict:
    sections = set()
    for h in ctx.headings:
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)*)", h["text"])
        if m:
            sections.add(m.group(1))
    labels = set()
    for p in ctx.paras.values():
        for m in LABEL_RE.finditer(p["text"]):
            labels.add(f"{m.group(1)}{m.group(2)}-{m.group(3)}")
    appendix = {m.group(1) for p in ctx.paras.values()
                for m in [re.match(r"^\s*附录\s*([A-Za-z0-9一二三四五六七八九十]+)", p["text"])] if m}
    return {"section": sections, "figure": labels, "table": labels, "appendix": appendix}


def r_L15(ctx, seq):
    out = []
    known = _known_targets(ctx)
    for r in ctx.rows("xref"):
        typ = (r["subject"] or "section").strip().lower()
        tgt = (r["value"] or "").strip()
        if not tgt:
            continue
        if typ.startswith("sec") or typ in ("章节", "section"):
            norm = tgt.lstrip("第§").rstrip("节章")
            if SECTION_RE.match(norm) and norm not in known["section"]:
                out.append(make("L15", ctx, [ctx.side(r["pid"], {"target": tgt})], tgt,
                                f"未检索到章节 {tgt}", seq))
        elif typ in ("figure", "图", "table", "表"):
            key = tgt if re.match(r"^[图表]", tgt) else None
            if key and key.replace(" ", "") not in {x.replace(" ", "") for x in known["figure"]}:
                out.append(make("L15", ctx, [ctx.side(r["pid"], {"target": tgt})], tgt,
                                f"未检索到 {tgt}", seq))
        elif typ in ("appendix", "附录"):
            if tgt.replace("附录", "").strip() not in known["appendix"]:
                out.append(make("L15", ctx, [ctx.side(r["pid"], {"target": tgt})], tgt,
                                f"未检索到附录 {tgt}", seq))
    return out


def r_L16(ctx, seq):
    out = []
    series = defaultdict(list)
    for p in ctx.paras.values():
        t = p["text"].strip()
        m = LABEL_RE.match(t)
        # 只统计图表标题（段首编号 + 标题文字）；正文中的「如图 3-7 所示」是引用，不占编号
        if m and len(t) > len(m.group(0)) + 1:
            series[(m.group(1), m.group(2))].append((int(m.group(3)), p["pid"], m.group(0)))
    for (kind, chapter), items in series.items():
        nums = sorted({n for n, _, _ in items})
        dup = [n for n in nums if sum(1 for x, _, _ in items if x == n) > 1]
        # 重号：同一编号出现在多个不同段落（正文引用不算，这里用标题式出现近似）
        gaps = [n for n in range(min(nums), max(nums)) if n not in nums] if len(nums) > 1 else []
        if gaps:
            first = next(i for i in items if i[0] == nums[0])
            out.append(make("L16", ctx, [ctx.side(first[1], {"label": first[2]})],
                            f"{kind}{chapter}",
                            f"{kind}编号跳号：缺 " + "、".join(f"{kind}{chapter}-{g}" for g in gaps), seq))
        for n in sorted(set(dup)):
            occ = [i for i in items if i[0] == n]
            if len({o[1] for o in occ}) > 1:
                out.append(make("L16", ctx, [ctx.side(o[1], {"label": o[2]}) for o in occ[:4]],
                                f"{kind}{chapter}-{n}", f"{kind}编号重号", seq))
    return out


def r_L17(ctx, seq):
    out = []
    toc = [p for p in ctx.paras.values()
           if str(p.get("style", "")).lower().startswith("toc")
           or str(p.get("style_name", "")).lower().startswith("toc")]
    if not toc:
        return out
    body = {normalize_ws(re.sub(r"^[0-9.\s]+", "", h["text"])): h for h in ctx.headings}
    for p in toc:
        txt = re.sub(r"[.…]{2,}\s*[0-9]+\s*$", "", p["text"]).strip()
        core = normalize_ws(re.sub(r"^[0-9.\s]+", "", txt))
        if not core or core in body:
            continue
        near = min(body, key=lambda b: levenshtein(core, b, cap=8), default=None)
        if near and 0 < levenshtein(core, near, cap=8) <= 8:
            out.append(make("L17", ctx, [ctx.side(p["pid"], {"toc": txt}),
                                         ctx.side(body[near]["pid"], {"heading": body[near]["text"]})],
                            txt, "目录条目与正文标题文本不一致", seq))
    return out


def r_L18(ctx, seq):
    out = []
    captions, refs = {}, set()
    for p in ctx.paras.values():
        t = p["text"].strip()
        m = LABEL_RE.match(t)
        if m and len(t) > len(m.group(0)) + 1:
            captions[f"{m.group(1)}{m.group(2)}-{m.group(3)}"] = p["pid"]
        for mm in LABEL_RE.finditer(t):
            if not (m and mm.start() == 0):
                refs.add(f"{mm.group(1)}{mm.group(2)}-{mm.group(3)}")
    for r in ctx.rows("xref"):
        if r["value"]:
            refs.add(str(r["value"]).replace(" ", ""))
    for label, pid in captions.items():
        if label not in refs and label.replace(" ", "") not in refs:
            out.append(make("L18", ctx, [ctx.side(pid, {"label": label})], label,
                            f"{label} 有标题但正文中未检索到引用", seq))
    return out


def r_L19(ctx, seq):
    out = []
    prev = None
    for h in ctx.headings:
        if prev is not None and h["level"] > prev["level"] + 1:
            out.append(make("L19", ctx, [ctx.side(prev["pid"], {"level": prev["level"]}),
                                         ctx.side(h["pid"], {"level": h["level"]})],
                            h["text"], f"由 H{prev['level']} 直接跳至 H{h['level']}", seq))
        prev = h
    return out


# 结构与论断
def _status_bucket(v: str) -> str | None:
    s = normalize_ws(v or "")
    for neg in STATUS_NEG:
        if neg in s:
            return "neg"
    for pos in STATUS_POS:
        if pos in s:
            return "pos"
    return None


def r_L20(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("status"):
        if r["subject"] and r["value"]:
            groups[normalize_key(r["subject"])].append(r)
    for subj, rs in groups.items():
        pos = [r for r in rs if _status_bucket(r["value"]) == "pos"]
        neg = [r for r in rs if _status_bucket(r["value"]) == "neg"]
        if pos and neg:
            out.append(make("L20", ctx,
                            [ctx.side(neg[0]["pid"], {"status": neg[0]["value"]}),
                             ctx.side(pos[0]["pid"], {"status": pos[0]["value"]})],
                            rs[0]["subject"],
                            f"状态矛盾：「{neg[0]['value']}」与「{pos[0]['value']}」", seq))
    return out


def r_L21(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("commitment"):
        if r["subject"]:
            groups[normalize_key(r["subject"])].append(r)
    for subj, rs in groups.items():
        scored = []
        for r in rs:
            m = r["meta"].get("modality") or ""
            lvl = next((v for k, v in MODALITY.items() if k and k in str(m)), None)
            if lvl:
                scored.append((lvl, r))
        if scored and max(s[0] for s in scored) - min(s[0] for s in scored) >= 2:
            hi = max(scored, key=lambda x: x[0])[1]
            lo = min(scored, key=lambda x: x[0])[1]
            out.append(make("L21", ctx,
                            [ctx.side(hi["pid"], {"modality": hi["meta"].get("modality")}),
                             ctx.side(lo["pid"], {"modality": lo["meta"].get("modality")})],
                            rs[0]["subject"], "情态强度冲突", seq))
    return out


def r_L22(ctx, seq):
    out = []
    by_duty = defaultdict(list)
    for r in ctx.rows("role"):
        if r["subject"] and r["value"]:
            by_duty[normalize_key(r["value"])].append(r)
    for duty, rs in by_duty.items():
        roles = {}
        for r in rs:
            roles.setdefault(normalize_key(r["subject"]), r)
        if len(roles) >= 2:
            out.append(make("L22", ctx, [ctx.side(r["pid"], {"role": r["subject"], "duty": r["value"]})
                                         for r in list(roles.values())[:4]],
                            rs[0]["value"], f"同一职责分配给 {len(roles)} 个不同角色", seq))
    return out


def r_L23(ctx, seq):
    out = []
    statuses = [r for r in ctx.rows("status") if r["subject"] and r["value"]]
    for c in ctx.rows("conclusion"):
        text = normalize_ws(str(c["value"] or ""))
        if not any(k in text for k in ("已完成", "已建成", "全面完成", "均已", "已实现", "已支持")):
            continue
        scope = normalize_key(c["subject"] or "")
        for s in statuses:
            if _status_bucket(s["value"]) != "neg":
                continue
            sp = ctx.paras.get(s["pid"] or "") or {}
            in_scope = scope and any(scope in normalize_key(x) for x in (sp.get("heading_path") or []))
            if in_scope or (s["subject"] and normalize_key(s["subject"]) in text):
                out.append(make("L23", ctx,
                                [ctx.side(c["pid"], {"conclusion": c["value"]}),
                                 ctx.side(s["pid"], {"status": s["value"], "subject": s["subject"]})],
                                c["subject"] or "", "结论与范围内的状态记录矛盾", seq))
                break
    return out


def r_L24(ctx, seq):
    out = []
    CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
    pat = re.compile(r"(?:综上所述|如上所述|上述|以上)[^。]{0,10}?([0-9一二两三四五六七八九])\s*[点条项方面]")
    enums = sorted(ctx.rows("enumeration"), key=lambda r: ctx.sort_key(r["pid"] or ""))
    for c in ctx.rows("conclusion") + ctx.rows("commitment"):
        text = (ctx.paras.get(c["pid"] or "") or {}).get("text") or ""
        m = pat.search(text)
        if not m:
            continue
        tok = m.group(1)
        declared = CN.get(tok) or (int(tok) if tok.isdigit() else None)
        if not declared:
            continue
        prior = [e for e in enums if ctx.sort_key(e["pid"] or "") < ctx.sort_key(c["pid"] or "")]
        if not prior:
            continue
        listed = parse_num(prior[-1]["meta"].get("count_listed")) or parse_num(prior[-1]["value"])
        if listed and int(listed) != declared:
            out.append(make("L24", ctx, [ctx.side(c["pid"], {"declared": declared}),
                                         ctx.side(prior[-1]["pid"], {"listed": listed})],
                            c["subject"] or "", f"总结称 {declared} 项，前文列举 {listed:g} 项", seq))
    return out


# 术语规范（仅在存在 authoritative 层时激活）
def _bare_occurrences(text: str, form: str, longer_forms: list[str]) -> int:
    """统计 form 作为独立写法出现的次数。

    两个必须处理的坑：
      1. 变体是标准写法的子串时会假命中——「星云」出现在「星云平台」里并不是变体误用。
         凡是被更长的登记写法覆盖的位置一律跳过。
      2. ASCII 形式需要词边界，否则「EN」会命中「OPEN」「WHEN」。
    """
    if not form or form not in text:
        return 0
    covered: set[int] = set()
    for lf in longer_forms:
        if not lf or len(lf) <= len(form):
            continue
        start = 0
        while (i := text.find(lf, start)) != -1:
            covered.update(range(i, i + len(lf)))
            start = i + 1
    ascii_form = form.isascii() and any(c.isalnum() for c in form)
    n, start = 0, 0
    while (i := text.find(form, start)) != -1:
        start = i + 1
        if any(j in covered for j in range(i, i + len(form))):
            continue
        if ascii_form:
            before = text[i - 1] if i > 0 else " "
            after = text[i + len(form)] if i + len(form) < len(text) else " "
            if (before.isascii() and before.isalnum()) or (after.isascii() and after.isalnum()):
                continue
        n += 1
    return n


def _auth_entries(ctx):
    return [e for e in ctx.glossary.get("entries", [])
            if "authoritative" in (e.get("sources") or [e.get("source")])]


def r_L25(ctx, seq):
    out = []
    if not ctx.glossary.get("has_authoritative"):
        return out
    for e in _auth_entries(ctx):
        pref = e.get("preferred") or e.get("key")
        for f in (e.get("forbidden") or []):
            form = f.get("form") if isinstance(f, dict) else f
            if not form:
                continue
            others = [pref, *(e.get("variants") or [])]
            for p in ctx.paras.values():
                if p["is_code"] or not _bare_occurrences(p["text"], form, others):
                    continue
                c = make("L25", ctx, [ctx.side(p["pid"], {"form": form, "preferred": pref})],
                         pref, f"使用了禁用写法「{form}」，标准写法为「{pref}」", seq)
                c["suggest"] = {"original_text": form, "suggested_text": pref}
                out.append(c)
    return out


def r_L26(ctx, seq):
    out = []
    if not ctx.glossary.get("has_authoritative"):
        return out
    for e in _auth_entries(ctx):
        if e.get("enforce") != "error":
            continue
        pref = e.get("preferred") or e.get("key")
        for v in (e.get("variants") or []):
            if not v or v == pref:
                continue
            others = [pref, *(x for x in (e.get("variants") or []) if x != v)]
            for p in ctx.paras.values():
                if p["is_code"] or not _bare_occurrences(p["text"], v, others):
                    continue
                c = make("L26", ctx, [ctx.side(p["pid"], {"form": v, "preferred": pref})],
                         pref, f"使用了变体「{v}」而非标准写法「{pref}」", seq)
                c["suggest"] = {"original_text": v, "suggested_text": pref}
                out.append(c)
    return out


# 立场与论证覆盖性
def r_L27(ctx, seq):
    out = []
    ratio = float((ctx.cfg.get("logic") or {}).get("gap_ratio") or 3)
    groups = defaultdict(lambda: defaultdict(list))
    for r in ctx.rows("metric"):
        k = (r["meta"].get("kind") or "").strip()
        if r["subject"] and k in ("目标", "实测"):
            groups[normalize_key(r["subject"])][k].append(r)
    for subj, byk in groups.items():
        if "目标" not in byk or "实测" not in byk:
            continue
        for t in byk["目标"]:
            for a in byk["实测"]:
                bt, ba = to_base(t["value"], t["unit"]), to_base(a["value"], a["unit"])
                if not bt or not ba or bt[1] != ba[1] or min(bt[0], ba[0]) <= 0:
                    continue
                gap = max(bt[0], ba[0]) / min(bt[0], ba[0])
                if gap > ratio:
                    out.append(make("L27", ctx,
                                    [ctx.side(t["pid"], {"kind": "目标", "value": t["value"]}),
                                     ctx.side(a["pid"], {"kind": "实测", "value": a["value"]})],
                                    t["subject"], f"目标与实测相差 {gap:.1f} 倍（阈值 {ratio}）", seq))
    return out


def r_L28(ctx, seq):
    out = []
    groups = defaultdict(list)
    for r in ctx.rows("position"):
        if r["subject"] and r["value"]:
            groups[normalize_key(r["subject"])].append(r)
    for subj, rs in groups.items():
        stances = {}
        for r in rs:
            stances.setdefault(normalize_key(r["value"]), r)
        if len(stances) >= 2:
            out.append(make("L28", ctx, [ctx.side(r["pid"], {"stance": r["value"]})
                                         for r in list(stances.values())[:4]],
                            rs[0]["subject"], f"出现 {len(stances)} 种不同立场", seq))
    return out


def r_L29(ctx, seq):
    out = []
    served = {normalize_key(str(r["meta"].get("serves_objective") or ""))
              for r in ctx.rows("initiative")} - {""}
    for o in ctx.rows("objective"):
        oid = normalize_key(str(o["subject"] or ""))
        if oid and oid not in served:
            out.append(make("L29", ctx, [ctx.side(o["pid"], {"objective": o["value"]})],
                            o["subject"] or "",
                            f"未检索到与目标 {o['subject']} 对应的举措，请确认", seq))
    return out


def r_L30(ctx, seq):
    out = []
    targets = {normalize_key(str(r["subject"] or "")) for r in ctx.rows("acceptance")} - {""}
    for i in ctx.rows("initiative"):
        iid = normalize_key(str(i["subject"] or ""))
        if iid and iid not in targets:
            out.append(make("L30", ctx, [ctx.side(i["pid"], {"initiative": i["value"]})],
                            i["subject"] or "",
                            f"未检索到与举措 {i['subject']} 对应的验收指标，请确认", seq))
    return out


def r_L31(ctx, seq):
    """量化承诺是否有对应的度量方式描述。

    判据必须是确定性的：取「提及该承诺主体的段落」及其后 2 段作为局部窗口，
    在窗口内查找度量类词汇。不使用扁平语料的字符距离——那种邻近度是排版的
    副产物，会让规则在不相干的文档改动后忽然改变结论。
    """
    out = []
    plist = list(ctx.paras.values())
    for c in ctx.rows("commitment"):
        text = str(c["value"] or "") + " " + ((ctx.paras.get(c["pid"] or "") or {}).get("text") or "")
        if not NUM_RE.search(text):
            continue                                   # 只处理量化承诺，不处理定性论断
        subj = (c["subject"] or "").strip()
        if not subj:
            continue
        window = []
        for i, p in enumerate(plist):
            if subj in p["text"]:
                window.extend(x["text"] for x in plist[i:i + 3])
        if any(w in "\n".join(window) for w in MEASURE_WORDS):
            continue
        out.append(make("L31", ctx, [ctx.side(c["pid"], {"commitment": c["value"]})], subj,
                        f"未检索到与承诺「{subj}」对应的度量方式或验证描述，请确认", seq))
    return out


def r_L32(ctx, seq):
    out = []
    hs = ctx.headings
    plist = list(ctx.paras.values())
    idx = {p["pid"]: i for i, p in enumerate(plist)}
    for n, h in enumerate(hs):
        key = next((k for k in SECTION_PROMISE if k in h["text"]), None)
        if not key:
            continue
        start = idx.get(h["pid"], 0) + 1
        end = idx.get(hs[n + 1]["pid"], len(plist)) if n + 1 < len(hs) else len(plist)
        body = "\n".join(p["text"] for p in plist[start:end])
        if len(normalize_ws(body)) < 20 or not any(w in body for w in SECTION_PROMISE[key]):
            out.append(make("L32", ctx, [ctx.side(h["pid"], {"heading": h["text"]})], h["text"],
                            f"未检索到与「{h['text']}」承诺内容对应的正文条目，请确认", seq))
    return out


DETECTORS = {name.split("_")[1]: fn for name, fn in list(globals().items())
             if name.startswith("r_L")}


# --------------------------------------------------------------------------
def run(run_dir: Path, cfg: dict, rules: list[str] | None, force: bool) -> dict:
    ctx = Ctx(run_dir, cfg)
    cdir = resolve_path(run_dir, "conflicts_candidate")
    guard_write_path(cdir, run_dir)
    cdir.mkdir(parents=True, exist_ok=True)

    todo = rules or sorted(RULES)
    seq: dict[str, int] = {}
    summary, total = {}, 0
    try:
        for rule in todo:
            fn = DETECTORS.get(rule)
            out_path = cdir / f"conflicts-candidate.{rule}.json"
            if fn is None:
                continue
            if out_path.exists() and not force:
                prev = read_json(out_path, {}) or {}
                summary[rule] = len(prev.get("candidates", []))
                total += summary[rule]
                continue                       # 已完成的规则不重算
            try:
                found = fn(ctx, seq)
            except Exception as exc:  # noqa: BLE001 - 单条规则失败不拖垮整轮
                found = []
                summary[rule + "_error"] = str(exc)[:200]
            guard_write_path(out_path, run_dir)
            atomic_write_json(out_path, {**version_header(), "rule": rule,
                                         "severity": RULES[rule][0],
                                         "candidates": found})
            summary[rule] = len(found)
            total += len(found)
    finally:
        ctx.con.close()

    index = {**version_header(), "total": total, "by_rule": summary,
             "rules_run": todo,
             "coverage_rules_to_comment": bool((cfg.get("logic") or {})
                                               .get("coverage_rules_to_comment"))}
    idx_path = cdir / "index.json"
    guard_write_path(idx_path, run_dir)
    atomic_write_json(idx_path, index)
    return {"total": total, "by_rule": summary, "dir": str(cdir)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="detect_conflicts.py", description="Pass 3 冲突检测")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--rules", help="逗号分隔，默认全部")
    ap.add_argument("--force", action="store_true", help="重算已完成的规则")
    ap.add_argument("--config")
    ap.add_argument("--session")
    ap.add_argument("--generation", type=int)
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    cfg = load_config(args.config)
    doc_dir = run_dir.parent
    if args.session:
        lease_verify(doc_dir, args.session, args.generation)
    rules = [r.strip().upper() for r in args.rules.split(",")] if args.rules else None
    # 全量台账两两比对可达数分钟，期间必须后台续租，否则会被误判为死亡
    with Heartbeat(doc_dir, args.session, args.generation,
                   int((cfg.get("concurrency") or {}).get("lease_minutes") or 30), "pass3"):
        res = run(run_dir, cfg, rules, args.force)
    emit({"ok": True, **res})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
