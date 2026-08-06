#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P 类：特定场景的描述范式审查（references/patterns.md）。

**为什么是数据驱动的**：范式规则是场景特定的，会持续增加。若把规则写进代码，
每加一条"风险条目必须写清影响与应对"就要改三处（类别枚举、闸门、prompt），
这条路走不通。规则因此外置为 YAML 规则包，**加规则不改代码**。

与 D1 / 错别字通道同构的三段式：

    脚本按 scope 确定性定位适用段落（不问模型）
        ↓
    LLM 逐要件封闭判定（「『影响』这一要件出现了吗？只答 Y/N/U」）
        ↓
    只有明确 N 才成 issue；U 按齐备处理（不确定即无问题）→ 过闸门②③

**红线**：P 类永不生成 suggested_text，只出批注或只进报告。任何一条 requires 项，
如果无法用"这个信息出现了吗"来问，就不该进规则包——那是论证链审查，
其误报不可收敛，已由 spec §9.6 关闭。

子命令
  scan   定位适用段落 → work/patterns/patterns-<chunk>.json（含裁定 payload）
  merge  合并裁定结果 → work/issues/issues-<chunk>.patterns.jsonl（category=P1）
  lint   只校验规则包语法与红线，不需要 run 目录（写规则时自查用）
退出码：0 成功 / 2 参数错 / 10 规则包不可解析或违反红线。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_jsonl, die, emit, read_json, read_jsonl, run_cli,
    version_header,
)
from workspace import SKILL_ROOT, guard_write_path, load_config, resolve_path  # noqa: E402

PATTERN_DIR = SKILL_ROOT / "assets" / "patterns"
SEVERITIES = ["Critical", "High", "Medium", "Low"]
ACTIONS = {"comment", "report_only"}
ID_RE = re.compile(r"^P[A-Za-z0-9_-]{2,30}$")


# --------------------------------------------------------------------------
# 规则包加载与校验
# --------------------------------------------------------------------------
def _compile(rx: str | None, where: str):
    if not rx:
        return None
    try:
        return re.compile(rx)
    except re.error as exc:
        die(EX.PARSE, f"{where} 的正则不合法：{rx}", f"错误：{exc}")


