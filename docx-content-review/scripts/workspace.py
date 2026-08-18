#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作目录解析、按文档隔离、写路径守卫、租约与分片 claim。

spec §5.1 / §11.1–§11.3 / D6 / D7 的实现。本文件是所有其他脚本的路径来源，
任何脚本都不得自行拼接输出路径。

子命令
  init          解析输出根 → 定位/新建文档隔离目录 → 建 run → 复制源文档
  locate        只查找已有文档目录，不创建（用于续跑判定）
  resolve       取某个 run 的标准路径（kind 见 KINDS）
  guard         校验一个写入路径是否落在本次运行的工作目录内
  lease         独占租约：acquire/heartbeat/verify/release/takeover/status
  claim         分片 claim：next/release/status/reclaim
  config        打印生效配置（默认配置 + 用户覆盖）
  fscheck       判断目录是否位于本地文件系统
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import socket
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EX,
    SkillError,
    atomic_write_json,
    atomic_write_text,
    die,
    emit,
    now_iso,
    read_json,
    run_cli,
    sha256_file,
    sha256_text,
    version_header,
    warn,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
ILLEGAL_SLUG_CHARS = set('/\\:*?"<>|')
NETWORK_FS = {
    "nfs", "nfs3", "nfs4", "cifs", "smbfs", "smb2", "smb3", "afs", "ncpfs",
    "fuse.sshfs", "fuse.s3fs", "fuse.rclone", "fuse.glusterfs", "glusterfs",
    "ceph", "9p", "davfs", "fuse.davfs",
}

# resolve 支持的路径种类。新增产物必须先在此登记，禁止各脚本自行拼路径。
KINDS = {
    # run 级
    "run": "",
    "manifest": "manifest.json",
    "config_snapshot": "config.snapshot.yaml",
    "work": "work",
    "output": "output",
    "logs": "logs",
    "source_copy_dir": "work",
    "normalized": "work/normalized.docx",
    "unpacked": "work/unpacked",
    "paragraphs": "work/paragraphs.jsonl",
    "headings": "work/headings.json",
    "chunks": "work/chunks",
    "chunk_index": "work/chunks/index.json",
    "issues": "work/issues",
    "facts": "work/facts",
    "ledger_db": "work/ledger.db",
    "glossary_candidates": "work/glossary-candidates.json",
    "glossary_extracted": "work/glossary-extracted.json",
    "glossary_merged": "work/glossary.merged.json",
    "conflicts_candidate": "work/conflicts",
    "conflicts_batches": "work/conflicts/batches",
    "conflicts_verdicts": "work/conflicts/verdicts",
    "conflicts_verified": "work/conflicts-verified.jsonl",
    "issues_verified": "work/issues-verified.jsonl",
    "typos": "work/typos",
    "patterns": "work/patterns",
    "verify": "work/verify",
    "patchlist": "work/patchlist.json",
    # 以下四项是 run 内的暂存位置。**最终交付物不在这里**——
    # 交付路径由 deliver_path() 依 manifest 的 deliver_dir 解析（见 DELIVERABLES）。
    "report": "output/report.md",
    "issues_xlsx": "output/issues.xlsx",
    "metrics": "output/metrics.json",
    "glossary_out": "output/glossary.merged.json",
    # 文档级（run 之外）
    "doc": "..",
    "doc_meta": "../doc.json",
    "doc_glossary": "../glossary.json",
    "owner": "../owner.json",
    "latest": "../latest.json",
    # 工作根级
    "review_memory": "../../review-memory.json",
}


# 产物命名：文件名 = filename_pattern.format(stem=原文件名, ts=时间戳, ext=下表后缀)。
# 五个产物都会生成，但**只有 deliver_kinds 里的进交付目录**，其余落在 run/output/
# 之下随临时目录一起清理。默认只交付 docx——报告、xlsx、metrics、术语表各有用途
# （转述、回灌审查记忆、调优、下一轮输入），但不该堆在用户的工作目录里。
ARTIFACTS = {
    "reviewed_docx": ".docx",
    "report":        ".report.md",
    "issues_xlsx":   ".issues.xlsx",
    "metrics":       ".metrics.json",
    "glossary_out":  ".glossary.json",
}
DEFAULT_DELIVERED = ["reviewed_docx"]
# 配置项 output.deliver_<x> → 产物 kind
DELIVER_FLAGS = {
    "reviewed_docx": None,          # 审查版 docx 恒为交付物，不可关
    "report": "deliver_report",
    "issues_xlsx": "deliver_issues_xlsx",
    "metrics": "deliver_metrics",
    "glossary_out": "deliver_glossary",
}


def delivered_kinds(cfg: dict) -> list[str]:
    out = ["reviewed_docx"]
    oc = cfg.get("output") or {}
    for kind, flag in DELIVER_FLAGS.items():
        if flag and oc.get(flag):
            out.append(kind)
    return out


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config(config_path: str | None = None, overrides: dict | None = None) -> dict:
    import yaml

    default_path = SKILL_ROOT / "assets" / "config.default.yaml"
    cfg = yaml.safe_load(default_path.read_text(encoding="utf-8")) or {}
    if config_path:
        p = Path(config_path)
        if not p.exists():
            die(EX.USAGE, f"配置文件不存在：{p}")
        user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = _deep_merge(cfg, user)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    # D4：落笔门槛强制 conservative，不可通过配置放宽
    cfg["apply_threshold"] = "conservative"
    return cfg


