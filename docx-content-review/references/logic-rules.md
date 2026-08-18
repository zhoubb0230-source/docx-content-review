# 逻辑冲突检测规则表（L01–L32）

**这些规则由 `detect_conflicts.py` 纯脚本实现，不需要你判断。** 本文件是脚本的规格说明，
供排障与理解报告使用；你在流程中的职责只有两件：

1. Pass 1 **照抄事实**进台账（`prompts/pass1-extract.md`）——抽得全不全直接决定这里的检出率；
2. Pass 4 **逐条裁定**候选是真冲突还是范围不同（`prompts/pass4-adjudicate.md`，
   题面由 `adjudicate_pass4.py build` 按批拼好，一批 10 组，各批可并行）。

为什么这样分工：分片后的子 Agent 是失忆的，看不到其他分片。跨片比对是集合运算，
不是语言理解任务——交给脚本可获得 100% 召回且零幻觉，代价只是要求抽取足够完整。

**所有 L 规则的输出都是「候选」，必须过 Pass 4 裁定后才进交付物。**

这条是 fail-closed 的：**没有裁定结果的候选一律不写进文档**，
UNSURE 也按「不构成矛盾」处理——唯一例外是 Critical 级的 UNSURE，
保留但在批注里标注「需人工确认」。
未准入的候选不会消失，它们照常出现在报告与 `issues.xlsx` 里，
并在报告的「本次未覆盖范围」一节按原因给出计数。

**术语类的七条默认不跑**：L01–L05（术语与命名）与 L25/L26（术语规范）
由 `logic.term_rules` 统一开关，默认 `false`。关闭时这七条不产候选，
`glossary_scan.py` 的 Pass 0 抽取整步跳过（它存在的目的就是喂这几条），
报告的「本次未覆盖范围」会写明这一维度没查。改成 `true` 即恢复，不需要改代码。
**关掉不等于不留痕**：脚本会把这七条的候选文件清成空并标 `skipped_reason`，
因为下游是按 glob 读候选目录的，留着上一轮的旧文件等于没关。

---

## 规则清单

| ID | 规则 | 严重度 | 动作 | 依赖的事实类型 |
|---|---|---|---|---|
| **术语与命名** |||||
| L01 | 同一 `term` 出现 ≥2 个实质不同的 `definition` | High | 批注 | terms |
| L02 | 同一 `acronym` 有 ≥2 个不同 `expansion` | High | 批注 | acronyms |
| L03 | `acronym` 全文未给出 `expansion` | Low | 批注 | acronyms |
| L04 | 同一 `entity` 多种写法且未登记为别名组 | Medium | 批注 | entities + glossary |
| L05 | 近义术语混用指代同一概念 | Low | 报告 | glossary 已确认的概念族 |
| **数值与量纲** |||||
| L06 | 同一 `(subject, scope, kind)` 出现不同 `value` | **Critical** | 批注 | metrics |
| L07 | 同一 `subject` 的 `unit` 不一致（ms/s、GB/G） | High | 批注 | metrics |
| L08 | 区间矛盾（下限 > 上限） | **Critical** | 批注 | metrics + qualifier |
| L09 | 百分比构成合计 ≠ 100%（容差 ±0.5%） | High | 批注 | metrics（unit=%） |
| L10 | `count_declared ≠ count_listed` | High | 修订*/批注 | enumerations |
| L11 | 数值与文字描述不符（称"翻倍"但比值非 2） | Medium | 批注 | commitments/conclusions/metrics |
| **时间与版本** |||||
| L12 | 同一 `event` 出现不同 `date` | **Critical** | 批注 | dates |
| L13 | 时序倒置（阶段 N 早于阶段 N-1） | High | 批注 | dates（含阶段名） |
| L14 | 同一 `subject` 的 `version` 倒退或不一致 | High | 批注 | versions |
| **引用与编号** |||||
| L15 | `xref` 指向不存在的章节/图/表/附录 | High | 批注 | xrefs + 标题树 |
| L16 | 图/表编号跳号或重号 | Medium | 批注 | 段落文本中的图表标题 |
| L17 | 目录条目与正文标题文本不一致 | Medium | 批注 | TOC 样式段落 + 标题树 |
| L18 | 图/表有标题但正文无引用 | Low | 报告 | 图表标题 + xrefs |
| L19 | 章节层级跳级（H2 直接跳 H4） | Low | 报告 | 标题树 |
| **结构与论断** |||||
| L20 | 同一 `subject` 的 `status` 前后矛盾 | **Critical** | 批注 | statuses |
| L21 | 同一承诺主体的 `modality` 强度冲突 | High | 批注 | commitments |
| L22 | 同一职责分配给不同 `role` | High | 批注 | roles |
| L23 | `conclusion` 与其 scope 内的 `status` 矛盾 | High | 批注 | conclusions + statuses |
| L24 | "综上所述"总结的条目数与前文列举不符 | Medium | 批注 | conclusions + enumerations |
| **术语规范**（仅在存在 `authoritative` 层时激活） |||||
| L25 | 使用了 `forbidden` 中登记的写法 | High | **修订** | glossary |
| L26 | 使用了 `variants` 而非 `preferred`，且 `enforce=error` | Medium | **修订** | glossary |
| **立场与论证覆盖性** |||||
| L27 | 同一 `subject` 的目标值与实测值差异 > `gap_ratio`（默认 3 倍） | High | 批注 | metrics（kind=目标/实测） |
| L28 | 同一 `subject` 的 `stance` 前后不一致 | High | 批注 | positions |
| L29 | 目标无任何举措指向 | Medium | **仅报告** | objectives + initiatives |
| L30 | 举措无任何验收指标 | Medium | **仅报告** | initiatives + acceptance |
| L31 | 量化承诺无度量方式描述 | Medium | **仅报告** | commitments |
| L32 | 章节标题承诺的内容正文缺失 | Low | **仅报告** | 标题树 + 正文 |

\* L10 能唯一确定正确数字时生成修订，否则批注。

---

## 三条实现约束

**L06 按 `(subject, scope, kind)` 分组，不是按 `(subject, scope)`。** 目标值与实测值天然不同，
把它们放进同一组会把每一对目标/实测都报成 Critical 冲突——那正是 L27 的职责，且严重度不同。

**L25/L26 的匹配必须排除更长写法的子串，且 ASCII 写法要求词边界。** 否则「星云」会命中
「星云平台」里的前两个字，「EN」会命中「OPEN」。这类误报会随术语表变大而线性增长。

**L29–L32 是集合差集，不是论证强度判断**——这是它们能进 v1 的原因。但它们**依赖抽取完整性**：
漏抽一条举措就会误报"目标无对应举措"。因此严重度上限 Medium，措辞统一为
「未检索到与 X 对应的 Y，请确认」，默认只进报告不进批注（`coverage_rules_to_comment: false`）。
首轮跑完应人工核对其误报率，再决定是否提升其地位。

---

## 不在 v1 范围内

「论断缺乏证据」「结论强度超过证据」「是否存在绝对化表达」这类判断**无客观阈值**，
由 `argument_review` 开关控制，默认关闭。

关闭的理由不是工作量，而是**误报不可收敛**：其他类别的误报可以通过不改清单逐条压制
（有限可枚举），但写不出一份《哪些论断不需要证据》的清单。技术文档的概述、引言、小结中
大量存在"本方案可有效支撑…"这类表述，其中绝大多数本就不需要就地给出证据。
这意味着该模块不会随迭代改善，而其他模块都会。
