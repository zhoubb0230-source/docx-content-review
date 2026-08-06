#!/usr/bin/env bash
# 回归测试：全部为确定性检查，不调用任何 LLM。
# LLM 那一段用 fixtures 里的参考产物替代（相当于一次理想的 Pass 1/2/4 输出），
# 因此本脚本验证的是脚本侧的正确性：闸门、冲突规则、回写与 D9 校验、源文档保护。
#
#   ./run_regression.sh [--keep]      # --keep 保留工作目录便于排障
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL="$(dirname "$HERE")"
S="$SKILL/scripts"
F="$HERE/fixtures"
WORK="$(mktemp -d)"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

# 技能目录只读性检查的基准：在跑任何流程之前先快照 (路径, mtime, 大小)
SKILL_SNAPSHOT="$WORK/skill-before.txt"
skill_state() {
  find "$SKILL" -type f -not -path '*/tests/*' -not -name '*.pyc' \
    -not -path '*__pycache__*' -printf '%p %T@ %s\n' | sort
}

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m✗\033[0m %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1（$3）"; else bad "$1：期望 $3，实际 $2"; fi; }
ge()   { if [ "$2" -ge "$3" ] 2>/dev/null; then ok "$1（$2 ≥ $3）"; else bad "$1：期望 ≥$3，实际 $2"; fi; }
jget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval('d'+sys.argv[1]))" "$1"; }

cleanup(){ [ "$KEEP" = 1 ] && echo "工作目录保留于 $WORK" || rm -rf "$WORK"; }
trap cleanup EXIT

DELIVER="$WORK/deliver"; TEMP="$WORK/temp"
mkdir -p "$DELIVER" "$TEMP"
init_run() {   # $1=fixture 名，回显 run_dir
  python3 "$S/workspace.py" init --source "$F/$1" \
    --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']"
}
seed_facts() { # $1=run_dir  $2=facts fixture —— 按锚点文本回填 pid
  python3 - "$1" "$F/$2" <<'PY'
import json,sys,pathlib
run,src=sys.argv[1],sys.argv[2]
facts=json.load(open(src,encoding="utf-8"))
paras=[json.loads(l) for l in open(f"{run}/work/paragraphs.jsonl",encoding="utf-8")]
def pid(a): return next((p["pid"] for p in paras if a and a in p["text"]),"p-000001")
out={k:[{**{kk:vv for kk,vv in it.items() if kk!="_anchor"},"pid":pid(it.get("_anchor"))}
        for it in v] for k,v in facts.items() if not k.startswith("_")}
d=pathlib.Path(run,"work","facts"); d.mkdir(parents=True,exist_ok=True)
(d/"facts-0001.json").write_text(json.dumps(out,ensure_ascii=False),encoding="utf-8")
PY
}

skill_state > "$SKILL_SNAPSHOT"

echo "══ 1. M1 源文档保护与目录隔离 ══"
BEFORE=$(sha256sum "$F/sample-basic.docx" | cut -d' ' -f1)
SRC_FILES_BEFORE=$(find "$F" -type f | wc -l)
RUN1=$(init_run sample-basic.docx)
python3 "$S/convert_doc.py" --run-dir "$RUN1" >/dev/null
python3 "$S/unpack.py" run --run-dir "$RUN1" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN1" >/dev/null
AFTER=$(sha256sum "$F/sample-basic.docx" | cut -d' ' -f1)
check "源文档 sha256 全程不变" "$([ "$BEFORE" = "$AFTER" ] && echo yes || echo no)" yes
check "源目录无新增文件" "$(find "$F" -type f | wc -l)" "$SRC_FILES_BEFORE"
check "技能目录无运行期写入" \
  "$([ "$(skill_state)" = "$(cat "$SKILL_SNAPSHOT")" ] && echo yes || echo no)" yes

