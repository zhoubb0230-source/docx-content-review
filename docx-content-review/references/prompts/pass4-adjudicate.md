# Pass 4 · 冲突裁定 prompt 模板

程序已经在结构化台账上做完了确定性比对，产出的是**候选**。你的唯一任务是判断
每条候选是真矛盾，还是因为适用范围/前提条件不同而并不矛盾。

## 关键约束

**严禁要求模型判断"哪一处是对的"。** 它无法知道 —— 文档之外的事实不在它的视野里，
这类判断必然是幻觉源。批注文案因此统一为"请确认以哪一处为准"，而不是"应改为 X"。

冲突双方的原文**必须同时进入上下文**。这是 Pass 4 存在的全部意义：
Pass 1 的子 Agent 是失忆的，只有到这一步才第一次看得见两侧。

## 调用形式

温度 0。**题面由 `adjudicate_pass4.py build` 拼好，一批一个文件**：
`work/conflicts/batches/adjudicate-bNN.json`（每批 `logic.adjudicate_batch_size` 组，
默认 10）。各批之间没有依赖，可以并行；一个子 Agent 只读自己那一批。

**不要自己去读 `conflicts-candidate.*.json`。** 一份长文档的候选可达几百条，
整份读进上下文之后，此后每一轮工具往返都要把它重算一遍——这是压测里
Pass 4 变慢的主因，不是模型答得慢。

每组的字段：`conflict_id` / `rule_description` / `subject` / `note` / `sides[]`
（各带 `heading_path`、`page`、`text`）。据此拼题面：

```
下面每组给出同一份文档中两处相关的描述。判断它们是否构成真实矛盾。

第 1 组（{{rule_description}}）
位置一　章节：{{sides[0].heading_path}}　第 {{sides[0].page}} 页
　　　　原文：{{sides[0].text}}
位置二　章节：{{sides[1].heading_path}}　第 {{sides[1].page}} 页
　　　　原文：{{sides[1].text}}

第 2 组
…

对每一组回答：CONFLICT / NOT_CONFLICT / UNSURE，并用一句话（不超过 30 字）
说明两处的适用范围有无差异。
```

题面里**没有** severity 与 action，这是有意的：模型看到「Critical」会倾向于答
CONFLICT，而这条判断应当只由两侧原文决定。

## 判定处理

| 回答 | 处理 |
|---|---|
| `CONFLICT` | 进入交付物（批注或修订，按规则的 action） |
| `NOT_CONFLICT` | 丢弃 |
| `UNSURE` | **按 NOT_CONFLICT 处理**；但 Critical 级的 UNSURE 保留，并在批注中标注"待人工确认" |
| **漏答 / 没跑到** | **同样不进交付物。** 这一档比 UNSURE 更严：Critical 也没有例外——它说明流程没走完，不是模型拿不准 |

这张表由脚本执行（`_common.py` 的 `conflict_admitted`），三处调用点共用同一份策略。
**你漏答一条，那条就不会进文档**，因此宁可如实答 UNSURE，也不要跳过。

## 结果写回

写到**本批自己的文件** `work/conflicts/verdicts/adjudicate-bNN.verdicts.jsonl`
（与批次文件同名，后缀换成 `.verdicts.jsonl`），本批答完一次性写入：

```json
{"conflict_id":"L06-0002","verdict":"CONFLICT","note":"同为核心链路目标值"}
```

`conflict_id` 直接抄题面里的那一个，**不要用组号**——组号只在本次调用内有意义。
并行时各写各的文件：多个子 Agent 往同一个文件 append，中断处会互相截断。

全部批次写完后跑一次 `adjudicate_pass4.py collect --run-dir <run>`，
它归并成下游唯一认的 `work/conflicts-verified.jsonl`，并报出三个数：

- `missing`：有候选、没裁定。**这些条目一律不进交付物**（未裁定 = 不确定 = 无问题），
  所以漏答的表现是「这条冲突凭空消失」。看到 `missing > 0` 就补跑对应批次再 collect。
- `invalid_lines`：格式不对的裁定行。**不会被当成 UNSURE 收下**——
  那是"模型拿不准"的待遇，不该给一行没答对格式的输出。
- `by_verdict`：三种结论各多少条。

collect 是幂等的，且会保留上一轮已归并的裁定，中断后只需补跑缺的批次。

**每批调用前后执行一次** `state.py heartbeat --run-dir <run> --session <sid>`：
本阶段脚本不在运行，没有自然心跳点，不续租会被误判为死亡而被接管。
（**每条**都心跳是旧写法——那是逐条裁定时代的产物，现在按批即可。）

## 常见的"看起来矛盾其实不矛盾"

判 NOT_CONFLICT 时最常见的合理理由，供参考（但仍以两处原文为准）：

- 适用范围不同：核心链路 vs 边缘链路、生产环境 vs 测试环境、一期 vs 二期
- 时间阶段不同：现状 vs 目标、基线 vs 规划
- 统计口径不同：峰值 vs 均值、含税 vs 不含税、全量 vs 增量
- 层级不同：总体目标 vs 分项目标
- 一处是引用外部标准的原文，一处是本文档的自有约定
