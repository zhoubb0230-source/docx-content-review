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
