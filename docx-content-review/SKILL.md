---
name: docx-content-review
description: 对 Word 文档（.doc/.docx）做错别字、语病语义与全文逻辑性审查，输出审查报告并回写修订与批注。当用户要求审查、校对、查错、找错别字、通读、把关、挑毛病，或要求检查前后一致性、前后矛盾、上下文冲突、术语是否统一、数据是否对得上、语病与错别字、表达是否有歧义时使用。也可按用户自备的规则包审查特定场景的描述范式（某类内容必须写清哪几项）。也用于合同、方案、规划、可研、评审材料、设计说明书等长文档的交叉一致性核对。不做格式排版审查，不修改任何样式。
platform: XAgent
version: 1.1.0
---

# docx-content-review

对 Word 文档执行四类审查，交付「审查报告 + 带修订与批注的 Word 文档」：

| 审查什么 | 由谁产出 | 类别 |
|---|---|---|
| 错别字 | 词表出候选 + 模型二选一裁定（支线） | A1 |
| 语病与语义（含语句不完整、歧义） | Pass 1 主审查 | A2–A8 / B1–B5 |
| 前后描述与数据的一致性 | 事实台账 + 脚本比对（`detect_conflicts.py`） | L06–L24、L27–L32 |
| 术语一致性（命名、缩略语、权威表写法） | 同上，**默认关闭**（`logic.term_rules`） | L01–L05、L25/L26 |
| 特定场景的描述范式 | 用户的规则包 + 模型逐要件裁定（支线，默认关） | P1 |

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
| **交付目录** | **只有审查版 docx** | Agent 当前工作目录 | 不可，这是交付物 |
| **临时目录** | 中间件 + 报告/xlsx/metrics/术语表 | **同交付目录**，落在 `<工作目录>/docx-review/` 之下 | 可整体删除 |

**交付物只有一个：带修订与批注的 docx。** 跑完长这样：

```
<工作目录>/
├── 系统设计说明书审查版_20260806_005525.docx      ← 唯一交付物
└── docx-review/                                   ← 可整体删除
    └── 系统设计说明书-2faa47ba04ec/run-.../
        ├── output/                                ← 报告等四项在这里
        │   ├── 系统设计说明书审查版_20260806_005525.report.md
        │   ├── 系统设计说明书审查版_20260806_005525.issues.xlsx
        │   ├── 系统设计说明书审查版_20260806_005525.metrics.json
        │   └── 系统设计说明书审查版_20260806_005525.glossary.json
        └── work/                                  ← 中间件
```

报告等四项**照常生成**，只是不堆到用户的工作目录——它们各有用途：

| 产物 | 用途 |
|---|---|
| `report.md` | **你据此向用户口头汇报审查结果**；用户要文件时再把路径给他 |
| `issues.xlsx` | 用户标注 accept/ignore 后回灌审查记忆（`import_decisions.py`） |
| `metrics.json` | 四道闸门的丢弃率，调 prompt 的唯一依据 |
| `glossary.json` | 人工修订后作为下一轮的 `--authoritative` 输入 |

用户明确要哪一个，就把 `output.deliver_report` / `deliver_issues_xlsx` /
`deliver_metrics` / `deliver_glossary` 中对应项改成 `true`，或直接把文件复制过去。

文件名统一为 **`<原文件名>审查版_<时间戳>.<后缀>`**（时间戳 `20260806_005525`，
全流程共用 init 时生成的同一个）。取路径用 `workspace.py deliver --run-dir <run>`
（返回 `paths` = 交付物，`artifacts` = 全部产物及其去向），**不要自己拼**。

只有在工作目录位于网络盘、或需要把大体积中间件挪到别的盘时，才用 `--temp-dir` 指定别处。

返回 `action` 决定下一步：

| action | 含义 | 你要做的 |
|---|---|---|
| `created` | 新建了 run | 继续第 1 步 |
| `resume_available` | 该文档有未完成的 run | **必须问用户**：续跑还是新建，再用 `--resume reuse` 或 `--resume new` 重新调用 |
| `resumed` | 已挂到旧 run | 跳到 `state.py stats` 看还差哪些分片 |

