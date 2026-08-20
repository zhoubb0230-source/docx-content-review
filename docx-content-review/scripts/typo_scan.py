#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错别字候选扫描（spec §9.7，M8）。

**为什么必须是独立通道**：错别字是唯一一个存在确定性候选生成手段的类别，
让 LLM 扫描它先天不利——
  1. LLM 对错别字有鲁棒性，这恰是缺陷：读到「帐号」「布署」会自动理解为正确词，
     注意力不会停留，它在做语义理解而非逐字比对；
  2. 配额挤占：一片 20 页可能有 20–40 个错别字，max_issues_per_chunk 瞬间打满；
  3. 重要性排挤：模型倾向报告「看起来更重要」的问题，错别字最不起眼。

与 D1 同构的三段式：脚本扫描出候选 → LLM 批量裁定（封闭选择题）→ 过闸门②③。
**脚本候选不得直接生成修订**（混淆集方法误报率高，LLM 裁定不可省略）。

子命令
  scan       扫描候选 → work/typos/typos-<chunk>.json（含供 LLM 裁定的分批 payload）
  merge      合并裁定结果 → work/issues/issues-<chunk>.typos.jsonl，类别记为 A1
  propagate  已确认的错字扩散到全文其余同形之处 → issues-<chunk>.propagated.jsonl
  lint       只自检词表，不需要 run 目录（扩表后必跑）

**召回率上限 = common-typos.txt 的覆盖范围。** 未登录词检测（切词后查不到的词即可疑）
能突破这个上限，但需 5 万词级词表才有信噪比，内置词表远未达到该规模，故不启用
（config: typo_check.oov_detection）。扩表是提升召回的唯一杠杆，且是可枚举、
可收敛的资产——与 never-flag 同性质。**不要靠放宽闸门②的 A1 阈值来提召回。**

退出码：0 成功。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX, atomic_write_json, atomic_write_jsonl, emit, levenshtein, read_json, read_jsonl,
    run_cli, version_header,
)
from workspace import SKILL_ROOT, guard_write_path, load_run_config, resolve_path  # noqa: E402

DICT_DIR = SKILL_ROOT / "assets" / "dict"


def _load_pairs(name: str, override: str | None = None) -> list[tuple[str, str, str]]:
    p = Path(override) if override else DICT_DIR / name
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0].strip(), parts[1].strip(),
                        parts[2].strip() if len(parts) > 2 else ""))
    return out


def _load_list(name: str, override: str | None = None) -> set[str]:
    p = Path(override) if override else DICT_DIR / name
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def lint(typos_path: str | None = None, traps_path: str | None = None) -> dict:
    """词表自检。扩表是提召回的唯一手段，也是最容易引入误报的地方——
    下面每一项都是实际踩过的坑，扩表后必跑。

    最要紧的是 `false_positive`：拿每条左串去撞 `typo-traps.txt` 里的合法句子。
    **只验"能查出错"不算验，还要验"不会把对的判成错"**——
    「按全」会被「按全流程」拆出来，「以经」会被「以经验」拆出来，
    这类条目让"扩表不抬高误报率"这个前提失效，必须改写或删除。
    """
    rows = [(w, r) for w, r, _ in _load_pairs("common-typos.txt", typos_path)]
    whitelist = _load_list("typo-whitelist.txt")
    traps = [t for t in _load_list("typo-traps.txt", traps_path)]
    seen, dup = set(), []
    for w, _ in rows:
        (dup.append(w) if w in seen else seen.add(w))
    problems = {
        # 建议与原文相同 → 闸门②直接判「建议与原文相同」，白白消耗一次裁定
        "same": [w for w, r in rows if w == r],
        # 单字左串会把「度」「作」「帐」这类高频字全部拉成候选，噪音淹没一切
        "single_char": [w for w, _ in rows if len(w) < 2],
        # 左串同时躺在白名单里 → 自相矛盾，扫描时永远被跳过
        "in_whitelist": [w for w, _ in rows if w in whitelist],
        "duplicated": dup,
        # 差异超出 A1 闸门（长度差 ≤2 且编辑距离 ≤3）→ 永远落不了笔，只会降级成批注
        "over_a1_gate": [w for w, r in rows
                         if abs(len(w) - len(r)) > 2 or levenshtein(w, r) > 3],
        # 负向语料：命中即误报。白名单能拦下的不算。
        "false_positive": [f"{w} ← 「{t}」" for w, _ in rows for t in traps
                           if w in t and not (w in whitelist
                                              or any(x in t and w in x for x in whitelist))],
    }
    return {"entries": len(rows), "whitelist": len(whitelist), "traps": len(traps),
            "ok": not any(problems.values()), "problems": problems}