def config_hash(cfg: dict) -> str:
    """配置指纹。用于判断「这份产物是不是按当前配置算出来的」。"""
    import yaml

    return sha256_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True))[:16]


def load_run_config(run_dir: Path | str | None, config_path: str | None = None,
                    overrides: dict | None = None) -> dict:
    """带 run 目录的脚本一律用这个，不要直接用 load_config。

    `load_config` 只认默认值 + 显式 `--config`，**不读 run 里的配置快照**。
    于是「init 时带了 --config，后面某一步忘了带」会得到一个静默的半生效状态：
    chunk.py 按新预算切片、verify_span.py 按旧上限截断，两边都不报错。
    这里让缺省行为回落到 `work/../config.snapshot.yaml`——那正是本次 run 的配置。
    """
    if config_path or run_dir is None:
        return load_config(config_path, overrides)
    snap = resolve_path(Path(run_dir), "config_snapshot")
    return load_config(str(snap) if snap.exists() else None, overrides)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# 输出根解析（spec §11.1）
# --------------------------------------------------------------------------
def _validate_root(root: Path, source: Path | None, label: str, *, allow_source_dir: bool) -> Path:
    """三项校验：不得在技能目录内、不得等于源文档目录（CWD 例外）、必须实际可写。"""
    if not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            die(EX.WORKSPACE, f"{label}无法创建：{root}（{exc}）")
    if not root.is_dir():
        die(EX.WORKSPACE, f"{label}不是目录：{root}")

    if root == SKILL_ROOT:
        die(EX.WORKSPACE, f"{label}不得为技能目录：{root}")
    try:
        root.relative_to(SKILL_ROOT)
        die(EX.WORKSPACE, f"{label}位于技能目录内：{root}",
            "技能目录在运行期只读。请指定技能目录之外的路径。")
    except ValueError:
        pass

    if source is not None and not allow_source_dir:
        src_dir = source.resolve().parent
        if root == src_dir and root != Path.cwd().resolve():
            die(EX.WORKSPACE, f"{label}与源文档所在目录相同：{root}",
                "为避免污染源文档目录，请指定其他目录（当前工作目录除外）。")

    probe = root / f".docx-review-probe-{os.getpid()}-{_rand_suffix()}"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        die(EX.WORKSPACE, f"{label}不可写：{root}（{exc}）")
    return root


def resolve_deliver_root(cli_dir: str | None, cfg: dict, source: Path | None) -> Path:
    """交付目录：最终产物落点。默认 Agent 当前工作目录。"""
    raw = (cli_dir or os.environ.get("DOCX_REVIEW_OUTPUT_DIR")
           or (cfg.get("workspace") or {}).get("output_dir"))
    root = (Path(raw).expanduser() if raw else Path.cwd()).resolve()
    # 交付到源文档所在目录是常见且合理的用法（产物带"审查版_时间戳"后缀，不会覆盖原件）
    return _validate_root(root, source, "交付目录", allow_source_dir=True)


def resolve_temp_root(cli_dir: str | None, cfg: dict, source: Path | None,
                      deliver_root: Path) -> Path:
    """临时根：只放中间件（`<临时根>/docx-review/…`），可整体删除。

    **默认就跟随交付目录**，也就是 Agent 当前工作目录——中间件与交付物同处一地，
    用户找得到、也能一眼看出哪些是可删的。只有在显式指定时才落到别处
    （比如工作目录在网络盘上、或想把大体积中间件放到另一块盘）。

    显式指定不可用时是硬失败：用户指名要那里，悄悄换地方比失败更糟。
    """
    explicit = (cli_dir or os.environ.get("DOCX_REVIEW_TEMP_DIR")
                or (cfg.get("workspace") or {}).get("temp_dir"))
    if not explicit:
        return deliver_root          # 已由 resolve_deliver_root 校验过
    root = Path(explicit).expanduser().resolve()
    # 交付目录可以是源文档所在目录（产物带"审查版_时间戳"后缀，不会覆盖原件），
    # 但**显式指定的临时根不行**——中间件会在那里建整棵目录树并反复读写，
    # 那是源文档所在的地方。CWD 仍是例外，由 _validate_root 内部放行。
    return _validate_root(root, source, "临时目录", allow_source_dir=False)


def _rand_suffix(n: int = 4) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def slugify(name: str, max_chars: int) -> str:
    stem = Path(name).stem
    out = []
    for ch in stem:
        if ch in ILLEGAL_SLUG_CHARS or ord(ch) < 32:
            out.append("-")
        else:
            out.append(ch)
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    s = s.strip("-. ") or "document"
    return s[:max_chars]


# --------------------------------------------------------------------------
# 文档隔离目录（spec §11.2）
# --------------------------------------------------------------------------
def doc_dir_candidates(base: Path, slug: str, sha12: str):
    yield base / f"{slug}-{sha12}"
    n = 2
    while n <= 99:
        yield base / f"{slug}-{sha12}-{n}"
        n += 1


