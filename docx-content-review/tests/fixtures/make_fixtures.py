#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成测试语料（spec §15.2）。开发期工具，依赖 python-docx，不随技能运行期使用。

    python3 make_fixtures.py [--outdir .]

产出（.docx 与答案清单一并提交，回归测试不需要 python-docx）：
  sample-basic.docx        + sample-basic.answers.json    正/负样本
  negative-set.docx        + negative-set.answers.json    ≥30 条合法但易误判表达（N1–N14）
  logic-injection.docx     + logic-injection.answers.json L01–L32 每条至少 1 例
  planning-tables.docx     + planning-tables.answers.json 规划类文档，表格密集
  style-regression.docx    + style-regression.answers.json 多字体字号，验证 D9
  typo-pattern.docx        + typo-pattern.answers.json    错别字通道与 P 类范式通道
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.shared import Pt
from docx.oxml.ns import qn


def _run(p, text, *, font=None, size=None, bold=False, italic=False):
    r = p.add_run(text)
    if font:
        r.font.name = font
        r._element.rPr.rFonts.set(qn("w:eastAsia"), font)
    if size:
        r.font.size = Pt(size)
    r.bold = bold
    r.italic = italic
    return r


def _doc():
    d = Document()
    st = d.styles["Normal"]
    st.font.name = "仿宋"
    st.font.size = Pt(16)  # 三号字
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "仿宋")
    return d


