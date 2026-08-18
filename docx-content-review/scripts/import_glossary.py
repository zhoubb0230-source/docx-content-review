#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部术语表导入 + 自检 + 三层合并（spec §9.1）。

三个来源合并为唯一出口 glossary.merged.json，下游只消费 merged、不感知来源——
用户提供或不提供外部表，Pass 1/2/3 的实现完全不变。

  authoritative  优先级 1  enforce=error  既是参照物也是规则来源，可生成修订
  extracted      优先级 2  enforce=warn   Pass 0 自动抽取
  fallback       优先级 3  enforce=off    只作参照物，纯误报抑制资产（供 N7 消费）

导入期自检：层内重复、跨层定义矛盾 → 报错终止，不做猜测性合并。

用法
  import_glossary.py --run-dir <run> [--authoritative <file>] [--fallback <file>]
                     [--extracted <jsonl>] [--config]
  import_glossary.py --inspect <file>              # 只解析并报告，不写盘
支持格式：CSV / XLSX / YAML / JSON / 纯文本（一行一词）
退出码：0 成功；1 失败；10 外部表自身矛盾或不可解析。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, die, emit, normalize_key, read_json, read_jsonl, run_cli,
    sha256_file, sha256_text, version_header,
)
from workspace import guard_write_path, load_run_config, resolve_path  # noqa: E402

PRIORITY = {"authoritative": 1, "extracted": 2, "fallback": 3}
SPLIT_CHARS = [";", "；", "|", "，", ","]
# 列名同义词：用户手上最可能是 Excel 导出的表，不强制其改列名
COLUMN_ALIASES = {
    "key": {"key", "术语", "词条", "名称", "term", "word"},
    "preferred": {"preferred", "标准写法", "规范写法", "推荐", "首选", "正确写法", "standard"},
    "variants": {"variants", "别名", "变体", "同义词", "简称", "alias", "aliases", "synonym"},
    "forbidden": {"forbidden", "禁用", "错误写法", "禁止", "误写", "deprecated"},
    "definition": {"definition", "定义", "释义", "说明", "含义", "desc", "description"},
    "scope": {"scope", "适用范围", "范围", "章节"},
    "enforce": {"enforce", "强制级别", "级别", "强度"},
    "case_sensitive": {"case_sensitive", "区分大小写", "大小写敏感"},
}


def _split_multi(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v)
    for ch in SPLIT_CHARS:
        s = s.replace(ch, "\x00")
    return [x.strip() for x in s.split("\x00") if x.strip()]


def _map_row(row: dict) -> dict:
    out: dict = {}
    for canon, names in COLUMN_ALIASES.items():
        for k, v in row.items():
            if k is None:
                continue
            if str(k).strip().lower() in names:
                out[canon] = v
                break
    return out


def _norm_entry(raw: dict, layer: str, defaults: dict) -> dict | None:
    key = str(raw.get("key") or "").strip()
    if not key:
        return None
    cs = raw.get("case_sensitive")
    cs = str(cs).strip().lower() in ("1", "true", "yes", "y", "是", "真") if cs is not None else False
    forb = []
    for f in _split_multi(raw.get("forbidden")):
        forb.append({"form": f, "reason": ""} if not isinstance(f, dict) else f)
    enforce = str(raw.get("enforce") or defaults.get(layer) or "warn").strip().lower()
    if enforce not in ("error", "warn", "off"):
        enforce = defaults.get(layer) or "warn"
    return {
        "key": key,
        "preferred": str(raw.get("preferred") or key).strip(),
        "variants": _split_multi(raw.get("variants")),
        "forbidden": forb,
        "definition": (str(raw.get("definition")).strip() if raw.get("definition") else ""),
        "source": layer,
        "priority": PRIORITY[layer],
        "scope": str(raw.get("scope") or "global").strip() or "global",
        "case_sensitive": cs,
        "enforce": enforce,
        "cluster_id": raw.get("cluster_id"),
        "cluster_confirmed": bool(raw.get("cluster_confirmed")) if layer == "authoritative"
        else False,
    }


