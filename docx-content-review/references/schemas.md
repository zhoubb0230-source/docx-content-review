# 数据结构定义

所有 JSONL 文件：**一行一条 JSON，无外层包裹、无 Markdown 代码围栏、无前言后语**。
字段缺失填 `null`，不允许自由发挥、不允许新增字段。

版本标识（`spec_version` / `schema_version` / `skill_version` / `runid`）写在
`report.md`、`issues.xlsx`、`glossary.merged.json`、`metrics.json`、`manifest.json` 头部。
字段增删时 `schema_version` 递增，下游据此判断兼容性。

---

## paragraphs.jsonl（`extract.py` 产出）

```json
{"pid":"p-000412","xpath_hint":"/w:document/w:body/w:p[412]","index":412,
 "heading_path":["3 系统设计","3.2 数据层","3.2.1 存储选型"],"level":3,"is_heading":false,
 "style":"Normal","style_name":"正文","text":"……","char_len":186,
 "in_table":false,"table_id":null,"row_idx":null,"cell_idx":null,
 "is_list":false,"is_code":false,"is_quote":false,
 "page_hint":137,"page_estimated":true}
```

- `heading_path` 是逻辑审查的核心上下文，必须准确。
- `is_code` / `in_table` / `is_quote` 用于命中不改清单 N9/N11/N10。
- `page_hint` **仅用于报告定位，不参与任何判定，也不作为切分判据**。

## chunks/index.json（`chunk.py` 产出）

```json
{"single_pass":false,"total_text_tokens":612340,"token_estimated":false,
 "tokenizer":"tiktoken:o200k_base",
 "chunks":[{"chunk_id":"0042","path":"…/chunk-0042.txt","chunk_type":"mixed",
   "text_tokens":15120,"context_tokens":28640,"pages_est":19,
   "page_from":131,"page_to":149,"heading_path":["3 系统设计"],
   "pids":["p-000401","…"],"review_pids":["…"],"context_pids":["p-000399","p-000400"],
   "needs_review_call":true,"oversized":false}]}
```

`chunk_type` ∈ `text` / `mixed` / `table_only`。**`table_only` 的分片不发起审查调用**，
只发起事实抽取调用。`context_pids` 是上文衔接区，**其中的 pid 不在审查范围**，
对它们上报问题会被闸门丢弃。

---

## issues-\<chunk\>.raw.jsonl（Pass 1 审查调用的输出 ← 你写这个）

```json
{"pid":"p-000412","category":"A2","original_text":"认真的完成","suggested_text":"认真地完成","evidence":"状语应用地","severity":"High"}
```

| 字段 | 约束 |
|---|---|
| `pid` | 必须是本分片内、且不在 `context_pids` 中的段落 ID |
| `category` | 封闭枚举：A1–A8 / B1–B5 / C1–C2。无法归类的**不要上报** |
| `original_text` | 必须在该段落中**逐字出现**（仅允许空白差异）；长度 4–120 字 |
| `suggested_text` | 仅 A 类给出；B/C 类留空字符串（给了也会被清空） |
| `evidence` | **≤25 字**。理由越长模型越容易自我说服 |
| `severity` | 封闭枚举：Critical / High / Medium / Low |

**禁止输出 `confidence` 字段。** 模型自评置信度不作为任何阈值（见 SKILL.md）。

## issues-\<chunk\>.jsonl（闸门后，`verify_span.py` 产出）

在上表基础上补 `chunk_id` / `rule_id` / `heading_path` / `page_hint` / `in_table` /
`is_code` / `gate_note` / `gates` / `action`。`action` ∈ `revision` / `comment` / `report_only`。

## issues-verified.jsonl（Pass 2 后）

再补：

```json
{"verify":{"result":"pass|drop","method":"blind_ab|closed_single|not_reviewed","position":"A|B"},
 "memory_hit":false,"memory_reason":null}
```