def locate_doc_dir(base: Path, slug: str, sha256: str, prefix_len: int, *, create: bool) -> tuple[Path, bool]:
    """返回 (文档目录, 是否新建)。命中同名目录后必须比对完整 sha256。"""
    sha12 = sha256[:prefix_len]
    for cand in doc_dir_candidates(base, slug, sha12):
        if not cand.exists():
            if not create:
                return cand, True
            cand.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                cand / "doc.json",
                {**version_header(), "slug": slug, "source_sha256": sha256, "created_at": now_iso()},
            )
            return cand, True
        stored = _stored_sha(cand)
        if stored is None:
            # 目录存在但无可信元数据：视为可接管的空壳，补写元数据
            if create:
                atomic_write_json(
                    cand / "doc.json",
                    {**version_header(), "slug": slug, "source_sha256": sha256, "created_at": now_iso()},
                )
            return cand, False
        if stored == sha256:
            return cand, False
        # sha12 前缀相同但全长不同 → 换下一个序号后缀
    die(EX.WORKSPACE, f"同一 sha12 前缀下的目录序号已超过 99：{base}/{slug}-{sha12}-*")


def _stored_sha(doc_dir: Path) -> str | None:
    meta = read_json(doc_dir / "doc.json")
    if isinstance(meta, dict) and meta.get("source_sha256"):
        return meta["source_sha256"]
    for run in sorted(doc_dir.glob("run-*")):
        man = read_json(run / "manifest.json")
        if isinstance(man, dict) and man.get("source_sha256"):
            return man["source_sha256"]
    return None


def new_runid() -> str:
    return "run-" + datetime.now().strftime("%Y%m%d-%H%M") + "-" + _rand_suffix(4)


# --------------------------------------------------------------------------
# 写路径守卫（D6）
# --------------------------------------------------------------------------
def guard_write_path(path: str | os.PathLike, run_dir: str | os.PathLike | None = None,
                     base_dir: str | os.PathLike | None = None) -> Path:
    """目标路径必须落在本次运行的工作目录之下，否则抛异常并终止，不做降级。"""
    p = Path(path)
    p = (p if p.is_absolute() else Path.cwd() / p)
    # 不能用 resolve()：目标文件可能尚不存在，父目录 resolve 即可
    parent = p.parent
    anchor = parent
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    real = (anchor.resolve() / p.relative_to(anchor)) if anchor != p else p.resolve()

    allowed: list[Path] = []
    if run_dir:
        allowed.append(Path(run_dir).resolve())
        # 交付目录由 manifest 声明，同样是本次运行的合法写入根
        man = read_json(Path(run_dir) / "manifest.json", {}) or {}
        if man.get("deliver_dir"):
            allowed.append(Path(man["deliver_dir"]).resolve())
    if base_dir:
        allowed.append(Path(base_dir).resolve())
    if not allowed:
        die(EX.SOURCE_GUARD, f"guard_write_path 未提供工作目录，拒绝写入：{p}")

    for root in allowed:
        try:
            real.relative_to(root)
            break
        except ValueError:
            continue
    else:
        die(
            EX.SOURCE_GUARD,
            f"拒绝写入工作目录之外的路径：{real}",
            "允许的写入根：" + " / ".join(str(a) for a in allowed),
        )

    try:
        real.relative_to(SKILL_ROOT)
        die(EX.SOURCE_GUARD, f"拒绝写入技能目录：{real}")
    except ValueError:
        pass
    return real


# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------
def resolve_path(run_dir: str | os.PathLike, kind: str) -> Path:
    if kind not in KINDS:
        die(EX.USAGE, f"未知的路径种类：{kind}（可用：{', '.join(sorted(KINDS))}）")
    rel = KINDS[kind]
    p = Path(run_dir).resolve()
    return (p / rel).resolve() if rel else p


def deliver_meta(run_dir: str | os.PathLike) -> dict:
    man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
    if not man.get("deliver_dir"):
        die(EX.ERROR, "manifest 中缺少 deliver_dir，请重新执行 workspace.py init")
    return man


def artifact_name(kind: str, man: dict) -> str:
    pattern = man.get("filename_pattern") or "{stem}审查版_{ts}{ext}"
    return pattern.format(stem=man.get("deliver_stem") or "document",
                          ts=man.get("deliver_ts") or "", ext=ARTIFACTS[kind])


def artifact_path(run_dir: str | os.PathLike, kind: str, *, man: dict | None = None) -> Path:
    """产物的最终落点。

    交付物 → `<交付目录>/<原文件名>审查版_<时间戳><后缀>`
    其余   → `<run>/output/` 下同名文件，随临时目录一起清理
    """
    if kind not in ARTIFACTS:
        die(EX.USAGE, f"未知的产物：{kind}（可用：{', '.join(ARTIFACTS)}）")
    man = man or deliver_meta(run_dir)
    name = artifact_name(kind, man)
    if kind in (man.get("deliver_kinds") or DEFAULT_DELIVERED):
        return Path(man["deliver_dir"]) / name
    return resolve_path(run_dir, "output") / name


# 向后兼容的别名：语义与 artifact_path 相同
deliver_path = artifact_path


def deliver_all(run_dir: str | os.PathLike) -> dict:
    """只列真正进交付目录的产物。"""
    man = deliver_meta(run_dir)
    kinds = man.get("deliver_kinds") or DEFAULT_DELIVERED
    return {k: str(artifact_path(run_dir, k, man=man)) for k in kinds}


def artifact_all(run_dir: str | os.PathLike) -> dict:
    """列全部产物及其去向，便于 Agent 向用户说明哪些在工作目录、哪些在临时目录。"""
    man = deliver_meta(run_dir)
    kinds = man.get("deliver_kinds") or DEFAULT_DELIVERED
    return {k: {"path": str(artifact_path(run_dir, k, man=man)), "delivered": k in kinds}
            for k in ARTIFACTS}