def _validate(rule: dict, src: str) -> dict:
    """校验一条规则并编译其正则。任何一项不合法即终止——规则包错了不做猜测性修复。"""
    rid = str(rule.get("id") or "").strip()
    if not ID_RE.match(rid):
        die(EX.PARSE, f"{src}：规则 id 不合法（{rid!r}）",
            "id 必须以 P 开头，仅含字母数字与 -_，长度 3–31，例：P-RISK-01")
    name = str(rule.get("name") or "").strip()
    if not name:
        die(EX.PARSE, f"{src}：规则 {rid} 缺少 name（批注正文要用它，不能省）")

    scope = rule.get("scope") or {}
    if not isinstance(scope, dict):
        die(EX.PARSE, f"{src}：规则 {rid} 的 scope 必须是映射")
    heading_rx = _compile(scope.get("heading_regex"), f"{src} {rid}.scope.heading_regex")
    para_rx = _compile(scope.get("paragraph_regex"), f"{src} {rid}.scope.paragraph_regex")
    excl_rx = _compile(scope.get("exclude_regex"), f"{src} {rid}.scope.exclude_regex")
    if heading_rx is None and para_rx is None:
        die(EX.PARSE, f"{src}：规则 {rid} 的 scope 至少要有 heading_regex 或 paragraph_regex",
            "没有定位条件的规则会命中全文每一段，这是误报的最大来源")

    reqs = rule.get("requires") or []
    if not isinstance(reqs, list) or not reqs:
        die(EX.PARSE, f"{src}：规则 {rid} 的 requires 不能为空")
    keys, norm_reqs = set(), []
    for r in reqs:
        if not isinstance(r, dict):
            die(EX.PARSE, f"{src}：规则 {rid} 的 requires 每项必须是映射")
        k = str(r.get("key") or "").strip()
        label = str(r.get("label") or "").strip()
        if not k or not label:
            die(EX.PARSE, f"{src}：规则 {rid} 的 requires 项缺少 key 或 label")
        if k in keys:
            die(EX.PARSE, f"{src}：规则 {rid} 的 requires 存在重复 key：{k}")
        keys.add(k)
        norm_reqs.append({"key": k, "label": label,
                          "hint": str(r.get("hint") or "").strip(),
                          "optional": bool(r.get("optional"))})

    action = str(rule.get("action") or "comment").strip()
    if action not in ACTIONS:
        die(EX.PARSE, f"{src}：规则 {rid} 的 action 只能是 comment 或 report_only（得到 {action!r}）",
            "P 类永不生成修订，因此没有 revision 这个选项")
    if rule.get("suggested_text") or rule.get("suggest"):
        die(EX.PARSE, f"{src}：规则 {rid} 不得携带建议文本",
            "P 类只指出要件缺失，不代作者落笔——这是 P 类能收敛的前提")
    sev = str(rule.get("severity") or "Medium").strip().capitalize()
    if sev not in SEVERITIES:
        die(EX.PARSE, f"{src}：规则 {rid} 的 severity 不在封闭枚举内：{sev}")

    ex = rule.get("examples") or {}
    pos = [e for e in (ex.get("positive") or []) if isinstance(e, dict) and e.get("text")]
    neg = [e for e in (ex.get("negative") or []) if isinstance(e, dict) and e.get("text")]
    for e in neg:
        if not e.get("why_flag") and not e.get("why_not_flag"):
            die(EX.PARSE, f"{src}：规则 {rid} 的反例必须注明 why_flag 或 why_not_flag",
                "why_flag = 该报的反面例子；why_not_flag = 场景不适用、不该报。"
                "两者的作用相反，不能省略")

    return {
        "id": rid, "name": name, "severity": sev, "action": action,
        "requires": norm_reqs, "source": src,
        "scope": {
            "heading_rx": heading_rx, "para_rx": para_rx, "exclude_rx": excl_rx,
            "min_chars": int(scope.get("min_chars") or 0),
            "max_chars": int(scope.get("max_chars") or 0),
            "heading_only": bool(scope.get("heading_only")),
            "skip_headings": bool(scope.get("skip_headings", True)),
        },
        "examples": {"positive": pos, "negative": neg},
    }


def load_packs(cfg: dict, extra: list[str] | None) -> list[dict]:
    """内置包 → 配置里的 packs → --patterns，后者覆盖同 id 的前者。"""
    import yaml

    pr = cfg.get("pattern_review") or {}
    paths: list[Path] = []
    if pr.get("use_builtin", True) and PATTERN_DIR.exists():
        paths += sorted(PATTERN_DIR.glob("*.yaml")) + sorted(PATTERN_DIR.glob("*.yml"))
    for p in (pr.get("packs") or []) + list(extra or []):
        q = Path(p)
        if not q.exists():
            die(EX.USAGE, f"规则包不存在：{q}")
        paths.append(q)

    merged: dict[str, dict] = {}
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            die(EX.PARSE, f"规则包不是合法 YAML：{path}", str(exc))
        if not isinstance(data, dict):
            die(EX.PARSE, f"规则包顶层必须是映射：{path}")
        rules = data.get("patterns")
        if rules is None:
            die(EX.PARSE, f"规则包缺少 patterns 列表：{path}")
        if not isinstance(rules, list):
            die(EX.PARSE, f"规则包的 patterns 必须是列表：{path}")
        for raw in rules:
            if not isinstance(raw, dict):
                die(EX.PARSE, f"规则包 {path} 的 patterns 每项必须是映射")
            if raw.get("enabled") is False:
                continue
            r = _validate(raw, path.name)
            merged[r["id"]] = r          # 同 id 后加载者覆盖，便于用户改写内置示例
    return list(merged.values())


