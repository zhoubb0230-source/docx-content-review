#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享底座：退出码、原子写、哈希、JSONL、CLI 骨架。

被本目录下所有脚本 import。不含任何业务判定逻辑，不联网。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator

SPEC_VERSION = "1.1"
SCHEMA_VERSION = "3"
SKILL_VERSION = "1.1.0"


# --------------------------------------------------------------------------
# 退出码（SKILL.md 的 CLI 契约表按此表转译给用户）
# --------------------------------------------------------------------------
class EX:
    OK = 0
    ERROR = 1              # 未分类失败
    USAGE = 2              # 参数错误
    ENV = 3                # 环境缺失（无转换器等）
    SOURCE_GUARD = 4       # 触碰源文档 / 写出工作目录之外
    DISK = 5               # 磁盘空间不足
    WORKSPACE = 6          # 工作目录不合法（在技能目录内 / 不可写）
    BUSY = 7               # 租约被他人持有
    VALIDATE = 8           # 产物校验失败
    NOT_OWNER = 9          # 令牌失效（spec §11.3.4 强制）
    PARSE = 10             # 输入数据无法解析


class SkillError(Exception):
    """带退出码的可控失败。main() 捕获后打印中文说明并以该码退出。"""

    def __init__(self, code: int, message: str, hint: str | None = None,
                 payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        # 失败时也要能给出可机读的处置依据（例如"该把这个参数设成多少"）。
        # 只放小字段，stdout 仍是单行 JSON。
        self.payload = payload or {}


def die(code: int, message: str, hint: str | None = None,
        payload: dict | None = None) -> "NoReturn":  # type: ignore[name-defined]
    raise SkillError(code, message, hint, payload)


# --------------------------------------------------------------------------
# 文件原子性：所有产物一律「写 .tmp → os.replace()」（spec §11.3.3）
# --------------------------------------------------------------------------
def atomic_write_bytes(path: str | os.PathLike, data: bytes) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def atomic_write_text(path: str | os.PathLike, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | os.PathLike, obj: Any, *, indent: int = 2) -> Path:
    return atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent) + "\n")


def atomic_write_jsonl(path: str | os.PathLike, rows: Iterable[Any]) -> Path:
    buf = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return atomic_write_text(path, buf)


def append_jsonl(path: str | os.PathLike, row: Any) -> None:
    """append-only 落盘（Pass 4 断点续跑依赖，spec §11.3.7b）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


def page_ref(rec: dict | None) -> str:
    """页码的措辞。**估算出来的页码不能写成确定的页码。**

    逐段页码只有在文档带分页标记时才是精确的；没有标记时是「字符偏移 ÷ 每页字符数」
    的线性推算，而带表格与图片的长文档远非均匀分布，正文中段能偏出几十页。
    评审人照着一个精确写法的页码翻过去找不到东西，只会认为这条是误报。
    """
    n = (rec or {}).get("page_hint")
    if not n:
        return "位置未知"
    return f"第 {n} 页" if not (rec or {}).get("page_estimated", True) else f"约第 {n} 页"


def read_jsonl_salvage(path: str | os.PathLike) -> tuple[list, int]:
    """尽量读：返回 (能解析的行, 丢掉的坏行数)。

    子 Agent 的产物被截断时，坏的只有最后那一行——前面每一行都是完整的记录。
    `read_jsonl` 遇到坏行直接抛（那是对的：主流程不该静默吃掉损坏数据），
    但**收口时把整片丢掉是另一个极端**：一片二十条问题，因为最后一行断在半路
    就全部作废，而那一片重跑还会撞上同一堵墙（输出装不下不是随机故障）。

    所以严格读用 `read_jsonl`，收口抢救用这个，并且必须把丢了几行报出来。
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    rows, dropped = [], 0
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                dropped += 1
    return rows, dropped