def parse_file(path: Path, layer: str, defaults: dict) -> list[dict]:
    """CSV / XLSX / YAML / JSON / 纯文本，五种输入。"""
    suffix = path.suffix.lower()
    rows: list[dict] = []
    try:
        if suffix in (".json",):
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("entries", data) if isinstance(data, dict) else data
            rows = [x if isinstance(x, dict) else {"key": x} for x in items]
        elif suffix in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            items = data.get("entries", data) if isinstance(data, dict) else data
            rows = [x if isinstance(x, dict) else {"key": x} for x in (items or [])]
        elif suffix in (".csv", ".tsv"):
            text = path.read_text(encoding="utf-8-sig")
            dialect = csv.excel_tab if suffix == ".tsv" else csv.excel
            rows = [_map_row(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]
        elif suffix in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook

            wb = load_workbook(str(path), read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            it = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(it, [])]
            for r in it:
                if r is None or all(c is None or str(c).strip() == "" for c in r):
                    continue
                rows.append(_map_row(dict(zip(header, r))))
            wb.close()
        elif suffix in (".txt", ".list", ""):
            rows = [{"key": ln.strip()} for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
        else:
            die(EX.USAGE, f"不支持的术语表格式：{suffix}",
                "支持 CSV / XLSX / YAML / JSON / 纯文本（一行一词）。")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, StopIteration) as exc:
        die(EX.PARSE, f"术语表无法解析：{path}（{exc}）")
    except ImportError as exc:
        die(EX.ENV, f"解析 {suffix} 需要额外依赖：{exc}")

    out = []
    for r in rows:
        e = _norm_entry(r, layer, defaults)
        if e:
            out.append(e)
    return out


def selfcheck(layers: dict[str, list[dict]], fail_on_conflict: bool) -> list[str]:
    """层内重复 + 跨层定义矛盾。用户的表自身有矛盾，必须由用户解决。"""
    problems = []
    for layer, entries in layers.items():
        seen: dict[str, dict] = {}
        for e in entries:
            k = normalize_key(e["key"], case_sensitive=e["case_sensitive"])
            if k in seen:
                prev = seen[k]
                if prev["preferred"] != e["preferred"] or (
                        prev["definition"] and e["definition"] and prev["definition"] != e["definition"]):
                    problems.append(
                        f"[{layer}] 层内重复且定义/标准写法冲突：「{e['key']}」"
                        f"（{prev['preferred']} / {e['preferred']}）")
            else:
                seen[k] = e
    auth = {normalize_key(e["key"]): e for e in layers.get("authoritative", [])}
    for e in layers.get("fallback", []):
        k = normalize_key(e["key"])
        a = auth.get(k)
        if a and a["preferred"] != e["preferred"]:
            problems.append(
                f"跨层冲突：「{e['key']}」在 authoritative 中标准写法为「{a['preferred']}」，"
                f"在 fallback 中为「{e['preferred']}」")
        if a and a["definition"] and e["definition"] and a["definition"] != e["definition"]:
            problems.append(f"跨层定义矛盾：「{e['key']}」在 authoritative 与 fallback 中定义不同")
    return problems


def merge(layers: dict[str, list[dict]]) -> list[dict]:
    """按归一化键聚合：高优先级层覆盖 preferred/definition；variants/forbidden 取并集。"""
    acc: dict[str, dict] = {}
    order = sorted(layers.items(), key=lambda kv: PRIORITY[kv[0]])
    for layer, entries in order:
        for e in entries:
            k = normalize_key(e["key"], case_sensitive=e["case_sensitive"])
            if k not in acc:
                acc[k] = {**e, "variants": list(e["variants"]), "forbidden": list(e["forbidden"]),
                          "sources": [layer]}
                continue
            cur = acc[k]
            cur["sources"].append(layer)
            # 低优先级层只贡献并集，不覆盖 preferred / definition
            for v in e["variants"]:
                if v not in cur["variants"]:
                    cur["variants"].append(v)
            forms = {f["form"] for f in cur["forbidden"]}
            for f in e["forbidden"]:
                if f["form"] not in forms:
                    cur["forbidden"].append(f)
                    forms.add(f["form"])
            if not cur["definition"] and e["definition"]:
                cur["definition"] = e["definition"]
            if e["cluster_id"] and not cur.get("cluster_id"):
                cur["cluster_id"] = e["cluster_id"]
            if e["cluster_confirmed"]:
                cur["cluster_confirmed"] = True
    for e in acc.values():
        # preferred 不得同时出现在 variants 中
        e["variants"] = [v for v in e["variants"] if v != e["preferred"]]
    return list(acc.values())


def load_extracted(run_dir: Path, explicit: str | None) -> list[dict]:
    """Pass 0 的 LLM 确认结果（JSONL）→ extracted 层。"""
    path = Path(explicit) if explicit else resolve_path(run_dir, "glossary_extracted")
    if not path.exists():
        return []
    rows = []
    if path.suffix == ".json":
        data = read_json(path, {}) or {}
        rows = data.get("entries", []) if isinstance(data, dict) else data
    else:
        rows = list(read_jsonl(path))
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        e = _norm_entry({
            "key": r.get("term") or r.get("key"),
            "preferred": r.get("preferred") or r.get("term") or r.get("key"),
            "variants": r.get("variants") or r.get("aliases"),
            "definition": r.get("definition"),
            "scope": r.get("scope"),
            "cluster_id": r.get("cluster_id"),
        }, "extracted", {"extracted": "warn"})
        if e:
            out.append(e)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="import_glossary.py", description="术语表导入与三层合并")
    ap.add_argument("--run-dir")
    ap.add_argument("--authoritative")
    ap.add_argument("--fallback")
    ap.add_argument("--extracted", help="Pass 0 确认结果；默认 work/glossary-extracted.json")
    ap.add_argument("--config")
    ap.add_argument("--inspect", help="只解析该文件并报告，不写盘")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve() if args.run_dir else None
    cfg = load_run_config(run_dir, args.config)
    g = cfg.get("glossary") or {}
    defaults = {k: str(v) for k, v in (g.get("enforce_defaults") or {}).items()}

    if args.inspect:
        entries = parse_file(Path(args.inspect), "authoritative", defaults)
        emit({"ok": True, "entries": len(entries), "sample": entries[:5]})
        return EX.OK

    if run_dir is None:
        die(EX.USAGE, "缺少 --run-dir")

    srcs = g.get("sources") or {}
    auth_path = args.authoritative or srcs.get("authoritative")
    fb_path = args.fallback or srcs.get("fallback")

    layers: dict[str, list[dict]] = {}
    src_hashes = {}
    for layer, p in (("authoritative", auth_path), ("fallback", fb_path)):
        if not p:
            continue
        pp = Path(p).expanduser()
        if not pp.exists():
            die(EX.USAGE, f"{layer} 术语表不存在：{pp}")
        layers[layer] = parse_file(pp, layer, defaults)
        src_hashes[layer] = {"path": str(pp), "sha256": sha256_file(pp),
                             "entries": len(layers[layer])}
    layers["extracted"] = load_extracted(run_dir, args.extracted)

    problems = selfcheck(layers, bool(g.get("fail_on_source_conflict", True)))
    if problems and g.get("fail_on_source_conflict", True):
        die(EX.PARSE, "外部术语表自身存在矛盾，已终止（不做猜测性合并）：\n  - " + "\n  - ".join(problems),
            "请修正术语表后重试；技能不会替用户猜测哪一个写法为准。")

    entries = merge(layers)
    # fallback 层产生的任何问题严重度自动降一级，且默认不进文档
    for e in entries:
        if e["source"] == "fallback":
            e["enforce"] = "off"
    payload = {
        **version_header(),
        "entries": entries,
        "layers": {k: len(v) for k, v in layers.items()},
        "sources": src_hashes,
        "has_authoritative": bool(layers.get("authoritative")),
        "problems": problems,
    }
    # 按 hash(外部表内容 + Pass 0 产物 + 合并规则版本) 缓存；哈希变化只失效 Pass 3/4
    payload["merged_hash"] = sha256_text(json.dumps(
        {"s": src_hashes, "e": [e["key"] for e in layers["extracted"]], "v": "1.1"},
        ensure_ascii=False, sort_keys=True))[:16]

    out = resolve_path(run_dir, "glossary_merged")
    guard_write_path(out, run_dir)
    atomic_write_json(out, payload)
    # 提升到文档目录层级，跨 run 复用
    doc_copy = resolve_path(run_dir, "doc_glossary")
    guard_write_path(doc_copy, run_dir, run_dir.parent)
    atomic_write_json(doc_copy, payload)

    emit({"ok": True, "entries": len(entries), "layers": payload["layers"],
          "has_authoritative": payload["has_authoritative"],
          "merged_hash": payload["merged_hash"], "path": str(out),
          "warnings": problems})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