# --------------------------------------------------------------------------
# 场景定位：全部确定性，不问模型
# --------------------------------------------------------------------------
def applies(rule: dict, para: dict) -> bool:
    sc = rule["scope"]
    text = (para.get("text") or "").strip()
    if not text:
        return False
    # 代码块与表格段落一律不参与：闸门③ 的 N9/N11 会在下游丢弃它们，
    # 在这里就排除可以省掉一次无用的模型调用。
    if para.get("is_code") or para.get("in_table"):
        return False
    if sc["skip_headings"] and para.get("is_heading") and not sc["heading_only"]:
        return False
    if sc["heading_only"] and not para.get("is_heading"):
        return False
    if sc["min_chars"] and len(text) < sc["min_chars"]:
        return False
    if sc["max_chars"] and len(text) > sc["max_chars"]:
        return False
    if sc["heading_rx"] and not sc["heading_rx"].search(" > ".join(para.get("heading_path") or [])):
        return False
    if sc["para_rx"] and not sc["para_rx"].search(text):
        return False
    if sc["exclude_rx"] and sc["exclude_rx"].search(text):
        return False
    return True


def scan(run_dir: Path, cfg: dict, chunk_id: str | None, extra: list[str] | None) -> dict:
    pr = cfg.get("pattern_review") or {}
    if not pr.get("enabled"):
        return {"enabled": False, "candidates": 0,
                "note": "pattern_review.enabled=false（默认关闭）"}
    rules = load_packs(cfg, extra)
    if not rules:
        return {"enabled": True, "rules": 0, "candidates": 0,
                "note": "未提供任何范式规则包，本通道跳过（见 references/patterns.md）"}

    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    chunks = [c for c in idx.get("chunks", []) if not chunk_id or c["chunk_id"] == chunk_id]
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    pdir = resolve_path(run_dir, "patterns")
    guard_write_path(pdir, run_dir)
    pdir.mkdir(parents=True, exist_ok=True)

    cap = int(pr.get("max_pattern_issues_per_chunk") or 20)
    batch = int(pr.get("batch_size") or 20)
    total, results = 0, []
    for c in chunks:
        cands = []
        for pid in c.get("review_pids") or c.get("pids") or []:
            para = paras.get(pid)
            if not para:
                continue
            for rule in rules:
                if not applies(rule, para):
                    continue
                cands.append({
                    "cid": f"{c['chunk_id']}-{len(cands) + 1:03d}",
                    "pid": pid, "pattern_id": rule["id"], "pattern_name": rule["name"],
                    "text": para["text"],
                    "checks": [{"key": r["key"], "label": r["label"], "hint": r["hint"]}
                               for r in rule["requires"]],
                })
        truncated = len(cands) > cap
        cands = cands[:cap]                     # 独立配额，不占用 max_issues_per_chunk
        used = {c2["pattern_id"] for c2 in cands}
        batches = [{"batch_id": f"p{i // batch + 1:02d}", "items": cands[i:i + batch]}
                   for i in range(0, len(cands), batch)]
        payload = {
            **version_header(), "chunk_id": c["chunk_id"], "candidates": cands,
            "truncated": truncated, "batches": batches,
            # 正反例随规则一并注入：写 prompt 时直接取用，不必回读规则包
            "rules": [{"id": r["id"], "name": r["name"],
                       "requires": r["requires"], "examples": r["examples"]}
                      for r in rules if r["id"] in used],
        }
        path = pdir / f"patterns-{c['chunk_id']}.json"
        guard_write_path(path, run_dir)
        atomic_write_json(path, payload)
        total += len(cands)
        results.append({"chunk_id": c["chunk_id"], "candidates": len(cands),
                        "batches": len(batches), "truncated": truncated})
    return {"enabled": True, "rules": len(rules), "candidates": total,
            "chunks": len(results), "results": results}


