#!/bin/bash
# watcher.sh — 等最后模型交卷 → 评分 → 报告 → 评分包 → 验证桌面交付物
cd "/c/Users/loong/Desktop/测试集/llm-eval"
export PYTHONIOENCODING=utf-8

echo "===== 开始守望 $(date '+%H:%M:%S') ====="
# 1) 等待竞测结束(最长4小时)
for i in $(seq 1 120); do
  S=$(curl -s --max-time 10 http://127.0.0.1:8787/api/race/status | python -c "
import json,sys
d=json.load(sys.stdin)
ms=d.get('models') or {}
k=ms.get('M07',{})
print(f\"{d['running']}|{d['finished']}/{d['total']}|M07:{k.get('status')}:{k.get('done')}/{k.get('total')}\")
" 2>/dev/null)
  echo "[$(date '+%H:%M:%S')] $S"
  case "$S" in False*) echo "竞测已结束"; break;; esac
  sleep 120
done

# 2) 评分(正式: LLM评审走中转站)
echo "===== 评分 scorer.py $(date '+%H:%M:%S') ====="
python scorer.py || echo "!!! scorer 失败(继续导出评分包, 不依赖评分)"

# 3) Excel 报告
echo "===== 报告 workbook.py $(date '+%H:%M:%S') ====="
python workbook.py || echo "!!! workbook 失败"

# 4) 评分包导出(桌面)
echo "===== 评分包 export_eval.py $(date '+%H:%M:%S') ====="
python export_eval.py || echo "!!! export_eval 失败"

# 5) 验证桌面交付物
echo "===== 桌面交付物验证 ====="
python - <<'EOF'
from pathlib import Path
import sys
sys.stdout.reconfigure(encoding="utf-8")
desk = Path.home() / "Desktop"
for name in ("测试作答", "评分包"):
    d = desk / name
    files = sorted(f.name for f in d.iterdir()) if d.exists() else []
    print(f"桌面\\{name}\\: {len(files)} 个文件 -> {files[:8]}")
rep = desk / "测试报告.xlsx"
print("桌面\\测试报告.xlsx:", rep.exists(), f"{rep.stat().st_size//1024}KB" if rep.exists() else "")
EOF
echo "===== 守望完成 $(date '+%H:%M:%S') ====="
