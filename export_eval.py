# -*- coding: utf-8 -*-
"""export_eval.py — 评分包导出器（供人工/外部评分使用）
从 results/results.jsonl 读取已施测题目与各模型作答，导出到 评分包/：
  1) 题目与官方答案.md   —— 逐题: 题号/维度/来源/难度/题干全文/官方答案(或官方判分口径)
  2) 作答_<MID_名称>.md  —— 每模型一份, 题号顺序与上一文档一一对应
  3) 人工评分表.csv      —— 行=模型×题目×轮次, 预留"人工得分/备注"空列
官方答案原则: 只取基准数据集官方标注(mc/exact/numeric/mathexact/f1);
schemavalid/ifbench 等程序判分题标注官方判分口径并附官方约束/Schema; 评审类标注官方评审维度。
用法: python export_eval.py [--out results/results.jsonl] [--dir 评分包]
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
NL = chr(10)

DIMS_OFFICIAL = ["语言理解", "信息提取", "创造力", "指令遵循", "复杂推理", "约束处理",
                 "结构化输出", "长文本组织", "上下文利用", "信息检索", "评估", "错误识别",
                 "文档解析"]


def dim_rank(d):
    return DIMS_OFFICIAL.index(d) if d in DIMS_OFFICIAL else 99


def official_answer(q):
    """返回 (官方答案文本, 是否无固定答案) —— 严格取自官方标注/官方口径, 不自行设想。"""
    sc = q.get("scoring") or {}
    mode = sc.get("mode", "llm")
    if mode == "mc":
        return "官方标注答案：" + str(sc.get("answer", "")), False
    if mode == "exact":
        ans = sc.get("answers") or [sc.get("answer", "")]
        return "官方标注答案：" + "；".join(str(a) for a in ans), False
    if mode == "numeric":
        return "官方标注答案：" + str(sc.get("value", "")), False
    if mode == "mathexact":
        return "官方标注答案：" + str(sc.get("answer", "")), False
    if mode == "f1":
        refs = sc.get("refs") or []
        return ("官方参考答案（基准官方标注，评分用字符级F1对比）："
                + NL + NL.join("> " + str(r).replace(NL, " ") for r in refs)), False
    if mode == "schemavalid":
        schema = json.dumps(sc.get("schema", {}), ensure_ascii=False, indent=1)
        return ("本题无固定答案｜官方判分口径：模型输出须为可通过下方【官方JSON Schema】校验的实例"
                + NL + "```json" + NL + schema + NL + "```"), True
    if mode == "ifbench":
        cons = []
        for ins, kw in zip(sc.get("instructions", []), sc.get("kwargs", [])):
            params = ", ".join(f"{k}={v}" for k, v in (kw or {}).items() if v is not None)
            cons.append(f"- {ins}（{params}）")
        return ("本题无固定答案｜官方判分口径：IFBench官方校验器逐条判定以下约束"
                + NL + NL.join(cons)), True
    # llm / coverage / hybrid 等评审类
    src = q.get("src", "")
    return (f"本题无固定答案｜官方评审维度（{src}官方协议）：" + str(q.get("points", ""))), True


def export(out_file="results/results.jsonl", out_dir=None):
    from runner import desktop_dir, load_questions
    recs = [json.loads(l) for l in (BASE / out_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not recs:
        return {"ok": False, "error": "results/results.jsonl 为空，请先施测或竞测"}
    qmap = {q["id"]: q for q in load_questions()}
    # 选取的题目 = 已作答记录中出现过的题目, 按 题库顺序(维度+题号) 排列
    qids = sorted({r["question_id"] for r in recs if r["question_id"] in qmap},
                  key=lambda i: (dim_rank(qmap[i]["dim"]), i))
    if not qids:
        return {"ok": False, "error": "作答记录中的题目不在当前题库中"}
    d = Path(out_dir) if out_dir else desktop_dir() / "评分包"
    d.mkdir(exist_ok=True)
    models = {}
    for r in recs:
        models.setdefault((r["model_id"], r.get("model_name", "")), []).append(r)
    models = dict(sorted(models.items()))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 1) 题目与官方答案 ----------
    md = ["# 评分包 · 题目与官方答案", "",
          f"- 生成时间：{stamp}",
          f"- 题目范围：本次施测实际选取的 {len(qids)} 道题（与各《作答_》文档按题号一一对应）",
          "- 参评模型：" + "、".join(f"{mid} {name}" for (mid, name) in models),
          "- 说明：官方答案均直接取自基准数据集的官方标注；无固定答案的题目给出官方判分口径（官方校验器/官方Schema/官方评审维度），不含任何自拟答案。", "",
          "---", ""]
    for qid in qids:
        q = qmap[qid]
        ans, _ = official_answer(q)
        md.append(f"## {qid} · {q['dim']} · {q.get('src', '')} · {q.get('type', '')}")
        md.append(f"- 来源详情：{q.get('src_detail', '')}　难度：{'★' * int(q.get('diff', 3))}")
        if q.get("images"):
            md.append(f"- 多模态题：共 {len(q['images'])} 张页面图（见 benchmarks_cache/omnidoc_images/，题目文档内以图片文件名标注）")
            md.append(f"- 图片文件：{'、'.join(Path(p).name for p in q['images'])}")
        md.append("")
        md.append("### 题目")
        for i, m in enumerate(q["messages"], 1):
            md.append(f"【第{i}轮·{'用户' if m['role'] == 'user' else '助手'}】{m['content']}")
        md.append("")
        md.append("### 官方答案")
        md.append(ans)
        md.append("")
        md.append("---")
        md.append("")
    (d / "题目与官方答案.md").write_text(NL.join(md), encoding="utf-8")

    # ---------- 2) 每模型作答(与上一文档一一对应) ----------
    files = ["题目与官方答案.md"]
    for (mid, name), rows in models.items():
        safe = (name or "模型").replace("/", "_").replace("\\", "_").replace(":", "_")
        by_q = {}
        for r in rows:
            by_q.setdefault(r["question_id"], []).append(r)
        m = [f"# {mid} {name} · 作答", "",
             f"- 生成时间：{stamp}",
             f"- 与《题目与官方答案.md》按题号一一对应（本模型共作答 {sum(1 for q_ in qids if q_ in by_q)}/{len(qids)} 题）",
             "", "---", ""]
        for qid in qids:
            qrs = sorted(by_q.get(qid, []), key=lambda r: r.get("run", 1))
            if not qrs:
                m.append(f"## {qid} · {qmap[qid]['dim']}")
                m.append("（本模型未作答/跳过）")
                m.append("")
                continue
            for r in qrs:
                m.append(f"## {qid} · {qmap[qid]['dim']} · 第{r.get('run', 1)}轮"
                         f"　（用时 {r.get('latency_s', '-')}s · {r.get('timestamp', '')}）")
                m.append("")
                m.append(r.get("response") or ("（无回答：" + str(r.get("error", "")) + "）"))
                m.append("")
                m.append("---")
                m.append("")
        fname = f"作答_{mid}_{safe}.md"
        (d / fname).write_text(NL.join(m), encoding="utf-8")
        files.append(fname)

    # ---------- 3) 人工评分表 CSV ----------
    import csv as _csv
    csv_path = d / "人工评分表.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["题号", "维度", "来源", "模型", "轮次", "官方答案或判分口径(截断)",
                    "模型回答", "人工得分(0-10)", "备注"])
        for qid in qids:
            q = qmap[qid]
            ans, _ = official_answer(q)
            ans_flat = ans.replace(NL, " ")[:300]
            for (mid, name), rows in models.items():
                qrs = sorted([r for r in rows if r["question_id"] == qid],
                             key=lambda r: r.get("run", 1))
                if not qrs:
                    w.writerow([qid, q["dim"], q.get("src", ""), f"{mid} {name}", "-",
                                ans_flat, "（未作答/跳过）", "", ""])
                for r in qrs:
                    w.writerow([qid, q["dim"], q.get("src", ""), f"{mid} {name}",
                                r.get("run", 1), ans_flat,
                                (r.get("response") or "").replace(NL, "⏎"), "", ""])
    files.append("人工评分表.csv")
    return {"ok": True, "dir": str(d), "files": files,
            "questions": len(qids), "models": len(models), "records": len(recs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/results.jsonl")
    ap.add_argument("--dir", default=None, help="默认导出到桌面\\评分包\\")
    args = ap.parse_args()
    r = export(args.out, args.dir)
    if r.get("ok"):
        print(f"评分包已导出 → {r['dir']}")
        print(f"题目 {r['questions']} 道 | 模型 {r['models']} 个 | 记录 {r['records']} 条")
        for f in r["files"]:
            print("  -", f)
    else:
        print("导出失败:", r.get("error"))


if __name__ == "__main__":
    main()
