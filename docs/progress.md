# 开发进度

> 跨 session 续接的**唯一入口**。新会话开场只读 `CLAUDE.md` + 本文件，禁止全仓扫描。
> 每次会话结束前必须更新本文件。

**最后更新**：2026-08-02

---

## 当前阶段

**M1–M6 的脚本侧已实现并通过回归；M7（3000 页压测）与 M8（错别字通道）未做。**

技能包结构完整、可运行：`SKILL.md`（259 行）+ 6 份 references + 5 份 prompt 模板
+ 22 个脚本 + 配置与词典资源 + 5 份 fixture。

`tests/run_regression.sh`：**39 项检查全部通过**，约 30 秒，不调用任何 LLM。

---

## 已完成模块

| 模块 | 文件 | 状态 |
|---|---|---|
| 共享底座（退出码/原子写/编辑距离） | `scripts/_common.py` | ✅ |
| 工作目录、隔离、守卫、租约、claim | `scripts/workspace.py` | ✅ |
| 环境探测、doc→docx | `scripts/env_probe.py`、`convert_doc.py` | ✅ 仅在 Linux+soffice 下实测 |
| 解包、符号链接清理、run 合并 | `scripts/unpack.py` | ✅ 碎片化文档 513→55 run，文本逐字不变 |
| 段落抽取、标题树、页密度校准 | `scripts/extract.py` | ✅ |
| token 计量、分片 | `scripts/tokenizer.py`、`chunk.py` | ✅ tiktoken 缺失时自动降级 |
| 闸门②（分类编辑距离） | `scripts/verify_span.py` | ✅ |
| 闸门③（不改清单硬过滤） | `scripts/filter_neverflag.py` | ✅ N7–N14 |
| 术语预筛与概念族聚类 | `scripts/glossary_scan.py` | ✅ |
| 术语表导入、自检、三层合并 | `scripts/import_glossary.py` | ✅ CSV/XLSX/YAML/JSON/TXT |
| 台账 SQLite 索引 | `scripts/ledger.py` | ✅ 删库可全量重建且幂等 |
| 冲突检测 L01–L32 | `scripts/detect_conflicts.py` | ✅ 27/27 适用规则在注入 fixture 上检出 |
| OOXML 底层 | `scripts/ooxml.py` | ✅ |
| 修订回写 | `scripts/apply_revisions.py` | ✅ 两段式，可断点 |
| 批注回写（六文件联动） | `scripts/apply_comments.py` | ✅ 增强层可降级 |
| 回写四项校验 | `scripts/validate_docx.py` | ✅ **含 D9 负向对照** |
| 状态、续跑、故障恢复 | `scripts/state.py` | ✅ 含 `doctor` |
| 闸门丢弃率统计 | `scripts/metrics.py` | ✅ |
| 报告与 issues.xlsx | `scripts/report.py` | ✅ 七个固定章节 |
| 审查记忆 | `scripts/import_decisions.py` | ✅ 仅精确匹配 |
| 错别字通道 | `scripts/typo_scan.py` + `assets/dict/` | ⚠️ 接口与词典就位，**未在真实错别字语料上标定** |

---

## 未完成 / 待验证

### 1. 全部 LLM 侧 prompt 未经真实模型验证 ⚠️ 最重要

`references/prompts/*.md` 五份模板是按 SPEC §12 的约束写的，但**从未用真实模型跑过**。
回归测试里 LLM 的位置全部由 fixture 中的参考产物替代。

因此以下 SPEC 验收指标**目前没有实测数据**：

- 负样本误报率 ≤5%（M4 核心指标）
- A 类召回率 ≥80%（M2）
- 盲测 A/B 两种排列下判定一致率 ≥90%（M2；低于此说明模型有强位置偏好，需换 prompt）
- Pass 4 裁定的准确性

**下一步应优先做这件事**：拿 `negative-set.docx`（33 条合法表达）跑一轮 Pass 1 + Pass 2，
用 `metrics.py collect` 看四道闸门各自的丢弃率。这是 SPEC 反复强调的"调优的唯一依据"。

### 2. M7 压测未做

`docs/SPEC.md` §7.4.3 的性能表是**建模估算，非实测**。需要 ≥500 页真实文档：
跑通全流程、记录实测页密度、确定 `parallelism` 最优值，然后回填该表。

### 3. M8 错别字通道未标定

`typo_scan.py` 的候选生成逻辑已就位，但：
- 单字混淆集（形近/音近）目前只加载不使用——直接按单字生成候选噪音过大，
  需要配合分词与未登录词检测才有可用信噪比；
- `assets/dict/common-words.txt` 只有 116 词，是占位规模，远不足以做未登录词检测；
- 未在"含 100 个已知错别字"的 fixture 上测过召回率。

### 4. 未在 Windows / macOS 上验证

`convert_doc.py` 的 Word COM 分支、`workspace.py` 的文件系统类型探测（见 ADR-014）
都只在 Linux 上跑过。

### 5. 多会话协作只做了单元级验证

令牌栅栏（退出码 9）、回写阶段禁止接管已在回归中覆盖；但"两个会话真正并发处理同一文档、
无重复处理无丢片"这项 M6 验收**未做端到端验证**。

---

## 下一步三条

1. **建立 prompt 的实测基线**：用 `negative-set.docx` 跑 Pass 1 + Pass 2，
   收 `metrics.json`，据四道闸门丢弃率调 `pass1-review.md`。没有这份数据，改 prompt 只能靠感觉。
2. **端到端并发验证**：同一文档起两个会话跑 Pass 1，核对无重复处理、无产物覆盖、无丢片。
3. **M7 压测**：≥500 页真实文档跑通，回填 SPEC §7.4.3 的性能表与实测页密度。

---

## 未决问题

- **L29–L32 的误报率没有基线。** SPEC 要求"首轮跑完应人工核对其误报率，再决定是否提升
  其地位"。目前默认 `coverage_rules_to_comment: false`（只进报告），在拿到基线前不要改。
- **XSD 校验是已知缺口**（ADR-007）。若要补齐，需决定是否内置 ECMA-376 schema
  （数 MB + 分发许可问题）。
- **`report.py` 的「术语表覆盖率」一节按 `preferred` 是否出现在全文判定**，
  对有 scope 限定的术语会偏乐观。真实语料上看过再决定要不要按 scope 细化。
