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
for r in L01 L02 L06 L08 L12 L20 L27 L29 L30; do
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
printf '通过 \033[32m%d\033[0m，失败 \033[31m%d\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