def _unique_window(text: str, s: int, e: int, pad: int = 6, limit: int = 40) -> tuple[int, int]:
    """取一段包含 [s,e) 且**在本段内唯一**的上下文窗口。

    **这个窗口是给模型看的上下文，不是落笔跨度。** 模型要判断「这两个字在这里
    是不是错字」，只看那两个字判不了，得看它周围。落笔跨度另有出路：候选带
    `occurrence`（段内第几处），回写时按序号定位，跨度就是错字本身。

    早先两者是同一个东西——`original_text` 就是这个窗口，于是一个两字的错字
    会被落成「删掉八十多字、再插入八十多字」的修订。撑宽窗口是为了消歧，
    不该连编辑范围一起撑宽。

    撑到 limit 仍不唯一（整段是重复内容）就返回最宽的那个。
    """
    while pad <= limit:
        lo, hi = max(0, s - pad), min(len(text), e + pad)
        if text.count(text[lo:hi]) == 1:
            return lo, hi
        if lo == 0 and hi == len(text):
            break
        pad += 6
    return max(0, s - limit), min(len(text), e + limit)


def _question(c: dict) -> dict:
    """题面只要这四项：定位靠 tid，判断靠 context + 两种写法。"""
    return {"tid": c["tid"], "context": c["context"],
            "wrong": c["wrong"], "right": c["right"]}


