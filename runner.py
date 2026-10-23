# -*- coding: utf-8 -*-
"""runner.py — LLM 十二能力测试施测器
用法:
  python runner.py --mock                 # 不联网, 生成确定性模拟回答(用于全链路验证)
  python runner.py                        # 按 models.yaml 正式施测
  python runner.py --models M01,M02       # 只测指定模型
  python runner.py --dims 语言理解,创造力  # 只测指定能力维度
  python runner.py --ids LU01,LU02        # 只测指定题目
输出: results/results.jsonl (每行一条原始记录)
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # 被重定向到StringIO等场景
NL = chr(10)


def desktop_dir():
    """桌面目录(兼容 OneDrive 重定向); 所有导出文件默认落这里。"""
    home = Path.home()
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop", home / "桌面"):
        if cand.is_dir():
            return cand
    return home


# 未配置任何模型时, --mock 模式使用的内置虚拟模型(仅离线验证链路, 不联网)
MOCK_FALLBACK_MODELS = [
    {"id": "M01", "name": "模拟模型A", "vendor": "内置", "base_url": "https://api.example.com/v1",
     "api_key": "", "model": "mock-a", "enabled": True,
     "pricing": {"input_per_m": 4.0, "output_per_m": 16.0}},
    {"id": "M02", "name": "模拟模型B", "vendor": "内置", "base_url": "https://api.example.com/v1",
     "api_key": "", "model": "mock-b", "enabled": True,
     "pricing": {"input_per_m": 2.0, "output_per_m": 8.0}},
    {"id": "M03", "name": "模拟视觉模型", "vendor": "内置", "base_url": "https://api.example.com/v1",
     "api_key": "", "model": "mock-v", "enabled": True, "vision": True,
     "pricing": {"input_per_m": 3.0, "output_per_m": 12.0}},
]


# ---------------------------------------------------------------- config
def load_yaml(path):
    import yaml  # pip install pyyaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def expand_key(v):
    """支持 env:VAR_NAME 形式读取环境变量, 避免明文密钥。"""
    if isinstance(v, str) and v.startswith("env:"):
        return os.environ.get(v[4:], "")
    return v or ""


def load_questions():
    qs = []
    qdir = BASE / "questions"
    for fp in sorted(qdir.glob("*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                qs.append(json.loads(line))
    return qs


# ---------------------------------------------------------------- http
def chat_once(cfg, messages, temperature, max_tokens):
    """OpenAI 兼容 /chat/completions 调用, 返回 (文本, usage, 延迟秒)。"""
    endpoint = cfg.get("endpoint") or cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + expand_key(cfg.get("api_key")),
               # 部分中转站在 Cloudflare 后面, 默认UA会被签名拦截(error 1010)
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
               "Accept": "application/json"}
    headers.update(cfg.get("headers") or {})
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=cfg.get("timeout_seconds", 120)) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    latency = round(time.time() - t0, 2)
    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    text = msg.get("content") or ""
    if not text.strip():
        # 思考模型可能把max_tokens烧在reasoning上导致content为空, 回退用推理文本
        text = msg.get("reasoning_content") or ""
    return text, data.get("usage") or {}, latency


def chat_with_retry(cfg, messages, temperature, max_tokens, retries, budget=None):
    """budget: 单题墙钟预算(秒); 每次尝试的读超时取剩余预算, 预算耗尽立即放弃"""
    last = None
    t0 = time.time()
    for attempt in range(retries + 1):
        try:
            if budget:
                left = budget - (time.time() - t0)
                if left < 15:
                    raise RuntimeError(f"单题预算{budget}s已耗尽")
                cfg2 = dict(cfg, timeout_seconds=int(min(cfg.get("timeout_seconds", 240), left)))
            else:
                cfg2 = cfg
            return chat_once(cfg2, messages, temperature, max_tokens)
        except Exception as e:  # 网络/限流/超时统一重试
            last = e
            if attempt < retries and (not budget or time.time() - t0 < budget - 15):
                time.sleep([3, 10, 30][min(attempt, 2)])  # 过载退避: 3s/10s/30s
    raise RuntimeError("调用失败(重试" + str(retries) + "次): " + str(last))


# ---------------------------------------------------------------- mock
def _seed(*parts):
    return int(hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest(), 16)


def _build_json_from_checks(checks):
    """按 json 检查项构造嵌套对象; 根级 length 检查决定根为数组。"""
    plain = [c for c in checks if c["path"] != ""]
    root_len = next((int(c["length"]) for c in checks
                     if c["path"] == "" and "length" in c), None)
    if root_len is not None:
        root = [{} for _ in range(root_len)]
    else:
        root = {} if not any(c["path"].split(".")[0].isdigit() for c in plain) else []

    def set_path(cur, keys, value):
        for i, k in enumerate(keys[:-1]):
            if k.isdigit():
                idx = int(k)
                while len(cur) <= idx:
                    cur.append({})
                cur = cur[idx]
            else:
                cur.setdefault(k, [] if keys[i + 1].isdigit() else {})
                cur = cur[k]
        last = keys[-1]
        if last.isdigit():
            idx = int(last)
            while len(cur) <= idx:
                cur.append({})
            cur[idx] = value
        else:
            cur[last] = value

    for c in plain:
        keys = c["path"].split(".")
        if "equals" in c:
            v = c["equals"]
            if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v):
                v = float(v) if "." in v else int(v)
        elif "contains" in c:
            v = c["contains"]
        elif "length" in c:
            v = [{} for _ in range(int(c["length"]))]
        else:
            v = "示例值"
        set_path(root, keys, v)
    return root


def mock_response(q, model_id, run):
    """确定性模拟回答: 让 runner→scorer→Excel 全链路可离线验证。"""
    sc = q.get("scoring") or {}
    mode = sc.get("mode", "llm")
    seed = _seed(model_id, q["id"], run)
    if mode == "mc":
        correct = seed % 100 < 65
        letters = sc.get("letters", "ABCD")
        ans = sc.get("answer", "A")
        pick = ans if correct else letters[(letters.index(ans) + 1 + seed) % len(letters)]
        return "经过分析，我认为正确答案是" + pick + "。"
    if mode == "exact":
        ok = seed % 100 < 60
        answers = sc.get("answers") or [sc.get("answer", "未知")]
        return "答案是" + "、".join(str(a) for a in answers) + "。" if ok \
            else "这个问题我不太确定，答案可能是别的。"
    if mode in ("numeric", "anchor"):
        ok = seed % 100 < 60
        val = sc.get("value", sc.get("anchor", 42))
        return "计算可得，答案是 " + str(val if ok else float(val) + 7) + "。"
    if mode == "mathexact":
        ok = seed % 100 < 60
        return ("最终答案：$\\boxed{" + str(sc.get("answer", "0")) + "}$" if ok
                else "最终答案：$\\boxed{0}$")
    if mode == "f1":
        refs = sc.get("refs") or [""]
        return str(refs[0])[:150]
    if mode == "json":
        obj = _build_json_from_checks(sc.get("checks", []))
        if seed % 10 == 0 and isinstance(obj, dict) and obj:  # 偶尔制造一个字段错误
            obj[next(iter(obj))] = "错误值"
        return "```json" + NL + json.dumps(obj, ensure_ascii=False, indent=2) + NL + "```"
    if mode == "constraints":
        text = "这是一段用于验证流程的示例文本，内容围绕题目主题展开，长度适中。"
        if seed % 100 < 30:  # 偶尔违反约束
            text += "很"
        return text
    if q.get("images"):  # 文档解析多模态题
        return "# 标题" + NL + NL + "这是模型识别出的正文段落，包含若干文字。" + NL + NL + "|列A|列B|" + NL + "|--|--|" + NL + "|1|2|"
    fill = "From the requirements, the model gives a structured answer with opening, body and closing."
    return (fill * 4)[:600]


def export_answers(out_path, mock):
    """施测完成后导出答案:
    1) 测试作答/<模型ID_名称>.md —— 每模型一份统一格式文档(人读, 每次运行覆盖为最新)
    2) results/answers/<时间戳>/ —— 每模型jsonl+作答汇总.md(机读与历史存档)
    返回 {"unified": 测试作答目录, "archive": 时间戳目录, "files": [模型文档名...]}
    """
    recs = [json.loads(l) for l in Path(out_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not recs:
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S") + ("_模拟" if mock else "")
    archive = BASE / "results" / "answers" / stamp
    unified = desktop_dir() / "测试作答"
    archive.mkdir(parents=True, exist_ok=True)
    unified.mkdir(exist_ok=True)
    by_model = {}
    for r in recs:
        by_model.setdefault((r["model_id"], r.get("model_name", "")), []).append(r)
    files = []
    for (mid, name), rows in sorted(by_model.items()):
        safe = (name or "模型").replace("/", "_").replace("\\", "_").replace(":", "_")
        rows = sorted(rows, key=lambda r: (r.get("question_id", ""), r.get("run", 1)))
        tin = sum((r.get("usage") or {}).get("prompt_tokens") or 0 for r in rows)
        tout = sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in rows)
        # ---- 机读 jsonl(存档目录) ----
        with open(archive / (mid + "_" + safe + ".jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "题目ID": r["question_id"], "能力维度": r["dim"], "轮次": r.get("run", 1),
                    "题目": NL.join(m["content"] if isinstance(m.get("content"), str)
                                    else "" for m in r["messages"]),
                    "回答": r.get("response", ""),
                    "输入tokens": (r.get("usage") or {}).get("prompt_tokens"),
                    "输出tokens": (r.get("usage") or {}).get("completion_tokens"),
                    "延迟秒": r.get("latency_s"), "时间": r.get("timestamp"),
                    "错误": r.get("error", ""),
                }, ensure_ascii=False) + NL)
        # ---- 统一格式 Markdown(测试作答/, 人读) ----
        md = ["# " + mid + " " + (name or "") + " · 测试作答文档", "",
              "- 运行模式：" + ("模拟" if mock else "正式"),
              "- 生成时间：" + time.strftime("%Y-%m-%d %H:%M:%S"),
              "- 作答题数：" + str(len(rows)) + "（含多轮）",
              "- tokens 合计：输入 " + str(tin) + " / 输出 " + str(tout), "",
              "---", ""]
        for r in rows:
            q = NL.join(m["content"] if isinstance(m.get("content"), str) else ""
                        for m in r["messages"])
            err = r.get("error") or ""
            md.append("## " + str(r["question_id"]) + " · " + str(r["dim"])
                      + " · 第" + str(r.get("run", 1)) + "轮")
            md.append("- 用时：" + str(r.get("latency_s", "-")) + "s　"
                      + "时间：" + str(r.get("timestamp", "-")))
            md.append("")
            md.append("### 题目")
            md.append(q)
            md.append("")
            md.append("### 回答")
            md.append(r.get("response") or ("（无回答：" + err + "）"))
            md.append("")
            md.append("---")
            md.append("")
        fname = mid + "_" + safe + ".md"
        (unified / fname).write_text(NL.join(md), encoding="utf-8")
        files.append(fname)
    (archive / "作答汇总.md").write_text(
        "# AI 作答记录（" + ("模拟" if mock else "正式") + "）" + NL + "生成时间："
        + time.strftime("%Y-%m-%d %H:%M:%S") + NL + NL
        + "统一格式文档见 测试作答/ 目录（每模型一份）" + NL,
        encoding="utf-8")
    return {"unified": str(unified), "archive": str(archive), "files": files}


def build_payload_messages(q, m):
    """把题库消息转成API载荷; 多模态题(vision模型)把图片转为base64 data URL附加到首条消息。"""
    msgs = [dict(x) for x in q["messages"]]
    imgs = q.get("images") or []
    if imgs and m.get("vision"):
        import base64
        parts = [{"type": "text", "text": msgs[0]["content"]}]
        for p in imgs:
            fp = BASE / p
            b64 = base64.b64encode(fp.read_bytes()).decode("ascii")
            mime = "image/png" if fp.suffix.lower() == ".png" else "image/jpeg"
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
        msgs[0] = {"role": msgs[0]["role"], "content": parts}
    return msgs


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="models.yaml")
    ap.add_argument("--mock", action="store_true", help="离线模拟, 不访问任何API")
    ap.add_argument("--out", default="results/results.jsonl")
    ap.add_argument("--models", default="", help="逗号分隔的模型ID过滤")
    ap.add_argument("--dims", default="", help="逗号分隔的能力维度过滤")
    ap.add_argument("--ids", default="", help="逗号分隔的题目ID过滤")
    ap.add_argument("--limit", type=int, default=0, help="最多施测条数(调试用)")
    args = ap.parse_args()

    cfg = load_yaml(BASE / args.config)
    defaults = cfg.get("defaults") or {}
    questions = load_questions()
    models = [m for m in cfg.get("models", []) if m.get("enabled", True)]
    if not models and args.mock:
        models = [dict(m) for m in MOCK_FALLBACK_MODELS]
        print("提示: models.yaml 未配置模型, 模拟模式使用内置虚拟模型(M01/M02/M03)")

    if args.models:
        keep = {x.strip() for x in args.models.split(",")}
        models = [m for m in models if m["id"] in keep]
    if args.dims:
        keep = {x.strip() for x in args.dims.split(",")}
        questions = [q for q in questions if q["dim"] in keep]
    if args.ids:
        keep = {x.strip().upper() for x in args.ids.split(",")}
        questions = [q for q in questions if q["id"].upper() in keep]

    out_path = BASE / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode_str = "MOCK" if args.mock else "正式"
    print("模型 " + str(len(models)) + " 个 | 题目 " + str(len(questions)) + " 道 | 模式: " + mode_str)

    n, errors, skipped = 0, 0, 0
    stop = False
    with open(out_path, "w", encoding="utf-8") as out:
        for m in models:
            if stop:
                break
            for q in questions:
                if stop:
                    break
                if q.get("images") and not m.get("vision"):
                    skipped += 1  # 多模态题仅对开启视觉能力的模型施测
                    continue
                runs = q.get("runs", defaults.get("runs", 1))
                for run in range(1, runs + 1):
                    n += 1
                    if args.limit and n > args.limit:
                        stop = True
                        break
                    rec = {"model_id": m["id"], "model_name": m["name"],
                           "question_id": q["id"], "dim": q["dim"], "run": run,
                           "messages": q["messages"],
                           "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
                    try:
                        if args.mock:
                            rec["response"] = mock_response(q, m["id"], run)
                            s1 = _seed(m["id"], q["id"], run)
                            s2 = _seed(m["id"], q["id"], "tok", run)
                            rec["usage"] = {"prompt_tokens": 220 + s1 % 380,
                                            "completion_tokens": 60 + s2 % 420}
                            rec["latency_s"] = round(0.8 + s1 % 500 / 100.0, 2)
                        else:
                            payload = build_payload_messages(q, m)
                            text, usage, latency = chat_with_retry(
                                m, payload,
                                m.get("temperature", defaults.get("temperature", 0.7)),
                                q.get("max_tokens", m.get("max_tokens", defaults.get("max_tokens", 4096))),
                                defaults.get("retries", 2))
                            rec["response"], rec["usage"], rec["latency_s"] = text, usage, latency
                    except Exception as e:
                        errors += 1
                        rec["response"], rec["error"] = "", str(e)
                    out.write(json.dumps(rec, ensure_ascii=False) + NL)
                    out.flush()
                    if n % 25 == 0:
                        print("  ...已施测 " + str(n) + " 条")

    manifest = {"mode": "mock" if args.mock else "live",
                "models": [m["id"] for m in models],
                "question_count": len(questions), "records": n - (1 if stop else 0),
                "errors": errors, "skipped_nonvision": skipped,
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    (BASE / "results" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    exp = export_answers(out_path, args.mock)
    print("完成: " + str(n - (1 if stop else 0)) + " 条记录, " + str(errors) + " 个错误, "
          + str(skipped) + " 条多模态跳过(非视觉模型) → " + str(out_path))
    if exp:
        print("统一格式作答文档(" + str(len(exp["files"])) + "份) → " + exp["unified"])


if __name__ == "__main__":
    main()
