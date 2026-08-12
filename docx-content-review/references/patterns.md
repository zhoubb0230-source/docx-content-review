# P 类：特定场景的描述范式审查

**默认关闭**（`pattern_review.enabled: false`）。启用后若没有任何规则包，本通道自动跳过。

范式审查回答的是一类跟 A/B/L 都不同的问题：**这段文字属于某个已知场景，
而该场景要求写清的几件事，有没有写。**

- A 类问"这句话本身有没有写错"；
- B 类问"这句话有没有歧义"；
- L 类问"这处跟别处对不对得上"；
- **P 类问"这类段落该有的要件，齐不齐"。**

判据来自**外部规则包**，不来自语言学，也不来自模型的判断力。

---

## 加一条规则不需要改代码

规则写在 YAML 里，脚本负责定位场景，模型只回答封闭问题。三段式与错别字通道同构：

```
scan_patterns.py scan     脚本按 scope 确定性定位适用段落（不问模型）
      ↓
LLM 逐要件封闭判定         prompts/pass1-pattern.md
                          「『影响』这一要件在这段里出现了吗？只答 Y / N / U」
      ↓
scan_patterns.py merge    只有明确 N 才成条目；U 按齐备处理（不确定即无问题）
      ↓
verify_span.py + filter_neverflag.py    两道既有闸门
```

加载顺序：内置 `assets/patterns/*.yaml` → 配置里的 `pattern_review.packs` →
命令行 `--patterns`。**同 id 后者覆盖前者**，所以改写内置示例只需在自己的包里
用同一个 id 重写一遍，不必去动技能目录。

写完先自查：`scan_patterns.py lint --patterns <你的包>`，不需要 run 目录。

---

## 规则包格式

```yaml
schema_version: 1
patterns:
  - id: P-RISK-01               # 必填。P 开头，字母数字与 -_，长度 3–31
    name: 风险条目描述范式        # 必填。批注正文用它，别写成代号
    enabled: true               # 可选。false 则跳过这条

    scope:                      # 场景定位。全部条件 AND，必须全部可脚本判定
      heading_regex: "风险|Risk"          # 匹配 heading_path 拼接后的字符串
      paragraph_regex: "^\\s*风险\\s*[0-9]+"  # 匹配段落正文
      exclude_regex: "^\\s*本章"           # 命中即排除（写 why_not_flag 的确定性版本）
      min_chars: 20
      max_chars: 0              # 0 = 不限
      skip_headings: true       # 默认 true：标题行不参与
      heading_only: false       # true 则只查标题行

    requires:                   # 要件清单。每项独立判定，模型一次只看一项
      - key: impact             # 必填，包内唯一
        label: 影响             # 必填，出现在批注正文里
        hint: 该风险发生后对进度/成本/质量的影响   # 给模型的判定说明
        optional: false         # true 表示缺失只进报告，不出批注

    severity: Medium            # Critical/High/Medium/Low，实际会被 severity_cap 压到 Medium
    action: comment             # comment | report_only。**没有 revision 这个选项**
                                # 闸门②会原样保留 report_only，不会把它升回 comment

    examples:
      positive:
        - text: "…"
          note: 为什么算齐备
      negative:
        - text: "…"
          why_flag: 缺影响与应对         # 该报的反面例子
        - text: "…"
          why_not_flag: 章节引导语，不适用  # **不该报**——场景不成立
```

`scope` 里 `heading_regex` 与 `paragraph_regex` **至少要有一个**。
两个都没有的规则会命中全文每一段，那是误报的最大来源，脚本直接拒绝加载。

反例的 `why_flag` 与 `why_not_flag` **必须二选一写上**，否则规则包加载失败。
两者作用相反：前者是"这样写就该报"，后者是"这看着像但不该报"。
后者才是误报抑制的主要资产——与 `never-flag.md` 里"反例数量 ≥ 正例"是同一件事。

---

## 三条红线

### 1. P 类永不生成修订

规则里写 `suggested_text` 会被拒绝加载；即使漏网，`verify_span.py` 也会清空它
（P 类不在 A 类白名单内，走"该类别不允许携带建议文本"分支）。

要件缺失没有唯一正确的补法——补什么内容只有作者知道。指出缺什么就够了。

### 2. requires 的每一项都要能用「这个信息出现了吗」来问

能问的：影响、应对措施、责任人、入参、出参、异常处理、验收标准、生效日期、
数据来源、依赖项、回滚方案。

不能问的：**充分、合理、清晰、必要、有说服力、是否论证到位**。

这条不是措辞讲究，是能否收敛的分界线。SPEC §9.6 关掉论证链审查
（`argument_review`）的理由是误报不可收敛——写不出一份《哪些论断不需要证据》的清单。
范式审查能进 v1，**唯一的原因是它的要件清单由规则包给出、是闭合的**：
场景不命中就不判，命中就只查清单上那几项。

**一旦某条 requires 变成"这段写得够不够充分"，P 类就退化成了论证链审查，
误报会立刻不可收敛。** 写规则时对每一条要件问一句：
两个人分别判定，会得出相同答案吗？不会，就不要写进去。

### 3. 严重度上限 Medium

与 L29–L32 同理：判定依赖场景定位的准确性，漏掉一个 `exclude_regex`
就会误报一片。`severity_cap` 默认 `Medium`，写 High 也会被压回来。

---

## 已知边界

- **表格与代码段不参与范式审查。** 表格行由闸门③ N11 统一丢弃，
  代码段由 N9 丢弃，`scan` 阶段就已排除，写规则时不必考虑。
  若将来需要审查表格行的填写范式，那是另一条通道，不要试图用 scope 绕过。
- **跨段落的范式管不了。** 一条规则只看一个段落。
  "风险表述分散在连续三段里"这种情况会被判成缺要件——
  规则该用 `min_chars` 或更严的 `paragraph_regex` 把这类段落排除在外。
- **`scan` 只看 `review_pids`**，与主审查通道的可审查范围一致。
- **P 类的跨度就是整段**，因此它的长度上限走 `pattern_review.max_span_chars`
  （默认 `0` = 不限），不受 `verification.max_span_chars`（默认 120）约束。
  两者的判据不同：后者是"跨度太长说明模型在圈整段"，对 P 类不成立——
  它本来问的就是"这一段该有的要件齐不齐"。共用一个上限会把真实文档里
  超过 120 字的风险条目、接口描述整组丢掉，且丢弃在报告里不露面。
- **P 类不进闸门④，但会原样留在 `issues-verified.jsonl` 里。**
  "不复核"不等于"淘汰"：它的裁定本就是封闭题，Pass 2 没有规则包再问一遍
  只会得到信息更少的答案，所以直接标 `not_reviewed` 通过。

---

## 排障

| 现象 | 多半是 |
|---|---|
| 候选数量爆炸 | `scope` 太松。先加 `paragraph_regex`，再加 `min_chars` |
| 明明该命中却没有候选 | `heading_regex` 匹配的是 `heading_path` 拼接串（` > ` 分隔），不是段落文本 |
| 全都判成缺要件 | `hint` 写得太抽象，模型判不出来。改写成"具体要出现什么信息" |
| 报了一堆章节概述句 | 缺 `exclude_regex`。把 `why_not_flag` 里的例子转成正则 |
| 规则包加载失败 | 先跑 `scan_patterns.py lint`，错误信息会指出是哪条规则的哪一项 |