def scan(run_dir: Path, cfg: dict, chunk_id: str | None) -> dict:
    tc = cfg.get("typo_check") or {}
    if not tc.get("enabled"):
        return {"enabled": False, "candidates": 0,
                "note": "typo_check.enabled=false"}

    typos = _load_pairs("common-typos.txt")
    whitelist = _load_list("typo-whitelist.txt")
    glossary = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}

    # 用户术语表里的写法**也是白名单**。行业术语（半导体装备、医疗器械这类）
    # 的用字与通用错词表天然会撞：只要术语里含着某条左串，这个词每出现一次
    # 就产出一个注定要被否掉的候选——既是噪音，也白白消耗一次裁定调用。
    # 以前术语表只在闸门③（N7）起作用，那已经是模型答完之后了。
    # （登记为禁用的写法不受保护——那正是要挑出来的，见 `_protected_forms`。）
    protected, forbidden = _protected_forms(glossary)

    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    chunks = [c for c in idx.get("chunks", []) if not chunk_id or c["chunk_id"] == chunk_id]
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    tdir = resolve_path(run_dir, "typos")
    guard_write_path(tdir, run_dir)
    tdir.mkdir(parents=True, exist_ok=True)

    total = 0
    results: list[dict] = []
    all_cands: list[dict] = []
    for c in chunks:
        cands = []
        for pid in c.get("review_pids") or c.get("pids") or []:
            p = paras.get(pid)
            if not p or p["is_code"]:
                continue
            text = p["text"]
            for wrong, right, why in typos + forbidden:
                if wrong not in text:
                    continue
                # 白名单命中即跳过：「帐篷」不应被改为「账篷」。
                # 术语表同理：左串落在用户登记的术语里就不生成候选。
                if _shielded(text, wrong, whitelist, protected):
                    continue
                for m in re.finditer(re.escape(wrong), text):
                    lo, hi = _unique_window(text, m.start(), m.end())
                    # 段内第几处。有了它，落笔跨度就不必再靠"撑到唯一"来消歧，
                    # 可以缩到错字本身——两个字的错字不该带出八十多字的修订
                    occ = text.count(wrong, 0, m.start())
                    cands.append({"tid": f"{c['chunk_id']}-{len(cands) + 1:03d}",
                                  "pid": pid, "wrong": wrong, "right": right,
                                  "rule": "common-typos" if (wrong, right, why) in typos
                                          else "glossary-forbidden",
                                  "reason": why, "context": text[lo:hi],
                                  # 窗口里可能不止一个 `wrong`（撑宽之后更容易），
                                  # 所以记下本处的偏移，merge 时按位置替换而不是全局 replace
                                  "wrong_at": m.start() - lo,
                                  # 段内第几处。落笔跨度靠它消歧，
                                  # 不再靠把窗口撑到唯一（见 merge）
                                  "occurrence": occ})
            # 单字混淆集（shape / pinyin）**刻意不用于生成候选**：
            # 按单字命中会把「度」「作」「帐」这类高频字全部拉成候选，噪音淹没一切。
            # 它要有信噪比，前提是先做分词 + 未登录词检测——而未登录词检测需要
            # 5 万词级词表，内置词表远未达到该规模（config: typo_check.oov_detection）。
            # 这两份词表目前只作为写 common-typos.txt 时的人工参照，不参与运行期判定。
        cap = int(tc.get("max_typos_per_chunk") or 200)
        truncated = len(cands) > cap
        cands = cands[:cap]                    # 独立配额，不占用 max_issues_per_chunk
        batch = int(tc.get("batch_size") or 50)
        groups = [cands[i:i + batch] for i in range(0, len(cands), batch)]

        # 主文件只给 merge 用；**分批题面单独落盘，子 Agent 只读自己那一批**。
        # 以前主文件里 candidates 与 batches[].items 是同一批数据的两份拷贝，
        # 200 个候选就是 8 万字符——子 Agent 一读整份，上下文当场见底。
        for old_batch in tdir.glob(f"typos-{c['chunk_id']}.t*.json"):
            old_batch.unlink()
        batches = []
        if chunk_id:                    # 只扫一片时保留按片分批（补跑用）
            for n, g in enumerate(groups, 1):
                bid = f"t{n:02d}"
                bpath = tdir / f"typos-{c['chunk_id']}.{bid}.json"
                guard_write_path(bpath, run_dir)
                atomic_write_json(bpath, {
                    **version_header(), "chunk_id": c["chunk_id"], "batch_id": bid,
                    "items": [_question(x) for x in g]})
                batches.append({"batch_id": bid, "count": len(g)})
        else:
            all_cands.extend(cands)     # 整篇扫描：跨片攒够一批再切（见下）

        payload = {**version_header(), "chunk_id": c["chunk_id"], "candidates": cands,
                   "truncated": truncated, "batches": batches,
                   "require_llm_adjudication": bool(tc.get("require_llm_adjudication", True))}
        path = tdir / f"typos-{c['chunk_id']}.json"
        guard_write_path(path, run_dir)
        atomic_write_json(path, payload)
        total += len(cands)
        results.append({"chunk_id": c["chunk_id"], "candidates": len(cands),
                        "batches": len(batches), "truncated": truncated})

    # **整篇扫描时跨片攒批。** 按片分批会让只有 3 个候选的分片也独占一次调用——
    # 26 片就是 26 次，而这些候选彼此无关、也不需要分片上下文（context 已在候选里）。
    # 跨片攒到 batch_size 再切，同样的候选量能少掉大半次调用。
    gbatches = []
    if not chunk_id:
        for old_batch in tdir.glob("typos-g*.json"):
            old_batch.unlink()
        batch = int(tc.get("batch_size") or 50)
        for n in range(0, len(all_cands), batch):
            bid = f"g{n // batch + 1:02d}"
            bpath = tdir / f"typos-{bid}.json"
            guard_write_path(bpath, run_dir)
            atomic_write_json(bpath, {**version_header(), "batch_id": bid,
                                      "items": [_question(x) for x in all_cands[n:n + batch]]})
            gbatches.append({"batch_id": bid, "count": len(all_cands[n:n + batch])})
        ipath = tdir / "typos-index.json"
        guard_write_path(ipath, run_dir)
        atomic_write_json(ipath, {**version_header(), "candidates": total,
                                  "batches": gbatches})
    # 只列有候选的片：候选为 0 的片不产单元，逐片报零对主 Agent 没有信息量，
    # 却按片数线性占用它的上下文。截断过的片必须留下——那是要处理的。
    return {"enabled": True, "candidates": total, "chunks": len(results),
            "batches": len(gbatches) or sum(r["batches"] for r in results),
            "truncated_chunks": [r["chunk_id"] for r in results if r.get("truncated")],
            "results": [r for r in results if r.get("candidates") or r.get("truncated")]}


