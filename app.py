# -*- coding: utf-8 -*-
"""app.py — LLM 十二能力评测台（本地网页版）
启动: python app.py   （或双击 启动评测台.bat）
浏览器自动打开 http://127.0.0.1:8787
功能: 题库浏览与自测 / 一键施测+评分+生成Excel报告 / 结果雷达图看板
仅用 Python 标准库, 无需额外安装(跑测试仍需 openpyxl/pyyaml)。
"""
import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE = Path(__file__).resolve().parent
PORT = 8787

STATE = {"running": False, "log": "", "done": False, "ok": False, "mode": "", "started": ""}


def load_questions():
    qs = []
    for fp in sorted((BASE / "questions").glob("*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                qs.append(json.loads(line))
    return qs


def _probe_models():
    """正式施测前连通性预检: 任一启用模型可达即放行, 全部失败则中止。"""
    sys.path.insert(0, str(BASE))
    from runner import chat_once, load_yaml
    cfg = load_yaml(BASE / "models.yaml")
    models = [m for m in cfg.get("models", []) if m.get("enabled", True)]
    if not models:
        STATE["log"] += "! models.yaml 中没有启用的模型, 请先在「API接入」页配置\n"
        return False
    passed = 0
    for m in models:
        try:
            chat_once(dict(m, timeout_seconds=25),
                      [{"role": "user", "content": "连接测试，请只回复OK"}], 0.0, 16)
            STATE["log"] += f"· {m['id']} {m.get('name', '')} 连接OK\n"
            passed += 1
        except Exception as e:
            STATE["log"] += f"· {m['id']} {m.get('name', '')} 连接失败: {str(e)[:150]}\n"
    return passed > 0


def run_pipeline(mode, mock=False):
    """mode: mock=模拟全流程 | answers=仅AI作答 | full=作答+评分+报告 | score=仅评分+报告(用已有作答)"""
    import contextlib
    import importlib
    import io
    import traceback

    sys.path.insert(0, str(BASE))
    mode_names = {"mock": "模拟全流程", "answers": "AI自动作答",
                  "full": "作答+评分+报告", "score": "评分+报告"}
    STATE.update(running=True, done=False, ok=False, log="", mode=mode_names[mode],
                 started=time.strftime("%H:%M:%S"))
    if mode == "mock":
        steps = [("施测 runner.py", "runner", ["--mock"]),
                 ("评分 scorer.py", "scorer", ["--mock"]),
                 ("报告 workbook.py", "workbook", [])]
    elif mode == "answers":
        steps = [("AI作答 runner.py", "runner", [])]
    elif mode == "score":
        steps = [("评分 scorer.py", "scorer", ["--mock"] if mock else []),
                 ("报告 workbook.py", "workbook", [])]
    else:
        steps = [("施测 runner.py", "runner", []),
                 ("评分 scorer.py", "scorer", []),
                 ("报告 workbook.py", "workbook", [])]
    ok_all = True
    try:
        # 预导入全部模块(在重定向前完成模块级初始化)
        importlib.import_module("runner")
        importlib.import_module("scorer")
        importlib.import_module("workbook")
        if mode in ("answers", "full") and not _probe_models():
            raise SystemExit("没有模型通过连接测试，请在「API接入」页检查配置（可用行内「测连接」按钮）")
        for name, mod, args in steps:
            STATE["log"] += f"\n===== [{time.strftime('%H:%M:%S')}] {name} =====\n"
            m = importlib.reload(importlib.import_module(mod))  # 重载以读取最新题库/配置
            old_argv = sys.argv
            sys.argv = [mod + ".py"] + args
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    m.main()
            finally:
                sys.argv = old_argv
            STATE["log"] += buf.getvalue()
        if mode == "answers":
            STATE["log"] += ("\n===== 作答完成 ✔ 答案已存入 results/answers/ "
                             "（每模型一份jsonl + 作答汇总.md，评分可在网页外进行）=====")
        else:
            STATE["log"] += "\n===== 全部完成 ✔ 报告已生成: results/测试报告.xlsx ====="
    except SystemExit as e:  # 预检失败 / scorer 等主动 sys.exit
        STATE["log"] += f"\n!!! 流程中止: {e}"
        ok_all = False
    except Exception:
        STATE["log"] += "\n!!! 异常:\n" + traceback.format_exc()
        ok_all = False
    STATE.update(running=False, done=True, ok=ok_all)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif path == "/api/questions":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(load_questions(), ensure_ascii=False).encode("utf-8"))
        elif path == "/api/pkg":
            from runner import desktop_dir
            d = desktop_dir() / "评分包"
            files = sorted(f.name for f in d.iterdir()
                           if f.is_file() and f.suffix in (".md", ".csv")) if d.exists() else []
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"files": files}, ensure_ascii=False).encode("utf-8"))
        elif path.startswith("/pkg/"):
            name = Path(unquote(urlparse(self.path).path)).name
            from runner import desktop_dir
            d = desktop_dir() / "评分包"
            fp = d / name
            ctype = ("text/csv; charset=utf-8" if fp.suffix == ".csv"
                     else "text/markdown; charset=utf-8")
            if fp.exists() and fp.parent == d and fp.suffix in (".md", ".csv"):
                self._send(200, ctype, fp.read_bytes())
            else:
                self._send(404, "text/plain; charset=utf-8", "文件不存在".encode("utf-8"))
        elif path == "/api/answers":
            from runner import desktop_dir
            d = desktop_dir() / "测试作答"
            files = sorted(f.name for f in d.glob("*.md")) if d.exists() else []
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"files": files}, ensure_ascii=False).encode("utf-8"))
        elif path.startswith("/answers/"):
            name = Path(unquote(urlparse(self.path).path)).name
            from runner import desktop_dir
            d = desktop_dir() / "测试作答"
            fp = d / name
            if fp.exists() and fp.parent == d and fp.suffix == ".md":
                self._send(200, "text/markdown; charset=utf-8", fp.read_bytes())
            else:
                self._send(404, "text/plain; charset=utf-8", "文档不存在".encode("utf-8"))
        elif path.startswith("/img/"):
            name = Path(unquote(urlparse(self.path).path)).name
            d = BASE / "benchmarks_cache" / "omnidoc_images"
            fp = d / name
            if fp.exists() and fp.parent == d:
                ctype = "image/png" if fp.suffix.lower() == ".png" else "image/jpeg"
                self._send(200, ctype, fp.read_bytes())
            else:
                self._send(404, "text/plain; charset=utf-8", "图片不存在".encode("utf-8"))
        elif path == "/api/race/status":
            import copy
            import race
            st = copy.deepcopy(race.STATE)
            now = time.time()
            for m in (st.get("models") or {}).values():  # 当前题实时计时
                if m.get("cur_started"):
                    m["cur_elapsed"] = round(now - m["cur_started"], 1)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(st, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/config":
            sys.path.insert(0, str(BASE))
            from runner import load_yaml
            cfg = load_yaml(BASE / "models.yaml")
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"models": cfg.get("models", []),
                                   "judge": cfg.get("judge") or {}},
                                  ensure_ascii=False).encode("utf-8"))
        elif path == "/api/status":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(STATE, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/results":
            fp = BASE / "results" / "scored.jsonl"
            rows = []
            if fp.exists():
                rows = [json.loads(l) for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()]
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"exists": fp.exists(), "rows": rows}, ensure_ascii=False).encode("utf-8"))
        elif path == "/report":
            fp = BASE / "results" / "测试报告.xlsx"
            if fp.exists():
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95%E6%8A%A5%E5%91%8A.xlsx")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(404, "text/plain; charset=utf-8", "报告尚未生成".encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", "not found".encode())

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if path == "/api/config":
            import yaml
            sys.path.insert(0, str(BASE))
            from runner import load_yaml
            cfg = load_yaml(BASE / "models.yaml")
            if isinstance(body.get("models"), list):
                cfg["models"] = body["models"]
            if body.get("judge"):
                cfg["judge"] = body["judge"]
            try:
                (BASE / "models.yaml").write_text(
                    yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
                self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
            except Exception as e:
                self._send(500, "application/json; charset=utf-8",
                           json.dumps({"ok": False, "error": str(e)},
                                      ensure_ascii=False).encode("utf-8"))
            return
        if path == "/api/test":
            sys.path.insert(0, str(BASE))
            from runner import chat_once
            try:
                text, _, latency = chat_once(
                    dict(body, timeout_seconds=int(body.get("timeout_seconds") or 20)),
                    [{"role": "user", "content": "连接测试，请只回复OK"}], 0.0, 16)
                resp = {"ok": True, "latency": latency, "reply": (text or "")[:60]}
            except Exception as e:
                resp = {"ok": False, "error": str(e)[:300]}
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return
        if path == "/api/export":
            import race
            if STATE["running"] or race.STATE["running"]:
                self._send(409, "application/json; charset=utf-8",
                           json.dumps({"ok": False, "error": "任务运行中，请稍后再导出"},
                                      ensure_ascii=False).encode("utf-8"))
                return
            try:
                from export_eval import export
                r = export()
            except Exception as e:
                r = {"ok": False, "error": str(e)[:200]}
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(r, ensure_ascii=False).encode("utf-8"))
            return
        if path == "/api/race/start":
            import race
            if STATE["running"] or race.STATE["running"]:
                self._send(409, "application/json; charset=utf-8", b'{"error":"busy"}')
                return
            models = [m for m in body.get("models", []) if m]
            if not models and not body.get("mock"):
                self._send(400, "application/json; charset=utf-8",
                           b'{"error":"no models"}')
                return
            threading.Thread(
                target=race.run_race,
                kwargs={"mock": bool(body.get("mock")),
                        "limit": int(body.get("limit") or 0),
                        "model_ids": models,
                        "resume": bool(body.get("resume", True)),
                        "stability": body.get("stability") or None},
                daemon=True).start()
            self._send(200, "application/json; charset=utf-8", b'{"started":true}')
            return
        if path == "/api/race/stop":
            import race
            race.STATE["stop"] = True
            self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
            return
        if path != "/api/run":
            self._send(404, "text/plain", b"not found")
            return
        import race
        if STATE["running"] or race.STATE["running"]:
            self._send(409, "application/json; charset=utf-8",
                       b'{"error":"task running"}')
            return
        mode = body.get("mode", "mock")
        if mode not in ("mock", "answers", "full", "score"):
            self._send(400, "application/json; charset=utf-8", b'{"error":"bad mode"}')
            return
        threading.Thread(target=run_pipeline, args=(mode, bool(body.get("mock"))),
                         daemon=True).start()
        self._send(200, "application/json; charset=utf-8", b'{"started":true}')


PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>LLM 十二能力评测台</title>
<style>
:root{--p:#1B2A4A;--pl:#D6E4F0;--pos:#1B7D46;--n1:#F7F7F5;--n2:#8C8A84;--line:#E9E9E8}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei","Noto Sans SC",sans-serif;color:#37352F;background:#FAFAF9}
header{background:var(--p);color:#fff;padding:14px 28px;display:flex;align-items:center;gap:18px}
header h1{font-size:19px;font-weight:600}
header .sub{font-size:12px;opacity:.75}
nav{display:flex;gap:6px;margin-left:auto}
nav button{border:0;background:rgba(255,255,255,.12);color:#fff;padding:8px 18px;border-radius:8px;cursor:pointer;font-size:14px}
nav button.on{background:#fff;color:var(--p);font-weight:600}
main{max-width:1200px;margin:20px auto;padding:0 20px}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(27,42,74,.08);padding:18px 22px;margin-bottom:16px}
h2{font-size:16px;color:var(--p);margin-bottom:10px}
.layout{display:flex;gap:16px;align-items:flex-start}
.side{width:190px;flex:none}
.side button{display:flex;justify-content:space-between;width:100%;border:1px solid var(--line);background:#fff;padding:8px 12px;border-radius:8px;margin-bottom:6px;cursor:pointer;font-size:13px}
.side button.on{background:var(--p);color:#fff;border-color:var(--p)}
.qmain{flex:1;min-width:0}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.badge{font-size:11px;padding:2px 10px;border-radius:10px;background:var(--pl);color:var(--p)}
.badge.src{background:#E8F5E9;color:var(--pos)}
.badge.dim{background:#FEF9E7;color:#9C6500}
.prompt{white-space:pre-wrap;background:var(--n1);border-radius:10px;padding:14px 16px;font-size:14px;line-height:1.75}
.opt{display:block;width:100%;text-align:left;margin:6px 0;padding:9px 14px;border:1px solid var(--line);border-radius:8px;background:#fff;cursor:pointer;font-size:14px;line-height:1.6}
.opt.sel{border-color:var(--p);background:var(--pl)}
textarea{width:100%;min-height:90px;margin:8px 0;padding:10px 12px;border:1px solid var(--line);border-radius:8px;font-size:14px;font-family:inherit}
button.pri{background:var(--p);color:#fff;border:0;border-radius:8px;padding:9px 22px;font-size:14px;cursor:pointer}
button.sec{background:#fff;border:1px solid var(--p);color:var(--p);border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;margin-right:8px}
.result{margin-top:12px;padding:12px 16px;border-radius:10px;font-size:14px;line-height:1.8;display:none}
.result.ok{display:block;background:#E8F5E9;color:var(--pos)}
.result.bad{display:block;background:#FDEDEC;color:#C0392B}
.result.self{display:block;background:#FEF9E7;color:#7A5A00}
.pager{display:flex;gap:8px;align-items:center;margin-top:14px}
.progress{height:8px;background:var(--n1);border-radius:4px;overflow:hidden;flex:1}
.progress i{display:block;height:100%;background:var(--pos)}
.stat{display:flex;gap:26px;margin-bottom:14px;flex-wrap:wrap}
.stat b{font-size:22px;color:var(--p)}
.stat span{font-size:12px;color:var(--n2)}
pre.log{background:#101828;color:#9FE8BF;padding:14px;border-radius:10px;font-size:12px;line-height:1.6;max-height:420px;overflow:auto;white-space:pre-wrap}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:center}
th{background:var(--p);color:#fff;font-weight:500}
td.l,th.l{text-align:left}
tr:hover td{background:var(--n1)}
.warn{background:#FEF9E7;color:#7A5A00;padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:12px;display:none}
canvas{max-width:100%;display:block;margin:0 auto}
.jl{font-size:13px;color:var(--p);font-weight:600;margin:14px 0 8px;display:block}
.mrow{display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap;align-items:center}
.mrow input{padding:7px 9px;border:1px solid var(--line);border-radius:6px;font-size:13px}
.mrow .w1{width:72px}.mrow .w2{width:120px}.mrow .w3{width:225px}
.mrow label{font-size:12px;color:var(--n2);display:flex;gap:4px;align-items:center}
.mrow button.sec{padding:6px 10px}
.miss{border-color:#C0392B !important;background:#FDEDEC !important}
#cfgmsg.err{color:#C0392B}
.hidden{display:none}
</style>
</head>
<body>
<header>
  <h1>LLM 十二能力评测台</h1><span class="sub">题库自测 · 自动施测评分 · Excel报告</span>
  <nav>
    <button id="tab-q" class="on" onclick="go('q')">题库·自测</button>
    <button id="tab-x" onclick="go('x')">并行实测</button>
    <button id="tab-c" onclick="go('c')">API接入</button>
    <button id="tab-r" onclick="go('r')">运行测试</button>
    <button id="tab-v" onclick="go('v')">结果看板</button>
  </nav>
</header>
<main>

<!-- ============ 题库自测 ============ -->
<section id="page-q">
  <div class="card">
    <div class="stat">
      <div><b id="st-total">0</b><br><span>题库总数</span></div>
      <div><b id="st-done">0</b><br><span>我已作答</span></div>
      <div><b id="st-right">0</b><br><span>答对(客观题)</span></div>
      <div><b id="st-rate">–</b><br><span>客观题正确率</span></div>
    </div>
    <div class="layout">
      <div class="side" id="dims"></div>
      <div class="qmain">
        <div class="badges" id="qbadges"></div>
        <div id="qimgs"></div>
        <div class="prompt" id="qprompt"></div>
        <div id="qopts"></div>
        <textarea id="qinput" placeholder="在此输入你的答案（选择题也可直接点上方选项）"></textarea>
        <button class="pri" onclick="submitAns()">对答案</button>
        <button class="sec" onclick="selfGrade(10)">自评：答对了</button>
        <button class="sec" onclick="selfGrade(5)">自评：半对</button>
        <button class="sec" onclick="selfGrade(0)">自评：没答对</button>
        <div class="result" id="qresult"></div>
        <div class="pager">
          <button class="sec" onclick="step(-1)">← 上一题</button>
          <div class="progress"><i id="pbar"></i></div>
          <span id="pnum" style="font-size:12px;color:#8C8A84"></span>
          <button class="sec" onclick="step(1)">下一题 →</button>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- ============ 并行实测 ============ -->
<section id="page-x" class="hidden">
  <div class="card">
    <h2>多模型并行作答（各自独立推进 · 单题限时 · 断点续跑）</h2>
    <p style="font-size:13px;color:#8C8A84;margin-bottom:10px">
      每个模型一条独立流水线：做完一题立即下一题，互不等待；单题限时（含重试）到点自动跳下一题；
      模型连续失败自动熔断→冷却→探活复活，多轮失败标记掉线；中途停止/崩溃后再点开始自动<b>断点续跑</b>（只补缺失题目）。</p>
    <div class="mrow" style="align-items:center;flex-wrap:wrap">
      <label style="font-size:13px">题量</label>
      <input class="w1" id="x-limit" type="number" value="0" min="0" max="249" title="0=全部249题">
      <label style="font-size:13px">单题限时(秒)</label>
      <input class="w1" id="x-qto" type="number" value="240" min="30" max="1200">
      <label style="font-size:13px">重试</label>
      <input class="w1" id="x-retry" type="number" value="2" min="0" max="5">
      <label style="font-size:13px">总上限(分)</label>
      <input class="w1" id="x-total" type="number" value="960" min="10">
      <label style="font-size:13px"><input type="checkbox" id="x-resume" checked>断点续跑</label>
      <label style="font-size:13px"><input type="checkbox" id="x-mock">模拟模式</label>
    </div>
    <div class="mrow" style="align-items:center">
      <span style="font-size:13px;color:#8C8A84">模型：</span>
      <span id="x-models" style="display:flex;gap:12px;flex-wrap:wrap"></span>
    </div>
    <div style="margin:10px 0 4px">
      <button class="pri" onclick="raceStart()">▶ 开始作答</button>
      <button class="sec" onclick="raceStop()">■ 停止</button>
      <span id="x-msg" style="font-size:13px;margin-left:10px"></span>
    </div>
    <div class="progress" style="margin-top:8px"><i id="x-bar" style="width:0"></i></div>
    <div id="x-grid" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px"></div>
  </div>
  <div class="card"><h2>事件日志（超时 / 熔断冷却 / 探活复活）</h2>
    <div id="x-hist" style="font-size:13px;max-height:220px;overflow:auto;color:#37352F">（未开始）</div></div>
  <div class="card"><h2>完成汇总（按模型计费）</h2>
    <div id="x-sum" style="font-size:13px">（未开始）</div>
    <div style="margin-top:12px">
      <button class="pri" style="display:none" onclick="raceScore()">评分并生成Excel报告</button>
      <button class="sec" onclick="exportPkg('x-pkg')">📦 导出评分包</button>
      <span id="x-pkg" style="margin-left:8px;font-size:13px"></span>
    </div></div>
</section>

<!-- ============ API接入 ============ -->
<section id="page-c" class="hidden">
  <div class="card">
    <h2>API 接入（保存后写入 models.yaml，施测时直接使用）</h2>
    <p style="font-size:13px;color:#8C8A84;margin-bottom:6px">
      接口须为 OpenAI 兼容格式（/chat/completions）。带 <b>*</b> 为必填，其余选填；api_key 可填 <b>env:环境变量名</b> 或直接填 key（仅存本机 models.yaml）。单价单位：元/百万tokens，仅用于费用估算。</p>
    <label class="jl">被测模型</label>
    <div id="cfgmodels"></div>
    <button class="sec" onclick="addModelRow()">＋ 添加模型</button>
    <label class="jl">评审模型（选填，仅“LLM评审”类主观题使用；建议与被测模型不同厂商）</label>
    <div class="mrow">
      <input class="w3" id="j-url" placeholder="评审模型API地址(选填)">
      <input class="w3" id="j-key" placeholder="评审API Key或env:变量(选填)">
      <input class="w2" id="j-model" placeholder="评审接口model(选填)">
    </div>
    <div style="margin-top:16px">
      <button class="pri" onclick="saveConfig()">保存配置</button>
      <span id="cfgmsg" style="font-size:13px;margin-left:10px"></span>
    </div>
  </div>
</section>

<!-- ============ 运行测试 ============ -->
<section id="page-r" class="hidden">
  <div class="card">
    <h2>一键施测 → 三层评分 → 生成 Excel 报告</h2>
    <div class="warn" id="rwarn">正式施测会调用 models.yaml 中启用的模型与评审模型，产生真实 API 费用。请先确认密钥已配置（api_key 支持 env:环境变量）。</div>
    <p style="font-size:13px;color:#8C8A84;margin-bottom:12px">
      模拟模式不联网、不花钱，用确定性假回答验证全链路；正式模式按题库对每个模型施测（生成类题目自动多轮）。
    </p>
    <button class="pri" onclick="runPipe('mock')">▶ ① 模拟全流程（不花钱）</button>
    <button class="pri" style="background:#1B7D46" onclick="runPipe('answers')">▶ ② AI自动作答（不评分，答案自动存入 results/answers/）</button>
    <button class="pri" style="background:#C0392B" onclick="runPipe('full')">▶ ③ 作答+评分+Excel报告</button>
    <a id="dlreport" class="sec" style="display:none;text-decoration:none;line-height:32px" href="/report" download>⬇ 下载测试报告.xlsx</a>
    <pre class="log" id="rlog" style="margin-top:14px">等待启动…</pre>
  </div>
</section>

<!-- ============ 结果看板 ============ -->
<section id="page-v" class="hidden">
  <div class="card"><h2>模型 × 十二能力 得分率</h2><div id="vtable">暂无结果，请先在「运行测试」页执行。</div></div>
  <div class="card"><h2>十三维雷达图（前4个模型）</h2><canvas id="radar" width="760" height="560"></canvas></div>
  <div class="card"><h2>测试作答文档（每模型一份·统一格式·位于桌面\测试作答\）</h2>
    <div id="vdocs" style="font-size:14px;line-height:2">暂无（运行测试或并行实测后生成）</div></div>
  <div class="card"><h2>导出评分包（题目 + 官方答案 + 各模型作答，交人工评分）</h2>
    <p style="font-size:13px;color:#8C8A84;margin-bottom:10px">
      导出到桌面\评分包\ 三件套：①题目与官方答案.md（官方标注答案，无标准答案的题给官方判分口径）②每模型一份《作答_》文档（题号一一对应）③人工评分表.csv（预留打分列）。取自最近一次施测/竞测结果。</p>
    <button class="pri" onclick="exportPkg('v-pkg')">📦 导出评分包</button>
    <span id="v-pkg" style="margin-left:10px;font-size:14px"></span></div>
</section>

</main>
<script>
const CANON = ["语言理解","信息提取","创造力","指令遵循","复杂推理","约束处理","结构化输出","长文本组织","上下文利用","信息检索","评估","错误识别"];
let DIMS = CANON.slice();
let QS = [], cur = 0, dimFilter = "全部";
let store = JSON.parse(localStorage.getItem("ev_answers") || "{}");

function saveStore(){ localStorage.setItem("ev_answers", JSON.stringify(store)); }
function norm(s){
  return (s||"").replace(/[\s，。、；：？！“”‘’（）【】《》—…·,.;:?!\n\t\r ()\[\]{}"'_\/-]/g,"").toLowerCase();
}
function stem(q){
  if(q.messages.length===1) return q.messages[0].content;
  return q.messages.map((m,i)=>`【第${i+1}轮·${m.role==="user"?"用户":"助手"}】${m.content}`).join("\n");
}
function optsOf(q){
  if((q.scoring||{}).mode!=="mc") return null;
  const lines = stem(q).split("\n"); const out=[];
  for(const l of lines){ const m=l.trim().match(/^([A-D])[．.、：:\s，,]\s*(.*)$/); if(m) out.push({k:m[1],t:m[2]}); }
  return out.length>=2?out:null;
}
function visibleIdx(){
  return QS.map((q,i)=>i).filter(i=>dimFilter==="全部"||QS[i].dim===dimFilter);
}

function renderDims(){
  const el=document.getElementById("dims"); el.innerHTML="";
  const mk=(name,count)=>{const b=document.createElement("button");
    b.className=dimFilter===name?"on":""; b.innerHTML=`<span>${name}</span><span>${count}</span>`;
    b.onclick=()=>{dimFilter=name; const vis=visibleIdx(); cur=vis.length?vis[0]:0; renderDims(); renderQ();}; el.appendChild(b);};
  mk("全部",QS.length);
  for(const d of DIMS) mk(d,QS.filter(q=>q.dim===d).length);
}
function renderQ(){
  const vis=visibleIdx(); if(!vis.length){document.getElementById("qprompt").textContent="该维度暂无题目";return;}
  if(!vis.includes(cur)) cur=vis[0];
  const q=QS[cur], o=optsOf(q), sel=(store[q.id||q.id]||{}).sel;
  document.getElementById("qbadges").innerHTML =
    `<span class="badge">${q.id}</span><span class="badge dim">${q.dim}</span>`+
    `<span class="badge">${q.sub||""}</span><span class="badge src">${q.src||""}</span>`+
    `<span class="badge">难度${"★".repeat(q.diff||3)}</span><span class="badge">${q.scoring.mode==="mc"?"客观":"主观/程序"}</span>`;
  document.getElementById("qprompt").textContent = stem(q);
  const imbox=document.getElementById("qimgs");
  imbox.innerHTML=(q.images||[]).map(p=>
    `<img src="/img/${encodeURIComponent(p.split("/").pop())}" style="max-width:100%;max-height:430px;border:1px solid #E9E9E8;border-radius:8px;margin:4px 0" alt="题目图片">`).join("");
  const oe=document.getElementById("qopts"); oe.innerHTML="";
  document.getElementById("qinput").style.display = o?"none":"block";
  document.getElementById("qinput").value = (store[q.id]||{}).txt||"";
  if(o) for(const x of o){const b=document.createElement("button");
    b.className="opt"+(sel===x.k?" sel":""); b.textContent=`${x.k}．${x.t}`;
    b.onclick=()=>{store[q.id]=store[q.id]||{}; store[q.id].sel=(store[q.id].sel===x.k?null:x.k); saveStore(); renderQ();};
    oe.appendChild(b);}
  const r=store[q.id]||{};
  const box=document.getElementById("qresult");
  box.className="result"; box.style.display="none";
  if(r.score!=null){box.className="result "+(r.score>=8?"ok":r.score>=5?"self":"bad"); box.style.display="block";
    box.innerHTML=`上次结果：<b>${r.score}/10</b>　参考答案：${r.ref||"见评分要点"}　<span style="color:#8C8A84">${r.note||""}</span>`;}
  const pos=vis.indexOf(cur)+1;
  document.getElementById("pnum").textContent=`${pos}/${vis.length}`;
  document.getElementById("pbar").style.width=(pos/vis.length*100)+"%";
}
function step(d){const vis=visibleIdx(); if(!vis.length)return;
  let pos=vis.indexOf(cur); pos=(pos+d+vis.length)%vis.length; cur=vis[pos]; renderQ();}
function submitAns(){
  const q=QS[cur]; const o=optsOf(q); const st=store[q.id]=store[q.id]||{};
  const box=document.getElementById("qresult"); let score=null,note="",ref=q.ref||"";
  const mode=(q.scoring||{}).mode;
  if(o){
    const sel=st.sel; if(!sel){alert("请先点选一个选项");return;}
    const ans=(q.scoring.answer||"");
    score=(sel===ans)?10:0; note=`你选 ${sel}，正确答案 ${ans}`; st.txt=sel;
  }else{
    const txt=document.getElementById("qinput").value; st.txt=txt;
    if(mode==="exact"){
      const answers=q.scoring.answers||[q.scoring.answer];
      const hit=answers.filter(a=>norm(txt).includes(norm(a))).length;
      score=Math.round(10*hit/answers.length); note=`要点命中 ${hit}/${answers.length}`;
    }else if(mode==="numeric"){
      const nums=(txt.match(/-?\d+(\.\d+)?/g)||[]).map(Number);
      const v=q.scoring.value; const hit=nums.some(n=>Math.abs(n-v)<=0.01*Math.max(1,Math.abs(v)));
      score=hit?10:0; note=`正确数值 ${v}`;
    }else{
      box.className="result self"; box.style.display="block";
      box.innerHTML=`本题为主观/程序题，请对照参考答案与评分要点<b>自评</b>：<br>参考答案/要点：${q.ref||""}<br><span style="color:#8C8A84">评分要点：${q.points||""}</span>`;
      st.ref=(q.ref||"").slice(0,80); saveStore(); statUI(); return;
    }
  }
  st.score=score; st.note=note; st.ref=(ref||"").slice(0,80); saveStore();
  box.className="result "+(score>=8?"ok":"bad"); box.style.display="block";
  box.innerHTML=`<b>${score>=8?"✔ 答对了":"✘ 不对"}</b>（${score}/10）　${note}<br>解析：${q.ref||""}<br><span style="color:#8C8A84">来源：${q.src||""} ${q.src_detail||""}</span>`;
  statUI();
}
function selfGrade(s){
  const q=QS[cur]; const st=store[q.id]=store[q.id]||{};
  st.score=s; st.note="主观题自评"; st.ref=(q.ref||"").slice(0,80); saveStore(); renderQ(); statUI();
}
function statUI(){
  const done=Object.values(store).filter(v=>v.score!=null);
  const obj=done.filter(v=>v.note&&!v.note.includes("自评"));
  document.getElementById("st-total").textContent=QS.length;
  document.getElementById("st-done").textContent=done.length;
  document.getElementById("st-right").textContent=obj.filter(v=>v.score>=8).length;
  document.getElementById("st-rate").textContent=obj.length?Math.round(obj.filter(v=>v.score>=8).length/obj.length*100)+"%":"–";
}
async function loadQ(){
  QS=await (await fetch("/api/questions")).json();
  QS.sort((a,b)=>a.id<b.id?-1:1);
  const extra=[...new Set(QS.map(q=>q.dim))].filter(d=>!CANON.includes(d));
  DIMS=CANON.concat(extra);
  renderDims(); renderQ(); statUI();
}

/* ---------- API接入 ---------- */
let CFG={models:[],judge:{}};
function esc(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;");}
function modelRow(m){
  const p=(m.pricing||{});
  return `<div class="mrow">
    <input class="w1" value="${esc(m.id)}" data-k="id" placeholder="模型ID*" title="必填：唯一编号，如 M01">
    <input class="w2" value="${esc(m.name)}" data-k="name" placeholder="填入模型名称*" title="必填">
    <input class="w3" value="${esc(m.base_url)}" data-k="base_url" placeholder="填入API地址(OpenAI兼容)*" title="必填：如 https://api.xxx.com/v1">
    <input class="w3" value="${esc(m.api_key)}" data-k="api_key" placeholder="填入API Key*（或env:环境变量）" title="必填">
    <input class="w2" value="${esc(m.model)}" data-k="model" placeholder="填入接口model名*" title="必填：调用时的 model 参数">
    <input class="w1" value="${m.temperature==null?"":m.temperature}" data-k="temperature" placeholder="温度0~2(选填)" title="选填，范围0~2，默认0.7">
    <input class="w1" value="${p.input_per_m==null?"":p.input_per_m}" data-k="in_price" placeholder="输入单价(选填)" title="选填，元/百万tokens，≥0">
    <input class="w1" value="${p.output_per_m==null?"":p.output_per_m}" data-k="out_price" placeholder="输出单价(选填)" title="选填，元/百万tokens，≥0">
    <input class="w2" value="${esc(m.vendor)}" data-k="vendor" placeholder="厂商(选填)">
    <label title="勾选后才参与文档解析图片题"><input type="checkbox" data-k="vision" ${m.vision?"checked":""}>视觉</label>
    <label title="勾选后参与施测"><input type="checkbox" data-k="enabled" ${m.enabled!==false?"checked":""}>启用</label>
    <button class="sec" onclick="testConn(this)">测连接</button>
    <button class="sec" onclick="this.parentNode.remove()">删</button>
  </div>`;
}
function renderCfg(){
  const box=document.getElementById("cfgmodels");
  box.innerHTML=(CFG.models||[]).map(modelRow).join("")||
    '<span style="font-size:13px;color:#8C8A84">暂无模型，点击下方「添加模型」</span>';
}
function addModelRow(){
  // 自动编号: 基于界面现有行的ID取下一个可用 Mxx
  const used=[...document.querySelectorAll("#cfgmodels [data-k=id]")].map(i=>i.value.trim()).filter(Boolean);
  let n=used.length+1; while(used.includes("M"+String(n).padStart(2,"0")))n++;
  const box=document.getElementById("cfgmodels");
  if(box.querySelector(".mrow")===null) box.innerHTML="";
  box.insertAdjacentHTML("beforeend", modelRow({id:"M"+String(n).padStart(2,"0")}));
}
const jv=id=>document.getElementById(id).value.trim();
function collectCfg(){
  const models=[...document.querySelectorAll("#cfgmodels .mrow")].map(row=>{
    const g=k=>{const el=row.querySelector('[data-k="'+k+'"]');
      return el?(el.type==="checkbox"?el.checked:el.value.trim()):null;};
    const m={id:g("id"),name:g("name"),vendor:g("vendor"),base_url:g("base_url"),
             api_key:g("api_key"),model:g("model"),enabled:g("enabled"),vision:g("vision")};
    if(g("temperature")!==""){const t=parseFloat(g("temperature"));if(!isNaN(t))m.temperature=t;}
    const ip=g("in_price"),op=g("out_price");
    if(ip!==""||op!=="")m.pricing={input_per_m:parseFloat(ip||"0")||0,output_per_m:parseFloat(op||"0")||0};
    return m;});
  const judge={base_url:jv("j-url"),api_key:jv("j-key"),model:jv("j-model"),temperature:0.0};
  return {models,judge};
}
async function loadConfig(){
  try{CFG=await (await fetch("/api/config")).json();}catch(e){CFG={models:[],judge:{}};}
  document.getElementById("j-url").value=(CFG.judge||{}).base_url||"";
  document.getElementById("j-key").value=(CFG.judge||{}).api_key||"";
  document.getElementById("j-model").value=(CFG.judge||{}).model||"";
  renderCfg();
}
async function saveConfig(){
  const msg=document.getElementById("cfgmsg");
  msg.classList.remove("err");
  // ---- 留空/范围校验: 红框+文字提示 ----
  const REQUIRED=[["id","模型ID"],["name","模型名称"],["base_url","API地址"],["api_key","API Key"],["model","接口model"]];
  const problems=[];
  const rows=[...document.querySelectorAll("#cfgmodels .mrow")];
  rows.forEach((row,ri)=>{
    const no=rows.length>1?("第"+(ri+1)+"行 "):"";
    REQUIRED.forEach(([k,label])=>{
      const el=row.querySelector('[data-k="'+k+'"]');
      if(el && !el.value.trim()){
        el.classList.add("miss");
        el.oninput=()=>el.classList.remove("miss");
        problems.push(no+"缺"+label);
      }
    });
    const t=row.querySelector('[data-k="temperature"]');
    if(t && t.value.trim() && (isNaN(+t.value)||+t.value<0||+t.value>2)){
      t.classList.add("miss"); t.oninput=()=>t.classList.remove("miss");
      problems.push(no+"温度需在0~2之间");
    }
    ["in_price","out_price"].forEach(k=>{
      const el=row.querySelector('[data-k="'+k+'"]');
      if(el && el.value.trim() && (isNaN(+el.value)||+el.value<0)){
        el.classList.add("miss"); el.oninput=()=>el.classList.remove("miss");
        problems.push(no+"单价需为≥0的数字");
      }
    });
  });
  if(problems.length){
    msg.textContent="✘ 请补全: "+problems.slice(0,4).join("；")+(problems.length>4?" 等":"");
    msg.classList.add("err");
    setTimeout(()=>{msg.textContent="";},4000);
    return;
  }
  msg.textContent="保存中…";
  try{
    const r=await fetch("/api/config",{method:"POST",headers:{'Content-Type':'application/json'},
      body:JSON.stringify(collectCfg())});
    msg.textContent=r.ok?"✔ 已保存到 models.yaml":"✘ 保存失败";
  }catch(e){msg.textContent="✘ 网络异常";}
  setTimeout(()=>msg.textContent="",3000);
}
async function testConn(btn){
  const row=btn.parentNode;
  const g=k=>{const el=row.querySelector('[data-k="'+k+'"]');return el?el.value.trim():"";};
  const old=btn.textContent; btn.textContent="测试中…"; btn.disabled=true;
  try{
    const r=await fetch("/api/test",{method:"POST",headers:{'Content-Type':'application/json'},
      body:JSON.stringify({base_url:g("base_url"),api_key:g("api_key"),model:g("model"),timeout_seconds:20})});
    const d=await r.json();
    btn.textContent=d.ok?("✔ "+Math.round(d.latency*100)/100+"s"):"✘ 失败";
    btn.title=d.ok?(d.reply||"OK"):(d.error||"").slice(0,220);
  }catch(e){btn.textContent="✘ 异常";}
  setTimeout(()=>{btn.textContent=old;btn.disabled=false;},3000);
}

/* ---------- 运行测试 ---------- */
async function runPipe(mode){
  document.getElementById("rwarn").style.display=(mode==="mock")?"none":"block";
  const r=await fetch("/api/run",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode})});
  if(r.status===409){alert("已有任务在运行");return;}
  poll();
}
let pollTimer=null;
async function poll(){
  clearTimeout(pollTimer);
  const s=await (await fetch("/api/status")).json();
  document.getElementById("rlog").textContent=s.log||"(运行中…)";
  document.getElementById("rlog").scrollTop=1e9;
  const dl=document.getElementById("dlreport");
  dl.style.display=(s.done&&s.ok)?"inline-block":"none";
  if(s.running||!s.done){pollTimer=setTimeout(poll,1200);}
}

/* ---------- 结果看板 ---------- */
async function loadV(){
  const d=await (await fetch("/api/results")).json();
  if(!d.exists||!d.rows.length)return;
  const rows=d.rows;
  const models=[...new Set(rows.map(r=>r.model_id))].sort();
  const tbl=document.createElement("table");
  let h="<tr><th class='l'>模型</th>"+DIMS.map(x=>`<th>${x.slice(0,3)}</th>`).join("")+"<th>综合</th><th>记录</th><th>费用(元)</th></tr>";
  const series={};
  for(const m of models){
    const mr=rows.filter(r=>r.model_id===m);
    const by={}; let tot=0,n=0,cost=0;
    for(const r of mr){by[r.dim]=by[r.dim]||[];by[r.dim].push(r.score/r.full);tot+=r.score/r.full;n++;cost+=(r.cost||0)+(r.judge_cost||0);}
    series[m]=DIMS.map(d2=>{const a=by[d2];return a?a.reduce((x,y)=>x+y,0)/a.length:null;});
    h+=`<tr><td class='l'><b>${m}</b> ${mr[0].model_name||""}</td>`+
      DIMS.map(d2=>{const v=series[m][DIMS.indexOf(d2)];return v==null?"<td>–</td>":`<td>${Math.round(v*100)}%</td>`;}).join("")+
      `<td><b>${Math.round(tot/n*100)}%</b></td><td>${n}</td><td>${cost.toFixed(2)}</td></tr>`;
  }
  tbl.innerHTML=h; document.getElementById("vtable").innerHTML=""; document.getElementById("vtable").appendChild(tbl);
  drawRadar(models.map(m=>({name:(rows.find(r=>r.model_id===m).model_name)||m,vals:series[m]})));
  fetch("/api/answers").then(r=>r.json()).then(d=>{
    const el=document.getElementById("vdocs");
    if(!el)return;
    el.innerHTML=(d.files||[]).length?
      d.files.map(f=>`<div>📄 <a href="/answers/${encodeURIComponent(f)}" target="_blank">${esc(f)}</a></div>`).join(""):
      "暂无（运行测试或并行实测后生成）";
  }).catch(()=>{});
}
function drawRadar(series){
  const c=document.getElementById("radar"),x=c.getContext("2d");
  const cx=c.width/2,cy=c.height/2+8,R=Math.min(c.width,c.height)/2-70;
  const colors=["#1B2A4A","#1B7D46","#D4820A","#C0392B"];
  x.clearRect(0,0,c.width,c.height);
  for(let g=1;g<=4;g++){
    x.beginPath();
    for(let i=0;i<=12;i++){const a=-Math.PI/2+i*2*Math.PI/12;const r=R*g/4;
      i?x.lineTo(cx+r*Math.cos(a),cy+r*Math.sin(a)):x.moveTo(cx+r*Math.cos(a),cy+r*Math.sin(a));}
    x.strokeStyle="#E9E9E8";x.stroke();}
  x.font="12px sans-serif";x.fillStyle="#37352F";x.textAlign="center";
  for(let i=0;i<12;i++){const a=-Math.PI/2+i*2*Math.PI/12;
    x.fillText(DIMS[i],cx+(R+26)*Math.cos(a),cy+(R+26)*Math.sin(a)+4);
    x.beginPath();x.moveTo(cx,cy);x.lineTo(cx+R*Math.cos(a),cy+R*Math.sin(a));x.strokeStyle="#F0F0EE";x.stroke();}
  series.slice(0,4).forEach((s,k)=>{
    x.beginPath();
    for(let i=0;i<=12;i++){const a=-Math.PI/2+(i%12)*2*Math.PI/12;const v=s.vals[i%12];const r=R*(v==null?0:Math.min(1,v));
      i?x.lineTo(cx+r*Math.cos(a),cy+r*Math.sin(a)):x.moveTo(cx+r*Math.cos(a),cy+r*Math.sin(a));}
    x.strokeStyle=colors[k];x.lineWidth=2;x.stroke();
    const lx=40,ly=30+k*24;x.fillStyle=colors[k];x.fillRect(lx,ly-10,26,4);x.font="13px sans-serif";x.textAlign="left";
    x.fillText(s.name,lx+34,ly);});
}

function go(t){
  for(const k of ["q","x","c","r","v"]){document.getElementById("page-"+k).classList.toggle("hidden",k!==t);
    document.getElementById("tab-"+k).classList.toggle("on",k===t);}
  if(t==="v")loadV();
  if(t==="c")loadConfig();
  if(t==="x"){loadRaceModels();pollRace();}
}
loadQ(); poll();

/* ---------- 并行作答(独立流水线) ---------- */
let raceTimer=null;
async function loadRaceModels(){
  let c; try{ c=await (await fetch("/api/config")).json(); }catch(e){return;}
  const box=document.getElementById("x-models");
  const list=(c.models||[]).filter(m=>m.enabled!==false);
  box.innerHTML=list.map(m=>`<label style="font-size:13px"><input type="checkbox" value="${esc(m.id)}" checked> ${esc(m.id)} ${esc(m.name)}</label>`).join("")||'<span style="font-size:13px;color:#8C8A84">未配置模型：正式模式请到「API接入」页添加；模拟模式可直接开始</span>';
}
async function raceStart(){
  const models=[...document.querySelectorAll("#x-models input:checked")].map(i=>i.value);
  if(!models.length && !document.getElementById("x-mock").checked){
    alert("请先到「API接入」页配置并启用模型");return;}
  const body={mock:document.getElementById("x-mock").checked,
    limit:parseInt(document.getElementById("x-limit").value||"0"),models,
    resume:document.getElementById("x-resume").checked,
    stability:{question_timeout:parseInt(document.getElementById("x-qto").value||"240"),
               retries:parseInt(document.getElementById("x-retry").value||"2"),
               max_total_minutes:parseInt(document.getElementById("x-total").value||"960")}};
  const r=await fetch("/api/race/start",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const msg=document.getElementById("x-msg");
  msg.textContent=r.ok?"作答进行中…":(r.status===409?"已有任务在运行":"启动失败");
  if(r.ok){clearTimeout(raceTimer);pollRace();}
}
async function raceStop(){await fetch("/api/race/stop",{method:"POST"});}
const BADGE={running:["⏳ 作答中","#D4820A"],cooldown:["❄ 熔断冷却","#8C8A84"],
  offline:["⚠ 掉线","#C0392B"],done:["✔ 完成","#1B7D46"],ok:["· 就绪","#1B7D46"],
  stopped:["⏹ 已停止","#8C8A84"],timeout_total:["⏱ 总时到","#C0392B"],crashed:["✘ 崩溃","#C0392B"]};
async function pollRace(){
  clearTimeout(raceTimer);
  let d; try{ d=await (await fetch("/api/race/status")).json(); }catch(e){return;}
  document.getElementById("x-bar").style.width=(d.total?Math.round(d.finished/d.total*100):0)+"%";
  document.getElementById("x-msg").textContent=d.running?
    (`进行中 ${d.finished}/${d.total}`):(d.summary?"已完成":(d.error?"出错: "+d.error:""));
  const ms=d.models||{};
  document.getElementById("x-grid").innerHTML=Object.entries(ms).map(([mid,m])=>{
    const [bt,bc]=BADGE[m.status]||["·"+m.status,"#8C8A84"];
    const curQ=m.cur_qid?`当前 ${m.cur_qid} · ${m.cur_elapsed||0}s`:(m.status==="done"?"":"等待中");
    const pct=m.total?Math.round((m.done+m.errors+m.skipped+m.resumed)/m.total*100):0;
    return `<div style="border:1px solid #E9E9E8;border-radius:10px;padding:10px 14px;min-width:200px;flex:1;max-width:340px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b>${mid}</b><span style="color:${bc};font-size:12px;font-weight:600">${bt}</span></div>
      <div style="font-size:12px;color:#8C8A84;margin:3px 0">${esc(m.name)} · ${curQ}</div>
      <div class="progress" style="height:6px"><i style="width:${pct}%"></i></div>
      <div style="font-size:12px;color:#8C8A84;margin-top:3px">
        ✓${m.done} ✘${m.errors} 跳${m.skipped} 续${m.resumed}/${m.total} · 均${m.avg||"-"}s · ¥${(m.cost||0).toFixed(3)}
        ${m.note?`<br><span style="color:#D4820A">${esc(m.note)}</span>`:""}</div></div>`;
  }).join("");
  const ev=d.events||[];
  if(ev.length){
    document.getElementById("x-hist").innerHTML=ev.slice(-40).reverse().map(e=>
      `<div><span style="color:#8C8A84">${e.t}</span> <b>${e.mid||""}</b> ${esc(e.qid||"")} ${esc(e.kind)}${e.kind==="cooldown"||e.kind==="offline"?esc(" "+(e.qid||"")):""}</div>`).join("");
  } else if(!d.running && !d.summary){
    document.getElementById("x-hist").textContent="（未开始）";
  }
  if(d.summary){
    document.getElementById("x-sum").innerHTML="<table><tr><th class='l'>模型</th><th>状态</th><th>答完</th><th>超时/错</th><th>平均</th><th>输入tok</th><th>输出tok</th><th>费用(元)</th></tr>"+
      Object.entries(d.summary.totals).map(([mid,t])=>{
        const [bt]=BADGE[t.status]||[t.status];
        return `<tr><td class='l'><b>${mid}</b> ${esc(t.name)}</td><td>${bt}</td><td>${t.done}</td><td>${t.errors}</td><td>${t.avg||"-"}s</td><td>${t.tin}</td><td>${t.tout}</td><td>${(t.cost||0).toFixed(4)}</td></tr>`;}).join("")+
      `</table><div style='font-size:12px;color:#8C8A84;margin-top:6px'>共 ${d.summary.records} 条作答${d.summary.resumed?` · 断点续跑复用 ${d.summary.resumed} 条`:""} · 统一格式文档: 桌面\测试作答\</div>`+
      ((d.summary.answers&&d.summary.answers.files||[]).length?
        "<div style='margin-top:8px;font-size:14px'>"+d.summary.answers.files.map(f=>
          `<a href="/answers/${encodeURIComponent(f)}" target="_blank" style="margin-right:14px">📄 ${esc(f)}</a>`).join("")+"</div>":"");
    [...document.querySelectorAll("#page-x button.pri")].find(b=>b.textContent.includes("评分"))?.style.setProperty("display","inline-block");
  }
  if(d.running) raceTimer=setTimeout(pollRace,1200);
}
async function raceScore(){
  const mock=document.getElementById("x-mock").checked;
  const r=await fetch("/api/run",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:"score",mock:mock})});
  if(r.ok){go("r");poll();}else if(r.status===409){alert("已有任务在运行");}
}
async function exportPkg(targetId){
  const el=document.getElementById(targetId);
  el.innerHTML="导出中…";
  try{
    const r=await fetch("/api/export",{method:"POST"});
    const d=await r.json();
    if(d.ok){
      el.innerHTML=d.files.map(f=>
        `<a href="/pkg/${encodeURIComponent(f)}" target="_blank" style="margin-right:12px">📦 ${esc(f)}</a>`).join("")
        +`<span style="color:#8C8A84;font-size:12px">（题目${d.questions}道·模型${d.models}个）</span>`;
    }else{el.innerHTML="✘ "+(d.error||"导出失败");}
  }catch(e){el.innerHTML="✘ 网络异常";}
}
</script>
</body>
</html>
"""


def main():
    url = f"http://127.0.0.1:{PORT}"
    print(f"评测台启动中: {url}  (Ctrl+C 退出)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError as e:
        print(f"端口 {PORT} 被占用({e})，请修改 app.py 中的 PORT 后重试")


if __name__ == "__main__":
    main()
