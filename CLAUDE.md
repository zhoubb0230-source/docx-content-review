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

优先级排序：**源文档完整性 > 样式不变性 > 定位准确率 > 回写安全性 > 误报率 > 逻辑检出率 > 语病召回率。**
指标冲突时按此序取舍。任何为提升后四项而放松前三项的方案一律否决。

---

## 目录地图：要改 X，去改哪个文件

| 要改什么 | 改哪里 | 同步改 |
|---|---|---|
| 输出目录解析、隔离规则、租约、claim | `scripts/workspace.py` | `SKILL.md` 并发一节 |
| 新增一个产物路径 | `scripts/workspace.py` 的 `KINDS` | —（禁止各脚本自行拼路径） |
| 环境探测、doc→docx | `scripts/env_probe.py` / `convert_doc.py` | — |
| run 合并规则、解包安全 | `scripts/unpack.py` | `references/ooxml.md` |
| 段落 ID、标题树、页码估算、代码块识别 | `scripts/extract.py` | `references/schemas.md` |
| token 计量、分片策略 | `scripts/tokenizer.py` / `chunk.py` | `references/schemas.md` |
| **新增/修改一个 A/B/C 类别** | `scripts/verify_span.py` 的 `edit_gate` | `references/taxonomy.md` + `prompts/pass1-review.md` + `tests/fixtures` |
| **新增一条不改清单规则** | `scripts/filter_neverflag.py` 的 `check` | `references/never-flag.md` + 负样本 fixture |
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
| 错别字通道 | `scripts/typo_scan.py` + `assets/dict/` | — |
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
ledger 重建幂等、术语表自检、修订回写、**D9 负向对照**、令牌栅栏、报告产物。

`corpus/` 放大型真实语料（不入 git）。小 fixture 跑快速回归，大语料只在里程碑跑。

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
