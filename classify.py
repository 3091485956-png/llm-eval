# -*- coding: utf-8 -*-
"""classify.py — 模型分类计算器（产出供 workbook「模型分类」子表使用）
口径(2026-10-24 用户裁决):
  1) 梯队固定带: T1 ≥75 / T2 65~75 / T3 <65(客观综合分)
  2) 创造力: LLM评审主观分, 只列参考, 不进主公式
  3) 性价比分母 = 施测费用(元, 不含评审费用)
  4) 保留题目区分度层(维度间标准差 <5pp → 区分度不足)
"""
import json
import statistics
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CANON = ["语言理解", "信息提取", "创造力", "指令遵循", "复杂推理", "约束处理",
         "结构化输出", "长文本组织", "上下文利用", "信息检索", "评估", "错误识别", "文档解析"]
OBJ_DIMS = [d for d in CANON[:12] if d != "创造力"]  # 11 个客观维度

TIER_BANDS = [(75.0, "T1"), (65.0, "T2")]  # ≥75→T1, 65~75→T2, <65→T3
GATE_DONE, GATE_ERR = 0.90, 0.10


def classify(scored_file="results/scored.jsonl"):
    from runner import load_questions
    questions = load_questions()
    # 应作答题数: 本场无模型答图片题(文档解析无记录), 按文本题计
    expected = len([q for q in questions if not q.get("images")])
    recs = [json.loads(l) for l in (BASE / scored_file).read_text(
        encoding="utf-8").splitlines() if l.strip()]

    dims_ran = {r["dim"] for r in recs}
    obj_dims = [d for d in OBJ_DIMS if d in dims_ran]  # 实际开考的客观维度
    models = {}
    for r in recs:
        m = models.setdefault(r["model_id"], {
            "name": r.get("model_name", ""), "dim_sum": {}, "dim_n": {},
            "valid": 0, "err": 0, "cost": 0.0, "judge_fail": 0})
        if r.get("dim") == "创造力" and "评审调用失败" in str(r.get("detail", "")):
            m["judge_fail"] += 1
        ok = bool((r.get("response") or "").strip()) and not r.get("error")
        if ok:
            m["valid"] += 1
            m["dim_sum"][r["dim"]] = m["dim_sum"].get(r["dim"], 0.0) + r["score"] / r.get("full", 10)
            m["dim_n"][r["dim"]] = m["dim_n"].get(r["dim"], 0) + 1
            m["cost"] += r.get("cost") or 0.0
        else:
            m["err"] += 1

    out = {"generated": time.strftime("%Y-%m-%d %H:%M"), "expected": expected,
           "obj_dims": obj_dims, "rows": [], "zmatrix": [], "disc": []}
    # ---- 维度级群体统计(z与区分度) ----
    dim_stats = {}
    for d in obj_dims + (["创造力"] if "创造力" in dims_ran else []):
        vals = [models[mid]["dim_sum"].get(d, 0) / models[mid]["dim_n"][d] * 100
                for mid in models if models[mid]["dim_n"].get(d)]
        dim_stats[d] = (statistics.mean(vals), statistics.pstdev(vals) if len(vals) > 1 else 0.0)

    rows = []
    for mid, m in models.items():
        rates = {d: m["dim_sum"].get(d, 0) / m["dim_n"][d] * 100
                 for d in obj_dims if m["dim_n"].get(d)}
        s_obj = statistics.mean(rates.values()) if rates else 0.0
        done_rate = m["valid"] / expected
        err_rate = m["err"] / expected
        gate = "✓" if (done_rate >= GATE_DONE and err_rate <= GATE_ERR) else "⚠"
        tier = next((t for th, t in TIER_BANDS if s_obj >= th), "T3")
        cost = round(m["cost"], 4)
        rows.append({"id": mid, "name": m["name"], "s_obj": round(s_obj, 1),
                     "done": done_rate, "err": err_rate, "gate": gate, "tier": tier,
                     "creativity": round(models[mid]["dim_sum"].get("创造力", 0)
                                          / models[mid]["dim_n"].get("创造力", 1) * 100, 1)
                     if models[mid]["dim_n"].get("创造力") else None,
                     "judge_fail": m["judge_fail"], "cost": cost,
                     "missing": expected - m["valid"] - m["err"]})
    # 性价比/象限(中位数切)
    cap_med = statistics.median(r["s_obj"] for r in rows)
    costs = [r["cost"] for r in rows if r["cost"] > 0]
    cost_med = statistics.median(costs) if costs else 0
    for r in rows:
        r["cp"] = round(r["s_obj"] / r["cost"], 1) if r["cost"] > 0 else None
        strong = r["s_obj"] >= cap_med
        cheap = r["cost"] <= cost_med if r["cost"] > 0 else True
        r["quad"] = ("高能低价" if strong and cheap else "高能高价" if strong
                     else "低能低价" if cheap else "低能高价") if r["cost"] > 0 else "费用未配置"
    # ---- 画像标签在 zmatrix 生成时统一打(双轨: z相对 + <50%绝对) ----
    zmatrix = []
    for r in rows:
        m = models[r["id"]]
        cells, tags = [], []
        for d in obj_dims + (["创造力"] if "创造力" in dims_ran else []):
            if not m["dim_n"].get(d):
                cells.append({"dim": d, "val": None, "z": None, "label": ""})
                continue
            val = m["dim_sum"][d] / m["dim_n"][d] * 100
            mu, sd = dim_stats[d]
            z = (val - mu) / sd if sd > 0 else 0.0
            lab = ""
            if d != "创造力":  # 客观维度才打标签
                if z >= 0.5:
                    lab = "◆相对优势"
                elif z <= -0.5:
                    lab = "▼相对短板"
                if val < 50:
                    lab = (lab + "+") if lab else ""
                    lab = "✖绝对短板" + ("(兼▼)" if z <= -0.5 else "")
            cells.append({"dim": d, "val": round(val, 1), "z": round(z, 2), "label": lab})
            if lab:
                tags.append(f"{d}{lab}")
        r["tags"] = tags
        zmatrix.append({"id": r["id"], "name": r["name"], "cells": cells})
    # ---- 题目区分度 ----
    for d in obj_dims + (["创造力"] if "创造力" in dims_ran else []):
        mu, sd = dim_stats[d]
        disc = "区分度不足" if sd < 5 else "区分度良好"
        hard = "偏难(均值<50%)" if mu < 50 else ("偏易(均值≥90%)" if mu >= 90 else "难度适中")
        if sd < 5 and mu >= 90:
            advice = "天花板效应：全员接近满分，建议下轮更换更难题目"
        elif sd < 5:
            advice = "模型间差异过小，参考价值有限"
        elif mu < 50 and sd >= 5:
            advice = "地板效应但区分度高：保留（拉开差距的关键维度）"
        else:
            advice = "保留"
        out["disc"].append({"dim": d, "mean": round(mu, 1), "std": round(sd, 1),
                            "hard": hard, "disc": disc, "advice": advice,
                            "subjective": d == "创造力"})
    rows.sort(key=lambda r: -r["s_obj"])
    out["rows"] = rows
    out["zmatrix"] = zmatrix
    out["cap_med"], out["cost_med"] = round(cap_med, 1), round(cost_med, 4)
    return out


def preview():
    o = classify()
    print("== 主表 ==")
    for r in o["rows"]:
        print(f"{r['id']} {r['name']:<18} 综合{r['s_obj']:>5} 完成率{r['done']:.0%} "
              f"错误率{r['err']:.0%} {r['gate']} {r['tier']} 创造力{r['creativity']}(参考) "
              f"费用{r['cost']}元 性价比{r['cp']} {r['quad']}")
        print("   画像:", "；".join(r["tags"]) or "无显著标签")
    print("\n== 区分度 ==")
    for d in o["disc"]:
        print(f"{d['dim']:<6} 均值{d['mean']:>5} std{d['std']:>5} {d['hard']} {d['disc']} → {d['advice']}")
    print("\n中位: 能力", o["cap_med"], "费用", o["cost_med"])


if __name__ == "__main__":
    preview()
