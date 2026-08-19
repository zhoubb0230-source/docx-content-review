# CLAUDE.md —— 开发者视角

**本文件只在开发/维护本仓时加载，运行期永不加载。**
**硬约束：`docx-content-review/SKILL.md` 及 `references/` 不得引用本文件的任何内容。**
技能包必须自包含——它会被单独分发，届时本文件不在。

---

## 项目定位

`docx-content-review/` 是一个交付给 XAgent 的技能包：对 Word 文档（0–3000 页）做
**语病 / 语义 / 全文逻辑性**审查，输出报告并回写修订与批注。

完整规格见 `docs/SPEC.md`（v1.1）。**规格是权威**；本文件只讲"代码在哪、怎么改"。

### 非目标（改代码前先确认没有越界）

不做格式排版审查；**不修改任何样式**；不做事实核查；不做润色改写；不处理 PDF/WPS。

### 三条底线（任何改动都不得削弱）

1. 源文档只读——第一个动作是复制，此后所有路径指向副本。
2. 不改任何样式（D9）——只改文本，且只以修订/批注形式改。
3. 不确定即无问题——所有判定是封闭选择题，UNSURE 按否定处理。

优先级排序：**源文档完整性 > 样式不变性 > 定位准确率 > 回写安全性 > 误报率 >
错别字召回率 > 逻辑检出率 > 语病召回率。**
指标冲突时按此序取舍。任何为提升后几项而放松前四项的方案一律否决。

错别字召回率排在逻辑与语病之前，是因为它的性质不同：**它靠词表（确定性资产），
扩表不抬高误报率**；而语病召回靠模型，提召回必然抬高误报。
因此"扩 `common-typos.txt`"永远可做，"放宽闸门②的 A1 阈值"永远不可做。

---

## 目录地图：要改 X，去改哪个文件

| 要改什么 | 改哪里 | 同步改 |
|---|---|---|
| 输出目录解析、隔离规则、租约、claim、run 配置改动（`reconfigure`） | `scripts/workspace.py` | `SKILL.md` 并发一节 |
| 新增一个产物路径 | `scripts/workspace.py` 的 `KINDS` | —（禁止各脚本自行拼路径） |
| 环境探测、doc→docx | `scripts/env_probe.py` / `convert_doc.py` | — |
| run 合并规则、解包安全 | `scripts/unpack.py` | `references/ooxml.md` |
| 段落 ID、标题树、页码估算、代码块识别 | `scripts/extract.py` | `references/schemas.md` |
| token 计量、分片策略 | `scripts/tokenizer.py` / `chunk.py` | `references/schemas.md` |
| **新增/修改一个 A/B/C 类别** | `scripts/verify_span.py` 的 `edit_gate` | `references/taxonomy.md` + `prompts/pass1-review.md` + `tests/fixtures` |
| **新增一条不改清单规则** | `scripts/filter_neverflag.py` 的 `check` | `references/never-flag.md` + 负样本 fixture |
| 位置类规则（N9/N10/N12）的豁免边界 | `filter_neverflag.py` 的 `exempt_regions` | **必须往 `tests/fixtures/neverflag-traps.json` 补正反两向的句子** |
| 闸门④盲测 A/B（拼装、判定表、一致率） | `scripts/verify_pass2.py` | `prompts/pass2-verify.md`；**改动必须重跑泄题检查（含分批 payload）** |
| Pass 4 裁定的分批题面与归并、漏答报数 | `scripts/adjudicate_pass4.py` | `prompts/pass4-adjudicate.md` + `references/schemas.md` |
| 分片预算、token 系数、重切片后作废旧产物 | `scripts/chunk.py` | `assets/config.default.yaml` 的 `chunking`；**改了预算必须重跑 chunk.py** |
| 子 Agent 看到的 prompt（自包含渲染、派活前体检） | `scripts/prompt_pack.py` | 对应的 `prompts/*.md`；**改了模板要重跑 prompt_pack build** |
| 阶段化单元（claim 的粒度、崩溃回收、成组派活） | `scripts/workspace.py` 的 `stage_units` / `reclaim_stage` / `next_pending_units` | `SKILL.md` 第 4 步 + 回归第 23、26 节 |
| **某个类别进不进交付物** | `verify_pass2.py` 的 `KEEP_SCOPE`（**不是** `REVIEW_SCOPE`） | `prompts/pass2-verify.md` 触发范围表 |
| **新增一条 L 规则** | `scripts/detect_conflicts.py`（加 `RULES` 项 + `r_LNN` 函数） | `references/logic-rules.md` + `logic-injection.facts.json` + 回归断言 |
| 事实台账字段 | `scripts/ledger.py` 的 `MAPPING` | `references/schemas.md` + `prompts/pass1-extract.md` |
| 术语表格式、合并规则 | `scripts/import_glossary.py` | `references/schemas.md` |
| 候选术语预筛、概念族聚类 | `scripts/glossary_scan.py` | `prompts/pass0-glossary.md` |
| OOXML 底层（run 拆分、rPr 拷贝、修订视图） | `scripts/ooxml.py` | `references/ooxml.md` |
| 修订回写 | `scripts/apply_revisions.py` | `references/ooxml.md` |
| 批注回写（六文件联动） | `scripts/apply_comments.py` | `references/ooxml.md` |
| 回写校验 | `scripts/validate_docx.py` | **必须同时加负向对照** |
| manifest、续跑、故障恢复 | `scripts/state.py` | `SKILL.md` |
| 闸门统计 | `scripts/metrics.py` | — |
| 报告章节、xlsx 列 | `scripts/report.py` | `assets/report-template.md`；增列要升 `schema_version` |
| 审查记忆 | `scripts/import_decisions.py` | — |
| **新增一条范式规则** | `assets/patterns/*.yaml`（**不改代码**），或用户自带的包 | `references/patterns.md` 的正反例约定 |
| 范式的场景定位逻辑、规则包校验 | `scripts/scan_patterns.py` | `references/patterns.md` + `prompts/pass1-pattern.md` |
| 错别字通道 | `scripts/typo_scan.py` + `assets/dict/` | `prompts/pass1-typo.md` |
| **扩充错别字词表**（提召回的唯一杠杆） | `assets/dict/common-typos.txt` | 白名单进 `typo-whitelist.txt`；**误报句进 `typo-traps.txt`** |
| 默认配置 | `assets/config.default.yaml` | `docs/SPEC.md` §16 |