若返回中 `lease.held` 为 true，说明另一个会话正在处理同一文档 → 见下方「并发」。

**拿到 run 之后立刻取独占租约**（`lease.held` 为 false 时直接取；为 true 时先按「并发」一节问过用户）：

```
workspace.py lease acquire --doc-dir <doc_dir> --session <sid> --runid <runid> --stage pass-1
```

`<sid>` 由你自取，本次运行内保持不变；返回的 `owner.generation` 就是后续所有
`--generation <n>` 要填的值，一并记下。

**这一步不能省。** Pass 3、Pass 4 与回写阶段的脚本都会校验租约，没有租约时一律以
退出码 9 终止，而那条错误信息说的是「已被另一个会话接管」——与实情正好相反，
会让你把一次正常运行误报成并发冲突。

收尾（`state.py stage --value completed` 之后）用 `workspace.py lease release
--doc-dir <doc_dir> --session <sid> --generation <n>` 释放。

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

**默认关闭**（`logic.term_rules: false`）。关闭时 `glossary_scan.py` 直接返回
`term_rules: false`，**不要发起任何调用**，直接进第 3 步；术语类规则
（L01–L05 命名与定义、L25/L26 权威表写法）也不会跑，报告的「本次未覆盖范围」
会写明这一维度没查。用户要这一维度时把 `logic.term_rules` 改成 `true` 重跑。

```
glossary_scan.py --run-dir <run>      # 脚本预筛候选术语，产出分批 payload
```

对 `work/glossary-candidates.json` 里的每个 batch，按 `references/prompts/pass0-glossary.md` 发起一次调用，把结果按行追加写入 `work/glossary-extracted.json`（`{"entries":[...]}`）。

```
import_glossary.py --run-dir <run> [--authoritative <表>] [--fallback <表>]
```

**这一步不受开关影响，照常执行**：用户自备的术语表要导入（CSV/XLSX/YAML/JSON/TXT 均可），
不改清单 N7 靠它避免把用户的专有写法当语病改掉。退出码 10 = 用户的表自身有矛盾，
**转达具体矛盾并请用户修正，不要替他猜**。

> 有权威术语表且 `term_rules` 打开时，命名不一致类问题可从「批注」升级为「修订」（L25/L26）。

### 第 3 步：分片与题面准备

```
chunk.py --run-dir <run>                                  # 切片
typo_scan.py scan --run-dir <run>                         # 错别字候选，整篇一次
scan_patterns.py scan --run-dir <run> [--patterns <包>]   # 范式候选（默认关，跳过）
prompt_pack.py build --run-dir <run>                      # 渲染每次调用的自包含 prompt
```

判据是 token 不是页数。`chunk.py` 返回 `single_pass: true` 即单片模式。
`table_only` 类型的分片**不发起审查调用**，只发起事实抽取调用。

**片数就是工作量**：一份 800 页的中文文档在默认配置下约 20 片。明显更多说明
`max_text_tokens` 被调小了，或 `split_point_fill_ratio` 太低（标题一多就断）。

**改了 `chunking` 下的任何参数必须重跑这一步**，并看 `stale_chunks_cleared`：
切法一变，旧的 `issues-<id>` / `facts-<id>` 就与同号的新分片对不上了，脚本会把它们
连同已渲染的 prompt 一起作废。这个数不为 0 是正常的——那些片会重新审查。

**`prompt_pack.py` 是这一步的关键。** 它把每一次调用要用的东西——类型体系、
不改清单、facts schema、术语表摘要、分片正文、候选题面——**预先拼成一个文件**，
一个单元一个。子 Agent 因此只需要「读一个文件 → 作答 → 写一个文件」，
既不必知道技能目录在哪，也没有机会顺手把 `references/` 下的大文件读进上下文。