def _protected_forms(glossary: dict) -> tuple[set[str], list[tuple[str, str, str]]]:
    """术语表里的受保护写法与登记的禁用写法。scan 与 propagate 共用同一份判据——
    两处各写一遍的下场是：候选阶段跳过了的写法，扩散阶段又原样捡回来。"""
    forbidden: list[tuple[str, str, str]] = []
    protected: set[str] = set()
    banned: set[str] = set()
    for e in (glossary or {}).get("entries", []):
        pref = e.get("preferred") or e.get("key")
        for f in (e.get("forbidden") or []):
            form = f.get("form") if isinstance(f, dict) else f
            if form:
                banned.add(form)
            if form and pref and levenshtein(form, pref, cap=2) <= 2:
                forbidden.append((form, pref, "术语表登记的禁用写法"))
        for w in (pref, e.get("key"), *(e.get("variants") or [])):
            w = (w or "").strip() if isinstance(w, str) else ""
            if w:
                protected.add(w)
    return protected - banned, forbidden


def _shielded(text: str, wrong: str, whitelist: set[str], protected: set[str]) -> bool:
    """这一段里的这个写法是不是被白名单/术语表罩住了（与 scan 的判据逐字相同）。"""
    if wrong in whitelist:
        return True
    return any(w in text and wrong in w for w in whitelist | protected)


