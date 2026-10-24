#!/bin/bash
cd "/c/Users/loong/Desktop/测试集/llm-eval"
export PYTHONIOENCODING=utf-8
echo "===== 正式评分 $(date '+%H:%M:%S') ====="
python scorer.py
echo "scorer 退出码: $?"
echo "===== Excel报告 $(date '+%H:%M:%S') ====="
python workbook.py
echo "===== 评分包导出 $(date '+%H:%M:%S') ====="
python export_eval.py
echo "===== 桌面终验 ====="
python - <<'PYEOF'
from pathlib import Path
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
desk = Path.home() / "Desktop"
d = desk / "测试作答"
files = sorted(f.name for f in d.iterdir())
print(f"桌面\测试作答\: {len(files)} 份 -> {files}")
p = desk / "评分包"
pf = sorted(f.name for f in p.iterdir()) if p.exists() else []
print(f"桌面\评分包\: {len(pf)} 个 -> {pf}")
rep = desk / "测试报告.xlsx"
print("桌面\测试报告.xlsx:", rep.exists(), f"{rep.stat().st_size//1024}KB" if rep.exists() else "")
rows = [json.loads(l) for l in open("results/scored.jsonl", encoding="utf-8")]
print(f"scored.jsonl: {len(rows)} 条 | 其中评审失败: {sum(1 for r in rows if '评审调用失败' in str(r.get('detail','')))}")
PYEOF
echo "===== 终版流水线完成 $(date '+%H:%M:%S') ====="
