# -*- coding: utf-8 -*-
"""scorer.py — 三层评分流水线
  ① 规则判分(确定性): mc / exact / numeric / json / constraints / anchor
  ② LLM 评审(主观主力): llm / coverage / hybrid
输出: results/scored.jsonl, 并打印 模型×维度 得分率摘要
用法:
  python scorer.py --mock        # 离线评审(验证链路)
  python scorer.py               # 用 models.yaml 中 judge 配置评分
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # 被重定向到StringIO等场景

# ---------------------------------------------------------------- utils
PUNCT_STRIP = "，。、；：？！“”‘’（）【】《》—…·,.;:?!\n\t\r ()[]{}\"'-_/"


def normalize(text):
    """去空白与中英标点后比较, 忽略大小写。"""
    return re.sub(r"[\s" + re.escape(PUNCT_STRIP) + r"]", "", str(text)).casefold()


def strip_cjk_punct(s):
    """仅去掉标点(保留汉字/字母/数字), 用于计数类约束。"""
    return re.sub(r"[\s" + re.escape(PUNCT_STRIP) + r"]", "", s)


def contains_answer(response, answer):
    """短答案包含判断; 纯数字答案要求数字边界, 避免把 120 匹配进 1200。"""
    resp_n, ans_n = normalize(response), normalize(answer)
    if not ans_n:
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", ans_n):
        return re.search(r"(?<!\d)" + re.escape(ans_n) + r"(?!\d)", resp_n) is not None
    return ans_n in resp_n


def extract_numbers(text):
    return [float(m.group()) for m in re.finditer(r"-?\d+(?:\.\d+)?", text.replace(",", ""))]


def parse_choice(text, letters="ABCD"):
    """从回答中提取选项字母集合: 优先显式标记, 其次行首字母, 最后孤立字母。"""
    text = text.strip()
    pats = [r"(?:正确答案|答案|应该?选|应选|选择|选)[^\nA-Za-z]{0,6}([A-D])",
            r"(?m)^\s*([A-D])\s*[\.、。:：\)]",
            r"([A-D])\s*[\.、。]",
            r"(?<![A-Za-z])([A-D])(?![A-Za-z])"]
    for p in pats:
        found = re.findall(p, text)
        if found:
            return set(found)
    return set()


def md5int(*parts):
    return int(hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest(), 16)


# ---------------------------------------------------------------- 判分层1: 规则
def score_mc(resp, sc):
    ans = set(sc.get("answer", ""))
    got = parse_choice(resp, sc.get("letters", "ABCD"))
    return (10 if got == ans else 0), f"标准{sorted(ans)} / 提取{sorted(got)}"


def score_exact(resp, sc):
    answers = [str(a) for a in (sc.get("answers") or [sc.get("answer")]) if a is not None]
    if not answers:
        return 0, "无标准答案"
    hits = sum(1 for a in answers if contains_answer(resp, a))
    return round(10 * hits / len(answers), 1), f"命中 {hits}/{len(answers)}: {answers}"


def score_numeric(resp, sc):
    vals = sc.get("values") or [sc.get("value")]
    nums = extract_numbers(resp)
    if not nums:
        return 0, "未找到数字"
    tol = sc.get("tolerance", 0.01)
    hits = sum(1 for v in vals
               if any(abs(n - float(v)) <= tol * max(1, abs(float(v))) for n in nums))
    return round(10 * hits / len(vals), 1), f"命中 {hits}/{len(vals)} (期望{vals})"


def _walk(obj, path):
    cur = obj
    for k in path.split("."):
        if isinstance(cur, list) and k.isdigit():
            i = int(k)
            cur = cur[i] if i < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
        if cur is None:
            return None
    return cur


def _strip_fences(text):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    return m.group(1).strip() if m else t


def score_json(resp, sc):
    try:
        obj = json.loads(_strip_fences(resp))
    except Exception:
        try:
            obj = json.loads(resp[resp.find("{"):resp.rfind("}") + 1])
        except Exception:
            return 0, "JSON解析失败"
    checks = sc.get("checks", [])
    if not checks:
        return 10, "可解析"
    passed, notes = 0, []
    for c in checks:
        val = _walk(obj, c["path"])
        ok = val is not None
        if ok and "equals" in c:
            exp = c["equals"]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                ok = abs(float(val) - float(exp)) < 1e-6
            else:
                ok = normalize(val) == normalize(exp)
        if ok and "contains" in c:
            hay = val if not isinstance(val, list) else json.dumps(val, ensure_ascii=False)
            ok = normalize(c["contains"]) in normalize(hay)
        if ok and "length" in c:
            ok = isinstance(val, list) and len(val) == int(c["length"])
        passed += 1 if ok else 0
        if not ok:
            notes.append(c["path"] + "✗")
    score = round(10 * passed / len(checks), 1)
    return score, ("全部通过" if passed == len(checks) else "未过: " + ",".join(notes))


def _sentences(text):
    return [p for p in (s.strip() for s in re.split(r"[。！？!?]", text)) if p]


def check_constraints(resp, constraints):
    out = []
    for c in constraints:
        t = c["type"]
        ok = False
        if t in ("max_len", "min_len", "exact_len"):
            n = len(strip_cjk_punct(resp))
            ok = (n <= c["max_len"]) if t == "max_len" else \
                 (n >= c["min_len"]) if t == "min_len" else (n == c["value"])
        elif t == "exact_len_no_punct":
            ok = len(strip_cjk_punct(resp)) == c["value"]
        elif t == "must_include":
            ok = all(normalize(w) in normalize(resp) for w in c["words"])
        elif t == "ordered_include":
            pos, ok = -1, True
            for w in c["words"]:
                i = normalize(resp).find(normalize(w))
                if i < 0 or i < pos:
                    ok = False
                    break
                pos = i
        elif t == "forbidden":
            r = normalize(resp)
            ok = all(normalize(w) not in r for w in c["words"])
        elif t == "has_digit":
            ok = re.search(r"\d", resp) is not None
        elif t == "no_digit":
            bad = set("0123456789") | set(c.get("extra_chars", "零一二三四五六七八九十百千万两"))
            ok = all(ch not in resp for ch in bad)
        elif t == "regex":
            ok = re.search(c["pattern"], resp, re.M) is not None
        elif t == "fullmatch":
            ok = re.fullmatch(c["pattern"], resp.strip(), re.M | re.S) is not None
        elif t == "starts_with":
            ok = resp.strip().startswith(c["value"])
        elif t == "ends_with":
            ok = resp.strip().endswith(c["value"])
        elif t == "line_count":
            ok = len([l for l in resp.splitlines() if l.strip()]) == c["value"]
        elif t == "line_starts_with":
            lines = [l.strip() for l in resp.splitlines() if l.strip()]
            n = c.get("first_n", len(lines))
            ok = bool(lines) and all(l.startswith(c["value"]) for l in lines[:n])
        elif t == "sentences_count":
            ok = len(_sentences(resp)) == c["value"]
        elif t == "sentence_len_max":
            ok = all(len(strip_cjk_punct(s)) <= c["value"] for s in _sentences(resp))
        elif t == "all_question":
            parts = [p for p in re.split(r"(?<=[！？!?])", resp.strip()) if p.strip()]
            ok = bool(parts) and all(p.strip().endswith(("？", "?")) for p in parts) \
                and len(parts) == c.get("value", len(parts))
        elif t == "starts_ends_same":
            core = strip_cjk_punct(resp.strip())
            ok = len(core) >= 2 and core[0] == core[-1]
        elif t == "word_count_max":
            ok = len(resp.split()) <= c["value"]
        elif t == "md_table_rows":
            ok = len([l for l in resp.splitlines() if l.strip().startswith("|")]) >= c["value"]
        elif t == "csv_lines":
            lines = [l for l in resp.splitlines() if l.strip()]
            ok = len(lines) >= c.get("min_lines", 3) and \
                all(l.count(",") == c["cols"] for l in lines)
        elif t == "yaml_lines":
            pat = re.compile(r"^\s*[\w\u4e00-\u9fff\-]+\s*:")
            ok = sum(1 for l in resp.splitlines() if pat.match(l)) >= c["value"] \
                and "\t" not in resp
        else:
            ok = False
        out.append((c, ok))
    return out


def score_constraints(resp, constraints):
    results = check_constraints(resp, constraints)
    if not results:
        return 0, "无约束定义"
    if any((not ok) and c.get("hard") for c, ok in results):
        return 0, "违反硬约束: " + ",".join(c["type"] for c, ok in results if not ok)
    passed = sum(1 for _, ok in results if ok)
    score = round(10 * passed / len(results), 1)
    detail = f"满足 {passed}/{len(results)}: " + " ".join(
        f"[{c['type']}{'✓' if ok else '✗'}]" for c, ok in results)
    return score, detail


def score_f1(resp, sc):
    """字符级F1(LongBench口径): 与任一参考答案的最高F1×10"""
    from collections import Counter

    def char_f1(pred, ref):
        p = re.sub(r"\s+", "", str(pred))
        r = re.sub(r"\s+", "", str(ref))
        if not p or not r:
            return 0.0
        pc, rc = Counter(p), Counter(r)
        overlap = sum((pc & rc).values())
        if not overlap:
            return 0.0
        prec, rec = overlap / sum(pc.values()), overlap / sum(rc.values())
        return 2 * prec * rec / (prec + rec)

    refs = sc.get("refs") or []
    if not refs:
        return 0, "无参考答案"
    best = max(char_f1(resp, r) for r in refs)
    return round(10 * best, 1), f"最高字符F1={best:.3f}"


def _math_norm(s):
    """LaTeX答案归一化(参考MATH-500常用minerva式简化)"""
    s = str(s)
    m = re.search(r"\\boxed\{([\s\S]+)\}", s)
    if m:
        s = m.group(1)
    s = s.replace("\\left", "").replace("\\right", "")
    for t in ("\\,", "\\;", "\\!", "\\ ", "$"):
        s = s.replace(t, "")
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = re.sub(r"\\text\{[^}]*\}", "", s)
    s = re.sub(r"\s+", "", s)
    return s.casefold().rstrip(".")


def score_mathexact(resp, sc):
    want = _math_norm(sc.get("answer", ""))
    got = _math_norm(resp)
    if not want:
        return 0, "无标准答案"
    if got == want or want in got:
        return 10, f"归一化一致({want[:40]})"
    nums = extract_numbers(_math_norm(re.sub(r"\\frac\{(\d+)\}\{(\d+)\}", r"\1/\2 ", resp)))
    wn = extract_numbers(re.sub(r"\\frac\{(\d+)\}\{(\d+)\}", r"\1/\2 ", sc.get("answer", "")))
    if wn and any(abs(a - b) < 1e-6 for a in nums for b in wn):
        return 10, "数值等价"
    return 0, f"期望{want[:40]} / 得到{got[:40]}"


def score_schemavalid(resp, sc):
    import jsonschema
    try:
        obj = json.loads(_strip_fences(resp))
    except Exception:
        try:
            obj = json.loads(resp[resp.find("{"):resp.rfind("}") + 1])
        except Exception:
            return 0, "JSON解析失败"
    try:
        jsonschema.validate(obj, sc["schema"])
        return 10, "符合Schema"
    except Exception as e:
        return 0, "校验失败: " + str(e)[:150]


def score_ifbench(resp, sc):
    """IFBench官方校验器逐条判分(vendored ifbench/, Apache-2.0)"""
    sys.path.insert(0, str(BASE))
    try:
        from ifbench import instructions_registry as reg
    except Exception as e:
        return 0, f"官方校验器加载失败: {e}"
    ids, kws = sc.get("instructions", []), sc.get("kwargs", [])
    passed, notes = 0, []
    for ins_id, kw in zip(ids, kws):
        cls = reg.INSTRUCTION_DICT.get(ins_id)
        if cls is None:
            notes.append(ins_id + "未注册")
            continue
        try:
            inst = cls(ins_id)
            keys = inst.get_instruction_args_keys() or []
            params = {}
            for k in keys:
                v = kw.get(k)
                if v is not None:
                    params[k] = int(v) if isinstance(v, float) and float(v).is_integer() else v
            inst.build_description(**params)
            ok = bool(inst.check_following(resp))
        except Exception as e:
            ok = False
            notes.append(ins_id + ":" + str(e)[:50])
        passed += 1 if ok else 0
    n = len(ids)
    return (round(10 * passed / n, 1) if n else 0), f"官方校验通过 {passed}/{n} " + "; ".join(notes)


# ---------------------------------------------------------------- 判分层2: LLM
JUDGE_SYS = ("你是严格且公正的阅卷评分官。请根据题目、参考答案与评分要点，"
             "对【模型回答】独立打分。不得因回答冗长而加分。")


def build_judge_prompt(q, resp):
    convo = "\n".join(f"[第{i}轮·{m['role']}]{m['content']}"
                      for i, m in enumerate(q["messages"], 1))
    sc = q.get("scoring") or {}
    parts = [f"【题目】\n{convo}",
             f"【参考答案/要点】\n{q.get('ref', '（无固定答案，按评分要点评）')}",
             f"【评分要点】\n{q.get('points', '按答案质量综合评分')}",
             f"【模型回答】\n{resp}",
             "评分规则：满分10分。先逐条核对要求与评分要点，指出对错；"
             "再检查是否违反硬约束（字数、禁用词、格式等）；最后给总分。"]
    if sc.get("mode") == "coverage":
        parts.append("这是覆盖度评分：逐条判断下面每个要点是否被模型回答覆盖"
                     f"（意思达到即可，不要求原文）：{json.dumps(sc['points'], ensure_ascii=False)}")
        parts.append('只输出JSON：{"covered":[1或0,...],"missed":["未覆盖的要点",...]}')
    else:
        parts.append("只按以下格式输出：\n分析：<逐条核对与扣分理由>\n得分：<0-10的整数>")
    return "\n".join(parts)


def parse_judge(text):
    m = re.search(r"得分[：:]\s*(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def parse_coverage(text, n):
    try:
        m = re.search(r"\{[\s\S]*\}", text)
        obj = json.loads(m.group())
        cov = obj.get("covered", [])
        if isinstance(cov, list) and len(cov) == n:
            return [1 if x else 0 for x in cov]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 单条评分
def score_one(q, rec, judge_fn):
    sc = q.get("scoring") or {"mode": "llm"}
    mode, resp = sc["mode"], rec.get("response", "")
    if rec.get("error") or not resp.strip():
        return 0, "规则自动判分", "模型无有效回答", None
    if mode == "mc":
        score, detail = score_mc(resp, sc)
    elif mode == "exact":
        score, detail = score_exact(resp, sc)
    elif mode == "numeric":
        score, detail = score_numeric(resp, sc)
    elif mode == "json":
        score, detail = score_json(resp, sc)
    elif mode == "constraints":
        score, detail = score_constraints(resp, sc.get("constraints", []))
    elif mode == "f1":
        score, detail = score_f1(resp, sc)
    elif mode == "mathexact":
        score, detail = score_mathexact(resp, sc)
    elif mode == "schemavalid":
        score, detail = score_schemavalid(resp, sc)
    elif mode == "ifbench":
        score, detail = score_ifbench(resp, sc)
    elif mode == "anchor":
        got = extract_numbers(resp)
        anchor, scale, tol = float(sc["anchor"]), float(sc.get("scale", 100)), float(sc.get("tolerance", 4))
        near = min(got, key=lambda x: abs(x - anchor)) if got else None
        diff = abs(near - anchor) if near is not None else 999
        score = 10 if diff <= tol / 2 else 8 if diff <= tol else 5 if diff <= tol * 2 else 0
        detail = f"锚定分{anchor}({scale}分制), 模型给{near}, 偏差{diff:.1f}"
    elif mode in ("llm", "coverage", "hybrid"):
        raw, ju = judge_fn(q, resp)
        if mode == "llm":
            s = parse_judge(raw)
            score = max(0.0, min(10.0, s)) if s is not None else 0.0
            detail = raw[:300]
            return score, "LLM评审", detail, ju
        if mode == "coverage":
            cov = parse_coverage(raw, len(sc["points"]))
            if cov:
                score = round(10 * sum(cov) / len(cov), 1)
                detail = f"覆盖 {sum(cov)}/{len(cov)}"
            else:
                score, detail = 0, "评审输出无法解析: " + raw[:200]
            return score, "LLM评审", detail, ju
        # hybrid: 规则约束 + LLM 评审加权
        cons = (sc.get("auto") or {}).get("constraints", [])
        results = check_constraints(resp, cons)
        if any((not ok) and c.get("hard") for c, ok in results):
            score = 0
            detail = "违反硬约束: " + ",".join(c["type"] for c, ok in results if not ok)
        else:
            passed = sum(1 for _, ok in results if ok)
            auto_s = round(10 * passed / len(cons), 1) if cons else 0
            s = parse_judge(raw)
            llm_s = max(0.0, min(10.0, s)) if s is not None else 0.0
            w = sc.get("weights", {"auto": 0.4, "llm": 0.6})
            score = round(auto_s * w.get("auto", 0.4) + llm_s * w.get("llm", 0.6), 1)
            detail = f"规则{auto_s}({passed}/{len(cons)}) + 评审{llm_s}"
        method = "LLM评审+规则"
        return score, method, detail, ju
    else:
        score, detail = 0, f"未知评分模式{mode}"
    method = "规则自动判分" if mode in ("mc", "exact", "numeric", "json", "constraints",
                                       "anchor", "f1", "mathexact", "schemavalid", "ifbench") else "规则自动判分"
    return score, method, detail, None


# ---------------------------------------------------------------- main
def make_judge_fn(cfg, mock):
    if mock:
        def judge(q, resp):
            mode = (q.get("scoring") or {}).get("mode")
            seed = md5int("judge", q["id"], resp[:64])
            if mode == "coverage":
                n = len(q["scoring"]["points"])
                text = json.dumps(
                    {"covered": [1 if (seed >> i) % 100 < 80 else 0 for i in range(n)]})
            else:
                text = f"分析：模拟评审，逐条核对了要点与约束。\n得分：{6 + seed % 4}"
            usage = {"prompt_tokens": 480 + seed % 300,
                     "completion_tokens": 120 + seed % 260}
            return text, usage
        return judge
    if not cfg or not cfg.get("model"):
        sys.exit("未配置 judge 评审模型: 请在 models.yaml 填写 judge 段, 或使用 --mock")
    sys.path.insert(0, str(BASE))
    from runner import chat_with_retry, load_yaml
    full = load_yaml(BASE / "models.yaml")

    def judge(q, resp):
        try:
            text, usage, _ = chat_with_retry(
                cfg, [{"role": "system", "content": JUDGE_SYS},
                      {"role": "user", "content": build_judge_prompt(q, resp)}],
                cfg.get("temperature", 0.0), 2048,
                (full.get("defaults") or {}).get("retries", 2))
            return text, usage or {}
        except Exception as e:
            # 评审调用失败不崩全局: 记0分并留痕
            return ("评审调用失败: " + str(e)[:150], {})
    return judge


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--results", default="results/results.jsonl")
    ap.add_argument("--out", default="results/scored.jsonl")
    args = ap.parse_args()

    judge_cfg = {}
    if not args.mock:
        from runner import load_yaml
        judge_cfg = (load_yaml(BASE / "models.yaml")).get("judge") or {}
    judge_fn = make_judge_fn(judge_cfg, args.mock)

    from runner import load_questions
    qmap = {q["id"]: q for q in load_questions()}
    recs = [json.loads(l) for l in (BASE / args.results).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"待评分 {len(recs)} 条")

    # 费用估算: 按各模型 pricing(元/百万tokens) 牌价计算, 未配置则留空
    from runner import load_yaml
    models_cfg = load_yaml(BASE / "models.yaml")
    price = {m["id"]: (m.get("pricing") or None) for m in models_cfg.get("models", [])}
    judge_price = (models_cfg.get("judge") or {}).get("pricing") or None

    def cost_of(in_t, out_t, p):
        if not p or in_t is None or out_t is None:
            return None
        return round(in_t / 1e6 * float(p.get("input_per_m", 0))
                     + out_t / 1e6 * float(p.get("output_per_m", 0)), 4)

    rows, summary, cost_sum = [], {}, {}
    for rec in recs:
        try:
            q = qmap.get(rec["question_id"])
            if not q:
                print(f"! 跳过未知题目 {rec['question_id']}")
                continue
            score, method, detail, ju = score_one(q, rec, judge_fn)
        except Exception as e:
            score, method, detail, ju = 0, "规则自动判分", f"评分异常: {str(e)[:150]}", None
        usage = rec.get("usage") or {}
        in_t, out_t = usage.get("prompt_tokens"), usage.get("completion_tokens")
        ju = ju or {}
        j_in, j_out = ju.get("prompt_tokens"), ju.get("completion_tokens")
        c_test = cost_of(in_t, out_t, price.get(rec["model_id"]))
        c_judge = cost_of(j_in, j_out, judge_price)
        rows.append({**rec, "score": score, "full": q.get("score", 10),
                     "scoring_method": method, "detail": str(detail)[:500],
                     "input_tokens": in_t, "output_tokens": out_t, "cost": c_test,
                     "judge_input_tokens": j_in if j_out else None,
                     "judge_output_tokens": j_out or None, "judge_cost": c_judge if j_out else None})
        acc = summary.setdefault((rec["model_id"], rec["dim"]), [0.0, 0])
        acc[0] += score / q.get("score", 10)
        acc[1] += 1
        for key, val in (("test", c_test), ("judge", c_judge)):
            if val is not None:
                cost_sum[rec["model_id"]] = cost_sum.get(rec["model_id"], {"test": 0.0, "judge": 0.0})
                cost_sum[rec["model_id"]][key] += val

    with open(BASE / args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    dims = sorted({d for _, d in summary})
    models = sorted({m for m, _ in summary})
    print("\n===== 模型 × 能力 得分率(%) =====")
    print("模型     " + " ".join(f"{d[:3]:>7}" for d in dims))
    for m in models:
        line = f"{m:<8}"
        for d in dims:
            acc = summary.get((m, d))
            line += f"{acc[0] / acc[1] * 100:7.0f}" if acc else "      -"
        if m in cost_sum:
            cs = cost_sum[m]
            line += f"  | 估算费用≈{cs['test'] + cs['judge']:.2f}元"
        print(line)
    print(f"\n已写入 {BASE / args.out} | 共 {len(rows)} 条")


if __name__ == "__main__":
    main()
