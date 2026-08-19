# 决策日志（ADR）

记录 SPEC 未规定、由实现方自行决定的事项。SPEC 已明确规定的（D1–D9、四道闸门、
L 规则表、目录结构、配置项）不在此重复。

格式：编号 / 日期 / 决策 / 备选方案 / 理由 / 影响文件。

---

## ADR-001　新增 `scripts/_common.py` 与 `scripts/ooxml.py`

- **日期**：2026-08-02
- **决策**：在 SPEC §4 的脚本清单之外增加两个共享模块。`_common.py` 放退出码、原子写、
  哈希、文本归一化、编辑距离、CLI 骨架；`ooxml.py` 放 run 定位/拆分、rPr 深拷贝、
  修订视图等 OOXML 底层操作。
- **备选**：把共享代码复制到各脚本；或塞进 `workspace.py`。
- **理由**：rPr 深拷贝逻辑一旦有多份实现，D9 就会在其中一份上被静默违反——这正是
  SPEC 反复警告的失败模式。集中一处、被三个回写/校验脚本共用，才可能保证一致。
  `_common.py` 同理：原子写若各写各的，"半写入文件表现为已完成"的问题会零星复现。
- **影响**：`scripts/_common.py`、`scripts/ooxml.py`，被全部脚本 import。

## ADR-002　新增 `scripts/glossary_scan.py` 与 `scripts/tokenizer.py`

- **日期**：2026-08-02
- **决策**：SPEC §9.1.4 要求 Pass 0 做脚本预筛、§7.2 要求实际调用 tokenizer，但脚本清单里
  没有对应文件。分别新建 `glossary_scan.py`（候选预筛 + 概念族聚类）与
  `tokenizer.py`（token 计量与降级）。
- **备选**：并入 `import_glossary.py` / `chunk.py`。
- **理由**：预筛与导入是两件事——预筛在 LLM 确认之前，导入在之后，合并会让"补充术语表
  只重跑 Pass 3/4"这条路径难以实现。tokenizer 独立则便于替换实现与打桩。
- **影响**：`scripts/glossary_scan.py`、`scripts/tokenizer.py`、`SKILL.md` 契约表。

## ADR-003　退出码分配

- **日期**：2026-08-02
- **决策**：0 成功 / 1 失败 / 2 参数错 / 3 环境缺失 / 4 写路径越界 / 5 磁盘不足 /
  6 工作目录不合法 / 7 租约被占 / 8 校验失败 / **9 令牌失效（SPEC 强制）** / 10 输入不可解析。
- **理由**：SPEC 只规定了 9 = NOT_OWNER。其余按"Agent 需要区别处理的失败种类"划分：
  3/5/6 要转达给用户去修环境，4 是内部 bug 必须立即停，7 要走并发交互，
  8 要回滚重试，10 要重试一次 LLM 调用。
- **影响**：`scripts/_common.py` 的 `EX`、`SKILL.md` 契约表。

## ADR-004　输出根等于源文档目录时的例外判定

- **日期**：2026-08-02
- **决策**：SPEC §11.1 要求"不得位于源文档所在目录（除非该目录恰好就是 CWD 且用户显式指定）"。
  实现为：`root == 源文档目录` 且 `root == CWD` 时放行，否则终止（退出码 6）。
- **备选**：要求额外的 `--allow-source-dir` 显式开关。
- **理由**：用户在文档所在目录里启动 Agent 是最常见的用法，再要求一个开关只会制造摩擦；
  而产物落在 `<root>/docx-review/` 子树内，对源目录的污染仅一个子目录。
  非 CWD 的情况（例如 `--output-dir` 指到某个文档目录）才是真正的误用，一律拒绝。
- **影响**：`scripts/workspace.py` 的 `resolve_output_root`。

## ADR-005　文档隔离目录增加 `doc.json`

- **日期**：2026-08-02
- **决策**：在文档目录下写 `doc.json` 记录完整 `source_sha256`；命中目录时先读它，
  读不到再回退扫描各 run 的 `manifest.json`。
- **理由**：SPEC §11.2 要求"命中目录后必须比对 manifest 中存储的完整 sha256"。
  但 run 可能被 `keep_runs` 淘汰、manifest 可能损坏，此时全长校验就失效了，
  碰撞防护退回到 12 位前缀。`doc.json` 让全长校验不依赖任何 run 的存活。
- **影响**：`scripts/workspace.py` 的 `locate_doc_dir` / `_stored_sha`。

## ADR-006　闸门③ 在单片上限之前执行

- **日期**：2026-08-02
- **决策**：`verify_span.py` 内联调用 `filter_neverflag.check`，在按 severity 截断到
  `max_issues_per_chunk` **之前**完成不改清单过滤。`filter_neverflag.py` 仍是这些规则的
  唯一实现，且可独立、幂等地重跑。
- **备选**：严格按 SPEC §8 的书写顺序，闸门②（含单片上限）跑完再跑闸门③。
- **理由**：实测发现按书写顺序执行时，代码块与表格里的噪音会先占满 20 条配额，把真问题
  截断掉——本该被硬过滤的条目反而挤掉了本该上报的条目。SPEC §3 的流水线图也是
  "编辑距离校验 → 不改清单过滤"这个顺序，与本决策一致。
- **影响**：`scripts/verify_span.py`。

## ADR-007　XSD 校验降级为结构约束校验

- **日期**：2026-08-02
- **决策**：`validate_docx.py` 默认执行"全部部件 XML 良构 + 本技能所涉元素的结构约束"
  （rPr 内 ins/del 排首位、w:r 的 rPr 排首位、修订 id 唯一、w:del 内不得有 w:t）；
  提供 `--xsd <目录>` 时才做真正的 XSD 校验。
- **备选**：在 `assets/` 内置 ECMA-376 的 wml.xsd 全套。
- **理由**：全套 schema 约数 MB 且有分发许可问题，而本技能只写入 `w:ins`/`w:del`/
  `w:comment*` 这几类元素，针对性的结构约束能覆盖实际风险面。**这是一处已知的能力缺口，
  已在 `references/ooxml.md` 与本条中写明**，不做隐性降级。
- **影响**：`scripts/validate_docx.py`。

## ADR-008　D9 逐 run 校验改为逐字符 (字, rPr) 比对

- **日期**：2026-08-02
- **决策**：样式不变性校验不读回写时写下的 `revision-provenance.json`，改为从磁盘上的
  基线与产物两份 `document.xml` 现场重算：① 拒绝全部修订视图必须逐字符匹配字与 rPr；
  ② 接受全部修订视图中每个段落用到的 rPr 必须是该段落基线 rPr 的子集。
- **理由**：第一版读溯源记录，在"删掉新增 run 的 rPr"这个负向对照下**通过了**——
  溯源只描述"当时做了什么"，无法证明"现在的文档是什么样"。SPEC 反复强调 D9 的失败是
  静默的，校验本身若也静默失效，这条约束就形同虚设。
- **影响**：`scripts/ooxml.py` 的 `styled_char_view`、`scripts/validate_docx.py` 的
  `check_style`、`tests/run_regression.sh` 的负向对照。

## ADR-009　rPr 指纹用自定义规范化串，不用 c14n2

- **日期**：2026-08-02
- **决策**：`ooxml.canonical()` 递归拼 `tag + 排序后的属性 + 子元素`，据此算 rPr 指纹。
- **理由**：`lxml` 的 `c14n2` 要求命名空间在作用域内声明，而深拷贝出来的 rPr 是尚未挂进
  文档的游离子树，序列化直接抛异常。自定义形式同时消除了属性书写顺序与前缀差异的干扰。
- **影响**：`scripts/ooxml.py`、`scripts/unpack.py` 的 run 合并键。

## ADR-010　L06 按 (subject, scope, kind) 分组

- **日期**：2026-08-02
- **决策**：SPEC §9.4 写的是"同一 `(subject, scope)` 出现不同 `value`"，实现按
  `(subject, scope, kind)` 三元组分组。
- **理由**：`kind` 区分目标/实测/预期/基线。按二元组分组会把每一对"目标 200ms、实测 1200ms"
  都报成 Critical 级数值冲突，而这正是 L27 的职责且严重度为 High。二者混淆会让
  Critical 级失去"必须人工确认"的含义。
- **影响**：`scripts/detect_conflicts.py` 的 `r_L06`、`references/logic-rules.md`。

## ADR-011　L25/L26 的匹配排除更长写法子串并要求 ASCII 词边界

- **日期**：2026-08-02
- **决策**：`_bare_occurrences()` 跳过被更长登记写法覆盖的位置；ASCII 写法两侧必须是非字母数字。
- **理由**：实测「星云」作为「星云平台」的变体登记后，会命中「星云平台」里的前两字，
  产生纯误报；「EN」会命中「OPEN」。这类误报随术语表变大而线性增长，且恰好落在
  L25/L26 这两条**会生成修订**的规则上，风险最高。
- **影响**：`scripts/detect_conflicts.py`。

## ADR-012　L31 用局部段落窗口，不用扁平语料的字符距离

- **日期**：2026-08-02
- **决策**：判断量化承诺是否有度量方式时，取"提及该承诺主体的段落及其后 2 段"作为窗口。
- **理由**：第一版还退回到"在拼接全文里取 ±200/400 字符"，导致规则结论取决于排版顺序——
  同样的文档改一处无关内容就可能翻转结论。覆盖性规则本就依赖抽取完整性，判据再不确定
  就无法排障。
- **影响**：`scripts/detect_conflicts.py` 的 `r_L31`。

## ADR-013　审查记忆的查询侧并入 `import_decisions.py`

- **日期**：2026-08-02
- **决策**：`import_decisions.py` 提供 `import`（回灌）与 `apply`（命中降级）两个子命令；
  `apply` 在 `review_memory.exact_match_only` 被关闭时直接以退出码 2 拒绝运行。
- **理由**：SPEC §11.7 只给了回灌侧的脚本名，查询侧需要一个归属。两者共用同一套键归一化，
  分开实现就会出现"写入用一种归一化、读取用另一种"的静默不一致。
  `exact_match_only` 的硬拒绝是为了兑现"泛化必须经人"这条约束——把它做成可配置项
  却不校验，等于留了一个自动泛化的后门。
- **影响**：`scripts/import_decisions.py`。

## ADR-014　网络文件系统探测失败时按本地处理

- **日期**：2026-08-02
- **决策**：`fs_type()` 读 `/proc/self/mountinfo`；读不到（非 Linux）时返回 `unknown`，
  `is_local_fs()` 对 `unknown` 返回 true。
- **理由**：SPEC §11.3.5 要求非本地文件系统时 fail-closed。但在 macOS/Windows 上无法用同样
  手段判定，若把 `unknown` 一律当作网络盘，协作模式在这些平台上会永远不可用。
  折中：能判定为网络盘的一律拒绝，判定不了的放行并在 `workspace.py fscheck` 中如实回报
  `fs_type: "unknown"`，由 Agent 决定是否提示用户。
- **影响**：`scripts/workspace.py` 的 `fs_type` / `is_local_fs`。

## ADR-015　token 估算基线用 CJK/非 CJK 分别折算

- **日期**：2026-08-02
- **决策**：tokenizer 不可得时，基线估算为 `CJK 字数 × 1.0 + 其余字符 × 0.4`，
  再乘配置的 `token_fallback_ratio`（默认 1.6）。
- **备选**：直接用总字符数 × 1.6。
- **理由**：SPEC §7.2 的 ×1.6 是针对"1 汉字 ≈ 1 token"这个基线的保守系数。纯用总字符数
  会在英文/数字密集的表格段落上大幅高估（英文平均约 0.25–0.4 token/字符），
  把本可以放进一片的内容切碎，反而增加调用次数。分别折算后再乘同一系数，
  保守性不变而精度更高。
- **影响**：`scripts/tokenizer.py`。

## ADR-016　临时目录与交付目录分离

- **日期**：2026-08-05
- **决策**：`workspace.py init` 解析两个互相独立的根：
  **交付目录**（`--output-dir`，默认 Agent 当前工作目录）放最终产物；
  **临时目录**（`--temp-dir`，**默认与交付目录同址**，即中间件落在 `<工作目录>/docx-review/`）
  放全部中间件。`docx-review/<文档slug>-<sha12>/run-*/` 这棵树整体移到临时根之下。
- **备选**：沿用 SPEC §11.1 的单一"输出根"，交付物留在 `run/output/`。
- **理由**：SPEC 写这条时把"中间件落点"与"交付物落点"当成了同一件事。实际使用中它们的
  生命周期完全相反——中间件跑完就该能删，交付物要长期留存。混在一处的直接后果是
  用户在自己的工作目录里找不到审查结果，而清理中间件又会连带删掉交付物。
