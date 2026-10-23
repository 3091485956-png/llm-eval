# -*- coding: utf-8 -*-
"""bench2jsonl.py — 官方基准 → questions/*.jsonl 题库转换器
数据源(全部公开渠道, 尊重原文不翻译):
  CLUE(afqmc/c3/cmrc2018) / LiveIdeaBench / IFBench / GSM8K+MATH-500(CoT Hub收录)
  jsonschemabench / LongBench(vcsum/qmsum/passage_retrieval*/2wikimqa)
  BeIR-scifact / MT-Bench human / HaluEval / OmniDocBench(多模态支线)
用法: python bench2jsonl.py          # 生成到 questions/ (旧题库归档到 questions_archive/)
      python bench2jsonl.py --keep   # 不归档, 直接覆盖同名维度文件
"""
import argparse
import ast
import json
import random
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

BASE = Path(__file__).resolve().parent
CACHE = BASE / "benchmarks_cache"
QDIR = BASE / "questions"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # 被重定向到StringIO等场景
random.seed(20261022)

DIMS_OFFICIAL = ["语言理解", "信息提取", "创造力", "指令遵循", "复杂推理", "约束处理",
                 "结构化输出", "长文本组织", "上下文利用", "信息检索", "评估", "错误识别",
                 "文档解析"]


def jl(path, n=0):
    lines = open(path, encoding="utf-8").readlines()
    return [json.loads(x) for x in (lines[:n] if n else lines)]


def parse_refs(s):
    """LongBench answers 字段容错解析: 标准python-repr列表 → list; 解析失败按单条处理。"""
    if isinstance(s, list):
        return s
    s = str(s).strip()
    if s.startswith("["):
        try:
            import ast as _ast
            v = _ast.literal_eval(s)
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception:
            pass
        inner = s[1:-1].strip()
        if inner.startswith("'") and inner.endswith("'"):
            inner = inner[1:-1]
        return [inner.replace("\\'", "'")]
    return [s]


def write_dim(dim, qs):
    for i, q in enumerate(qs, 1):
        q["id"] = f"{dim_code(dim)}{i:02d}"
        q.setdefault("dim", dim)
        q.setdefault("score", 10)
        q.setdefault("type", "简答")
        q.setdefault("diff", 3)
        q.setdefault("contam", "高·公开原题")
    QDIR.mkdir(exist_ok=True)
    with open(QDIR / f"{file_code(dim)}.jsonl", "w", encoding="utf-8") as f:
        for q in qs:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    print(f"{dim}: {len(qs)} 题 → questions/{file_code(dim)}.jsonl")


_CODE = {"语言理解": ("LU", "lu"), "信息提取": ("IE", "ie"), "创造力": ("CR", "cr"),
         "指令遵循": ("INS", "ins"), "复杂推理": ("REA", "rea"), "约束处理": ("CON", "cons"),
         "结构化输出": ("SO", "so"), "长文本组织": ("LT", "lt"), "上下文利用": ("CU", "cu"),
         "信息检索": ("IR", "ir"), "评估": ("EV", "ev"), "错误识别": ("ER", "er"),
         "文档解析": ("DP", "dp")}


def dim_code(d):
    return _CODE[d][0]


def file_code(d):
    return _CODE[d][1]