def source_copy_path(run_dir: str | os.PathLike, ext: str) -> Path:
    ext = ext if ext.startswith(".") else "." + ext
    return resolve_path(run_dir, "work") / f"source-copy{ext.lower()}"


def working_docx(run_dir: str | os.PathLike) -> Path:
    """当前应被处理的 docx：优先归一化产物，其次 .docx 副本。源文档永不出现在此。"""
    norm = resolve_path(run_dir, "normalized")
    if norm.exists():
        return norm
    cp = source_copy_path(run_dir, ".docx")
    if cp.exists():
        return cp
    die(EX.ERROR, f"工作副本缺失：{norm} 与 {cp} 均不存在（请重跑 Pass -1）")


# --------------------------------------------------------------------------
# 文件系统类型（spec §11.3.5 第 3 条）
# --------------------------------------------------------------------------
def fs_type(path: str | os.PathLike) -> str:
    p = Path(path).resolve()
    try:
        entries = []
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split(" - ")
                if len(parts) < 2:
                    continue
                left, right = parts[0].split(), parts[1].split()
                if len(left) < 5 or not right:
                    continue
                entries.append((left[4], right[0]))
        best, best_type = "", "unknown"
        for mp, ftype in entries:
            if (str(p) == mp or str(p).startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                best, best_type = mp, ftype
        return best_type
    except OSError:
        return "unknown"


def is_local_fs(path: str | os.PathLike) -> bool:
    t = fs_type(path)
    return t.split(".")[0] not in {x.split(".")[0] for x in NETWORK_FS} and t not in NETWORK_FS


# --------------------------------------------------------------------------
# 独占租约（spec §11.3.4）
# --------------------------------------------------------------------------
def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def lease_status(doc_dir: Path) -> dict:
    owner = read_json(doc_dir / "owner.json")
    if not isinstance(owner, dict) or not owner.get("session_id"):
        return {"held": False, "owner": None, "expired": True, "idle_seconds": None}
    exp = _parse_iso(owner.get("expires_at"))
    hb = _parse_iso(owner.get("last_heartbeat"))
    now = datetime.now(timezone.utc).astimezone()
    expired = exp is None or exp < now
    idle = int((now - hb).total_seconds()) if hb else None
    return {"held": not expired, "owner": owner, "expired": expired, "idle_seconds": idle}


def lease_acquire(doc_dir: Path, session: str, runid: str, stage: str, minutes: int,
                  *, takeover: bool = False) -> dict:
    st = lease_status(doc_dir)
    cur = st["owner"] or {}
    gen = int(cur.get("generation") or 0)
    if st["held"] and cur.get("session_id") != session and not takeover:
        raise SkillError(
            EX.BUSY,
            f"该文档已有进行中的审查任务（会话 {cur.get('session_id')}，阶段 {cur.get('stage')}，"
            f"最后活动 {st['idle_seconds']} 秒前）",
            "请向用户呈现选项：加入协作 / 接管 / 独立重跑（见 SKILL.md「并发」一节）。",
        )
    if cur.get("session_id") == session and not takeover:
        gen = int(cur.get("generation") or 1)
    else:
        gen = gen + 1  # 纪元单调递增，每次接管 +1
    now = datetime.now(timezone.utc).astimezone()
    owner = {
        "session_id": session,
        "generation": gen,
        "runid": runid,
        "acquired_at": cur.get("acquired_at") if cur.get("session_id") == session else now.isoformat(timespec="seconds"),
        "last_heartbeat": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        "stage": stage,
    }
    atomic_write_json(doc_dir / "owner.json", owner)
    return owner


def lease_verify(doc_dir: Path, session: str, generation: int | None) -> dict:
    owner = read_json(doc_dir / "owner.json")
    if not isinstance(owner, dict) or owner.get("session_id") != session:
        raise SkillError(
            EX.NOT_OWNER,
            "本会话的写入权限已失效——该文档的审查任务已被另一个会话接管",
            "已完成的分片结果仍然有效并已保留。建议：切换到另一个会话查看进度，或选择「独立重跑」。",
        )
    if generation is not None and int(owner.get("generation") or 0) != int(generation):
        raise SkillError(
            EX.NOT_OWNER,
            f"本会话持有的纪元为 {generation}，当前已是 {owner.get('generation')}——已被接管",
            "已完成的分片结果仍然有效并已保留。建议：切换到另一个会话查看进度，或选择「独立重跑」。",
        )
    return owner


def lease_heartbeat(doc_dir: Path, session: str, generation: int | None, minutes: int,
                    stage: str | None = None) -> dict:
    owner = lease_verify(doc_dir, session, generation)
    now = datetime.now(timezone.utc).astimezone()
    owner["last_heartbeat"] = now.isoformat(timespec="seconds")
    owner["expires_at"] = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    if stage:
        owner["stage"] = stage
    atomic_write_json(doc_dir / "owner.json", owner)
    return owner


def lease_release(doc_dir: Path, session: str, generation: int | None) -> None:
    lease_verify(doc_dir, session, generation)
    p = doc_dir / "owner.json"
    if p.exists():
        p.unlink()


class Heartbeat:
    """长操作（Pass 3、回写）期间的后台续租线程（spec §11.3.7a）。"""

    def __init__(self, doc_dir: Path, session: str, generation: int | None, minutes: int,
                 stage: str, interval: float = 60.0):
        self.args = (Path(doc_dir), session, generation, minutes, stage)
        self.interval = interval
        self._stop = None
        self._thread = None

    def __enter__(self):
        import threading

        if not self.args[1]:
            return self
        self._stop = threading.Event()

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    lease_heartbeat(*self.args[:4], stage=self.args[4])
                except SkillError:
                    return
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False


# --------------------------------------------------------------------------
# 分片 claim（spec §11.3.2）
# --------------------------------------------------------------------------
def claim_path(run_dir: Path, chunk_id: str) -> Path:
    return resolve_path(run_dir, "chunks") / f"{chunk_id}.claim"


def _claim_live(p: Path) -> bool:
    data = read_json(p)
    if not isinstance(data, dict):
        return False
    exp = _parse_iso(data.get("expires_at"))
    return bool(exp and exp > datetime.now(timezone.utc).astimezone())


def claim_acquire(run_dir: Path, chunk_id: str, session: str, generation: int | None,
                  minutes: int) -> bool:
    """O_EXCL 原子创建。返回 False 表示该片已被他人持有。"""
    p = claim_path(run_dir, chunk_id)
    guard_write_path(p, run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone()
    payload = {
        "session_id": session,
        "generation": generation,
        "claimed_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        # _debug 仅供排障，严禁参与判活或接管逻辑
        "_debug": {"hostname": socket.gethostname(), "pid": os.getpid(),
                   "agent_version": os.environ.get("AGENT_VERSION", "")},
    }
    import json as _json

    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if _claim_live(p):
            return False
        # 过期 claim：回收前必须先看产物是否已存在（调用方负责），此处直接续期占用
        try:
            p.unlink()
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, OSError):
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_json.dumps(payload, ensure_ascii=False))
    return True


