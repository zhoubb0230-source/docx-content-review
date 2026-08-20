#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸门④：盲测 A/B 二次复核的脚手架（spec §8 闸门④、prompts/pass2-verify.md）。

**四道闸门里唯一靠模型的一道，因此"盲"必须由脚本保证，不能靠 Agent 自觉。**

判定表要求"选中原文所在项才算通过"——也就是说，应用这张表的人必须知道原文在哪一侧。
如果由 Agent 一边拼装 A/B 一边应用判定表，盲测就只是名义上的：
拼装者知道答案。因此本脚本把两件事拆开落到两个文件：

  pass2-<排列>.json       给模型看的 payload —— **不含原文在哪一侧的任何信息**
  pass2-<排列>.key.json   给 merge 用的对照表 —— Agent 不需要读，也不该读

位置由 manifest 的 `ab_seed` 逐条派生（`Random(f"{seed}:{item_id}")`），
因此**可复现**：同一个 run 重跑 build 得到完全相同的排列。
`--arrangement mirror` 生成镜像排列，两次跑完用 `consistency` 得出
M2 的验收指标「两种排列下判定一致率 ≥90%」——低于此说明模型有强位置偏好，需换 prompt。

子命令
  build        拼装待复核集 → work/verify/pass2-<排列>.json（+ .key.json）
  merge        裁定结果 → work/issues-verified.jsonl（按判定表决定 pass/drop）
  consistency  比对 primary 与 mirror 两次裁定 → 一致率
退出码：0 成功 / 2 参数错 / 10 输入不可解析。
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_jsonl, die, emit, normalize_ws, read_json,
    read_jsonl, run_cli, sha256_text, version_header,
)
from workspace import guard_write_path, load_run_config, resolve_path  # noqa: E402

ARRANGEMENTS = ("primary", "mirror")
AB_CLASSES = {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "L25", "L26"}
SINGLE_CLASSES = {"B1", "B2", "B3", "B4", "B5"}
C_CLASSES = {"C1", "C2"}
P_CLASSES = {"P1"}
SINGLE_ASPECT = {"B1": "指代", "B2": "歧义", "B3": "结构", "B4": "施受关系", "B5": "逻辑关系"}

# 复核范围：A 类走 A/B 对照，B 类走封闭单问。
#   C 类不复核——它不生成修订也不生成批注，复核没有意义（taxonomy.md）。
#   P 类不复核——它的裁定本来就是封闭题，且当时**规则包的正反例在上下文里**；
#     Pass 2 没有规则包，再问一遍只会得到信息更少的答案。
REVIEW_SCOPE = {
    "conservative": AB_CLASSES,
    "balanced": AB_CLASSES | SINGLE_CLASSES,
    "thorough": AB_CLASSES | SINGLE_CLASSES,
}
# 保留范围 ≠ 复核范围。**这两件事早先共用了一张表，于是"不复核"被实现成了"丢弃"**：
# `issues-verified.jsonl` 是下游（report / apply_comments / apply_revisions / metrics）
# 的唯一入口，被 collect 滤掉的类别在报告里也一并消失。实测后果：
#   - P 类（范式支线）过完两道闸门后整组蒸发，需求 5 的产出到不了任何交付物；
#   - thorough 与 balanced 完全等价，C 类连报告都进不去，
#     而 taxonomy.md 与 pass2-verify.md 都写明它"只进报告"。
# 与 ADR-029 是同一类错误：凡是"必须通过 X 才能进交付物"的规则，
# 都该写成一个准入函数，而不是从集合里悄悄少掉几类。
# P 类恒在保留范围内——它由 pattern_review.enabled 控制，与 strictness 无关。
KEEP_SCOPE = {
    "conservative": AB_CLASSES | P_CLASSES,
    "balanced": AB_CLASSES | SINGLE_CLASSES | P_CLASSES,
    "thorough": AB_CLASSES | SINGLE_CLASSES | C_CLASSES | P_CLASSES,
}


def _scope(cfg: dict, table: dict) -> set:
    return table.get(str(cfg.get("strictness") or "balanced"), table["balanced"])


def item_id(rec: dict) -> str:
    """稳定标识：同一条问题在两次排列、两次运行里必须得到同一个 id。

    **段内序号也是标识的一部分。** 同一段里同一个错字的第一处与第二处是两条
    独立的判定（各自问过模型、各自落笔），键里不带序号，`collect` 的
    `seen` 就会把后面几处当成"重叠区产生的同一条"丢掉——用户看到的是
    「第一处改了，后面几处原样留着」。没有序号的记录（主审查通道）标识不变。
    """
    occ = rec.get("occurrence")
    key = f"{rec.get('chunk_id')}|{rec.get('pid')}|{rec.get('category')}|" \
          f"{normalize_ws(rec.get('original_text') or '')}" \
          + (f"|#{occ}" if isinstance(occ, int) else "")
    return sha256_text(key)[:16]