# ---------------------------------------------------------------- 语言理解: CLUE
def conv_lu():
    qs = []
    # afqmc: 语义相似二分类(官方 label 1=相似)
    rows = pq.read_table(CACHE / "clue_afqmc_validation.parquet").to_pylist()
    for r in random.sample(rows, 12):
        label = int(r["label"])
        qs.append({
            "sub": "afqmc 语义相似度", "src": "CLUE", "type": "单选", "diff": 3,
            "src_detail": "CLUE/afqmc validation 官方原题(label官方)",
            "messages": [{"role": "user", "content":
                f"判断下面两句话的语义是否相近。\n句子1：{r['sentence1']}\n句子2：{r['sentence2']}\n"
                "A．语义相近\nB．语义不相近\n请直接回答选项字母。"}],
            "ref": "A(相近)" if label == 1 else "B(不相近)",
            "points": "答对得满分",
            "scoring": {"mode": "mc", "answer": "A" if label == 1 else "B", "letters": "AB"},
        })
    # c3: 对话阅读理解(官方 choice/answer)
    rows = pq.read_table(CACHE / "clue_c3_validation.parquet").to_pylist()
    picked = random.sample(rows, 8)
    for r in picked:
        choices = ast.literal_eval(r["choice"]) if isinstance(r["choice"], str) else r["choice"]
        ctx = ast.literal_eval(r["context"]) if isinstance(r["context"], str) else r["context"]
        letters = "ABCD"[: len(choices)]
        ans = letters[choices.index(r["answer"])]
        opts = "\n".join(f"{letters[i]}．{c}" for i, c in enumerate(choices))
        qs.append({
            "sub": "c3 对话阅读理解", "src": "CLUE", "type": "单选", "diff": 3,
            "src_detail": "CLUE/c3 validation 官方原题",
            "messages": [{"role": "user", "content":
                "阅读对话，回答问题。\n" + "\n".join(ctx) +
                f"\n问题：{r['question']}\n{opts}\n请直接回答选项字母。"}],
            "ref": f"{ans}（{r['answer']}）",
            "points": "答对得满分",
            "scoring": {"mode": "mc", "answer": ans, "letters": letters},
        })
    return qs


# ---------------------------------------------------------------- 信息提取: cmrc2018
def conv_ie():
    rows = pq.read_table(CACHE / "clue_cmrc2018_validation.parquet").to_pylist()
    picked = random.sample(rows, 20)
    qs = []
    for r in picked:
        answers = ast.literal_eval(r["answers"]) if isinstance(r["answers"], str) else r["answers"]
        if not isinstance(answers, dict):
            answers = {"text": [str(answers)]}
        refs = list(dict.fromkeys(answers["text"]))
        qs.append({
            "sub": "cmrc2018 抽取式阅读理解", "src": "CLUE", "type": "简答", "diff": 3,
            "src_detail": "CLUE/cmrc2018 validation 官方原题; 按官方字符级F1判分",
            "messages": [{"role": "user", "content":
                f"阅读以下文章，回答问题，直接给出答案片段。\n文章：{r['context']}\n问题：{r['question']}"}],
            "ref": "；".join(refs),
            "points": "字符级F1×10（CLUE官方指标）",
            "scoring": {"mode": "f1", "refs": refs},
        })
    return qs


# ---------------------------------------------------------------- 创造力: LiveIdeaBench
def conv_cr():
    probs = [p.strip() for p in open(CACHE / "liveidea_problems.txt", encoding="utf-8")
             .read().split("\n===\n") if p.strip()]
    qs = []
    for kw in probs[:20]:
        qs.append({
            "sub": "科学发散思维(最小语境)", "src": "LiveIdeaBench", "type": "生成",
            "diff": 4, "runs": 2, "max_tokens": 2048,
            "src_detail": "LiveIdeaBench v1 官方关键词题面(Nature Comm., MIT); "
                          "评审沿用官方 orig/feas/flue 三维度",
            "messages": [{"role": "user", "content":
                f"Scientific keyword: {kw}\n"
                "Propose one novel and feasible scientific research idea related to the keyword "
                "above, in English, within 150 words. State the idea, why it is novel, and why "
                "it is feasible."}],
            "ref": "无固定答案（官方LLM评审）",
            "points": "新颖性(orig)4分/可行性(feas)3分/流畅性(flue)3分——LiveIdeaBench官方维度",
            "scoring": {"mode": "llm"},
        })
    return qs


# ---------------------------------------------------------------- 指令遵循/约束处理: IFBench
def conv_ins_con():
    """IFBench 官方原题 + 官方校验器(scoring.mode=ifbench, scorer 内调用 ifbench_lib 原版代码)。"""
    rows = jl(CACHE / "ifbench_test.jsonl")
    single, multi = [], []
    for r in rows:
        ids = r["instruction_id_list"]
        item = {
            "sub": "官方约束指令", "src": "IFBench", "type": "生成",
            "src_detail": f"IFBench test 官方原题; 约束: {','.join(ids)}; "
                          "官方 ifbench/instructions.py 校验器逐条判分(Apache-2.0)",
            "messages": [{"role": "user", "content": r["prompt"]}],
            "ref": f"约束: {','.join(ids)}",
            "points": "官方校验器逐条通过, 通过率×10",
            "scoring": {"mode": "ifbench", "instructions": list(ids),
                        "kwargs": [dict(k) for k in r["kwargs"]]},
        }
        if len(ids) == 1:
            single.append(item)
        else:
            multi.append(item)
    random.shuffle(single)
    random.shuffle(multi)
    return single[:20], multi[:20]