- **影响**：`scripts/workspace.py`（`resolve_deliver_root` / `resolve_temp_root` /
  `deliver_path` / `clean-temp`）、`report.py`、`metrics.py`、`unpack.py pack`、
  `import_decisions.py`、`assets/config.default.yaml`、`SKILL.md`。

## ADR-017　临时根默认跟随工作目录，不引入平台特定路径

- **日期**：2026-08-05（同日修订）
- **决策**：`resolve_temp_root` 在未显式指定时**直接返回交付根**，中间件落在
  `<工作目录>/docx-review/`。显式指定（`--temp-dir` / 环境变量 / 配置项）不可用时
  硬失败（退出码 6），不做静默回退——用户指名要那里。
- **被推翻的初版**：曾按平台取默认值（Windows `D:\temp_doc_review`、其他平台系统临时目录），
  并在默认值不可用时回退。**这是对需求的误读**：用户报告的 `D:\temp_doc_review\docx-review`
  是他观察到的现象，不是期望值；他要的是中间件也在工作目录下。
- **理由**：中间件与交付物同处一地，用户找得到、也能一眼看出 `docx-review/` 是可删的那个。
  引入平台特定的默认路径既增加了一处需要解释的行为，又制造了"产物散落在两个盘"的困惑。
  跨盘的需求（工作目录在网络盘、中间件体积大）是少数情形，交给显式参数即可。
- **影响**：`scripts/workspace.py`（删除 `default_temp_root` / `system_temp_root`）、
  `assets/config.default.yaml`（删除 `temp_dir_windows` / `temp_dir_posix`）、`SKILL.md`。

## ADR-018　交付物统一命名 `<原文件名>审查版_<时间戳>.<后缀>`

- **日期**：2026-08-05
- **决策**：五个交付物共用同一前缀，时间戳在 `init` 时生成一次并存入 manifest，
  全流程复用（`{stem}审查版_{ts}{ext}`，`ts` 为 `%Y%m%d_%H%M%S`）。同名时追加 `-2`，不覆盖。
- **理由**：秒级时间戳让同一天多次审查不互相覆盖；同前缀让五个产物在文件管理器里自然聚拢，
  也让"删临时目录"这个动作不会波及任何需要保留的东西。时间戳必须在 init 时定死——
  若各脚本各自取当前时间，同一次运行的产物会带上不同的时间戳而散落。
- **影响**：`scripts/workspace.py` 的 `DELIVERABLES` / `deliver_path`、`assets/config.default.yaml`。

## ADR-019　批注正文用中文标签，规则号降为末尾标记

- **日期**：2026-08-05
- **决策**：`_common.py` 新增 `CATEGORY_LABELS` / `LOGIC_GROUP_LABELS` / `rule_label()`。
  批注正文首行是中文说法（「前后数值不一致」「“的/地/得”误用」），规则号只出现在
  末尾的「（检测规则 L06，详见审查报告）」中。报告的分类统计与章节明细同步加中文列。
- **理由**：批注是给评审人看的，`【L06】` 对他们没有意义。规则号仍需保留以便排障与追溯，
  但不应占据首要位置。
- **附带修掉的两处文案错误**：
  ① 冲突双方落在同一段落时（如 L08「下限 5000 大于上限 3000」写在一句话里），
     原文案会渲染成"本处与彼处冲突"，即自己跟自己冲突；
  ② 「请确认以哪一处为准」只对**两侧是同一事实的互斥表述**的规则成立
     （`RIVAL_RULES`）。对 L08/L10/L13/L24/L29–L32 这类规则，两侧不是竞争关系，
     套用同一句话会让评审人不知道要确认什么，改为「请核对后确认」并附相关位置。
  ③ `note` 中的 `kind=目标 范围=全局` 是字段名，属开发者用语，改为
     「「并发连接数」的目标值出现 2 种：5000、3000（适用范围：全局）」。
- **影响**：`scripts/_common.py`、`scripts/apply_comments.py`、`scripts/report.py`、
  `scripts/detect_conflicts.py` 的 `r_L06` note。

## ADR-020　只有审查版 docx 是交付物

- **日期**：2026-08-06
- **决策**：五个产物照常生成，但只有 `reviewed_docx` 落在交付目录；`report.md` /
  `issues.xlsx` / `metrics.json` / `glossary.json` 落在 `<run>/output/`，随临时目录一起清理。
  由 `output.deliver_report` / `deliver_issues_xlsx` / `deliver_metrics` / `deliver_glossary`
  四个开关控制（默认全 `false`），init 时算出 `deliver_kinds` 存入 manifest，
  `artifact_path()` 据此决定落点。
- **备选**：① 全部交付（初版）；② 干脆不生成那四项。
- **理由**：用户要的是「带修订与批注的 Word 文档」，其余四项对他是噪音。
  但它们不能不生成——`report.md` 是 Agent 向用户汇报的依据，`issues.xlsx` 是审查记忆的
  回灌入口，`metrics.json` 是调 prompt 的唯一依据（SPEC §10.5 明写"没有这份数据，调优是盲的"），
  `glossary.json` 是下一轮的 `--authoritative` 输入。删掉它们等于砍掉迭代路径。
  折中是**生成但不交付**：需要时按路径取，或打开对应开关。
- **连带效果**：`clean-temp` 现在会连同这四项一起删除，因此它的返回值里明确列出
  `also_removed` 与提示语，SKILL.md 也要求 Agent 在清理前先确认用户不需要。
- **影响**：`scripts/workspace.py`（`ARTIFACTS` / `delivered_kinds` / `artifact_path` /
  `artifact_all` / `clean-temp`）、`scripts/report.py`、`assets/config.default.yaml`、`SKILL.md`。

## ADR-021　范式审查做成数据驱动的 P 类，规则包外置

- **日期**：2026-08-06
- **决策**：新增 P 类（场景描述范式）。`category` 固定为 `P1`（封闭枚举不变），
  具体范式由 `rule_id` 承载（`P-RISK-01`…，开放集合）。规则写在 YAML 规则包里
  （`assets/patterns/*.yaml` + 用户的 `--patterns`），**加规则不改代码**。
  执行链路与 D1、错别字通道同构：脚本按 `scope` 确定性定位 → LLM 逐要件答 Y/N/U →
  只有明确 N 成条目 → 过闸门②③。
- **备选**：① 把范式规则写进 `verify_span.py` 的类别体系；② 塞进 B 类；
  ③ 打开 `argument_review` 用它兜住。
- **理由**：范式是场景特定的、会持续增加的。写进代码意味着每加一条规则要改三处
  （类别枚举、闸门、prompt），而用户明确说"正例/反例待后续补充"——规则会一直加。
  塞进 B 类会让 never-flag 的适用范围和 `metrics.json` 的分类丢弃率同时失去意义
  （B 类判据是语言学，P 类判据是外部规则包，动作也不同）。
- **与 SPEC §9.6 的边界**：论证链审查被关闭的理由是误报不可收敛——写不出一份
  《哪些论断不需要证据》的清单。**范式审查能进 v1.1 的唯一原因是它的要件清单
  由规则包给出、是闭合的。** 因此写死一条红线：任何一条 `requires`，
  如果无法用「这个信息出现了吗」来问，就不该进规则包。规则包加载时强制校验
  三项（必须有 scope 定位条件、不得携带建议文本、反例必须注明 why_flag/why_not_flag），
  违反即以退出码 10 拒绝加载，不做猜测性修复。
- **闸门代价**：出奇地小。`verify_span.py` 只需把 `P1` 加进 `VALID_CATEGORIES`——
  `edit_gate` 的默认分支本就是「该类别不允许携带建议文本」，P 类会自动降级为批注。
  额外只加了严重度上限（`P_SEVERITY_CAP = Medium`）与 `rule_id` 透传。
- **影响**：新增 `scripts/scan_patterns.py`、`assets/patterns/builtin-examples.yaml`、
  `references/patterns.md`、`references/prompts/pass1-pattern.md`；
  改 `verify_span.py`、`filter_neverflag.py`、`report.py`、`apply_comments.py`、
  `_common.py`、`workspace.py` 的 `KINDS`、配置、`SKILL.md`、`taxonomy.md`。

## ADR-022　侧通道必须有自己的输出文件与配额

- **日期**：2026-08-06
- **决策**：`verify_span.py` 增加 `--out` 与 `--cap`，`filter_neverflag.py` 增加 `--file`。
  错别字与范式两条支线各写 `issues-<chunk>.typos.jsonl` / `.patterns.jsonl`，
  闸门统计文件按输出文件名派生。
- **理由**：原实现的输出路径是硬编码的 `issues-<chunk>.jsonl`。错别字通道的 `merge`
  已经写了自己的文件并提示"需再过两道闸门"，但照做就会**覆盖主通道的产物**——
  这是接通道时才会暴露的设计漏洞，不是使用错误。配额同理：错别字一片可能有 40 条，
  用主通道的 20 条上限会把它截断成随机取舍。
- **连带**：`filter_neverflag.py` 的 `check()` 对 P 类在位置类规则（N9/N10/N11/N12）
  之后即返回。N7/N8/N13/N14 判的是「改动本身该不该做」，而 P 类不改任何字，
  它的 `original_text` 是整个段落——拿整段去撞 fallback 术语表必然命中，
  会把所有范式条目误杀。
- **影响**：`scripts/verify_span.py`、`scripts/filter_neverflag.py`、
  `scripts/typo_scan.py` 的返回提示、`SKILL.md` 第 4.5 步。

## ADR-023　错别字通道转正；不做未登录词检测

- **日期**：2026-08-06
- **决策**：`typo_check.enabled` 默认改为 `true`，补 `prompts/pass1-typo.md`，
  接入 `SKILL.md` 主流程第 4.5 步，`common-typos.txt` 从 41 条扩到 210 条，
  新增 `typo_scan.py lint` 词表自检。**不启用未登录词检测**
  （`typo_check.oov_detection: false`），单字混淆集的空转循环删除并写明原因。
- **理由**：用户把错别字列为第一需求，而原状态是：description 里承诺了错别字，
  主流程里却没有 typo 支线；`scan` 产出了裁定 payload，`merge` 准备好读裁定结果，
  中间那步没有 prompt 模板——通道是断的。
  未登录词检测能突破「错词对表只查已知错误」的天花板，但需 5 万词级词表才有信噪比
  （现有 `common-words.txt` 只有 117 词）。内置大词表涉及词表来源与分发许可，
  先不做；召回率上限因此就是 `common-typos.txt` 的覆盖范围。
- **优先级序列相应调整**：`CLAUDE.md` 的取舍序列里，「错别字召回率」插到
  「逻辑检出率」之前。理由不是需求排序，而是两者性质不同：
  **错别字召回靠词表（确定性资产），扩表不抬高误报率**；语病召回靠模型，
  提召回必然抬高误报。把两者并列在末尾是原序列的一个错误。
  推论：扩 `common-typos.txt` 永远可做，**放宽闸门②的 A1 阈值永远不可做**。
- **词表登记规则**（`lint` 强制）：左串必须 ≥2 字（单字会把「度」「作」「帐」
  这类高频字全部拉成候选）；不得与白名单冲突；建议与原文不得相同；
  差异不得超出 A1 闸门（长度差 ≤2 且编辑距离 ≤3，否则永远落不了笔）。
  实际踩到的坑：`子节` 会命中「子节点」、`以经` 会命中「以经济」、
  `在次` 会命中「在次年」——这类条目一律删除或加白名单。
- **影响**：`assets/config.default.yaml`、`assets/dict/common-typos.txt`、
  `assets/dict/typo-whitelist.txt`、`scripts/typo_scan.py`、
  `references/prompts/pass1-typo.md`、`SKILL.md`、`CLAUDE.md`、`docs/SPEC.md` §9.7。

## ADR-024　单片截断必须为 B 类保留下限席位

- **日期**：2026-08-06
- **背景**：第一次用真实模型跑 `sample-basic.docx`（28 条正例）得到的实测数据。
- **观察到的现象**：模型产出 25 条，四道脚本闸门**一条没丢**（幻觉 0、长度 0、
  闸门②降级 0、不改清单 0），但 `max_issues_per_chunk: 20` 的截断
  **把 5 条 B 类整组砍光**，最终交付的 20 条全是 A 类。
- **根因**：`pass1-review.md` 规定「A 类用 High，B 类用 Medium」，
  于是"按 severity 排序截断"在这里等价于"按类别排序截断"。
  一片里只要 A 类满 20 条，指代不明、句法歧义、主客体颠倒就一条也出不来。
- **这不是新机制**：SPEC §9.7 论证错别字必须独立通道时写的就是同一件事——
  「配额挤占…截断按 severity 排序，A1 会被 Critical 级挤掉」。
  只是这次受害者换成了 B 类。错别字与范式的解法是独立通道，
  但 **B 类与 A 类同出一次调用，拆不成通道**，因此改为保底席位。
