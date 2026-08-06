# 需求对齐分析与调整方案

> 输入：用户明确的五条 skill 需求。
> 输出：现状差距判断 + 调整方案。**本文件是开发期文档，技能包不得引用。**
> 日期：2026-08-06

---

## 一、五条需求 vs 现状

| # | 需求 | 现状承载 | 判定 |
|---|---|---|---|
| 1 | 错别字审查 | A1 类（LLM 顺带发现）+ `typo_scan.py` 独立通道 | ⚠️ **半成品**——通道骨架在，但默认关闭、未接入主流程、无裁定 prompt、词典是占位规模 |
| 2 | 语义问题（语病、语句不完整） | A2–A8 + B1–B5，闸门②按类别校验，never-flag N1–N14 | ✅ 设计覆盖完整，**一处小缺口**（见 §4） |
| 3 | 前后描述逻辑冲突 | L01–L05、L20–L24、L28 | ✅ 设计覆盖 |
| 4 | 前后数据一致性冲突 | L06–L14 | ✅ 设计覆盖 |
| 5 | 特定场景的描述范式审查 | **无** | ❌ **完全缺位**，且当前架构没有任何装配位 |

**共同风险**：需求 2/3/4 的"覆盖"是**设计覆盖，不是实测覆盖**。
`references/prompts/*.md` 五份模板从未用真实模型跑过（见 `progress.md` §未完成 1），
SPEC §15.3 的四项验收指标目前都没有数据。这一条对下面所有方案都是前置约束。

---

## 二、需求 1：错别字通道必须转正

### 现状的具体问题

1. **SKILL.md 的 description 承诺了"语病与错别字"，但主流程里没有任何 typo 支线。**
   第 4 步 Pass 1 只发起「审查」和「事实抽取」两次调用，错别字实际只能靠 A1
   在通用审查里顺带发现——而 SPEC §9.7 自己已经论证了这条路先天不利
   （模型对错别字有鲁棒性、配额挤占、重要性排挤）。承诺与流程不一致。
2. **缺 `references/prompts/pass1-typo.md`。** `typo_scan.py scan` 产出了带
   `batches` 的裁定 payload，`merge` 也准备好读 `.verdicts.jsonl`，
   但中间那步"LLM 批量裁定"没有 prompt 模板，Agent 无从发起。通道是断的。
3. **词典是占位规模。** `common-typos.txt` 41 行、`common-words.txt` 117 词。
   错别字的召回率**完全由词表规模决定**，当前规模下召回率接近 0。
4. **单字混淆集是空转代码。** `typo_scan.py:113-117` 遍历形近/音近对之后直接
   `continue`，一个候选都不产生。注释说明了原因（无分词与词表时噪音过大），
   但留一段看起来在工作实则空转的循环是负债。
5. `typo_check.enabled: false`。

### 调整方案

| 动作 | 文件 | 说明 |
|---|---|---|
| 补裁定 prompt | 新增 `references/prompts/pass1-typo.md` | 封闭选择题：给 ±6 字上下文 + 原字 + 候选正字，只答 `A`(原字正确)/`B`(应改)/`C`(都不对)，一次 ≤50 条，输出一行一个字母 |
| 接入主流程 | `SKILL.md` 第 4 步 | Pass 1 后增设 typo 支线：`typo_scan.py scan` → 逐 batch 裁定 → `typo_scan.py merge` → 过闸门②③。**与主审查并列，不合并调用** |
| 扩充错词对表 | `assets/dict/common-typos.txt` 41 → 目标 500+ | 这是召回率的**唯一杠杆**，且与 never-flag 同属"可枚举、可收敛"资产 |
| 删除空转代码 | `typo_scan.py:113-117` | 要么删掉，要么在词表就位后真正启用；不保留空转循环 |
| 默认开启 | `config.default.yaml` `typo_check.enabled: true` | 用户把错别字列为第 1 条需求 |
| 标定 fixture | 新增 `tests/fixtures/typo-set.docx` + answers | 含 N 个已知错别字 + M 个白名单陷阱（"帐篷""日月潭""其它"），回归加召回率/误报率断言 |

### 一个需要用户拍板的取舍：要不要内置大词表

`common-words.txt` 用于**未登录词检测**（识别"不在词表里的字串 → 可能是错字"）。
这条路要求词表在 5 万词量级以上，否则误报淹没一切。两个选择：

- **(a) 内置 5–10 万词表**：数 MB，符合 SPEC §9.7"资源总体积数 MB"的预期，
  召回率显著提升。代价是要确定词表来源与分发许可。
- **(b) 放弃未登录词检测**，只靠「错词对表 + 术语表 `forbidden` + 编辑距离 ≤2」。
  零许可风险，召回率上限就是词表覆盖到的那些词。

**建议先做 (b) 保证通道可用**，(a) 作为后续增量——但需要用户先确认是否接受
技能包体积增加数 MB。

