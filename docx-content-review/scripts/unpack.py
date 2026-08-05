#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解包 + 清理符号链接 + 相邻同格式 run 合并（spec §6.1）。

为什么必须合并 run：Word 会因修订 ID、拼写检查标记把一个句子拆成多个 <w:r>，
所以"肉眼可见的短语在 XML 中往往不是连续字符串"。这是定位失败的首要原因。
合并只改结构，不改内容与渲染——只合并 rPr 完全相同且仅含 w:t 的相邻 run。

子命令
  run    解包 → 去符号链接 → 合并 run → 记录原始各部件哈希（D9 基线）
  pack   把 unpacked/ 重新打包为 docx（不美化 XML，保持部件顺序）
退出码：0 成功；1 失败；4 写路径越界。
"""
from __future__ import annotations

import argparse
import stat
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import EX, atomic_write_json, die, emit, run_cli, sha256_file, version_header  # noqa: E402
from workspace import (  # noqa: E402
    deliver_path, guard_write_path, resolve_path, working_docx,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
# D9 第 3 条：这些部件的字节哈希必须全程不变
STYLE_PARTS = [
    "word/styles.xml", "word/theme/theme1.xml", "word/fontTable.xml",
    "word/numbering.xml", "word/settings.xml", "word/webSettings.xml",
]
# run 内出现以下任一元素即不可合并（含域、图形、换行、制表、批注/脚注引用等）
MERGE_SAFE_CHILDREN = {f"{{{W}}}rPr", f"{{{W}}}t"}
# 位于这些父元素之下的 run 不参与合并，避免触碰既有修订标记
NO_MERGE_PARENTS = {
    f"{{{W}}}ins", f"{{{W}}}del", f"{{{W}}}moveFrom", f"{{{W}}}moveTo",
    f"{{{W}}}smartTag", f"{{{W}}}sdtContent",
}


def _is_symlink_entry(zi: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((zi.external_attr >> 16) & 0xFFFF)


def unpack(docx: Path, dest: Path, run_dir: Path) -> dict:
    guard_write_path(dest, run_dir)
    if dest.exists():
        import shutil

        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    parts, skipped = {}, []
    with zipfile.ZipFile(docx) as zf:
        for zi in zf.infolist():
            if _is_symlink_entry(zi):
                skipped.append(zi.filename)          # 外部来源文档不可信
                continue
            name = zi.filename
            if name.endswith("/"):
                continue
            target = (dest / name).resolve()
            try:
                target.relative_to(dest.resolve())    # 防 zip slip
            except ValueError:
                skipped.append(name)
                continue
            guard_write_path(target, run_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zi) as fh, open(target, "wb") as out:
                out.write(fh.read())
            parts[name] = zi
    return {"parts": list(parts), "skipped_symlinks": skipped}


def part_hashes(docx: Path) -> dict:
    """原始 docx 各部件的 sha256（D9 校验基线，不依赖解包目录）。"""
    out = {}
    with zipfile.ZipFile(docx) as zf:
        import hashlib

        for zi in zf.infolist():
            if zi.filename.endswith("/") or _is_symlink_entry(zi):
                continue
            with zf.open(zi) as fh:
                out[zi.filename] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _rpr_key(run) -> str:
    """与 ooxml.canonical 同源：不依赖命名空间作用域，可用于游离子树。"""
    import ooxml as ox

    rpr = run.find(f"{{{W}}}rPr")
    return "" if rpr is None else ox.canonical(rpr)


def _mergeable(run) -> bool:
    for child in run:
        if not isinstance(child.tag, str) or child.tag not in MERGE_SAFE_CHILDREN:
            return False
    return run.find(f"{{{W}}}t") is not None


def merge_runs(doc_xml: Path) -> dict:
    """合并相邻的同格式 run。返回 {before, after, merged}。"""
    from lxml import etree

    tree = etree.parse(str(doc_xml))
    root = tree.getroot()
    before = len(root.findall(f".//{{{W}}}r"))
    merged = 0

    for para in root.iter(f"{{{W}}}p"):
        for parent in [para] + [e for e in para.iter() if isinstance(e.tag, str)]:
            if parent.tag in NO_MERGE_PARENTS:
                continue
            prev = None
            prev_key = None
            for child in list(parent):
                if not isinstance(child.tag, str):
                    continue
                if child.tag != f"{{{W}}}r":
                    prev, prev_key = None, None
                    continue
                if not _mergeable(child):
                    prev, prev_key = None, None
                    continue
                key = _rpr_key(child)
                if prev is not None and key == prev_key:
                    ptexts = prev.findall(f"{{{W}}}t")
                    ctexts = child.findall(f"{{{W}}}t")
                    text = "".join(t.text or "" for t in ptexts) + "".join(t.text or "" for t in ctexts)
                    for extra in ptexts[1:]:
                        prev.remove(extra)
                    tgt = ptexts[0]
                    tgt.text = text
                    if text != text.strip():
                        tgt.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    parent.remove(child)
                    merged += 1
                    continue
                prev, prev_key = child, key

    after = len(root.findall(f".//{{{W}}}r"))
    # 禁止重新格式化或美化 XML
    tree.write(str(doc_xml), xml_declaration=True, encoding="UTF-8", standalone=True)
    return {"runs_before": before, "runs_after": after, "merged": merged}


def pack(unpacked: Path, target: Path, run_dir: Path) -> Path:
    guard_write_path(target, run_dir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    files = [p for p in sorted(unpacked.rglob("*")) if p.is_file()]
    # [Content_Types].xml 必须是包内第一项
    files.sort(key=lambda p: (p.name != "[Content_Types].xml", str(p.relative_to(unpacked))))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, str(p.relative_to(unpacked)).replace("\\", "/"))
    import os

    os.replace(tmp, target)
    return target


def cmd_run(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    docx = working_docx(run_dir)
    dest = resolve_path(run_dir, "unpacked")
    info = unpack(docx, dest, run_dir)

    doc_xml = dest / "word" / "document.xml"
    if not doc_xml.exists():
        die(EX.ERROR, f"缺少 word/document.xml：{docx} 不是合法的 Word 文档")

    hashes = part_hashes(docx)
    baseline = {
        **version_header(),
        "source_docx": str(docx),
        "source_docx_sha256": sha256_file(docx),
        "part_hashes": hashes,
        "style_parts": {k: hashes.get(k) for k in STYLE_PARTS},
        "skipped_symlinks": info["skipped_symlinks"],
    }
    merge = {"merged": 0} if args.no_merge else merge_runs(doc_xml)
    baseline["merge"] = merge

    # 保存合并后、回写前的 document.xml 快照。D9 的逐 run rPr 比对以它为基准：
    # run 合并只改结构不改渲染，因此它与原文档在样式上等价，且与回写产物可逐 run 对齐。
    snap = resolve_path(run_dir, "work") / "document.baseline.xml"
    guard_write_path(snap, run_dir)
    snap.write_bytes(doc_xml.read_bytes())
    baseline["document_baseline"] = str(snap)
    out = resolve_path(run_dir, "work") / "unpack-baseline.json"
    guard_write_path(out, run_dir)
    atomic_write_json(out, baseline)

    emit({"ok": True, "unpacked": str(dest), "parts": len(info["parts"]),
          "skipped_symlinks": len(info["skipped_symlinks"]), "baseline": str(out), **merge})
    return EX.OK


def cmd_pack(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    src = resolve_path(run_dir, "unpacked")
    if not src.exists():
        die(EX.ERROR, f"解包目录不存在：{src}")
    # 默认直接打包到交付目录（<原文件名>审查版_<时间戳>.docx），不留在临时目录
    target = Path(args.output) if args.output else deliver_path(run_dir, "reviewed_docx")
    if target.exists() and not args.overwrite:
        # 同名交付物不覆盖，追加序号
        stem, suffix, n = target.stem, target.suffix, 2
        while target.with_name(f"{stem}-{n}{suffix}").exists() and n < 100:
            n += 1
        target = target.with_name(f"{stem}-{n}{suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    p = pack(src, target, run_dir)
    emit({"ok": True, "docx": str(p), "size": p.stat().st_size})
    return EX.OK


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="unpack.py", description="解包与 run 合并")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="解包 + 去符号链接 + 合并 run")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--no-merge", action="store_true")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("pack", help="重新打包；默认输出到交付目录")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--output", help="留空则用交付路径 <原文件名>审查版_<时间戳>.docx")
    p.add_argument("--overwrite", action="store_true", help="允许覆盖同名交付物")
    p.set_defaults(func=cmd_pack)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    run_cli(main)