- **决策**：`chunking.min_b_class_slots`（默认 5）。截断时先给 B 类留出
  `min(B 类实际条数, 席位数, cap // 2)` 个位置，其余按严重度填满。
- **`cap // 2` 这一项是必须的**：初版写成 `min(len(B), floor, cap)`，
  在 `--cap 3` 下会保留 3 条全是 B 类、A 类一条不剩——把问题原样倒过来犯一遍。
  保底是为了让少数类不被整组挤掉，不是为了让它独占。
- **影响**：`scripts/verify_span.py`、`assets/config.default.yaml`、
  `docs/SPEC.md` §8 闸门② 第 4 条与 §16。

## ADR-025　答案清单要声明期望的闸门处置，而不是假定条条都能落笔

- **日期**：2026-08-06
- **背景**：把 `sample-basic.answers.json` 自己的 28 条建议灌进闸门②，
  1 条被丢弃、2 条被降级。
- **三条各自的性质不同**：
  ① `A4 "进行。"` 只有 3 字，低于 `min_span_chars: 4` 被丢弃——
     **答案清单自己违反了 prompt 里「太短无法定位」那条规则**。
     跨度改成「对所有出域数据进行。」即可，这是清单的错。
  ② 两条 A5（「提高…积极性和…水平」「降低了…可用性和运维成本」）
     的建议是**重写**不是最小修改，被闸门按 A5 的距离上限降级为批注。
     **这是闸门在正确工作**：A 类的定义是"有唯一正确答案"，
     一个动词辖两个宾语、只能靠重构才能改对的句子，本来就不该以修订形式落笔。
- **决策**：答案清单的每条正例增加 `expect` 字段（`revision` / `comment`），
  声明它经闸门②之后应当变成什么；回归据此断言。
  **不调整 A5 的距离上限**——那会让 A5 变成改写的入口。
- **为什么值得单列一条 ADR**：没有这个字段，回归只能断言"闸门没丢东西"，
  而降级恰恰是闸门最该做对的事。把期望写进清单，闸门放宽或收紧都会被立刻抓到。
- **影响**：`tests/fixtures/make_fixtures.py`、`sample-basic.answers.json`、
  `tests/run_regression.sh`。

## ADR-026　参考台账不得凭空造事实，且必须有负向对照

- **日期**：2026-08-06
- **背景**：为 `pass1-extract.md` 建实测基线时，把真实模型的抽取结果与
  `logic-injection.facts.json`（回归里"理想的 Pass 1 产出"）对比，发现 L23 的差异。
- **发现**：参考台账里有一条
  `{"subject":"数据层","status":"建设中","_anchor":"多租户隔离能力当前不支持"}`——
  **锚点句讲的是多租户隔离，与 subject 无关**。全文提到"数据层"只有一处，
  就是那条结论本身，文档里根本没有与之矛盾的状态句。
  把这一行删掉，L23 立刻归零：**这条规则的"检出"完全建立在一条杜撰的记录上。**
- **两件事都要修**：
  ① `logic-injection.docx` 补上真实的另一半（「数据层存储选型仍在评估中，尚未完成。」），
     L23 从此靠真实句子命中；
  ② 参考台账改为锚定该句，`status` 由"建设中"改为"未完成"。
- **守卫**：回归新增「参考台账的每条事实都能在正文中找到出处」——
  `_anchor` 必须命中真实段落，且 `subject`/`role`/`term`/`name` 必须出现在该段落里，
  `conclusions.scope` 必须出现在该段落或其标题路径中。
  **负向对照**：把那条伪造记录放回去，该检查必须失败（已验证，报错精确指到那一行）。
- **为什么必须有这道守卫**：参考台账是回归里 LLM 的替身。它一旦掺进文档里没写的记录，
  被验证的就不再是规则，而是那条杜撰——这与 `CLAUDE.md`「加校验时必须同时加负向对照」
  记录的 `validate_docx.py` 第一版是同一类错误：**校验对象里混进了本应被校验的东西。**
- **顺带补齐**：参考台账缺 L24 需要的枚举行（「上述两点」），
  导致 L11 与 L24 长期未被任何断言覆盖。回归的规则断言从 9 条扩到 12 条
  （加 L11 / L23 / L24）。
- **影响**：`tests/fixtures/make_fixtures.py`、`logic-injection.docx`、
  `logic-injection.facts.json`、`logic-injection.answers.json`、`tests/run_regression.sh`。

## ADR-027　闸门④的"盲"必须由脚本保证，不能靠 Agent 自觉

- **日期**：2026-08-06
- **背景**：准备测 M2 验收项「盲测 A/B 两种排列下判定一致率 ≥90%」时发现，
  这个指标**根本算不出来**——Pass 2 在脚本侧完全没有实现。
- **现状**：`workspace.py init` 往 manifest 写了 `ab_seed`，**全仓再没有第二处引用它**。
  SKILL.md 第 5 步要求 Agent 自己"合并待复核集、批量复核、写 issues-verified.jsonl"。
  与错别字通道当初的断法完全一致：种子预留了、prompt 写了，中间那段没有。
- **后果有三层，一层比一层严重**：
  ① 位置随机化实际由模型自己做，manifest 里的种子是摆设，可复现是假的；
  ② 没有任何地方记录原文当时在哪一侧，**M2 指标无法计算**；
  ③ **判定表要求"选中原文所在项才算通过"**——应用这张表的人必须知道答案。
     由 Agent 一边拼 A/B 一边应用判定表，盲测只剩名义：拼装者知道答案。
- **决策**：新增 `verify_pass2.py`，把"拼装"与"判定"拆到两个文件：
  `pass2-<排列>.json` 给模型（不含任何侧别信息），
  `pass2-<排列>.key.json` 给 merge（Agent 不该读）。
  位置按 `Random(f"{ab_seed}:{item_id}")` 逐条派生 → 同一 run 重跑得到同一排列。
  `--arrangement mirror` + `consistency` 子命令让 M2 指标第一次可计算。
- **实现中发现的泄题**（两轮才堵住，记下来免得以后改回去）：
  ① 上下文里那一段**正是原文所在的段落**，原样给出等于把答案写在题面上；
  ② 只挡本条不够——同一片里段落相邻，一批 10 组的上下文大面积重叠，
     **实测 15 条里 14 条的选项出现在邻条的上下文里**。
  必须把**全部待判跨度**（原文与建议）从**每一条**上下文里挡成 `【　】`。
  这类泄题失效得很隐蔽：一致率会漂亮得反常，看起来像模型判得准。
- **复核范围**：A 类走 A/B 对照，B 类走封闭单问；**C 类与 P 类不复核**——
  C 类不生成修订也不生成批注；P 类的裁定本来就是封闭题，且当时规则包的正反例
  在上下文里，Pass 2 没有规则包，再问一遍只会得到信息更少的答案。
- **负向对照**：判定表的六条淘汰路径（选中建议侧 / 两者都没有 / 两者都有 /
  无法解析 / 缺裁定 / 单问 UNSURE）在回归里逐条走通，不能只验通过路径。
- **影响**：新增 `scripts/verify_pass2.py`；`workspace.py` 的 `KINDS` 加 `verify`；
  `SKILL.md` 第 5 步、CLI 契约表；`prompts/pass2-verify.md`；`tests/run_regression.sh`。

## ADR-028　错词表必须配负向语料，否则"扩表不抬高误报率"这个前提不成立

- **日期**：2026-08-06
- **背景**：准备测 `pass1-typo` 裁定准确率时，先对词表做了一次子串误命中审计——
  写 23 句**完全合法**的文本，看有多少会撞上词表。**23 句全中。**
- **两类问题，性质不同**：
  ① **左串是常见词组的碎片**。「按全」会被「按全流程」拆出来，「备按」被「设备按标准」，
     「按排」被「按排班表」，「以经」被「以经验」，「换存」被「置换存储」，
     「符和」被「音符和节拍」。这类条目在真实公文里会持续误命中。
  ② **本身是收录在册的异体/变体**：交待、撤消、部份。按词表登记规则 3
     （异体不算错）本就不该进表。
- **为什么这必须修，而不是留给 LLM 裁定去救**：
  `CLAUDE.md` 把「错别字召回率」排在逻辑与语病之前，理由是
  **它靠词表这个确定性资产，扩表不抬高误报率**。
  一旦某条左串是碎片，这个前提就失效了——那条目既抬误报又消耗裁定调用。
  优先级序列里「误报率」本来就排在「错别字召回率」之前。
- **修法**：14 条改写成不会被拆开的长形式（同 `登陆系统`/`登陆账号` 的既有做法：
  `按全` → `按全生产`/`按全管理`/`信息按全`），5 条删除。
  改写后逐条验过：13 句真错字全部仍能命中，召回没有损失。
- **真正的产物是 `assets/dict/typo-traps.txt`**：把这些合法句子固化成负向语料，
  `typo_scan.py lint` 拿每条左串去撞它们，命中即报错（退出码 10）。
  **这与 never-flag 的反例、`validate_docx.py` 的负向对照是同一件事：
  只验"能查出错"不算验，还要验"不会把对的判成错"。**
  以后每加一条词都会自动撞一遍；在真实语料上发现新误报时，把那句话原样加进去。
  **语料只增不减。**
- **`lint` 增加 `--typos` / `--traps`**：负向对照要构造一份坏词表，
  而技能目录在运行期只读（回归第 1 节有断言），因此不能就地改内置词表。
- **影响**：`assets/dict/common-typos.txt`（231 条）、
  新增 `assets/dict/typo-traps.txt`（35 条）、`scripts/typo_scan.py`、
  `tests/run_regression.sh`、`CLAUDE.md` 测试一节。

## ADR-029　Pass 4 的准入策略必须 fail-closed，且三处调用点共用一份

- **日期**：2026-08-06
- **发现**：`apply_comments` / `apply_revisions` / `report` 三处各写了一遍
  `if v and v.get("verdict") == "NOT_CONFLICT": continue`。
  `v is None`（Pass 4 没跑、跑了一半、或那条漏答）时条件不成立，
  **候选照常写进文档**。实测：把 `conflicts-verified.jsonl` 清空，
  `apply_comments plan` 依旧计划写入 22 条批注——
  **「Pass 4 完全跳过」与「全部裁定为 CONFLICT」产出一模一样。**
- **同时，UNSURE 也没有被按判定表处理**：`prompts/pass4-adjudicate.md` 写明
  「UNSURE 按 NOT_CONFLICT 处理，仅 Critical 保留」，但代码里只排除 NOT_CONFLICT，
  于是全部裁定为 UNSURE 也是 22 条批注。
- **这违反了三处已经写死的东西**：底线三「不确定即无问题」、
  `logic-rules.md`「所有 L 规则的输出都是候选，必须过 Pass 4 裁定后才进交付物」、
  以及 prompt 自己的判定表。
- **决策**：策略收进 `_common.py` 的 `conflict_admitted(candidate, verdict)`，
  三处调用点共用。CONFLICT 准入；NOT_CONFLICT 丢弃；
  UNSURE 丢弃（Critical 例外，标注「需人工确认」）；
  **未裁定丢弃，Critical 也没有例外**——未裁定说明流程没走完，
  与"模型拿不准"不是一回事，不该享受同样的宽容。
- **但不得静默隐藏**：未准入的候选照常进报告与 `issues.xlsx`，
  并在报告「本次未覆盖范围」按原因计数。
  静默隐藏与 fail-open 一样糟——用户会以为文档里已经没有这些冲突了。
- **回归覆盖四条路径**（全 CONFLICT / 未裁定 / 全 NOT_CONFLICT / 全 UNSURE）
  加一条「未裁定的候选仍在报告中可见」。
- **教训与 ADR-026 同源**：同一份策略在三处各写一遍，就会有一处写错，
  而且错的那一处是**默认放行**。凡是"某某必须通过 X 才能进交付物"的规则，
  实现上都应该是一个准入函数，而不是三个排除条件。
- **影响**：`scripts/_common.py`、`apply_comments.py`、`apply_revisions.py`、
  `report.py`、`references/logic-rules.md`、`prompts/pass4-adjudicate.md`、
  `docs/SPEC.md` §9.5、`tests/run_regression.sh`。

## ADR-030　N7 的 fallback 压制必须与"是否动了该术语"挂钩

- **日期**：2026-08-06
- **背景**：审 `pass0-glossary` 时注意到术语表有个别处没有的性质——
  **它是唯一一个用来压制发现的输入**（N7 抑制误报、L05 概念族）。
  放行方向的 fail-open 会多写东西，压制方向的 fail-open 会少写东西，
  而后者在输出里没有任何痕迹。