# ---------------------------------------------------------------- 复杂推理: GSM8K + MATH-500
def conv_rea():
    qs = []
    for r in random.sample(jl(CACHE / "gsm8k_test.jsonl"), 10):
        ans = r["answer"].split("####")[-1].strip().replace(",", "")
        qs.append({
            "sub": "GSM8K 多步算术", "src": "CoT Hub收录:GSM8K", "type": "简答", "diff": 3,
            "src_detail": "GSM8K test 官方原题(MIT, openai/grade-school-math)",
            "messages": [{"role": "user", "content": r["question"] +
                          "\nReason step by step, then output the final numeric answer on the last line as \"#### <answer>\"."}],
            "ref": ans, "points": "最终数值正确得满分",
            "scoring": {"mode": "numeric", "value": float(ans)},
        })
    math_rows = [x for x in jl(CACHE / "math500.jsonl") if int(x.get("level", 3)) >= 3]
    for r in random.sample(math_rows, 10):
        qs.append({
            "sub": f"MATH-500({r.get('subject', '')} L{r.get('level')})", "src": "CoT Hub收录:MATH",
            "type": "简答", "diff": 5,
            "src_detail": "MATH-500 test 官方原题(MIT, HuggingFaceH4/MATH-500)",
            "messages": [{"role": "user", "content": r["problem"] +
                          "\nSolve it, then output the final answer in \\boxed{}."}],
            "ref": r["answer"], "points": "LaTeX归一化后与官方答案一致得满分",
            "scoring": {"mode": "mathexact", "answer": r["answer"]},
        })
    return qs


# ---------------------------------------------------------------- 结构化输出: jsonschemabench
def conv_so():
    files = sorted((CACHE / "data" / "Github_easy").glob("*.json")) + \
            sorted((CACHE / "data" / "Github_medium").glob("*.json"))
    files = [f for f in files if f.stat().st_size < 1600][:400]
    qs = []
    for f in random.sample(files, 20):
        schema = json.load(open(f, encoding="utf-8"))
        qs.append({
            "sub": "按Schema生成JSON实例", "src": "jsonschemabench", "type": "转换",
            "diff": 4, "src_detail": f"jsonschemabench 官方Schema({f.parent.name}/{f.stem})",
            "messages": [{"role": "user", "content":
                "Generate one valid JSON instance that conforms to the following JSON Schema. "
                "Output only the JSON instance.\n```json\n" +
                json.dumps(schema, ensure_ascii=False, indent=1) + "\n```"}],
            "ref": "符合Schema即满分(官方程序校验)",
            "points": "jsonschema库校验通过=10分, 解析失败/校验失败=0分",
            "scoring": {"mode": "schemavalid", "schema": schema},
        })
    return qs


# ---------------------------------------------------------------- 长文本组织: LongBench 摘要
def conv_lt():
    qs = []
    rows = random.sample(jl(CACHE / "longbench_v1/data/vcsum.jsonl"), 10)
    for r in rows:
        qs.append({
            "sub": "vcsum 会议纪要摘要", "src": "LongBench", "type": "简答", "diff": 4,
            "src_detail": "LongBench/vcsum 官方原题(中文字符级F1)",
            "messages": [{"role": "user", "content": "请对给定会议记录进行摘要。\n" + r["context"]}],
            "ref": str(r["answers"])[:120],
            "points": "与官方参考答案的字符级F1×10",
            "scoring": {"mode": "f1", "refs": parse_refs(r["answers"])},
        })
    rows = random.sample(jl(CACHE / "longbench_v1/data/qmsum.jsonl"), 10)
    for r in rows:
        refs = parse_refs(r["answers"])
        qs.append({
            "sub": "qmsum 查询式会议摘要", "src": "LongBench", "type": "简答", "diff": 4,
            "src_detail": "LongBench/qmsum 官方原题(英文ROUGE-F1口径, 采用字符级F1近似)",
            "messages": [{"role": "user", "content": r["input"] + "\n" + r["context"]}],
            "ref": str(refs)[:120],
            "points": "字符级F1×10",
            "scoring": {"mode": "f1", "refs": refs},
        })
    return qs


