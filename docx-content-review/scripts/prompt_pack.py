#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Pass 1 的每一次调用渲染成**一个自包含的 prompt 文件**。

为什么需要这一步：`references/prompts/pass1-*.md` 是模板，占位符要填
`taxonomy.md` 全文（7.1k 字符）、`never-flag.md` 全文（7.3k 字符）、
`schemas.md` 的 facts 小节、术语表摘要、以及分片正文。子 Agent 自己去填，
意味着它要先把这些文件**全部读进上下文**——一次审查调用的固定开销就有 1.7 万字符，
再加正文与候选，实测在 64k 级别的子 Agent 上直接溢出，而且每个子 Agent 都要重读一遍。

渲染成一个文件之后，子 Agent 的动作退化成：**读这一个文件 → 作答 → 写一个文件**。
它不需要知道技能目录在哪，也没有机会顺手读进别的大文件。

这不违反「LLM 调用不在脚本里」：这里只做确定性的字符串替换，
判断与调用仍然由 Agent 发起。

子命令
  build   渲染 → work/prompts/<stage>-<unit>.md
  list    只报数：每个阶段有多少单元、缺哪些 prompt
退出码：0 成功 / 2 参数错 / 10 输入不可解析。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tokenizer as tk  # noqa: E402

from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_text, die, emit, read_json, run_cli,
)
from workspace import (  # noqa: E402
    SKILL_ROOT, STAGES, guard_write_path, load_run_config, resolve_path, stage_units,
)

PROMPT_DIR = SKILL_ROOT / "references" / "prompts"
REF_DIR = SKILL_ROOT / "references"

HEADER = """<!-- 本文件由 prompt_pack.py 渲染，**自包含**：读完这一个文件即可作答。
     不要再去读 references/ 下的任何文件，也不要读分片原文或候选文件——
     需要的内容都已经在下面了。 -->

# 本次任务：{title}

- 输出写到：`{output}`
- 写法：{how}
- **一次性整块写入**（一个 heredoc 写完全部行），不要逐行追加。
- 写完就结束，返回 `{{"unit":"{unit}","output":"{output}","lines":<行数>}}`，
  **不要把正文内容带回给主 Agent**。

---

"""

# 分片再小也得装得下一段完整正文，否则切出来的片没有审查价值
MIN_TEXT_CHARS = 2000

JSONL_HOW = "每行一条 JSON（JSONL），无外层数组、无代码围栏、无前后说明文字"
JSON_HOW = "一个 JSON 对象，无代码围栏、无前后说明文字"


RUNTIME_MARK = "<!-- RUNTIME -->"


def _body(name: str) -> str:
    """取模板里真正要送给模型的那一段。

    两种标记：显式的 `<!-- RUNTIME -->`（typo/pattern 用，它们的开发者说明夹在中间），
    或第一个 `---`（review/extract 用，`---` 之后整篇都是 prompt）。
    **不能整份塞进去**——模板里写给开发者的部分含 `{{占位符}}` 与"输入在哪个文件"
    这类说明，模型照着读会去找文件、会把占位符当成要填的内容。
    """
    text = (PROMPT_DIR / name).read_text(encoding="utf-8")
    if RUNTIME_MARK in text:
        return text.split(RUNTIME_MARK, 1)[1].strip()
    parts = text.split("\n---\n", 1)
    return (parts[1] if len(parts) == 2 else text).strip()


def _section(path: Path, heading_prefix: str) -> str:
    """取某个 `## 标题` 小节的全文（到下一个 `## ` 为止）。"""
    out, keep = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            keep = line.startswith(heading_prefix)
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def glossary_summary(run_dir: Path, budget_chars: int) -> str:
    """术语表摘要。没有术语表时明确说「没有」——留空会让模型以为是漏给了。"""
    data = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}
    entries = data.get("entries") or []
    if not entries:
        return "（本次运行没有术语表。遇到拿不准的专有写法一律不报，不要「纠正」专名。）"
    lines = []
    for e in entries:
        pref = e.get("preferred") or e.get("key") or ""
        variants = [v for v in (e.get("variants") or []) if v and v != pref]
        line = f"- {pref}" + (f"（也可能写作：{'、'.join(variants[:6])}）" if variants else "")
        if sum(len(x) for x in lines) + len(line) > budget_chars:
            lines.append(f"- …（其余 {len(entries) - len(lines)} 条略）")
            break
        lines.append(line)
    return "\n".join(lines)