- **发现**：`filter_neverflag` 的 N7 判据是
  `if term and len(term) >= 2 and term in normalize_key(text): return "N7"`——
  **只要跨度里出现该 fallback 术语就压制**。用户表里放一个「系统」或「平台」，
  技术文档里几乎每句话都含这两个字，于是含它们的发现全部被静默吞掉。
  实测：「通过帐号登录系统」「认真的完成了平台建设」「改善了平台的响应速度问题」
  三条全被 N7 丢弃。
- **这个场景很容易发生**，不是构造出来的：SKILL.md 明确建议用户把
  Pass 0 抽出的 `glossary.json` 人工修订后作为下一轮的输入，
  而 `glossary_scan.py` 的 `min_term_freq: 3` 预筛只会让高频通用词更容易入选。
- **决策**：fallback 层的意义是「别改它」，因此压制条件改为
  **建议里该术语没了**——说明这条问题确实要动术语本身：
  `if sugg and term in nt and term not in ns`。
  B 类没有建议、不改任何字，"别改它"对它不适用，因此不参与本条压制。
- **一个已知的边界**：把 fallback 术语「星云」改成「星云平台」时，
  术语仍是建议的子串，不会被压制。这类"要求展开别名"的情形由 N7 的
  别名组分支（`_alias_groups`）覆盖，暂不额外处理。
- **与 ADR-029 是一对**：那条是放行方向 fail-open（未裁定照样写进文档），
  这条是压制方向 fail-open（术语沾边就吞掉发现）。
  **凡是"某个条件成立就跳过检查"的判据，都要问一句：这个条件在真实语料上
  会有多大比例成立？** 若接近全集，它就不是过滤器，是开关。
- **影响**：`scripts/filter_neverflag.py`、`references/never-flag.md` N7 一节、
  `tests/run_regression.sh`。

## ADR-031　修订必须自带改动理由；批注范围必须覆盖完整正文

- **日期**：2026-08-10
- **背景**：用户实跑一轮后报了两个现场问题——修订处没有批注，看不出为什么改；
  部分批注"只选中前面几行"。两条都出在回写的最后一厘米：判定全对，交付物读不出来。

### 一、修订处没有批注（`apply_comments.py`）

- **原实现**：`if iid in revised: continue`——已落笔为修订的问题**不再出批注**，
  注释写的是"不再重复批注"。
- **这个前提是错的**：修订标记只显示"改成了什么"，不显示"为什么改"。
  评审人拿到一份只有修订的文档，只能整批接受或整批拒绝——判断又被丢回给人，
  而"降低人工复核成本"正是本技能存在的理由。
- **决策**：每一处落笔的修订同时写一条 `kind: "revision"` 的批注，正文只答"为什么改"：
  中文类别标签 + evidence + 末尾规则号。**有 evidence 时不重复"原文 → 改为"**，
  那是修订标记已经显示的东西；没有 evidence 才退回它。
- **锚定不能按原文定位**：改动落地后原文已进 `w:del`，而 `para_runs` 跳过修订标记内的 run，
  按原文定位必然失败、退回整段。改为 `apply_revisions.py` 把每处的 `del_id`/`ins_id`
  写进溯源，`apply_comments.py` 按 id 把范围精确套在 `w:del`+`w:ins` 外面。
  回归断言"拒绝视图圈住的正是原文、接受视图正是建议"。
- **顺带修掉一个静默丢失**：原先 `revised` 取自**补丁清单**，
  于是"计划了修订但定位失败"的条目既没有修订也没有批注。
  改为取溯源里的 `applied`，没落笔的照常出普通批注。
- **计量**：`metrics.py` 的 `actions["comment"]` 排除 `kind: revision`，
  否则同一条问题会在 revision 与 comment 上各记一次。

### 二、批注只圈住前面几行（`ooxml.py`、`apply_comments.py`）

- **根因是把两件事当成了一件**：定位只能在**可拆分 run**（不在修订标记内、
  只含 `rPr` 与 `w:t`）上做——那是拆 run 时保证 rPr 可深拷贝的要求；
  但原实现直接拿这批 run 的首尾当**批注范围的边界**。
  于是含 `w:br`/`w:tab`/图形的 run、以及既有或刚写入的修订，全被排除在范围之外。
  三种现场（旧实现实测）：

  | 段落长相 | 旧实现圈住 | 应为 |
  |---|---|---|
  | 末尾 run 含软换行 | `前文` | `前文后文` |
  | 整段文字在一个含 `w:br` 的 run 里 | `` （零长度） | `第一行` |
  | 每行都带软换行 | `第三行` | `第一行第二行第三行` |

- **决策**：新增 `ooxml.comment_range_nodes()`，范围边界一律取 **`w:p` 的直接子节点**
  （`commentRangeEnd` 插进 `w:ins`/`w:del` 内部会变成修订的一部分）；
  锚点定位不到或没给锚点时退回**整段**，而不是退回"可拆分 run 的首尾"。
- **边界对齐到 run，因此实际圈住的可能比锚点多**。这是刻意的：
  为精确到字符而拆 run，是拿"样式不变性"换"定位准确率"，与优先级序列相反。
  **多圈安全，少圈会让评审人看不出批注在说哪一段。**
- **另一处是纯粹的截断**：冲突批注的锚点写的是 `paras[pid]["text"][:40]`——
  40 字在 Word 里就是一两行。改为取该侧句子，取不到才整段。

### 三、加校验必须同时加负向对照（`validate_docx.py`）

- 原来的第四项只查"每个 `commentReference` 都有配对的 Start/End"。
  **锚定"存在"不等于锚定"圈对了"**：上面三种现场全都 Start/End 齐备，校验全绿。
- 补两条，都基于磁盘上的文档现场重算（`ooxml.comment_coverage`）：
  范围内必须有正文；范围必须覆盖完整锚点。
  后者拿 `commentlist.json` 的计划锚点做预期——它由 `plan` 写、不是 `apply` 的自述，
  因此不违反"不读回写时的溯源记录"那条（ADR-007 的教训）。
- 范围内含刚写入的修订时，原文只在"拒绝修订"视图里连续，两个视图任一命中即算覆盖。
- 两条负向对照进回归：把范围塌成零长度、把范围缩到只剩第一个 run，
  都必须以退出码 8 被抓到。后者的报错信息就是用户报的那句话。
- **影响**：`scripts/ooxml.py`、`apply_comments.py`、`apply_revisions.py`、
  `validate_docx.py`、`metrics.py`、`SKILL.md`、`references/ooxml.md`、
  `tests/run_regression.sh`（103 → 107 项）。

## ADR-032　批注正文只留信息量，锚定精确到字符

- **日期**：2026-08-10
- **背景**：ADR-031 上线后用户又看了一轮，提了两条——批注抬头有套话，
  以及「的/地/得误用」那条圈住了接近整段 215 字，实际有问题的只是其中一句。

### 一、抬头只留类别标签

- 去掉两处套话：修订理由批注的 `— 已在此处标为修订，请确认后接受或拒绝`，
  以及冲突批注的 `— <规则通用描述>`。
- **判据是"这句话有没有增加信息"**：批注就锚在修订上，"已在此处标为修订"是废话；
  "术语不一致 — 同一术语出现多个不同定义"两半说的是同一件事，
  下一行的 note 才是本条的具体内容。
- 批注正文的可读性不是文案偏好，是有效性问题：**写长了评审人就不看了**，
  等于回到"只有修订标记没有理由"的状态。
- 同一判据下互斥型冲突的 `　此处：…` 一行也去掉了：锚定精确到字符之后
  （见下一节），本处那一句已经被高亮圈住，批注里再抄一遍就是重复。
  只留 `与「<章节>」（第 N 页）的描述不一致：<另一处原文>`。
  **这一条依赖锚定精度**——范围还圈着整段时，"此处"是必要的定位信息；
  精确了它才变成冗余。两件事一起改才成立。

### 二、锚定精确到字符（`ooxml.isolate_span`）

- **ADR-031 的判断在这里是错的**。当时写「边界对齐到 run，实际圈住的可能比锚点多，
  这是刻意的：为精确到字符而拆 run，是拿样式不变性换定位准确率」。
  真实文档上这个"多一点"是**整段**——`unpack.py` 的 run 合并（513→55）
  正是为了让定位可做，代价是合并后一整段常常只剩一只 run。
  于是"多圈一点"实际等于"圈住全段"，等于没有锚定。
- **而这个取舍根本不成立**：切出来的每段文本逐字不变、`rPr` 深拷贝自原 run，
  "拒绝修订视图的逐字符 (字, rPr)"与"每段用到的 rPr 指纹集合"都不变——
  正是 D9 校验的两个判据。这跟修订回写用的是同一套机制（`new_run` + `clone_rpr`），
  写错了会被同一套负向对照抓住。**不是拿样式换定位，是我没意识到它已经被证伪机制覆盖。**
- 实现只切边界的两只 run，中间的原样保留；先切尾再切头（单 run 跨度时是同一只，
  先切头会让尾部偏移失效）。定位不到仍退回整段。
- 回归加两条：真实文档上"锚点是段落里一小截"必须圈住恰好那一截；
  单 run 长段落（134 字）里的 11 字锚点必须圈住 11 字，且正文与 rPr 指纹不变。
- **教训**：优先级序列（样式不变性 > 定位准确率）是用来裁"两者确实冲突"的，
  不该拿来给"我没想清楚证伪机制"当挡箭牌。先问一句"现有校验能不能抓住做错的情形"，
  能抓住就不是取舍。
- **影响**：`scripts/ooxml.py`、`apply_comments.py`、`references/ooxml.md` §4.1/4.3、
  `tests/run_regression.sh`（107 → 108 项）。

## ADR-033　术语类规则整组可关，默认关闭

- **日期**：2026-08-11
- **背景**：用户明确「术语的检查先跳过，暂时不需要这部分检查」。
- **范围**：按 `references/logic-rules.md` 的分组取两组共七条——
  **术语与命名** L01（术语定义不一致）/ L02（缩略语不一致）/ L03（缩略语未展开）/
  L04（名称写法不一致）/ L05（近义术语混用），
  **术语规范** L25（禁用写法）/ L26（变体写法）。
  由 `logic.term_rules` 一个开关统一控制，默认 `false`，改 `true` 即恢复，不改代码。
- **Pass 0 的术语抽取一并跳过**。它不是检查，是喂这七条规则的输入
  （2000 页约 10 次调用）；规则不跑，抽取就是纯浪费。
  `glossary_scan.py` 自己判断并返回 `term_rules: false`，
  不依赖 Agent 记得跳过——与 `scan_patterns.py` 在 `pattern_review.enabled=false`
  时的处理一致。
- **`import_glossary.py` 不受影响**：用户自备的术语表照常导入，
  因为它还有另一个消费者——不改清单 N7 靠它避免把用户的专有写法当语病改掉。
  **关掉"查术语"不等于关掉"别改术语"**，这两件事方向相反（见 ADR-030）。

### 关掉必须留痕，否则等于没关

- 下游 `apply_comments.py` / `report.py` 是按 `glob("conflicts-candidate.*.json")`
  读候选目录的。**只是不跑规则的话，上一轮留下的候选文件照样会被读进文档**——
  用户改了开关重跑，术语批注却还在。
  因此关闭时把这七条的候选文件**清成空并标 `skipped_reason`**。
- 反向同理：空文件不能被"已完成的规则不重算"当成已完成，
  否则把开关重新打开也不会重算。判据加了 `not prev.get("skipped_reason")`。
  回归里这两条各有一条断言，且"打开后 L01 重新检出"就是反向路径的证明。
- **不静默隐藏**（ADR-029 的规矩）：报告的「本次未覆盖范围」新增一行写明这一维度没查；
  「术语表覆盖率」一节加提示，避免被读成"术语已核对过"；
  结尾的"下一轮更准"不再推销权威术语表——规则关着时传了表也不会有术语类发现，
  那是误导。
- SKILL.md 补一条判断题：**用户明确提到术语要不要统一时，把开关打开再跑，
  不要在关闭状态下含糊带过**。技能的 description 仍保留"术语是否统一"作为触发词——
  能力还在，只是默认不跑。
- **影响**：`assets/config.default.yaml`、`scripts/detect_conflicts.py`、
  `glossary_scan.py`、`report.py`、`SKILL.md`、`references/logic-rules.md`、
  `tests/run_regression.sh`（108 → 110 项）。

## ADR-034　保留范围与复核范围必须是两张表；跨度上限与动作按类别分离

- **日期**：2026-08-12
- **背景**：一次从设计到实现的通读。四条缺陷互相独立，但都指向同一个形状——
  **某个为 A/B 类定的判据被无差别地套到了所有通道上**。

### 一、闸门④ 的 strictness 表同时被当成了"要不要留下"