def product_ok(path: str | os.PathLike) -> bool:
    """产物是否「完整可用」。**「文件存在」不等于「做完了」。**

    脚本写盘走 atomic_write_*，半写不会表现为完成；但子 Agent 是用 shell 写的，
    环境抖动、被杀、写到一半断开，都会留下一个**存在但截断**的文件。
    而续跑判定看的是文件在不在——于是这个单元被判为已完成，
    内容却是半截：JSONL 少了后一半，或 JSON 根本解析不了（`read_json` 静默返回默认值，
    整片事实凭空消失，报告里也看不出来）。

    所以凡是子 Agent 写的产物，判完成时都要过这一关。
    """
    p = Path(path)
    if not p.exists():
        return False
    # **空的 JSONL 是合法的**：这一片一个问题都没查出来，就该是零行。
    # 把空文件判成"没做完"，这类分片会被无限重派——而"没查出问题"恰恰是常态。
    # 空的 .json 则不合法：JSON 至少要有一个对象。
    if p.stat().st_size == 0:
        return p.suffix == ".jsonl"
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if p.suffix == ".jsonl":
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                return False
        return True
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


def read_jsonl(path: str | os.PathLike) -> Iterator[dict]:
    p = Path(path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise SkillError(EX.PARSE, f"{p} 第 {lineno} 行不是合法 JSON：{exc}") from exc


def sha256_file(path: str | os.PathLike, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 文本归一化
# --------------------------------------------------------------------------
def normalize_ws(text: str) -> str:
    """仅归一化空白：exact match 校验允许的唯一变形（spec §8 闸门②）。"""
    return "".join(ch for ch in text if not ch.isspace())


def normalize_width(text: str) -> str:
    """全半角统一（审查记忆的键归一化，spec §11.7；不做任何语义处理）。"""
    return unicodedata.normalize("NFKC", text)


def normalize_key(text: str, *, case_sensitive: bool = False) -> str:
    """术语表归一化键：去空白 + 全半角统一 + 大小写处理（spec §9.1.2）。"""
    s = normalize_width(text).strip()
    s = "".join(s.split())
    return s if case_sensitive else s.lower()


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def version_header() -> dict:
    """spec §10.6：所有产物头部写入的版本标识。"""
    return {
        "spec_version": SPEC_VERSION,
        "schema_version": SCHEMA_VERSION,
        "skill_version": SKILL_VERSION,
    }


def levenshtein(a: str, b: str, *, cap: int | None = None) -> int:
    """编辑距离。cap 用于提前退出，超过 cap 时返回 cap+1。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if cap is not None and abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(val)
            best = min(best, val)
        prev = cur
        if cap is not None and best > cap:
            return cap + 1
    return prev[-1]


def is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


# --------------------------------------------------------------------------
# 面向读者的标签
# --------------------------------------------------------------------------
# 批注要给人看。规则号（A2、L06）是给排障用的，评审人看到「【L06】」不知道是什么，
# 所以批注正文一律用中文标签，规则号只作为末尾的可追溯标记。
CATEGORY_LABELS = {
    "A1": "错别字", "A2": "“的/地/得”误用", "A3": "标点误用", "A4": "成分残缺",
    "A5": "搭配不当", "A6": "关联词误用", "A7": "重复赘余", "A8": "数字或单位有误",
    "B1": "指代不明", "B2": "表述有歧义", "B3": "长句结构混乱", "B4": "主客体颠倒",
    "B5": "连词与语义不符", "C1": "表达冗长", "C2": "段落过长",
    # P 类的规则号由用户的规则包定义（P-RISK-01…），是开放集合，无法在此穷举；
    # 批注正文用规则的 name 字段（「风险条目描述范式」），这里只兜底。
    "P1": "描述要件缺失",
}
# L 规则按性质归组，让读者一眼知道"这是哪一类毛病"
LOGIC_GROUP_LABELS = {
    "L01": "术语不一致", "L02": "缩略语不一致", "L03": "缩略语未说明",
    "L04": "名称写法不一致", "L05": "近义term混用",
    "L06": "前后数值不一致", "L07": "单位不一致", "L08": "区间自相矛盾",
    "L09": "百分比合计不符", "L10": "条目数与列举不符", "L11": "数值与文字描述不符",
    "L12": "前后日期不一致", "L13": "时间顺序颠倒", "L14": "版本号不一致",
    "L15": "引用对象不存在", "L16": "图表编号异常", "L17": "目录与正文不一致",
    "L18": "图表未被引用", "L19": "章节层级跳级",
    "L20": "前后状态矛盾", "L21": "要求强度前后冲突", "L22": "职责分配冲突",
    "L23": "结论与正文矛盾", "L24": "总结条目数不符",
    "L25": "用了禁用写法", "L26": "未用标准写法",
    "L27": "目标与实测差距过大", "L28": "前后立场不一致",
    "L29": "目标缺对应举措", "L30": "举措缺验收指标", "L31": "承诺缺度量方式",
    "L32": "章节内容缺失",
}
SEVERITY_PREFIX = {"Critical": "[严重]", "High": "[重要]", "Medium": "[提示]", "Low": "[提示]"}


def conflict_admitted(candidate: dict, verdict: dict | None) -> tuple[bool, str]:
    """Pass 4 裁定 → 该冲突候选是否准入交付物。三处调用点必须用同一份策略。

    **未裁定与 UNSURE 都是"不确定"，一律不进交付物**——这是底线三，
    也是 `logic-rules.md` 写死的"所有 L 规则的输出都是候选，必须过 Pass 4 裁定后
    才进交付物"。

    早先三处调用点各写了一遍 `if v and v.get("verdict") == "NOT_CONFLICT": continue`，
    于是 `v is None`（Pass 4 没跑或跑了一半）时条件不成立，候选**照常写进文档**——
    Pass 4 完全跳过与全部裁定为 CONFLICT，产出一模一样。这是 fail-open。

    唯一的例外是 Critical 级的 UNSURE：数值/日期/状态矛盾漏掉的代价更大，
    保留但必须在批注里标注"待人工确认"（见 prompts/pass4-adjudicate.md 判定表）。
    **未裁定没有这个例外**——它说明流程没走完，不是模型拿不准。
    """
    v = (verdict or {}).get("verdict")
    if v == "CONFLICT":
        return True, ""
    if v == "NOT_CONFLICT":
        return False, "裁定为不构成矛盾"
    if v == "UNSURE":
        if (candidate.get("severity") or "") == "Critical":
            return True, "unsure_critical"
        return False, "UNSURE 按不构成矛盾处理（不确定即无问题）"
    return False, "未经 Pass 4 裁定，不进交付物"


def rule_label(rule: str) -> str:
    """规则号 → 面向读者的中文标签。未登记的规则原样返回。"""
    return CATEGORY_LABELS.get(rule) or LOGIC_GROUP_LABELS.get(rule) or rule


def emit(obj: Any) -> None:
    """脚本的唯一 stdout 出口：单行 JSON，便于 Agent 解析且不打印大对象。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def warn(message: str) -> None:
    sys.stderr.write(f"[warn] {message}\n")


def run_cli(main_fn) -> None:
    """统一 CLI 骨架：把 SkillError 转成中文说明 + 规范退出码。"""
    try:
        rc = main_fn(sys.argv[1:])
    except SkillError as exc:
        sys.stderr.write(f"[终止] {exc.message}\n")
        if exc.hint:
            sys.stderr.write(exc.hint.rstrip() + "\n")
        emit({"ok": False, "exit_code": exc.code, "error": exc.message, **exc.payload})
        sys.exit(exc.code)
    except KeyboardInterrupt:
        sys.stderr.write("[中断] 用户终止；已完成的产物保留。\n")
        sys.exit(EX.ERROR)
    sys.exit(EX.OK if rc is None else rc)