def collect(run_dir: Path, cfg: dict) -> list[dict]:
    """主通道 + 两条支线的过闸产物。顺序按文件名，保证可复现。

    这里用的是**保留范围**（KEEP_SCOPE），不是复核范围：不进闸门④的类别
    （C、P）照样要留下，只是在 merge 里标成 not_reviewed。
    """
    idir = resolve_path(run_dir, "issues")
    scope = _scope(cfg, KEEP_SCOPE)
    out, seen = [], set()
    for path in sorted(idir.glob("issues-*.jsonl")):
        if path.name.endswith(".raw.jsonl"):
            continue
        for rec in read_jsonl(path):
            cat = (rec.get("category") or "").strip().upper()
            if cat not in scope:
                continue
            iid = item_id(rec)
            if iid in seen:              # 重叠区可能产生同一条，保留首次
                continue
            seen.add(iid)
            out.append({**rec, "id": iid, "_src": path.name})
    return out


MASK = "【　】"


def context_of(rec: dict, paras: list[dict], idx: dict, masks: list[str]) -> str:
    """前后各一段，仅供理解。上下文本身不参与评价。

    **必须把所有待判跨度从上下文里挡掉，不只是本条的。**
    上下文里那一段正是原文所在的段落，原样给出等于把答案写在题面上——
    模型只要拿两个选项去上下文里比对，命中的那个就是原文。

    只挡本条是不够的：同一片里的段落彼此相邻，一批 10 条的上下文大面积重叠，
    甲的上下文会原样带出乙的原文。实测 15 条里有 14 条这样漏答案。
    盲测会因此彻底失效，而且失效得很隐蔽——一致率会漂亮得反常，
    看起来像模型判得准。
    """
    i = idx.get(rec.get("pid"))
    if i is None:
        return ""
    lo, hi = max(0, i - 1), min(len(paras), i + 2)
    ctx = " ".join(p["text"] for p in paras[lo:hi] if p["text"].strip())
    for span in masks:
        if span:
            ctx = ctx.replace(span, MASK)
    return ctx