`method: not_reviewed` = 该类别不进闸门④（C 类、P 类，以及建议已被闸门②清空的 A 类）。
**不复核不等于淘汰**：这些条目 `result` 恒为 `pass`，按各自的 `action` 决定去向。
这个文件是 report / apply_comments / apply_revisions / metrics 四处的唯一入口，
被它漏掉的类别在报告里也会一并消失。

---

## facts-\<chunk\>.json（Pass 1 抽取调用的输出 ← 你写这个）

**三条硬约束**：

1. **不得做术语归一化**——`variants_seen` 必须记录文档中的实际写法。台账记录的是
   "文档里实际怎么写的"，术语判定发生在 Pass 3。这条解耦让"补充术语表"的代价
   从"重跑 Pass 1（占总成本 90%）"降到"只重跑 Pass 3/4（数分钟）"。
2. **`serves_objective` 只在文档显式写明对应关系时填写**（如"为实现目标 O1，开展…"），
   不得自行推断。推断出的对应关系会让 L29 失去意义——它会把"漏抽"伪装成"已覆盖"。
   **宁可留 `null`。**
3. **表格必须抽取。** 规划类、可研类文档的关键数字绝大多数在表格中，`metrics` 的主要
   来源是表格而非正文。表格跳过语病审查，但事实抽取不得跳过，且需标记 `source:"表格"`。

```json
{
  "terms":      [{"term":"边缘节点","definition":"……","pid":"p-000121","is_definition":true}],
  "acronyms":   [{"acronym":"MEC","expansion":"多接入边缘计算","pid":"p-000121","first_use":true}],
  "entities":   [{"name":"星云平台","variants_seen":["星云","Nebula"],"pid":"p-000305"}],
  "metrics":    [{"subject":"端到端时延","value":"200","unit":"ms","qualifier":"≤","scope":"核心链路","kind":"目标","source":"正文","pid":"p-000418"}],
  "positions":  [{"subject":"服务架构","stance":"微服务","pid":"p-000210"}],
  "objectives": [{"obj_id":"O1","statement":"实现研发效率提升","pid":"p-000188"}],
  "initiatives":[{"init_id":"I1","statement":"建设统一代码平台","serves_objective":"O1","pid":"p-000260"}],
  "acceptance": [{"target":"I1","criterion":"平台接入率≥80%","pid":"p-000271"}],
  "dates":      [{"event":"一期上线","value":"2026-03","pid":"p-000502"}],
  "versions":   [{"subject":"星云平台","value":"v2.3","pid":"p-000511"}],
  "roles":      [{"role":"数据owner","duty":"审批数据出域申请","pid":"p-000733"}],
  "xrefs":      [{"type":"section","target":"3.2.1","pid":"p-000640"}],
  "numbering":  [{"type":"figure","label":"图 3-7","caption":"……","pid":"p-000651"}],
  "commitments":[{"subject":"灰度发布","modality":"必须","statement":"……","pid":"p-000812"}],
  "statuses":   [{"subject":"多租户隔离","status":"不支持","pid":"p-000903"}],
  "enumerations":[{"claim":"三个方面","count_declared":3,"count_listed":4,"pid":"p-001002"}],
  "conclusions":[{"scope":"3.2 数据层","statement":"……","pid":"p-000455"}]
}
```

**枚举字段的取值范围**：

| 字段 | 取值 |
|---|---|
| `metrics.kind` | 目标 / 实测 / 预期 / 基线 / 行业参考 |
| `metrics.source` | 正文 / 表格 |
| `metrics.qualifier` | ≤ / ≥ / = / 约 / 不超过 / 不低于 / 上限 / 下限 / null |
| `commitments.modality` | 必须 / 应 / 将 / 计划 / 不低于 / 不得 / 宜 / 可 / 可选 / 建议 |
| `statuses.status` | 已完成 / 已支持 / 建设中 / 规划中 / 不支持 / 未完成 |
| `xrefs.type` | section / figure / table / appendix |
| `numbering.type` | figure / table |

---

## conflicts-candidate.\<RULE\>.json（`detect_conflicts.py` 产出）