def render_review(run_dir: Path, cfg: dict, unit: dict) -> str:
    ch = cfg.get("chunking") or {}
    chunk_text = Path(unit["source"]).read_text(encoding="utf-8")
    body = _body("pass1-review.md")
    body = (body
            .replace("{{TAXONOMY}}", (REF_DIR / "taxonomy.md").read_text(encoding="utf-8").strip())
            .replace("{{NEVER_FLAG}}", (REF_DIR / "never-flag.md").read_text(encoding="utf-8").strip())
            .replace("{{GLOSSARY}}", glossary_summary(run_dir, int(ch.get("glossary_tokens") or 4000)))
            .replace("{{MAX_ISSUES}}", str(int(ch.get("max_issues_per_chunk") or 40)))
            .replace("{{CHUNK}}", chunk_text))
    return body


def render_extract(run_dir: Path, cfg: dict, unit: dict) -> str:
    chunk_text = Path(unit["source"]).read_text(encoding="utf-8")
    schema = _section(REF_DIR / "schemas.md", "## facts-")
    return (_body("pass1-extract.md")
            .replace("{{SCHEMA}}", schema)
            .replace("{{CHUNK}}", chunk_text))


def render_typo(run_dir: Path, cfg: dict, unit: dict) -> str:
    payload = read_json(unit["source"], {}) or {}
    items = payload.get("items") or []
    lines = [f"{n}. （tid={x['tid']}）上下文「{x['context']}」 —— "
             f"原写法：{x['wrong']} / 候选写法：{x['right']}"
             for n, x in enumerate(items, 1)]
    return _body("pass1-typo.md") + f"""

## 本批候选（共 {len(items)} 条）

{chr(10).join(lines)}

## 本批的输出格式

每行一条：`{{"tid":"<上面那个 tid>","verdict":"A|B|C"}}`。
**tid 原样抄回**——它是这条候选的唯一定位方式，抄错等于这一条静默消失。
"""


def render_pattern(run_dir: Path, cfg: dict, unit: dict) -> str:
    payload = read_json(unit["source"], {}) or {}
    rules = {r["id"]: r for r in payload.get("rules") or []}
    blocks = []
    for x in payload.get("items") or []:
        rule = rules.get(x.get("pattern_id")) or {}
        checks = "\n".join(f"   - {c['key']}：{c['label']}（{c['hint']}）"
                           for c in x.get("checks") or [])
        blocks.append(f"（cid={x['cid']}）范式：{x.get('pattern_name') or rule.get('name', '')}\n"
                      f"   原文：{x.get('text', '')}\n{checks}")
    ex = []
    for r in rules.values():
        pos = "；".join(e.get("text", "") for e in (r.get("examples") or {}).get("positive", [])[:2])
        neg = "；".join(e.get("text", "") for e in (r.get("examples") or {}).get("negative", [])[:2])
        ex.append(f"- {r.get('name')}：要件齐备的例子「{pos}」；缺要件的例子「{neg}」")
    return _body("pass1-pattern.md") + f"""

## 本批用到的规则与正反例

{chr(10).join(ex) or "（无）"}

## 本批待判条目（共 {len(blocks)} 条）

{(chr(10) + chr(10)).join(blocks)}

## 本批的输出格式

每个条目的**每一个要件**answer 一行：`{{"cid":"<上面那个 cid>","key":"<要件 key>","answer":"Y|N|U"}}`。
"""


RENDERERS = {"review": (render_review, JSONL_HOW),
             "extract": (render_extract, JSON_HOW),
             "typo": (render_typo, JSONL_HOW),
             "pattern": (render_pattern, JSONL_HOW)}

TITLES = {"review": "语病与语义审查 · 分片 {cid}",
          "extract": "事实抽取 · 分片 {cid}",
          # 错别字批次跨分片（候选彼此无关），所以标题里只报批号
          "typo": "错别字裁定 · 第 {bid} 批",
          "pattern": "范式要件裁定 · 分片 {cid} 第 {bid} 批"}