# ---------------------------------------------------------------- 上下文利用: LongBench
def conv_cu():
    qs = []
    # passage_retrieval: input=主题, answers=段落N
    for path, n, lang in (("passage_retrieval_zh", 7, "zh"), ("passage_retrieval_en", 7, "en")):
        for r in random.sample(jl(CACHE / f"longbench_v1/data/{path}.jsonl"), n):
            refs = parse_refs(r["answers"])
            instr = ("下列段落来自同一文章，请判断哪一段包含指定主题的介绍，"
                     "回答段落编号（如：段落27）。主题：" if lang == "zh" else
                     "The following paragraphs come from the same article. Which paragraph "
                     "introduces the specified topic? Answer with the paragraph number "
                     "(e.g., Paragraph 27). Topic: ")
            tail = "请只回答段落编号。" if lang == "zh" else "Answer with the paragraph number only."
            qs.append({
                "sub": f"{path} 长文段落定位", "src": "LongBench", "type": "简答", "diff": 4,
                "src_detail": f"LongBench/{path} 官方原题({'中文' if lang == 'zh' else '英文'})",
                "messages": [{"role": "user", "content": instr + r["input"] + "\n\n" + r["context"] + "\n" + tail}],
                "ref": refs[0], "points": "定位正确得满分",
                "scoring": {"mode": "exact", "answers": [refs[0], refs[0].replace("段落", "").replace("Paragraph", "").strip()]},
            })
    for r in random.sample(jl(CACHE / "longbench_v1/data/2wikimqa.jsonl"), 6):
        refs = parse_refs(r["answers"])
        qs.append({
            "sub": "2wikimqa 多跳问答", "src": "LongBench", "type": "简答", "diff": 4,
            "src_detail": "LongBench/2wikimqa 官方原题(英文, F1)",
            "messages": [{"role": "user", "content": r["input"] + "\n" + r["context"]}],
            "ref": "；".join(refs)[:100], "points": "字符级F1×10",
            "scoring": {"mode": "f1", "refs": refs},
        })
    return qs


# ---------------------------------------------------------------- 信息检索: scifact
def conv_ir():
    corpus = {r["_id"]: r for r in jl(CACHE / "scifact_full/scifact/corpus.jsonl")}
    queries = {r["_id"]: r["text"] for r in jl(CACHE / "scifact_full/scifact/queries.jsonl")}
    qrels = {}
    for line in open(CACHE / "scifact_full/scifact/qrels/test.tsv", encoding="utf-8").readlines()[1:]:
        qid, cid, s = line.strip().split("\t")
        if int(s) >= 1:
            qrels.setdefault(qid, []).append(cid)
    pool = [q for q in qrels if len(qrels[q]) == 1 and q in queries and qrels[q][0] in corpus]
    qs = []
    for qid in random.sample(pool, 20):
        gold = qrels[qid][0]
        distract_pool = [c for c in corpus if c != gold and corpus[c].get("text")]
        docs = random.sample(distract_pool, 4) + [gold]
        random.shuffle(docs)
        ans_letter = "ABCDE"[docs.index(gold)]
        doc_text = "\n\n".join(
            f"Document {i + 1}: {corpus[c]['title']}. {corpus[c]['text'][:260]}"
            for i, c in enumerate(docs))
        qs.append({
            "sub": "证据文档定位", "src": "BEIR-scifact", "type": "单选", "diff": 4,
            "src_detail": "BEIR/scifact 官方claim与corpus构造(qrels官方标注)",
            "messages": [{"role": "user", "content":
                "Claim: " + queries[qid] +
                "\n\nWhich of the following documents SUPPORTS the claim above?\n" + doc_text +
                "\nAnswer with the document number (1-5) only."}],
            "ref": f"Document {docs.index(gold) + 1}",
            "points": "定位正确得满分",
            "scoring": {"mode": "exact", "answers": [str(docs.index(gold) + 1), f"Document {docs.index(gold) + 1}"]},
        })
    return qs