def claim_renew(run_dir: Path, chunk_id: str, session: str, minutes: int) -> bool:
    """每次 LLM 调用前后续期，避免退避超出 TTL（spec §7.4.2 第 1 条）。"""
    p = claim_path(run_dir, chunk_id)
    data = read_json(p)
    if not isinstance(data, dict) or data.get("session_id") != session:
        return False
    now = datetime.now(timezone.utc).astimezone()
    data["expires_at"] = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    atomic_write_json(p, data)
    return True


def claim_release(run_dir: Path, chunk_id: str, session: str | None = None) -> bool:
    p = claim_path(run_dir, chunk_id)
    if not p.exists():
        return False
    if session:
        data = read_json(p)
        if isinstance(data, dict) and data.get("session_id") not in (session, None):
            return False
    p.unlink()
    return True


def chunk_products(run_dir: Path, chunk_id: str) -> dict:
    return {
        "issues": resolve_path(run_dir, "issues") / f"issues-{chunk_id}.jsonl",
        "facts": resolve_path(run_dir, "facts") / f"facts-{chunk_id}.json",
    }


def chunk_done(run_dir: Path, chunk_id: str, *, need_issues: bool = True) -> bool:
    """文件存在性即状态（spec §11.3.3）。纯表格片不产 issues，只看 facts。"""
    prod = chunk_products(run_dir, chunk_id)
    ok_facts = prod["facts"].exists()
    ok_issues = prod["issues"].exists() or not need_issues
    return ok_facts and ok_issues


def list_chunk_ids(run_dir: Path) -> list[str]:
    idx = read_json(resolve_path(run_dir, "chunk_index"), {})
    if isinstance(idx, dict) and idx.get("chunks"):
        return [c["chunk_id"] for c in idx["chunks"]]
    return sorted(p.stem.split("-")[-1] for p in resolve_path(run_dir, "chunks").glob("chunk-*.txt"))


def next_pending_chunk(run_dir: Path, session: str, generation: int | None, minutes: int) -> dict | None:
    """工作池循环的核心：claim 下一个未完成分片。"""
    idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
    metas = {c["chunk_id"]: c for c in idx.get("chunks", [])}
    for cid in list_chunk_ids(run_dir):
        meta = metas.get(cid, {})
        need_issues = meta.get("chunk_type") != "table_only"
        if chunk_done(run_dir, cid, need_issues=need_issues):
            claim_release(run_dir, cid)
            continue
        cp = claim_path(run_dir, cid)
        if cp.exists() and _claim_live(cp):
            continue
        if cp.exists() and not _claim_live(cp):
            # 回收过期 claim 前必须先检查产物（上面已查），此处安全回收
            claim_release(run_dir, cid)
        if claim_acquire(run_dir, cid, session, generation, minutes):
            return {"chunk_id": cid, **meta}
    return None