- **发现**：`verify_pass2.collect()` 按 `STRICTNESS_SCOPE` 过滤，而 `merge()`
  **只写 `collect()` 的结果**到 `issues-verified.jsonl`——那是 report / apply_comments /
  apply_revisions / metrics 四处的**唯一入口**。该表三档都不含 `P1` / `C1` / `C2`。
- **后果实测**（输入 A2 + C1 + P1，三档输出均为 `['A2']`）：
  ① **P 类整条通道是断的**。范式支线过完两道闸门、产物写进 `issues-0001.patterns.jsonl`
     之后原地蒸发，需求 5 在交付物和报告里都看不见。报告 §6 还会显示
     「适用段落 N / 要件缺失 0」——读起来像"查过了，没问题"。
  ② **`thorough` 与 `balanced` 完全等价**，C 类连报告都进不去，
     而 `taxonomy.md` 与 `pass2-verify.md` 都写明它"只进报告"。
- **为什么回归没发现**：`run_regression.sh` 第 9 节到
  `issues-0001.patterns.jsonl` 为止就停了，从没跨过 `verify_pass2`。
  110 项里没有一项走完"支线 → 闸门④ → 交付物"这条整链。
- **决策**：拆成 `REVIEW_SCOPE`（进不进闸门④）与 `KEEP_SCOPE`（留不留在漏斗里）
  两张表。`collect()` 用后者，`build()` 用前者；不复核的类别走
  `merge()` 里既有的 `not_reviewed → result: pass` 通路（那条通路本来就在，
  只是永远走不到）。P 类恒在保留范围内——它由 `pattern_review.enabled` 控制，
  与 strictness 无关。
- **与 ADR-029 同源**：那条是"准入策略写成三个排除条件，于是有一处默认放行"；
  这条是"保留策略借用了复核范围的表，于是有两类默认丢弃"。
  **凡是决定"什么能进交付物"的集合，都必须是一个显式的、单独命名的东西。**

### 二、`max_span_chars` 是给 A/B 类定的，却作用于 P 类

- `verification.max_span_chars: 120` 的判据是「跨度太长说明模型在圈整段」。
  但 P 类的 `original_text` **按设计就是整段**（`scan_patterns.merge`）——
  它问的是"这一类段落该有的要件齐不齐"，不是"这句话哪里写错了"。
- **实测**：155 字的风险段落 → `length_drop: 1`。真实可研/方案里的风险条目、
  接口描述普遍超过 120 字，就算修好第一条，P 类也留不下几条。
  且这个丢弃只落在 `metrics.json` 里，报告中不露面（违反 ADR-029「不得静默隐藏」）。
- **决策**：新增 `pattern_review.max_span_chars`（默认 `0` = 不限），P 类走它。
  **不动 `verification.max_span_chars`**——放宽它会让 A/B 类失去"在圈整段"这个判据。
- **这是 ADR-022 没走完的另一半**：那条把侧通道的**输出文件与配额**分开了，
  **阈值没分开**。配额、输出、阈值三者是一套东西。

### 三、闸门②无条件重算 action，把上游的 report_only 升成了 comment

- `verify_span` 原先写死 `"action": "revision" if sugg else "comment"`，覆盖入参。
- 后果：`scan_patterns.merge` 的「只缺可选要件 → 降为只进报告，不打扰评审人」
  失效；规则包里写 `action: report_only` 同样失效——`references/patterns.md`
  把它列为合法取值，实际是空头支票。
- **决策**：上游显式声明 `report_only` 时原样保留；C 类无条件置为 `report_only`
  （`taxonomy.md` 写死"不生成修订也不生成批注"，此前它靠"下游读不到"来实现，
  第一条修好后就会真的写进文档）。

### 四、主流程从来没有取过租约

- `SKILL.md` 全文没有一处 `workspace.py lease acquire`，但第 6/7/8 步要求带
  `--session` 调 `detect_conflicts` / `apply_revisions` / `apply_comments` /
  `state.py heartbeat`——这些都走 `lease_verify`。
- **实测**：`owner.json` 不存在时 exit 9，文案是
  「本会话写入权限已失效——该文档已被另一个会话接管」。
  而 `SKILL.md` 的决策点规定 exit 9 不重试不降级、直接转达用户。
  **照文档跑一遍标准流程，结果是在 Pass 3 处以一句与实情完全相反的话终止。**
  `--generation <n>` 的取值来源同样没有出处：`init` 的返回里不含 generation。
- **决策**：`init` 的 `created` / `resumed` 两个分支补上 `lease` 与 `next`
  （取租约的完整命令）；`SKILL.md` 第 0 步补一步 acquire，并说明
  `owner.generation` 就是后续 `--generation` 的取值；exit 9 的决策点补一句
  「先用 `lease status` 分清是没取还是被接管」。
  **不在 `init` 里自动取租约**——并发时要先把现状呈现给用户再由他选
  （加入协作 / 接管 / 独立重跑），自动取会把这个交互吞掉。
- **教训**：`workspace.py` 的租约实现是完整的，回归第 6 节也测过令牌栅栏，
  但**没有一项测试是"照着 SKILL.md 的主流程从头跑一遍"**。
  单元层面全对，串起来第一步就断。

### 回归

新增第 14、15 两节共 11 项（110 → 121）。四条修复各配负向对照，
已逐条验证：回退修复后对应断言全部失败，且报错精确指出是哪一条
（「P 类过完两道闸门后在 Pass 2 合并处整组消失」「C 类动作被升成了 comment」
「只缺可选要件的降级被闸门②覆盖成了 comment」「整段 P 类被跨度上限误杀」）。
长度那条同时验反向：同样长度的 B 类仍必须被丢弃。

- **影响**：`scripts/verify_pass2.py`、`verify_span.py`、`workspace.py`、
  `report.py`（P 类的报告分类标签）、`assets/config.default.yaml`、
  `SKILL.md`、`references/patterns.md`、`taxonomy.md`、
  `prompts/pass2-verify.md`、`tests/run_regression.sh`。

## ADR-035　闸门③ 的位置类规则判的是区域，不是整段

- **日期**：2026-08-12
- **背景**：ADR-034 通读时记下、本轮处理。N7 在 ADR-030 里已经按"压制条件不能接近
  全集"修过一遍，N9/N10/N12 还是老样子。
- **发现**：三条规则的原文都是"这个**区域**内的内容不审查"——代码块、引文、
  图表标题各自是一块区域。但实现是拿**整段文本**去撞正则，命中即丢弃该段的**全部**发现：
  - `[A-Za-z_][\w.\-]*\s*=\s*\S+`：段里出现一次 `cpu_usage=80%`，整段免检；
  - URL 正则：技术文档里带一条链接的段落非常常见；
  - `(?:法|条例|办法|规定|标准|规范|合同|协议)\s*第…[条款项]`：
    「按照本办法第五条执行，各部门应当认真的落实审批流程」这种**作者自己写的句子**
    会整段进 N10 豁免；
  - `\{[^{}]*\}\s*$`：以花括号结尾的段落；
  - `^\s*(?:图|表)\s*[0-9]+\s`：「表 3 中列出的各项指标改善了…问题。」被当成表题。
- **决策**：新增 `exempt_regions()`，返回段落内被豁免的**字符区间**；
  `check()` 命中的条件改为「跨度与某个豁免区间有重叠」。
  - 整段豁免仍保留，但只给真正以整段为单位的情形：`is_code`（`extract.py`
    已按中文字符占比排除散文）、`is_quote` 样式、整段被引号括住、
    以及**没有句末标点**的标签行（真正的图表标题都没有 `。`）。
  - 片段豁免给其余情形：命令/路径/URL/`key=value` 的匹配区间、条号本身、
    引号括住的引文本体、以及「《X 法》第 N 条规定：」之后到段末
    （引文常常不带引号，只靠这个冒号分界；**没有援引标志时不适用**，
    否则任何「要求：」都会让后半段免检）。
  - 跨度定位不到时退回旧的整段语义。闸门②本应保证能定位，这条只是兜底，
    方向选压制而非放行（误报率在优先级序列里排在召回率之前）。
- **先有语料，再动规则**：新增 `tests/fixtures/neverflag-traps.json`（21 条，
  正向 13 / 反向 8）与 `filter_neverflag.py --traps`。这与 ADR-028 给错词表配
  `typo-traps.txt` 是同一件事：**只验"能压住"不算验，还要验"不会把对的压住"。**
- **语料当场撞出一个存量漏洞**：`never-flag.md` 自己举的反例
  「合同第 3.2 条约定：乙方应于验收合格之日起十五个工作日内交付。」
  **旧实现根本匹配不上**——条号正则不接受小数分节（`第[〇零…0-9]+[条款项]` 撞上
  `3.2` 的小数点即失败），于是这条文档里写着的保护从来没有生效过。
  已把条号改为 `第\s*[…0-9]+(?:\.[0-9]+)*\s*[条款项]`。
  写这份语料的收益，第一条就抵回来了。
- **负向对照**：把 `_span_range` 打桩成"定位不到"，即退回整段语义，
  这组语料必须失败（实测 8 条反向全部失败，报错精确到句）。
  另有一组同段对照：同一个段落里，落在豁免区域外的语病保留、落在区域内的压制。
- **影响**：`scripts/filter_neverflag.py`、`references/never-flag.md` N9/N10/N12、
  新增 `tests/fixtures/neverflag-traps.json`、`tests/run_regression.sh`（121 → 125）、
  `CLAUDE.md` 的目录地图与测试一节。

## ADR-036　跨度在段内不唯一时不落笔；术语规范化改为整段替换

- **日期**：2026-08-12
- **背景**：ADR-034 通读时记下的最后一条。优先级序列里「定位准确率」排第三，
  但这条路径上既没有守卫，也没有负向对照。
- **现场**：`ooxml.locate_span` 取**首个**匹配，而问题记录里只有 `pid` 与
  `original_text`，没有偏移量。`logic-injection.docx` 里就有现成的一段：

  > 「本期指标目标为 **200ms**，实测值为 1**200ms**。」

  把「200ms」改掉，**改中的是目标值还是实测值，取决于 `find` 而不是取决于判定**。
  回归里已固化成负向对照：把守卫打桩关掉，改中的确实是目标值那一处。
- **决策**：新增 `ooxml.span_count()`，`apply_revisions` 据此分三种处置——
  唯一则落笔；带 `all_occurrences` 则逐处全换；其余**不落笔**，
  记为失败并附出现次数。未落笔的条目照常出普通批注（ADR-031 修过的那条通路），
  不静默消失。这是底线三「不确定即无问题」在定位这一层的落实。

### 顺带修掉的存量缺陷：L25/L26 只换首处

术语规范化的语义是"这一段里的这个禁用写法**全部**换掉"，不存在"改哪一处"的问题。
但原实现对每条候选只 `locate_span` 一次——**换完首处就收工，剩下的原样留着，
文档会停在半规范化状态**。若只加唯一性守卫而不处理这一条，L25/L26 会从
"换一半"退化成"一处也不换"，那是拿一个缺陷换另一个。

因此 `detect_conflicts` 给 L25/L26 的 `suggest` 打上 `all_occurrences: True`，
`apply_revisions` 循环替换。循环不需要额外记账：换完一处后原文已进 `w:del`，
而 `para_runs` 跳过修订标记内的 run，下一轮 `locate_span` 自然找到下一处。

### 批注锚点的歧义判定必须在 plan 里做

第一版把判定放在 `_anchor_paragraph`（apply 阶段），回归立刻抓到：
**那时段落已经被修订改过**，被替换掉的那一处进了 `w:del`、不再参与定位，
于是 `span_count` 返回 1，落笔精确圈住了剩下的那一处——
计划说"说不清是哪一处"，产出却指着其中一处。

改为在 `build_plan` 里按**原始段落文本**判定（`_anchor_or_whole`）。
`commentlist.json` 由 `plan` 写、`validate_docx` 也拿它做预期，三者必须同源。
**圈错一处比圈住整段更糟**：评审人会照着高亮去找问题，而问题不在那里。

### 错别字通道：候选窗口撑到唯一

`typo_scan` 原先取固定 ±6 字窗口。整句重复的段落里（「系统布署完成后需要复核。
系统布署完成后需要复核，并记录结论。」）这个窗口不唯一，产出的候选**注定落不了笔**——
白白消耗一次裁定调用。改为一路撑到段内唯一（上限 ±40）。
窗口撑宽后同一窗口里可能出现两次错词（「1200ms」里就含着「200ms」），
因此候选记下 `wrong_at` 偏移，`merge` 按位置替换而不是全局 `replace`。