# 同名不同容的两个文档必须完全隔离。两份源文件各放一个子目录——
# 输出根不得等于源文档所在目录（除非它就是 CWD），这本身也是一项守卫。
mkdir -p "$WORK/srcA" "$WORK/srcB"
cp "$F/sample-basic.docx" "$WORK/srcA/dup.docx"
cp "$F/logic-injection.docx" "$WORK/srcB/dup.docx"
RUN_A=$(python3 "$S/workspace.py" init --source "$WORK/srcA/dup.docx" --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
RUN_B=$(python3 "$S/workspace.py" init --source "$WORK/srcB/dup.docx" --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
check "同名不同容文档产物隔离" "$([ "$(dirname "$RUN_A")" != "$(dirname "$RUN_B")" ] && echo yes || echo no)" yes

echo
echo "══ 2. 写路径守卫 ══"
python3 "$S/workspace.py" guard --run-dir "$RUN1" --path /tmp/escape.txt >/dev/null 2>&1
check "拒绝写工作目录之外" "$?" 4
python3 "$S/workspace.py" guard --run-dir "$RUN1" --path "$SKILL/state.json" >/dev/null 2>&1
check "拒绝写技能目录" "$?" 4

echo
echo "══ 3. M2 闸门 ══"
python3 "$S/chunk.py" --run-dir "$RUN1" >/dev/null
python3 - "$RUN1" "$F/sample-basic.answers.json" <<'PY'
import json,sys,pathlib
run,ans=sys.argv[1],json.load(open(sys.argv[2],encoding="utf-8"))
paras=[json.loads(l) for l in open(f"{run}/work/paragraphs.jsonl",encoding="utf-8")]
def pid(sub): return next((p["pid"] for p in paras if sub in p["text"]),"p-999999")
rows=[{"pid":pid(a["original_text"]),"category":a["category"],
       "original_text":a["original_text"],"suggested_text":a["suggested_text"],
       "evidence":(a["note"] or "见类别定义")[:25],"severity":"High"} for a in ans["positive"]]
rows+=[{"pid":"p-000004","category":"A1","original_text":"这段文字文档里根本不存在",
        "suggested_text":"XXXX","evidence":"幻觉","severity":"High"},
       {"pid":"p-000044","category":"A3","original_text":"sudo apt-get install -y libreoffice-writer",
        "suggested_text":"sudo apt-get install -y libreoffice-writer。","evidence":"代码块","severity":"Low"},
       {"pid":"p-000002","category":"Z9","original_text":"本系统采用微服务架构",
        "suggested_text":"","evidence":"无法归类","severity":"Low"}]
d=pathlib.Path(run,"work","issues"); d.mkdir(parents=True,exist_ok=True)
(d/"issues-0001.raw.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
PY
G=$(python3 "$S/verify_span.py" --run-dir "$RUN1" --chunk 0001)
check "幻觉条目被丢弃"       "$(echo "$G" | jget "['hallucination_drop']")" 1
check "无法归类条目被丢弃"   "$(echo "$G" | jget "['unknown_category']")" 1
ge    "不改清单硬过滤生效"   "$(echo "$G" | jget "['neverflag_drop']")" 1
python3 "$S/filter_neverflag.py" --run-dir "$RUN1" --all >/dev/null
NEG=$(python3 "$S/filter_neverflag.py" --probe "执行 kubectl get pods -n prod 查看运行状态。" | jget "['hit']")
check "N9 代码命令命中"      "$NEG" N9

echo
echo "══ 4. M3 冲突检测 L01–L32 ══"
RUN2=$(init_run logic-injection.docx)
python3 "$S/unpack.py" run --run-dir "$RUN2" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN2" >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN2" >/dev/null
seed_facts "$RUN2" logic-injection.facts.json
python3 "$S/ledger.py" build --run-dir "$RUN2" >/dev/null
C=$(python3 "$S/detect_conflicts.py" --run-dir "$RUN2")
FIRED=$(echo "$C" | python3 -c "import json,sys;d=json.load(sys.stdin)['by_rule'];print(sum(1 for k,v in d.items() if v))")
ge "触发的 L 规则数（不含需术语表/TOC 的 L05/L17/L25/L26/L32）" "$FIRED" 25
for r in L01 L02 L06 L08 L11 L12 L20 L23 L24 L27 L29 L30; do
  N=$(echo "$C" | jget "['by_rule']['$r']")
  [ "$N" -ge 1 ] && ok "$r 检出（$N）" || bad "$r 未检出"
done
# ledger.db 删库后可全量重建且结果一致
cp "$RUN2/work/conflicts/index.json" "$WORK/before.json"
rm -f "$RUN2/work/ledger.db"
python3 "$S/ledger.py" rebuild --run-dir "$RUN2" >/dev/null
python3 "$S/detect_conflicts.py" --run-dir "$RUN2" --force >/dev/null
python3 - "$WORK/before.json" "$RUN2/work/conflicts/index.json" <<'PY'
import json,sys
a=json.load(open(sys.argv[1]))["by_rule"]; b=json.load(open(sys.argv[2]))["by_rule"]
sys.exit(0 if a==b else 1)
PY
check "删除 ledger.db 后重建结果一致" "$?" 0

# 参考台账不得凭空造事实。它是回归里「理想的 Pass 1 产出」，一旦掺进文档里
# 根本没写的记录，被验证的就不是规则而是那条杜撰——L23 曾经就是这样"通过"的：
# 靠一条 subject 写「数据层」、锚点却指向讲多租户隔离那句的 status。
for FX in logic-injection planning-tables; do
python3 - "$F/$FX.facts.json" "$RUN2" "$FX" <<'PYEOF'
import json,sys,re
src,run,name=sys.argv[1],sys.argv[2],sys.argv[3]
facts=json.load(open(src,encoding="utf-8"))
paras=[json.loads(l) for l in open(f"{run}/work/paragraphs.jsonl",encoding="utf-8")]
if name!="logic-injection":        # 只有本 run 的段落可比，其余仅查 _anchor 是否齐备
    paras=None
ws=lambda t: "".join(str(t or "").split())
# subject 必须真的出现在锚点段落里；conclusions 的 scope 是章节路径，比标题
SUBJ={"statuses":"subject","positions":"subject","roles":"role",
      "versions":"subject","terms":"term","entities":"name"}
bad=[]
for kind,items in facts.items():
    if kind.startswith("_") or not isinstance(items,list): continue
    for it in items:
        a=it.get("_anchor")
        if not a:
            bad.append(f"{name}/{kind} 缺 _anchor：{it}"); continue
        if paras is None: continue
        hit=next((p for p in paras if a in p["text"]),None)
        if not hit:
            bad.append(f"{name}/{kind} 锚点在正文中找不到：{a!r}"); continue
        if kind in SUBJ:
            v=ws(it.get(SUBJ[kind]))
            if v and v not in ws(hit["text"]):
                bad.append(f"{name}/{kind} 的 {SUBJ[kind]}={v!r} 未出现在锚点段落：{hit['text'][:30]!r}")
        if kind=="conclusions":
            v=ws(it.get("scope"))
            where=ws(hit["text"])+ws("".join(hit.get("heading_path") or []))
            if v and v not in where:
                bad.append(f"{name}/conclusions 的 scope={v!r} 既不在锚点段落也不在其标题路径中")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "参考台账 $FX 的每条事实都能在正文中找到出处" "$?" 0
done

# 权威术语表激活 L25/L26；表自身矛盾时必须终止
python3 "$S/import_glossary.py" --run-dir "$RUN2" \
  --authoritative "$F/sample-authoritative.csv" --fallback "$F/sample-fallback.txt" >/dev/null
C2=$(python3 "$S/detect_conflicts.py" --run-dir "$RUN2" --force --rules L25,L26)
ge "L25 禁用写法（需权威表）" "$(echo "$C2" | jget "['by_rule']['L25']")" 1
ge "L26 变体写法（需权威表）" "$(echo "$C2" | jget "['by_rule']['L26']")" 1
printf '术语,标准写法\n边缘节点,边缘节点\n边缘节点,边沿节点\n' > "$WORK/bad.csv"
python3 "$S/import_glossary.py" --run-dir "$RUN2" --authoritative "$WORK/bad.csv" >/dev/null 2>&1
check "术语表自身矛盾时终止" "$?" 10

echo
echo "══ 5. M5 回写与 D9 样式不变性 ══"
RUN3=$(init_run style-regression.docx)
python3 "$S/unpack.py" run --run-dir "$RUN3" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN3" >/dev/null
python3 - "$RUN3" <<'PY'
import json,sys,pathlib
run=sys.argv[1]
paras=[json.loads(l) for l in open(f"{run}/work/paragraphs.jsonl",encoding="utf-8")]
def pid(s): return next(p["pid"] for p in paras if s in p["text"])
rows=[{"id":"I-0001","pid":pid("帐号"),"category":"A1","original_text":"通过帐号登录",
       "suggested_text":"通过账号登录","verify":{"result":"pass"},"severity":"High"},
      {"id":"I-0002","pid":pid("严格的执行"),"category":"A2","original_text":"严格的执行",
       "suggested_text":"严格地执行","verify":{"result":"pass"},"severity":"High"}]
pathlib.Path(run,"work","issues-verified.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
PY
python3 "$S/apply_revisions.py" plan --run-dir "$RUN3" >/dev/null
R=$(python3 "$S/apply_revisions.py" apply --run-dir "$RUN3")
check "修订全部应用成功" "$(echo "$R" | jget "['failed']")" 0
python3 "$S/apply_comments.py" plan --run-dir "$RUN3" >/dev/null
python3 "$S/apply_comments.py" apply --run-dir "$RUN3" >/dev/null
python3 "$S/validate_docx.py" --run-dir "$RUN3" >/dev/null
check "四项校验全部通过" "$?" 0

# 负向对照：漏拷 rPr 是 D9 最容易被静默违反的地方，校验必须抓到
cp "$RUN3/work/unpacked/word/document.xml" "$WORK/good.xml"
python3 - "$RUN3" "$S" <<'PY'
import sys; sys.path.insert(0,sys.argv[2])
from lxml import etree
import ooxml as ox
p=f"{sys.argv[1]}/work/unpacked/word/document.xml"
t=etree.parse(p); root=t.getroot()
for ins in root.iter(ox.q("ins")):
    for r in ins.iter(ox.q("r")):
        rpr=r.find(ox.q("rPr"))
        if rpr is not None: r.remove(rpr)
t.write(p,xml_declaration=True,encoding="UTF-8",standalone=True)
PY
python3 "$S/validate_docx.py" --run-dir "$RUN3" >/dev/null 2>&1
check "负向对照：漏拷 rPr 被 D9 校验抓到" "$?" 8
cp "$WORK/good.xml" "$RUN3/work/unpacked/word/document.xml"

echo
echo "══ 6. M6 状态、续跑与令牌栅栏 ══"
python3 "$S/state.py" rebuild --run-dir "$RUN2" >/dev/null
DOC2=$(dirname "$RUN2")
python3 "$S/workspace.py" lease acquire --doc-dir "$DOC2" --session s-aaa --stage pass3 >/dev/null
python3 "$S/workspace.py" lease verify --doc-dir "$DOC2" --session s-aaa >/dev/null
check "持令牌者可写" "$?" 0
python3 "$S/workspace.py" lease takeover --doc-dir "$DOC2" --session s-bbb --stage pass3 >/dev/null
python3 "$S/detect_conflicts.py" --run-dir "$RUN2" --session s-aaa --generation 1 >/dev/null 2>&1
check "被接管会话写入以退出码 9 拒绝" "$?" 9
python3 "$S/workspace.py" lease takeover --doc-dir "$DOC2" --session s-ccc --stage writeback >/dev/null 2>&1
check "回写阶段不提供接管" "$?" 2
python3 "$S/state.py" doctor --run-dir "$RUN2" >/dev/null
check "doctor 体检可运行" "$?" 0

echo
echo "══ 7. 报告与产物 ══"
python3 - "$RUN2" <<'PY'
import json,sys,glob,pathlib
run=sys.argv[1]; rows=[]
for f in glob.glob(f"{run}/work/conflicts/conflicts-candidate.*.json"):
    for c in json.load(open(f,encoding="utf-8"))["candidates"]:
        rows.append({"conflict_id":c["conflict_id"],"verdict":"CONFLICT","note":"范围一致"})
pathlib.Path(run,"work","conflicts-verified.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
pathlib.Path(run,"work","issues-verified.jsonl").write_text("",encoding="utf-8")
PY
python3 "$S/metrics.py" collect --run-dir "$RUN2" >/dev/null
RP=$(python3 "$S/report.py" --run-dir "$RUN2")
ge "报告条目数" "$(echo "$RP" | jget "['rows']")" 20
ge "严重级人工确认项" "$(echo "$RP" | jget "['critical']")" 3
DLV=$(python3 "$S/workspace.py" deliver --run-dir "$RUN2")
for k in report issues_xlsx metrics glossary_out; do
  P=$(echo "$DLV" | jget "['artifacts']['$k']['path']")
  [ -s "$P" ] && ok "产出 $k" || bad "缺少 $k（$P）"
done
python3 - "$(echo "$DLV" | jget "['artifacts']['metrics']['path']")" <<'PYEOF'
import sys,json
m=json.load(open(sys.argv[1],encoding="utf-8"))
sys.exit(0 if "gates" in m and "gate_rates" in m else 1)
PYEOF
check "metrics.json 含四项闸门丢弃率" "$?" 0

echo
python3 "$S/workspace.py" init --source "$WORK/srcA/dup.docx" --temp-dir "$WORK/srcA" >/dev/null 2>&1
check "临时根不得等于源文档所在目录" "$?" 6

BEFORE_ALL=$(sha256sum "$F"/*.docx | sha256sum)
check "全部 fixture 文档 sha256 未被改动" \
  "$([ "$(sha256sum "$F"/*.docx | sha256sum)" = "$BEFORE_ALL" ] && echo yes || echo no)" yes

echo
echo "══ 8. 交付路由、命名与批注可读性 ══"
D=$(python3 "$S/workspace.py" deliver --run-dir "$RUN2")
DDIR=$(echo "$D" | jget "['deliver_dir']")
check "交付目录 = 工作目录（非临时目录）" \
  "$([ "$DDIR" = "$DELIVER" ] && echo yes || echo no)" yes
DOCX=$(echo "$D" | jget "['paths']['reviewed_docx']")
STEM=$(basename "$DOCX")
echo "$STEM" | grep -qE '^logic-injection审查版_[0-9]{8}_[0-9]{6}\.docx$' \
  && ok "交付物命名格式（$STEM）" || bad "交付物命名格式不符：$STEM"
# 交付物有且只有 docx；其余四项照常生成但留在 run/output/
NDLV=$(echo "$D" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['paths']))")
check "交付物只有一项" "$NDLV" 1
for k in report issues_xlsx metrics glossary_out; do
  P=$(echo "$D" | jget "['artifacts']['$k']['path']")
  DL=$(echo "$D" | jget "['artifacts']['$k']['delivered']")
  case "$P:$DL" in
    "$RUN2/output/"*":False") ok "$k 生成于 run/output/，未进交付目录";;
    *) bad "$k 去向不对：$P (delivered=$DL)";;
  esac
done
python3 "$S/unpack.py" pack --run-dir "$RUN2" >/dev/null
[ -s "$DOCX" ] && ok "审查版 docx 已产出到交付目录" || bad "审查版 docx 未产出"
for k in report issues_xlsx metrics glossary_out; do
  P=$(echo "$D" | jget "['artifacts']['$k']['path']")
  [ -s "$P" ] && ok "$k 仍照常生成" || bad "$k 未生成（$P）"
done
NDOCX=$(find "$DELIVER" -maxdepth 1 -name '*审查版*' -not -name '*.docx' | wc -l)
check "交付目录内无 docx 之外的审查产物" "$NDOCX" 0
TMPLEFT=$(find "$TEMP" -maxdepth 4 -name '*审查版*' | wc -l)
check "临时目录内不残留交付物" "$TMPLEFT" 0

# 默认行为：不传任何目录参数时，临时根必须跟随工作目录（中间件落在 <CWD>/docx-review/）
mkdir -p "$WORK/cwdtest" && cp "$F/sample-basic.docx" "$WORK/srcC.docx" 2>/dev/null || true
mkdir -p "$WORK/srcC" && cp "$F/sample-basic.docx" "$WORK/srcC/doc.docx"
DEF=$(cd "$WORK/cwdtest" && python3 "$S/workspace.py" init --source "$WORK/srcC/doc.docx")
DEF_TEMP=$(echo "$DEF" | jget "['temp_root']")
DEF_DLV=$(echo "$DEF" | jget "['deliver_dir']")
DEF_RUN=$(echo "$DEF" | jget "['run_dir']")
check "默认临时根 = 工作目录" \
  "$([ "$DEF_TEMP" = "$WORK/cwdtest" ] && echo yes || echo no)" yes
check "默认交付目录 = 工作目录" \
  "$([ "$DEF_DLV" = "$WORK/cwdtest" ] && echo yes || echo no)" yes
case "$DEF_RUN" in "$WORK/cwdtest/docx-review/"*) ok "中间件落在 <工作目录>/docx-review/ 下";;
  *) bad "中间件落在 $DEF_RUN";; esac
[ -d "$WORK/cwdtest/docx-review" ] && ok "工作目录下已建 docx-review/" || bad "未建 docx-review/"

# 批注正文必须是中文说法，规则号只作末尾标记
python3 "$S/apply_comments.py" plan --run-dir "$RUN2" >/dev/null
python3 - "$RUN2" <<'PYEOF'
import json,sys
items=json.load(open(f"{sys.argv[1]}/work/commentlist.json",encoding="utf-8"))["comments"]
bad=[i for i in items if i["text"].startswith("【L") or i["text"].startswith("【A")]
raw=[i for i in items if "kind=" in i["text"] or "scope=" in i["text"]]
sys.exit(0 if not bad and not raw and items else 1)
PYEOF
check "批注正文无裸规则号/字段名" "$?" 0
python3 - "$RUN2" <<'PYEOF'
import json,sys
items=json.load(open(f"{sys.argv[1]}/work/commentlist.json",encoding="utf-8"))["comments"]
# 「以哪一处为准」只能出现在两侧为互斥表述的规则上
wrong=[i for i in items if "以哪一处为准" in i["text"]
       and any(r in i["text"] for r in ("L03","L08","L10","L13","L16","L18","L19","L24",
                                        "L29","L30","L31","L32"))]
sys.exit(0 if not wrong else 1)
PYEOF
check "非互斥类规则不套用「以哪一处为准」" "$?" 0

echo
echo "══ 9. 支线：错别字与范式（P 类） ══"

# 词表与规则包自检——扩表/加规则后最容易踩的四类坑，先卡住
python3 "$S/typo_scan.py" lint >/dev/null 2>&1
check "错词表自检通过（无同形项/单字项/白名单冲突/超 A1 闸门）" "$?" 0
LINT=$(python3 "$S/scan_patterns.py" lint)
ge "内置范式规则数" "$(echo "$LINT" | jget "['rules']")" 2
ge "范式规则携带的正反例数" "$(echo "$LINT" | jget "['examples_total']")" 6

# 缺 scope 定位条件、带建议文本、反例未注明 why —— 三条红线都必须拒绝加载
mkdir -p "$WORK/patterns"
cat > "$WORK/patterns/noscope.yaml" <<'YAML'
patterns:
  - id: P-BAD-01
    name: 无定位条件
    requires: [{key: x, label: X}]
YAML
python3 "$S/scan_patterns.py" lint --patterns "$WORK/patterns/noscope.yaml" >/dev/null 2>&1
check "范式规则缺 scope 定位条件时拒绝加载" "$?" 10
cat > "$WORK/patterns/withsugg.yaml" <<'YAML'
patterns:
  - id: P-BAD-02
    name: 携带建议文本
    scope: {paragraph_regex: "风险"}
    requires: [{key: x, label: X}]
    suggested_text: "补充影响与应对"
YAML
python3 "$S/scan_patterns.py" lint --patterns "$WORK/patterns/withsugg.yaml" >/dev/null 2>&1
check "范式规则携带建议文本时拒绝加载" "$?" 10
cat > "$WORK/patterns/badneg.yaml" <<'YAML'
patterns:
  - id: P-BAD-03
    name: 反例未注明理由
    scope: {paragraph_regex: "风险"}
    requires: [{key: x, label: X}]
    examples:
      negative: [{text: "风险1：需要关注。"}]
YAML
python3 "$S/scan_patterns.py" lint --patterns "$WORK/patterns/badneg.yaml" >/dev/null 2>&1
check "范式反例未注明 why_flag/why_not_flag 时拒绝加载" "$?" 10

# 端到端：同一片上主通道 + 两条支线并存，三份产物互不覆盖
mkdir -p "$WORK/tp"
cp "$F/typo-pattern.docx" "$WORK/tp/typo-pattern.docx"
printf 'pattern_review:\n  enabled: true\n' > "$WORK/tp/cfg.yaml"
RUN3=$(cd "$WORK/tp" && python3 "$S/workspace.py" init --source "$WORK/tp/typo-pattern.docx" \
       --config "$WORK/tp/cfg.yaml" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN3" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN3" >/dev/null
python3 "$S/import_glossary.py" --run-dir "$RUN3" >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN3" >/dev/null

TS=$(python3 "$S/typo_scan.py" scan --run-dir "$RUN3" --config "$WORK/tp/cfg.yaml")
ge "错别字候选覆盖植入的 8 处" "$(echo "$TS" | jget "['candidates']")" 8
PS=$(python3 "$S/scan_patterns.py" scan --run-dir "$RUN3" --config "$WORK/tp/cfg.yaml")
check "范式候选数（2 风险 + 2 接口，章节引导语被 scope 排除）" \
      "$(echo "$PS" | jget "['candidates']")" 4

# 白名单陷阱：含错词表左串但写法正确的四段，一个候选都不该有
python3 - "$RUN3" "$F/typo-pattern.answers.json" <<'PYEOF'
import json,sys
run,ans=sys.argv[1],json.load(open(sys.argv[2],encoding="utf-8"))
cands=json.load(open(f"{run}/work/typos/typos-0001.json",encoding="utf-8"))["candidates"]
hit=[t for t in ans["traps"] if any(c["context"] in t["text"] or t["contains"]==c["wrong"]
                                    for c in cands)]
sys.exit(1 if hit else 0)
PYEOF
check "白名单陷阱未产生候选（帐篷/以经济/子节点/登陆作战）" "$?" 0

# 三通道并存：主通道用真实 pid，支线走各自的裁定
python3 - "$RUN3" <<'PYEOF'
import json,pathlib,sys
run=sys.argv[1]
paras={json.loads(l)["text"]:json.loads(l)["pid"]
       for l in open(f"{run}/work/paragraphs.jsonl",encoding="utf-8")}
pid=next(p for t,p in paras.items() if "阀值" in t)
pathlib.Path(run,"work","issues").mkdir(parents=True,exist_ok=True)
pathlib.Path(run,"work","issues","issues-0001.raw.jsonl").write_text(json.dumps(
 {"pid":pid,"category":"A8","original_text":"设定为 200 毫秒","suggested_text":"设定为 200ms",
  "evidence":"单位不统一","severity":"High"},ensure_ascii=False)+"\n",encoding="utf-8")
t=json.load(open(f"{run}/work/typos/typos-0001.json",encoding="utf-8"))
with open(f"{run}/work/typos/typos-0001.verdicts.jsonl","w",encoding="utf-8") as f:
    for c in t["candidates"]:
        f.write(json.dumps({"pid":c["pid"],"wrong":c["wrong"],"context":c["context"],
                            "verdict":"B"},ensure_ascii=False)+"\n")
p=json.load(open(f"{run}/work/patterns/patterns-0001.json",encoding="utf-8"))
# 第 2 条风险缺影响与应对；owner 答 U（不确定即无问题，不得成条目）
verdicts={0:{"impact":"Y","mitigation":"Y","owner":"Y"},1:{"impact":"N","mitigation":"N","owner":"U"},
          2:{"request":"Y","response":"Y","error":"Y"},3:{"request":"Y","response":"N","error":"N"}}
with open(f"{run}/work/patterns/patterns-0001.verdicts.jsonl","w",encoding="utf-8") as f:
    for i,c in enumerate(p["candidates"]):
        for k,v in verdicts[i].items():
            f.write(json.dumps({"cid":c["cid"],"key":k,"answer":v},ensure_ascii=False)+"\n")
PYEOF
I3="$RUN3/work/issues"
python3 "$S/verify_span.py" --run-dir "$RUN3" --chunk 0001 >/dev/null
python3 "$S/typo_scan.py" merge --run-dir "$RUN3" --chunk 0001 --config "$WORK/tp/cfg.yaml" >/dev/null
python3 "$S/verify_span.py" --run-dir "$RUN3" --chunk 0001 \
  --in "$I3/issues-0001.typos.jsonl" --out "$I3/issues-0001.typos.jsonl" --cap 100 >/dev/null
python3 "$S/filter_neverflag.py" --run-dir "$RUN3" --chunk 0001 --file "$I3/issues-0001.typos.jsonl" >/dev/null
PM=$(python3 "$S/scan_patterns.py" merge --run-dir "$RUN3" --chunk 0001 --config "$WORK/tp/cfg.yaml")
check "范式条目数（风险2 缺 2 项 + 接口2 缺 2 项，各合并为 1 条）" "$(echo "$PM" | jget "['merged']")" 2
python3 "$S/verify_span.py" --run-dir "$RUN3" --chunk 0001 \
  --in "$I3/issues-0001.patterns.jsonl" --out "$I3/issues-0001.patterns.jsonl" --cap 20 >/dev/null
python3 "$S/filter_neverflag.py" --run-dir "$RUN3" --chunk 0001 --file "$I3/issues-0001.patterns.jsonl" >/dev/null

check "主通道产物未被支线覆盖" "$(wc -l < "$I3/issues-0001.jsonl")" 1
ge   "错别字支线产物条数"     "$(wc -l < "$I3/issues-0001.typos.jsonl")" 8
check "范式支线产物条数"       "$(wc -l < "$I3/issues-0001.patterns.jsonl")" 2
check "三条通道的闸门统计各自成档" \
      "$(ls "$I3" | grep -c 'gates.json')" 3

# P 类的三条硬约束：不带建议、不升到 High、rule_id 保留规则包里的开放标识
python3 - "$I3/issues-0001.patterns.jsonl" <<'PYEOF'
import json,sys
rows=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8")]
assert rows, "无范式条目"
assert all(not r.get("suggested_text") for r in rows), "P 类出现了建议文本"
assert all(r["action"] in ("comment","report_only") for r in rows), "P 类动作越界"
assert all(r["severity"] in ("Medium","Low") for r in rows), "P 类严重度超过 Medium"
assert all(r["rule_id"].startswith("P-") for r in rows), "P 类 rule_id 丢了规则包标识"
assert all(r["category"]=="P1" for r in rows), "P 类 category 不是 P1"
# owner 答 U 的那条不得成为缺失项
assert not any("责任人" in (r.get("evidence") or "") for r in rows), "U 被当成了缺失"
PYEOF
check "P 类：无建议文本 / 严重度≤Medium / 保留规则号 / U 不成条目" "$?" 0

# 闸门③ 不得用词级规则误杀整段的 P 类条目
python3 - "$I3/issues-0001.patterns.neverflag.json" <<'PYEOF'
import json,sys
sys.exit(0 if json.load(open(sys.argv[1],encoding="utf-8"))["dropped"]==0 else 1)
PYEOF
check "闸门③ 未用词级规则误杀 P 类条目" "$?" 0

# 关掉开关后必须彻底静默（默认配置即为关闭）
PD=$(python3 "$S/scan_patterns.py" scan --run-dir "$RUN3")
check "pattern_review 关闭时不产生任何候选" "$(echo "$PD" | jget "['candidates']")" 0

echo
echo "══ 10. 截断的类别偏差与答案清单的闸门期望 ══"

mkdir -p "$WORK/sb"
cp "$F/sample-basic.docx" "$WORK/sb/sample-basic.docx"
RUN4=$(cd "$WORK/sb" && python3 "$S/workspace.py" init --source "$WORK/sb/sample-basic.docx" | jget "['run_dir']")
for c in "unpack.py run" "extract.py" "import_glossary.py" "chunk.py"; do
  python3 "$S/${c%% *}" ${c#* } --run-dir "$RUN4" >/dev/null 2>&1 || \
  python3 "$S/${c%% *}" --run-dir "$RUN4" >/dev/null
done

# 把答案清单灌进闸门②：它的 expect 字段声明了每条应当变成修订还是被降级为批注
python3 - "$RUN4" "$F/sample-basic.answers.json" <<'PYEOF'
import json,pathlib,sys
run,key=sys.argv[1],json.load(open(sys.argv[2],encoding="utf-8"))
paras=[json.loads(l) for l in open(f"{run}/work/paragraphs.jsonl",encoding="utf-8")]
rows=[]
for e in key["positive"]:
    pid=next((p["pid"] for p in paras if e["original_text"] in p["text"]),None)
    if not pid:
        print("答案清单条目在正文中找不到：", e["original_text"]); sys.exit(1)
    rows.append({"pid":pid,"category":e["category"],"original_text":e["original_text"],
                 "suggested_text":e.get("suggested_text",""),"evidence":"答案清单",
                 "severity":"High" if e["category"][0]=="A" else "Medium"})
pathlib.Path(run,"work","issues").mkdir(parents=True,exist_ok=True)
pathlib.Path(run,"work","issues","key.raw.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
PYEOF
check "答案清单每条都能在正文中逐字定位" "$?" 0
K="$RUN4/work/issues"
python3 "$S/verify_span.py" --run-dir "$RUN4" --chunk 0001 \
  --in "$K/key.raw.jsonl" --out "$K/key.jsonl" --cap 99 >/dev/null
python3 - "$K/key.jsonl" "$F/sample-basic.answers.json" <<'PYEOF'
import json,sys
got={ (r["category"], r["original_text"]): r for r in
      (json.loads(l) for l in open(sys.argv[1],encoding="utf-8")) }
key=json.load(open(sys.argv[2],encoding="utf-8"))["positive"]
bad=[]
for e in key:
    r=got.get((e["category"], e["original_text"]))
    if r is None:
        bad.append(f'{e["category"]} {e["original_text"][:16]} 被闸门丢弃'); continue
    want=e.get("expect","revision")
    # expect=revision → 建议必须留下；expect=comment → 建议必须被清空（降级）
    if want=="revision" and e.get("suggested_text") and not r["suggested_text"]:
        bad.append(f'{e["category"]} {e["original_text"][:16]} 本应落笔却被降级：{r["gate_note"]}')
    if want=="comment" and r["suggested_text"]:
        bad.append(f'{e["category"]} {e["original_text"][:16]} 本应降级却落了笔')
for b in bad: print("  ", b)
sys.exit(1 if bad else 0)
PYEOF
check "闸门②对每条答案的处置与 expect 一致（含两条只能重写的 A5 被降级）" "$?" 0

# 截断不得按类别整组砍：A 类恒 High、B 类恒 Medium，纯按 severity 排序会让 B 类全灭
python3 "$S/verify_span.py" --run-dir "$RUN4" --chunk 0001 \
  --in "$K/key.raw.jsonl" --out "$K/capped.jsonl" >/dev/null
python3 - "$K/capped.jsonl" <<'PYEOF'
import json,sys
rows=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8")]
b=[r for r in rows if r["category"].startswith("B")]
print(f"   截断后保留 {len(rows)} 条，其中 B 类 {len(b)} 条")
sys.exit(0 if len(b)>=5 else 1)
PYEOF
check "28 条正例过 cap=20 后，B 类保底席位未被 A 类挤占" "$?" 0

# 保底席位的边界：它是为了让少数类不被整组挤掉，不能反过来让 B 类独占配额
python3 - "$RUN4" "$K" "$S" <<'PYEOF'
import json,pathlib,subprocess,sys
run,I,S=sys.argv[1],sys.argv[2],sys.argv[3]
rows=[json.loads(l) for l in open(f"{I}/key.raw.jsonl",encoding="utf-8")]
A=[r for r in rows if r["category"].startswith("A")]
B=[r for r in rows if r["category"].startswith("B")]
def case(subset, cap):
    pathlib.Path(I,"edge.raw.jsonl").write_text(
        "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in subset),encoding="utf-8")
    cmd=["python3",f"{S}/verify_span.py","--run-dir",run,"--chunk","0001",
         "--in",f"{I}/edge.raw.jsonl","--out",f"{I}/edge.jsonl","--cap",str(cap)]
    subprocess.run(cmd,capture_output=True,text=True)
    got=[json.loads(l) for l in open(f"{I}/edge.jsonl",encoding="utf-8")]
    return (sum(1 for r in got if r["category"].startswith("A")),
            sum(1 for r in got if r["category"].startswith("B")))
bad=[]
if case(A, 20)[1] != 0:                bad.append("无 B 类时不应凭空保留 B")
if case(A+B[:2], 20)[1] != 2:          bad.append("B 类少于席位数时应全留")
if case(A+B, 1) != (1, 0):             bad.append("cap=1 时应让位给高严重度项，而不是给 B 类")
a3,b3 = case(A+B, 3)
if b3 > 1 or a3 < 2:                   bad.append(f"cap=3 时席位应压到 1（得到 A{a3}+B{b3}）")
for x in bad: print("   ", x)
sys.exit(1 if bad else 0)
PYEOF
check "保底席位边界：无 B 类/不足席位/cap 小于席位数时不反噬 A 类" "$?" 0

# 负样本集：34 段合法表达，任何一条上报都是误报
mkdir -p "$WORK/ns"
cp "$F/negative-set.docx" "$WORK/ns/negative-set.docx"
RUN5=$(cd "$WORK/ns" && python3 "$S/workspace.py" init --source "$WORK/ns/negative-set.docx" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN5" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN5" >/dev/null
python3 "$S/import_glossary.py" --run-dir "$RUN5" >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN5" >/dev/null
# 错别字通道在纯合法文本上必须零候选——负样本里有「其它」「渡口」这类词表边界情形
TN=$(python3 "$S/typo_scan.py" scan --run-dir "$RUN5")
check "负样本集上错别字通道零候选" "$(echo "$TN" | jget "['candidates']")" 0

echo
echo "══ 11. 闸门④：盲测 A/B 脚手架 ══"

# RUN4 已有 28 条正例过闸产物（第 10 节灌入）；复用它建复核集
python3 "$S/verify_span.py" --run-dir "$RUN4" --chunk 0001 --in "$K/key.raw.jsonl" >/dev/null
B1=$(python3 "$S/verify_pass2.py" build --run-dir "$RUN4")
ge "待复核条目数" "$(echo "$B1" | jget "['items']")" 10
python3 "$S/verify_pass2.py" build --run-dir "$RUN4" --arrangement mirror >/dev/null

# 盲测的全部意义在于 payload 不泄题：既不能带「原文在哪一侧」，
# 也不能让上下文原样带出待判跨度——同批条目的上下文彼此重叠，只挡本条远远不够
python3 - "$RUN4" <<'PYEOF'
import json,sys
run=sys.argv[1]; V=f"{run}/work/verify"
bad=[]
raw=open(f"{V}/pass2-primary.json",encoding="utf-8").read()
for f in ("orig_side","suggested_text","category","severity","evidence"):
    if f in raw: bad.append(f"payload 含字段 {f}")
for arr in ("primary","mirror"):
    d=json.load(open(f"{V}/pass2-{arr}.json",encoding="utf-8"))
    for b in d["batches"]:
        whole=" ".join(x.get("context") or "" for x in b["items"])
        for x in b["items"]:
            if x["form"]!="ab": continue
            if x["A"] in whole or x["B"] in whole:
                bad.append(f"{arr}/{b['batch_id']} 的选项出现在同批上下文里：{x['A'][:12]!r}")
for x in bad[:6]: print("   ", x)
sys.exit(1 if bad else 0)
PYEOF
check "payload 不泄题（无侧别字段，选项不出现在任何上下文里）" "$?" 0

# 位置由 ab_seed 逐条派生 → 可复现；mirror 必须是逐条镜像
python3 - "$RUN4" <<'PYEOF'
import json,sys
run=sys.argv[1]; V=f"{run}/work/verify"
k1=json.load(open(f"{V}/pass2-primary.key.json",encoding="utf-8"))["key"]
k2=json.load(open(f"{V}/pass2-mirror.key.json",encoding="utf-8"))["key"]
ab=[i for i,v in k1.items() if v["form"]=="ab"]
bad=[i for i in ab if k2[i]["orig_side"]==k1[i]["orig_side"]]
same=len({v["orig_side"] for i,v in k1.items() if v["form"]=="ab"})
if bad: print(f"    {len(bad)} 条在 mirror 里没有翻面")
if same<2: print("    primary 的位置全落在同一侧，随机化没生效")
sys.exit(1 if bad or same<2 else 0)
PYEOF
check "mirror 逐条镜像，且 primary 的位置两侧都有" "$?" 0

python3 "$S/verify_pass2.py" build --run-dir "$RUN4" >/dev/null
H1=$(python3 - "$RUN4" <<'PYEOF'
import hashlib,sys;print(hashlib.sha256(open(f"{sys.argv[1]}/work/verify/pass2-primary.key.json","rb").read()).hexdigest())
PYEOF
)
python3 "$S/verify_pass2.py" build --run-dir "$RUN4" >/dev/null
H2=$(python3 - "$RUN4" <<'PYEOF'
import hashlib,sys;print(hashlib.sha256(open(f"{sys.argv[1]}/work/verify/pass2-primary.key.json","rb").read()).hexdigest())
PYEOF
)
check "重跑 build 得到完全相同的排列（ab_seed 可复现）" "$([ "$H1" = "$H2" ] && echo yes || echo no)" yes

# 判定表的负向对照：五条淘汰路径都要走通，不能只验通过路径
python3 - "$RUN4" <<'PYEOF'
import json,sys,pathlib
run=sys.argv[1]; V=pathlib.Path(run,"work","verify")
key=json.load(open(V/"pass2-primary.key.json",encoding="utf-8"))["key"]
ab=[i for i,v in key.items() if v["form"]=="ab"]
sg=[i for i,v in key.items() if v["form"]=="single"]
rows=[]
for n,i in enumerate(ab):
    o=key[i]["orig_side"]; flip="B" if o=="A" else "A"
    rows.append({"id":i,"answer":[o,flip,"两者都没有","两者都有","???"][min(n,4)]})
rows=[r for r in rows if r["id"]!=ab[-1]]        # 末条整体缺裁定
for n,i in enumerate(sg):
    rows.append({"id":i,"answer":["YES","NO","UNSURE"][min(n,2)]})
(V/"pass2-primary.verdicts.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
PYEOF
M=$(python3 "$S/verify_pass2.py" merge --run-dir "$RUN4")
python3 - "$RUN4" <<'PYEOF'
import json,sys
want={"选中建议所在项","模型认为原文没问题","模型未能区分，视为不可靠",
      "缺裁定结果，按淘汰处理"}
got={json.loads(l)["verify"].get("note","") for l in
     open(f"{sys.argv[1]}/work/issues-verified.jsonl",encoding="utf-8")}
miss=[w for w in want if w not in got]
unparsed=[g for g in got if g.startswith("无法解析")]
unsure=[g for g in got if "UNSURE" in g]
for m in miss: print("    未走到淘汰路径：", m)
if not unparsed: print("    未走到淘汰路径：无法解析的回答")
if not unsure:   print("    未走到淘汰路径：UNSURE 按 NO")
sys.exit(1 if miss or not unparsed or not unsure else 0)
PYEOF
check "判定表六条淘汰路径全部走通（负向对照）" "$?" 0
ge "淘汰计数" "$(echo "$M" | jget "['drop']")" 5

# 一致率：M2 验收项，此前根本算不出来
python3 - "$RUN4" <<'PYEOF'
import json,pathlib,sys
V=pathlib.Path(sys.argv[1],"work","verify")
key=json.load(open(V/"pass2-primary.key.json",encoding="utf-8"))["key"]
prim={json.loads(l)["id"]:json.loads(l)["answer"]
      for l in open(V/"pass2-primary.verdicts.jsonl",encoding="utf-8")}
mir={k:("B" if v=="A" else "A" if v=="B" else v) for k,v in prim.items()}
(V/"pass2-mirror.verdicts.jsonl").write_text(
    "".join(json.dumps({"id":k,"answer":v},ensure_ascii=False)+"\n" for k,v in mir.items()),
    encoding="utf-8")
PYEOF
CONS=$(python3 "$S/verify_pass2.py" consistency --run-dir "$RUN4")
check "两种排列判定一致率可计算且达标" "$(echo "$CONS" | jget "['meets_threshold']")" True

echo
printf '通过 \033[32m%d\033[0m，失败 \033[31m%d\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
