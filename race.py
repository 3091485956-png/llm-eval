# -*- coding: utf-8 -*-
"""race.py — 多模型并行独立作答引擎(非竞速)
每个模型一条独立流水线: 做完一题立刻下一题, 互不等待。
单题限时 question_timeout(默认240s, 含重试), 到点强制跳下一题;
连续失败熔断→冷却→探活复活(最多revive_probes轮)→掉线; 全场总时长上限; 断点续跑。
产出: results/results.jsonl(兼容 scorer/workbook/export_eval) + 桌面测试作答。
用法: python race.py --mock --limit 5 --models M01,M02
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FTimeout
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WRITE_LOCK = threading.Lock()

STATE = {"running": False, "stop": False, "started": "", "mode": "",
         "models": {},           # mid -> {name,total,qidx,cur_qid,cur_started,done,errors,skipped,resumed,status,note,elapsed_sum,tin,tout,cost}
         "finished": 0, "total": 0, "events": [], "summary": None, "error": "",
         "finished_at": ""}

STABILITY_DEFAULTS = {"question_timeout": 240, "retries": 2,
                      "max_total_minutes": 960, "breaker_fails": 5,
                      "cooldown_seconds": 180, "revive_probes": 3}


def _seed(*parts):
    import hashlib
    return int(hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest(), 16)


def _cost(in_t, out_t, p):
    if not p or in_t is None or out_t is None:
        return 0.0
    return round(in_t / 1e6 * float(p.get("input_per_m", 0))
                 + out_t / 1e6 * float(p.get("output_per_m", 0)), 4)


def _event(kind, mid="", qid=""):
    ev = {"t": time.strftime("%H:%M:%S"), "kind": kind, "mid": mid, "qid": qid}
    STATE["events"].append(ev)
    if len(STATE["events"]) > 400:
        del STATE["events"][:200]


def _probe(m):
    """探活: 极小请求, 通了返回True"""
    from runner import chat_once
    cfg = dict(m, timeout_seconds=30)
    text, _, _ = chat_once(cfg, [{"role": "user", "content": "1"}], 0, 8)
    return bool((text or "").strip())


def run_race(mock=False, limit=0, model_ids=None, dims=None, out_path=None,
             resume=True, stability=None):
    from runner import (build_payload_messages, chat_with_retry, export_answers,
                        load_questions, load_yaml, mock_response)
    cfg = load_yaml(BASE / "models.yaml")
    stab = dict(STABILITY_DEFAULTS)
    stab.update(cfg.get("stability") or {})
    stab.update(stability or {})
    defaults = cfg.get("defaults") or {}
    models = [m for m in cfg.get("models", []) if m.get("enabled", True)]
    if model_ids:
        keep = set(model_ids)
        models = [m for m in models if m["id"] in keep]
    if not models:
        if mock:
            from runner import MOCK_FALLBACK_MODELS
            models = [dict(m) for m in MOCK_FALLBACK_MODELS]
        else:
            STATE["error"] = "没有可用模型(请在API接入页配置并启用)"
            return
    price = {m["id"]: m.get("pricing") or None for m in models}
    qs = load_questions()
    if dims:
        keep = set(dims)
        qs = [q for q in qs if q["dim"] in keep]
    qs = [q for q in qs if not q.get("images") or any(m.get("vision") for m in models)]
    if limit:
        qs = qs[: int(limit)]

    out_path = Path(out_path) if out_path else BASE / "results" / "results.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- 断点续跑: 载入已成功记录(该模型×该题), 开跳 ----
    done_set = set()
    if resume and out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("response", "").strip() and not r.get("error"):
                    done_set.add((r["model_id"], r["question_id"]))
            except Exception:
                pass
        if done_set:
            _event("resume", qid=str(len(done_set)) + "条已有记录将跳过")
    elif out_path.exists():
        import shutil
        archive = out_path.with_name("results_" + time.strftime("%Y%m%d_%H%M%S") + ".jsonl")
        shutil.move(str(out_path), str(archive))
        _event("archive", qid=archive.name)

    out_f = open(out_path, "a" if (resume and done_set) else "w", encoding="utf-8")

    STATE.update(running=True, stop=False, started=time.strftime("%H:%M:%S"),
                 mode="模拟" if mock else "正式", models={}, events=[],
                 summary=None, error="", finished_at="",
                 total=len(qs) * len(models))
    for m in models:
        STATE["models"][m["id"]] = {
            "name": m.get("name", ""), "total": len(qs), "qidx": 0,
            "cur_qid": None, "cur_started": None, "done": 0, "errors": 0,
            "skipped": 0, "resumed": 0, "status": "ok", "note": "",
            "elapsed_sum": 0.0, "tin": 0, "tout": 0, "cost": 0.0}

    deadline = time.time() + stab["max_total_minutes"] * 60

    def ask_one(m, q, budget):
        """在预算内完成单题; 超时抛 TimeoutError"""
        mid = m["id"]
        t0 = time.time()
        if mock:
            time.sleep(0.3 + (_seed(mid, q["id"]) % 900) / 1000.0)
            resp = mock_response(q, mid, 1)
            s1, s2 = _seed(mid, q["id"]), _seed(mid, q["id"], "tok")
            return resp, {"prompt_tokens": 220 + s1 % 380,
                          "completion_tokens": 60 + s2 % 420}, round(time.time() - t0, 2)
        left = max(30, int(budget - (time.time() - t0)))
        cfg2 = dict(m, timeout_seconds=min(240, left))
        payload = build_payload_messages(q, m)
        resp, usage, lat = chat_with_retry(
            cfg2, payload,
            m.get("temperature", defaults.get("temperature", 0.7)),
            q.get("max_tokens", m.get("max_tokens", defaults.get("max_tokens", 4096))),
            stab["retries"], budget=budget)
        return resp, usage or {}, round(time.time() - t0, 2)

    def worker(m):
        mid = m["id"]
        ms = STATE["models"][mid]
        streak, cooldowns = 0, 0
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            for qi, q in enumerate(qs, 1):
                if STATE["stop"] or time.time() > deadline:
                    ms["status"] = "stopped" if STATE["stop"] else "timeout_total"
                    _event("stop_worker", mid, ms["status"])
                    return
                ms["qidx"] = qi
                # ---- 图片题: 非视觉跳过 ----
                if q.get("images") and not m.get("vision"):
                    ms["skipped"] += 1
                    continue
                # ---- 断点续跑跳过 ----
                if (mid, q["id"]) in done_set:
                    ms["resumed"] += 1
                    continue
                # ---- 熔断冷却/复活 ----
                if streak >= stab["breaker_fails"]:
                    cd = stab["cooldown_seconds"]
                    cooldowns += 1
                    if cooldowns > stab["revive_probes"]:
                        ms["status"] = "offline"
                        ms["note"] = f"熔断后{cooldowns-1}轮探活均失败, 标记掉线"
                        _event("offline", mid)
                        return
                    ms["status"] = "cooldown"
                    ms["note"] = f"连续{streak}次失败, 冷却{cd}s(第{cooldowns}/{stab['revive_probes']}轮)"
                    _event("cooldown", mid, ms["note"])
                    time.sleep(cd)
                    try:
                        if not mock and not _probe(m):
                            streak = stab["breaker_fails"]  # 保持熔断, 下轮再冷却
                            ms["note"] = "探活失败, 继续冷却"
                            continue
                    except Exception:
                        streak = stab["breaker_fails"]
                        continue
                    streak = 0
                    ms["status"] = "ok"
                    ms["note"] = "探活成功, 恢复作答"
                    _event("revive", mid)
                # ---- 作答(带看门狗) ----
                ms.update(cur_qid=q["id"], cur_started=time.time(), status="running")
                budget = stab["question_timeout"]
                fut = pool.submit(ask_one, m, q, budget)
                rec = {"model_id": mid, "model_name": m.get("name", ""),
                       "question_id": q["id"], "dim": q["dim"], "run": 1,
                       "messages": q["messages"],
                       "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
                try:
                    resp, usage, elapsed = fut.result(timeout=budget + 15)
                    tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
                    cost = _cost(tin, tout, price.get(mid))
                    rec.update(response=resp, usage=usage, latency_s=elapsed)
                    with WRITE_LOCK:
                        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out_f.flush()
                    ms.update(done=ms["done"] + 1, cur_qid=None, cur_started=None,
                              status="ok", note="",
                              elapsed_sum=round(ms["elapsed_sum"] + elapsed, 2),
                              tin=ms["tin"] + (tin or 0), tout=ms["tout"] + (tout or 0),
                              cost=round(ms["cost"] + cost, 4))
                    STATE["finished"] += 1
                    streak = 0
                except FTimeout:
                    rec.update(response="", error=f"单题超时({budget}s), 强制跳下一题", usage={})
                    with WRITE_LOCK:
                        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out_f.flush()
                    ms.update(errors=ms["errors"] + 1, cur_qid=None, cur_started=None,
                              status="ok", note="")
                    STATE["finished"] += 1
                    streak += 1
                    _event("timeout", mid, q["id"])
                except Exception as e:
                    rec.update(response="", error=str(e)[:300], usage={})
                    with WRITE_LOCK:
                        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out_f.flush()
                    ms.update(errors=ms["errors"] + 1, cur_qid=None, cur_started=None,
                              status="ok", note="")
                    STATE["finished"] += 1
                    streak += 1
                    _event("error", mid, q["id"])
            ms["status"] = "done"
            _event("model_done", mid, f"完成{ms['done']}题")
        except Exception as e:
            ms["status"] = "crashed"
            ms["note"] = str(e)[:120]
            STATE["error"] = str(e)[:200]
        finally:
            pool.shutdown(wait=False)

    threads = []
    try:
        for m in models:
            t = threading.Thread(target=worker, args=(m,), daemon=True,
                                 name="worker-" + m["id"])
            t.start()
            threads.append(t)
            time.sleep(1.5)  # 错峰启动
        for t in threads:
            t.join()
    except Exception as e:
        STATE["error"] = str(e)[:300]
    finally:
        out_f.close()
        try:
            exp = export_answers(out_path, mock) or {"unified": "", "archive": "", "files": []}
        except Exception:
            exp = {"unified": "", "archive": "", "files": []}
        totals = {}
        for m in models:
            ms = STATE["models"][m["id"]]
            if ms["done"]:
                ms["avg"] = round(ms["elapsed_sum"] / ms["done"], 2)
            totals[m["id"]] = ms
        STATE["summary"] = {"totals": totals, "answers": exp,
                            "answers_dir": exp.get("unified", ""),
                            "records": sum(v["done"] for v in totals.values()),
                            "results": str(out_path),
                            "resumed": sum(v["resumed"] for v in totals.values())}
        STATE.update(running=False, finished_at=time.strftime("%H:%M:%S"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--models", default="")
    ap.add_argument("--dims", default="")
    ap.add_argument("--out", default="results/results.jsonl")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--question-timeout", type=int, default=240)
    args = ap.parse_args()
    run_race(mock=args.mock, limit=args.limit,
             model_ids=[x for x in args.models.split(",") if x],
             dims=[x for x in args.dims.split(",") if x], out_path=BASE / args.out,
             resume=not args.no_resume,
             stability={"question_timeout": args.question_timeout})
    if STATE["error"]:
        print("出错:", STATE["error"])
    s = STATE.get("summary") or {}
    print("作答完成:", s.get("records", 0), "条 | 续跑复用:", s.get("resumed", 0))
    for mid, t in (s.get("totals") or {}).items():
        print(f"  {mid} {t['name']}: {t['status']} done={t['done']} err={t['errors']} "
              f"skip={t['skipped']} avg={t.get('avg', '-')}s")


if __name__ == "__main__":
    main()