# --------------------------------------------------------------------------
def build_basic(outdir: Path) -> None:
    d = _doc()
    answers = {"positive": [], "negative": []}

    d.add_heading("1 系统概述", level=1)
    p = d.add_paragraph("本系统采用微服务架构，支撑全公司的研发协同需求。")

    def pos(text, category, original, suggested, note="", expect="revision"):
        """expect：该条经闸门②之后应当变成什么。

        `revision` = 建议能落笔；`comment` = 建议必然超出该类别的修改幅度，
        应被降级为批注。**降级不是失败，是闸门在拒绝改写。**
        A 类的定义是「有唯一正确答案」，凡是只能靠重写才能改对的，
        本来就不该以修订形式落笔。
        """
        d.add_paragraph(text)
        answers["positive"].append({"category": category, "original_text": original,
                                    "suggested_text": suggested, "note": note,
                                    "expect": expect})

    d.add_heading("2 已知问题样本", level=1)
    # A1 错别字
    pos("平台已完成部署，用户可通过帐号登录后使用全部功能。", "A1", "通过帐号登录", "通过账号登录", "帐/账 形近误用")
    pos("安装程序会自动按装依赖组件，无需人工干预。", "A1", "自动按装依赖组件", "自动安装依赖组件", "")
    pos("系统布署完成后需重启服务进程。", "A1", "系统布署完成后", "系统部署完成后", "")
    # A2 的地得
    pos("研发团队认真的完成了本阶段的全部开发任务。", "A2", "认真的完成", "认真地完成", "状语用地")
    pos("这套方案设计得非常合理，实施的很顺利。", "A2", "实施的很顺利", "实施得很顺利", "补语用得")
    pos("必须严格的执行数据出域审批流程。", "A2", "严格的执行", "严格地执行", "")
    # A3 标点
    pos("本章介绍三部分内容:架构设计、数据模型、接口规范。", "A3", "内容:架构设计", "内容：架构设计", "中英冒号混用")
    pos("详见《系统设计说明书 第三章。", "A3", "《系统设计说明书 第三章。", "《系统设计说明书》第三章。", "书名号不配对")
    pos("请参阅附录A(接口清单)获取完整定义。", "A3", "附录A(接口清单)", "附录A（接口清单）", "中英括号混用")
    # A4 成分残缺
    pos("通过本次架构升级，使系统的整体吞吐能力得到显著提升。", "A5", "通过本次架构升级，使系统",
        "本次架构升级使系统", "介词滥用致主语残缺，修法含删除故归 A5")
    pos("为了保证数据安全，必须对所有出域数据进行。", "A4",
        "对所有出域数据进行。", "对所有出域数据进行加密。",
        "缺宾语中心语；跨度取到 4 字以上才定位得住（min_span_chars=4）")
    pos("能否顺利上线，取决于团队持续投入。", "A4", "取决于团队持续投入",
        "取决于团队能否持续投入", "两面对一面，纯增补")
    # A5 搭配不当
    pos("本阶段的主要任务是提高研发人员的工作积极性和工作水平。", "A5", "提高研发人员的工作积极性和工作水平",
        "调动研发人员的工作积极性、提高其工作水平",
        "动宾搭配不当；一个动词辖两个宾语，只能重写不能最小修改", expect="comment")
    pos("该方案降低了系统的可用性和运维成本。", "A5", "降低了系统的可用性和运维成本",
        "提升了系统的可用性、降低了运维成本",
        "一动带两宾搭配矛盾；同样只能重写", expect="comment")
    pos("我们改善了平台的响应速度问题。", "A5", "改善了平台的响应速度问题", "改善了平台的响应速度", "")
    # A6 关联词
    pos("虽然当前架构存在瓶颈，因此我们启动了本次重构。", "A6", "虽然当前架构存在瓶颈，因此",
        "由于当前架构存在瓶颈，因此", "关联词错配")
    pos("不但没有降低成本，而且投入进一步增加。", "A6", "不但没有降低成本，而且", "不但没有降低成本，反而", "")
    pos("只有严格执行流程，就能保证交付质量。", "A6", "只有严格执行流程，就能", "只要严格执行流程，就能", "")
    # A7 赘余
    pos("预计本期投入大约在 200 万元左右。", "A7", "大约在 200 万元左右", "在 200 万元左右", "大约/左右重复")
    pos("该模块目前正在开发中，预计下月完成。", "A7", "该模块目前正在开发中", "该模块正在开发", "目前/正在/中叠用")
    pos("这是一个非常十分关键的技术选型决策。", "A7", "一个非常十分关键的技术选型", "一个非常关键的技术选型", "")
    # A8 数字单位
    pos("核心链路端到端时延不超过 200ms，边缘链路不超过 0.5s。", "A8", "不超过 0.5s", "不超过 500ms", "同一 subject 单位不统一")
    pos("本期预算 1,00,000 元，已完成审批。", "A8", "预算 1,00,000 元", "预算 100,000 元", "千分位错位")
    # B 类
    d.add_heading("3 需语境判断样本", level=1)
    pos("平台对接了订单系统与库存系统，它的接口规范尚未最终确定。", "B1", "它的接口规范尚未最终确定", "", "指代不明")
    pos("重要的客户数据保护措施已经全部落实到位。", "B2", "重要的客户数据保护措施", "", "多重定语两解")
    pos("为了实现在多云环境下对异构算力资源进行统一纳管并向上层业务提供一致的资源视图从而降低"
        "业务方在资源申请与调度环节的认知负担我们在本期规划中设计了统一资源抽象层。",
        "B3", "为了实现在多云环境下", "", "长句主干不可辨")
    pos("这项政策受到了全体研发人员的一致拥护和支持，研发人员也因此被政策所激励。", "B4",
        "研发人员也因此被政策所激励", "", "主客体颠倒")
    pos("本期完成了三个模块的开发，因此团队规模保持不变。", "B5", "因此团队规模保持不变", "", "无因果却用因此")

    d.add_heading("4 不应上报的合法表达", level=1)
    for text, n in NEGATIVE_SAMPLES[:8]:
        d.add_paragraph(text)
        answers["negative"].append({"text": text, "rule": n})

    d.add_heading("5 代码与表格", level=1)
    d.add_paragraph("部署命令如下：")
    cp = d.add_paragraph("sudo apt-get install -y libreoffice-writer")
    cp.style = d.styles["Normal"]
    d.add_paragraph("/usr/local/etc/nebula/config.yaml")
    d.add_paragraph("timeout_ms = 200")
    t = d.add_table(rows=3, cols=3)
    t.style = "Table Grid"
    data = [["指标", "目标值", "单位"], ["端到端时延", "200", "ms"], ["接入率", "80", "%"]]
    for i, row in enumerate(data):
        for j, v in enumerate(row):
            t.cell(i, j).text = v

    d.save(outdir / "sample-basic.docx")
    (outdir / "sample-basic.answers.json").write_text(
        json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ≥30 条合法但极易被误判的表达，覆盖 N1–N14（spec §15.2 第 2 条）
NEGATIVE_SAMPLES = [
    ("本期采用统一的接入方式，各业务线使用相同的鉴权协议。", "N1"),
    ("我们运用成熟的开源组件构建底座，避免重复造轮子。", "N1"),
    ("在本次评审中，我们对方案做了充分的论证。", "N1"),
    ("为保障稳定性，灰度期间我们逐步放量。", "N2"),
    ("逐步放量的策略，在灰度期间我们始终坚持。", "N2"),
    ("数据出域申请由数据 owner 审批。", "N3"),
    ("由数据 owner 对数据出域申请进行审批。", "N3"),
    ("这事儿得先跟业务方对齐口径。", "N4"),
    ("此事需先与业务方对齐口径。", "N4"),
    ("平台上线。", "N5"),
    ("平台已于本月完成全量上线，并通过了全部验收用例。", "N5"),
    ("方案已通过评审了。", "N6"),
    ("方案已通过评审。", "N6"),
    ("边缘节点（edge node）在本文中简称 EN。", "N7"),
    ("星云平台又称 Nebula，二者指同一系统。", "N7"),
    ("本期 P0 需求共 12 项，P1 需求 27 项。", "N8"),
    ("SRE 团队负责线上稳定性，QA 团队负责质量准入。", "N8"),
    ("执行 kubectl get pods -n prod 查看运行状态。", "N9"),
    ("配置项 max_retry=3 表示最多重试三次。", "N9"),
    ("《网络安全法》第二十一条规定：国家实行网络安全等级保护制度。", "N10"),
    ("合同第 3.2 条约定：乙方应于验收合格之日起十五个工作日内交付。", "N10"),
    ("上表中「—」表示该季度无预算安排。", "N11"),
    ("表头「时延/ms」为端到端时延，单位毫秒。", "N11"),
    ("图 3-7 系统总体架构图", "N12"),
    ("表 4-2 分年度预算安排表（单位：万元）", "N12"),
    ("本期共完成 3 个一级模块与十二项子任务。", "N13"),
    ("第一阶段于 2026 年 3 月启动，二期于同年 9 月启动。", "N13"),
    ("三、风险分析与应对", "N14"),
    ("4.2 数据层存储选型", "N14"),
    ("附录 A 接口清单", "N14"),
    ("本方案在保证一致性的前提下，兼顾了可用性与分区容错。", "N1"),
    ("研发效能度量以交付周期、变更失败率、恢复时长三项为主。", "N8"),
    ("详见 https://example.internal/docs/nebula/api 中的接口说明。", "N9"),
]


def build_negative(outdir: Path) -> None:
    d = _doc()
    d.add_heading("负样本集：以下全部为合法表达，任何上报均计为误报", level=1)
    rows = []
    for i, (text, rule) in enumerate(NEGATIVE_SAMPLES, 1):
        d.add_paragraph(text)
        rows.append({"idx": i, "text": text, "never_flag_rule": rule, "expect": "no_issue"})
    d.save(outdir / "negative-set.docx")
    (outdir / "negative-set.answers.json").write_text(
        json.dumps({"total": len(rows), "samples": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


# --------------------------------------------------------------------------
def build_logic(outdir: Path) -> None:
    """L01–L32 每条至少一例，附答案清单。"""
    d = _doc()
    a = []

    def sec(title, level=1):
        d.add_heading(title, level=level)

    def para(text, rule=None, note=""):
        d.add_paragraph(text)
        if rule:
            a.append({"rule": rule, "text": text, "note": note})

    sec("1 总则")
    para("边缘节点是指部署在网络边缘、承担就近计算任务的物理或虚拟节点。", "L01", "定义 A")
    para("本平台使用 MEC（多接入边缘计算）作为边缘侧的统一技术底座。", "L02", "展开 A")
    para("EN 的资源规格由资源池统一分配。", "L03", "缩略语首次出现未展开")
    para("星云平台负责统一调度；星云平台的资源视图对上层透明。")
    para("核心链路端到端时延目标为不超过 200ms。", "L06", "数值 A")
    para("本期总预算按三部分构成：基础设施 50%、平台研发 30%、运营支撑 15%。", "L09", "合计 95%")
    para("一期上线时间定为 2026 年 3 月。", "L12", "日期 A")
    para("星云平台当前版本为 v2.3。", "L14", "版本 A")
    para("详见第 9.9 节的容灾设计说明。", "L15", "指向不存在的章节")
    para("多租户隔离能力当前不支持，计划二期建设。", "L20", "状态 A")
    para("灰度发布必须覆盖全部核心链路。", "L21", "情态 A")
    para("数据出域申请由数据 owner 审批。", "L22", "职责 A")
    para("服务架构采用微服务。", "L28", "立场 A")
    para("本期设定目标 O1：实现研发效率提升 30%。", "L29", "目标无对应举措")
    para("本期开展举措 I9：建设统一日志平台。", "L30", "举措无验收指标")
    para("平台可用性不低于 99.95%。", "L31", "量化承诺无度量方式")

    sec("2 架构设计")
    para("边缘节点指的是靠近用户侧、用于内容缓存的专用服务器。", "L01", "定义 B，与 1 冲突")
    para("MEC 在本文中指移动边缘控制器。", "L02", "展开 B，与 1 冲突")
    para("边沿节点的规格与边缘结点保持一致。", "L04", "同一实体多种写法")
    para("智能助手与 AI 助手在本期均指同一套研发辅助工具。", "L05", "近义术语混用")
    para("核心链路端到端时延目标为不超过 500ms。", "L06", "数值 B，与 1 冲突")
    para("边缘链路时延不超过 0.5s，核心链路不超过 200ms。", "L07", "单位不一致")
    para("并发连接数下限为 5000，上限为 3000。", "L08", "区间矛盾")
    para("本期吞吐量将实现翻倍，由 1000 QPS 提升至 1500 QPS。", "L11", "翻倍与数值不符")
    para("一期上线时间定为 2026 年 6 月。", "L12", "日期 B，与 1 冲突")
    para("二期于 2026 年 1 月启动，晚于一期上线。", "L13", "时序倒置")
    para("星云平台当前版本为 v2.1。", "L14", "版本倒退")
    para("如图 3-7 所示，系统分为接入层与计算层。")
    para("多租户隔离已完成建设并投入使用。", "L20", "状态 B，与 1 冲突")
    para("灰度发布为可选步骤，团队可自行决定是否执行。", "L21", "情态冲突")
    para("数据出域申请由安全合规组审批。", "L22", "职责冲突")
    para("服务架构采用单体架构以降低运维复杂度。", "L28", "立场冲突")
    para("本节结论：数据层已全面完成建设。", "L23", "结论与 status 矛盾")
    para("本期指标目标为 200ms，实测值为 1200ms。", "L27", "目标与实测差距超 3 倍")

    sec("2.1 存储选型", level=2)
    # L23 的另一半。没有这句，全文提到「数据层」的地方就只剩那条结论本身，
    # L23 无从触发——参考台账曾靠凭空补一条 status 让它命中，那是伪造事实。
    para("数据层存储选型仍在评估中，尚未完成。", "L23", "与前述结论矛盾的 status")
    sec("2.1.1.1 冷热分层", level=4)   # L19 跳级
    a.append({"rule": "L19", "text": "2.1.1.1 冷热分层", "note": "H2 直接跳 H4"})

    sec("3 风险分析")
    para("本章从三个方面分析风险：技术风险、进度风险、成本风险、合规风险。", "L10",
         "声明三个实列四项")
    para("综上所述，上述两点已充分说明本方案的可行性。", "L24", "总结条目数与前文不符")

    sec("4 附图附表")
    d.add_paragraph("图 3-7 系统总体架构图")
    d.add_paragraph("图 3-9 数据流向图")
    a.append({"rule": "L16", "text": "图 3-9", "note": "图编号跳号（缺 3-8）"})
    d.add_paragraph("表 4-1 分年度预算安排表")
    a.append({"rule": "L18", "text": "表 4-1", "note": "表存在但正文无引用"})
    d.add_paragraph("本平台采用边沿节点作为统一称谓。")
    a.append({"rule": "L25", "text": "边沿节点", "note": "需外部权威术语表激活"})
    d.add_paragraph("边缘结点的部署密度由容量模型确定。")
    a.append({"rule": "L26", "text": "边缘结点", "note": "需外部权威术语表激活"})
    a.append({"rule": "L17", "text": "目录条目与正文标题不一致", "note": "需含 TOC 的文档，见 planning-tables"})
    a.append({"rule": "L32", "text": "风险分析", "note": "章节承诺内容缺失需人工核对"})

    d.save(outdir / "logic-injection.docx")
    (outdir / "logic-injection.answers.json").write_text(
        json.dumps({"total": len(a), "expected": a}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


# --------------------------------------------------------------------------
def build_planning(outdir: Path) -> None:
    """规划/可研类：表格密集，验证表格事实抽取与 L27–L32。"""
    d = _doc()
    d.add_heading("某单位信息化建设三年规划（2026—2028）", level=1)
    d.add_heading("一、建设目标", level=1)
    d.add_paragraph("为实现目标 O1（研发效率提升 30%），本期开展举措 I1：建设统一代码平台。")
    d.add_paragraph("为实现目标 O2（运维成本下降 20%），本期开展举措 I2：建设统一监控平台。")
    d.add_paragraph("目标 O3：实现全域数据资产可见可管。")

    d.add_heading("二、预算安排", level=1)
    t = d.add_table(rows=5, cols=4)
    t.style = "Table Grid"
    rows = [["科目", "2026 年", "2027 年", "2028 年"],
            ["基础设施", "1200", "800", "600"],
            ["平台研发", "900", "1100", "700"],
            ["运营支撑", "300", "400", "500"],
            ["合计", "2400", "2300", "1800"]]
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            t.cell(i, j).text = v
    d.add_paragraph("表 2-1 分年度预算安排表（单位：万元）")
    d.add_paragraph("三年总投入 6500 万元，其中 2026 年投入 2400 万元。")

    d.add_heading("三、关键指标", level=1)
    t2 = d.add_table(rows=5, cols=4)
    t2.style = "Table Grid"
    rows2 = [["指标", "基线", "目标值", "单位"],
             ["端到端时延", "800", "200", "ms"],
             ["平台接入率", "35", "80", "%"],
             ["变更失败率", "12", "3", "%"],
             ["平均恢复时长", "45", "15", "min"]]
    for i, r in enumerate(rows2):
        for j, v in enumerate(r):
            t2.cell(i, j).text = v
    d.add_paragraph("表 3-1 关键指标目标表")
    d.add_paragraph("验收标准：I1 的平台接入率不低于 80%。")

    d.add_heading("四、里程碑", level=1)
    t3 = d.add_table(rows=4, cols=3)
    t3.style = "Table Grid"
    rows3 = [["阶段", "启动时间", "完成时间"],
             ["一期", "2026-01", "2026-03"],
             ["二期", "2026-04", "2026-09"],
             ["三期", "2027-01", "2027-06"]]
    for i, r in enumerate(rows3):
        for j, v in enumerate(r):
            t3.cell(i, j).text = v
    d.add_paragraph("表 4-1 里程碑计划表")
    d.add_paragraph("平台可用性不低于 99.95%，端到端时延不超过 200ms。")

    d.add_heading("五、风险分析", level=1)
    d.add_paragraph("本章说明主要风险及应对措施。")

    d.save(outdir / "planning-tables.docx")
    (outdir / "planning-tables.answers.json").write_text(json.dumps({
        "expected_facts": {
            "objectives": ["O1", "O2", "O3"],
            "initiatives": ["I1", "I2"],
            "acceptance_targets": ["I1"],
            "metrics_from_table": ["端到端时延", "平台接入率", "变更失败率", "平均恢复时长"],
            "tables": 3,
        },
        "expected_conflicts": [
            {"rule": "L29", "note": "O3 无对应举措"},
            {"rule": "L30", "note": "I2 无验收指标"},
            {"rule": "L31", "note": "可用性 99.95% 无度量方式"},
            {"rule": "L32", "note": "风险分析章节无风险条目"},
            {"rule": "L09", "note": "2026 年分项 1200+900+300=2400，与合计一致；2027 年 800+1100+400=2300 一致"},
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
def build_style(outdir: Path) -> None:
    """样式回归样本：多字体字号混排，回写后逐 run 比对 rPr（D9）。"""
    d = _doc()
    h = d.add_paragraph()
    _run(h, "第一章 样式回归验证", font="黑体", size=22, bold=True)

    p = d.add_paragraph()
    _run(p, "本段前半部分为三号仿宋，", font="仿宋", size=16)
    _run(p, "中间这一段为四号黑体加粗，", font="黑体", size=14, bold=True)
    _run(p, "后半部分为小四楷体斜体。", font="楷体", size=12, italic=True)

    p = d.add_paragraph()
    _run(p, "中英文混排：平台代号 ", font="仿宋", size=16)
    _run(p, "Nebula-Edge v2.3", font="Times New Roman", size=16)
    _run(p, " 已完成部署，用户可通过帐号登录。", font="仿宋", size=16)

    p = d.add_paragraph()
    _run(p, "带颜色的强调文本：", font="仿宋", size=16)
    r = _run(p, "必须严格的执行审批流程", font="仿宋", size=16, bold=True)
    r.font.color.rgb = None

    for i in range(1, 4):
        lp = d.add_paragraph(style="List Number")
        _run(lp, f"编号列表第 {i} 项，内容为三号仿宋。", font="仿宋", size=16)

    p = d.add_paragraph()
    _run(p, "分页后继续：", font="仿宋", size=16)
    p.add_run().add_break(WD_BREAK.PAGE)

    p = d.add_paragraph()
    _run(p, "第二页正文，字体保持三号仿宋不变。", font="仿宋", size=16)

    d.save(outdir / "style-regression.docx")
    (outdir / "style-regression.answers.json").write_text(json.dumps({
        "invariants": [
            "styles.xml/theme1.xml/fontTable.xml/numbering.xml/settings.xml/webSettings.xml 字节哈希不变",
            "document.xml 中不得出现 rPrChange/pPrChange/sectPrChange/tblPrChange/tcPrChange/trPrChange",
            "未改动 run 的 rPr 序列化结果不变",
            "新增 run 的 rPr 与来源 run 逐属性相等",
        ],
        "editable_spans": [
            {"category": "A1", "original_text": "帐号", "suggested_text": "账号"},
            {"category": "A2", "original_text": "严格的执行", "suggested_text": "严格地执行"},
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_typo_pattern(outdir: Path) -> None:
    """错别字支线 + P 类范式支线的联合语料。

    错别字部分刻意混入白名单陷阱（帐篷、以经济、子节点）——词表是靠字符串匹配的，
    这类噪音必然存在，能不能答对它们才是裁定调用的主要价值。
    """
    d = _doc()
    d.add_heading("一、系统概述", level=1)
    typos, traps = [], []

    def typo(text, wrong, right):
        d.add_paragraph(text)
        typos.append({"text": text, "wrong": wrong, "right": right})

    def trap(text, wrong):
        """含错词表左串、但此处写法正确，不该被改。"""
        d.add_paragraph(text)
        traps.append({"text": text, "contains": wrong})

    typo("系统布署完成后，需通过帐号登录管理后台并完成初始化配置。", "布署", "部署")
    typo("响应时间的阀值设定为 200 毫秒，超出后触发告警。", "阀值", "阈值")
    typo("客户端与服务端之间通过秘钥协商建立加密通道。", "秘钥", "密钥")
    typo("本期工做已按计划完成，各模块进入联调阶段。", "工做", "作")
    typo("截止目前，已完成三个一级模块的开发与自测。", "截止目前", "截至目前")
    typo("双方签定合同后十五个工作日内启动实施。", "签定合同", "签订合同")
    typo("运维团队每季度组织一次容灾演炼，覆盖全部核心链路。", "容灾演炼", "容灾演练")
    typo("综上所诉，本方案在成本与工期上均可满足要求。", "综上所诉", "综上所述")

    trap("现场保障人员在野外搭起帐篷，作为临时值守点。", "帐篷")
    trap("本规划以经济效益与社会效益并重为原则。", "以经济")
    trap("每个子节点独立承担一部分计算任务。", "子节点")
    trap("登陆作战演练不在本系统的支撑范围内。", "登陆")

    d.add_heading("二、风险分析", level=1)
    patterns = []

    def pat(text, pid_note, missing):
        d.add_paragraph(text)
        patterns.append({"text": text, "pattern_id": "P-RISK-01",
                         "note": pid_note, "expect_missing": missing})

    pat("风险1：第三方结算接口稳定性不足。若可用性低于 99%，将导致日终对账延迟一个工作日；"
        "拟增加本地缓存与失败重试，并保留人工对账通道，由集成组负责跟进。",
        "影响与应对齐备", [])
    pat("风险2：核心开发人员集中在两人，存在单点依赖，需要重点关注。",
        "缺影响与应对", ["影响", "应对措施"])
    d.add_paragraph("本章分析项目实施过程中面临的主要风险与应对思路。")   # 引导语，scope 应排除

    d.add_heading("三、接口设计", level=1)
    d.add_paragraph("接口：订单查询。入参为订单号（必填）与租户 ID（必填）；"
                    "出参返回订单主体、状态与最后更新时间；订单不存在时返回 40401。")
    d.add_paragraph("接口：订单创建。根据购物车内容生成订单记录。")

    d.save(str(outdir / "typo-pattern.docx"))
    (outdir / "typo-pattern.answers.json").write_text(json.dumps({
        "typos": typos, "traps": traps, "patterns": patterns,
        "notes": [
            "typos：错词表应产出候选；裁定为 B 后经闸门② A1 校验（长度差≤2 且差异字符≤3）",
            "traps：含错词表左串但写法正确。白名单拦掉一部分，其余靠裁定答 A",
            "patterns：P-RISK-01 的 scope 应命中风险1/风险2，排除章节引导语",
            "P 类恒无 suggested_text，动作恒为 comment 或 report_only",
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args()
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    build_basic(out)
    build_negative(out)
    build_logic(out)
    build_planning(out)
    build_style(out)
    build_typo_pattern(out)
    print("fixtures written to", out)


if __name__ == "__main__":
    main()