---

## 开发环境

```bash
pip install lxml openpyxl pyyaml          # 运行期必需
pip install python-docx                    # 仅生成 fixture 用，运行期不需要
sudo apt-get install -y libreoffice-writer # 仅 .doc 转换需要
```

`tiktoken` 是可选的：`tokenizer.py` 会尝试加载本地已缓存的词表，**任何失败（含无网络）
都静默退回保守估算**并置 `token_estimated: true`。运行期绝不联网。

生成 fixture：`python3 docx-content-review/tests/fixtures/make_fixtures.py`
（`.docx` 与答案清单都已入库，跑回归不需要 python-docx）。

---

## 编码约定

**子 Agent 的上下文是硬约束。** 凡是会被子 Agent 读到的产物，都要按
「一个单元一个文件」落盘，且**只放这个单元用得上的字段**。主文件（全量候选）
只给 merge 用。同一批数据不要在一个文件里存两份（`candidates` + `batches[].items`
就是这么把 typo payload 撑到两倍的）。stdout 同理：`claim next` 曾经把整条 chunk 元信息
连同三个 pid 列表一起吐出来，一条工具输出几 KB，全程留在子 Agent 的上下文里。

每个 `scripts/*.py` 必须：

- **可独立 CLI 调用**，`--help` 有效；
- **幂等**——重复执行结果一致，这是断点续跑的前提；
- **不联网**；
- **退出码规范**：0 成功 / 1 失败 / 2 参数错 / 3 环境缺失 / 4 写路径越界 / 5 磁盘不足 /
  6 工作目录不合法 / 7 租约被占 / 8 校验失败 / 9 令牌失效 / 10 输入不可解析
  （常量在 `scripts/_common.py` 的 `EX`）；
- **不打印大对象**——stdout 只输出单行 JSON 摘要，正文一律落盘；
- **所有写操作走 `guard_write_path`**，路径从 `resolve_path` 取，禁止硬编码拼接；
- **所有产物写入走 `atomic_write_*`**（写 `.tmp` → `os.replace()`）。
  半写入的文件绝不能表现为"已完成"——这是"文件存在性即状态"机制成立的必要条件。

对模型的调用**不在脚本里**：脚本全是确定性逻辑，LLM 调用由 Agent 按
`references/prompts/*.md` 发起。这样换模型、打桩测试都不需要动脚本。

注释用中文，与技能内其他文案一致；**不出现任何厂商模型名称**，一律称
"Agent / 主 Agent / 子 Agent"。