# ---------------------------------------------------------------- 评估: MT-Bench human
def conv_ev():
    t = pq.read_table(CACHE / "mtbench_human.parquet").to_pylist()
    rows = [r for r in t if r.get("winner") in ("model_a", "model_b")
            and r.get("turn") in ("1", 1)]
    qs = []
    for r in random.sample(rows, 20):
        ca = ast.literal_eval(r["conversation_a"]) if isinstance(r["conversation_a"], str) else r["conversation_a"]
        cb = ast.literal_eval(r["conversation_b"]) if isinstance(r["conversation_b"], str) else r["conversation_b"]
        ans_a = next((m["content"] for m in reversed(ca) if m["role"] == "assistant"), None)
        ans_b = next((m["content"] for m in reversed(cb) if m["role"] == "assistant"), None)
        if not ans_a or not ans_b:
            continue
        win = "A" if r["winner"] == "model_a" else "B"
        qs.append({
            "sub": "回答优劣判别(人工金标)", "src": "MT-Bench human judgments",
            "type": "单选", "diff": 4,
            "src_detail": "lmsys/mt_bench_human_judgments 官方人工评审(取无平局样本)",
            "messages": [{"role": "user", "content":
                "Question: " + ca[0]["content"] +
                "\n\nResponse A:\n" + ans_a +
                "\n\nResponse B:\n" + ans_b +
                "\n\nWhich response is better, A or B? Answer with a single letter."}],
            "ref": f"{win}(人工评审一致)", "points": "与人工金标一致得满分",
            "scoring": {"mode": "mc", "answer": win, "letters": "AB"},
        })
    return qs


# ---------------------------------------------------------------- 错误识别: HaluEval
def conv_er():
    qs = []
    for r in random.sample(jl(CACHE / "halu_qa.json"), 12):
        opts = [("A", r["right_answer"]), ("B", r["hallucinated_answer"])]
        random.shuffle(opts)
        ans = "A" if opts[0][1] == r["right_answer"] else "B"
        qs.append({
            "sub": "qa 幻觉识别", "src": "HaluEval", "type": "单选", "diff": 4,
            "src_detail": "HaluEval qa_data 官方原题(right/hallucinated官方标注)",
            "messages": [{"role": "user", "content":
                "Knowledge: " + r["knowledge"] + "\nQuestion: " + r["question"] +
                "\nWhich answer is correct (the other one is hallucinated)?\n" +
                f"A．{opts[0][1]}\nB．{opts[1][1]}\nAnswer with a single letter."}],
            "ref": ans, "points": "识别正确得满分",
            "scoring": {"mode": "mc", "answer": ans, "letters": "AB"},
        })
    rows = jl(CACHE / "halu_general.json")
    yes = [r for r in rows if r.get("hallucination") == "yes"][:200]
    no = [r for r in rows if r.get("hallucination") == "no"][:200]
    for r in random.sample(yes, 4) + random.sample(no, 4):
        ans = "A" if r["hallucination"] == "yes" else "B"
        qs.append({
            "sub": "一般知识幻觉判断", "src": "HaluEval", "type": "单选", "diff": 4,
            "src_detail": "HaluEval general_data 官方原题(hallucination官方标注)",
            "messages": [{"role": "user", "content":
                "User query: " + r["user_query"] + "\nResponse: " + r["chatgpt_response"] +
                "\nDoes the response contain hallucination or factual errors?\nA．Yes\nB．No\nAnswer with a single letter."}],
            "ref": ans, "points": "判断正确得满分",
            "scoring": {"mode": "mc", "answer": ans, "letters": "AB"},
        })
    random.shuffle(qs)
    return qs


# ---------------------------------------------------------------- 文档解析(多模态支线): OmniDocBench
def omnidoc_repick_and_download():
    """按 subset 多样性重选12页并下载图片(已存在则跳过)"""
    data = json.load(open(CACHE / "omnidocbench.json", encoding="utf-8"))
    for p in data:
        a = p["page_info"]["page_attribute"]
        p["_a"] = a if isinstance(a, dict) else {}
    by_subset = {}
    for p in data:
        by_subset.setdefault(p["_a"].get("subset", "?"), []).append(p)
    picks, seen_subset = [], Counter()
    for sub, pool in sorted(by_subset.items(), key=lambda kv: -len(kv[1])):
        for p in pool[:3]:
            if len(picks) >= 12:
                break
            picks.append(p)
            seen_subset[sub] += 1
        if len(picks) >= 12:
            break
    img_dir = CACHE / "omnidoc_images"
    img_dir.mkdir(exist_ok=True)
    out = []
    for p in picks:
        im = p["page_info"]["image_path"]
        dst = img_dir / im
        if not dst.exists():
            for _ in range(3):
                try:
                    req = urllib.request.Request(
                        "https://hf-mirror.com/datasets/opendatalab/OmniDocBench/resolve/main/images/" + im,
                        headers={"User-Agent": "curl/8"})
                    with urllib.request.urlopen(req, timeout=90) as r:
                        dst.write_bytes(r.read())
                    break
                except Exception:
                    time.sleep(2)
            else:
                print("  图片下载失败, 跳过:", im)
                continue
        out.append({"image": im, **p["_a"], "layout_dets": p["layout_dets"]})
    json.dump(out, open(CACHE / "omnidoc_picked.json", "w", encoding="utf-8"), ensure_ascii=False)
    return out