```json
{"rule":"L06","severity":"Critical",
 "candidates":[{"conflict_id":"L06-0002","rule":"L06","severity":"Critical",
   "action":"comment","description":"同一指标在相同范围下出现不同数值",
   "subject":"端到端时延","note":"kind=目标 范围=核心链路 下出现 2 个不同数值",
   "chapter_span":2,
   "sides":[{"pid":"p-000418","text":"……","heading_path":["1 总则"],"page_hint":1,"value":"200","unit":"ms"},
            {"pid":"p-000905","text":"……","heading_path":["2 架构设计"],"page_hint":2,"value":"500","unit":"ms"}]}]}
```

生成修订的规则（L10/L25/L26）另带 `suggest`：

```json
{"suggest":{"original_text":"星云","suggested_text":"星云平台","all_occurrences":true}}
```

`all_occurrences` = 该段落里这个写法**全部**替换（术语规范化的语义就是如此）。
不带这个标记时，跨度在段内出现多次即拒绝落笔——"改哪一处"没有依据
（见 `references/ooxml.md` §2.1）。

## conflicts/verdicts/adjudicate-bNN.verdicts.jsonl（Pass 4 输出 ← 你写这个）

```json
{"conflict_id":"L06-0002","verdict":"CONFLICT","note":"同为核心链路目标值"}
```

`verdict` ∈ `CONFLICT` / `NOT_CONFLICT` / `UNSURE`。`note` ≤30 字，说明范围差异。
**UNSURE 按 NOT_CONFLICT 处理**，但 Critical 级的 UNSURE 保留并标注"待人工确认"。

**一批一个文件**（题面来自 `conflicts/batches/adjudicate-bNN.json`）：各批可以并行，
而并行时往同一个文件 append 会在中断处互相截断。`adjudicate_pass4.py collect`
把它们归并成下游唯一认的 `work/conflicts-verified.jsonl`（同样的 schema），
并报出 `missing`（有候选没裁定）与 `invalid_lines`（格式不对，**不按 UNSURE 收下**）。
分文件 + 幂等 collect 是断点续跑的前提：只补跑缺的批次即可。

## verify/pass2-<排列>.vNN.verdicts.jsonl（闸门④输出 ← 你写这个）

```json
{"id":"3b1089d0a7737959","answer":"A"}
```

题面来自同名的 `pass2-<排列>.vNN.json`，一批一个文件、各批独立可并行，
裁定写回同名的 `.verdicts.jsonl`。`verify_pass2.py merge` 会把所有分批文件一起读进来。
**`.key.json` 不要读**——它记的是原文在哪一侧。

---

## glossary.merged.json（唯一出口，下游只消费它）

```json
{"entries":[{"key":"边缘节点","preferred":"边缘节点",
  "variants":["边缘结点","edge node","EN"],
  "forbidden":[{"form":"边沿节点","reason":"错别字"}],
  "definition":"……","source":"authoritative","priority":1,"scope":"global",
  "case_sensitive":false,"enforce":"error","cluster_id":"c001","cluster_confirmed":false,
  "sources":["authoritative","extracted"]}],
 "layers":{"authoritative":2,"extracted":34,"fallback":3},
 "has_authoritative":true,"merged_hash":"fa4547af1baa021f"}
```

`enforce` ∈ `error`（可生成修订）/ `warn`（报告+批注）/ `off`（只作参照物，纯误报抑制）。
`cluster_confirmed:false` 的概念族**不触发任何 L 规则**，只进报告的「待确认概念族」一节。

## review-memory.json（工作目录级，跨文档共享）

```json
{"schema_version":"1","entries":[
  {"key_hash":"…","category":"B3","original_norm":"…","decision":"ignore",
   "reason":"公司写作习惯","created_at":"…","hit_count":7}]}
```

键是 `(category, 归一化后的 original_text)` 的哈希；归一化**仅限空白与全半角统一**，
不做任何语义处理。命中即降级为 `report_only`。
**严禁从历史决策自动泛化规则**——记忆只能精确匹配，泛化必须经人。
