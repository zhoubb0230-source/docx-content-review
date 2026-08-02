#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token 计量（spec §7.2）。

硬约束：必须按实际 token 计量，不得按字数估算——规划类文档中的表格、参数、
英文专名在中文 tokenizer 下 token 密度显著更高，按字数估算会系统性低估。

优先使用本地可用的真实 tokenizer；不可得时（含运行期无网络导致的词表下载失败）
退回保守估算并置 token_estimated=true，由 chunk.py 写入 manifest。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_ENC = None
_TRIED = False
_MODE = "estimate"


def _load():
    """只尝试本地已缓存的词表；任何失败都静默退回估算（运行期不得依赖网络）。"""
    global _ENC, _TRIED, _MODE
    if _TRIED:
        return _ENC
    _TRIED = True
    if os.environ.get("DOCX_REVIEW_DISABLE_TOKENIZER"):
        return None
    try:
        import tiktoken  # type: ignore

        for name in ("o200k_base", "cl100k_base"):
            try:
                _ENC = tiktoken.get_encoding(name)
                _MODE = f"tiktoken:{name}"
                return _ENC
            except Exception:  # noqa: BLE001 - 含网络失败、缓存缺失
                continue
    except ImportError:
        pass
    return None


def mode() -> str:
    _load()
    return _MODE


def available() -> bool:
    return _load() is not None


def _estimate(text: str) -> float:
    """保守基线：CJK 逐字 1 token，其余字符按 0.4 token 折算。"""
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x3000 <= o <= 0x303F \
                or 0xFF00 <= o <= 0xFFEF:
            cjk += 1
    return cjk + (len(text) - cjk) * 0.4


def count(text: str, fallback_ratio: float = 1.6) -> int:
    if not text:
        return 0
    enc = _load()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:  # noqa: BLE001
            pass
    return int(_estimate(text) * fallback_ratio) + 1


if __name__ == "__main__":
    import json

    data = sys.stdin.read()
    print(json.dumps({"mode": mode(), "estimated": not available(), "tokens": count(data)},
                     ensure_ascii=False))