---

## 测试

```bash
docx-content-review/tests/run_regression.sh          # 全量，约 30 秒，不调用 LLM
docx-content-review/tests/run_regression.sh --keep   # 保留工作目录排障
```

回归覆盖：源文档保护、目录隔离、写路径守卫、三道脚本闸门、L01–L32 检出、
ledger 重建幂等、术语表自检、修订回写、**D9 负向对照**、
**批注锚定（每处修订带理由批注、范围覆盖完整正文 + 两条负向对照）**、令牌栅栏、报告产物、
两条支线（错词表自检 + 负向语料对照 + 白名单陷阱零候选；范式规则包三条红线拒绝加载、
P 类无建议文本、裁定 U 不成条目、三通道产物互不覆盖）、
闸门④脚手架（payload 不泄题、排列可复现、判定表六条淘汰路径）、
**分片预算与重切片后作废旧产物（含"不清就静默跳片"的负向对照）**、
**闸门④与 Pass 4 的分批并行（分批不泄题、并行与串行判定逐字相同、漏答必须报数）**、
**阶段化单元（prompt 自包含、claim 不吐大对象、崩溃回收与重派、三通道 --all 收口互不覆盖）**、
**照 SKILL.md 第 4 步把波内循环跑一遍（第 27 节）**。

**扩词表时记住**：左串不能是常见词组的碎片。「按全」会被「按全流程」拆出来——
这类条目既抬误报又白耗裁定调用，让「扩表不抬高误报率」的前提失效。
`typo-traps.txt` 就是拦这个的，发现新误报时把那句话原样加进去，只增不减。

改动词表、规则包或不改清单后，先跑这三个自检再跑全量：

```bash
python3 docx-content-review/scripts/typo_scan.py lint
python3 docx-content-review/scripts/scan_patterns.py lint --patterns <你的包>
python3 docx-content-review/scripts/filter_neverflag.py \
        --traps docx-content-review/tests/fixtures/neverflag-traps.json
```

第三个是**闸门③的负向语料**：既验"该压制的压住了"，也验"不该压制的没被压住"。
压制方向的 fail-open 比放行方向更危险——它表现为"什么都没查出来"，
输出里没有任何痕迹。在真实语料上发现新的误压制时，把那一段原样加进去，**只增不减**。

`corpus/` 放大型真实语料（不入 git）。小 fixture 跑快速回归，大语料只在里程碑跑。

### 单元测试全绿不等于主流程能跑

ADR-034 里四条断链有两条是这样漏掉的：范式支线的回归测到
`issues-0001.patterns.jsonl` 为止就停了，从没跨过 `verify_pass2`；租约的单元行为
（令牌栅栏、回写阶段禁止接管）测过，但没有一项测试是**照着 `SKILL.md` 的主流程
从头跑一遍**。结果是两条通道各自都对，串起来第一步就断。

加一条通道、加一个流程步骤时，除了给它自己的单元断言，还要问一句：
**它的产物最终流进了哪个文件，那个文件有没有被断言过？**

这条在 ADR-043 上又兑现了一次，而且这次断在第一步：`review` 单元的完成标记指向
闸门产物，闸门却要等整波跑完才跑——`exhausted` 永远不为真，第一波永远出不去。
221 项全绿，因为第 26 节的构造顺序是「写产物 → **立刻跑闸门** → 断言 exhausted」，
恰好把它盖住了。**回归第 27 节就是为此加的：照着 SKILL.md 第 4 步的顺序跑波内循环，
中途不跑任何闸门。** 动 claim / 单元 / 阶段相关的东西，先看这一节。

配套的通用守卫是一句话：**每个阶段的 `done_marker` 必须等于它的 `output`**——
完成判定只能看子 Agent 自己写的那个文件，不能看任何后续步骤的产物。

### 加校验时必须同时加负向对照

`validate_docx.py` 的第一版在"删掉新增 run 的 rPr"这个负向对照下**是通过的**——
因为它读的是回写时记录的溯源信息，而溯源只描述"当时做了什么"，无法证明"现在的文档是什么样"。
这类校验不写负向对照就等于没写。

---

## 当前进度 → `docs/progress.md`
## 决策日志 → `docs/decisions.md`
## 完整规格 → `docs/SPEC.md`

新会话开场**只读 `CLAUDE.md` + `docs/progress.md`，禁止全仓扫描**。
脚本通过 CLI 契约调用，不读源码。一次会话聚焦一个模块，不跨模块重构。