def propagate(run_dir: Path, cfg: dict) -> dict:
    """**同形扩散**：一处错字被确认之后，全文其余同形之处一并成条目。

    现场反馈的那一幕：`sporsor` 在第一处被查出来并落了修订，后面三处原样留着，
    既没有修订也没有批注——评审人只能自己去全文搜一遍。

    根因不是漏检，是**通道的覆盖面**：错别字通道按词表扫，词表里没有的写法
    （英文拼写、行业内的臆造词）它一个候选都出不了；主审查通道由模型逐片读，
    模型在哪一片注意到就只报哪一片。两条通道都没有"全文同一个写法"的视角，
    而这恰恰是脚本最擅长、模型最不擅长的事。

    扩散出来的条目**不直接落笔**：它们与其它候选一样过闸门②③，然后逐条进闸门④
    的盲测——同一个字串在不同上下文里可能一个是错、一个是对（「帐篷」与「帐号」），
    位置的判断权仍在模型手里，脚本只负责把该问的地方都问到。
    因此本步必须跑在第 4 步收口之后、闸门④ build 之前。

    只扩散 A1：它的判定对象是字串本身。其余类别（语病、歧义、数值单位）
    的判定依赖上下文，同形不等于同错，扩散过去就是成批误报。
    """
    tc = cfg.get("typo_check") or {}
    if not tc.get("propagate_confirmed", True):
        return {"enabled": False, "candidates": 0,
                "note": "typo_check.propagate_confirmed=false"}

    idir = resolve_path(run_dir, "issues")
    paras = {p["pid"]: p for p in read_jsonl(resolve_path(run_dir, "paragraphs"))}
    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    pid_chunk: dict[str, str] = {}
    for c in idx.get("chunks", []):
        for pid in c.get("review_pids") or c.get("pids") or []:
            pid_chunk.setdefault(pid, c["chunk_id"])

    glossary = read_json(resolve_path(run_dir, "glossary_merged"), {}) or {}
    whitelist = _load_list("typo-whitelist.txt")
    protected, _ = _protected_forms(glossary)
    min_len = int(tc.get("min_span_chars") or 2)
    cap = int(tc.get("max_propagated") or 500)

    # 已确认的错字对 + 已被占住的字符区间。**区间要按字符算**：
    # 同一段里既有的条目改的是哪几个字，扩散就不能再碰那几个字，
    # 否则两条补丁落在同一处，回写时后一条找不到原文（前一条已经进了 w:del）。
    pairs: dict[tuple[str, str], str] = {}
    taken: dict[str, list[tuple[int, int]]] = {}
    for path in sorted(idir.glob("issues-*.jsonl")):
        if path.name.endswith(".raw.jsonl") or path.name.endswith(".propagated.jsonl"):
            continue
        for rec in read_jsonl(path):
            orig = (rec.get("original_text") or "").strip()
            pid = rec.get("pid") or ""
            text = (paras.get(pid) or {}).get("text") or ""
            if orig and text:
                occ = rec.get("occurrence")
                start = -1
                for _ in range(max(0, occ if isinstance(occ, int) else 0) + 1):
                    start = text.find(orig, start + 1)
                    if start < 0:
                        break
                if start >= 0:
                    taken.setdefault(pid, []).append((start, start + len(orig)))
            sugg = (rec.get("suggested_text") or "").strip()
            if (rec.get("category") or "") != "A1" or not sugg or sugg == orig:
                continue
            if len(orig) < min_len:
                continue
            pairs.setdefault((orig, sugg), (rec.get("evidence") or "错别字"))

    rows: dict[str, list[dict]] = {}
    total = skipped_shielded = skipped_taken = 0
    truncated = False
    for (wrong, right), why in sorted(pairs.items()):
        for pid, para in paras.items():
            cid = pid_chunk.get(pid)
            if not cid or para.get("is_code") or wrong not in para["text"]:
                continue
            text = para["text"]
            if _shielded(text, wrong, whitelist, protected):
                skipped_shielded += 1
                continue
            for m in re.finditer(re.escape(wrong), text):
                span = (m.start(), m.end())
                if any(a < span[1] and span[0] < b for a, b in taken.get(pid, [])):
                    skipped_taken += 1          # 这几个字已经有条目管了
                    continue
                if total >= cap:
                    truncated = True
                    break
                taken.setdefault(pid, []).append(span)
                rows.setdefault(cid, []).append({
                    "chunk_id": cid, "pid": pid, "category": "A1", "rule_id": "A1",
                    "severity": "High",
                    "original_text": wrong, "suggested_text": right,
                    "occurrence": text.count(wrong, 0, m.start()),
                    "evidence": (why or "错别字")[:25],
                    "source": "typo_propagate", "action": "revision",
                })
                total += 1
            if truncated:
                break
        if truncated:
            break

    # 幂等：每次重跑都从头生成，旧产物先清掉（否则上一轮多出来的条目会赖着不走）
    for old in idir.glob("issues-*.propagated.jsonl"):
        old.unlink()
    for cid, items in rows.items():
        out = idir / f"issues-{cid}.propagated.jsonl"
        guard_write_path(out, run_dir)
        atomic_write_jsonl(out, items)
    return {"enabled": True, "pairs": len(pairs), "candidates": total,
            "chunks": len(rows), "truncated": truncated,
            "skipped_shielded": skipped_shielded, "skipped_covered": skipped_taken,
            "note": "需再过 verify_span.py / filter_neverflag.py 的 --channel propagated；"
                    "扩散出来的条目照常进闸门④，不直接落笔"}


