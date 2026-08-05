---
name: docx-content-review
description: 对 Word 文档（.doc/.docx）做语病、语义与全文逻辑性审查，输出审查报告并回写修订与批注。当用户要求审查、校对、查错、通读、把关、挑毛病，或要求检查前后一致性、前后矛盾、上下文冲突、术语是否统一、数据是否对得上、语病与错别字、表达是否有歧义时使用。也用于合同、方案、规划、可研、评审材料、设计说明书等长文档的交叉一致性核对。不做格式排版审查，不修改任何样式。
platform: XAgent
version: 1.1.0
---

# docx-content-review

对 Word 文档执行**语病 / 语义 / 逻辑性**三类审查，交付「审查报告 + 带修订与批注的 Word 文档」。
支持 0–3000 页，支持中断续跑与多会话协作。

## 三条不可违反的底线

1. **源文档只读。** 第一个动作是复制源文档，此后所有路径一律指向副本。任何脚本都不得以源文档路径为写入目标。
2. **不改任何样式。** 只改文本，且只以修订/批注形式改。字体、字号、行距、缩进、编号、颜色、段落格式、页面设置一律不动。发现格式问题也不报告（那是 `docx-format-audit` 的职责）。
3. **不确定即无问题。** 所有判定都是封闭选择题；UNSURE 一律按"无问题"处理。宁可漏报，不可误报。

## 你不做什么

不做格式排版审查；不做事实核查（只查文档内部自洽性，不判断与外部世界是否相符）；不做润色、改写、压缩；不处理 PDF 与 WPS 专有格式。不保证发现所有问题——召回率不是本技能的优化目标。

---

## 主流程

所有脚本位于 `scripts/`，用 `python3 scripts/<name>.py` 调用，**依 CLI 契约表调用即可，不必阅读脚本源码**。
每个脚本 stdout 输出单行 JSON；失败时 stderr 给出中文说明，退出码见契约表。

### 第 0 步：初始化与续跑判定

```
workspace.py init --source <文档路径> [--output-dir <交付目录>] [--temp-dir <临时根>] [--config <yaml>]
```

**两个目录，分工明确**：

| 目录 | 放什么 | 默认值 | 可否删除 |
|---|---|---|---|
| **交付目录** | 最终产物：审查版 docx、报告、issues.xlsx、metrics、术语表 | Agent 当前工作目录 | 不可，这是交付物 |
| **临时目录** | 全部中间件：源文档副本、解包目录、分片、台账、冲突候选 | Windows `D:\temp_doc_review`；其他平台系统临时目录下的 `temp_doc_review` | 可整体删除 |

交付物文件名固定为 **`<原文件名>审查版_<时间戳>.<后缀>`**（时间戳格式 `20260805_121153`，
全流程共用 init 时生成的同一个时间戳）。取交付路径用
`workspace.py deliver --run-dir <run>`，**不要自己拼**。

返回 `action` 决定下一步：

| action | 含义 | 你要做的 |
|---|---|---|
| `created` | 新建了 run | 继续第 1 步 |
| `resume_available` | 该文档有未完成的 run | **必须问用户**：续跑还是新建，再用 `--resume reuse` 或 `--resume new` 重新调用 |
| `resumed` | 已挂到旧 run | 跳到 `state.py stats` 看还差哪些分片 |

若返回中 `lease.held` 为 true，说明另一个会话正在处理同一文档 → 见下方「并发」。

记下返回的 `run_dir`，后续所有脚本都用它。**不要自己拼接任何输出路径**——需要路径时用 `workspace.py resolve --run-dir <run> --kind <kind>`。

### 第 1 步：Pass -1　归一化

```
env_probe.py [--require-doc]          # 输入是 .doc 时加 --require-doc
convert_doc.py --run-dir <run>        # .docx 输入会自动跳过
unpack.py run --run-dir <run>         # 解包 + 去符号链接 + 合并相邻同格式 run
extract.py --run-dir <run>            # → paragraphs.jsonl + headings.json
```

`convert_doc.py` 退出码 3 = 环境无转换工具。**照原样把 stderr 的安装指引转达用户并终止**，不要尝试用文本提取绕过。

