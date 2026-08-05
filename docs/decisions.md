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