def merge(run_dir: Path, cfg: dict, chunk_id: str) -> dict:
    """把 LLM 裁定结果并入该片的 issues。裁定为 A（原字正确）或「都不对」的一律丢弃。"""
    tc = cfg.get("typo_check") or {}
    tdir = resolve_path(run_dir, "typos")
    # 三种都收：跨片批次（typos-gNN）、按片批次（typos-<片>.tNN）、旧的整片单文件。
    # 跨片批次里混着别的分片的裁定，靠 tid 归属——tid 的前缀就是 chunk_id。
    verdict_files = sorted(tdir.glob("typos-g*.verdicts.jsonl"))
    verdict_files += sorted(tdir.glob(f"typos-{chunk_id}.t*.verdicts.jsonl"))
    single = tdir / f"typos-{chunk_id}.verdicts.jsonl"
    if single.exists():
        verdict_files.insert(0, single)
    if not verdict_files:
        return {"merged": 0, "note": f"无裁定结果：{tdir}/typos-{chunk_id}[.tNN].verdicts.jsonl"}
    payload = read_json(tdir / f"typos-{chunk_id}.json", {}) or {}
    cands = {(c["pid"], c["wrong"], c["context"]): c for c in payload.get("candidates", [])}
    by_tid = {c["tid"]: c for c in payload.get("candidates", []) if c.get("tid")}

    rows, seen = [], set()
    for v in [x for f in verdict_files for x in read_jsonl(f)]:
        if str(v.get("verdict", "")).strip().upper() != "B":
            continue                      # 只有裁定「应为建议写法」才采纳
        # tid 是首选定位方式：回填 pid/wrong/context 三个字段容易抄错，
        # 抄错的结果是这一条静默消失（对不上候选就丢），没有任何痕迹。
        c = by_tid.get(v.get("tid")) or cands.get(
            (v.get("pid"), v.get("wrong"), v.get("context"))) or {}
        if not c or c.get("tid") in seen:
            continue
        if c.get("tid"):
            seen.add(c["tid"])
        # **落笔跨度就是错字本身**，靠 `occurrence` 指明是段内第几处。
        # 以前这里写的是整个唯一性窗口：一个两字的错字会被落成
        # 「删掉八十多字、再插入八十多字」的修订，批注也跟着圈住一大片，
        # 评审人看不出到底改了哪两个字。窗口的职责是消歧，不是编辑范围。
        rows.append({
            "chunk_id": chunk_id, "pid": c["pid"], "category": "A1", "rule_id": "A1",
            "severity": "High",
            "original_text": c["wrong"], "suggested_text": c["right"],
            "occurrence": c.get("occurrence", 0),
            "evidence": (c.get("reason") or "错别字")[:25],
            "source": "typo_channel", "action": "revision",
        })
    out = resolve_path(run_dir, "issues") / f"issues-{chunk_id}.typos.jsonl"
    guard_write_path(out, run_dir)
    atomic_write_jsonl(out, rows)
    return {"merged": len(rows), "path": str(out),
            "note": "需再过 verify_span.py（--in/--out 指向本文件、--cap 用独立配额）"
                    "与 filter_neverflag.py（--file 指向本文件）"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="typo_scan.py", description="错别字候选扫描（M8）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--chunk")
    p.add_argument("--config")
    p = sub.add_parser("merge")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--chunk", help="不给 = 合并全部分片（阶段化流程用这个）")
    p.add_argument("--config")
    p = sub.add_parser("propagate",
                       help="已确认的错字扩散到全文其余同形之处（收口之后、闸门④之前跑）")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--config")
    p = sub.add_parser("lint", help="自检词表，不需要 run 目录")
    # 允许指向别处的词表：技能目录在运行期只读，负向对照不该去改它
    p.add_argument("--typos", help="错词表路径；默认用内置的")
    p.add_argument("--traps", help="负向语料路径；默认用内置的")
    args = ap.parse_args(argv)
    if args.cmd == "lint":
        res = lint(getattr(args, "typos", None), getattr(args, "traps", None))
        emit({"ok": res["ok"], **res})
        return EX.OK if res["ok"] else EX.PARSE
    run_dir = Path(args.run_dir).resolve()
    cfg = load_run_config(run_dir, args.config)
    if args.cmd == "scan":
        emit({"ok": True, **scan(run_dir, cfg, args.chunk)})
    elif args.cmd == "propagate":
        emit({"ok": True, **propagate(run_dir, cfg)})
    elif args.chunk:
        emit({"ok": True, **merge(run_dir, cfg, args.chunk)})
    else:
        res = [merge(run_dir, cfg, c["chunk_id"]) for c in
               (read_json(resolve_path(run_dir, "chunk_index"), {}) or {}).get("chunks", [])]
        emit({"ok": True, "chunks": len(res), "merged": sum(r["merged"] for r in res)})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