### 第 2 步：Pass 0　术语台账（必须整体完成后再进 Pass 1）

```
glossary_scan.py --run-dir <run>      # 脚本预筛候选术语，产出分批 payload
```

对 `work/glossary-candidates.json` 里的每个 batch，按 `references/prompts/pass0-glossary.md` 发起一次调用，把结果按行追加写入 `work/glossary-extracted.json`（`{"entries":[...]}`）。

```
import_glossary.py --run-dir <run> [--authoritative <表>] [--fallback <表>]
```

用户若提供了术语表就传入（CSV/XLSX/YAML/JSON/TXT 均可）。退出码 10 = 用户的表自身有矛盾，**转达具体矛盾并请用户修正，不要替他猜**。

> 有权威术语表时，命名不一致类问题可从「批注」升级为「修订」（L25/L26）。首轮通常没有；报告结尾会提示用户如何为下一轮准备。

### 第 3 步：分片

```
chunk.py --run-dir <run>
```

判据是 token 不是页数。返回 `single_pass: true` 即单片模式。`table_only` 类型的分片**不发起审查调用**，只发起事实抽取调用。

### 第 4 步：Pass 1　逐片审查与抽取（可并行）

每个子 Agent 循环：

```
workspace.py claim next --run-dir <run> --session <sid> --generation <n>
  → exhausted:true 就退出；否则拿到 chunk_id
```

对该片发起**两次独立调用**（绝不合并成一次）：

1. **审查**：`references/prompts/pass1-review.md` + `work/chunks/chunk-<id>.txt`
   → 逐行写入 `work/issues/issues-<id>.raw.jsonl`
2. **事实抽取**：`references/prompts/pass1-extract.md` + 同一分片
   → 写入 `work/facts/facts-<id>.json`

**每次调用前后都要续期 claim**（限流退避可能超过 TTL）：
`workspace.py claim renew --run-dir <run> --chunk <id> --session <sid>`

然后过脚本闸门：

```
verify_span.py --run-dir <run> --chunk <id>       # 闸门②③，产出 issues-<id>.jsonl
filter_neverflag.py --run-dir <run> --chunk <id>  # 幂等复查
metrics.py bump --run-dir <run> --pass pass1_review --input-tokens N --output-tokens M
```

输出无法解析成 JSONL 时**重试一次**，提示"上次输出无法解析"；二次失败：
`state.py mark --run-dir <run> --chunk <id> --status failed --error "json parse error"`

**子 Agent 只返回 `{路径, 计数, 状态}`，绝不返回问题正文。** 主 Agent 上下文只保留统计数字。

### 第 5 步：Pass 2　盲测 A/B 二次复核

把所有 `issues-*.jsonl` 合并为待复核集，按 `references/prompts/pass2-verify.md`
批量复核（一次 ≤10 组），结果写入 `work/issues-verified.jsonl`（在原记录上加
`"verify": {"result": "pass"|"drop"}`）。

```
import_decisions.py apply --run-dir <run>    # 审查记忆命中者降级为 report_only
```

### 第 6 步：Pass 3　冲突检测（纯脚本，独占）

```
ledger.py build --run-dir <run>
detect_conflicts.py --run-dir <run> --session <sid> --generation <n>
```

### 第 7 步：Pass 4　冲突裁定（独占）

对 `work/conflicts/conflicts-candidate.*.json` 中的每条候选，按
`references/prompts/pass4-adjudicate.md` 裁定，**逐条 append** 到
`work/conflicts-verified.jsonl`（append-only，中断后只需重跑剩余项）。

**每次调用前后执行** `state.py heartbeat --run-dir <run> --session <sid>` ——
此阶段脚本不在运行，没有自然心跳点，不续租会被误判为死亡。

### 第 8 步：回写与报告（严格单写）

```
apply_revisions.py plan  --run-dir <run> --session <sid> --generation <n>
apply_revisions.py apply --run-dir <run> --session <sid> --generation <n>
apply_comments.py  plan  --run-dir <run> --session <sid> --generation <n>
apply_comments.py  apply --run-dir <run> --session <sid> --generation <n>
validate_docx.py --run-dir <run>
```