# --------------------------------------------------------------------------
# init：Pass -1 的第 1–5、8 步
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    cfg = load_config(args.config)
    ws = cfg.get("workspace") or {}
    src = Path(args.source).expanduser()
    if not src.exists() or not src.is_file():
        die(EX.USAGE, f"源文档不存在或不可读：{src}")
    ext = src.suffix.lower()
    if ext not in (".doc", ".docx", ".dotx", ".docm"):
        die(EX.USAGE, f"不支持的输入格式：{ext}（本技能只处理 .doc / .docx）")

    # 交付目录（最终产物）与临时目录（中间件）相互独立
    deliver_root = resolve_deliver_root(args.output_dir, cfg, src)
    temp_root = resolve_temp_root(args.temp_dir, cfg, src, deliver_root)
    base = temp_root / (ws.get("subdir") or "docx-review")
    base.mkdir(parents=True, exist_ok=True)

    # 2) 源文档 sha256 + 元数据
    stat = src.stat()
    sha = sha256_file(src)
    slug = slugify(src.name, int(ws.get("slug_max_chars") or 40))
    doc_dir, is_new_doc = locate_doc_dir(base, slug, sha, int(ws.get("hash_prefix_len") or 12), create=True)

    # 3) 磁盘空间预检
    mult = int(ws.get("min_free_space_multiplier") or 6)
    free = shutil.disk_usage(str(base)).free
    need = stat.st_size * mult
    if free < need:
        die(
            EX.DISK,
            f"磁盘可用空间不足：需要约 {need / 1048576:.0f} MiB（源文档 {stat.st_size / 1048576:.1f} MiB × {mult}），"
            f"当前可用 {free / 1048576:.0f} MiB",
            "请清理磁盘或改用 --output-dir 指向空间充足的分区。禁止为腾空间自动删除任何产物。",
        )

    # 续跑判定
    latest = read_json(doc_dir / "latest.json")
    resume_candidate = None
    if isinstance(latest, dict) and latest.get("runid"):
        cand = doc_dir / latest["runid"]
        man = read_json(cand / "manifest.json")
        if cand.exists() and isinstance(man, dict) and man.get("stage") != "completed":
            resume_candidate = {"runid": latest["runid"], "run_dir": str(cand),
                                "stage": man.get("stage"), "stats": man.get("stats", {})}
    if resume_candidate and args.resume == "auto":
        emit({"ok": True, "action": "resume_available", "doc_dir": str(doc_dir),
              "deliver_dir": str(deliver_root),
              "resume": resume_candidate, "lease": lease_status(doc_dir),
              "source_sha256": sha,
              "hint": "存在未完成的 run。请询问用户：续跑（--resume reuse）还是新建（--resume new）。"})
        return EX.OK
    if resume_candidate and args.resume == "reuse":
        run_dir = Path(resume_candidate["run_dir"])
        _ensure_source_copy(run_dir, src, sha, ext)
        man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
        man["deliver_dir"] = str(deliver_root)      # 交付目录可能随会话变化，续跑时刷新
        atomic_write_json(resolve_path(run_dir, "manifest"), man)
        emit({"ok": True, "action": "resumed", "doc_dir": str(doc_dir), "run_dir": str(run_dir),
              "runid": run_dir.name, "source_sha256": sha,
              "stage": resume_candidate["stage"], "stats": resume_candidate.get("stats", {}),
              "local_fs": is_local_fs(base), "deliver_dir": str(deliver_root),
              "lease": lease_status(doc_dir),
              "next": "workspace.py lease acquire --doc-dir <doc_dir> --session <sid> "
                      f"--runid {run_dir.name} --stage pass-1",
              "deliverables": deliver_all(run_dir)})
        return EX.OK

    # 新建 run
    runid = new_runid()
    run_dir = doc_dir / runid
    for kind in ("work", "output", "logs"):
        (run_dir / KINDS[kind]).mkdir(parents=True, exist_ok=True)
    for kind in ("chunks", "issues", "facts", "conflicts_candidate"):
        resolve_path(run_dir, kind).mkdir(parents=True, exist_ok=True)

    # 4-5) 复制源文档并校验
    copy_path = _ensure_source_copy(run_dir, src, sha, ext)

    # 8) manifest 初始状态
    import yaml

    atomic_write_text(resolve_path(run_dir, "config_snapshot"),
                      yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    manifest = {
        **version_header(),
        "runid": runid,
        "source": str(src.resolve()),
        "source_name": src.name,
        "source_sha256": sha,
        "source_size": stat.st_size,
        "source_mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds"),
        "generation": int((lease_status(doc_dir)["owner"] or {}).get("generation") or 0),
        "stage": "pass-1",
        "created_at": now_iso(),
        "config": {"strictness": cfg.get("strictness"),
                   "max_context_tokens": (cfg.get("chunking") or {}).get("max_context_tokens")},
        "config_hash": config_hash(cfg),
        "ab_seed": args.seed if args.seed is not None else random.randint(1, 2 ** 31 - 1),
        # 交付元信息：交付物文件名在 init 时定死，全流程共用同一时间戳
        "deliver_dir": str(deliver_root),
        "deliver_stem": Path(src.name).stem,
        "deliver_ts": datetime.now().strftime(
            (cfg.get("output") or {}).get("timestamp_format") or "%Y%m%d_%H%M%S"),
        "filename_pattern": (cfg.get("output") or {}).get("filename_pattern")
        or "{stem}审查版_{ts}{ext}",
        "deliver_kinds": delivered_kinds(cfg),
        "temp_root": str(temp_root),
        "chunks": [],
        "stats": {"total_chunks": 0, "done": 0, "failed": 0, "pending": 0},
    }
    atomic_write_json(resolve_path(run_dir, "manifest"), manifest)
    atomic_write_json(doc_dir / "latest.json", {"runid": runid, "updated_at": now_iso()})
    _prune_runs(doc_dir, int(ws.get("keep_runs") or 3))

    emit({
        "ok": True, "action": "created", "doc_dir": str(doc_dir), "run_dir": str(run_dir),
        "runid": runid, "source_sha256": sha, "source_copy": str(copy_path),
        "needs_conversion": ext == ".doc", "new_doc_dir": is_new_doc,
        "local_fs": is_local_fs(base), "fs_type": fs_type(base),
        "temp_root": str(temp_root), "deliver_dir": str(deliver_root),
        # 租约不在 init 里自动获取：并发时要先把现状呈现给用户再由他选
        # （加入协作 / 接管 / 独立重跑）。但**必须获取**——Pass 3/4 与回写的
        # 全部脚本都会 lease_verify，没有 owner.json 时一律 exit 9，
        # 而那条错误信息说的是"已被另一个会话接管"，与实情完全相反。
        "lease": lease_status(doc_dir),
        "next": "workspace.py lease acquire --doc-dir <doc_dir> --session <sid> "
                f"--runid {runid} --stage pass-1",
        "deliverables": deliver_all(run_dir), "artifacts": artifact_all(run_dir),
    })
    return EX.OK