返回里的 `max_chars` 是**单个子 Agent 的输入规模上界**，值得看一眼：
默认配置下约 2.3 万字符（固定开销约 8 千 + 分片正文）。子 Agent 的上下文窗口偏小时，
调低 `chunking.max_text_tokens` 重跑第 3 步即可——这个数会同比例下降。

### 第 4 步：Pass 1　三波并行

**一个子 Agent 只做一件事：一次调用，一份产物。** 不要让它领一整片去做三件事——
那样它的上下文要同时装下类型体系、facts schema、整片正文、错别字候选和三份产物，
在窗口偏小的子 Agent 上会直接溢出；而且**环境抖一下，这一片的三次调用全白跑**。

| 波次 | `--stage` | 单元 | 读 | 写 |
|---|---|---|---|---|
| 一 | `review` | 每个非纯表格分片 | `work/prompts/review-<片>.md` | `issues-<片>.raw.jsonl` |
| 二 | `extract` | 每个分片 | `work/prompts/extract-<片>.md` | `facts-<片>.json` |
| 三 | `typo` / `pattern` | 每片每批候选 | `work/prompts/typo-<片>-<批>.md` | `typos-<片>.<批>.verdicts.jsonl` |

三波之间没有依赖，先后顺序随意，也可以混在一起派——分开只是为了让每一波的
子 Agent 指令完全一致、便于统计。**波内当然并行。**

**主 Agent 每一波的循环**：

```
workspace.py claim next --run-dir <run> --stage <阶段> --session <sid> --generation <n>
  → exhausted:true 就换下一波；否则拿到 {unit, prompt, output}
```

把 `prompt` 与 `output` 两个路径原样交给子 Agent，指令就一句：

> 读 `<prompt>` 这个文件，照它写的做，把结果写到它指定的 `<output>`，
> 然后返回 `{"unit":…, "lines":N}`。**不要读别的文件，不要把正文内容带回来。**

每轮派 `parallelism` 个，全部返回后再派下一轮，直到 `exhausted`。
**主 Agent 上下文里只留统计数字。**

**收口（纯脚本，主 Agent 一次跑完，不要按片各跑一次）**：

```
verify_span.py --run-dir <run> --all --channel main && \
filter_neverflag.py --run-dir <run> --all --channel main && \
typo_scan.py merge --run-dir <run> && \
verify_span.py --run-dir <run> --all --channel typos && \
filter_neverflag.py --run-dir <run> --all --channel typos
```

（范式支线开启时，把最后三条换成 `scan_patterns.py merge` + `--channel patterns` 的两条。）

`metrics.py bump --run-dir <run> --pass pass1_review`（以及 `--pass typo` / `--pass pattern`）
每波记一次即可。`--input-tokens` / `--output-tokens` 可选：平台报得出就带上，
**不要为了填这两个数额外发一次工具调用**。

#### 子 Agent 失败了怎么办（会失败，要按会失败来设计）

产物存在性即状态，所以**重派是安全的**：已完成的单元不会被重做，失败的单元
下一轮会被重新领走。三件事必须做：

1. **回收崩掉的 claim。** 子 Agent 中途死掉时，它占着的单元会一直锁到 TTL
   （`chunk_claim_minutes`，默认 60 分钟）到期——整轮都在等一个不会回来的活。
   每波结束后跑一次：
   `workspace.py claim reclaim --run-dir <run> --stage <阶段> --session <sid>`
   （带 `--session` 只回收本会话自己的；别人的正在跑，不能动。）
2. **看 `claim status`。** `workspace.py claim status --run-dir <run> --stage <阶段>`
   给出 total / done / claimed / pending，以及前 20 个 pending 单元。
   `pending` 不再下降就是卡住了，不要空转。
3. **同一单元连续失败 3 次就放过它**（`retry.max_attempts_per_chunk`）：
   `state.py mark --run-dir <run> --chunk <片> --status failed --error "<原因>"`，
   继续下一波。报告会写明哪些片没审到——**不要因为一片失败就整轮重来**。