def build(run_dir: Path, cfg: dict, stages: list[str], chunk_id: str | None) -> dict:
    pdir = resolve_path(run_dir, "prompts")
    guard_write_path(pdir, run_dir)
    pdir.mkdir(parents=True, exist_ok=True)

    ch = cfg.get("chunking") or {}
    limit = int(ch.get("max_prompt_chars") or 0)
    written, sizes, biggest = {}, [], ("", 0)
    # 每个 prompt 的**字符数**（不是字节数——中文一个字三字节，按字节量会差三倍）。
    # claim 打包时要按它算预算，不该为此把每个 prompt 都读一遍。
    index: dict[str, int] = {}
    # 固定开销 = prompt 里正文之外的部分（类型体系、不改清单、schema、格式说明）。
    # 建议值要按它来算：能留给正文的是 limit - fixed，而不是按总量等比例缩。
    fixed_max = 0
    for stage in stages:
        render, how = RENDERERS[stage]
        n = 0
        for unit in stage_units(run_dir, stage):
            if chunk_id and unit["chunk_id"] != chunk_id:
                continue
            title = TITLES[stage].format(cid=unit["chunk_id"], bid=unit.get("batch_id", ""))
            text = HEADER.format(title=title, output=unit["output"], how=how,
                                 unit=unit["unit"]) + render(run_dir, cfg, unit)
            guard_write_path(unit["prompt"], run_dir)
            atomic_write_text(unit["prompt"], text)
            sizes.append(len(text))
            index[f"{stage}/{unit['unit']}"] = len(text)
            if stage in ("review", "extract"):
                src = Path(unit["source"])
                body = len(src.read_text(encoding="utf-8")) if src.exists() else 0
                fixed_max = max(fixed_max, len(text) - body)
            if len(text) > biggest[1]:
                biggest = (f"{stage}/{unit['unit']}", len(text))
            n += 1
        written[stage] = n

    idx_path = pdir / "index.json"
    guard_write_path(idx_path, run_dir)
    prev = read_json(idx_path, {}) or {}
    prev.update(index)                        # 只渲染了一部分时保留其余单元的记录
    atomic_write_json(idx_path, prev)

    mx = max(sizes) if sizes else 0
    # ── 输出侧的账 ──
    # 派活前的体检一直只量**输入**（prompt 有多少字符）。真实运行里撞上的却是
    # 输出：子 Agent 单轮的生成预算被「思考 + 最多 N 条问题明细」耗尽，
    # 回复被截断在半路——产物没写成，harness 报的是「ran out of room」。
    # 输入侧一个字都没超，所以旧体检看不见它。
    #
    # 这两个数从来没有对过账：`max_issues_per_chunk` 说一片最多报几条，
    # `output_reserve_tokens` 说切分时为输出留了多少额度。默认 40 条 × 最坏一条
    # （跨度上限 120 字 ×2 + evidence 25 + 键名）≈ 12,760 token，而预留是 8,000。
    # **预留额度是记在切分账上的，从来没有人拿它去校验模型真要吐多少。**
    est = estimate_output(cfg)
    res_out = {"est_output_tokens_worst": est["worst"], "est_output_tokens_typical": est["typical"],
               "output_reserve_tokens": est["reserve"], "max_issues_per_chunk": est["cap"]}

    over = [x for x in sizes if limit and x > limit]
    res = {"dir": str(pdir), "written": written, "max_chars": mx, **res_out,
           "avg_chars": round(sum(sizes) / len(sizes)) if sizes else 0,
           "max_unit": biggest[0], "limit": limit, "over_limit": len(over),
           "note": "子 Agent 只读 prompt 与写 output 两个文件，不要再读 references/"}
    if over:
        # **在派活之前就拦下来。** 分片对子 Agent 来说过大时，表现是「跑了一半，
        # 3/5 的子 Agent 失败」——要跑完一整波才看得出来，而且看到的是失败计数，
        # 不是原因。这里把它变成第 3 步的一条明确失败，并直接给出该设成多少。
        # 正文可用额度 = 上限 - 固定开销（类型体系、不改清单、schema、格式说明）。
        # CJK 下 1 个 est-token ≈ 1 个字符，所以这个额度直接就是 max_text_tokens 该设的值。
        cur = int(ch.get("max_text_tokens") or 10000)
        room = (limit - fixed_max) if fixed_max else int(limit * 0.65)
        res["fixed_overhead_chars"] = fixed_max
        if room < MIN_TEXT_CHARS:
            # **上限比固定开销还小时，调 max_text_tokens 是没用的**——正文缩到 0 也超。
            # 这时候给个"更小的建议值"等于把 Agent 送进死循环：改了、重跑、还是 8。
            floor = fixed_max + MIN_TEXT_CHARS
            res["min_viable_prompt_chars"] = floor
            die(EX.VALIDATE,
                f"max_prompt_chars={limit} 比固定开销（{fixed_max} 字符）还小，"
                f"任何分片大小都过不了",
                f"固定开销是类型体系 + 不改清单 + schema，压不下去。\n"
                f"把上限调到至少 {floor}：\n"
                f"  workspace.py reconfigure --run-dir <run> "
                f"--set chunking.max_prompt_chars={floor}",
                payload=res)
        suggest = max(MIN_TEXT_CHARS, min(cur, (room // 1000) * 1000))
        res["suggest_max_text_tokens"] = suggest
        die(EX.VALIDATE,
            f"{len(over)} 个单元的 prompt 超过 max_prompt_chars={limit}"
            f"（最大 {mx} 字符，在 {biggest[0]}）",
            f"分片对子 Agent 来说太大了。改配置并重跑第 3 步：\n"
            f"  workspace.py reconfigure --run-dir <run> "
            f"--set chunking.max_text_tokens={suggest}\n"
            f"  chunk.py --run-dir <run> && typo_scan.py scan --run-dir <run> "
            f"&& prompt_pack.py build --run-dir <run>\n"
            f"（子 Agent 窗口确实够大时，改 chunking.max_prompt_chars 放宽本项）",
            payload=res)

    if est["worst"] > est["reserve"]:
        # 输入过得去、输出过不去。**这类失败重试是无效的**——同一个 prompt
        # 会再撞一次，表现是"某一片挂了六次才偶然成功"（思考长度是随机的）。
        # 唯一有效的动作是把这一片最坏能吐多少压下来。
        res["suggest_max_issues_per_chunk"] = est["fits"]
        die(EX.VALIDATE,
            f"最坏输出约 {est['worst']} token，超过为输出预留的 {est['reserve']} token"
            f"（{est['cap']} 条 × 每条最坏 {est['per_issue']} token）",
            f"输入没超，超的是输出：子 Agent 一轮吐不完就会被截断，产物写不成，"
            f"而重试无效（同一个 prompt 会再撞一次）。\n"
            f"先压条数上限——它是最直接的杠杆，且不增加派活次数：\n"
            f"  workspace.py reconfigure --run-dir <run> "
            f"--set chunking.max_issues_per_chunk={est['fits']}\n"
            f"  prompt_pack.py build --run-dir <run>\n"
            f"（压完还失败，再考虑调小 chunking.max_text_tokens；"
            f"子 Agent 单轮输出额度确实够大时，改 chunking.output_reserve_tokens 放宽本项）",
            payload=res)
    return res


def estimate_output(cfg: dict) -> dict:
    """一片审查最坏要吐多少 token。

    只数**结构化产出**：条数上限 × 每条的字段上限。思考 token 不在这里估——
    它随模型与题目变化，估不准；能确定的是"结构化部分至少要占掉这么多"，
    留给思考的就是预留额度减去它。所以这道账要留够余量，而不是刚好卡上。
    """
    ch = cfg.get("chunking") or {}
    ver = cfg.get("verification") or {}
    cap = int(ch.get("max_issues_per_chunk") or 40)
    reserve = int(ch.get("output_reserve_tokens") or 8000)
    span = int(ver.get("max_span_chars") or 120)
    # 一条记录：original_text + suggested_text 各到跨度上限，evidence 25 字，
    # 加上 pid/category/rule_id/severity 与 JSON 的键名标点（实测约 90 字符）
    worst_chars = span * 2 + 25 + 90
    typ_chars = min(span, 25) * 2 + 20 + 90
    per_issue = tk.count("必" * worst_chars, 1.0)
    return {"cap": cap, "reserve": reserve, "per_issue": per_issue,
            "worst": per_issue * cap,
            "typical": tk.count("必" * typ_chars, 1.0) * cap,
            # 能装下的条数上限（留 20% 余量给思考与格式波动）
            "fits": max(5, int(reserve * 0.8 // max(1, per_issue)))}


def listing(run_dir: Path, stages: list[str]) -> dict:
    out = {}
    for stage in stages:
        units = stage_units(run_dir, stage)
        out[stage] = {"units": len(units),
                      "prompts": sum(1 for u in units if u["prompt"].exists()),
                      "done": sum(1 for u in units if u["done_marker"].exists())}
    return {"stages": out}


def _stages(arg: str | None, cfg: dict, run_dir: Path) -> list[str]:
    if arg:
        picked = [x.strip() for x in arg.split(",") if x.strip()]
        bad = [x for x in picked if x not in STAGES]
        if bad:
            die(EX.USAGE, f"未知阶段：{','.join(bad)}", f"可选：{'/'.join(STAGES)}")
        return picked
    out = ["review", "extract"]
    if (cfg.get("typo_check") or {}).get("enabled"):
        out.append("typo")
    if (cfg.get("pattern_review") or {}).get("enabled"):
        out.append("pattern")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="prompt_pack.py", description="Pass 1 自包含 prompt 渲染")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "list"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--stage", help="逗号分隔；默认按配置决定（review,extract[,typo][,pattern]）")
        p.add_argument("--chunk", help="只渲染这一片（补跑用）")
        p.add_argument("--config")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    stages = _stages(args.stage, cfg, run_dir)
    if args.cmd == "build":
        emit({"ok": True, **build(run_dir, cfg, stages, args.chunk)})
    else:
        emit({"ok": True, **listing(run_dir, stages)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