def _ensure_source_copy(run_dir: Path, src: Path, sha: str, ext: str) -> Path:
    dst = source_copy_path(run_dir, ext)
    guard_write_path(dst, run_dir)
    if dst.exists() and sha256_file(dst) == sha:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except OSError as exc:
        die(EX.SOURCE_GUARD, f"源文档复制失败：{exc}",
            "源文档可能被占用或目标目录不可写。禁止原地操作，请排除后重试。")
    if sha256_file(dst) != sha:
        die(EX.SOURCE_GUARD, "源文档副本校验失败（sha256 不一致），可能复制过程中源文件被修改")
    return dst


def _prune_runs(doc_dir: Path, keep: int) -> None:
    runs = sorted([p for p in doc_dir.glob("run-*") if p.is_dir()], key=lambda p: p.name)
    for old in runs[:-keep] if keep > 0 else []:
        # 从不自动删除 output/，只清 work/
        work = old / "work"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 其他子命令
# --------------------------------------------------------------------------
def cmd_locate(args) -> int:
    cfg = load_config(args.config)
    ws = cfg.get("workspace") or {}
    src = Path(args.source).expanduser()
    if not src.exists():
        die(EX.USAGE, f"源文档不存在：{src}")
    sha = sha256_file(src)
    root = resolve_temp_root(args.temp_dir, cfg, src,
                             resolve_deliver_root(None, cfg, src))
    base = root / (ws.get("subdir") or "docx-review")
    slug = slugify(src.name, int(ws.get("slug_max_chars") or 40))
    prefix = int(ws.get("hash_prefix_len") or 12)
    found = None
    for cand in doc_dir_candidates(base, slug, sha[:prefix]):
        if not cand.exists():
            break
        if _stored_sha(cand) == sha:
            found = cand
            break
    out: dict[str, Any] = {"ok": True, "source_sha256": sha, "sha12": sha[:prefix],
                           "base": str(base), "found": bool(found)}
    if found:
        latest = read_json(found / "latest.json") or {}
        man = read_json(found / str(latest.get("runid") or "") / "manifest.json") or {}
        out.update({"doc_dir": str(found), "latest_runid": latest.get("runid"),
                    "stage": man.get("stage"), "stats": man.get("stats", {}),
                    "lease": lease_status(found)})
    emit(out)
    return EX.OK