- **负向对照**（三条，均已验证回退后失败）：
  ① 打桩 `span_count` 恒为 1 → 歧义跨度改中目标值那一处；
  ② 去掉 `_anchor_or_whole` 的歧义判定 → 批注圈住其中一处而非整段；
  ③ 窗口退回固定 ±6 → 重复句里的候选窗口不唯一。
  另有正向：整段替换必须覆盖全部两处、拒绝修订视图逐字还原、四项校验仍全过。
- **影响**：`scripts/ooxml.py`（新增 `span_count`，顺带删掉 `locate_span` 里
  两个分支都 `return None` 的死代码）、`apply_revisions.py`、`apply_comments.py`、
  `detect_conflicts.py`（L25/L26）、`typo_scan.py`、`references/ooxml.md` §2.1、
  `tests/run_regression.sh`（125 → 134）。

## ADR-037　三处小口子：台账按内容判重、支线单独计量、回归依赖预检

- **日期**：2026-08-12
- ADR-034 通读时记下的三条，单独拿出来一次清掉。共同点是**留了字段/参数但没接上**。

### 一、`ledger.ingested.sha` 写空串且从不比对

`build` 只按 `chunk_id` 判重。分片失败重跑时 `facts-<chunk>.json` 会被整个改写，
但库里留的是**上一轮的旧事实**——而 Pass 3 的全部 L 规则都建立在这份台账上，
结论会指向文档里已经不存在的内容。`sha` 这一列本来就是为此留的。

改为按文件 sha256 判重；内容变了就先 `DELETE FROM facts WHERE chunk_id=?`
再重新导入，保持幂等。回归加三条（内容变则重新入库 / 不产生重复行 / 未变则不重复入库），
并验证过回退后前两条会失败。

### 二、`metrics.py bump --pass` 的枚举里没有支线

错别字有 `typo`，范式**一个都没有**，SKILL.md 第 4.5 步也没让 Agent 对支线计量。
两条支线每片各多一次调用，混进 `pass1_review` 就算不出耗时占比——
而 M7 压测的产出之一正是这个。加 `pattern`，SKILL.md 补一句。

### 三、回归缺依赖预检

缺 `lxml` 时脚本照跑，结果是 60 个失败 + 满屏 traceback，而真正的原因只有一句话
（本轮开场就踩了一次）。开头加一句预检，退出码 3 = 环境缺失，与脚本自己的约定一致。

- **影响**：`scripts/ledger.py`、`scripts/metrics.py`、`SKILL.md` 第 4.5 步、
  `tests/run_regression.sh`（134 → 137）。

## ADR-038　送审文档已带批注时不得覆盖；计数口径必须与文档一致

- **日期**：2026-08-12
- **背景**：第二轮通读。前一轮盯的是"通道断没断"，这一轮盯的是
  **"输入不是白纸时会怎样"**——六份 fixture 里没有一份带既有批注或既有修订，
  这条路径从没被走过。

### 一、审查一份已经带批注的文档，会把原有批注全部抹掉（且四项校验全绿）

- **复现**：在 `sample-basic.docx` 上挂一条「张三：这一段请业务方再确认一次。」
  （id=1），跑完批注回写后：

  ```
  id=1 author=内容审查 text=[提示] 指代不明（检测规则 B1）
  ```

  张三的批注**连人带话没了**，而 `validate_docx` 四项全过、退出码 0。
- **两层原因**：
  ① `apply_comments` 另起一份 `comments.xml` 整份写入，既有批注不在其中；
  ② 编号从 1 开始，与正文里既有的 `commentRangeStart id=1` 撞号——
     旧锚点转而指向新批注。
- **为什么校验看不出来**：第四项查的是「每个 `commentReference` 都有配对的
  Start/End 且 id 在 comments.xml 中声明」。撞号之后 `refs` 与 `declared`
  都含 "1"、覆盖范围也非空，**每一条都成立**。
  这与 ADR-008（D9 校验读溯源而非现场）、ADR-031（锚定"存在"不等于"圈对了"）
  是同一类错误的第三次出现：**校验的判据描述了结构，没有描述"用户的东西还在不在"。**
- **决策**：
  - `apply_comments` 在既有 `comments.xml` 上**追加**；编号从
    `max(既有 id) + 1` 起（`_next_comment_id`，同时看正文锚点与 comments.xml）。
  - 增强层降级时**恢复原内容而不是删文件**——那些部件可能是文档自带的
    （别人的已解决标记、回复线程），删掉等于替用户丢数据。
  - `unpack` 存下 `comments.baseline.xml`，`validate_docx` 据此新增一项：
    **源文档原有的每条批注必须仍在，且正文逐字不变**。判据取自回写前的现场，
    不是回写时的自述。
  - `check_structure` 增加批注 id 唯一性检查（`commentRangeStart` 与 `comment` 各一条）。
  - 新增 fixture `existing-comments.docx`（`make_fixtures.py` 里只用 lxml 生成，
    因此顶层的 python-docx 导入改为惰性）。
- **负向对照**：退回"另起一份"后，三条断言全部失败，**其中包括第四项校验
  以退出码 8 抓住**——这正是原实现缺的那道。

### 二、截断的去重键漏掉了原文

`(pid, category)` 会把同一段落里同一类别的**不同**问题折叠成一条，
回填时被当成"已选过"跳过。实测：`cap=3`、输入 1×A2 + 5×B1（同一 pid），
**只保留 2 条**——配额没用满，还丢了一条真问题。
改为与分片内去重同源的 `(pid, category, normalize_ws(original_text))`。

### 三、未准入的冲突候选被记成了「批注」

`report.py` 的「交付动作」直接取候选的 `action`，而未过 Pass 4 准入的候选
**并没有写进文档**。实测 28 条被记成 `comment`——
而 SKILL.md 要求 Agent 正是照着这份报告向用户口头汇报。
改为 `admitted is False` 时记 `report_only`；它们仍照常出现在报告与 xlsx 里
（ADR-029：不静默隐藏），只是不再虚报批注数。

- **影响**：`scripts/apply_comments.py`、`unpack.py`、`validate_docx.py`、
  `verify_span.py`、`report.py`、`tests/fixtures/make_fixtures.py`、
  新增 `tests/fixtures/existing-comments.docx`、`references/schemas.md`、
  `tests/run_regression.sh`（137 → 144）。
## ADR-039　长文档的成本在「工具轮次 × 上下文」，不在脚本

- **日期**：2026-08-18
- **背景**：第一次真实压测——801 页、211,573 字符——跑了 4 小时仍停在第 4 步，
  31 片里第一轮的 6 片都没做完，部分子 Agent 的上下文已到 175k。
  脚本侧全程只占几十秒，瓶颈完全在流程形状上。三个独立成因：

  1. **片数被虚高的 token 系数抬了两倍。** `_estimate` 已经把 CJK 逐字记 1 token
     （真实 tokenizer 约 1 token / 1.4–1.7 字），`token_fallback_ratio` 又乘 1.6，
     等于一个汉字算 1.6 token。叠加「无条件扣掉 4000 术语摘要预算」
     （`term_rules` 默认关，根本没有术语摘要）与切点比例 0.5（标题一多就断，
     每片只装半程），实测同一份 800 页正文切出 37 片，改后 19 片。
  2. **子 Agent 连做多片，上下文平方级累积。** 技能的 prompt 契约写的是
     「每次调用无状态、自包含」——那是对 API 调用成立的，但执行载体是一个持续对话的
     子 Agent，类型体系、不改清单、每片正文与三次调用的产物全都留在同一个上下文里。
     连做 5 片之后，第 5 片的每一轮工具往返都要重算前 4 片。
  3. **每片 20+ 轮工具往返里只有 3 轮在做审查。** 其余是每次调用前后各一次
     claim 续期（6 轮）、六个闸门脚本各一轮、以及「逐行写入」「逐条 append」
     这类措辞诱导出的逐行落盘。每一轮都要重算当下的全部上下文。

- **决策**：
  - 系数改 1.0；术语摘要预算按「有没有术语表」计；新增
    `split_point_fill_ratio`（默认 0.7）；`max_text_tokens` 维持 15000——
    系数修正后它的含义从「约 9400 汉字」变成「15000 汉字」，片本身已经大了 1.6 倍。
    同步把 `max_issues_per_chunk` 20→40、`min_b_class_slots` 5→10、
    错别字配额 100→200，**片变大而上限不动等于拿召回换速度**。
  - 子 Agent 改为**一片一个、做完即退**；`renew_claim_on_llm_call` 关掉，
    `chunk_claim_minutes` 20→60 覆盖单片全程；闸门脚本用 `&&` 串成一条命令；
    支线候选扫描改为整篇跑一次；写盘一律整块写。
  - 闸门④与 Pass 4 按批分文件：`verify_pass2.py build` 额外落
    `pass2-<排列>.vNN.json`，`merge` 收所有分批裁定文件；新增
    `adjudicate_pass4.py`（`build` / `collect` / `status`）承担 Pass 4 的题面拼装与归并。
    两步都从「主 Agent 串行、把整份候选读进上下文」变成「子 Agent 并行、各读一批」。
- **备选**：只调 `parallelism`（并行度不是瓶颈，轮次与上下文才是）；
  把三次调用合并成一次（违反「一次调用只做一件事」，且实测会互相污染）；
  把 `max_text_tokens` 推到 20000 以上（片越大定位准确率越掉，
  而定位准确率的优先级高于所有召回指标）。
- **顺带修掉的两个存量缺陷**：
  1. **重新分片不清旧产物 = 静默跳片。** `chunk.py` 只删 `chunk-*.txt`，而续跑判定是
     「文件存在性即状态」，于是换了切法之后新的第 1 片顶着旧的 `issues-0001` 被判为
     已完成，那部分正文再也不会被看到，输出里没有任何痕迹。现按 chunk 签名比对，
     内容变了就作废它的全部产物（含两条支线与 claim），并在返回里报数。
  2. **`load_config` 不读 run 的配置快照。** 「init 时带了 `--config`、后面某一步忘了带」
     会得到静默的半生效状态：chunk.py 按新预算切、verify_span.py 按旧上限截，
     两边都不报错。新增 `load_run_config`，缺省回落到 `config.snapshot.yaml`，
     全部带 run 目录的脚本改用它。
- **影响**：`assets/config.default.yaml`、`scripts/chunk.py`、`scripts/tokenizer.py`、
  `scripts/workspace.py`、`scripts/verify_pass2.py`、`scripts/adjudicate_pass4.py`（新增）、
  其余 14 个脚本的 `load_run_config` 切换、`SKILL.md` 第 3/4/4.5/5/7 步与并发一节、
  `prompts/pass1-review.md`（`{{MAX_ISSUES}}`）/`pass2-verify.md`/`pass4-adjudicate.md`、
  `references/schemas.md`、`references/taxonomy.md`、`references/never-flag.md`、
  `references/logic-rules.md`、回归第 20–22 节（新增 27 项）。
## ADR-040　子 Agent 的上下文是硬约束：一个单元 = 一次调用 = 一份产物

- **日期**：2026-08-18
- **背景**：ADR-039 的改动之后，在 OpenCode 与 Deepseek 两个 harness 上各跑了一次
  同一份 800 页文档。分片降到 18 片（预期内），但**两次都没跑完**，
  现象是子 Agent 频繁失败，已定位两条原因：上下文溢出，以及运行环境抖动。
  拆开看是四件事：

  1. **一个子 Agent 领一片、做三次调用**，它的上下文要同时装下 `taxonomy.md`
     + `never-flag.md` + facts schema + 整片正文（还常常读两遍）+ 错别字候选
     + 三份产物。窗口偏小的子 Agent 直接溢出。
  2. **错别字候选主文件里同一批数据存了两份**（`candidates` 与 `batches[].items`），
     200 个候选就是 8 万字符；子 Agent 读整份，上下文当场见底。
  3. **`claim next` 把整条 chunk 元信息原样吐出**，其中 `pids` / `review_pids` /
     `context_pids` 在长文档上是几百个条目。技能自己定的「不打印大对象」，
     claim 是第一个破的。
  4. **失败的代价是一整片**：环境抖一下，这一片的三次调用全白跑；而崩掉的子 Agent
     还占着 claim 直到 TTL（当时是 60 分钟）到期，整轮都在等一个不会回来的活。