**半写的产物不会被当成完成。** 子 Agent 被杀在写盘中途时，会留下一个存在但
截断的文件。`claim status` 把这类单元单独报成 `corrupt`，它们照常会被重新派出去；
收口时 `--all` 只作废那一片并在 `unparsable` 里报数，**不会因为一个坏文件
让整轮过闸退出**。想一次清干净就跑 `state.py doctor --run-dir <run> --fix`。

输出无法解析成 JSONL 时**重试一次**，提示"上次输出无法解析"；二次失败按上面第 3 条处理。

子 Agent 频繁失败（而不是偶发）时，先降 `concurrency.parallelism`，
再考虑调低 `chunking.max_text_tokens` 让单元变小——**不要提高并发去"赶进度"**。

### 第 4.5 步：两条支线在做什么（错别字 / 范式）

这两类问题都**不能靠主审查顺带发现**，各走独立通道：脚本出候选 → 模型答封闭题 →
过同样的两道闸门。两条支线各有独立配额，不占用主审查每片的条数上限。

候选扫描与题面渲染都在第 3 步做完了，第 4 步的第三波只是去答题。
`typo_scan.py scan` 的返回里带每片的候选数，**候选为 0 的片不会生成单元**，
不需要你判断。

**错别字**（`typo_check.enabled` 默认开）：模型对错别字有鲁棒性，让它自己找先天不利，
所以退化成二选一——A 原字正确 / B 应改 / C 都不对。裁定按 `tid` 回填，
`typo_scan.py merge` 只采纳 B。

**范式**（`pattern_review.enabled` 默认关，无规则包时自动跳过）：每个要件只答 Y/N/U，
U 按「要件齐备」处理。

用户要求"重点查错别字"时，把 `typo_check.max_typos_per_chunk` 调高即可；
**不要去放宽闸门②的 A1 阈值**——召回靠词表，不靠放松校验。

用户提到"某类内容必须写清哪几项"（风险要写影响与应对、接口要写入参出参异常…），
那是范式审查：开 `pattern_review.enabled`，并按 `references/patterns.md` 写规则包。
**首轮通常没有规则包**，此时如实告诉用户这一类暂不覆盖，不要临时编规则。

### 第 5 步：Pass 2　盲测 A/B 二次复核

```
verify_pass2.py build --run-dir <run>            # 拼装待复核集（含位置随机化）
```

脚本已把三个通道的过闸产物合并、按 manifest 的 `ab_seed` 逐条定好 A/B 位置，
并**按批落盘**：`work/verify/pass2-primary.vNN.json`，每批 ≤10 组
（返回里给 `batches` 与文件名格式）。

**各批之间没有依赖，这一步同样并行**：派 `parallelism` 个子 Agent，
每人领一个批次文件，按 `references/prompts/pass2-verify.md` 复核，
把 `{"id":"…","answer":"A"}` 整块写入**同名的** `pass2-primary.vNN.verdicts.jsonl`。
各写各的文件——多个子 Agent 往同一个文件 append，中断处会互相截断。

**不要用合并文件 `pass2-primary.json` 发起调用**，也不要把它读进主 Agent：
一份长文档能出几百条待复核项，整份进了上下文，此后每一轮工具往返都要重算它一遍。
它只留作备查。

**同目录下的 `.key.json` 不要读、更不要带进调用。** 它记的是原文在哪一侧，
是 `merge` 应用判定表用的。看了它，盲测就只剩名义。

```
verify_pass2.py merge --run-dir <run>            # 判定表 → work/issues-verified.jsonl
```

`merge` 会把所有分批裁定文件一起读进来（`verdict_files` 报出读到几份）。
判定表由脚本执行：选中原文所在项 = 通过；选中建议侧 / 两者都没有 / 两者都有 /
答案无法解析 / 缺裁定 = 一律淘汰。B 类的封闭单问里 UNSURE 按 NO 处理。

**漏跑一批 = 那批整批淘汰**（`missing_verdict` 会报数）。看到这个数不为 0，
补跑缺的那几批再 merge，不要放着不管——它是"少了一批复核"，不是"这批没问题"。