### 不建议做的

**不要为提升错别字召回而放宽闸门②的 A1 阈值**（长度差 ≤2 且差异字符 ≤3）。
召回靠词典，不靠放松校验。放宽阈值会让 A1 变成任意改写的入口。

---

## 三、需求 5：新增 P 类「范式审查」——这是主要的架构改动

### 为什么当前架构接不住

`category` 是硬编码封闭枚举（`verify_span.py` 的 `VALID_CATEGORIES`），
`edit_gate` 是 if-else 链，never-flag 是 md 文本 + `filter_neverflag.py` 硬编码 `check`，
prompt 是静态 md。**要加一条"风险条目必须写清影响与应对"这样的规则，
今天必须改三处代码。** 而用户说的是"待后续补充正例/反例"——
意味着规则会持续增加，改代码的路子走不通。

### 设计原则：数据驱动，加规则不改代码

范式规则外置为 YAML 规则包，用户可用 `--patterns <file>` 传入自己的规则：

```yaml
schema_version: 1
patterns:
  - id: P-RISK-01
    name: 风险条目描述范式
    scope:                            # 场景定位，全部条件 AND，且必须全部可脚本判定
      heading_regex: "风险|Risk"       # 所在标题路径命中
      paragraph_regex: "^风险\\s*[0-9一二三]"
      min_chars: 20
      skip_in_table: true
    requires:                         # 要件清单，逐条独立封闭判定
      - key: impact
        label: 影响
        hint: 说明该风险发生后对进度/成本/质量的影响
      - key: mitigation
        label: 应对措施
        hint: 给出降低或规避该风险的具体动作
      - key: owner
        label: 责任人
        optional: true                # optional 项缺失只进报告，不出批注
    severity: Medium
    action: comment                   # comment | report_only，**永远不能是 revision**
    examples:
      positive:
        - text: "风险1：第三方接口不稳定。若可用性低于 99%，订单同步将延迟；拟增加本地缓存与重试，由集成组负责。"
          note: 影响/应对/责任人三项齐备
      negative:
        - text: "风险1：第三方接口可能不稳定，需要关注。"
          why_flag: 缺影响与应对
        - text: "本章分析项目面临的主要风险。"
          why_not_flag: 章节引导语，不是风险条目——本范式不适用
```

**`examples.negative` 刻意分两种**：`why_flag`（该报的反面例子）与
`why_not_flag`（**不该报**——即范式级的 never-flag）。后者是误报抑制的主要资产，
呼应 never-flag.md 已确立的"反例数量 ≥ 正例"原则。这也正是用户
"待后续补充正例/反例"的落点：**补例子 = 编辑 YAML，不碰代码。**

### 执行链路：与 D1 / 错别字通道同构的三段式

```
scan_patterns.py scan   脚本按 scope 确定性定位适用段落（不问模型）
                        → work/patterns/pattern-<chunk>.json（正反例随规则注入 payload）
        ↓
LLM 逐要件封闭判定       prompts/pass1-pattern.md
                        「这段里『影响』这一要件出现了吗？只答 是/否/不确定」
        ↓
scan_patterns.py merge  只有明确「否」才成 issue；「不确定」按齐备处理（不确定即无问题）
        ↓
verify_span.py + filter_neverflag.py   两道既有闸门
```

**为什么不能直接问模型"这段是否符合范式"**：那是开放问题，违反 SKILL.md
「中等能力模型适配」第 5 条。拆成"要件 X 在这段里出现了吗"才是封闭题。

### 闸门适配的代价出奇地小

- `verify_span.py`：**只需把 `P` 类加进 `VALID_CATEGORIES`**。
  `edit_gate` 的默认分支已经是 `return False, "类别 X 不允许携带建议文本"`，
  P 类会自动被降级为批注并清空 `suggested_text`——正好是想要的行为。
- 独立配额 `max_pattern_issues_per_chunk`（默认 20），不占用 `max_issues_per_chunk`。
- 严重度上限 **Medium**：与 L29–L32 同理，判定依赖场景定位准确性，不配 High。
- `pattern_review.enabled: false` 默认关闭，且**无规则包时自动跳过** → 现有回归零影响。

### 为什么单立 P 类，而不是塞进 B 类

B 类是"语句本身有歧义"，判据是语言学；P 类是"内容要件缺失"，判据是外部规则包。
两者的动作也不同（P 类永不升级为修订）。混在一起会让 never-flag 的适用范围
和 `metrics.json` 的分类丢弃率同时失去意义——而丢弃率是调 prompt 的唯一依据。

### 与 `argument_review` 的边界（这条必须写死，否则实现时会滑过去）

SPEC §9.6 关掉论证链审查的理由是**误报不可收敛**：写不出一份
《哪些论断不需要证据》的清单。范式审查形似而质不同——
**它的要件清单由外部规则包给出，是闭合的**；场景不命中就不判，命中就只查
清单上那几项。因此它可以收敛，而论证链审查不能。