- **决策**：把 Pass 1 的最小单位从「分片」降到「一次调用」。
  - `workspace.py` 新增阶段化单元（`stage_units` / `next_pending_unit` /
    `reclaim_stage`），claim 粒度变成 `<阶段>-<单元>`；`claim next --stage` 只返回
    `{unit, chunk_id, stage, prompt, output}`，不再吐任何列表。
  - 新增 `prompt_pack.py`：把每一次调用要用的东西预先拼成**一个自包含的 prompt 文件**。
    子 Agent 的动作退化成「读一个文件 → 作答 → 写一个文件」，
    它既不需要知道技能目录在哪，也没有机会顺手读进 `references/` 下的大文件。
    实测单个子 Agent 的输入上界从「全套 references + 正文 + 候选」降到约 2.3 万字符。
  - 错别字与范式的候选**按批分文件**（`typos-<片>.<批>.json`），题面只留四个字段；
    裁定按 `tid` 回填（原先要回填 pid/wrong/context 三个字段，抄错一个字这条就静默消失）。
  - 闸门改为整轮收口：`verify_span.py --all --channel main|typos|patterns`
    与 `filter_neverflag.py --all --channel …`，一条命令扫完全部分片——
    闸门是纯脚本，按片各发一次工具调用等于白白多 N 轮往返。
  - `chunk_claim_minutes` 60 → 30：单元变小了，锁死的窗口也该同比例变小。
- **备选**：继续压缩 `taxonomy.md` / `never-flag.md`（削弱的是第一道人工判据，
  而闸门③本来就是脚本在兜底，收益不值得）；把分片切得更小（片数即调用数，
  且定位准确率会掉）；提高并发（失败的单元要重派，重派比串行更贵）。
- **顺带修掉的一个存量缺陷**：**「文件存在」不等于「做完了」**。脚本写盘走
  `atomic_write_*`，半写不会表现为完成；但子 Agent 是用 shell 写的，被杀在中途就会
  留下一个存在但截断的文件，而续跑判定只看文件在不在。更糟的是 `read_json` 对坏 JSON
  静默返回默认值——整片事实凭空消失，报告里也看不出来。新增 `_common.product_ok`，
  阶段完成判定改为「存在且可解析」，`claim status` 单独报 `corrupt`，
  `state.py doctor --fix` 作废半写产物，`--all` 收口对坏文件只作废那一片并报数
  （一个坏文件曾经会让整轮过闸以退出码 10 结束）。
- **影响**：`scripts/workspace.py`、`scripts/prompt_pack.py`（新增）、`scripts/_common.py`、
  `scripts/typo_scan.py`、`scripts/scan_patterns.py`、`scripts/verify_span.py`、
  `scripts/filter_neverflag.py`、`scripts/state.py`、`scripts/chunk.py`、
  `assets/config.default.yaml`、`SKILL.md` 第 3/4/4.5 步与并发一节、
  `prompts/pass1-typo.md`、`prompts/pass1-pattern.md`、`references/schemas.md`、
  回归第 23–24 节（新增 27 项）。
## ADR-041　分片大小由子 Agent 的窗口决定；配置改动只有一个入口

- **日期**：2026-08-18
- **背景**：ADR-040 之后又跑了一轮，主 Agent 自己给出的判断是
  「核心问题是分片过大导致子代理上下文溢出（3/5 失败），按技能指引调小
  `chunking.max_text_tokens`。先确认配置合并方式」。**判断是对的，两个问题是真的**：

  1. **默认 `max_text_tokens: 15000` 对中等 harness 偏大。** 渲染出的审查 prompt
     实测最大 21124 字符（固定开销 8207 + 正文）。更要紧的是**撑爆的往往是输出而不是输入**：
     事实抽取的产出长度与正文成正比，片越大越容易撞上单次输出上限，
     截断的 JSON 解析不了，这一片就会反复失败——重试多少次都一样。
  2. **"配置合并方式"确实说不清。** `load_run_config` 让缺省 `--config` 的脚本读 run 的
     配置快照（ADR-039），但**改配置**没有入口：只给 chunk.py 传一份新 yaml，
     其余脚本仍读旧快照，又回到半生效状态。Agent 停下来先问，是对的。
  3. 更根本的是**这个错误发现得太晚**：要派完一整波才看得见，而且看到的是
     「5 个失败 3 个」，不是原因。
- **决策**：
  - 默认 `max_text_tokens` 15000 → 10000（800 页文档约 26 片）。片数因此上升，
    这是有意的取舍：**片大到子 Agent 装不下时，失败的单元会一直失败**，
    而片小只是调用多。
  - 新增 `chunking.max_prompt_chars`（默认 20000）与 `prompt_pack.py` 的**派活前体检**：
    超限即以退出码 8 终止，并在 stdout 里给出 `suggest_max_text_tokens`。
    建议值按「上限 − 固定开销」算，不按总量等比例缩——等比例缩出来的值可能仍然超，
    Agent 会陷在「改了、重跑、还是 8」的循环里。上限本身比固定开销还小时，
    改口建议调 `max_prompt_chars` 并给出最低可行值。
  - 新增 `workspace.py reconfigure`：把新值深合并进 run 的配置快照，
    并在返回的 `rerun` 里说明这次改动要重跑哪些步骤。**这是改配置的唯一入口**；
    `--config` 留给「整轮都用另一份配置」。D4 照旧强制 `apply_threshold: conservative`。
  - `_common.die` 支持 `payload`，失败时 stdout 也能带可机读的处置依据。
- **备选**：让子 Agent 失败后自动降级重试（重试解决不了"输出装不下"，只是多烧几轮）；
  压缩 `taxonomy.md` / `never-flag.md` 缩小固定开销（削弱的是第一道判据，
  而 8207 字符里真正能省的不多）。
- **影响**：`assets/config.default.yaml`、`scripts/prompt_pack.py`、`scripts/workspace.py`、
  `scripts/_common.py`、`SKILL.md` 第 0/3 步与退出码分诊、回归第 25 节（新增 10 项）。
## ADR-042　派活的固定开销与单元大小无关，所以要成组派

- **日期**：2026-08-18
- **背景**：又一轮实跑：近 60 分钟第一波还没跑完，平台侧的统计是
  **LLM 33 分钟 / 工具调用 24 分钟 / 出字速度 60 token/s**。三点判读：

  1. **工具调用的 24 分钟不是脚本慢。** 实测每个脚本在 800 页语料上是
     76–183 毫秒（`claim next` 98ms、`verify_span --all` 81ms、`chunk.py` 183ms），
     整轮几百次调用加起来不到一分钟。这 24 分钟是**每次调用的固定开销 × 调用次数**：
     模型要先把这次工具调用「说出口」（60 token/s 下一次一两秒），
     再加上拉起子 Agent、往返、把结果重新读进上下文。
  2. **33 + 24 ≈ 57 ≈ 墙上时间 60 分钟，说明子 Agent 实际是串行执行的。**
     真并行时墙上时间应显著小于各项之和。此时 `parallelism` 调多少都没用。
  3. 单元拆细（ADR-040）解决了上下文溢出，但把派活次数抬到了 60+ 次；
     在串行 harness 上，固定开销因此变成主项。
- **决策**：
  - `claim next --stage X --count N`：按 `concurrency.subagent_budget_chars`
    （默认 40000 字符）成组派活。审查单元近两万字符，一次给两个；
    错别字单元几千字符，一次给五六个。**预算按字符算，不是字节**——
    中文一字三字节，按 `stat()` 的字节数会把包打成三分之一大（回归里配了负向对照）。
    prompt 的字符数由 `prompt_pack.py` 落进 `work/prompts/index.json`，
    claim 不必为此把每个 prompt 都读一遍。
  - **错别字候选跨分片攒批**：候选彼此无关，判断只需要候选自带的上下文，
    按片分批会让只有三五个候选的分片也独占一次调用（26 片 = 26 次）。
    改为整篇扫描时跨片攒到 `batch_size` 再切，实测 6 片 3 处候选从 3 次降到 1 次。
    `tid` 的前缀就是分片号，`merge` 据此把裁定归回各自的分片。
  - `SKILL.md` 增加「耗时都花在哪」一节：把「工具调用耗时 = 次数 × 单次开销、
    脚本只占 0.1 秒」写进流程，并要求先确认子 Agent 是不是真并行。
- **顺带修掉的一个缺陷（会导致无限重派）**：`product_ok` 把**空文件**一律判为未完成，
  而「这一片一个问题都没查出来」的合法产物就是一个**空 JSONL**——这类分片会被
  反复重派，而「没查出问题」恰恰是常态。改为：空 `.jsonl` 合法（零行），
  空 `.json` 不合法（JSON 至少要有一个对象）。
- **备选**：回到「一个子 Agent 领一整片」（上下文会溢出，ADR-040 已否）；
  提高 `parallelism`（串行 harness 上无效）；合并审查与抽取两次调用
  （违反「一次调用只做一件事」，且实测会互相污染）。
- **影响**：`scripts/workspace.py`、`scripts/prompt_pack.py`、`scripts/typo_scan.py`、
  `scripts/_common.py`、`assets/config.default.yaml`、`SKILL.md` 第 4/4.5 步、
  `prompts/pass1-typo.md`、回归第 26 节（新增 13 项）。

## ADR-043　完成标记必须是子 Agent 自己写的产物，不能是闸门产物

- **日期**：2026-08-19
- **现场**：用户在 OpenCode 与 Deepseek 上各跑一次 800 页（211,573 字符）的文档，
  十几个小时都停在第一波，日志里是上下文溢出与二十多条排队中的子 Agent 消息，
  平台统计显示智能体在约 90 分钟处终止。
- **根因**：`stage_units()` 给 `review` 阶段的 `done_marker` 指的是
  `issues-<片>.jsonl`——那是**闸门②③的产物**；而子 Agent 写的是
  `issues-<片>.raw.jsonl`。闸门按 SKILL.md 是「整波跑完之后的收口」，于是环闭上了：

  1. 子 Agent 把 `.raw.jsonl` 写好返回；
  2. `claim status` 仍报 `done: 0 / pending: 30`（它看的是闸门产物）；
  3. SKILL.md 要求每波结束跑一次 `claim reclaim`，而 `reclaim_stage` 释放
     「没有 done_marker」的全部 claim——**刚做完的单元也被放掉**；
  4. 下一次 `claim next` 又把同样的单元发出去。

  `exhausted` 永远不为 true，收口永远不会开始，第一波永远出不去。
  已复现：三个子 Agent 做完 0001–0003 之后，下一轮领到的还是 0001、0002。
- **决策**：`done_marker` 一律等于该单元的 `output`。`chunk_products` 拆出
  `issues_raw`，`chunk_done` 改判子 Agent 产物且过 `product_ok`（半写不算完成）。
- **备选**：把闸门改成每波跑一次。否决——闸门是纯脚本收口，按片各跑一次正是
  ADR-039 花力气压掉的工具往返；而且这会让「做完了没有」依赖一个可以不跑的步骤。
- **为什么 221 项回归全绿还漏掉**：第 26 节的构造顺序是「写 raw → **立刻跑闸门** →
  断言 exhausted」，恰好把这个缺陷盖住了；而 SKILL.md 的顺序是「写 raw → reclaim →
  再派一轮 → …… → 最后才跑闸门」。这正是 CLAUDE.md 记下的那一条：
  **没有一项测试是照着 SKILL.md 的主流程从头跑一遍的。**
- **影响**：`scripts/workspace.py`（`chunk_products` / `chunk_done` / `stage_units`）、
  `tests/run_regression.sh` 第 27 节（新增，含负向对照与"完成标记必须等于产物"的
  通用守卫）、`SKILL.md` 第 4 步。

## ADR-044　`state.py stats` 每次重扫产物；并给出每一波的进度

- **日期**：2026-08-19
- **现场**：`workspace.py init` 会在 manifest 里写一个占位
  `stats: {total_chunks: 0, done: 0, failed: 0, pending: 0}`，而 `stats` 子命令
  只在 `not man.get("stats")` 时才重建——全零字典是真值，这个分支**永远不进**。
  于是整个 run 期间 `state.py stats` 恒定输出 0/0/0。
  而 SKILL.md 第 0 步的续跑路径写的正是「跳到 `state.py stats` 看还差哪些分片」：
  接手的会话看到的永远是"什么都没做"。
- **决策**：`stats` 每次都重建（就是扫一遍产物目录，800 页实测毫秒级），
  并在返回里增加 `stages`，给出 review/extract/typo/pattern 四波各自的
  total / done / claimed / pending / corrupt。
- **理由**：manifest 自己的文档就写着「不是权威状态，只是派生视图」。既然如此，
  就不该有"缓存命中"这条路径。分片级的 `stats.done` 要求审查与抽取都做完，
  Pass 1 跑到一半时它恒为 0——续跑时真正要看的是每一波各差多少，
  一次调用给全，也省掉四次 `claim status`（串行 harness 上每次往返都是一两秒）。
- **影响**：`scripts/state.py`、`SKILL.md` 第 0 步与契约表、回归第 27 节。

## ADR-045　闸门摘要的丢弃数用减法算，不枚举丢弃原因的键名