def omnidoc_markdown(page):
    """按 order 与 anno 官方标注重建页面 Markdown 作为参考答案"""
    blocks = [b for b in page["layout_dets"] if not str(b.get("ignore")).lower() == "true"]
    blocks.sort(key=lambda b: int(b.get("order") or 0))
    parts = []
    for b in blocks:
        cat = b.get("category_type")
        attr = b.get("attribute") or {}
        if isinstance(attr, str):
            try:
                attr = ast.literal_eval(attr)
            except Exception:
                attr = {}
        txt = (b.get("text") or "").strip()
        if cat == "title":
            parts.append("## " + txt)
        elif cat == "table" and attr.get("html"):
            parts.append(attr["html"])
        elif cat in ("equation_isolated", "equation_semantic"):
            if attr.get("latex"):
                parts.append("$$" + attr["latex"] + "$$")
            elif txt:
                parts.append("$" + txt + "$")
        elif cat == "code_txt":
            parts.append("```\n" + txt + "\n```")
        elif cat in ("text_block", "list_group") and txt:
            parts.append(txt)
    return "\n\n".join(parts)


def conv_dp():
    pages = omnidoc_repick_and_download()
    qs = []
    for p in pages:
        gt = omnidoc_markdown(p)
        if len(gt) < 80:
            continue
        qs.append({
            "sub": f"{p.get('data_source')}/{p.get('subset')}", "src": "OmniDocBench",
            "type": "转换", "diff": 5, "multimodal": True,
            "src_detail": f"OmniDocBench 官方页面({p.get('data_source')}, {p.get('language')}); "
                          "参考答案由官方layout标注重建, 字符级F1判分",
            "images": ["benchmarks_cache/omnidoc_images/" + p["image"]],
            "messages": [{"role": "user", "content":
                "Convert this document page to well-structured Markdown. Keep the reading "
                "order, output tables as HTML or Markdown tables, and formulas in LaTeX "
                "($...$ for inline, $$...$$ for display). Output only the Markdown."}],
            "ref": gt[:150] + "…",
            "points": "与官方标注重建的Markdown计算字符级F1×10",
            "scoring": {"mode": "f1", "refs": [gt]},
        })
    return qs


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="不归档旧题库")
    args = ap.parse_args()

    if not args.keep and QDIR.exists() and any(QDIR.glob("*.jsonl")):
        arc = BASE / "questions_archive"
        arc.mkdir(exist_ok=True)
        import shutil
        for f in QDIR.glob("*.jsonl"):
            shutil.move(str(f), str(arc / f.name))
        print(f"旧题库已归档 → questions_archive/ ({len(list(arc.glob('*.jsonl')))} 个文件)")

    ins, con = conv_ins_con()
    write_dim("语言理解", conv_lu())
    write_dim("信息提取", conv_ie())
    write_dim("创造力", conv_cr())
    write_dim("指令遵循", ins)
    write_dim("复杂推理", conv_rea())
    write_dim("约束处理", con)
    write_dim("结构化输出", conv_so())
    write_dim("长文本组织", conv_lt())
    write_dim("上下文利用", conv_cu())
    write_dim("信息检索", conv_ir())
    write_dim("评估", conv_ev())
    write_dim("错误识别", conv_er())
    write_dim("文档解析", conv_dp())
    total = sum(len(jl(QDIR / f"{file_code(d)}.jsonl")) for d in DIMS_OFFICIAL)
    print(f"\n题库重建完成: 共 {total} 题, 维度 {len(DIMS_OFFICIAL)} 个(含文档解析多模态支线)")


if __name__ == "__main__":
    main()