# --------------------------------------------------------------------------
# 裁定结果合并
# --------------------------------------------------------------------------
def merge(run_dir: Path, cfg: dict, chunk_id: str, extra: list[str] | None) -> dict:
    """裁定 → issues。只有明确「N」（要件不存在）才成条目；「U」按齐备处理。"""
    pr = cfg.get("pattern_review") or {}
    pdir = resolve_path(run_dir, "patterns")
    verdict_path = pdir / f"patterns-{chunk_id}.verdicts.jsonl"
    payload = read_json(pdir / f"patterns-{chunk_id}.json", {}) or {}
    if not verdict_path.exists():
        return {"merged": 0, "note": f"无裁定结果：{verdict_path}"}

    rules = {r["id"]: r for r in load_packs(cfg, extra)}
    cands = {c["cid"]: c for c in payload.get("candidates", [])}
    sev_cap = str(pr.get("severity_cap") or "Medium").capitalize()
    cap_rank = SEVERITIES.index(sev_cap) if sev_cap in SEVERITIES else 2
    unsure_present = bool(pr.get("unsure_as_present", True))

    missing: dict[str, list[str]] = {}
    counters = {"verdicts": 0, "answer_N": 0, "answer_U": 0, "unknown_cid": 0}
    for v in read_jsonl(verdict_path):
        counters["verdicts"] += 1
        cid, key = v.get("cid"), v.get("key")
        ans = str(v.get("answer") or "").strip().upper()[:1]
        if cid not in cands:
            counters["unknown_cid"] += 1
            continue
        if ans == "U" or ans not in ("Y", "N"):
            counters["answer_U"] += 1
            if unsure_present:
                continue                      # 不确定即无问题
            ans = "N"
        if ans != "N":
            continue
        counters["answer_N"] += 1
        missing.setdefault(cid, []).append(key)

    rows = []
    for cid, keys in missing.items():
        c = cands[cid]
        rule = rules.get(c["pattern_id"])
        if not rule:
            continue
        by_key = {r["key"]: r for r in rule["requires"]}
        req_missing = [by_key[k]["label"] for k in keys if k in by_key and not by_key[k]["optional"]]
        opt_missing = [by_key[k]["label"] for k in keys if k in by_key and by_key[k]["optional"]]
        if not req_missing and not opt_missing:
            continue
        # 只缺可选要件 → 降为只进报告，不打扰评审人
        action = rule["action"] if req_missing else "report_only"
        labels = req_missing or opt_missing
        sev = rule["severity"]
        if SEVERITIES.index(sev) < cap_rank:
            sev = sev_cap
        rows.append({
            "chunk_id": chunk_id, "pid": c["pid"], "category": "P1",
            "rule_id": c["pattern_id"], "pattern_name": rule["name"],
            "severity": sev,
            "original_text": c["text"],
            "suggested_text": "",             # P 类永不落笔
            "evidence": ("缺少：" + "、".join(labels))[:25],
            "missing": labels, "source": "pattern_channel", "action": action,
        })

    out = resolve_path(run_dir, "issues") / f"issues-{chunk_id}.patterns.jsonl"
    guard_write_path(out, run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_jsonl(out, rows)
    return {"merged": len(rows), "path": str(out), **counters,
            "note": "需再过 verify_span.py（--in/--out 指向本文件）与 filter_neverflag.py"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="scan_patterns.py", description="P 类范式审查（场景定位 + 要件裁定）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--chunk", required=(name == "merge"))
        p.add_argument("--patterns", action="append", help="额外规则包，可多次")
        p.add_argument("--config")
    p = sub.add_parser("lint", help="只校验规则包，不需要 run 目录")
    p.add_argument("--patterns", action="append")
    p.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(getattr(args, "config", None))

    if args.cmd == "lint":
        rules = load_packs(cfg, args.patterns)
        emit({"ok": True, "rules": len(rules),
              "ids": [r["id"] for r in rules],
              "requires_total": sum(len(r["requires"]) for r in rules),
              "examples_total": sum(len(r["examples"]["positive"]) + len(r["examples"]["negative"])
                                    for r in rules)})
        return EX.OK

    run_dir = Path(args.run_dir).resolve()
    if args.cmd == "scan":
        emit({"ok": True, **scan(run_dir, cfg, args.chunk, args.patterns)})
    else:
        emit({"ok": True, **merge(run_dir, cfg, args.chunk, args.patterns)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