`validate_docx.py` 退出码 8 = 四项检查有失败。**丢弃回写产物**，从
`work/normalized.docx` 重新解包再试一次；仍失败则只交付报告，并在给用户的说明中
明确写出失败的检查项。绝不交付未通过校验的 docx。

校验通过后打包与出报告：

```
unpack.py pack --run-dir <run>      # 不传 --output 即打包到交付目录，文件名自动
state.py rebuild --run-dir <run>
metrics.py collect --run-dir <run>
report.py --run-dir <run>           # 报告/xlsx/术语表同样直接写交付目录
```

全部完成后把交付目录里的文件路径报给用户。若用户要求清理中间件：

```
state.py stage --run-dir <run> --value completed
workspace.py clean-temp --run-dir <run>     # 只删临时目录，交付物不受影响
```

---

## CLI 契约表

所有脚本：可独立调用、幂等、不联网、不打印大对象。`--config <yaml>` 处处可选。

| 脚本 | 用途 | 关键输入 | 输出 |
|---|---|---|---|
| `workspace.py init` | 建工作目录、复制源文档、续跑判定 | `--source` | run_dir / action / lease |
| `workspace.py locate` | 只查已有文档目录 | `--source` | doc_dir / stage |
| `workspace.py resolve` | 取临时目录内的标准路径 | `--run-dir --kind` | path |
| `workspace.py deliver` | 取交付物最终路径 | `--run-dir [--kind]` | 交付目录 + 各产物路径 |
| `workspace.py clean-temp` | 删除本次 run 的临时目录 | `--run-dir` | 已删路径 + 保留的交付物 |
| `workspace.py lease` | 租约 status/acquire/takeover/heartbeat/verify/release | `--doc-dir --session` | owner |
| `workspace.py claim` | 分片 next/renew/release/status | `--run-dir --session` | chunk |
| `env_probe.py` | 环境探测 | `--require-doc` | converters / can_convert_doc |
| `convert_doc.py` | doc→docx（输出路径显式指定） | `--run-dir` | docx |
| `unpack.py run` | 解包 + 合并 run + 记录 D9 基线 | `--run-dir` | 合并统计 |
| `unpack.py pack` | 重新打包；默认写交付目录 | `--run-dir [--output]` | docx |
| `extract.py` | 段落抽取 + 标题树 + 页码估算 | `--run-dir` | paragraphs / headings |
| `glossary_scan.py` | 候选术语预筛 + 概念族聚类 | `--run-dir` | candidates / batches |
| `import_glossary.py` | 术语表导入 + 自检 + 三层合并 | `--run-dir --authoritative --fallback` | entries / layers |
| `chunk.py` | 分片（按 token） | `--run-dir` | chunks / single_pass |
| `verify_span.py` | 闸门②③ | `--run-dir --chunk` | 各闸门丢弃计数 |
| `filter_neverflag.py` | 不改清单硬过滤 | `--run-dir --chunk\|--all` | dropped / by_rule |
| `ledger.py build\|rebuild\|stats` | 台账 SQLite 索引 | `--run-dir` | stats |
| `detect_conflicts.py` | L01–L32 冲突检测 | `--run-dir [--rules]` | total / by_rule |
| `apply_revisions.py plan\|apply` | 修订回写（两段式） | `--run-dir` | patches / applied |
| `apply_comments.py plan\|apply` | 批注回写（六文件联动） | `--run-dir` | comments / anchored |
| `validate_docx.py` | 回写后四项校验 | `--run-dir` | pass / failed |
| `state.py rebuild\|stats\|stage\|mark\|heartbeat\|doctor` | 状态与续跑 | `--run-dir` | stats / stage |
| `metrics.py bump\|collect\|show` | 闸门丢弃率统计 | `--run-dir` | gates / gate_rates |
| `import_decisions.py import\|apply\|show` | 审查记忆 | `--run-dir` | hits |
| `report.py` | report.md + issues.xlsx | `--run-dir` | 路径 / 计数 |
| `typo_scan.py scan\|merge` | 错别字候选（默认关闭） | `--run-dir` | candidates |

**退出码**：0 成功 / 1 失败 / 2 参数错 / 3 环境缺失 / 4 写路径越界 / 5 磁盘不足 /
6 工作目录不合法 / 7 租约被占 / 8 校验失败 / 9 令牌失效 / 10 输入不可解析。