**红线：P 类不得退化为"这段写得够不够充分"的开放判断。**
任何一条 `requires` 项，如果无法用"这个信息出现了吗"来问，就不该进规则包。

### 配套改动清单

| 文件 | 改动 |
|---|---|
| `scripts/scan_patterns.py` | 新增。`scan` / `merge` 两个子命令，与 `typo_scan.py` 同构 |
| `assets/patterns/*.yaml` | 新增。内置规则包（先留空或放 1–2 条示例），用户规则用 `--patterns` 传入 |
| `references/patterns.md` | 新增。规则包 schema 说明 + 判定原则 + 红线 |
| `references/prompts/pass1-pattern.md` | 新增。要件封闭判定 prompt |
| `scripts/verify_span.py` | 一行：`P_CLASSES` 并入 `VALID_CATEGORIES` |
| `scripts/report.py` | 加「范式符合性」章节：按 `pattern_id` 给「适用 N 条 / 不合 M 条」 |
| `scripts/apply_comments.py` | 中文批注文案（沿用 ADR-019）：「风险条目缺少『应对措施』」+ 末尾 `【P-RISK-01】` |
| `assets/config.default.yaml` | `pattern_review` 段 |
| `SKILL.md` | 第 4 步增设 pattern 支线；CLI 契约表加一行；按需查阅表加一行 |
| `CLAUDE.md` 目录地图 | 加两行：「新增一条范式规则 → `assets/patterns/*.yaml`（**不改代码**）」「场景定位逻辑 → `scan_patterns.py`」 |
| `tests/fixtures/` | `pattern-set.docx` + answers（含 `why_not_flag` 类负样本） |

---

## 四、需求 2 的一处小缺口：句子不完整但补法不唯一

A4「成分残缺」要求**纯增补**（`is_subsequence(orig, sugg) and len(sugg) > len(orig)`），
这道闸门是对的——它保证了 A4 的修订可信。但它导致一类真实问题没有出口：

- 「本方案的实施路径包括」（句末截断，宾语整体缺失）
- 「该模块在完成接口联调后，」（后续分句丢失）

报 A4 会因"补法不唯一"而变成猜测性修订；报 B3 又不满足「≥80 字」。
结果是**这类问题只能被丢弃**。

**建议新增 B6「句子不完整（成分残缺且补法不唯一）」→ 只批注**，判定要件：
① 主干确实不完整，不是省略式表达或标题名词短语（N14）；
② **补法不唯一**——能从上下文唯一确定补法的走 A4，不走 B6。

改动面很小：`taxonomy.md` + `pass1-review.md` + `verify_span.py` 的 `B_CLASSES`
+ 负样本 fixture。列为**可选**——它增加一类误报面，值得先拿到实测基线再上。

---

## 五、需要用户确认：优先级序列要不要动

`CLAUDE.md` 现有排序把「语病召回率」放在最末：

> 源文档完整性 > 样式不变性 > 定位准确率 > 回写安全性 > 误报率 > 逻辑检出率 > 语病召回率

用户把错别字列为第 1 条需求，与此有张力。建议改为：

> 源文档完整性 > 样式不变性 > 定位准确率 > 回写安全性 > 误报率 > **错别字召回率** > 逻辑检出率 > 语病召回率

**理由不是"用户说它第一所以排前面"**，而是两者性质不同：
错别字召回靠词典（确定性资产，扩表不抬高误报率），语病召回靠模型
（提升召回必然抬高误报）。把它们并列在末尾是原排序的一个错误。

前四项是底线，任何方案都不动。

---

## 六、实施顺序

| 序 | 事项 | 依赖 | 估时 |
|---|---|---|---|
| 1 | **P 类骨架**：规则包 schema + `scan_patterns.py` + `patterns.md` + 配置项 | 无（不依赖模型，也不依赖用户补例子） | ~0.5 天 |
| 2 | **错别字通道转正**：prompt + 接流程 + 扩表 + fixture + 回归断言 | 词表方案 (a)/(b) 需用户拍板 | ~1 天 |
| 3 | **真实模型基线**：`negative-set.docx` 跑 Pass 1+2，收 `metrics.json` | 无——`progress.md` 已列为下一步第 1 条 | ~1 天 |
| 4 | **填范式规则**：用户补 3–5 条场景的正反例 → 写进 `assets/patterns/` → 标定 fixture | 依赖用户提供例子 | 随例子量 |
| 5 | B6（可选） | 建议在第 3 步拿到基线之后再决定 | ~0.5 天 |

第 1 步与第 3 步可并行——P 类骨架全是确定性脚本，不需要模型。

**建议先做第 1 步**：它把"待后续补充正例/反例"变成一个纯数据操作，
用户随时可以往里填，而不必等一次代码改动。
