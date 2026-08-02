#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境探测（spec §5.2）。

只报告能力，不做任何转换、不写任何文件。stdout 输出单行 JSON。
退出码：0 探测完成（不代表可转换）；3 输入为 .doc 且无可用转换器（仅在 --require-doc 时）。
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import EX, die, emit, run_cli  # noqa: E402

INSTALL_HINT = """请安装其一后重试：
  Debian/Ubuntu:  sudo apt-get install -y libreoffice-writer
  RHEL/CentOS:    sudo yum install -y libreoffice-writer
  macOS:          brew install --cask libreoffice
或在 Windows 环境下运行本任务（需已安装 Microsoft Word）。"""


def _probe_binary(name: str) -> dict:
    path = shutil.which(name)
    if not path:
        return {"name": name, "available": False, "detail": "not found in PATH"}
    detail = path
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        first = (out.stdout or out.stderr or "").strip().splitlines()
        if first:
            detail = f"{path} ({first[0].strip()})"
    except (OSError, subprocess.SubprocessError):
        pass
    return {"name": name, "available": True, "detail": detail}


def _probe_word_com() -> dict:
    if platform.system().lower() != "windows":
        return {"name": "word_com", "available": False, "detail": "not on windows"}
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return {"name": "word_com", "available": False, "detail": "pywin32 not installed"}
    app = None
    try:
        app = win32com.client.Dispatch("Word.Application")
        ver = getattr(app, "Version", "unknown")
        return {"name": "word_com", "available": True, "detail": f"Word.Application {ver}"}
    except Exception as exc:  # noqa: BLE001 - COM 异常类型不稳定
        return {"name": "word_com", "available": False, "detail": f"dispatch failed: {exc}"}
    finally:
        # 用后必须 Quit() 且吞掉异常，确保进程不残留
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass


def probe() -> dict:
    osname = {"windows": "windows", "linux": "linux", "darwin": "darwin"}.get(
        platform.system().lower(), platform.system().lower())
    # 探测顺序：Windows 先 Word COM，其他平台先 soffice
    if osname == "windows":
        converters = [_probe_word_com(), _probe_binary("soffice"), _probe_binary("unoconv")]
    else:
        converters = [_probe_binary("soffice"), _probe_binary("libreoffice"), _probe_binary("unoconv")]
    libs = {}
    for mod in ("lxml", "openpyxl", "yaml"):
        try:
            __import__(mod)
            libs[mod] = True
        except ImportError:
            libs[mod] = False
    return {
        "os": osname,
        "os_detail": platform.platform(),
        "converters": converters,
        "python": platform.python_version(),
        "libs": libs,
        "can_convert_doc": any(c["available"] for c in converters),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="env_probe.py", description="环境探测")
    ap.add_argument("--require-doc", action="store_true",
                    help="输入为 .doc 时使用：无可用转换器则以退出码 3 终止")
    args = ap.parse_args(argv)

    info = probe()
    if args.require_doc and not info["can_convert_doc"]:
        probed = " ".join(f"{c['name']}(未找到)" for c in info["converters"] if not c["available"])
        die(
            EX.ENV,
            "检测到输入为 .doc 格式，但当前环境无可用的格式转换工具。\n"
            f"当前系统：{info['os_detail']}\n已探测：{probed}",
            INSTALL_HINT,
        )
    missing = [k for k, v in info["libs"].items() if not v]
    if missing:
        info["warning"] = f"缺少 Python 依赖：{', '.join(missing)}（pip install {' '.join(missing)}）"
    emit({"ok": True, **info})
    return EX.OK


if __name__ == "__main__":
    run_cli(main)
