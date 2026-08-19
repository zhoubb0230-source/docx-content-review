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

# 依赖预检。缺 lxml 时不预检的后果是几十个失败 + 满屏 traceback，
# 而真正的原因只有一句话。退出码 3 = 环境缺失，与脚本自己的约定一致。
MISSING=$(python3 - <<'PY'
import importlib.util
print(" ".join(m for m, pkg in (("lxml","lxml"),("yaml","pyyaml"),("openpyxl","openpyxl"))
                if not importlib.util.find_spec(m)))
PY
)
if [ -n "$MISSING" ]; then
  printf '\033[31m[终止]\033[0m 缺少运行期依赖：%s\n' "$MISSING" >&2
  echo "请先执行：pip install lxml openpyxl pyyaml" >&2
  exit 3
fi

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
# 术语类七条默认关闭（logic.term_rules: false）。关掉不等于不留痕：
# 下游是按 glob 读候选目录的，旧候选文件必须被清成空，否则等于没关。
C0=$(python3 "$S/detect_conflicts.py" --run-dir "$RUN2")
python3 - "$RUN2" "$C0" <<'PYEOF'
import json,sys,pathlib
run,out=sys.argv[1],json.loads(sys.argv[2])
TERM={"L01","L02","L03","L04","L05","L25","L26"}
bad=[]
if set(out.get("by_rule",{}))&TERM: bad.append(f"关闭状态下仍产出了术语类候选：{out['by_rule']}")
idx=json.load(open(f"{run}/work/conflicts/index.json",encoding="utf-8"))
if set(idx.get("skipped_rules") or [])!=TERM: bad.append(f"index 未记全跳过的规则：{idx.get('skipped_rules')}")
for r in sorted(TERM):
    p=pathlib.Path(run,"work","conflicts",f"conflicts-candidate.{r}.json")
    if not p.exists(): bad.append(f"{r} 的候选文件不存在（下游 glob 读不到 = 状态不明）"); continue
    d=json.load(open(p,encoding="utf-8"))
    if d.get("candidates"): bad.append(f"{r} 关闭后仍留着 {len(d['candidates'])} 条候选")
    if not d.get("skipped_reason"): bad.append(f"{r} 的空文件没标 skipped_reason")
for b in bad: print("   ",b)
sys.exit(1 if bad else 0)
PYEOF
check "术语类 L01–L05/L25/L26 默认不跑，旧候选被清空" "$?" 0
G0=$(python3 "$S/glossary_scan.py" --run-dir "$RUN2")
check "关闭时 Pass 0 术语抽取整步跳过（零 batch，不需发起调用）" \
  "$(echo "$G0" | jget "['batches']")" 0

# 打开开关后必须重算——空文件不能被当成「已完成」，否则开了也不生效
printf 'logic:\n  term_rules: true\n' > "$WORK/term-on.yaml"
C=$(python3 "$S/detect_conflicts.py" --run-dir "$RUN2" --config "$WORK/term-on.yaml")
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
python3 "$S/detect_conflicts.py" --run-dir "$RUN2" --force --config "$WORK/term-on.yaml" >/dev/null
python3 - "$WORK/before.json" "$RUN2/work/conflicts/index.json" <<'PY'
import json,sys
a=json.load(open(sys.argv[1]))["by_rule"]; b=json.load(open(sys.argv[2]))["by_rule"]
sys.exit(0 if a==b else 1)
PY
check "删除 ledger.db 后重建结果一致" "$?" 0

# 分片重跑后 facts 文件被改写，build 必须按 sha 重新入库——
# 只按 chunk_id 判重的话，库里留的是上一轮的旧事实，而 Pass 3 全部建立在它之上
python3 - "$RUN2" <<'PY'
import json,re,sys,pathlib
run=pathlib.Path(sys.argv[1])
f=run/"work"/"facts"/"facts-0001.json"
d=json.load(open(f,encoding="utf-8"))
# 事实必须落地：pid 要真实存在，数值要在那一段里找得到（ledger 的幻觉闸门）
pid=num=None
for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8"):
    q=json.loads(l); m=re.search(r"[0-9]+", q["text"] or "")
    if m: pid,num=q["pid"],m.group(0); break
assert pid, "语料里没有带数字的段落，构造无效"
d.setdefault("metrics",[]).append({"pid":pid,"subject":"重跑后新增指标",
                                   "value":num,"unit":"ms","kind":"目标"})
f.write_text(json.dumps(d,ensure_ascii=False),encoding="utf-8")
PY
LB=$(python3 "$S/ledger.py" build --run-dir "$RUN2")
check "facts 变更后 build 重新入库（不是按 chunk_id 判重）" \
      "$(echo "$LB" | jget "['chunks_refreshed']")" 1
python3 - "$RUN2" <<'PY'
import sqlite3,sys
con=sqlite3.connect(f"{sys.argv[1]}/work/ledger.db")
n=con.execute("SELECT COUNT(*) FROM facts WHERE subject='重跑后新增指标'").fetchone()[0]
d=con.execute("SELECT COUNT(*) FROM facts WHERE chunk_id='0001'").fetchone()[0]
old=con.execute("SELECT rows FROM ingested WHERE chunk_id='0001'").fetchone()[0]
sys.exit(0 if n==1 and d==old else 1)   # 新事实入库；旧行清干净，没有重复
PY
check "重新入库不产生重复行（先删旧行再导入，幂等）" "$?" 0
LB2=$(python3 "$S/ledger.py" build --run-dir "$RUN2")
check "内容未变时不重复入库" "$(echo "$LB2" | jget "['chunks_refreshed']")" 0
python3 "$S/ledger.py" rebuild --run-dir "$RUN2" >/dev/null
python3 "$S/detect_conflicts.py" --run-dir "$RUN2" --force --config "$WORK/term-on.yaml" >/dev/null

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
C2=$(python3 "$S/detect_conflicts.py" --run-dir "$RUN2" --force --rules L25,L26 --config "$WORK/term-on.yaml")
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
def ptext(s): return next(p["text"] for p in paras if s in p["text"])
rows=[{"id":"I-0001","pid":pid("帐号"),"category":"A1","original_text":"通过帐号登录",
       "suggested_text":"通过账号登录","verify":{"result":"pass"},"severity":"High",
       "evidence":"「帐号」为异体写法"},
      {"id":"I-0002","pid":pid("严格的执行"),"category":"A2","original_text":"严格的执行",
       "suggested_text":"严格地执行","verify":{"result":"pass"},"severity":"High"},
      # 只出批注不落修订的一条：锚点是整段（跨三个不同 rPr 的 run）
      {"id":"I-0003","pid":pid("本段前半部分"),"category":"B1",
       "original_text":ptext("本段前半部分"),"verify":{"result":"pass"},
       "severity":"Medium","evidence":"全段语义重复"},
      # 锚点只是段落里的一小截：范围必须精确到字符，不能圈住整段
      {"id":"I-0004","pid":pid("编号列表第 1 项"),"category":"B1",
       "original_text":"内容为三号仿宋","verify":{"result":"pass"},
       "severity":"Low","evidence":"指代不明"}]
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

# 每一处修订都必须带上「为什么改」的批注：只有修订标记的话，
# 评审人只能整批接受或整批拒绝，等于把判断又丢回给人。
python3 - "$RUN3" "$S" <<'PY'
import json,sys
sys.path.insert(0,sys.argv[2])
from lxml import etree
import ooxml as ox
run=sys.argv[1]
prov=json.load(open(f"{run}/work/revision-provenance.json",encoding="utf-8"))
patches={p["patch_id"]:p for p in
         json.load(open(f"{run}/work/patchlist.json",encoding="utf-8"))["patches"]}
items=json.load(open(f"{run}/work/commentlist.json",encoding="utf-8"))["comments"]
rev={i["ref"]:i for i in items if i.get("kind")=="revision"}
root=etree.parse(f"{run}/work/unpacked/word/document.xml").getroot()
cov=ox.comment_coverage(root)
bad=[]
if not prov["applied"]: bad.append("本轮没有任何修订，这条断言失去意义")
for pid_ in prov["applied"]:
    it=rev.get(pid_)
    if not it:
        bad.append(f"修订 {pid_} 没有对应的说明批注"); continue
    head,*rest=it["text"].split("\n")
    # 抬头只有类别标签，不带「已在此处标为修订，请确认后接受或拒绝」这类套话
    if "—" in head or len(head)>12:
        bad.append(f"修订 {pid_} 的批注抬头带了套话：{head}")
    if len(rest)<2:
        bad.append(f"修订 {pid_} 的批注没给出改动理由：{it['text']}")
    c=cov.get(str(it["comment_id"]))
    if not c:
        bad.append(f"修订 {pid_} 的批注没有锚定范围"); continue
    # 范围 = **这处改动所在的整句**。评审人要看到的是「这句话被改了」：
    #   只圈 del/ins 高亮只有两三个字，看不出改的是哪句；
    #   圈住整段则又回到「不知道问题在哪」（这正是现场反馈的那条）。
    # 所以断言两头：拒绝视图里必须含原文、接受视图里必须含建议，
    # 且范围不得越过句子边界。
    if patches[pid_]["original_text"] not in c["reject"]:
        bad.append(f"修订 {pid_} 的批注范围没圈住原文："
                   f"{c['reject']!r} 不含 {patches[pid_]['original_text']!r}")
    if patches[pid_]["suggested_text"] not in c["accept"]:
        bad.append(f"修订 {pid_} 的批注范围没圈住建议：{c['accept']!r}")
    inner = c["reject"].strip()
    if any(ch in inner[:-1] for ch in "。！？"):
        bad.append(f"修订 {pid_} 的批注范围跨了句子边界：{inner!r}")
for b in bad: print("   ",b)
sys.exit(1 if bad else 0)
PY
check "每处修订都有说明批注，范围是该处改动所在的整句" "$?" 0

# 锚点是段落里的一小截时，范围必须精确到字符——真实文档上验一遍，
# 顺带证明「为锚定而拆 run」没有动到正文与 rPr（四项校验在上面已经跑过）
python3 - "$RUN3" "$S" <<'PY'
import json,sys; sys.path.insert(0,sys.argv[2])
from lxml import etree
import ooxml as ox
run=sys.argv[1]
items=json.load(open(f"{run}/work/commentlist.json",encoding="utf-8"))["comments"]
root=etree.parse(f"{run}/work/unpacked/word/document.xml").getroot()
cov=ox.comment_coverage(root)
paras={f"p-{i:06d}":p for i,p in enumerate(root.iter(ox.q("p")),1)}
bad=[]
for it in items:
    if it.get("kind")=="revision" or not it.get("anchor"): continue
    got=cov.get(str(it["comment_id"]),{}).get("reject","")
    if got!=it["anchor"]:
        whole="".join(t.text or "" for t in paras[it["pid"]].iter(ox.q("t")))
        bad.append(f"批注 {it['comment_id']} 圈住 {len(got)} 字，锚点 {len(it['anchor'])} 字"
                   f"（全段 {len(whole)} 字）：{got[:40]!r}")
for b in bad: print("   ",b)
sys.exit(1 if bad else 0)
PY
check "批注范围精确到锚点字符边界（不放大到整段）" "$?" 0

# 批注范围必须覆盖完整正文。旧实现拿「可拆分 run」的首尾当边界，
# 于是含 w:br/w:tab/图形的 run 被排除在外——在 Word 里就是「只选中前面几行」。
python3 - "$S" <<'PY'
import sys
sys.path.insert(0,sys.argv[1])
from lxml import etree
import ooxml as ox
from apply_comments import _anchor_paragraph
W=ox.W
def para(*specs):
    p=etree.Element(ox.q("p"),nsmap={"w":W}); etree.SubElement(p,ox.q("pPr"))
    for kind,txt in specs:
        r=etree.SubElement(p,ox.q("r"))
        if kind in ("t","tbr"):
            t=etree.SubElement(r,ox.q("t")); t.text=txt
        if kind in ("br","tbr"): etree.SubElement(r,ox.q("br"))
    return p
def covered(p,cid=1):
    d=etree.Element(ox.q("document"),nsmap={"w":W}); d.append(p)
    return ox.comment_coverage(d).get(str(cid),{}).get("reject","")
bad=[]
cases={"末尾 run 含软换行":para(("t","前文"),("tbr","后文")),
       "整段只有一个含换行的 run":para(("tbr","第一行"),),
       "每行都带软换行":para(("tbr","第一行"),("tbr","第二行"),("t","第三行")),
       "普通多 run 段落":para(("t","甲乙丙"),("t","丁戊"))}
for name,p in cases.items():
    want="".join(t.text or "" for t in p.iter(ox.q("t")))
    _anchor_paragraph(p,"",1)
    got=covered(p)
    if got!=want: bad.append(f"{name}：整段锚定只圈住 {got!r}，应为 {want!r}")
# 给了锚点就必须精确到字符：合并后整段常常只有一只 run，
# 边界若只能落在 run 之间，「一句话有语病」会圈住整段两百多字。
p=para(("t","第一句有语病"),("t","第二句没问题"))
_anchor_paragraph(p,"有语病",1)
if covered(p)!="有语病": bad.append(f"精确锚点未切到字符边界：{covered(p)!r}")
long_p="本节说明系统的整体架构与关键取舍。"*5+"为了更好的支撑业务增长，平台采用分层设计。"+"其余部分从略。"*4
p=para(("t",long_p))
before=[ox.rpr_key(r) for r in p.iter(ox.q("r"))]
_anchor_paragraph(p,"为了更好的支撑业务增长",1)
if covered(p)!="为了更好的支撑业务增长":
    bad.append(f"单 run 长段落里的锚点被放大：{len(covered(p))} 字 / 全段 {len(long_p)} 字")
if "".join(t.text or "" for t in p.iter(ox.q("t")))!=long_p:
    bad.append("为锚定而拆 run 改动了正文")
if set(before)!={ox.rpr_key(r) for r in p.iter(ox.q("r")) if ox.run_text(r)}:
    bad.append("为锚定而拆 run 引入了新的 rPr 指纹（违反 D9）")
# 同段第二条批注不受第一条的引用符影响
p=para(("t","甲乙丙"),("t","丁戊"))
_anchor_paragraph(p,"",1); _anchor_paragraph(p,"丁戊",2)
d=etree.Element(ox.q("document"),nsmap={"w":W}); d.append(p)
c=ox.comment_coverage(d)
if c["1"]["reject"]!="甲乙丙丁戊" or c["2"]["reject"]!="丁戊":
    bad.append(f"同段两条批注互相干扰：{c}")
for b in bad: print("   ",b)
sys.exit(1 if bad else 0)
PY
check "批注范围覆盖完整正文（含 w:br 等不可拆分 run）" "$?" 0

cp "$RUN3/work/unpacked/word/document.xml" "$WORK/good.xml"
# 负向对照一：范围塌成零长度 —— Start/End 齐备，但 Word 里选不中任何字
python3 - "$RUN3" "$S" <<'PY'
import sys; sys.path.insert(0,sys.argv[2])
from lxml import etree
import ooxml as ox
p=f"{sys.argv[1]}/work/unpacked/word/document.xml"
t=etree.parse(p); root=t.getroot()
s=next(iter(root.iter(ox.q("commentRangeStart"))))
cid=s.get(ox.q("id"))
for e in root.iter(ox.q("commentRangeEnd")):
    if e.get(ox.q("id"))==cid:
        e.getparent().remove(e); break
s.addnext(e)
t.write(p,xml_declaration=True,encoding="UTF-8",standalone=True)
PY
python3 "$S/validate_docx.py" --run-dir "$RUN3" >/dev/null 2>&1
check "负向对照：批注范围为空被校验抓到" "$?" 8
cp "$WORK/good.xml" "$RUN3/work/unpacked/word/document.xml"

# 负向对照二：范围只盖住锚点的前半截 —— 正是用户看到的「只选中前面几行」
python3 - "$RUN3" "$S" <<'PY'
import json,sys; sys.path.insert(0,sys.argv[2])
from lxml import etree
import ooxml as ox
run=sys.argv[1]
items=json.load(open(f"{run}/work/commentlist.json",encoding="utf-8"))["comments"]
cid=str(next(i["comment_id"] for i in items if i.get("kind")!="revision"))
p=f"{run}/work/unpacked/word/document.xml"
t=etree.parse(p); root=t.getroot()
end=next(e for e in root.iter(ox.q("commentRangeEnd")) if e.get(ox.q("id"))==cid)
start=next(e for e in root.iter(ox.q("commentRangeStart")) if e.get(ox.q("id"))==cid)
para=start.getparent()
first=ox.para_content_nodes(para)[0]
end.getparent().remove(end)
first.addnext(end)                     # 范围缩到只剩第一个 run
t.write(p,xml_declaration=True,encoding="UTF-8",standalone=True)
PY
python3 "$S/validate_docx.py" --run-dir "$RUN3" >/dev/null 2>&1
check "负向对照：批注只圈住锚点前半截被校验抓到" "$?" 8
cp "$WORK/good.xml" "$RUN3/work/unpacked/word/document.xml"

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
check "错词表自检通过（同形/单字/白名单冲突/超 A1 闸门/撞负向语料）" "$?" 0
LT=$(python3 "$S/typo_scan.py" lint)
ge "负向语料条数（合法句子，命中即误报）" "$(echo "$LT" | jget "['traps']")" 30

# 负向对照：加一个碎片式左串（「按全」会被「按全流程」拆出来），自检必须失败。
# 用 --typos 指向副本，**不碰技能目录**——它在运行期只读，第 1 节有断言。
cp "$SKILL/assets/dict/common-typos.txt" "$WORK/ct-bad.txt"
printf '按全\t安全\t负向对照\n' >> "$WORK/ct-bad.txt"
python3 "$S/typo_scan.py" lint --typos "$WORK/ct-bad.txt" >/dev/null 2>&1
check "负向对照：碎片式左串被负向语料抓出" "$?" 10
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

# 关掉开关后必须彻底静默
printf 'pattern_review:\n  enabled: false\n' > "$WORK/tp/off.yaml"
PD=$(python3 "$S/scan_patterns.py" scan --run-dir "$RUN3" --config "$WORK/tp/off.yaml")
check "pattern_review 关闭时不产生任何候选" "$(echo "$PD" | jget "['candidates']")" 0