> 需要测模型的位置偏好时（M2 验收项）：`build --arrangement mirror` 再复核一轮，
> 然后 `verify_pass2.py consistency --run-dir <run>` 给出两种排列的判定一致率。
> **低于 0.9 说明模型答的是"哪一侧"而不是"哪一个有错"，要换 prompt，不要调阈值。**
> 常规运行不需要跑镜像排列。

```
import_decisions.py apply --run-dir <run>    # 审查记忆命中者降级为 report_only
```

### 第 6 步：Pass 3　冲突检测（纯脚本，独占）

```
ledger.py build --run-dir <run>
detect_conflicts.py --run-dir <run> --session <sid> --generation <n>
```

### 第 7 步：Pass 4　冲突裁定（独占，可并行）

```
adjudicate_pass4.py build --run-dir <run>        # 候选 → 分批题面
```

脚本把全部候选拼成题面并按 `logic.adjudicate_batch_size`（默认 10 组）分批落到
`work/conflicts/batches/adjudicate-bNN.json`。**不要自己去读
`conflicts-candidate.*.json`**——那是几百条带原文的记录，读进主 Agent 之后，
此后每一轮工具往返都要把它重算一遍。

各批无依赖，派子 Agent 并行：每人一个批次文件，按
`references/prompts/pass4-adjudicate.md` 裁定，把
`{"conflict_id":"L06-0002","verdict":"CONFLICT","note":"…"}` 整块写入
`work/conflicts/verdicts/adjudicate-bNN.verdicts.jsonl`。

```
adjudicate_pass4.py collect --run-dir <run>      # 归并 → work/conflicts-verified.jsonl
```

**看 `collect` 返回的 `missing` 与 `invalid_lines`。** 未裁定的候选一律不进交付物
（未裁定 = 不确定 = 无问题），所以漏答的表现是「这条冲突凭空消失」，
报告里也看不出来。不为 0 就补跑对应批次再 collect（collect 幂等，已归并的不会丢）。

