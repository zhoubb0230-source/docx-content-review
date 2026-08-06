# Pass 1 支线 · 范式要件裁定 prompt 模板（P 类）

**任务已经被脚本切到最小**：场景由 `scan_patterns.py` 按规则包的 `scope`
确定性定位好了，你只回答一件事——**这段文字里，某个指定的信息出现了没有。**

## 你不判断的三件事

1. **不判断这段该不该适用这条范式。** 场景已定，规则包的 `exclude_regex`
   已经把不适用的排除掉了。你只看要件在不在。
2. **不判断写得好不好。** 要件"出现了但写得潦草"一律算出现（Y）。
   充分性判断没有客观阈值，那是被 spec §9.6 关掉的论证链审查，不是范式审查。
3. **不给建议。** P 类永远不生成修订，输出里没有建议字段的位置。

## 输入

`work/patterns/patterns-<chunk>.json`，每批 ≤20 个候选（`batches[i].items`）。
每个候选带 `cid` / `text` / `checks`（要件清单），`rules[]` 里带该规则的正反例。

**发起调用时把对应规则的正反例一并贴进 prompt**——反例比正例更重要，
尤其是标了 `why_not_flag` 的那些。

## prompt 形式

```
下面每段文字都属于「{{pattern_name}}」这一类描述。
对每段，逐项判断指定的信息是否在这段文字中出现。

判定标准：
- 该信息以任何措辞出现了 → Y
- 该信息确实没有出现 → N
- 拿不准 → U

只看信息在不在，不评价写得好不好、充不充分、合不合理。

参考（这样写算齐备）：
{{examples.positive[i].text}}

参考（这样写才算缺失）：
{{examples.negative[i].text}} —— {{why_flag}}

参考（这类不要判成缺失）：
{{examples.negative[j].text}} —— {{why_not_flag}}

────────────────
[{{cid}}] {{text}}
  需判断：
    impact  = 影响：该风险发生后对进度/成本/质量的影响
    mitigation = 应对措施：为降低或规避该风险准备采取的动作
────────────────
[{{cid}}] …

输出：每行一条 JSON，形如 {"cid":"0001-001","key":"impact","answer":"Y"}
每个候选的每一项要件各输出一行。不要代码围栏，不要任何其他文字。
```

## 输出与写回

逐行追加到 `work/patterns/patterns-<chunk>.verdicts.jsonl`：

```json
{"cid":"0001-001","key":"impact","answer":"Y"}
{"cid":"0001-001","key":"mitigation","answer":"N"}
```

`answer` 是封闭枚举 `Y` / `N` / `U`，只能选不能造。缺行、拼错、答了别的值，
`scan_patterns.py merge` 一律按 `U` 处理。

## U 一律按「出现了」处理

这是本通道接受召回损失换精确率的地方，与闸门④ 的 `UNSURE → NO` 同源：
**不确定即无问题**。不要为了多留几条而把 U 当 N。

由 `pattern_review.unsure_as_present` 控制（默认 `true`）。改成 `false`
会让每一次犹豫都变成一条批注，误报率会立刻上去——除非手上有实测基线，否则不要动。

## 调用约束

- 温度 0；一次 ≤20 个候选（`batch_size`）。
- 一次调用只做这一件事，**不要和 Pass 1 的审查或事实抽取合并**。
- 无状态、自包含，不做多轮对话。
- 候选数有独立配额 `max_pattern_issues_per_chunk`（默认 20），
  不占用主审查的 20 条。

## 之后会发生什么（不需要你做）

`merge` 只把明确 `N` 的要件转成条目，`category` 固定为 `P1`，
`rule_id` 是规则号（如 `P-RISK-01`），`suggested_text` 恒为空。
随后照常过闸门② `verify_span.py` 与闸门③ `filter_neverflag.py`，
并入 Pass 2 的待复核集。