def cmd_resolve(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    if args.kind:
        emit({"ok": True, "path": str(resolve_path(run_dir, args.kind))})
    else:
        emit({"ok": True, "paths": {k: str(resolve_path(run_dir, k)) for k in sorted(KINDS)}})
    return EX.OK


def cmd_guard(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    base = run_dir.parent.parent if args.allow_base else None
    p = guard_write_path(args.path, run_dir, base)
    emit({"ok": True, "path": str(p)})
    return EX.OK


def cmd_lease(args) -> int:
    doc_dir = Path(args.doc_dir).resolve()
    cfg = load_config(args.config)
    minutes = int((cfg.get("concurrency") or {}).get("lease_minutes") or 30)
    if args.op == "status":
        emit({"ok": True, **lease_status(doc_dir)})
    elif args.op == "acquire":
        owner = lease_acquire(doc_dir, args.session, args.runid or "", args.stage or "",
                              minutes, takeover=False)
        emit({"ok": True, "owner": owner})
    elif args.op == "takeover":
        if (args.stage or "") in ("writeback", "apply", "report"):
            die(EX.USAGE, "回写阶段禁止接管（spec §11.3.6）",
                "请等待原任务完成，或选择「独立重跑」。")
        owner = lease_acquire(doc_dir, args.session, args.runid or "", args.stage or "",
                              minutes, takeover=True)
        emit({"ok": True, "owner": owner, "note": "已轮换纪元，原会话令牌作废"})
    elif args.op == "heartbeat":
        owner = lease_heartbeat(doc_dir, args.session, args.generation, minutes, args.stage)
        emit({"ok": True, "owner": owner})
    elif args.op == "verify":
        owner = lease_verify(doc_dir, args.session, args.generation)
        emit({"ok": True, "owner": owner})
    elif args.op == "release":
        lease_release(doc_dir, args.session, args.generation)
        emit({"ok": True, "released": True})
    return EX.OK


def cmd_claim(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    cfg = load_config(args.config)
    minutes = int((cfg.get("concurrency") or {}).get("chunk_claim_minutes") or 20)
    if args.op == "next":
        got = next_pending_chunk(run_dir, args.session, args.generation, minutes)
        emit({"ok": True, "chunk": got, "exhausted": got is None})
    elif args.op == "renew":
        emit({"ok": True, "renewed": claim_renew(run_dir, args.chunk, args.session, minutes)})
    elif args.op == "release":
        emit({"ok": True, "released": claim_release(run_dir, args.chunk, args.session)})
    elif args.op == "status":
        ids = list_chunk_ids(run_dir)
        idx = read_json(resolve_path(run_dir, "chunk_index"), {}) or {}
        metas = {c["chunk_id"]: c for c in idx.get("chunks", [])}
        done = [c for c in ids if chunk_done(run_dir, c, need_issues=metas.get(c, {}).get("chunk_type") != "table_only")]
        claimed = [c for c in ids if claim_path(run_dir, c).exists() and _claim_live(claim_path(run_dir, c))]
        emit({"ok": True, "total": len(ids), "done": len(done), "claimed": len(claimed),
              "pending": len(ids) - len(done)})
    return EX.OK


def cmd_deliver(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    if args.kind:
        emit({"ok": True, "path": str(artifact_path(run_dir, args.kind))})
    else:
        man = deliver_meta(run_dir)
        emit({"ok": True, "deliver_dir": man["deliver_dir"],
              "paths": deliver_all(run_dir), "artifacts": artifact_all(run_dir)})
    return EX.OK


def cmd_clean_temp(args) -> int:
    """删除本次 run 的临时目录。交付物在交付目录，不受影响。"""
    run_dir = Path(args.run_dir).resolve()
    man = read_json(resolve_path(run_dir, "manifest"), {}) or {}
    stage = man.get("stage")
    if stage != "completed" and not args.force:
        die(EX.USAGE, f"当前阶段为 {stage}，尚未完成；删除临时目录会丢失续跑所需的中间件",
            "确认无需续跑再加 --force。")
    # 必须在删除之前把路径读出来——manifest 就在待删目录里
    allart = artifact_all(run_dir)
    kept = {k: v["path"] for k, v in allart.items() if v["delivered"]}
    dropped = {k: v["path"] for k, v in allart.items() if not v["delivered"]}
    missing = [k for k, v in kept.items() if not Path(v).exists()]
    if missing and not args.force:
        die(EX.USAGE, f"交付物尚未全部产出（缺 {missing}），拒绝删除临时目录",
            "先跑完 report.py 与打包步骤，或加 --force 强制删除。")
    doc_dir = run_dir.parent
    shutil.rmtree(run_dir, ignore_errors=True)
    # 文档目录若只剩指针类文件，一并清掉
    leftovers = [x for x in doc_dir.iterdir()
                 if x.name not in ("latest.json", "doc.json", "owner.json", "glossary.json")]
    if not leftovers:
        shutil.rmtree(doc_dir, ignore_errors=True)
    emit({"ok": True, "removed": str(run_dir), "deliverables_kept": kept,
          "also_removed": dropped,
          "note": "报告/xlsx/metrics/术语表未列为交付物，随临时目录一并删除；"
                  "需要保留请先复制，或在配置中打开对应的 output.deliver_* 开关"})
    return EX.OK


def cmd_config(args) -> int:
    emit({"ok": True, "config": load_config(args.config)})
    return EX.OK


def cmd_fscheck(args) -> int:
    emit({"ok": True, "path": str(Path(args.path).resolve()), "fs_type": fs_type(args.path),
          "local": is_local_fs(args.path)})
    return EX.OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="workspace.py", description="工作目录与并发控制")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化工作目录并复制源文档（Pass -1）")
    p.add_argument("--source", required=True)
    p.add_argument("--output-dir", help="交付目录（最终产物），默认 Agent 当前工作目录")
    p.add_argument("--temp-dir", help="临时目录根（只放中间件），默认跟随交付目录")
    p.add_argument("--config")
    p.add_argument("--resume", choices=["auto", "new", "reuse"], default="auto")
    p.add_argument("--seed", type=int)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("locate", help="查找已有文档目录，不创建")
    p.add_argument("--source", required=True)
    p.add_argument("--temp-dir")
    p.add_argument("--config")
    p.set_defaults(func=cmd_locate)

    p = sub.add_parser("resolve", help="取标准路径")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--kind")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("guard", help="校验写路径")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--allow-base", action="store_true", help="同时允许写文档目录/工作根（审查记忆）")
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser("lease", help="独占租约")
    p.add_argument("op", choices=["status", "acquire", "takeover", "heartbeat", "verify", "release"])
    p.add_argument("--doc-dir", required=True)
    p.add_argument("--session")
    p.add_argument("--generation", type=int)
    p.add_argument("--runid")
    p.add_argument("--stage")
    p.add_argument("--config")
    p.set_defaults(func=cmd_lease)

    p = sub.add_parser("claim", help="分片 claim")
    p.add_argument("op", choices=["next", "renew", "release", "status"])
    p.add_argument("--run-dir", required=True)
    p.add_argument("--session")
    p.add_argument("--generation", type=int)
    p.add_argument("--chunk")
    p.add_argument("--config")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("deliver", help="打印交付物最终路径")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--kind", choices=sorted(ARTIFACTS))
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("clean-temp", help="删除本次 run 的临时目录（交付物不受影响）")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--force", action="store_true", help="即使 stage != completed 也删除")
    p.set_defaults(func=cmd_clean_temp)

    p = sub.add_parser("config", help="打印生效配置")
    p.add_argument("--config")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("fscheck", help="判断是否本地文件系统")
    p.add_argument("--path", required=True)
    p.set_defaults(func=cmd_fscheck)
    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "op", None) in ("acquire", "takeover", "heartbeat", "verify", "release", "next", "renew") \
            and not getattr(args, "session", None):
        die(EX.USAGE, "该操作需要 --session")
    return args.func(args)


if __name__ == "__main__":
    run_cli(main)