**每批调用前后执行一次** `state.py heartbeat --run-dir <run> --session <sid>` ——
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
report.py --run-dir <run>           # 报告等四项写 run/output/，不进交付目录
```

**收尾**：读 `run/output/` 里的 report.md，把审查结果**当面讲给用户**——
问题总数、按严重度分档、必须人工确认的严重级冲突有哪几条。
然后只报一个文件路径：交付目录里的审查版 docx。
其余四项只在用户问起时再给路径。

若用户要求清理中间件（**报告等四项会一并删除，先确认用户不需要**）：

```
state.py stage --run-dir <run> --value completed
workspace.py clean-temp --run-dir <run>
```

---

## CLI 契约表

所有脚本：可独立调用、幂等、不联网、不打印大对象。`--config <yaml>` 处处可选。

| 脚本 | 用途 | 关键输入 | 输出 |
|---|---|---|---|
| `workspace.py init` | 建工作目录、复制源文档、续跑判定 | `--source` | run_dir / action / lease |
| `workspace.py locate` | 只查已有文档目录 | `--source` | doc_dir / stage |
| `workspace.py resolve` | 取临时目录内的标准路径 | `--run-dir --kind` | path |
| `workspace.py deliver` | 取产物路径与去向 | `--run-dir [--kind]` | paths（交付物）+ artifacts（全部及去向） |
| `workspace.py clean-temp` | 删除本次 run 的临时目录 | `--run-dir` | 已删路径 + 保留的交付物 |
| `workspace.py lease` | 租约 status/acquire/takeover/heartbeat/verify/release | `--doc-dir --session` | owner |
| `workspace.py claim` | 单元 next/renew/release/status/reclaim（`--stage` 分波） | `--run-dir --session [--stage]` | unit / prompt / output |
| `env_probe.py` | 环境探测 | `--require-doc` | converters / can_convert_doc |
| `convert_doc.py` | doc→docx（输出路径显式指定） | `--run-dir` | docx |
| `unpack.py run` | 解包 + 合并 run + 记录 D9 基线 | `--run-dir` | 合并统计 |
| `unpack.py pack` | 重新打包；默认写交付目录 | `--run-dir [--output]` | 审查版 docx |
| `extract.py` | 段落抽取 + 标题树 + 页码估算 | `--run-dir` | paragraphs / headings |
| `glossary_scan.py` | 候选术语预筛 + 概念族聚类 | `--run-dir` | candidates / batches |
| `import_glossary.py` | 术语表导入 + 自检 + 三层合并 | `--run-dir --authoritative --fallback` | entries / layers |
| `chunk.py` | 分片（按 token） | `--run-dir` | chunks / single_pass / stale_chunks_cleared |
| `prompt_pack.py build\|list` | 渲染每次调用的自包含 prompt（一个单元一个文件） | `--run-dir [--stage --chunk]` | written / max_chars |
| `verify_span.py` | 闸门②③ | `--run-dir --chunk [--in --out --cap]` | 各闸门丢弃计数 |
| `verify_pass2.py build\|merge\|consistency` | 闸门④盲测 A/B 脚手架（build 按批落盘，可并行复核） | `--run-dir [--arrangement]` | items / batches / pass / drop / 一致率 |
| `filter_neverflag.py` | 不改清单硬过滤 | `--run-dir --chunk\|--all [--channel --file]` | dropped / by_rule |
| `ledger.py build\|rebuild\|stats` | 台账 SQLite 索引 | `--run-dir` | stats |
| `detect_conflicts.py` | L01–L32 冲突检测 | `--run-dir [--rules]` | total / by_rule |
| `adjudicate_pass4.py build\|collect\|status` | Pass 4 裁定脚手架（分批题面 + 归并 + 漏答报数） | `--run-dir` | batches / verdicts / missing |
| `apply_revisions.py plan\|apply` | 修订回写（两段式） | `--run-dir` | patches / applied |
| `apply_comments.py plan\|apply` | 批注回写（六文件联动） | `--run-dir` | comments / anchored |
| `validate_docx.py` | 回写后四项校验 | `--run-dir` | pass / failed |
| `state.py rebuild\|stats\|stage\|mark\|heartbeat\|doctor` | 状态与续跑 | `--run-dir` | stats / stage |
| `metrics.py bump\|collect\|show` | 闸门丢弃率统计 | `--run-dir` | gates / gate_rates |
| `import_decisions.py import\|apply\|show` | 审查记忆 | `--run-dir` | hits |
| `report.py` | report.md + issues.xlsx（写 run/output/） | `--run-dir` | 路径 / 计数 / artifacts |
| `typo_scan.py scan\|merge` | 错别字候选（支线，默认开） | `--run-dir [--chunk]` | candidates |
| `scan_patterns.py scan\|merge\|lint` | 范式场景定位与裁定合并（支线，默认关） | `--run-dir [--chunk --patterns]` | rules / candidates |

**退出码**：0 成功 / 1 失败 / 2 参数错 / 3 环境缺失 / 4 写路径越界 / 5 磁盘不足 /
6 工作目录不合法 / 7 租约被占 / 8 校验失败 / 9 令牌失效 / 10 输入不可解析。

---

## 决策点

**单片还是分片** —— 不用你判断，`chunk.py` 按 token 计量后给出 `single_pass`。页数不是判据：同样 22k tokens，紧排版约 30 页，稀排版可达 60 页。

**strictness 怎么选** —— 默认 `balanced`（A+B 类）。用户明确要"只报确定的错"用 `conservative`（仅 A 类）；要"尽量多提示"用 `thorough`（加 C 类，但 C 类永不入文档）。
**`apply_threshold` 恒为 `conservative`，用户要求放宽也不行**——告诉用户报告可以更宽，但落笔门槛不放。

**用户要查术语怎么办** —— 术语类规则（L01–L05、L25/L26）默认关闭。
用户明确提到术语、名称、缩略语要不要统一，就把 `logic.term_rules` 改成 `true`
再跑，并照常做第 2 步的 Pass 0；**不要在关闭状态下含糊带过**，
如实说这一维度默认没查、问他要不要开。

**修订还是批注** —— 不用你判断，脚本已决定：唯一确定的正确替换 → 修订；无唯一答案（歧义、指代不明、逻辑冲突、范式要件缺失）→ 批注；仅风格倾向 → 只进报告。
**修订不是"批注的替代"**：每一处落笔的修订同时带一条说明改动理由的批注（`kind: revision`），
否则评审人只能整批接受或整批拒绝。转述给用户时按"修订处数"讲，不要把这些批注算成额外的问题。

**批注和报告里怎么称呼问题** —— 脚本已经把规则号译成了中文（「前后数值不一致」「的/地/得误用」），
规则号只作为末尾的可追溯标记。**转述给用户时也用中文说法，不要念规则号**——
评审人看到「L06」不知道是什么。

**遇到 exit 9（NOT_OWNER）** —— 先确认第 0 步的 `lease acquire` 到底跑过没有：
**没取租约与被别人接管，报的是同一个退出码和同一句话**。用
`workspace.py lease status --doc-dir <doc_dir>` 看一眼——`held: false` 就是自己没取，
补取即可，不要转达给用户。

确实被接管时（`held: true` 且 owner 不是本会话）不重试、不降级。转达为人话：本会话写入权限已失效，该文档已被另一个会话接管；已完成的分片结果仍然有效并已保留；建议切换到另一个会话查看进度，或选择「独立重跑」。

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

主 Agent 主动并行时用同一套 claim 机制：每轮启动 `parallelism`（默认 5，上限建议 8）个子 Agent，
**每个只领一个单元（一次调用、一份产物）、做完即退**，本轮全部返回后再派下一轮，
**主 Agent 不做任何分配决策**。

不要让子 Agent 循环领活，也不要让它一个人做完一片的三次调用：它的上下文会随做过的
单元累积，而每一次工具往返都要重算当下的全部上下文——连做的开销是平方级的，
窗口偏小时还会直接溢出。第 5 步与第 7 步同理，那两步也是按批分文件、各批独立。

**并发不是越高越好。** 子 Agent 频繁失败时先降 `parallelism`：失败的单元要重派，
重派的开销比串行还大。

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
9. 每片有条数上限（`max_issues_per_chunk`，默认 40）。无上限时模型会持续"发现"问题以显得尽职。
   片变大时这个上限要同步变大——它是截断源，不是「每片就这么多问题」。
10. 每次调用无状态、自包含，不做多轮对话。
11. **一个子 Agent = 一次调用 = 一份产物。** 前一条说的"无状态"对 API 调用天然成立，
    但子 Agent 是一个持续的会话：让它连做几件事，前面读过的正文、写过的产物会全程
    留在它的上下文里，既撑爆窗口，也让每一轮工具往返都要重算一遍。
    调用要用的东西由 `prompt_pack.py` 预先拼成一个文件，**子 Agent 不读 `references/`**。

**禁止让模型输出 `confidence` 分数并据此卡阈值。** 言语化置信度存在系统性过度自信与分数饱和，不存在有效阈值点；且已作出判断的 Agent 倾向为自身判断辩护。质量由四道工程闸门保证，不由模型自评保证。

---

## 按需查阅

| 什么时候 | 读哪个 |
|---|---|
| 判定某问题属于哪一类、某类的判定要件 | `references/taxonomy.md` |
| 拿不准该不该上报、需要反例 | `references/never-flag.md` |
| 要写或改一条范式规则、范式通道排障 | `references/patterns.md` |
| 想知道某条 L 规则怎么判、动作是什么 | `references/logic-rules.md` |
| 需要字段定义、schema | `references/schemas.md` |
| 回写出问题、要理解 OOXML 结构 | `references/ooxml.md` |
| 发起某次 LLM 调用 | `references/prompts/<对应文件>.md` |