def build(run_dir: Path, cfg: dict, arrangement: str) -> dict:
    man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
    seed = man.get("ab_seed")
    if seed is None:
        die(EX.PARSE, "manifest 缺少 ab_seed",
            "该字段由 workspace.py init 写入；旧 run 需重新 init 或手工补上。")

    rows = collect(run_dir, cfg)
    paras = list(read_jsonl(resolve_path(run_dir, "paragraphs")))
    idx = {p["pid"]: i for i, p in enumerate(paras)}
    batch = int((cfg.get("verification") or {}).get("verify_batch_size") or 10)

    # 全部 A/B 待判跨度（原文与建议）——它们在任何一条上下文里出现都会泄露答案。
    # 长串先挡，避免短串先替换后把长串截断成挡不掉的碎片。
    masks = sorted(
        {r["original_text"] for r in rows
         if r["category"] in AB_CLASSES and (r.get("suggested_text") or "").strip()}
        | {(r.get("suggested_text") or "").strip() for r in rows
           if r["category"] in AB_CLASSES and (r.get("suggested_text") or "").strip()},
        key=len, reverse=True)

    review = _scope(cfg, REVIEW_SCOPE)
    items, key = [], {}
    for rec in rows:
        cat, sugg = rec["category"], (rec.get("suggested_text") or "").strip()
        if cat not in review:
            continue          # C 类与 P 类不进复核集；它们仍在 collect 的保留范围内
        ctx = context_of(rec, paras, idx, masks)
        if cat in AB_CLASSES and sugg:
            # 位置逐条派生：同一 run 重跑得到同一排列；mirror 恒为其镜像
            rng = random.Random(f"{seed}:{rec['id']}")
            orig_side = "A" if rng.random() < 0.5 else "B"
            if arrangement == "mirror":
                orig_side = "B" if orig_side == "A" else "A"
            first = rec["original_text"] if orig_side == "A" else sugg
            second = sugg if orig_side == "A" else rec["original_text"]
            items.append({"id": rec["id"], "form": "ab", "A": first, "B": second,
                          "context": ctx})
            key[rec["id"]] = {"form": "ab", "orig_side": orig_side, "category": cat}
        elif cat in SINGLE_CLASSES:
            items.append({"id": rec["id"], "form": "single",
                          "span": rec["original_text"], "context": ctx,
                          "aspect": SINGLE_ASPECT.get(cat, "表述")})
            key[rec["id"]] = {"form": "single", "orig_side": None, "category": cat}
        # A 类但建议已被闸门②清空 → 已经是批注，没有可对照的两项，跳过复核

    batches = [{"batch_id": f"v{i // batch + 1:02d}", "items": items[i:i + batch]}
               for i in range(0, len(items), batch)]
    vdir = resolve_path(run_dir, "verify")
    guard_write_path(vdir, run_dir)
    vdir.mkdir(parents=True, exist_ok=True)

    payload = {**version_header(), "arrangement": arrangement, "batches": batches,
               "count": len(items)}
    ppath = vdir / f"pass2-{arrangement}.json"
    kpath = vdir / f"pass2-{arrangement}.key.json"
    for p in (ppath, kpath):
        guard_write_path(p, run_dir)
    atomic_write_json(ppath, payload)
    atomic_write_json(kpath, {**version_header(), "arrangement": arrangement,
                              "ab_seed": seed, "key": key})

    # 每批单独落一个文件。合并文件（上面那个）留着备查，但**发起调用时不要用它**：
    # 一份 800 页文档能出几百条待复核项，整份读进上下文之后，后面每一轮工具往返
    # 都要把它重算一遍。分批落盘之后每个子 Agent 只读自己那一批，
    # 而且各批之间没有任何依赖——闸门④天然可以并行。
    for old_batch in vdir.glob(f"pass2-{arrangement}.v*.json"):
        old_batch.unlink()                       # 批数可能变少，先清再写
    for b in batches:
        bp = vdir / f"pass2-{arrangement}.{b['batch_id']}.json"
        guard_write_path(bp, run_dir)
        atomic_write_json(bp, {**version_header(), "arrangement": arrangement,
                               "batch_id": b["batch_id"], "items": b["items"]})
    return {"arrangement": arrangement, "candidates": len(rows), "items": len(items),
            "batches": len(batches), "payload": str(ppath),
            "batch_dir": str(vdir),
            "batch_pattern": f"pass2-{arrangement}.v<NN>.json",
            "verdict_pattern": f"pass2-{arrangement}.v<NN>.verdicts.jsonl",
            "note": "按批发起调用：每批一个 pass2-<排列>.vNN.json，裁定写同名 .verdicts.jsonl；"
                    "payload 不含原文在哪一侧的信息，对照表在 .key.json，调用时不要带上它"}


# --------------------------------------------------------------------------
# 判定表（prompts/pass2-verify.md）
# --------------------------------------------------------------------------
def _ab_verdict(answer: str, orig_side: str) -> tuple[str, str]:
    a = (answer or "").strip().upper()
    if a in ("A", "B"):
        # 选中原文所在项 = 模型独立认出原文有错，且认为建议更好 → 通过
        return ("pass", "选中原文所在项") if a == orig_side else ("drop", "选中建议所在项")
    if a in ("NONE", "两者都没有", "都没有", "N"):
        return "drop", "模型认为原文没问题"
    if a in ("BOTH", "两者都有", "都有", "D"):
        return "drop", "模型未能区分，视为不可靠"
    return "drop", f"无法解析的回答（{answer!r}），按淘汰处理"


def _single_verdict(answer: str) -> tuple[str, str]:
    a = (answer or "").strip().upper()
    if a in ("YES", "Y"):
        return "pass", "确认存在多解"
    if a in ("NO", "N"):
        return "drop", "模型认为不构成歧义"
    return "drop", "UNSURE 一律按 NO 处理（不确定即无问题）"


def verdict_files(vdir: Path, arrangement: str) -> list[Path]:
    """并行复核的产物是每批一个裁定文件；单文件写法照旧支持。

    两种都收：`pass2-<排列>.verdicts.jsonl`（一个子 Agent 全揽）与
    `pass2-<排列>.vNN.verdicts.jsonl`（每批一个）。并行时**必须各写各的文件**——
    多个子 Agent 往同一个文件 append，中断处会互相截断，而 JSONL 的坏行是静默丢失。
    """
    out = []
    single = vdir / f"pass2-{arrangement}.verdicts.jsonl"
    if single.exists():
        out.append(single)
    out += sorted(vdir.glob(f"pass2-{arrangement}.v*.verdicts.jsonl"))
    return out


def read_answers(vdir: Path, arrangement: str) -> dict:
    answers = {}
    for path in verdict_files(vdir, arrangement):
        for v in read_jsonl(path):
            if v.get("id"):
                answers[v["id"]] = v.get("answer")
    return answers