- **日期**：2026-08-19
- **现场**：`verify_span.py --all` 的 `dropped` 按前缀 `drop_` 累加计数器，
  而计数器实际叫 `length_drop` / `bad_schema` / `context_pid` / `dedup_drop`——
  一个都匹配不上，这一行**永远输出 0**。压测中 233 条候选被长度闸门全数丢弃，
  摘要报的是 `count: 0, dropped: 0`，读起来是"模型什么都没查出来"，
  而实情是"查出来的全被闸门丢了"。这两件事要采取的行动完全相反。
- **决策**：`dropped = Σ(raw − count)`。减法自维护，以后新增丢弃原因不必回来改。
- **说明**：`metrics.py collect` 读的是 `issues-*.gates.json`，一直是对的；
  错的只是主 Agent 每一波唯一看得见的那个摘要数字。
- **影响**：`scripts/verify_span.py`、回归第 27 节（含负向对照）。

## ADR-046　主 Agent 的上下文预算：绝对路径每处只说一次

- **日期**：2026-08-19
- **现场**：ADR-043 修完之后重跑，主 Agent 上下文占用仍然偏高（窗口 256k）。
- **量了一遍**：照 SKILL.md 主流程在 800 页语料（30 片 / 60 个 Pass 1 单元）上回放，
  逐条记下主 Agent 会看到的 stdout 与它自己要吐出的命令——**97,680 字符 / 68 次工具往返**。
  其中 **49% 是同一条 run_dir 绝对路径的重复**：每个单元两条绝对路径，
  在 `claim next` 的 stdout 里出现一次，主 Agent 把它转给子 Agent 时再出现一次，
  60 个单元乘 2 条路径乘 2 处 = 240 处。
- **决策**：
  1. `claim next` 的单元视图砍到 `{"unit", "prompt"}`，`prompt` 只留**文件名**，
     目录由顶层 `dir` 说一次。
     - **`output` 是冗余的**：prompt 文件抬头本来就写着「输出写到：…」，
       连返回格式都写好了。子 Agent 只读这一个文件——这正是 ADR-040 的原则；
       主 Agent 要看进度有 `claim status`，不需要自己对文件。
     - `chunk_id` 与 `stage` 同样冗余：`unit` 就是 `chunk_id`，`stage` 在顶层。
  2. `workspace.py init` 不再吐 `artifacts`（五条绝对路径）。产物路径由
     `workspace.py deliver` 按需给出——init 这一刻它们都还不存在。
  3. `filter_neverflag --all` 与 `typo_scan scan` 只列**非零**的片，另给一个按规则的汇总。
     逐片报零在 800 页上是 30 行、3000 页上是 120 行，读不出任何信息却按片数线性占用上下文。
- **实测**：97,680 → **53,121 字符（降 45%）**；一条 `claim next` 从 985 → 267 字符。
- **配套的第二个杠杆（配置，不改代码）**：`--count` 与 `subagent_budget_chars`
  的默认值是按 ≤64k 窗口定的，`--count 4` 才是真正的上限而不是预算。
  同一份语料：`4/40000` → 34 次 claim / 29 个子 Agent / 53k 字符；
  `6/120000` → 14 次 / 10 个 / 33k 字符；`8/160000` → 14 次 / 8 个 / 32k 字符。
  **不改默认值**：默认要能在小窗口上跑。已写进 SKILL.md 与配置注释，按窗口自行调。
- **要说清的取舍**：打得更包省的是**派活开销与主 Agent 上下文，不是生成时间**——
  总输出量不变。代价在子 Agent：一次装下的 prompt 越多，每轮工具往返要重算的
  上下文越大（连做是平方级的，ADR-040 就是为此把单位拆到「一次调用」的），
  崩一次丢掉的单元也越多。所以不要一路调到窗口上限。
- **守卫**：回归第 26 节新增「一条 `claim next` 里 run_dir 只准出现一次、
  单元视图不超过 120 字符」。这类退化不会让任何断言变红，只会让上下文悄悄涨回去，
  所以得有一条数字断言盯着。
- **影响**：`scripts/workspace.py`、`scripts/filter_neverflag.py`、`scripts/typo_scan.py`、
  `assets/config.default.yaml`、`SKILL.md` 第 4 步与契约表、`tests/run_regression.sh`。

## ADR-047　真实语料上报回来的三类误报：都出在"没有证据也照样下结论"

- **日期**：2026-08-19
- **来源**：用户在半导体装备行业的评审材料上跑完一轮后反馈四条。前三条是缺陷，
  第四条是数据问题，但顺带暴露出一个时机错误。

### ① 平行表的同名指标被判成前后矛盾（L06）

表1 是设备A 的各项指标、表2 是设备B 的同名指标，两张表的行首都写着
「截至2024年12月底，累计总投入」，值分别是 400 与 460 —— 报成了数值冲突。

区分这两条的信息**只在表题里**，而表题在表格外面：分片渲染出来的表格是
`行N: 单元格(pid) | …`，模型能看到表题（它在同一片正文里），但 schema 从没要求它
把表题填进 `scope`。于是两条事实的 `(subject, scope)` 完全相同。

- **决策**：两层。脚本侧 `Ctx.spans_tables()` —— 一组事实横跨两张不同表格时不报
  （L06/L07/L08/L27），百分比合计（L09）把 table_id 并进分组键；prompt 侧要求
  表格类 metric 必须填 `scope`。
- **正文↔表格、同表之内照常比**：「正文说 200ms、表里写 300ms」是真冲突，不能一起压掉。
- **取舍写在明处**：这会漏掉「两张表之间确实自相矛盾」。按 CLAUDE.md 的优先级序列
  误报率高于逻辑检出率；要救回这一类，正确做法是让 `scope` 带上表题，
  而不是放开这道判据。

### ② 「未检索到图33」，而那张图就在文档里（L15）

```python
LABEL_RE = re.compile(r"(图|表)\s*([0-9]+)\s*[-–—.－]\s*([0-9]+)")
```

**它只认「章-序」式**（图3-7）。用全篇流水号的文档（图1…图33，Word 自动编号出来的
多半是这种）**一个已知编号都识别不出来**，`_known_targets["figure"]` 是空集，
于是每一条 `图NN` 引用都成立 —— 满屏"未检索到"。
同一个正则还喂着 L16（跳号/重号）与 L18（有图无引用），这两条规则在这类文档上
长期是静默零覆盖。

- **决策**：
  1. `LABEL_RE` 两种方案都认（第 3 组为 None 即流水式），`label_series()` 保证
     **两种方案不混进同一序列**——混了之后「图3-7」与「图33」会被当成同一列的两个号，
     跳号立刻变成误报。
  2. **已知集为空时 L15 闭嘴。** 空集之下每一条引用都成立，这正是压制方向的
     fail-open（同 ADR-035）：表现为满屏误报，而输出里看不出根因。
     列表式自动编号的号根本不进正文，这一条是那种情况唯一的兜底。
  3. 标题判定改用 `caption_label()`：编号后面必须跟分隔符。
     「图33 系统架构」是图题，「图33所示的架构」是引用——流水式下这两者只差一个空格，
     不区分就会凭空造出编号。

### ③ 「目标与实测差距过大」，翻到那一段却没有这句话（L27）

根因不在 L27，在它脚下的台账：**`ledger.py` 对模型写的事实不做任何校验**。
pid 编造的、数值文档里根本没有的，照单全收，而全部 L 规则都建立在这份台账上。

对照之下，审查通道从第一天就有这道闸门（`verify_span` 的 `hallucination_drop`：
pid 必须存在、`original_text` 必须在那个 pid 的正文里逐字找得到）。
**同一类风险，两条通道一条有防护一条没有**——这是 ADR-034 那句话的又一次兑现：
为 A/B 类定的判据没有推广到其他通道。

- **决策**：`ledger.ingest` 加两道判据，命中即丢并计数（`dropped.bad_pid` /
  `dropped.value_not_found`）：pid 必须在 `paragraphs.jsonl` 里；`metric` 的 value
  其数字部分必须在该段正文中出现。
- **只在能确定时判否**：value 里没有数字（「高/中/低」这类取值）一律放行；
  千分位与全角先归一；非 metric 不做逐字比（statement/definition 本来就是转述）。
  丢弃是有代价的方向——丢错了就是漏检。
- 丢弃数进 stdout：全部 L 规则站在这份台账上，静默丢等于静默漏检。

### ④ 行业术语撞上通用错词表（不是缺陷，但时机错了）

半导体装备这类领域的专有用字与 `common-typos.txt` 天然会撞，而且是**成批**的——
同一个术语出现几十次就是几十条候选。机制本来就有（闸门③ 的 N7），
但它在**模型答完之后**才生效：每条误报仍然花掉了一次裁定调用。

- **决策**：`typo_scan.py scan` 把用户术语表的 `preferred` / `key` / `variants`
  一并当作白名单，在**生成候选之前**就跳过（登记为 `forbidden` 的写法不受保护——
  那正是要挑出来的）。SKILL.md 补上怎么让用户提供术语表，以及
  为什么用 `--fallback` 而不是 `--authoritative`（前者只保护、不新增检查）。

### 覆盖

回归第 28 节，三条误报各配正反两向 + 闸门的边界用例（非数值/千分位/非 metric
一律放行；登记为禁用的写法不受保护）。**负向对照在这一节抓到了两次错**：
先是我的 fixture 数值没落地被新闸门拦下，再是"不登记术语表就该出候选"这条
一开始读错了字段——两次都是构造无效而不是实现有问题，但没有对照就看不出来。

- **影响**：`scripts/detect_conflicts.py`、`scripts/ledger.py`、`scripts/typo_scan.py`、
  `references/prompts/pass1-extract.md`、`SKILL.md`、`tests/run_regression.sh`。

## ADR-048　把"顺带发现"变成可枚举的扫查

- **日期**：2026-08-19
- **起因**：用户问「为什么总是有一些顺带识别出来的问题，能不能一次性识别出来」。
  把 ADR-028…047 的根因归类之后，答案是：**能，但只对其中一半**。

### 复发的两个形状

统计 ADR-029 起的 20 条，绝大多数落在两个形状里：

1. **同一条判据只落实了一部分兄弟。** ADR-029（三处调用点各写一遍 `verdict` 判断，
   `v is None` 时全部失效）、ADR-034（为 A/B 类定的跨度上限被套到 P 类）、
   ADR-035（N7 的区域判据没推广到 N9/N10/N12）、ADR-043（四个阶段里只有 review
   的完成标记指错）、ADR-047①（三条通道有幻觉闸门，事实通道没有）。
2. **证据基为空时照样下结论。** ADR-044（`stats` 的全零占位永远命中）、
   ADR-047②（一个图号都没认出来 → 已知集为空 → 每条引用都"不存在"）。

这两类都不需要真实语料就能发现——**它们是不变量，不是事实判断**。
以前发现不了，是因为没有一项测试去枚举它们。

### 做法

- **形状 1** 已在 ADR-043 落地成通用守卫：回归第 27 节断言
  「每个阶段的 `done_marker` 必须等于它的 `output`」，四个阶段一起过。
- **形状 2** 本轮落地成回归第 29 节：把「声称某物不存在」的规则列出来，
  逐条把它的证据基清空，断言闭嘴；再各配一条证据基非空的负向对照，
  防止这道守卫退化成静音开关。

### 这一次扫查当场抓到的

- **L15 的 section 分支。** ADR-047 补这道判据时只补了图与附录，章节漏了——
  同一条判据在三个分支里落实了两个。**这正是形状 1，而且是在修形状 2 的当天犯的。**
- **L29 / L30 在整类证据为空时把每一条都报一遍。** 抽取 prompt 明确要求
  `serves_objective` 只在文档显式写明时才填、"宁可留 null"，所以"全篇皆空"是
  预期内的常态；不拦这一下，这条规则在多数文档上会把**每一个**目标都报一遍。
- **回归自己的 L29/L30 断言一直是靠这个缺陷通过的。** `logic-injection` fixture
  的证据侧本来就是空的：加上守卫之后两条立刻变红。已给 fixture 补上正例
  （O2←I8 的显式对应 + I8 的验收标准），断言才第一次成立。
  **这是本轮最值得记的一条**：一条断言长期为真，可能是因为规则对，
  也可能是因为规则错得恰好让它为真。

### 扫查不到的那一半

ADR-047 的①与②来自真实语料，合成输入扫不出来：
「这份文档的图号是全篇流水的」「这两张表是按设备并列的」属于**现实与假设不符**，
不是不变量违反。这一类只有真实语料能暴露，而 `corpus/` 的里程碑压测一直没做
（progress.md「未完成」第 2 条）。**这是当前最大的已知缺口，不要指望扫查覆盖它。**

- **影响**：`scripts/detect_conflicts.py`（L15 section 分支、L29、L30）、
  `tests/fixtures/make_fixtures.py` + `logic-injection.docx` / `.facts.json`、
  `tests/run_regression.sh` 第 29 节、`CLAUDE.md`。