# 不带 --config 时读的是**本次 run 的配置快照**，不是默认配置。
# 这个 run 是带 pattern_review: true 初始化的，所以照样出 4 条候选。
# 旧行为是回落到默认配置 → 同一个 run 里 chunk.py 按新预算切、verify_span.py 按旧上限截，
# 两边都不报错，只是结果对不上。
PD2=$(python3 "$S/scan_patterns.py" scan --run-dir "$RUN3")
check "缺省 --config 时按 run 的配置快照生效（不是默认配置）" \
      "$(echo "$PD2" | jget "['candidates']")" 4

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
echo "══ 12. Pass 4 裁定缺失时必须 fail-closed ══"

# RUN2 是 logic-injection，已有全部冲突候选。逐一验四条路径。
CVF="$RUN2/work/conflicts-verified.jsonl"
cp "$CVF" "$WORK/cv.bak" 2>/dev/null || true
setverdict() { python3 - "$RUN2" "$1" <<'PYEOF'
import json,glob,sys,pathlib
run,v=sys.argv[1],sys.argv[2]
rows=[] if v=="none" else [
  {"conflict_id":c["conflict_id"],"verdict":v,"note":"回归"}
  for f in glob.glob(f"{run}/work/conflicts/conflicts-candidate.*.json")
  for c in json.load(open(f,encoding="utf-8"))["candidates"]]
pathlib.Path(run,"work","conflicts-verified.jsonl").write_text(
  "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
PYEOF
}
plancount() { python3 "$S/apply_comments.py" plan --run-dir "$RUN2" | jget "['comments']"; }

setverdict CONFLICT; BASE=$(plancount)
ge "全部裁定 CONFLICT 时有批注（基准）" "$BASE" 5

# 这条是本节的核心：Pass 4 没跑过，一条批注都不许进文档。
# 原实现三处都写 `if v and v.get("verdict")=="NOT_CONFLICT"`，v 为 None 时条件不成立，
# 于是「Pass 4 完全跳过」与「全部裁定为 CONFLICT」产出一模一样。
setverdict none
check "Pass 4 未裁定 → 零批注（不得 fail-open）" "$(plancount)" 0
setverdict NOT_CONFLICT
check "全部裁定 NOT_CONFLICT → 零批注" "$(plancount)" 0

# UNSURE 按 NOT_CONFLICT 处理，唯一例外是 Critical 级（prompts/pass4-adjudicate.md 判定表）
setverdict UNSURE
NU=$(plancount)
python3 - "$RUN2" <<'PYEOF'
import json,sys
items=json.load(open(f"{sys.argv[1]}/work/commentlist.json",encoding="utf-8"))["comments"]
bad=[i for i in items if i["severity"]!="Critical"]
noflag=[i for i in items if "需人工确认" not in i["text"]]
for i in bad:    print("    非 Critical 的 UNSURE 混进来了：", i["text"][:30])
for i in noflag: print("    Critical 的 UNSURE 未标注待人工确认：", i["text"][:30])
sys.exit(1 if bad or noflag or not items else 0)
PYEOF
check "UNSURE 只保留 Critical 级且标注待人工确认" "$?" 0
[ "$NU" -lt "$BASE" ] && ok "UNSURE 保留数少于全 CONFLICT（$NU < $BASE）" \
                      || bad "UNSURE 未被收窄（$NU vs $BASE）"

# 未准入的候选不得从报告里消失——静默隐藏和 fail-open 一样糟
setverdict none
python3 "$S/report.py" --run-dir "$RUN2" >/dev/null
python3 - "$RUN2" <<'PYEOF'
import glob,sys
md=open(glob.glob(f"{sys.argv[1]}/output/*.report.md")[0],encoding="utf-8").read()
sys.exit(0 if "未写入文档的冲突候选" in md else 1)
PYEOF
check "未裁定的候选仍在报告中列出（不静默隐藏）" "$?" 0

cp "$WORK/cv.bak" "$CVF" 2>/dev/null || true

echo
echo "══ 13. N7 压制必须有比例：fallback 术语不得吞掉整份审查 ══"

# fallback 层的意义是「别改它」。若判成「跨度里出现该术语就压制」，
# 用户表里只要有「系统」「平台」这类两字词，含这两个字的发现就全被静默吞掉——
# 压制方向的 fail-open 比放行方向更危险：它表现为"什么都没查出来"。
python3 - "$S" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import filter_neverflag as nf
glo={"entries":[{"key":"系统","preferred":"系统","source":"fallback","enforce":"off"},
                {"key":"平台","preferred":"平台","source":"fallback","enforce":"off"},
                {"key":"数据 owner","preferred":"数据 owner","source":"fallback","enforce":"off"}]}
g,fb=nf._alias_groups(glo),nf._fallback_terms(glo)
def hit(cat,text,sugg):
    return nf.check({"original_text":text,"category":cat,"suggested_text":sugg},
                    {"text":text},{"skip":{}},glo,g,fb)
bad=[]
# 术语只是出现在跨度里、建议并未改动它 → 必须保留
for cat,t,s2 in [("A1","通过帐号登录系统","通过账号登录系统"),
                 ("A2","认真的完成了平台建设","认真地完成了平台建设"),
                 ("A5","改善了平台的响应速度问题","改善了平台的响应速度")]:
    if hit(cat,t,s2): bad.append(f"{cat} 「{t}」被误压制")
# B 类不改任何字，「别改它」对它不适用 → 必须保留
if hit("B1","它的接口规范尚未确定，涉及系统与平台",""):
    bad.append("B 类被 fallback 术语压制")
# 建议确实动了 fallback 术语 → 必须压制（这是 N7 本来的用途）
if hit("A5","数据 owner 审批","数据责任人审批") != "N7":
    bad.append("确实改动 fallback 术语时未被 N7 压制")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "N7 只在建议动了 fallback 术语时压制" "$?" 0

echo
echo "══ 14. 保留范围 ≠ 复核范围；跨度上限与动作按类别分离 ══"

# 闸门④ 是"要不要复核"，不是"要不要留下"。两者共用一张表时，不复核的类别
# （C、P）会在 issues-verified.jsonl 这个唯一漏斗处整组消失——报告里也看不见。
# 这里用第 9 节已经跑完两条支线的 RUN3（含 issues-0001.patterns.jsonl）。
python3 "$S/verify_pass2.py" build --run-dir "$RUN3" --config "$WORK/tp/cfg.yaml" >/dev/null
python3 - "$RUN3" <<'PYEOF'
import json,sys
run=sys.argv[1]
pay=json.load(open(f"{run}/work/verify/pass2-primary.json",encoding="utf-8"))
ids={i["id"] for b in pay["batches"] for i in b["items"]}
key=json.load(open(f"{run}/work/verify/pass2-primary.key.json",encoding="utf-8"))["key"]
# P 类必须不在复核集里（它的裁定本就是封闭题，Pass 2 没有规则包）
assert not any(v["category"]=="P1" for v in key.values()), "P 类被送进了闸门④"
with open(f"{run}/work/verify/pass2-primary.verdicts.jsonl","w",encoding="utf-8") as f:
    for i in sorted(ids):
        f.write(json.dumps({"id":i,"answer":"A"},ensure_ascii=False)+"\n")
PYEOF
python3 "$S/verify_pass2.py" merge --run-dir "$RUN3" --config "$WORK/tp/cfg.yaml" >/dev/null
python3 - "$RUN3" <<'PYEOF'
import json,sys
rows=[json.loads(l) for l in open(f"{sys.argv[1]}/work/issues-verified.jsonl",encoding="utf-8")]
p=[r for r in rows if r["category"]=="P1"]
assert p, "P 类过完两道闸门后在 Pass 2 合并处整组消失（issues-verified 里一条都没有）"
assert all(r["verify"]["method"]=="not_reviewed" for r in p), "P 类不该进复核集"
assert all(r["verify"]["result"]=="pass" for r in p), "不复核被当成了淘汰"
assert any(r["category"].startswith("A") for r in rows), "主通道/错别字通道条目丢失"
PYEOF
check "P 类过闸门后仍留在 issues-verified（不复核 ≠ 丢弃）" "$?" 0

# 负向对照：把 P 类从保留范围里拿掉，上面那条断言必须失败
python3 - "$S" "$RUN3" <<'PYEOF'
import json,sys,pathlib
sys.path.insert(0, sys.argv[1])
import verify_pass2 as v
from pathlib import Path
v.KEEP_SCOPE = {k: (s - v.P_CLASSES) for k, s in v.KEEP_SCOPE.items()}
rows=v.collect(Path(sys.argv[2]), {"strictness":"balanced"})
sys.exit(0 if not any(r["category"]=="P1" for r in rows) else 1)
PYEOF
check "负向对照：P 类不在保留范围时确实会消失（说明这条断言有效）" "$?" 0

# C 类：thorough 下只进报告，且动作恒为 report_only（永不入文档）
python3 - "$S" "$RUN3" <<'PYEOF'
import json,sys,pathlib
sys.path.insert(0, sys.argv[1])
import verify_span as vs, workspace as ws
from pathlib import Path
run=Path(sys.argv[2]); raw=run/"work"/"issues"/"issues-c.raw.jsonl"
para=next(json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")
          if "阀值" in json.loads(l)["text"])
raw.write_text(json.dumps({"pid":para["pid"],"category":"C1",
    "original_text":para["text"][:40],"suggested_text":"改写得更简洁",
    "evidence":"冗长","severity":"Low"},ensure_ascii=False)+"\n",encoding="utf-8")
out=run/"work"/"issues"/"issues-c.jsonl"
vs.process(run,"0001",raw,ws.load_config(None),out,20)
rows=[json.loads(l) for l in open(out,encoding="utf-8")]
assert rows, "C 类被闸门②丢弃了"
assert rows[0]["action"]=="report_only", f"C 类动作被升成了 {rows[0]['action']}（会写进文档）"
assert not rows[0]["suggested_text"], "C 类带了建议文本"
cfg={"strictness":"thorough"}
import verify_pass2 as v
assert "C1" in v._scope(cfg, v.KEEP_SCOPE), "thorough 下 C 类不在保留范围内（等于没有 thorough）"
assert "C1" not in v._scope(cfg, v.REVIEW_SCOPE), "C 类被送进了闸门④"
assert "C1" not in v._scope({"strictness":"balanced"}, v.KEEP_SCOPE), "balanced 下不该保留 C 类"
PYEOF
check "C 类：动作恒为 report_only，仅 thorough 保留且不复核" "$?" 0

# 上游声明的 report_only 不得被闸门②升回 comment
python3 - "$S" "$RUN3" <<'PYEOF'
import json,sys
sys.path.insert(0, sys.argv[1])
import verify_span as vs, workspace as ws
from pathlib import Path
run=Path(sys.argv[2]); raw=run/"work"/"issues"/"issues-ro.raw.jsonl"
para=next(json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")
          if "风险2" in json.loads(l)["text"])
raw.write_text(json.dumps({"pid":para["pid"],"category":"P1","rule_id":"P-RISK-01",
    "pattern_name":"风险条目描述范式","original_text":para["text"],"suggested_text":"",
    "evidence":"缺少：责任人","severity":"Medium","action":"report_only"},
    ensure_ascii=False)+"\n",encoding="utf-8")
out=run/"work"/"issues"/"issues-ro.jsonl"
vs.process(run,"0001",raw,ws.load_config(None),out,20)
r=json.loads(open(out,encoding="utf-8").readline())
assert r["action"]=="report_only", \
    f"只缺可选要件的降级被闸门②覆盖成了 {r['action']}（规则包的 action 形同虚设）"
PYEOF
check "上游的 report_only 不被闸门②覆盖（规则包 action 生效）" "$?" 0

# P 类的跨度就是整段：不得被 A/B 类的 max_span_chars 误杀，而 A 类同样长度必须被丢
python3 - "$S" "$RUN3" <<'PYEOF'
import json,sys
sys.path.insert(0, sys.argv[1])
import verify_span as vs, workspace as ws
from pathlib import Path
run=Path(sys.argv[2]); cfg=ws.load_config(None)
long=("风险3：第三方结算接口在跨境场景下的稳定性存在不确定性，历史上曾出现连续三次超时导致对账中断，"
      "需在本期实施过程中持续跟踪并评估其对整体进度的影响，同时协调供应商完成链路优化与容量评估，"
      "并在联调阶段安排专项压测以验证改造效果，相关结论将在月度例会上同步给项目组与业务方。")
assert len(long) > int(cfg["verification"]["max_span_chars"]), "构造的段落没有超过 A/B 类上限"
# 造一个含该长段落的片
pj=run/"work"/"paragraphs.jsonl"
rows=[json.loads(l) for l in open(pj,encoding="utf-8")]
extra=dict(rows[0]); extra.update({"pid":"p-099999","text":long,"char_len":len(long),
                                   "is_heading":False,"is_code":False,"in_table":False})
with open(pj,"a",encoding="utf-8") as f: f.write(json.dumps(extra,ensure_ascii=False)+"\n")
cdir=run/"work"/"chunks"; ctext=cdir/"chunk-9999.txt"
ctext.write_text(f"[p-099999] {long}\n",encoding="utf-8")
idx=json.load(open(cdir/"index.json",encoding="utf-8"))
idx["chunks"].append({"chunk_id":"9999","pids":["p-099999"],"review_pids":["p-099999"],
                      "context_pids":[],"chunk_type":"text"})
json.dump(idx,open(cdir/"index.json","w",encoding="utf-8"),ensure_ascii=False)

def gate(cat, action=""):
    raw=run/"work"/"issues"/f"issues-len-{cat}.raw.jsonl"
    rec={"pid":"p-099999","category":cat,"rule_id":cat,"original_text":long,
         "suggested_text":"","evidence":"e","severity":"Medium"}
    if cat=="P1": rec["pattern_name"]="风险条目描述范式"
    raw.write_text(json.dumps(rec,ensure_ascii=False)+"\n",encoding="utf-8")
    out=run/"work"/"issues"/f"issues-len-{cat}.jsonl"
    return vs.process(run,"9999",raw,cfg,out,20)
p=gate("P1"); b=gate("B1")
assert p["length_drop"]==0 and p["kept"]==1, f"整段 P 类被跨度上限误杀（{p['length_drop']}）"
assert b["length_drop"]==1 and b["kept"]==0, "负向对照：同样长度的 B 类本应被丢弃"
PYEOF
check "P 类整段跨度不被 max_span_chars 误杀（B 类同长度仍被丢：负向对照）" "$?" 0

echo
echo "══ 15. 主流程的租约：没取租约与被接管必须可区分 ══"

# SKILL.md 第 0 步要求 init 之后立刻 lease acquire。**这一步早先缺在文档里**：
# 照主流程跑，Pass 3 会以 exit 9 终止，而那条错误信息说的是"已被另一个会话接管"
# ——与实情正好相反，会把一次正常运行误报成并发冲突。
INIT5=$(python3 "$S/workspace.py" init --source "$F/sample-basic.docx" \
        --output-dir "$DELIVER" --temp-dir "$WORK/t15")
RUN5=$(echo "$INIT5" | jget "['run_dir']")
DOC5=$(echo "$INIT5" | jget "['doc_dir']")
check "init 返回 lease 现状（SKILL.md 第 0 步据此判断要不要问用户）" \
      "$(echo "$INIT5" | jget "['lease']['held']")" "False"
check "init 给出取租约的下一步命令" \
      "$(echo "$INIT5" | python3 -c "import json,sys;print('lease acquire' in json.load(sys.stdin)['next'])")" "True"

# 现状：没取租约 → exit 9，且文案与"被接管"完全一样
python3 "$S/detect_conflicts.py" --run-dir "$RUN5" --session s-flow --generation 1 >/dev/null 2>&1
check "未取租约时 Pass 3 以 exit 9 终止" "$?" 9
check "此时 held=false，可据此与真正的被接管区分开" \
      "$(python3 "$S/workspace.py" lease status --doc-dir "$DOC5" | jget "['held']")" "False"

# 按 SKILL.md 补取租约后，同一条命令必须通过；generation 取自 acquire 的返回
OWN5=$(python3 "$S/workspace.py" lease acquire --doc-dir "$DOC5" --session s-flow \
       --runid "$(basename "$RUN5")" --stage pass3 | jget "['owner']['generation']")
python3 "$S/detect_conflicts.py" --run-dir "$RUN5" --session s-flow --generation "$OWN5" >/dev/null 2>&1
check "取租约后同一条命令通过（generation 来自 acquire 返回）" "$?" 0
# 负向对照：纪元不匹配仍必须是 exit 9（令牌栅栏没有被这次改动放松）
python3 "$S/detect_conflicts.py" --run-dir "$RUN5" --session s-flow \
        --generation "$((OWN5 + 1))" >/dev/null 2>&1
check "负向对照：纪元不匹配仍以 exit 9 拒绝" "$?" 9
python3 "$S/workspace.py" lease release --doc-dir "$DOC5" --session s-flow --generation "$OWN5" >/dev/null

echo
echo "══ 16. 闸门③ 的位置类规则判的是区域，不是整段 ══"

# N9/N10/N12 讲的都是"这个区域内的内容不审查"。早先实现成"段落里出现该特征就
# 整段免检"——技术文档里一句话带一条 URL、一个 cpu_usage=80%、一句"按照本办法
# 第五条执行"都极常见，按整段判等于把大半个审查静默关掉，且输出里没有任何痕迹。
python3 "$S/filter_neverflag.py" --traps "$F/neverflag-traps.json" >/dev/null 2>&1
check "不改清单负向语料全部符合预期（该压的压住、不该压的没压）" "$?" 0
TR=$(python3 "$S/filter_neverflag.py" --traps "$F/neverflag-traps.json")
ge "负向语料条数（正向 + 反向）" "$(echo "$TR" | jget "['cases']")" 20

# 负向对照：把跨度定位关掉 → 退回整段语义，这组语料必须失败
python3 - "$S" "$F/neverflag-traps.json" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import filter_neverflag as nf
nf._span_range = lambda ptext, span: None      # 定位不到即退回旧的整段语义
res = nf.traps(Path(sys.argv[2]), nf.load_run_config(None))
for p in res["problems"][:3]:
    print("   ", p)
sys.exit(0 if (not res["ok"] and len(res["problems"]) >= 5) else 1)
PYEOF
check "负向对照：按整段免检时语料必然失败（说明这组语料咬得住）" "$?" 0

# 逐条落到具体现场：同一段里，落在豁免区域外的语病必须活下来
python3 - "$S" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import filter_neverflag as nf
cfg = nf.load_run_config(None)
def hit(para, span, cat="A2"):
    return nf.check({"original_text": span, "category": cat, "suggested_text": ""},
                    {"text": para}, cfg, {}, [], set())
bad = []
# 跨度落在区域外 → 保留；落在区域内 → 压制。同一个段落，两种结果。
para = "详见 https://example.internal/docs 的说明，该文档认真的记录了接口约定。"
if hit(para, "认真的记录了接口约定"):        bad.append("URL 段落里的正文语病被误压制")
if hit(para, "https://example.internal/docs") != "N9": bad.append("跨度就是 URL 时未被 N9 压制")
para = "《网络安全法》第二十一条规定：国家实行网络安全等级保护制度。"
if hit(para, "国家实行网络安全等级保护制度", "A4") != "N10": bad.append("引文本体未被 N10 保护")
if hit(para, "《网络安全法》第二十一条", "A3") != "N10":       bad.append("条号未被 N10 保护")
para = "表 3 中列出的各项指标改善了系统的响应速度问题，需在下一轮复核。"
if hit(para, "改善了系统的响应速度问题", "A5"): bad.append("以「表 3」开头的正文句被 N12 误压制")
if not hit("图 3-7 系统总体架构图", "系统总体架构图", "A4"): bad.append("真正的图题未被 N12 压制")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "同段之内：豁免区域外保留、区域内压制" "$?" 0

echo
echo "══ 17. 定位歧义：跨度在段内不唯一时不得落笔 ══"

# locate_span 取的是首个匹配。「本期指标目标为 200ms，实测值为 1200ms。」里
# 把「200ms」改掉，改中的是目标值还是实测值，取决于 find 而不是取决于判定。
INIT7=$(python3 "$S/workspace.py" init --source "$F/logic-injection.docx" \
        --output-dir "$DELIVER" --temp-dir "$WORK/t17")
RUN7=$(echo "$INIT7" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN7" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN7" >/dev/null

python3 - "$RUN7" <<'PYEOF'
import json,sys,pathlib
run=pathlib.Path(sys.argv[1])
paras=[json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")]
def find(sub): return next(p for p in paras if sub in p["text"])
amb  = find("实测值为")            # 「200ms」出现两次（1200ms 里也含 200ms）
uniq = amb                          # 同一段里另取一个唯一跨度
term = find("QPS 提升至")           # 「QPS」出现两次，术语规范化要全换
assert amb["text"].count("200ms")==2, amb["text"]
assert term["text"].count("QPS")==2, term["text"]
patches=[
  {"patch_id":"AMB","pid":amb["pid"],"category":"A8",
   "original_text":"200ms","suggested_text":"200 ms","source":"issue"},
  {"patch_id":"UNI","pid":uniq["pid"],"category":"A8",
   "original_text":"实测值为 1200ms","suggested_text":"实测值为 1100ms","source":"issue"},
  {"patch_id":"ALL","pid":term["pid"],"category":"L26",
   "original_text":"QPS","suggested_text":"qps","source":"conflict",
   "all_occurrences":True},
]
(run/"work"/"patchlist.json").write_text(
    json.dumps({"patches":patches,"demoted_to_comment":[]},ensure_ascii=False),encoding="utf-8")
# 未落笔的条目必须照常出普通批注 → 需要它在 issues-verified 里有对应记录
rows=[{"id":"AMB","chunk_id":"0001","pid":amb["pid"],"category":"A8","rule_id":"A8",
       "severity":"High","original_text":"200ms","suggested_text":"200 ms",
       "evidence":"单位写法","action":"revision","verify":{"result":"pass"}}]
(run/"work"/"issues-verified.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
(run/"work"/"conflicts").mkdir(parents=True,exist_ok=True)
PYEOF

AR=$(python3 "$S/apply_revisions.py" apply --run-dir "$RUN7")
check "歧义跨度不落笔，唯一跨度与整段替换照常落笔" "$(echo "$AR" | jget "['applied']")" 2
check "歧义跨度记为失败" "$(echo "$AR" | jget "['failed']")" 1
python3 - "$RUN7" <<'PYEOF'
import json,sys
prov=json.load(open(f"{sys.argv[1]}/work/revision-provenance.json",encoding="utf-8"))
assert set(prov["applied"])=={"UNI","ALL"}, prov["applied"]
f=prov["failed"][0]
assert f["patch_id"]=="AMB" and "出现 2 次" in f["reason"], f
# 术语规范化必须把该段里的两处都换掉——只换首处会让文档半规范化
allp=next(p for p in prov["provenance"] if p["patch_id"]=="ALL")
assert allp["occurrences"]==2, f"整段替换只落了 {allp['occurrences']} 处"
uni=next(p for p in prov["provenance"] if p["patch_id"]=="UNI")
assert uni["occurrences"]==1 and uni["consistent"], uni
PYEOF
check "失败原因指明出现次数；整段替换覆盖全部两处" "$?" 0

# 改中的必须是「实测值」那一处，不是目标值那一处
python3 - "$S" "$RUN7" <<'PYEOF'
import sys
from lxml import etree
sys.path.insert(0, sys.argv[1])
import ooxml as ox
root=etree.parse(f"{sys.argv[2]}/work/unpacked/word/document.xml").getroot()
acc, rej = ox.text_view(root, accept=True), ox.text_view(root, accept=False)
assert "目标为 200ms" in acc, "目标值被误改（改中了首个匹配）"
assert "实测值为 1100ms" in acc, "唯一跨度未按预期落笔"
assert "由 1000 qps 提升至 1500 qps" in acc, f"术语未全部替换"
assert "由 1000 QPS 提升至 1500 QPS" in rej, "拒绝修订视图与原文不一致"
PYEOF
check "接受视图改的是实测值那一处，拒绝视图逐字还原" "$?" 0

python3 "$S/apply_comments.py" plan --run-dir "$RUN7" >/dev/null
python3 - "$RUN7" <<'PYEOF'
import json,sys
cl=json.load(open(f"{sys.argv[1]}/work/commentlist.json",encoding="utf-8"))["comments"]
amb=[c for c in cl if c.get("ref")=="AMB"]
assert amb, "计划了修订但未落笔的条目静默消失了（既无修订也无批注）"
assert amb[0].get("kind")!="revision", "未落笔的条目不该出修订理由批注"
PYEOF
check "未落笔的条目照常出普通批注，不静默丢失" "$?" 0

python3 "$S/apply_comments.py" apply --run-dir "$RUN7" >/dev/null
python3 - "$S" "$RUN7" <<'PYEOF'
import json,sys
from lxml import etree
sys.path.insert(0, sys.argv[1])
import ooxml as ox
run=sys.argv[2]
root=etree.parse(f"{run}/work/unpacked/word/document.xml").getroot()
cover=ox.comment_coverage(root)
cl=json.load(open(f"{run}/work/commentlist.json",encoding="utf-8"))["comments"]
amb=next(c for c in cl if c.get("ref")=="AMB")
got=cover[str(amb["comment_id"])]["reject"]
# 锚点「200ms」在段内出现两次 → 不猜是哪一处，退回整段。
# 圈错一处比圈住整段更糟：评审人会照着高亮去找问题，而问题不在那里。
assert got.count("200ms")==2, f"歧义锚点被圈到了其中一处：{got!r}"
PYEOF
check "歧义锚点退回整段，而不是圈住首个匹配" "$?" 0

python3 "$S/validate_docx.py" --run-dir "$RUN7" >/dev/null 2>&1
check "多处替换 + 歧义退让之后，四项校验仍全过（D9 未被放松）" "$?" 0

# 负向对照：关掉唯一性守卫，歧义跨度会落到首个匹配上
python3 - "$S" "$F" "$WORK" <<'PYEOF'
import json,subprocess,sys,shutil,pathlib
S,F,W=sys.argv[1],sys.argv[2],sys.argv[3]
out=subprocess.run([sys.executable,f"{S}/workspace.py","init","--source",f"{F}/logic-injection.docx",
                    "--output-dir",f"{W}/d17b","--temp-dir",f"{W}/t17b"],capture_output=True,text=True)
run=pathlib.Path(json.loads(out.stdout)["run_dir"])
for cmd in (["unpack.py","run"],["extract.py"]):
    subprocess.run([sys.executable,f"{S}/{cmd[0]}"]+cmd[1:]+["--run-dir",str(run)],capture_output=True)
paras=[json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")]
amb=next(p for p in paras if "实测值为" in p["text"])
(run/"work"/"patchlist.json").write_text(json.dumps({"patches":[
    {"patch_id":"AMB","pid":amb["pid"],"category":"A8","original_text":"200ms",
     "suggested_text":"200 ms","source":"issue","all_occurrences":False}],
    "demoted_to_comment":[]},ensure_ascii=False),encoding="utf-8")
# 打桩：让 span_count 恒为 1，等于关掉守卫
sys.path.insert(0,S)
import ooxml as ox, apply_revisions as ar, workspace as ws
ox.span_count=lambda para,needle: 1
ar.apply_plan(run, ws.load_config(None))
from lxml import etree
root=etree.parse(str(run/"work"/"unpacked"/"word"/"document.xml")).getroot()
acc=ox.text_view(root,accept=True)
# 守卫关掉后，改中的是目标值那一处——这正是守卫要拦的现场
sys.exit(0 if "目标为 200 ms" in acc else 1)
PYEOF
check "负向对照：关掉守卫即改中目标值那一处（说明守卫拦的是真现场）" "$?" 0

# typo 通道：候选窗口自动撑到唯一，否则产出的候选注定落不了笔
python3 - "$S" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import typo_scan as ts
# 整句重复：±6 的固定窗口在这里必然不唯一，必须一路撑到能区分两处为止
t="系统布署完成后需要复核。系统布署完成后需要复核，并记录结论。"
s=t.index("布署")
lo,hi=ts._unique_window(t, s, s+2)
assert t.count(t[lo:hi])==1, f"窗口仍不唯一，这条候选注定落不了笔：{t[lo:hi]!r}"
assert "布署" in t[lo:hi]
# 第二处也要能各自撑到唯一，且两处窗口不同（否则 merge 会把它们合成一条）
s2=t.index("布署", s+1)
lo2,hi2=ts._unique_window(t, s2, s2+2)
assert t.count(t[lo2:hi2])==1 and (lo,hi)!=(lo2,hi2), "两处候选的窗口没有区分开"
# 窗口里出现两次错词时按位置替换，不得全局 replace
w, at = "布署与布署", 0
assert w[:at]+"部署"+w[at+2:] == "部署与布署", "按位置替换写错了"
assert w.replace("布署","部署") == "部署与部署", "全局 replace 会顺手改掉另一处"
PYEOF
check "错别字候选窗口自动撑到段内唯一" "$?" 0

echo
echo "══ 18. 送审文档已带批注：不得覆盖，不得撞号 ══"

# 评审场景里文档常常已经有别人的批注。原实现另起一份 comments.xml 整份写入，
# 把既有批注连人带话抹掉；编号又从 1 开始，与正文里既有的 commentRangeStart
# 撞号，旧锚点转而指向新批注。**四项校验对此曾经全绿**。
INIT8=$(python3 "$S/workspace.py" init --source "$F/existing-comments.docx" \
        --output-dir "$DELIVER" --temp-dir "$WORK/t18")
RUN8=$(echo "$INIT8" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN8" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN8" >/dev/null
check "unpack 存下了既有批注的现场快照" \
      "$([ -f "$RUN8/work/comments.baseline.xml" ] && echo yes || echo no)" yes

python3 - "$RUN8" <<'PYEOF'
import json,pathlib,sys
run=pathlib.Path(sys.argv[1])
paras=[json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")]
p=next(x for x in paras if len(x["text"]) > 12 and not x["is_heading"])
(run/"work"/"issues-verified.jsonl").write_text(json.dumps({
  "id":"I1","chunk_id":"0001","pid":p["pid"],"category":"B1","rule_id":"B1",
  "severity":"Medium","original_text":p["text"][:16],"suggested_text":"",
  "evidence":"指代不明","action":"comment","verify":{"result":"pass"}},
  ensure_ascii=False)+"\n",encoding="utf-8")
(run/"work"/"conflicts").mkdir(parents=True,exist_ok=True)
PYEOF
python3 "$S/apply_comments.py" plan --run-dir "$RUN8" >/dev/null
AC=$(python3 "$S/apply_comments.py" apply --run-dir "$RUN8")
check "既有批注被保留" "$(echo "$AC" | jget "['existing_comments_kept']")" 1

python3 - "$RUN8" <<'PYEOF'
import json,sys
from lxml import etree
W="{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
run=sys.argv[1]
c=etree.parse(f"{run}/work/unpacked/word/comments.xml").getroot()
by={x.get(W+"id"):(x.get(W+"author"),"".join(t.text or "" for t in x.iter(W+"t")))
    for x in c.iter(W+"comment")}
assert "1" in by, "源文档原有的批注被整份覆盖掉了"
assert by["1"]==("张三","这一段请业务方再确认一次。"), f"原有批注被改写：{by['1']}"
assert len(by)==2, f"新批注没写进去或与既有批注合并了：{list(by)}"
new=[k for k in by if k!="1"]
assert new and int(new[0])>1, f"新批注编号与既有批注撞号：{new}"
# 正文里每个 id 的 commentRangeStart 必须只有一个
d=etree.parse(f"{run}/work/unpacked/word/document.xml").getroot()
ids=[e.get(W+"id") for e in d.iter(W+"commentRangeStart")]
assert len(ids)==len(set(ids)), f"commentRangeStart 撞号：{ids}"
PYEOF
check "原批注逐字保留、新批注另行编号、锚点不撞号" "$?" 0

python3 "$S/validate_docx.py" --run-dir "$RUN8" >/dev/null 2>&1
check "四项校验通过" "$?" 0

# 负向对照：退回"另起一份 comments.xml"，第四项校验必须以退出码 8 抓住
python3 - "$RUN8" <<'PYEOF'
import sys
from lxml import etree
W="{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
p=f"{sys.argv[1]}/work/unpacked/word/comments.xml"
root=etree.parse(p).getroot()
for c in list(root.iter(W+"comment")):
    if c.get(W+"id")=="1":
        root.remove(c)          # 模拟"整份覆盖"：既有批注消失
etree.ElementTree(root).write(p,xml_declaration=True,encoding="UTF-8",standalone=True)
PYEOF
python3 "$S/validate_docx.py" --run-dir "$RUN8" >/dev/null 2>&1
check "负向对照：既有批注消失时校验必须失败" "$?" 8

echo
echo "══ 19. 计数口径：报给用户的数字必须与文档里的一致 ══"

# 截断的去重键只用 (pid, category) 会把同段同类的**不同**问题折叠成一条，
# 回填时被当成"已选过"跳过——配额没用满，真问题却被丢掉。
python3 - "$S" <<'PYEOF'
import json,sys,tempfile,pathlib
sys.path.insert(0, sys.argv[1])
import verify_span as vs, workspace as ws
run=pathlib.Path(tempfile.mkdtemp())
(run/"work"/"chunks").mkdir(parents=True); (run/"work"/"issues").mkdir(parents=True)
text="甲方认真的完成了验收。乙的接口尚未定。丙的规范尚未定。丁的口径尚未定。戊的边界尚未定。己的范围尚未定。"
para={"pid":"p-000001","index":1,"heading_path":[],"level":0,"is_heading":False,"style":"Normal",
      "style_name":"Normal","text":text,"char_len":len(text),"in_table":False,"table_id":None,
      "row_idx":None,"cell_idx":None,"is_list":False,"is_code":False,"is_quote":False,
      "page_hint":1,"page_estimated":True}
(run/"work"/"paragraphs.jsonl").write_text(json.dumps(para,ensure_ascii=False)+"\n",encoding="utf-8")
(run/"work"/"chunks"/"chunk-0001.txt").write_text(f"[{para['pid']}] {text}\n",encoding="utf-8")
json.dump({"chunks":[{"chunk_id":"0001","pids":["p-000001"],"review_pids":["p-000001"],
                      "context_pids":[]}]},
          open(run/"work"/"chunks"/"index.json","w",encoding="utf-8"),ensure_ascii=False)
rows=[{"pid":"p-000001","category":"A2","original_text":"认真的完成了验收",
       "suggested_text":"认真地完成了验收","evidence":"e","severity":"High"}]
rows+=[{"pid":"p-000001","category":"B1","original_text":o,"suggested_text":"",
        "evidence":"e","severity":"Medium"}
       for o in ["乙的接口尚未定","丙的规范尚未定","丁的口径尚未定","戊的边界尚未定","己的范围尚未定"]]
raw=run/"work"/"issues"/"r.jsonl"
raw.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
out=run/"work"/"issues"/"o.jsonl"
vs.process(run,"0001",raw,ws.load_config(None),out,3)
kept=[json.loads(l) for l in open(out,encoding="utf-8")]
assert len(kept)==3, f"cap=3 只保留了 {len(kept)} 条，配额没用满"
assert len({(r['category'],r['original_text']) for r in kept})==3, "保留的条目重复了"
PYEOF
check "截断按 (pid,类别,原文) 去重，配额用得满" "$?" 0

# 未准入交付物的冲突候选不得记成「批注」——Agent 正是照着这个数字口头汇报的
python3 - "$S" "$RUN2" <<'PYEOF'
import json,pathlib,shutil,sys
sys.path.insert(0, sys.argv[1])
import report as rp, workspace as ws
run=pathlib.Path(sys.argv[2])
cv=run/"work"/"conflicts-verified.jsonl"
bak=cv.read_text(encoding="utf-8") if cv.exists() else None
cv.write_text("", encoding="utf-8")            # Pass 4 全部未裁定 → 一条都不进文档
rows=rp.load_rows(run, ws.load_config(None))
held=[r for r in rows if r.get("kind")=="conflict" and r.get("admitted") is False]
assert held, "构造失败：没有未准入的候选"
bad=[r for r in held if r["action"]!="report_only"]
if bak is not None: cv.write_text(bak, encoding="utf-8")
assert not bad, f"{len(bad)} 条未写进文档的候选被记成了「{bad[0]['action']}」"
PYEOF
check "未准入的冲突候选记为 report_only（不虚报批注数）" "$?" 0

echo
echo "══ 20. 分片预算：虚高的 token 系数与重切片后的陈旧产物 ══"

# 造一份与真实压测同形的正文（约 6 万字、H2 密集），直接写 paragraphs.jsonl——
# chunk.py 只吃它，不需要真做一份 800 页的 docx。
mkdir -p "$WORK/big"
cp "$F/sample-basic.docx" "$WORK/big/big.docx"
RUN5=$(python3 "$S/workspace.py" init --source "$WORK/big/big.docx" \
       --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN5" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN5" >/dev/null
python3 - "$RUN5" <<'PYEOF'
import json,pathlib,random,sys
run=pathlib.Path(sys.argv[1]); random.seed(7)
body="本系统在设计上采用分层架构，各层之间通过标准接口交互，确保模块可替换与可测试。"
rows=[]; i=0; chars=0; sec=0
while chars < 60000:
    i+=1
    if i%14==1:
        sec+=1; t=f"{sec} 第 {sec} 章 模块设计与接口约定"
        rows.append({"pid":f"p-{i:06d}","index":i,"heading_path":[t],"level":2,
                     "is_heading":True,"style":"Heading2","style_name":"heading 2","text":t,
                     "char_len":len(t),"in_table":False,"table_id":None,"row_idx":None,
                     "cell_idx":None,"is_list":False,"is_code":False,"is_quote":False,
                     "page_hint":1+chars//265,"page_estimated":True})
        chars+=len(t); continue
    t=(body*7)[:random.randint(120,320)]
    rows.append({"pid":f"p-{i:06d}","index":i,"heading_path":[f"{sec} 第 {sec} 章 模块设计与接口约定"],
                 "level":None,"is_heading":False,"style":"Normal","style_name":"Normal","text":t,
                 "char_len":len(t),"in_table":False,"table_id":None,"row_idx":None,"cell_idx":None,
                 "is_list":False,"is_code":False,"is_quote":False,"page_hint":1+chars//265,
                 "page_estimated":True})
    chars+=len(t)
(run/"work"/"paragraphs.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
PYEOF

# 旧参数：系数 1.6（一个汉字算 1.6 token）+ 无条件扣掉术语摘要预算 + 切点比例 0.5
cat > "$WORK/big/legacy.yaml" <<'YAML'
chunking:
  max_context_tokens: 45000
  max_text_tokens: 15000
  token_fallback_ratio: 1.6
  glossary_tokens: 4000
  output_reserve_tokens: 12000
  split_point_fill_ratio: 0.5
YAML
OLDN=$(python3 "$S/chunk.py" --run-dir "$RUN5" --config "$WORK/big/legacy.yaml" | jget "['chunks']")
NEWN=$(python3 "$S/chunk.py" --run-dir "$RUN5" | jget "['chunks']")
# 片数就是 Pass 1 的调用数与工具轮次数。系数修正（1.6→1.0）与切点比例（0.5→0.7）
# 一起把它压下来；默认 max_text_tokens 后来又为了子 Agent 的窗口调小，
# 所以这里断言的是"明显更少"，不是某个固定倍数。
python3 -c "import sys;sys.exit(0 if $OLDN >= $NEWN*12//10 else 1)"
check "同一份正文：新预算的片数比旧参数少两成以上（$OLDN → $NEWN）" "$?" 0

# 没有术语表时不得从预算里扣术语摘要（term_rules 默认关闭，那 4000 是白扣的）
python3 - "$S" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import chunk as ck, workspace as ws
cfg=ws.load_config(None)
no_g, with_g = ck.overhead_tokens(cfg, False), ck.overhead_tokens(cfg, True)
gl=int(cfg["chunking"]["glossary_tokens"])
assert with_g - no_g == gl, f"术语摘要预算未随术语表存在与否变化：{no_g} / {with_g}"
assert no_g == cfg["chunking"]["instruction_tokens"] + cfg["chunking"]["output_reserve_tokens"]
PYEOF
check "无术语表时不扣术语摘要预算" "$?" 0

# 重新分片 = 旧产物作废。不清的话「文件存在性即状态」会让新的第 1 片被判为已完成，
# 而那部分正文再也不会被看到，输出里没有任何痕迹。
seed_products() { python3 - "$RUN5" <<'PYEOF'
import json,pathlib,sys
run=pathlib.Path(sys.argv[1])
(run/"work"/"issues").mkdir(parents=True,exist_ok=True)
(run/"work"/"facts").mkdir(parents=True,exist_ok=True)
(run/"work"/"typos").mkdir(parents=True,exist_ok=True)
(run/"work"/"issues"/"issues-0001.jsonl").write_text("",encoding="utf-8")
(run/"work"/"issues"/"issues-0001.raw.jsonl").write_text("",encoding="utf-8")
(run/"work"/"issues"/"issues-0001.typos.jsonl").write_text("",encoding="utf-8")
(run/"work"/"facts"/"facts-0001.json").write_text("{}",encoding="utf-8")
(run/"work"/"typos"/"typos-0001.json").write_text("{}",encoding="utf-8")
PYEOF
}
seed_products
# 同配置重跑必须是幂等的：一个产物都不许动（断点续跑的前提）
IDEM=$(python3 "$S/chunk.py" --run-dir "$RUN5" | jget "['stale_chunks_cleared']")
check "同配置重跑不清任何产物（幂等）" "$IDEM" 0
check "幂等重跑后产物仍在" \
  "$([ -f "$RUN5/work/facts/facts-0001.json" ] && echo yes || echo no)" yes

cat > "$WORK/big/small.yaml" <<'YAML'
chunking:
  max_text_tokens: 3000
YAML
CLR=$(python3 "$S/chunk.py" --run-dir "$RUN5" --config "$WORK/big/small.yaml" | jget "['stale_chunks_cleared']")
ge "换切法后作废的分片数" "$CLR" 1
check "陈旧的 facts 已作废" \
  "$([ -f "$RUN5/work/facts/facts-0001.json" ] && echo no || echo yes)" yes
check "陈旧的 issues（含两条支线）已作废" \
  "$(ls "$RUN5/work/issues" 2>/dev/null | grep -c 'issues-0001') " "0 "
DONE=$(python3 "$S/workspace.py" claim status --run-dir "$RUN5" --session s-clr | jget "['done']")
check "重切片后没有分片被误判为已完成" "$DONE" 0

# 负向对照：把清理关掉，同一个现场必须立刻退化成「静默跳片」
python3 - "$S" "$RUN5" "$WORK/big/small.yaml" <<'PYEOF'
import json,pathlib,sys
sys.path.insert(0, sys.argv[1])
import chunk as ck, workspace as ws
run=pathlib.Path(sys.argv[2])
for p,c in ((run/"work"/"issues"/"issues-0001.raw.jsonl",""),
            (run/"work"/"facts"/"facts-0001.json","{}")):
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(c,encoding="utf-8")
ck.clear_stale_products = lambda run_dir, stale: 0          # 关掉清理
ck.build(run, ws.load_config(sys.argv[3]))                  # 换成另一种切法
done = ws.chunk_done(run, "0001")
print("    关掉清理后 chunk-0001 被判为已完成" if done else "    未复现")
sys.exit(0 if done else 1)
PYEOF
check "负向对照：不清陈旧产物即静默跳片（说明这项断言咬得住）" "$?" 0

echo
echo "══ 21. 闸门④按批落盘：可并行，且并行不改变判定 ══"

# RUN4 在第 11 节已经 build 过并写了一份「单文件」裁定，先记下它的判定结果作为基准
python3 "$S/verify_pass2.py" build --run-dir "$RUN4" >/dev/null
BN=$(python3 "$S/verify_pass2.py" build --run-dir "$RUN4" | jget "['batches']")
V4="$RUN4/work/verify"
check "分批 payload 文件数与批数一致" "$(ls "$V4" | grep -c '^pass2-primary\.v[0-9]*\.json$')" "$BN"

# 分批文件同样不许泄题：批内不含侧别信息，且答案不在同批任何一条上下文里
python3 - "$RUN4" <<'PYEOF'
import glob,json,sys
bad=[]
for f in sorted(glob.glob(f"{sys.argv[1]}/work/verify/pass2-primary.v*.json")):
    raw=open(f,encoding="utf-8").read()
    for k in ("orig_side","suggested_text","category","severity","evidence","key"):
        if k in raw: bad.append(f"{f.split('/')[-1]} 含字段 {k}")
    d=json.load(open(f,encoding="utf-8"))
    whole=" ".join(x.get("context") or "" for x in d["items"])
    for x in d["items"]:
        if x["form"]=="ab" and (x["A"] in whole or x["B"] in whole):
            bad.append(f"{d['batch_id']} 的选项出现在同批上下文里")
for b in bad[:5]: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "分批 payload 不泄题" "$?" 0

python3 "$S/verify_pass2.py" merge --run-dir "$RUN4" >/dev/null
HS=$(sha256sum "$RUN4/work/issues-verified.jsonl" | cut -d' ' -f1)

# 把那份单文件裁定按批拆开（模拟 N 个子 Agent 各写各的），判定结果必须逐字相同
python3 - "$RUN4" <<'PYEOF'
import glob,json,pathlib,sys
V=pathlib.Path(sys.argv[1],"work","verify")
ans={json.loads(l)["id"]:json.loads(l)["answer"]
     for l in open(V/"pass2-primary.verdicts.jsonl",encoding="utf-8")}
for f in sorted(glob.glob(str(V/"pass2-primary.v*.json"))):
    d=json.load(open(f,encoding="utf-8"))
    rows=[{"id":x["id"],"answer":ans[x["id"]]} for x in d["items"] if x["id"] in ans]
    (V/f"pass2-primary.{d['batch_id']}.verdicts.jsonl").write_text(
        "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
(V/"pass2-primary.verdicts.jsonl").unlink()      # 只留分批文件
PYEOF
MP=$(python3 "$S/verify_pass2.py" merge --run-dir "$RUN4")
ge "分批裁定文件被全部读到" "$(echo "$MP" | jget "['verdict_files']")" "$BN"
check "并行分批与单文件写法的判定逐字相同" \
  "$([ "$(sha256sum "$RUN4/work/issues-verified.jsonl" | cut -d' ' -f1)" = "$HS" ] && echo yes || echo no)" yes

# 少跑一批 = 那批全部缺裁定 → 必须整批淘汰（fail-closed），不能当成"没问题"放行
LAST=$(ls "$V4" | grep '^pass2-primary\.v[0-9]*\.verdicts\.jsonl$' | tail -1)
LOST=$(python3 -c "
import json,sys;print(sum(1 for _ in open(sys.argv[1],encoding='utf-8')))" "$V4/$LAST")
mv "$V4/$LAST" "$V4/$LAST.bak"
MM=$(python3 "$S/verify_pass2.py" merge --run-dir "$RUN4")
ge "漏跑一批时缺裁定条数" "$(echo "$MM" | jget "['missing_verdict']")" "$LOST"
mv "$V4/$LAST.bak" "$V4/$LAST"
python3 "$S/verify_pass2.py" merge --run-dir "$RUN4" >/dev/null

echo
echo "══ 22. Pass 4 分批裁定：候选不进主 Agent 上下文，漏答必须看得见 ══"

# RUN2 是 logic-injection，已有全部冲突候选
CVF2="$RUN2/work/conflicts-verified.jsonl"
cp "$CVF2" "$WORK/cv2.bak" 2>/dev/null || true
BB=$(python3 "$S/adjudicate_pass4.py" build --run-dir "$RUN2")
NC=$(echo "$BB" | jget "['candidates']")
NB=$(echo "$BB" | jget "['batches']")
ge "候选被拼成批次" "$NB" 2
check "分批文件数与批数一致" \
  "$(ls "$RUN2/work/conflicts/batches" | grep -c '^adjudicate-b[0-9]*\.json$')" "$NB"

# 题面只给两侧原文与位置。severity/action 属于"这条有多要紧"，会把模型往 CONFLICT 上带
python3 - "$RUN2" "$NC" <<'PYEOF'
import glob,json,sys
run,total=sys.argv[1],int(sys.argv[2])
groups=[]; bad=[]
for f in sorted(glob.glob(f"{run}/work/conflicts/batches/adjudicate-b*.json")):
    raw=open(f,encoding="utf-8").read()
    for k in ("severity","action","chapter_span"):
        if f'"{k}"' in raw: bad.append(f"题面含 {k}")
    d=json.load(open(f,encoding="utf-8"))
    groups+=d["groups"]
    for g in d["groups"]:
        if not g["sides"]: bad.append(f"{g['conflict_id']} 没有任何一侧原文")
if len(groups)!=total: bad.append(f"分批后条数对不上：{len(groups)} vs {total}")
if len({g["conflict_id"] for g in groups})!=total: bad.append("conflict_id 有重复")
for b in bad[:5]: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "题面完整、不重不漏、不含倾向性字段" "$?" 0

# 全部裁定 CONFLICT → collect 归并 → 与直接写 conflicts-verified.jsonl 等效
answer_all() { python3 - "$RUN2" "$1" "$2" <<'PYEOF'
import glob,json,pathlib,sys
run,verdict,skip=sys.argv[1],sys.argv[2],sys.argv[3]
vdir=pathlib.Path(run,"work","conflicts","verdicts"); vdir.mkdir(parents=True,exist_ok=True)
for f in vdir.glob("*.jsonl"): f.unlink()
pathlib.Path(run,"work","conflicts-verified.jsonl").write_text("",encoding="utf-8")
for f in sorted(glob.glob(f"{run}/work/conflicts/batches/adjudicate-b*.json")):
    d=json.load(open(f,encoding="utf-8"))
    if d["batch_id"]==skip: continue          # 模拟漏跑一批
    (vdir/f"adjudicate-{d['batch_id']}.verdicts.jsonl").write_text(
        "".join(json.dumps({"conflict_id":g["conflict_id"],"verdict":verdict,"note":"回归"},
                           ensure_ascii=False)+"\n" for g in d["groups"]),encoding="utf-8")
PYEOF
}
answer_all CONFLICT ""
C1=$(python3 "$S/adjudicate_pass4.py" collect --run-dir "$RUN2")
check "全部裁定后无漏答" "$(echo "$C1" | jget "['missing']")" 0
check "归并条数 = 候选条数" "$(echo "$C1" | jget "['verdicts']")" "$NC"
ge "裁定后有批注进文档" "$(python3 "$S/apply_comments.py" plan --run-dir "$RUN2" | jget "['comments']")" 5

# collect 幂等：再跑一次不丢、不重
H4=$(sha256sum "$CVF2" | cut -d' ' -f1)
python3 "$S/adjudicate_pass4.py" collect --run-dir "$RUN2" >/dev/null
check "collect 幂等" \
  "$([ "$(sha256sum "$CVF2" | cut -d' ' -f1)" = "$H4" ] && echo yes || echo no)" yes

# 漏跑一批：必须报出来。conflict_admitted 对未裁定是 fail-closed（对的），
# 于是漏答表现为「这条冲突凭空消失」——不报数就没人会发现。
answer_all CONFLICT b01
C2=$(python3 "$S/adjudicate_pass4.py" collect --run-dir "$RUN2")
ge "漏跑一批时报出缺裁定条数" "$(echo "$C2" | jget "['missing']")" 1
python3 -c "import sys;sys.exit(0 if $(python3 "$S/apply_comments.py" plan --run-dir "$RUN2" | jget "['comments']") < $(echo "$C1" | jget "['verdicts']") else 1)"
check "漏答的候选不进交付物" "$?" 0

# 格式不对的裁定行不得被当成 UNSURE 收下——那是"模型拿不准"的待遇，
# Critical 级的 UNSURE 还会被保留并标注，等于把一行乱码升格成一条冲突
answer_all CONFLICT ""
python3 - "$RUN2" <<'PYEOF'
import glob,json,pathlib,sys
run=sys.argv[1]
f=sorted(glob.glob(f"{run}/work/conflicts/verdicts/*.jsonl"))[0]
rows=[json.loads(l) for l in open(f,encoding="utf-8")]
rows[0]["verdict"]="大概是矛盾吧"
pathlib.Path(f).write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),
                           encoding="utf-8")
PYEOF
C3=$(python3 "$S/adjudicate_pass4.py" collect --run-dir "$RUN2")
ge "无法解析的裁定行被计数" "$(echo "$C3" | jget "['invalid_lines']")" 1
ge "无法解析 = 未裁定（计入缺裁定）" "$(echo "$C3" | jget "['missing']")" 1
python3 - "$RUN2" <<'PYEOF'
import json,sys
bad=[json.loads(l) for l in open(f"{sys.argv[1]}/work/conflicts-verified.jsonl",encoding="utf-8")
     if json.loads(l)["verdict"] not in ("CONFLICT","NOT_CONFLICT","UNSURE")]
sys.exit(1 if bad else 0)
PYEOF
check "无法解析的裁定不写入交付入口" "$?" 0

cp "$WORK/cv2.bak" "$CVF2" 2>/dev/null || true

echo
echo "══ 23. 阶段化单元：子 Agent 只读一个文件、只写一个文件 ══"

mkdir -p "$WORK/st"
cp "$F/typo-pattern.docx" "$WORK/st/st.docx"
printf 'pattern_review:\n  enabled: true\n' > "$WORK/st/cfg.yaml"
RUN6=$(cd "$WORK/st" && python3 "$S/workspace.py" init --source "$WORK/st/st.docx" \
       --config "$WORK/st/cfg.yaml" --temp-dir "$TEMP" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN6" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN6" >/dev/null
python3 "$S/import_glossary.py" --run-dir "$RUN6" >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN6" >/dev/null
python3 "$S/typo_scan.py" scan --run-dir "$RUN6" >/dev/null
python3 "$S/scan_patterns.py" scan --run-dir "$RUN6" >/dev/null
PP=$(python3 "$S/prompt_pack.py" build --run-dir "$RUN6")
ge "四个阶段都渲染了 prompt" \
   "$(echo "$PP" | jget "[\"written\"]['review']+d['written']['extract']+d['written']['typo']+d['written']['pattern']")" 4

# 自包含：占位符全部填掉，且子 Agent 需要的东西都在文件里
python3 - "$RUN6" <<'PYEOF'
import glob,json,pathlib,sys
run=pathlib.Path(sys.argv[1]); bad=[]
chunk=(run/"work"/"chunks"/"chunk-0001.txt").read_text(encoding="utf-8")
tax=(pathlib.Path(__file__).parent if False else None)
rv=(run/"work"/"prompts"/"review-0001.md").read_text(encoding="utf-8")
ex=(run/"work"/"prompts"/"extract-0001.md").read_text(encoding="utf-8")
for name,txt in (("review",rv),("extract",ex)):
    if "{{" in txt: bad.append(f"{name} 仍有未替换的占位符")
    if chunk.strip()[:60] not in txt: bad.append(f"{name} 里没有分片正文")
    if str(run) not in txt: bad.append(f"{name} 没写明输出路径")
# 审查 prompt 必须自带类型体系与不改清单——否则子 Agent 还得去读 references/
if "A2" not in rv or "绝对不要上报" not in rv: bad.append("review 缺类型体系/不改清单")
# 抽取 prompt 不该夹带审查用的大文件
if "绝对不要上报" in ex: bad.append("extract 夹带了不改清单（白占上下文）")
for b in bad[:5]: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "prompt 自包含：占位符已填、正文与输出路径都在、不夹带无关大文件" "$?" 0

# 每个阶段的 prompt 都比"读全套 references"小得多；review 是上界
python3 - "$RUN6" "$SKILL" <<'PYEOF'
import pathlib,sys
run,skill=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2])
refs=sum(len((skill/"references"/f).read_text(encoding="utf-8"))
         for f in ("taxonomy.md","never-flag.md","schemas.md"))
sizes={p.name:len(p.read_text(encoding="utf-8"))
       for p in (run/"work"/"prompts").glob("*.md")}
big=[n for n,v in sizes.items() if not n.startswith("review") and v > refs]
print("    ", {n:v for n,v in sorted(sizes.items())}, f"references 合计 {refs}")
sys.exit(1 if big else 0)
PYEOF
check "非审查阶段的 prompt 小于三份 references 之和" "$?" 0

# claim next 是给子 Agent 看的：只给它用得上的路径，不吐 pid 列表
CN=$(python3 "$S/workspace.py" claim next --run-dir "$RUN6" --stage review --session s-st)
check "claim next 返回单元与它要读的 prompt" "$(echo "$CN" | jget "['chunk']['unit']")" 0001
python3 - <<PYEOF
import json,sys
r=json.loads('''$CN''')
d=r["chunk"]
bad=[k for k in ("pids","review_pids","context_pids") if k in d]
if bad: print("    claim 输出里有大列表：", bad)
# 目录只说一次（顶层 dir），单元里必须是文件名而不是绝对路径——每个单元一条绝对路径，
# 在 stdout 与派活指令里各出现一次，60 个单元就是几万字符
if "/" in d.get("prompt",""): bad.append("prompt 是绝对路径，应为文件名")
if "output" in d: bad.append("output 冗余：prompt 抬头已写明输出路径")
if not r.get("dir"): bad.append("顶层缺 dir")
n=len(json.dumps(d,ensure_ascii=False))
if n > 120: bad.append("单元视图过大 %d" % n)
for b in bad[:3]: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "claim next 单元视图只有 unit 与 prompt 文件名（目录只说一次）" "$?" 0

# 阶段之间互不干扰：review 被占住，extract 照领不误
CE=$(python3 "$S/workspace.py" claim next --run-dir "$RUN6" --stage extract --session s-st2)
check "另一阶段的同一分片可并行领取" "$(echo "$CE" | jget "['stage']")" extract
CR=$(python3 "$S/workspace.py" claim next --run-dir "$RUN6" --stage review --session s-other)
check "同阶段同单元不会被两个会话同时领走" "$(echo "$CR" | jget "['exhausted']")" True

# 子 Agent 崩了：claim 还在、产物没有。不 reclaim 就一直锁到 TTL
ST=$(python3 "$S/workspace.py" claim status --run-dir "$RUN6" --stage review)
check "崩掉的单元仍被计为 claimed" "$(echo "$ST" | jget "['claimed']")" 1
RC=$(python3 "$S/workspace.py" claim reclaim --run-dir "$RUN6" --stage review --session s-st)
check "reclaim 回收本会话没有产物的 claim" "$(echo "$RC" | jget "['released']")" 1
CR2=$(python3 "$S/workspace.py" claim next --run-dir "$RUN6" --stage review --session s-other)
check "回收后可被重新领走（重派是安全的）" "$(echo "$CR2" | jget "['chunk']['unit']")" 0001

# 别人正在跑的 claim 不许动
python3 "$S/workspace.py" claim reclaim --run-dir "$RUN6" --stage review --session s-st >/dev/null
CR3=$(python3 "$S/workspace.py" claim status --run-dir "$RUN6" --stage review)
check "带 --session 的 reclaim 不动别的会话" "$(echo "$CR3" | jget "['claimed']")" 1

# 三波产出 → 一次性收口。三条通道的产物互不覆盖
python3 - "$RUN6" <<'PYEOF'
import json,pathlib,sys
run=pathlib.Path(sys.argv[1])
paras={json.loads(l)["text"]:json.loads(l)["pid"]
       for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")}
pid=next(p for t,p in paras.items() if "阀值" in t)
(run/"work"/"issues").mkdir(parents=True,exist_ok=True)
(run/"work"/"issues"/"issues-0001.raw.jsonl").write_text(json.dumps(
 {"pid":pid,"category":"A8","original_text":"设定为 200 毫秒","suggested_text":"设定为 200ms",
  "evidence":"单位不统一","severity":"High"},ensure_ascii=False)+"\n",encoding="utf-8")
(run/"work"/"facts").mkdir(parents=True,exist_ok=True)
(run/"work"/"facts"/"facts-0001.json").write_text("{}",encoding="utf-8")
# 错别字候选跨片攒批：批次文件是 typos-gNN.json，裁定写同名 .verdicts.jsonl
for b in json.load(open(run/"work"/"typos"/"typos-index.json",encoding="utf-8"))["batches"]:
    t=json.load(open(run/"work"/"typos"/f"typos-{b['batch_id']}.json",encoding="utf-8"))
    (run/"work"/"typos"/f"typos-{b['batch_id']}.verdicts.jsonl").write_text(
      "".join(json.dumps({"tid":x["tid"],"verdict":"B"},ensure_ascii=False)+"\n"
              for x in t["items"]),encoding="utf-8")
p=json.load(open(run/"work"/"patterns"/"patterns-0001.p01.json",encoding="utf-8"))
v={0:{"impact":"Y","mitigation":"Y","owner":"Y"},1:{"impact":"N","mitigation":"N","owner":"U"},
   2:{"request":"Y","response":"Y","error":"Y"},3:{"request":"Y","response":"N","error":"N"}}
with open(run/"work"/"patterns"/"patterns-0001.p01.verdicts.jsonl","w",encoding="utf-8") as f:
    for i,c in enumerate(p["items"]):
        for k,a in v[i].items():
            f.write(json.dumps({"cid":c["cid"],"key":k,"answer":a},ensure_ascii=False)+"\n")
PYEOF
python3 "$S/verify_span.py" --run-dir "$RUN6" --all --channel main >/dev/null
python3 "$S/filter_neverflag.py" --run-dir "$RUN6" --all --channel main >/dev/null
python3 "$S/typo_scan.py" merge --run-dir "$RUN6" >/dev/null
TSW=$(python3 "$S/verify_span.py" --run-dir "$RUN6" --all --channel typos)
python3 "$S/filter_neverflag.py" --run-dir "$RUN6" --all --channel typos >/dev/null
python3 "$S/scan_patterns.py" merge --run-dir "$RUN6" >/dev/null
PSW=$(python3 "$S/verify_span.py" --run-dir "$RUN6" --all --channel patterns)
ge "错别字通道整轮过闸（tid 回填对得上）" "$(echo "$TSW" | jget "['count']")" 8
ge "范式通道整轮过闸" "$(echo "$PSW" | jget "['count']")" 2
python3 - "$RUN6" <<'PYEOF'
import json,pathlib,sys
d=pathlib.Path(sys.argv[1],"work","issues")
main=[json.loads(l) for l in open(d/"issues-0001.jsonl",encoding="utf-8")]
typo=[json.loads(l) for l in open(d/"issues-0001.typos.jsonl",encoding="utf-8")]
pat=[json.loads(l) for l in open(d/"issues-0001.patterns.jsonl",encoding="utf-8")]
bad=[]
if {r["category"] for r in main} != {"A8"}: bad.append("主通道被支线覆盖了")
if {r["category"] for r in typo} != {"A1"}: bad.append("错别字通道内容不对")
if {r["category"] for r in pat} != {"P1"}: bad.append("范式通道内容不对")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "三条通道的产物互不覆盖（--all 扫描按通道分文件）" "$?" 0

# 全部单元完成后，各阶段都应报 exhausted
for st in review extract typo pattern; do
  E=$(python3 "$S/workspace.py" claim next --run-dir "$RUN6" --stage "$st" --session s-fin | jget "['exhausted']")
  [ "$E" = "True" ] || bad "阶段 $st 仍有未完成单元"
done
ok "四个阶段全部完成后不再派活（产物存在即状态）"

# 重切片必须把已渲染的 prompt 一并作废，否则子 Agent 会照着旧正文作答
printf 'chunking:\n  max_text_tokens: 300\n  single_pass_limit: 100\n' > "$WORK/st/small.yaml"
python3 "$S/chunk.py" --run-dir "$RUN6" --config "$WORK/st/small.yaml" >/dev/null
check "重切片后旧的 prompt 已作废" \
  "$([ -f "$RUN6/work/prompts/review-0001.md" ] && echo no || echo yes)" yes
PPL=$(python3 "$S/prompt_pack.py" list --run-dir "$RUN6" --stage review)
ge "重切片后单元数变多" "$(echo "$PPL" | jget "['stages']['review']['units']")" 2
check "重切片后 prompt 缺失可被发现" "$(echo "$PPL" | jget "['stages']['review']['prompts']")" 0

echo
echo "══ 24. 环境抖动：半写产物不得被当成「已完成」 ══"

# 重新把 RUN6 切回默认切法并补齐题面
python3 "$S/chunk.py" --run-dir "$RUN6" >/dev/null
python3 "$S/typo_scan.py" scan --run-dir "$RUN6" >/dev/null
python3 "$S/scan_patterns.py" scan --run-dir "$RUN6" >/dev/null
python3 "$S/prompt_pack.py" build --run-dir "$RUN6" >/dev/null
rm -f "$RUN6"/work/chunks/*.claim

# 子 Agent 写到一半被杀：facts 是截断的 JSON
python3 - "$RUN6" <<'PYEOF'
import pathlib,sys
f=pathlib.Path(sys.argv[1],"work","facts","facts-0001.json")
f.parent.mkdir(parents=True,exist_ok=True)
f.write_text('{"terms":[{"term":"边缘节点","definit', encoding="utf-8")   # 断在半路
PYEOF
check "半写产物文件确实存在（构造成立）" \
  "$([ -s "$RUN6/work/facts/facts-0001.json" ] && echo yes || echo no)" yes
SE=$(python3 "$S/workspace.py" claim status --run-dir "$RUN6" --stage extract)
check "半写不算完成" "$(echo "$SE" | jget "['done']")" 0
ge "半写被单独报出来" "$(echo "$SE" | jget "['corrupt']")" 1
CX=$(python3 "$S/workspace.py" claim next --run-dir "$RUN6" --stage extract --session s-fix)
check "半写的单元会被重新派出去" "$(echo "$CX" | jget "['chunk']['unit']")" 0001

# 负向对照：按「文件存在」判定的话，这个单元就是"已完成"——
# 而 read_json 对坏 JSON 静默返回默认值，整片事实凭空消失，报告里也看不出来
python3 - "$S" "$RUN6" <<'PYEOF'
import pathlib,sys
sys.path.insert(0, sys.argv[1])
import workspace as ws
u=[x for x in ws.stage_units(pathlib.Path(sys.argv[2]), "extract") if x["unit"]=="0001"][0]
exists_only = u["done_marker"].exists()
really_ok   = ws.product_ok(u["done_marker"])
print(f"    按存在性判定：{exists_only}；按可解析判定：{really_ok}")
sys.exit(0 if (exists_only and not really_ok) else 1)
PYEOF
check "负向对照：只看存在性会把半写判成已完成" "$?" 0

DOC=$(python3 "$S/state.py" doctor --run-dir "$RUN6" --fix)
ge "doctor 报出半写产物" "$(echo "$DOC" | jget "['findings']" | grep -c 半写)" 1
check "doctor --fix 之后半写产物已作废" \
  "$([ -f "$RUN6/work/facts/facts-0001.json" ] && echo no || echo yes)" yes

# 收口时一个坏文件不得拖垮整轮：其余分片照常过闸
python3 "$S/chunk.py" --run-dir "$RUN6" --config "$WORK/st/small.yaml" >/dev/null
python3 - "$RUN6" <<'PYEOF'
import json,pathlib,sys
run=pathlib.Path(sys.argv[1]); d=run/"work"/"issues"; d.mkdir(parents=True,exist_ok=True)
ids=[c["chunk_id"] for c in json.load(open(run/"work"/"chunks"/"index.json",encoding="utf-8"))["chunks"]]
paras=[json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")]
pids={c: [p["pid"] for p in paras if p["pid"] in set(
        json.load(open(run/"work"/"chunks"/"index.json",encoding="utf-8"))["chunks"][i]["pids"])]
      for i, c in enumerate(ids)}
good=json.dumps({"pid":pids[ids[-1]][0],"category":"A2","original_text":"认真的完成",
                 "suggested_text":"认真地完成","evidence":"状语应用地","severity":"High"},
                ensure_ascii=False)
(d/f"issues-{ids[0]}.raw.jsonl").write_text('{"pid":"p-000001","categ', encoding="utf-8")  # 半写
(d/f"issues-{ids[-1]}.raw.jsonl").write_text(good+"\n", encoding="utf-8")
print(f"    构造：{ids[0]} 半写，{ids[-1]} 完整（共 {len(ids)} 片）")
PYEOF
SW=$(python3 "$S/verify_span.py" --run-dir "$RUN6" --all --channel main)
ge "半写的那一片被单独作废并报数" "$(echo "$SW" | jget "['unparsable']")" 1
ge "其余分片照常过闸（一个坏文件不拖垮整轮）" "$(echo "$SW" | jget "['chunks']")" 1

echo

echo "══ 25. 配置改动与 prompt 体量：在派活之前就拦下来 ══"

# 改配置的唯一入口：合并进快照，且说明要重跑什么
RC=$(python3 "$S/workspace.py" reconfigure --run-dir "$RUN6" --set chunking.max_text_tokens=7000)
check "reconfigure 报出改了哪一项" "$(echo "$RC" | jget "['changed'][0]")" chunking.max_text_tokens
python3 - "$RC" <<'PYEOF'
import json,sys
d=json.loads(sys.argv[1])
sys.exit(0 if d["rerun"] and "第 3 步" in d["rerun"][0] else 1)
PYEOF
check "reconfigure 说明这次改动要重跑哪些步骤" "$?" 0

# 关键：此后**不带 --config** 的脚本按新值跑（旧行为是回落默认配置，半生效且不报错）
python3 "$S/chunk.py" --run-dir "$RUN6" >/dev/null
python3 - "$RUN6" <<'PYEOF'
import json,pathlib,sys,yaml
run=pathlib.Path(sys.argv[1])
snap=yaml.safe_load((run/"config.snapshot.yaml").read_text(encoding="utf-8"))
idx=json.load(open(run/"work"/"chunks"/"index.json",encoding="utf-8"))
assert snap["chunking"]["max_text_tokens"]==7000, "快照没写进去"
over=[c for c in idx["chunks"] if c["text_tokens"] > 7000]
assert not over, f"{len(over)} 片超过改后的预算，说明 chunk.py 没读到新配置"
PYEOF
check "缺省 --config 时后续脚本按 reconfigure 之后的值跑" "$?" 0

# D4：落笔门槛写什么都无效
python3 "$S/workspace.py" reconfigure --run-dir "$RUN6" --set apply_threshold=thorough >/dev/null
python3 - "$RUN6" <<'PYEOF'
import pathlib,sys,yaml
snap=yaml.safe_load(pathlib.Path(sys.argv[1],"config.snapshot.yaml").read_text(encoding="utf-8"))
sys.exit(0 if snap["apply_threshold"]=="conservative" else 1)
PYEOF
check "reconfigure 不能放宽落笔门槛（D4）" "$?" 0

# 以下用与真实压测同形的语料（约 21 万字）：小 fixture 的分片再大也就几千字符，
# 撞不出「子 Agent 装不下」这个现场
mkdir -p "$WORK/big2"
cp "$F/sample-basic.docx" "$WORK/big2/big2.docx"
RUN7=$(python3 "$S/workspace.py" init --source "$WORK/big2/big2.docx" \
       --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN7" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN7" >/dev/null
python3 - "$RUN7" <<'PYEOF'
import json,pathlib,random,sys
run=pathlib.Path(sys.argv[1]); random.seed(7)
body="本系统在设计上采用分层架构，各层之间通过标准接口交互，确保模块可替换与可测试。"
rows=[]; i=0; chars=0; sec=0
while chars < 211573:
    i+=1
    if i%14==1:
        sec+=1; t=f"{sec} 第 {sec} 章 模块设计与接口约定"; lvl,h=2,True
    else:
        t=(body*7)[:random.randint(120,320)]; lvl,h=None,False
    rows.append({"pid":f"p-{i:06d}","index":i,"heading_path":[f"{sec} 章"],"level":lvl,
                 "is_heading":h,"style":"Normal","style_name":"Normal","text":t,"char_len":len(t),
                 "in_table":False,"table_id":None,"row_idx":None,"cell_idx":None,"is_list":False,
                 "is_code":False,"is_quote":False,"page_hint":1+chars//265,"page_estimated":True})
    chars+=len(t)
(run/"work"/"paragraphs.jsonl").write_text(
    "".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8")
PYEOF

# 默认配置必须自洽：默认预算渲染出的 prompt 不得超过默认上限
python3 "$S/chunk.py" --run-dir "$RUN7" >/dev/null
python3 "$S/typo_scan.py" scan --run-dir "$RUN7" >/dev/null
PD0=$(python3 "$S/prompt_pack.py" build --run-dir "$RUN7"); RC0=$?
check "默认配置自洽：800 页语料按默认预算渲染不超上限" "$RC0" 0
python3 - "$PD0" <<'PYEOF'
import json,sys
d=json.loads(sys.argv[1])
print("    默认配置：prompt 最大", d["max_chars"], "/ 上限", d["limit"])
PYEOF

# 分片太大：必须在派活之前以退出码 8 终止，并给出该设成多少
python3 "$S/workspace.py" reconfigure --run-dir "$RUN7" --set chunking.max_text_tokens=15000 >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN7" >/dev/null
python3 "$S/typo_scan.py" scan --run-dir "$RUN7" >/dev/null
PB=$(python3 "$S/prompt_pack.py" build --run-dir "$RUN7" 2>/dev/null); RC8=$?
check "prompt 超限以退出码 8 终止" "$RC8" 8
ge "报出超限单元数" "$(echo "$PB" | jget "['over_limit']")" 1
python3 - "$PB" <<'PYEOF'
import json,sys
d=json.loads(sys.argv[1])
print("    建议值：", d.get("suggest_max_text_tokens"), "（上限", d["limit"],
      "，实测最大", d["max_chars"], "，固定开销", d.get("fixed_overhead_chars"), "）")
sys.exit(0 if d.get("suggest_max_text_tokens", 10**9) < 15000 else 1)
PYEOF
check "给出建议值（严格小于当前值）" "$?" 0

# 照建议改完就该通过——建议值不能只是"更小"，得真的解决问题
python3 - "$S" "$RUN7" "$PB" <<'PYEOF'
import json,subprocess,sys
S,run,pb=sys.argv[1],sys.argv[2],json.loads(sys.argv[3])
for cmd in ([f"{S}/workspace.py","reconfigure","--run-dir",run,
             "--set",f"chunking.max_text_tokens={pb['suggest_max_text_tokens']}"],
            [f"{S}/chunk.py","--run-dir",run],
            [f"{S}/typo_scan.py","scan","--run-dir",run]):
    subprocess.run([sys.executable]+cmd,check=True,capture_output=True)
r=subprocess.run([sys.executable,f"{S}/prompt_pack.py","build","--run-dir",run],capture_output=True)
out=json.loads(r.stdout.decode().strip().splitlines()[-1])
print("    照建议改完：exit",r.returncode,"，最大",out.get("max_chars"),"/ 上限",out.get("limit"))
sys.exit(0 if r.returncode==0 and out["over_limit"]==0 else 1)
PYEOF
check "照建议值改完即通过（建议值是可执行的，不是安慰）" "$?" 0

# 上限比固定开销还小时，调分片没用——必须换一条建议，否则 Agent 会陷在
# 「改了、重跑、还是 8」的死循环里
python3 "$S/workspace.py" reconfigure --run-dir "$RUN7" --set chunking.max_prompt_chars=1200 >/dev/null
PZ=$(python3 "$S/prompt_pack.py" build --run-dir "$RUN7" 2>/dev/null)
python3 - "$PZ" <<'PYEOF'
import json,sys
d=json.loads(sys.argv[1])
print("    上限过低时给的是：", d["error"][:36], "→ 至少", d.get("min_viable_prompt_chars"))
sys.exit(0 if d.get("min_viable_prompt_chars") and "suggest_max_text_tokens" not in d else 1)
PYEOF
check "上限低于固定开销时改口建议调上限（不给无效的分片建议）" "$?" 0

echo "══ 26. 派活打包：按上下文预算成组领取 ══"

# RUN7 是 800 页语料（第 25 节建的），先恢复默认配置并渲染 prompt
python3 "$S/workspace.py" reconfigure --run-dir "$RUN7" \
        --set chunking.max_text_tokens=10000 --set chunking.max_prompt_chars=20000 >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN7" >/dev/null
python3 "$S/typo_scan.py" scan --run-dir "$RUN7" >/dev/null
python3 "$S/prompt_pack.py" build --run-dir "$RUN7" >/dev/null
rm -f "$RUN7"/work/chunks/*.claim

P1=$(python3 "$S/workspace.py" claim next --run-dir "$RUN7" --stage review --count 8 --session pk1)
ge "一次能领到多个单元" "$(echo "$P1" | jget "['count']")" 2

# 预算是按**字符**算的。用 stat() 的字节数会把包打成三分之一大（中文一字三字节）
python3 - "$RUN7" "$P1" <<'PYEOF'
import json,pathlib,sys,yaml
run,got=pathlib.Path(sys.argv[1]),json.loads(sys.argv[2])
cfg=yaml.safe_load((run/"config.snapshot.yaml").read_text(encoding="utf-8"))
budget=cfg["concurrency"]["subagent_budget_chars"]
idx=json.load(open(run/"work"/"prompts"/"index.json",encoding="utf-8"))
chars=sum(idx[f"review/{u['unit']}"] for u in got["units"])
byts=sum(len((pathlib.Path(got["dir"])/u["prompt"]).read_bytes()) for u in got["units"])
print(f"    本组 {got['count']} 个单元：{chars} 字符 / {byts} 字节（预算 {budget}）")
assert chars <= budget, "超预算"
assert byts > budget, "构造无效：这组的字节数没有超过预算，测不出字符/字节的差别"
PYEOF
check "打包按字符数算预算，不是字节数（负向对照：同一组按字节算会超）" "$?" 0

# 主 Agent 的上下文预算：run_dir 那条绝对路径**每次领取只准出现一次**。
# 实测 800 页文档的 Pass 1，主 Agent 上下文里近一半的字符是这条前缀的重复——
# 每单元两条绝对路径，在 stdout 里一次、转给子 Agent 时再一次。
python3 - "$RUN7" "$P1" <<PYEOF
import json,sys
run,got=sys.argv[1],json.loads(sys.argv[2])
raw=json.dumps(got,ensure_ascii=False)
bad=[]
if raw.count(run) != 1: bad.append(f"run_dir 在一条 claim next 里出现了 {raw.count(run)} 次，应为 1（顶层 dir）")
per=[len(json.dumps(u,ensure_ascii=False)) for u in got["units"]]
if per and max(per) > 120: bad.append(f"单元视图 {max(per)} 字符，超出预算")
print(f"    一条 claim next：{len(raw)} 字符，单元视图最大 {max(per) if per else 0} 字符")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "claim next 里 run_dir 只出现一次（主 Agent 上下文预算）" "$?" 0

# 领过的不会再被领走
P2=$(python3 "$S/workspace.py" claim next --run-dir "$RUN7" --stage review --count 8 --session pk2)
python3 - "$P1" "$P2" <<'PYEOF'
import json,sys
a={u["unit"] for u in json.loads(sys.argv[1])["units"]}
b={u["unit"] for u in json.loads(sys.argv[2])["units"]}
sys.exit(1 if a & b else 0)
PYEOF
check "两次领取不重叠" "$?" 0

# 预算小于单个 prompt 时也必须派得出去，否则整波死锁
python3 "$S/workspace.py" reconfigure --run-dir "$RUN7" --set concurrency.subagent_budget_chars=100 >/dev/null
P3=$(python3 "$S/workspace.py" claim next --run-dir "$RUN7" --stage review --count 8 --session pk3)
check "预算再小也至少派一个（不死锁）" "$(echo "$P3" | jget "['count']")" 1

# **一个问题都没查出来的分片，产物就是一个空 JSONL——那是"做完了"，不是"没做完"。**
# 判成没做完，这类分片会被无限重派，而"没查出问题"恰恰是常态。
python3 - "$RUN7" <<'PYEOF'
import json,pathlib,sys
run=pathlib.Path(sys.argv[1])
d=run/"work"/"issues"; d.mkdir(parents=True,exist_ok=True)
for c in json.load(open(run/"work"/"chunks"/"index.json",encoding="utf-8"))["chunks"]:
    (d/f"issues-{c['chunk_id']}.raw.jsonl").write_text("",encoding="utf-8")
PYEOF
P4=$(python3 "$S/workspace.py" claim next --run-dir "$RUN7" --stage review --count 8 --session pk4)
check "零问题的分片算已完成（空 JSONL 是合法产物，不得无限重派）" "$(echo "$P4" | jget "['exhausted']")" True
python3 - "$S" <<'PYEOF'
import pathlib,sys,tempfile
sys.path.insert(0, sys.argv[1])
from _common import product_ok
d=pathlib.Path(tempfile.mkdtemp())
(d/"a.jsonl").write_text("",encoding="utf-8")
(d/"b.json").write_text("",encoding="utf-8")
(d/"c.json").write_text('{"terms":[',encoding="utf-8")
assert product_ok(d/"a.jsonl") is True,  "空 JSONL 应算完成"
assert product_ok(d/"b.json") is False,  "空 JSON 不该算完成"
assert product_ok(d/"c.json") is False,  "半写 JSON 不该算完成"
PYEOF
check "空 JSONL 合法、空 JSON 与半写 JSON 不合法" "$?" 0
python3 "$S/workspace.py" reconfigure --run-dir "$RUN7" --set concurrency.subagent_budget_chars=40000 >/dev/null

# 错别字候选跨片攒批：按片分批会让只有几个候选的分片也独占一次调用。
# 用 typo-pattern 那份 fixture 切成多片来验——候选总数不变，批次数不该跟着片数走。
mkdir -p "$WORK/gb"
cp "$F/typo-pattern.docx" "$WORK/gb/gb.docx"
RUN8=$(python3 "$S/workspace.py" init --source "$WORK/gb/gb.docx" \
       --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN8" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN8" >/dev/null
python3 "$S/import_glossary.py" --run-dir "$RUN8" >/dev/null
python3 "$S/workspace.py" reconfigure --run-dir "$RUN8" \
        --set chunking.max_text_tokens=120 --set chunking.single_pass_limit=100 >/dev/null
NCH=$(python3 "$S/chunk.py" --run-dir "$RUN8" | jget "['chunks']")
TS8=$(python3 "$S/typo_scan.py" scan --run-dir "$RUN8")
ge "构造：切成多片" "$NCH" 5
python3 - "$TS8" <<'PYEOF'
import json,sys
n=[r["candidates"] for r in json.loads(sys.argv[1])["results"] if r["candidates"]]
print("    候选分布在", len(n), "个分片上：", n)
sys.exit(0 if len(n) >= 2 else 1)
PYEOF
check "构造：候选分散在多个分片上" "$?" 0
check "跨片攒批：批次数不跟着片数走" "$(echo "$TS8" | jget "['batches']")" 1
TU8=$(python3 "$S/workspace.py" claim status --run-dir "$RUN8" --stage typo)
check "错别字单元数 = 批次数（不是分片数）" "$(echo "$TU8" | jget "['total']")" 1

# 跨片批次里混着多个分片的候选，靠 tid 归属；merge 必须各归各片
python3 - "$RUN8" <<'PYEOF'
import json,pathlib,sys
run=pathlib.Path(sys.argv[1]); t=run/"work"/"typos"
b=json.load(open(t/"typos-g01.json",encoding="utf-8"))
cids={x["tid"].split("-")[0] for x in b["items"]}
assert len(cids) >= 2, f"这一批只覆盖了 {cids}，构造无效"
(t/"typos-g01.verdicts.jsonl").write_text(
    "".join(json.dumps({"tid":x["tid"],"verdict":"B"},ensure_ascii=False)+"\n"
            for x in b["items"]),encoding="utf-8")
PYEOF
check "构造：一批里混着多个分片的候选" "$?" 0
MG8=$(python3 "$S/typo_scan.py" merge --run-dir "$RUN8")
ge "merge 按 tid 把裁定归回各自的分片" "$(echo "$MG8" | jget "['merged']")" 8
python3 - "$RUN8" <<'PYEOF'
import json,pathlib,sys
d=pathlib.Path(sys.argv[1],"work","issues")
files=sorted(d.glob("issues-*.typos.jsonl"))
per={f.name: sum(1 for _ in open(f,encoding="utf-8")) for f in files if f.stat().st_size}
print("    各片各归各的：", per)
sys.exit(0 if len(per) >= 2 else 1)
PYEOF
check "裁定落回了多个分片的产物（没有全堆到第一片）" "$?" 0

echo "══ 27. 照 SKILL.md 第 4 步把波内循环跑一遍 ══"
# 单元测试全绿不等于主流程能跑（CLAUDE.md）。这一节不测单个脚本，测的是
# **SKILL.md 写的那个顺序**：claim next → 子 Agent 写 output → claim reclaim → 再来一轮，
# **中途不跑任何闸门**（闸门按 SKILL.md 是整波跑完之后的收口）。
# 上一版在这里断掉过：review 的完成标记指向闸门产物 issues-<片>.jsonl，
# 而闸门要等全波跑完才跑 —— done 恒 0、pending 恒不降、reclaim 把做完的单元一并放掉，
# 下一轮又领到同样的单元。第一波永远出不去，而每个脚本的单元测试都是绿的。

RUN9=$(python3 "$S/workspace.py" init --source "$WORK/big2/big2.docx" --resume new \
       --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN9" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN9" >/dev/null
# 切小一点，逼出多轮循环：一片一轮的现场测不出「第二轮又领到同一个单元」
python3 "$S/workspace.py" reconfigure --run-dir "$RUN9" \
        --set chunking.max_text_tokens=300 --set chunking.single_pass_limit=100 >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN9" >/dev/null
ge "构造：切成多片才测得出多轮循环" \
  "$(python3 "$S/workspace.py" claim status --run-dir "$RUN9" --stage review | jget "['total']")" 5
python3 "$S/typo_scan.py" scan --run-dir "$RUN9" >/dev/null
python3 "$S/prompt_pack.py" build --run-dir "$RUN9" >/dev/null

# ① 通用守卫：完成标记必须就是子 Agent 写的那个文件。
#    这一条挡的是整类缺陷，不只是 review 那一处。
python3 - "$S" "$RUN9" <<'PYEOF'
import pathlib,sys
sys.path.insert(0, sys.argv[1])
import workspace as ws
run=pathlib.Path(sys.argv[2]); bad=[]
for st in ws.STAGES:
    for u in ws.stage_units(run, st):
        if u["done_marker"] != u["output"]:
            bad.append(f"{st}/{u['unit']}: 完成标记 {u['done_marker'].name} ≠ 产物 {u['output'].name}")
for b in bad[:3]: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "每个阶段的完成标记就是子 Agent 自己写的产物" "$?" 0

# ② 回放波内循环。派活规模照 SKILL.md：每轮 parallelism 个子 Agent，各领一组。
replay_wave() {   # $1=run_dir $2=stage —— 回显「轮数 完成数 是否 exhausted」
  python3 - "$S" "$1" "$2" <<'PYEOF'
import json,pathlib,subprocess,sys
S,run,stage=sys.argv[1],sys.argv[2],sys.argv[3]
sys.path.insert(0, S); import workspace as ws
def call(*a):
    return json.loads(subprocess.run([sys.executable,f"{S}/workspace.py","claim",*a,
        "--run-dir",run,"--stage",stage],capture_output=True,text=True).stdout)
total=call("status")["total"]
seen, rounds, prev_pending = set(), 0, total+1
while rounds < total*4 + 10:
    rounds += 1
    units=[]
    for _ in range(5):                                   # parallelism 个子 Agent
        got=call("next","--count","4","--session","w1","--generation","1")
        if got.get("exhausted") or not got["units"]: break
        units += got["units"]
    if not units: break
    outs={x["unit"]: x["output"] for x in ws.stage_units(pathlib.Path(run), stage)}
    for u in units:                                      # 子 Agent 读 prompt、按它抬头写 output
        if u["unit"] in seen: print(f"    重复派发：{stage}/{u['unit']}")
        seen.add(u["unit"])
        out=outs[u["unit"]]; out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text('{"terms":[],"acronyms":[],"entities":[],"metrics":[],"positions":[],'
                       '"objectives":[],"initiatives":[],"acceptance":[],"dates":[],'
                       '"versions":[],"roles":[],"xrefs":[],"numbering":[],"commitments":[],'
                       '"statuses":[],"enumerations":[],"conclusions":[]}'
                       if out.suffix==".json" else "", encoding="utf-8")
    call("reclaim","--session","w1")                     # SKILL.md：每波结束回收崩掉的 claim
    st=call("status")
    if st["pending"] >= prev_pending:
        print(f"    第 {rounds} 轮 pending 没有下降：{prev_pending} → {st['pending']}")
        break
    prev_pending=st["pending"]
final=call("status"); ex=call("next","--session","w1","--generation","1").get("exhausted")
print(f"{rounds} {final['done']}/{final['total']} {ex}")
PYEOF
}
for st in review extract typo; do
  R=$(replay_wave "$RUN9" "$st"); echo "$R" | grep -q '^ ' && echo "$R"
  L=$(echo "$R" | tail -1)
  echo "    $st：$(echo "$L" | cut -d' ' -f1) 轮，完成 $(echo "$L" | cut -d' ' -f2)"
  check "$st 波：不跑闸门也能一路收敛到 exhausted" "$(echo "$L" | cut -d' ' -f3)" True
  check "$st 波：全部单元都拿到了产物" \
    "$(echo "$L" | cut -d' ' -f2 | awk -F/ '{print ($1==$2)?"yes":"no"}')" yes
done

# ③ 负向对照：把 review 的完成标记换回闸门产物，同一段循环必须立刻退化成无限重派
python3 - "$S" "$RUN9" <<'PYEOF'
import json,pathlib,sys
sys.path.insert(0, sys.argv[1])
import workspace as ws
run=pathlib.Path(sys.argv[2])
for f in (run/"work"/"issues").glob("issues-*.raw.jsonl"): f.unlink()
orig=ws.stage_units
def patched(run_dir, stage):                       # 完成标记 = 闸门产物（旧行为）
    us=orig(run_dir, stage)
    if stage=="review":
        for u in us:
            u["done_marker"]=ws.resolve_path(run_dir,"issues")/f"issues-{u['chunk_id']}.jsonl"
    return us
ws.stage_units=patched
first=[u["unit"] for u in ws.next_pending_units(run,"review","neg",1,30,4,40000)]
for u in ws.stage_units(run,"review"):
    if u["unit"] in first: u["output"].write_text("", encoding="utf-8")
ws.reclaim_stage(run,"review","neg")
again=[u["unit"] for u in ws.next_pending_units(run,"review","neg",1,30,4,40000)]
print("    旧行为下第二轮又领到：", again[:2])
sys.exit(0 if first and first==again else 1)
PYEOF
check "负向对照：完成标记指向闸门产物即退化成无限重派（说明这项断言咬得住）" "$?" 0

# ⑤ 闸门摘要必须报出真实丢弃数。这是主 Agent 每一波唯一能看到的闸门信号：
#    报 0 的时候，「模型什么都没查出来」与「查出来的全被闸门丢了」长得一模一样。
python3 - "$S" "$RUN9" <<'PYEOF'
import json,pathlib,sys
sys.path.insert(0, sys.argv[1]); import workspace as ws
run=pathlib.Path(sys.argv[2])
for f in (run/"work"/"issues").glob("issues-*"): f.unlink()
u=ws.stage_units(run,"review")[0]
u["output"].parent.mkdir(parents=True,exist_ok=True)
# 跨度只有 2 字 → 必被 min_span_chars 丢掉
u["output"].write_text(json.dumps({"pid":"p-000001","category":"A2","original_text":"的的",
    "suggested_text":"的","evidence":"重复","severity":"High"},ensure_ascii=False)+"\n",
    encoding="utf-8")
PYEOF
VS9=$(python3 "$S/verify_span.py" --run-dir "$RUN9" --all --channel main)
check "闸门摘要报出真实丢弃数（负向对照：按错误的键名前缀累加恒为 0）" \
  "$(echo "$VS9" | jget "['dropped']")" 1
check "被闸门丢光时 count 为 0（与 dropped 一同读才不误判）" \
  "$(echo "$VS9" | jget "['count']")" 0
rm -rf "$RUN9"/work/issues "$RUN9"/work/facts

# ④ 续跑可见性：会话被平台杀掉之后，接手的会话必须看得见已经做了多少。
#    init 会在 manifest 里写一个全零的 stats 占位——以前 stats 只在缺失时才重建，
#    于是这个占位永远命中，续跑的会话看到的永远是 0/0/0。
rm -rf "$RUN9"/work/issues "$RUN9"/work/facts
python3 - "$S" "$RUN9" <<'PYEOF'
import pathlib,sys
sys.path.insert(0, sys.argv[1]); import workspace as ws
run=pathlib.Path(sys.argv[2])
for u in ws.stage_units(run,"review")[:3]:
    u["output"].parent.mkdir(parents=True,exist_ok=True)
    u["output"].write_text("", encoding="utf-8")
PYEOF
ST9=$(python3 "$S/state.py" stats --run-dir "$RUN9")
check "续跑：stats 报出真实的每波进度（不是 init 写下的全零占位）" \
  "$(echo "$ST9" | jget "['stages']['review']['done']")" 3
ge "续跑：stats 同时给出各波总数" "$(echo "$ST9" | jget "['stages']['extract']['total']")" 5
check "续跑：分片级 done 仍按「审查+抽取都做完」算（没被每波进度顶掉）" \
  "$(echo "$ST9" | jget "['stats']['done']")" 0

echo "══ 28. 真实语料上报回来的三类误报 ══"
# 三条都来自用户实跑的反馈，三条都配负向对照——压制方向的 fail-open 比放行方向
# 更危险：它表现为"什么都没查出来"，输出里没有任何痕迹（ADR-035）。

RUN10=$(python3 "$S/workspace.py" init --source "$WORK/big2/big2.docx" --resume new \
        --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
python3 "$S/unpack.py" run --run-dir "$RUN10" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN10" >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN10" >/dev/null

# 造现场：两张平行表（设备1 / 设备2，同名指标不同值）+ 流水式图号 + 一条引用
seed10() {   # $1=facts 文件内容由 python 生成；每次重建 paragraphs 与台账
  python3 - "$RUN10" "$1" <<PYEOF
import json,pathlib,sys
run,mode=pathlib.Path(sys.argv[1]),sys.argv[2]
pp=run/"work"/"paragraphs.jsonl"
base=[json.loads(l) for l in open(pp,encoding="utf-8") if json.loads(l)["pid"][:2]=="p-"
      and not json.loads(l)["pid"].startswith("p-9")]
def mk(pid,text,**kw):
    d={"pid":pid,"index":900000+int(pid[-3:]),"text":text,"is_heading":False,"level":None,
       "style":None,"in_table":False,"table_id":None,"row_idx":None,"cell_idx":None,
       "is_code":False,"is_list":False,"is_quote":False,"heading_path":["附加"],"page_hint":9}
    d.update(kw); return d
extra=[
  mk("p-900001","截至2024年12月底，累计总投入400元",in_table=True,table_id=901,row_idx=1,cell_idx=1),
  mk("p-900002","截至2024年12月底，累计总投入460元",in_table=True,table_id=902,row_idx=1,cell_idx=1),
  mk("p-900003","图33 系统总体架构"),
  mk("p-900004","图33所示的架构分为三层，具体如下。"),
  mk("p-900005","本期端到端时延目标为200ms，实测值为1200ms。"),
  # 负向对照要用的两处：同一张表里的另一格、以及正文里的同一指标。
  # 数值必须真的写在这一段里——ledger 的幻觉闸门会把对不上的事实丢掉
  mk("p-900006","截至2024年12月底，累计总投入460元",in_table=True,table_id=901,row_idx=2,cell_idx=1),
  mk("p-900007","截至2024年12月底，全项目累计总投入460元。"),
]
with open(pp,"w",encoding="utf-8") as f:
    for r in base+extra: f.write(json.dumps(r,ensure_ascii=False)+"\n")
M=lambda pid,v,**kw: dict({"subject":"累计总投入","value":v,"unit":"元","kind":"目标",
                           "source":"表格","pid":pid},**kw)
facts={"metrics":[]}
if mode in ("parallel","same_table","body_vs_table"):
    facts["metrics"]=[M("p-900001","400"), M("p-900002","460")]
    if mode=="same_table":      # 负向对照：两条落在同一张表里 → 必须报
        facts["metrics"][1]=M("p-900006","460")
    if mode=="body_vs_table":   # 负向对照：正文 vs 表格 → 必须报
        facts["metrics"][1]=M("p-900007","460",source="正文")
if mode=="xref_ok":
    facts["xrefs"]=[{"type":"figure","target":"图33","pid":"p-900004"}]
if mode=="xref_missing":
    facts["xrefs"]=[{"type":"figure","target":"图99","pid":"p-900004"}]
if mode=="ghost_pid":
    facts["metrics"]=[{"subject":"端到端时延","value":"200","unit":"ms","kind":"目标","pid":"p-900005"},
                      {"subject":"端到端时延","value":"1200","unit":"ms","kind":"实测","pid":"p-777777"}]
if mode=="ghost_value":
    facts["metrics"]=[{"subject":"端到端时延","value":"200","unit":"ms","kind":"目标","pid":"p-900005"},
                      {"subject":"端到端时延","value":"9900","unit":"ms","kind":"实测","pid":"p-900003"}]
if mode=="grounded":
    facts["metrics"]=[{"subject":"端到端时延","value":"200","unit":"ms","kind":"目标","pid":"p-900005"},
                      {"subject":"端到端时延","value":"1200","unit":"ms","kind":"实测","pid":"p-900005"}]
fd=run/"work"/"facts"; fd.mkdir(parents=True,exist_ok=True)
for old in fd.glob("facts-*.json"): old.unlink()
allkeys={k:[] for k in ("terms","acronyms","entities","metrics","positions","objectives",
  "initiatives","acceptance","dates","versions","roles","xrefs","numbering","commitments",
  "statuses","enumerations","conclusions")}
allkeys.update(facts)
(fd/"facts-0001.json").write_text(json.dumps(allkeys,ensure_ascii=False),encoding="utf-8")
PYEOF
  python3 "$S/ledger.py" rebuild --run-dir "$RUN10" >/dev/null
}
rule_count() {  # $1=规则号 —— 回显该规则检出条数
  python3 "$S/detect_conflicts.py" --run-dir "$RUN10" --force | jget "['by_rule']['$1']"
}

# ① 平行表：表1 是设备1、表2 是设备2，行首都写「累计总投入」——不是前后矛盾
seed10 parallel
check "平行表的同名指标不判冲突（用户实跑误报）" "$(rule_count L06)" 0
seed10 same_table
check "负向对照：同一张表内的同名指标不同值，照常报" "$(rule_count L06)" 1
seed10 body_vs_table
check "负向对照：正文与表格对不上，照常报" "$(rule_count L06)" 1

# ② 流水式图号：文档用「图33」而不是「图3-3」，旧版一个已知编号都认不出来
seed10 xref_ok
check "流水式编号能被识别，引用存在即不报（用户实跑误报）" "$(rule_count L15)" 0
seed10 xref_missing
check "负向对照：引用了不存在的编号，照常报" "$(rule_count L15)" 1
python3 - "$S" <<PYEOF
import sys
sys.path.insert(0, sys.argv[1]); import detect_conflicts as dc
bad=[]
if not dc.caption_label("图33 系统总体架构"): bad.append("「图33 系统总体架构」应判为图题")
if not dc.caption_label("图3-7 系统总体架构"): bad.append("「图3-7 …」应判为图题")
if dc.caption_label("图33所示的架构分为三层"): bad.append("「图33所示…」是引用，不是图题")
k=[dc.label_key(m) for m in dc.LABEL_RE.finditer("见图33 与图 3-7")]
if k != ["图33","图3-7"]: bad.append(f"编号归一化不对：{k}")
if dc.label_series(dc.LABEL_RE.match("图33"))[0] == dc.label_series(dc.LABEL_RE.match("图3-7"))[0]:
    bad.append("两种编号方案被混进了同一序列")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "图题与引用分得开，两种编号方案不混列" "$?" 0

# ③ 事实的幻觉闸门：审查通道一直有，事实通道一条都没有
seed10 grounded
check "落地的事实照常进台账并检出（负向对照的基线）" "$(rule_count L27)" 1
seed10 ghost_pid
LG=$(python3 "$S/ledger.py" rebuild --run-dir "$RUN10")
check "编造的 pid 被丢弃并报数" "$(echo "$LG" | jget "['dropped'].get('bad_pid',0)")" 1
check "丢掉幻觉事实后不再产出无处可查的冲突" "$(rule_count L27)" 0
seed10 ghost_value
LG2=$(python3 "$S/ledger.py" rebuild --run-dir "$RUN10")
check "数值在该段正文里找不到的事实被丢弃并报数" \
  "$(echo "$LG2" | jget "['dropped'].get('value_not_found',0)")" 1
check "丢掉不落地的数值后不再产出对不上的冲突" "$(rule_count L27)" 0
python3 - "$S" <<PYEOF
import sys
sys.path.insert(0, sys.argv[1]); import ledger
bad=[]
# 只在能确定的时候判否：纯文字的指标值必须放行，否则「高/中/低」会被全部误杀
if not ledger.value_grounded("metric","高","散热性能高"): bad.append("非数值指标被误杀")
if not ledger.value_grounded("metric","1,200","上限为 1200 台"): bad.append("千分位没归一")
if not ledger.value_grounded("commitment","9999","随便一句话"): bad.append("非 metric 不该逐字比")
if ledger.value_grounded("metric","460","累计总投入400元"): bad.append("对不上的数值没被拦住")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "闸门只在能确定时判否（非数值/千分位/非 metric 一律放行）" "$?" 0

# ④ 行业术语与通用错词表撞车：术语表要在**生成候选之前**就起作用，
#    而不是等模型答完再由闸门③压制——那时一次裁定调用已经花掉了
python3 - "$S" "$RUN10" <<PYEOF
import json,pathlib,subprocess,sys
S,run=sys.argv[1],pathlib.Path(sys.argv[2])
sys.path.insert(0, S)
import typo_scan as ts
pairs=ts._load_pairs("common-typos.txt")
assert pairs, "错词表为空，构造无效"
wrong,right,_=pairs[0]
term=wrong+"机"                       # 造一个含左串的"行业术语"
gm=run/"work"/"glossary.merged.json"
para={"pid":"p-950001","index":950001,"text":f"本项目采用{term}完成镀膜工序。",
      "is_heading":False,"level":None,"style":None,"in_table":False,"table_id":None,
      "row_idx":None,"cell_idx":None,"is_code":False,"is_list":False,"is_quote":False,
      "heading_path":["附加"],"page_hint":9}
pp=run/"work"/"paragraphs.jsonl"
rows=[json.loads(l) for l in open(pp,encoding="utf-8")]
rows=[r for r in rows if r["pid"]!="p-950001"]+[para]
with open(pp,"w",encoding="utf-8") as f:
    for r in rows: f.write(json.dumps(r,ensure_ascii=False)+"\n")
idx=json.load(open(run/"work"/"chunks"/"index.json",encoding="utf-8"))
idx["chunks"][0].setdefault("review_pids",[]).append("p-950001")
json.dump(idx,open(run/"work"/"chunks"/"index.json","w",encoding="utf-8"),ensure_ascii=False)

def scan(entries):
    gm.write_text(json.dumps({"entries":entries},ensure_ascii=False),encoding="utf-8")
    out=subprocess.run([sys.executable,f"{S}/typo_scan.py","scan","--run-dir",str(run)],
                       capture_output=True,text=True)
    d=json.loads(out.stdout)
    tot=0
    for f in (run/"work"/"typos").glob("typos-????.json"):
        for it in json.load(open(f,encoding="utf-8")).get("candidates",[]):
            if it.get("pid")=="p-950001": tot+=1
    return tot

no_term = scan([])                                            # 负向对照：没有术语表
with_term = scan([{"key":term,"preferred":term,"variants":[],"forbidden":[]}])
banned = scan([{"key":term,"preferred":term,"variants":[],
                "forbidden":[{"form":term}]}])                # 登记为禁用的不受保护
print(f"    含左串「{wrong}」的术语「{term}」：无术语表 {no_term} 条候选，"
      f"登记后 {with_term} 条，登记为禁用写法 {banned} 条")
bad=[]
if no_term < 1: bad.append("构造无效：这个术语本来就不产候选，测不出保护效果")
if with_term != 0: bad.append("术语表里的写法仍被生成为错别字候选")
if banned < 1: bad.append("登记为禁用的写法被误保护了")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "用户术语表在生成候选之前就起作用（负向对照：不登记则照常出候选）" "$?" 0

echo "══ 29. 扫查：证据基为空时不许下结论 ══"
# 这一节不测某一条规则，测的是**一整类缺陷**：
#   「我在集合 S 里没找到 X」——当 S 是空的，这句话说明的是"我一个都没认出来"，
#   不是"文档里没有 X"。此时每一条 X 都成立，表现为满屏误报而看不出根因。
# L15 的流水式图号（ADR-047）就是这么误报的；这一节把这类缺陷变成可枚举的。
#
# **加一条「声称某物不存在」的规则时，必须在这里补一行。** 否则它迟早重演。

# 用自己的源文件，不蹭前面小节的残留——这一节要能单独看懂、单独重跑
mkdir -p "$WORK/ev"
cp "$F/sample-basic.docx" "$WORK/ev/ev.docx"
RUN11=$(python3 "$S/workspace.py" init --source "$WORK/ev/ev.docx" \
        --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
[ -f "$RUN11/work/source-copy.docx" ] || bad "第 29 节构造失败：init 没有复制源文档"
python3 "$S/unpack.py" run --run-dir "$RUN11" >/dev/null
python3 "$S/extract.py" --run-dir "$RUN11" >/dev/null
python3 "$S/chunk.py" --run-dir "$RUN11" >/dev/null

# 把语料换成「一个可识别结构都没有」：无编号标题、无图表编号、无附录
python3 - "$RUN11" <<PYEOF
import json,pathlib,sys
run=pathlib.Path(sys.argv[1])
paras=[{"pid":f"p-{i:06d}","index":i,"text":t,"is_heading":False,"level":None,"style":None,
        "in_table":False,"table_id":None,"row_idx":None,"cell_idx":None,"is_code":False,
        "is_list":False,"is_quote":False,"heading_path":["概述"],"page_hint":1}
       for i,t in enumerate(["系统概述","本系统由接入层与数据层组成，各层通过标准接口交互。",
                             "详见前文相关说明。"],1)]
with open(run/"work"/"paragraphs.jsonl","w",encoding="utf-8") as f:
    for r in paras: f.write(json.dumps(r,ensure_ascii=False)+"\n")
(run/"work"/"headings.json").write_text(json.dumps({"headings":[]},ensure_ascii=False),
                                        encoding="utf-8")
PYEOF

seed11() {  # $1=facts JSON（单行）
  python3 - "$RUN11" "$1" <<PYEOF
import json,pathlib,sys
run=pathlib.Path(sys.argv[1])
allk={k:[] for k in ("terms","acronyms","entities","metrics","positions","objectives",
 "initiatives","acceptance","dates","versions","roles","xrefs","numbering","commitments",
 "statuses","enumerations","conclusions")}
allk.update(json.loads(sys.argv[2]))
fd=run/"work"/"facts"; fd.mkdir(parents=True,exist_ok=True)
for old in fd.glob("facts-*.json"): old.unlink()
(fd/"facts-0001.json").write_text(json.dumps(allk,ensure_ascii=False),encoding="utf-8")
PYEOF
  python3 "$S/ledger.py" rebuild --run-dir "$RUN11" >/dev/null
}
fired11() { python3 "$S/detect_conflicts.py" --run-dir "$RUN11" --force | jget "['by_rule']['$1']"; }

# L15 三个分支：图 / 章节 / 附录。三个分支同一条判据，
# 上一轮只落实了两个（ADR-047 补图与附录时漏了章节），所以三个都要断言。
seed11 '{"xrefs":[{"type":"figure","target":"图33","pid":"p-000002"}]}'
check "L15/图：一个图号都没认出来时不报" "$(fired11 L15)" 0
seed11 '{"xrefs":[{"type":"section","target":"9.9","pid":"p-000002"}]}'
check "L15/章节：一个带编号的标题都没有时不报" "$(fired11 L15)" 0
seed11 '{"xrefs":[{"type":"appendix","target":"附录Z","pid":"p-000002"}]}'
check "L15/附录：一条附录都没有时不报" "$(fired11 L15)" 0

# L29/L30：覆盖性规则。整类证据为空 = 这一类没抽出来，不是"每一条都缺"
seed11 '{"objectives":[{"obj_id":"O1","statement":"提升良率","pid":"p-000002"}],"initiatives":[{"init_id":"I1","statement":"建平台","serves_objective":null,"pid":"p-000002"}]}'
check "L29：全篇没有一条对应关系时不报（prompt 本就要求宁可留 null）" "$(fired11 L29)" 0
seed11 '{"initiatives":[{"init_id":"I1","statement":"建平台","serves_objective":null,"pid":"p-000002"}]}'
check "L30：全篇一条验收指标都没抽到时不报" "$(fired11 L30)" 0

# 负向对照：证据基一旦非空，这些规则必须照常开口，否则这道守卫就成了静音开关
python3 - "$RUN11" <<PYEOF
import json,pathlib,sys
run=pathlib.Path(sys.argv[1])
rows=[json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")]
rows.append({"pid":"p-000004","index":4,"text":"图1 系统总体架构","is_heading":False,
  "level":None,"style":None,"in_table":False,"table_id":None,"row_idx":None,"cell_idx":None,
  "is_code":False,"is_list":False,"is_quote":False,"heading_path":["概述"],"page_hint":1})
rows.append({"pid":"p-000005","index":5,"text":"1.1 接入层","is_heading":True,"level":2,
  "style":None,"in_table":False,"table_id":None,"row_idx":None,"cell_idx":None,
  "is_code":False,"is_list":False,"is_quote":False,"heading_path":["概述"],"page_hint":1})
rows.append({"pid":"p-000006","index":6,"text":"附录A 缩略语表","is_heading":False,"level":None,
  "style":None,"in_table":False,"table_id":None,"row_idx":None,"cell_idx":None,
  "is_code":False,"is_list":False,"is_quote":False,"heading_path":["概述"],"page_hint":1})
with open(run/"work"/"paragraphs.jsonl","w",encoding="utf-8") as f:
    for r in rows: f.write(json.dumps(r,ensure_ascii=False)+"\n")
(run/"work"/"headings.json").write_text(json.dumps(
    {"headings":[{"pid":"p-000005","text":"1.1 接入层","level":2}]},ensure_ascii=False),
    encoding="utf-8")
PYEOF
seed11 '{"xrefs":[{"type":"figure","target":"图33","pid":"p-000002"},{"type":"section","target":"9.9","pid":"p-000002"},{"type":"appendix","target":"附录Z","pid":"p-000002"}]}'
check "负向对照：证据基非空时三个分支照常报（守卫不是静音开关）" "$(fired11 L15)" 3
seed11 '{"objectives":[{"obj_id":"O1","statement":"提升良率","pid":"p-000002"},{"obj_id":"O2","statement":"降本","pid":"p-000002"}],"initiatives":[{"init_id":"I1","statement":"建平台","serves_objective":"O1","pid":"p-000002"}],"acceptance":[{"target":"I9","criterion":"接入率≥80%","pid":"p-000002"}]}'
check "负向对照：有对应关系时 L29 照常报没被认领的目标" "$(fired11 L29)" 1
check "负向对照：有验收指标时 L30 照常报没有验收的举措" "$(fired11 L30)" 1

echo "══ 30. 诊断包：可外发，且不含正文 ══"
# 真实语料是优化这个技能的唯一有效输入，而语料通常不能外传。
# 诊断包抽的是「排障需要、但不泄露内容」的那一层。
# **不含正文这件事必须是可验证的，不能是承诺**——所以自检拿 paragraphs.jsonl
# 逐条去撞，且这里配了负向对照：塞一句真正文进去，自检必须抓到。

DG=$(python3 "$S/diagnose.py" --run-dir "$RUN2")
check "诊断包生成成功且通过泄漏自检" "$(echo "$DG" | jget "['leak_check']")" passed
python3 - "$S" "$RUN2" "$(echo "$DG" | jget "['path']")" <<PYEOF
import json,pathlib,sys
sys.path.insert(0, sys.argv[1])
import diagnose as dg
from workspace import load_run_config
run=pathlib.Path(sys.argv[2]); bundle=json.load(open(sys.argv[3],encoding="utf-8"))
bad=[]
# ① 正向：包里该有的几块都在，且都是数字不是文本
for sec in ("structure","workload","funnel","facts","conflicts","typos"):
    if sec not in bundle: bad.append(f"缺少 {sec}")
st=bundle.get("structure",{})
for k in ("paragraphs","tables","parallel_tables","figure_table_labels","numbered_headings"):
    if k not in st: bad.append(f"structure 缺少 {k}")
# ② 负向对照：塞一句真正文，自检必须抓到。抓不到就说明这道自检是摆设
para=json.loads(open(run/"work"/"paragraphs.jsonl",encoding="utf-8").readline())
probe=dict(bundle); probe["_leak"]=para["text"]
if not dg._assert_no_text(probe, run):
    bad.append("负向对照：塞进正文后自检没有抓到")
# ③ 正常包必须干净
if dg._assert_no_text(bundle, run):
    bad.append("正常诊断包里出现了正文")
for b in bad[:3]: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "诊断包结构完整；负向对照：塞进正文即被自检抓到" "$?" 0

# 编号方案与平行表是两条最贵误报的直接指纹（ADR-047 ①②），必须报出来
python3 - "$(echo "$DG" | jget "['path']")" <<PYEOF
import json,sys
st=json.load(open(sys.argv[1],encoding="utf-8"))["structure"]
lab=st["figure_table_labels"]
bad=[]
if set(lab) != {"chapter_scheme","flat_scheme"}: bad.append(f"编号方案指纹不对：{lab}")
if not isinstance(st["parallel_tables"], int): bad.append("平行表计数缺失")
print(f"    指纹：编号 章-序 {lab['chapter_scheme']} / 流水 {lab['flat_scheme']}，"
      f"疑似平行表 {st['parallel_tables']} 张")
for b in bad: print("   ", b)
sys.exit(1 if bad else 0)
PYEOF
check "诊断包带出编号方案与平行表指纹（两类最贵误报的来源）" "$?" 0

echo "══ 31. 落笔跨度与页码：现场反馈的两条 ══"
# ① 一个两字的错字，不该落成「删掉一整句、再插入一整句」的修订，
#    批注也不该圈住整段。跨度撑宽是为了消歧（唯一性），不该连编辑范围一起撑宽——
#    有了「段内第几处」，跨度就能缩到错字本身。
# ② 逐段页码在有分页标记时是精确的，没有标记时是线性推算。
#    把推算写成确定的页码，评审人照着翻过去找不到东西，只会认为这条是误报。

python3 - "$WORK" <<PYEOF
import sys
try:
    from docx import Document
except ImportError:
    sys.exit(9)
d=Document(); d.add_heading("1 总则", level=1)
# 三句一段：错字在中间那句。批注该圈中间这句，不是整段、也不只是那两个字
d.add_paragraph("本系统采用分层架构，接入层负责协议转换与鉴权。"
                "平台已完成布署并通过验收，运行状态良好。"
                "后续按季度评估容量并输出报告。")
# 同一个错字在同段出现两次：靠序号消歧，两处都该能落笔
d.add_paragraph("一期布署完成后进入试运行。二期布署计划于下季度启动。")
import pathlib; pathlib.Path(sys.argv[1],"span").mkdir(parents=True,exist_ok=True)
d.save(str(pathlib.Path(sys.argv[1],"span","span.docx")))
PYEOF
if [ $? -eq 9 ]; then
  echo "  （跳过：本机无 python-docx，无法生成本节 fixture）"
else
RUN12=$(python3 "$S/workspace.py" init --source "$WORK/span/span.docx" \
        --output-dir "$DELIVER" --temp-dir "$TEMP" | jget "['run_dir']")
python3 "$S/workspace.py" lease acquire --doc-dir "$(dirname "$RUN12")" --session sp \
        --runid "$(basename "$RUN12")" --stage pass-1 >/dev/null
for c in "unpack.py run" "extract.py" "chunk.py" "typo_scan.py scan"; do
  python3 $S/$c --run-dir "$RUN12" >/dev/null
done

# 候选必须带段内序号，且同段两处各有各的序号
python3 - "$RUN12" <<PYEOF
import json,pathlib,sys
run=pathlib.Path(sys.argv[1]); t=run/"work"/"typos"
cands=[c for f in t.glob("typos-????.json")
       for c in json.load(open(f,encoding="utf-8")).get("candidates",[])]
bad=[]
if not cands: bad.append("没有候选，构造无效")
if any("occurrence" not in c for c in cands): bad.append("候选没有带段内序号")
byp={}
for c in cands: byp.setdefault(c["pid"],[]).append(c["occurrence"])
dup=[v for v in byp.values() if len(v)>1]
if not dup: bad.append("构造无效：没有同段两处的情形")
elif sorted(dup[0])!=[0,1]: bad.append(f"同段两处的序号不对：{dup}")
# 全部裁定为 B
for f in sorted(t.glob("typos-g*.json")):
    items=json.load(open(f,encoding="utf-8"))["items"]
    (t/f"{f.stem}.verdicts.jsonl").write_text("".join(
        json.dumps({"tid":i["tid"],"verdict":"B"},ensure_ascii=False)+"\n" for i in items),
        encoding="utf-8")
for b in bad: print("   ",b)
sys.exit(1 if bad else 0)
PYEOF
check "错别字候选带段内序号（同段两处各有各的序号）" "$?" 0

python3 "$S/typo_scan.py" merge --run-dir "$RUN12" >/dev/null
python3 - "$RUN12" <<PYEOF
import json,pathlib,sys
rows=[json.loads(l) for l in
      open(pathlib.Path(sys.argv[1],"work","issues","issues-0001.typos.jsonl"),encoding="utf-8")]
bad=[]
if not rows: bad.append("merge 没有产出")
for r in rows:
    if len(r["original_text"])>4:
        bad.append(f"落笔跨度不是错字本身：{r['original_text']!r}（{len(r['original_text'])} 字）")
    if len(r["suggested_text"])!=len(r["original_text"]):
        bad.append(f"建议与原文长度不一致：{r['suggested_text']!r}")
print(f"    落笔跨度：{[r['original_text'] for r in rows]} → {[r['suggested_text'] for r in rows]}")
for b in bad[:3]: print("   ",b)
sys.exit(1 if bad else 0)
PYEOF
check "落笔跨度就是错字本身（不再撑宽到唯一性窗口）" "$?" 0

python3 "$S/verify_span.py" --run-dir "$RUN12" --all --channel typos >/dev/null
python3 "$S/filter_neverflag.py" --run-dir "$RUN12" --all --channel typos >/dev/null
KEPT=$(python3 - "$RUN12" <<PYEOF
import json,pathlib,sys
p=pathlib.Path(sys.argv[1],"work","issues","issues-0001.typos.jsonl")
print(sum(1 for _ in open(p,encoding="utf-8")))
PYEOF
)
ge "两字跨度过得了闸门②（A1 的下限独立于 min_span_chars）" "$KEPT" 2

python3 "$S/verify_pass2.py" build --run-dir "$RUN12" >/dev/null
python3 - "$RUN12" <<PYEOF
import json,pathlib,sys
run=pathlib.Path(sys.argv[1]); vd=run/"work"/"verify"
key=json.loads((vd/"pass2-primary.key.json").read_text(encoding="utf-8"))["key"]
for f in sorted(vd.glob("pass2-primary.v[0-9][0-9].json")):
    items=json.loads(f.read_text(encoding="utf-8"))["items"]
    (vd/f"{f.stem}.verdicts.jsonl").write_text("".join(
        json.dumps({"id":i["id"],"answer":key[i["id"]]["orig_side"]},ensure_ascii=False)+"\n"
        for i in items),encoding="utf-8")
PYEOF
python3 "$S/verify_pass2.py" merge --run-dir "$RUN12" >/dev/null
python3 "$S/apply_revisions.py" plan  --run-dir "$RUN12" --session sp --generation 1 >/dev/null
AP=$(python3 "$S/apply_revisions.py" apply --run-dir "$RUN12" --session sp --generation 1)
ge "同段两处各自落笔（靠序号消歧，不再整条拒绝）" "$(echo "$AP" | jget "['applied']")" 2
check "落笔没有失败项" "$(echo "$AP" | jget "['failed']")" 0

python3 "$S/apply_comments.py" plan  --run-dir "$RUN12" --session sp --generation 1 >/dev/null
python3 "$S/apply_comments.py" apply --run-dir "$RUN12" --session sp --generation 1 >/dev/null
python3 - "$S" "$RUN12" <<PYEOF
import pathlib,sys
sys.path.insert(0, sys.argv[1])
from lxml import etree
import ooxml as ox
root=etree.parse(str(pathlib.Path(sys.argv[2],"work","unpacked","word","document.xml"))).getroot()
paras={}
cov=ox.comment_coverage(root)
bad=[]
three=[c for c in cov.values() if "分层架构" in c["reject"] or "运行状态良好" in c["reject"]]
if not three: bad.append("没找到三句段落上的批注")
for c in three:
    r=c["reject"].strip()
    if "分层架构" in r or "按季度评估" in r:
        bad.append(f"批注圈到了同段的其它句子：{r!r}")
    if "布署" not in r: bad.append(f"批注没圈住被改的那句：{r!r}")
    if len(r) < 8: bad.append(f"批注只圈住了错字本身，看不出改的是哪句：{r!r}")
    print(f"    批注范围：{r!r}")
for b in bad[:3]: print("   ",b)
sys.exit(1 if bad else 0)
PYEOF
check "批注圈住的是被改的那一句（不是整段，也不只是那两个字）" "$?" 0
VD=$(python3 "$S/validate_docx.py" --run-dir "$RUN12")
check "回写四项校验通过" "$(echo "$VD" | jget "['pass']")" True
fi

# ② 页码：有渲染分页标记时逐段精确；没有时是线性推算，措辞必须带「约」
python3 - "$S" <<PYEOF
import sys
sys.path.insert(0, sys.argv[1])
from _common import page_ref
bad=[]
if page_ref({"page_hint":36,"page_estimated":False}) != "第 36 页": bad.append("精确页码措辞不对")
if page_ref({"page_hint":36,"page_estimated":True}) != "约第 36 页": bad.append("估算页码没带「约」")
if page_ref({"page_hint":36}) != "约第 36 页": bad.append("缺字段时未按估算处理（默认必须保守）")
for b in bad: print("   ",b)
sys.exit(1 if bad else 0)
PYEOF
check "估算出来的页码不写成确定的页码" "$?" 0

python3 - "$S" "$RUN2" <<PYEOF
import json,pathlib,shutil,subprocess,sys
S,run=sys.argv[1],pathlib.Path(sys.argv[2])
from lxml import etree
W="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
doc=run/"work"/"unpacked"/"word"/"document.xml"
backup=doc.read_bytes()
t=etree.parse(str(doc)); r=t.getroot()
ps=list(r.iter(f"{{{W}}}p")); n=0
for i,p in enumerate(ps):
    if i and i%3==0:
        run_el=p.find(f"{{{W}}}r")
        if run_el is not None:
            run_el.insert(0, etree.Element(f"{{{W}}}lastRenderedPageBreak")); n+=1
t.write(str(doc), xml_declaration=True, encoding="UTF-8", standalone=True)
out=json.loads(subprocess.run([sys.executable,f"{S}/extract.py","--run-dir",str(run)],
                              capture_output=True,text=True).stdout)
rows=[json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")]
bad=[]
if out["page_density"].get("page_hint_source")!="rendered_breaks":
    bad.append(f"有分页标记却没用：{out['page_density']}")
if any(x["page_estimated"] for x in rows): bad.append("按分页标记算出来的页码仍被标为估算")
if max(x["page_hint"] for x in rows) < 2: bad.append("页码没有随分页标记递增")
print(f"    注入 {n} 个渲染分页标记 → 来源 {out['page_density'].get('page_hint_source')}，"
      f"最大页 {max(x['page_hint'] for x in rows)}")
doc.write_bytes(backup)
subprocess.run([sys.executable,f"{S}/extract.py","--run-dir",str(run)],capture_output=True)
rows2=[json.loads(l) for l in open(run/"work"/"paragraphs.jsonl",encoding="utf-8")]
# 负向对照：把标记去掉，必须退回线性推算并标为估算
if not all(x["page_estimated"] for x in rows2):
    bad.append("负向对照：没有分页标记时仍声称页码精确")
for b in bad[:3]: print("   ",b)
sys.exit(1 if bad else 0)
PYEOF
check "有分页标记就逐段精确；负向对照：没有标记时退回推算并标为估算" "$?" 0

echo
printf '通过 \033[32m%d\033[0m，失败 \033[31m%d\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