def merge(run_dir: Path, cfg: dict, arrangement: str) -> dict:
    vdir = resolve_path(run_dir, "verify")
    kpath = vdir / f"pass2-{arrangement}.key.json"
    if not kpath.exists():
        die(EX.PARSE, f"对照表不存在：{kpath}", "先跑 verify_pass2.py build。")
    files = verdict_files(vdir, arrangement)
    if not files:
        die(EX.PARSE, f"裁定结果不存在：{vdir}/pass2-{arrangement}[.vNN].verdicts.jsonl",
            "按 references/prompts/pass2-verify.md 逐 batch 复核后写入该文件。")
    key = (read_json(kpath, {}) or {}).get("key") or {}
    answers = read_answers(vdir, arrangement)

    rows, counters = [], {"pass": 0, "drop": 0, "not_reviewed": 0, "missing_verdict": 0}
    for rec in collect(run_dir, cfg):
        k = key.get(rec["id"])
        if not k:
            # 未进入复核集：A 类建议已被闸门②清空，或类别本就不复核（C 类、P 类）。
            # **不复核不等于淘汰**——按原动作原样保留，由各自的 action 决定去向。
            rec["verify"] = {"result": "pass", "method": "not_reviewed",
                             "note": "未进入复核集，按原动作保留"}
            counters["not_reviewed"] += 1
        elif rec["id"] not in answers:
            rec["verify"] = {"result": "drop", "method": k["form"],
                             "note": "缺裁定结果，按淘汰处理"}
            counters["missing_verdict"] += 1
            counters["drop"] += 1
        else:
            ans = answers[rec["id"]]
            if k["form"] == "ab":
                result, note = _ab_verdict(ans, k["orig_side"])
            else:
                result, note = _single_verdict(ans)
            rec["verify"] = {"result": result, "method": "blind_ab" if k["form"] == "ab"
                             else "closed_single", "position": k["orig_side"],
                             "answer": ans, "note": note}
            counters[result] += 1
        rec.pop("_src", None)
        rows.append(rec)

    out = resolve_path(run_dir, "issues_verified")
    guard_write_path(out, run_dir)
    atomic_write_jsonl(out, rows)
    return {"arrangement": arrangement, "total": len(rows), "output": str(out),
            "verdict_files": len(files), **counters}


def consistency(run_dir: Path, cfg: dict) -> dict:
    """M2 验收项：两种排列下的判定一致率 ≥90%。

    低于此说明模型有强位置偏好——它答的是"哪一侧"而不是"哪一个有错"，
    此时不能靠调阈值补救，必须换 prompt。
    """
    vdir = resolve_path(run_dir, "verify")
    res: dict[str, dict] = {}
    for arr in ARRANGEMENTS:
        key = (read_json(vdir / f"pass2-{arr}.key.json", {}) or {}).get("key") or {}
        answers = read_answers(vdir, arr)
        if not key or not answers:
            die(EX.PARSE, f"{arr} 排列的产物不全",
                "两种排列都要跑一遍 build + 复核，才能算一致率。")
        got = {}
        for iid, ans in answers.items():
            k = key.get(iid)
            if not k:
                continue
            got[iid] = (_ab_verdict(ans, k["orig_side"])[0]
                        if k["form"] == "ab" else _single_verdict(ans)[0])
        res[arr] = got

    shared = sorted(set(res["primary"]) & set(res["mirror"]))
    ab_only = [i for i in shared
               if ((read_json(vdir / "pass2-primary.key.json", {}) or {})
                   .get("key", {}).get(i, {}).get("form") == "ab")]
    agree = [i for i in ab_only if res["primary"][i] == res["mirror"][i]]
    rate = (len(agree) / len(ab_only)) if ab_only else None
    return {"ab_items": len(ab_only), "agreed": len(agree),
            "agreement_rate": round(rate, 4) if rate is not None else None,
            "threshold": 0.9,
            "meets_threshold": (rate >= 0.9) if rate is not None else None,
            "disagreed": [i for i in ab_only if res["primary"][i] != res["mirror"][i]],
            "note": "低于 0.9 说明模型有强位置偏好，应换 prompt 而不是调阈值"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="verify_pass2.py", description="闸门④盲测 A/B 脚手架")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--arrangement", choices=ARRANGEMENTS, default="primary")
        p.add_argument("--config")
    p = sub.add_parser("consistency")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--config")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    if args.cmd == "build":
        emit({"ok": True, **build(run_dir, cfg, args.arrangement)})
    elif args.cmd == "merge":
        emit({"ok": True, **merge(run_dir, cfg, args.arrangement)})
    else:
        emit({"ok": True, **consistency(run_dir, cfg)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
