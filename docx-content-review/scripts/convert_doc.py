#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc → docx 归一（spec §5.1 第 7 步、§5.2）。

D6 铁律：只转换 work/source-copy.doc，输出目录**显式指定**为 run 的 work/。
源文档路径不得作为本脚本的任何参数出现——传入源文档路径会被 guard 拒绝。

用法
  convert_doc.py --run-dir <run> [--timeout 600]
退出码：0 成功（或输入已是 docx，无需转换）；3 无转换器；1 转换失败/产物不可解包。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import EX, die, emit, run_cli  # noqa: E402
from env_probe import INSTALL_HINT, probe  # noqa: E402
from workspace import guard_write_path, resolve_path, source_copy_path  # noqa: E402


def _find_copy(run_dir: Path) -> Path:
    for ext in (".doc", ".docx", ".docm", ".dotx"):
        p = source_copy_path(run_dir, ext)
        if p.exists():
            return p
    die(EX.ERROR, f"未找到源文档副本：{resolve_path(run_dir, 'work')}/source-copy.*",
        "请先执行 workspace.py init。")


def _verify_docx(path: Path) -> None:
    """转换后必须校验产物可解包且 word/document.xml 存在。"""
    if not path.exists() or path.stat().st_size == 0:
        die(EX.ERROR, f"转换产物不存在或为空：{path}")
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        die(EX.ERROR, f"转换产物不是合法的 docx（ZIP 损坏）：{exc}")
    if "word/document.xml" not in names:
        die(EX.ERROR, f"转换产物缺少 word/document.xml：{path}")


def _soffice_convert(binary: str, src: Path, outdir: Path, timeout: int) -> tuple[bool, str]:
    # --outdir 必须显式传入，否则 LibreOffice 会在源文件同级目录产出（违反 D6）
    # 每次调用使用独立的 UserInstallation，避免并发时用户配置目录冲突
    uno = outdir / ".uno"
    cmd = [
        binary, "--headless", "--norestore",
        f"-env:UserInstallation=file://{uno}",
        "--convert-to", "docx", "--outdir", str(outdir), str(src),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{binary} 转换超时（{timeout}s）"
    except OSError as exc:
        return False, f"{binary} 启动失败：{exc}"
    if proc.returncode != 0:
        return False, f"{binary} 退出码 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()[:400]}"
    return True, (proc.stdout or "").strip()[:400]


def _word_com_convert(src: Path, dst: Path) -> tuple[bool, str]:
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return False, "pywin32 not installed"
    app = None
    doc = None
    try:
        app = win32com.client.Dispatch("Word.Application")
        app.Visible = False
        app.DisplayAlerts = False
        # 必须打开副本且 ReadOnly=True；SaveAs2 必须给完整目标路径
        doc = app.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False)
        doc.SaveAs2(str(dst), FileFormat=16)  # 16 = wdFormatDocumentDefault (.docx)
        return True, "word_com"
    except Exception as exc:  # noqa: BLE001 - COM 异常类型不稳定
        return False, f"Word COM 转换失败：{exc}"
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:  # noqa: BLE001
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="convert_doc.py", description="doc → docx 归一")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--force", action="store_true", help="已有 normalized.docx 时也重新转换")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    src = _find_copy(run_dir)
    workdir = resolve_path(run_dir, "work")
    target = resolve_path(run_dir, "normalized")
    guard_write_path(target, run_dir)

    if src.suffix.lower() in (".docx", ".docm", ".dotx"):
        _verify_docx(src)
        emit({"ok": True, "converted": False, "docx": str(src),
              "note": "输入已是 docx，跳过转换"})
        return EX.OK

    if target.exists() and not args.force:
        try:
            _verify_docx(target)
            emit({"ok": True, "converted": False, "docx": str(target), "note": "已存在有效的归一产物"})
            return EX.OK
        except Exception:  # noqa: BLE001 - 损坏则删除重转（§11.6）
            target.unlink(missing_ok=True)

    info = probe()
    avail = [c["name"] for c in info["converters"] if c["available"]]
    if not avail:
        probed = " ".join(f"{c['name']}(未找到)" for c in info["converters"])
        die(EX.ENV,
            "检测到输入为 .doc 格式，但当前环境无可用的格式转换工具。\n"
            f"当前系统：{info['os_detail']}\n已探测：{probed}",
            INSTALL_HINT)

    errors = []
    for name in avail:
        if name == "word_com":
            ok, detail = _word_com_convert(src, target)
            produced = target
        elif name in ("soffice", "libreoffice"):
            ok, detail = _soffice_convert(name, src, workdir, args.timeout)
            produced = workdir / (src.stem + ".docx")
        elif name == "unoconv":
            try:
                proc = subprocess.run(["unoconv", "-f", "docx", "-o", str(target), str(src)],
                                      capture_output=True, text=True, timeout=args.timeout)
                ok = proc.returncode == 0
                detail = (proc.stderr or "").strip()[:400]
            except (OSError, subprocess.SubprocessError) as exc:
                ok, detail = False, str(exc)
            produced = target
        else:
            continue

        if not ok:
            errors.append(f"{name}: {detail}")
            continue
        if produced != target:
            if not produced.exists():
                errors.append(f"{name}: 未在 {workdir} 产出 {produced.name}")
                continue
            guard_write_path(target, run_dir)
            shutil.move(str(produced), str(target))
        try:
            _verify_docx(target)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: 产物校验失败（{exc}）")
            target.unlink(missing_ok=True)
            continue
        emit({"ok": True, "converted": True, "converter": name, "docx": str(target),
              "size": target.stat().st_size})
        return EX.OK

    die(EX.ERROR, "doc → docx 转换失败：\n  " + "\n  ".join(errors),
        "重复失败请检查文档是否损坏或受密码保护；本技能不会对源文档做任何原地操作。")


if __name__ == "__main__":
    run_cli(main)
