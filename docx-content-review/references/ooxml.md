# OOXML 操作要点

本技能内嵌的四项 docx 处理能力。**排障时读本文件**；正常流程中脚本已经处理好这些细节，
不需要你手动操作 XML。

---

## 1. 解包与 run 合并

`.docx` 是 ZIP。解包后**先删除所有符号链接条目**（外部来源文档不可信），再做 run 合并。
同时防 zip slip：解出的路径必须仍在目标目录之内。

**为什么必须合并 run**：Word 会因修订 ID、拼写检查标记、语言标记把一个句子拆成多个 `<w:r>`，
所以**文档中肉眼可见的短语，在 XML 里往往不是连续字符串**。这是所有定位失败的首要原因。
实测在碎片化文档上，合并可把 513 个 run 降到 55 个，文本逐字不变。

合并的安全条件（缺一不可）：

- 两个 run 相邻且同父；
- `rPr` 规范化后完全相同（含 `rFonts` 的四个属性、`sz`/`szCs`、`b`/`i`/`color`/`u`/`spacing`）；
- 两者都只含 `rPr` 与 `w:t`——含域（`fldChar`）、图形（`drawing`）、换行（`br`）、
  制表（`tab`）、批注/脚注引用的 run 一律不合并；
- 父元素不是 `w:ins` / `w:del` / `w:moveFrom` / `w:moveTo` / `w:smartTag` / `w:sdtContent`
  （避免触碰既有修订标记）。

合并后若文本首尾有空白，必须补 `xml:space="preserve"`。

**编辑 `document.xml` 时禁止重新格式化或美化 XML**：缩进会改变 `w:t` 的文本内容。

## 2. 段落级 ID 与定位

段落 ID 按文档顺序（含表格内段落）从 1 递增：`p-000412`。定位一段文字时：

1. 取该段落中"可定位"的 run——不在既有修订标记内、且只含 `rPr` 与 `w:t`；
2. 把它们的文本拼接成一个字符串；
3. 在拼接串上找目标位置，再映射回具体 run 与偏移。

不能直接在单个 run 里找——见上一节。

## 3. 修订回写（w:ins / w:del）

本技能**只做行内文本替换**：不删段落、不合并段落、不新增段落。这是刻意的能力约束，
用于把回写风险降到最低。

替换 run 内部一段文字，产出结构是：

```xml
<w:r><w:rPr>…</w:rPr><w:t>前段</w:t></w:r>
<w:del w:id="1000" w:author="内容审查" w:date="2026-08-02T08:36:43Z">
  <w:r><w:rPr>…</w:rPr><w:delText>被删段</w:delText></w:r>
</w:del>
<w:ins w:id="1001" w:author="内容审查" w:date="2026-08-02T08:36:43Z">
  <w:r><w:rPr>…</w:rPr><w:t>新增段</w:t></w:r>
</w:ins>
<w:r><w:rPr>…</w:rPr><w:t>后段</w:t></w:r>
```

要点：

- **删除态的文本元素是 `<w:delText>`，不是 `<w:t>`。** 写错会让 Word 显示异常。
- **前段 / 被删段 / 后段 / 新增段，四者的 `rPr` 都必须是原 `rPr` 的完整深拷贝。**
  漏拷或部分拷贝会让 Word 套用默认样式——三号字变五号、仿宋变等线，
  **而且只在用户接受修订后才显现**，回写当时完全看不出来。这是 D9 最容易被静默违反的地方。
- `w:id` 必须全局唯一。脚本从文档中已有的最大 id 起跳 1000。
- `w:rPr` 内若出现 `w:ins` / `w:del`，必须排在其他子元素之前（schema 强制的元素顺序）。
- 段落标记删除（`<w:pPr><w:rPr><w:del/></w:rPr></w:pPr>`）语义是"把本段并入下一段"。
  本技能不产生这种结构。

**禁止输出任何格式修订标记**：`w:rPrChange`、`w:pPrChange`、`w:sectPrChange`、
`w:tblPrChange`、`w:tcPrChange`、`w:trPrChange`。这些元素存在即表示改动了格式，违反 D9。

## 4. 批注回写（六文件联动）

**核心层与增强层必须分开对待**：

| 层 | 文件 | 缺失后果 |
|---|---|---|
| **核心层** | `word/comments.xml` + `document.xml` 中的 `commentRangeStart`/`commentRangeEnd`/`commentReference` + `word/_rels/document.xml.rels` 关系项 + `[Content_Types].xml` 覆盖项 | **批注不可见** |
| **增强层** | `commentsExtended.xml`、`commentsIds.xml`、`commentsExtensible.xml` | 仅失去回复线程、已解决标记，**批注照常可见** |

核心层结构自 Office 2007 起稳定；增强层随 Word 版本演进。**增强层写入失败时降级为
仅核心层并记录，不得导致整体失败。**

锚定结构（三者缺一批注即不可见）：

```xml
<w:commentRangeStart w:id="1"/>
<w:r><w:rPr>…</w:rPr><w:t>被批注的文字</w:t></w:r>
<w:commentRangeEnd w:id="1"/>
<w:r><w:commentReference w:id="1"/></w:r>
```

注意 `commentReference` 所在的 run **不要加 `rStyle`**：引用 `CommentReference` 样式
需要该样式存在于 `styles.xml`，而 D9 禁止修改 `styles.xml`。裸 run 即可，Word 正常渲染。

必要的注册：

- `document.xml.rels` 增加 `Type=".../relationships/comments" Target="comments.xml"`
- `[Content_Types].xml` 增加
  `Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"`

`apply_comments.py` 一次性处理全部批注（3000 页可能上千条），避免反复解包/打包。

## 5. 重新打包

`[Content_Types].xml` 必须是包内第一项。其余部件按路径排序即可。不要用 `zip` 的默认
额外字段（脚本用 Python `zipfile`，不写扩展属性）。

## 6. 回写后的四项校验

`validate_docx.py` 全部**基于磁盘上的两份文档现场重算**，不读回写时记录的溯源信息——
溯源记录只描述"当时做了什么"，无法证明"现在的文档是什么样"。

1. **结构校验**：全部部件 XML 良构；`rPr` 内 `ins`/`del` 排首位；`w:r` 的 `rPr` 排首位；
   修订 id 唯一；`w:del` 内不得出现 `w:t`。（提供 `--xsd <目录>` 时额外做真 XSD 校验。）
2. **无未追踪变更**：「拒绝全部修订」视图必须与基线逐字相同。任何未被 `w:ins`/`w:del`
   包裹的文本变更，在"接受修订后"视图里不可见，极易无意产生。
3. **样式不变性（D9）**：
   - `styles.xml` / `theme/theme1.xml` / `fontTable.xml` / `numbering.xml` /
     `settings.xml` / `webSettings.xml` 字节哈希不变；
   - 无任何格式修订标记；
   - **逐字符 (字, rPr) 比对**：拒绝修订视图必须与基线完全一致；接受修订视图中，
     每个段落用到的 rPr 必须是该段落基线 rPr 的子集。
4. **批注锚定完整**：每个 `commentReference` 都有配对的 Start/End，且 id 在 `comments.xml` 中存在；
   反过来 `comments.xml` 中声明却无引用的批注同样报错（那样的批注不可见）。

**任一失败即判定回写失败，丢弃产物、回滚，绝不交付未通过校验的 docx。**