---

## 决策点

**单片还是分片** —— 不用你判断，`chunk.py` 按 token 计量后给出 `single_pass`。页数不是判据：同样 22k tokens，紧排版约 30 页，稀排版可达 60 页。

**strictness 怎么选** —— 默认 `balanced`（A+B 类）。用户明确要"只报确定的错"用 `conservative`（仅 A 类）；要"尽量多提示"用 `thorough`（加 C 类，但 C 类永不入文档）。
**`apply_threshold` 恒为 `conservative`，用户要求放宽也不行**——告诉用户报告可以更宽，但落笔门槛不放。

**修订还是批注** —— 不用你判断，脚本已决定：唯一确定的正确替换 → 修订；无唯一答案（歧义、指代不明、逻辑冲突）→ 批注；仅风格倾向 → 只进报告。

**批注和报告里怎么称呼问题** —— 脚本已经把规则号译成了中文（「前后数值不一致」「的/地/得误用」），
规则号只作为末尾的可追溯标记。**转述给用户时也用中文说法，不要念规则号**——
评审人看到「L06」不知道是什么。

**遇到 exit 9（NOT_OWNER）** —— 不重试、不降级。转达为人话：本会话写入权限已失效，该文档已被另一个会话接管；已完成的分片结果仍然有效并已保留；建议切换到另一个会话查看进度，或选择「独立重跑」。

---

## 并发：同一文档被多个会话处理

`workspace.py init` 报告 `lease.held` 时**禁止静默失败或静默接管**。呈现现状（运行 ID、当前阶段、已完成片数、**真实的最后活动间隔**），按对方所处阶段给出选项：

| 对方阶段 | 可选项 |
|---|---|
| Pass 0 | 等待 / 独立重跑 |
| Pass 1–2 | **加入协作（推荐）** / 接管 / 独立重跑 |
| Pass 3–4 | 等待并轮询 / 接管 / 独立重跑 |
| 回写中 | 等待 / 独立重跑 —— **不提供接管** |

加入协作还需同时满足：配置快照哈希一致、技能版本一致、工作目录在本地文件系统（`workspace.py fscheck`）、Pass 0 已完成。任一不满足只能独立重跑。

主 Agent 主动并行时用同一套 claim 机制：启动 `parallelism`（默认 5，上限建议 8）个子 Agent 跑同一个 claim 循环即可，**主 Agent 不做任何分配决策**。

---

## 中等能力模型适配（写 prompt 时必须遵守）

1. 一次调用只做一件事。审查与抽取分两次调用，绝不合并。
2. 输出固定 JSONL，一行一条，无外层包裹、无代码围栏、无前言后语。
3. `evidence` ≤25 字。理由越长模型越容易自我说服，误报率越高。
4. 不让模型做算术：数值比较、计数、百分比合计、编号连续性全部交给脚本。
5. 不问开放问题（"这个问题重要吗""整体逻辑是否通顺""文档质量如何"）。
6. 反例优先：`never-flag.md` 的反例数量应 ≥ 正例。
7. 温度设 0 或平台最低值。
8. 类别、严重度、动作、复核结论全部是封闭枚举，只能选不能造。
9. 每片上限 20 条。无上限时模型会持续"发现"问题以显得尽职。
10. 每次调用无状态、自包含，不做多轮对话。

**禁止让模型输出 `confidence` 分数并据此卡阈值。** 言语化置信度存在系统性过度自信与分数饱和，不存在有效阈值点；且已作出判断的 Agent 倾向为自身判断辩护。质量由四道工程闸门保证，不由模型自评保证。

---

## 按需查阅

| 什么时候 | 读哪个 |
|---|---|
| 判定某问题属于哪一类、某类的判定要件 | `references/taxonomy.md` |
| 拿不准该不该上报、需要反例 | `references/never-flag.md` |
| 想知道某条 L 规则怎么判、动作是什么 | `references/logic-rules.md` |
| 需要字段定义、schema | `references/schemas.md` |
| 回写出问题、要理解 OOXML 结构 | `references/ooxml.md` |
| 发起某次 LLM 调用 | `references/prompts/<对应文件>.md` |
