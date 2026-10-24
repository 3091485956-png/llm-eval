# -*- coding: utf-8 -*-
"""workbook.py — 评分报告工作簿生成器
读取 models.yaml + questions/*.jsonl + results/scored.jsonl,
重建完整工作簿(六表/公式链/下拉/雷达图)并注入数据 → results/测试报告.xlsx
说明: 不直接改写已有 xlsx(openpyxl 读写会丢失图表), 而是每次重建,
      因此任何手工对旧报告的修改都会被覆盖 — 请只改本目录的配置与题库。
用法: python workbook.py
"""
import json
import math
import platform
import re
import sys
import time
import unicodedata
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.chart import RadarChart, Reference
from openpyxl.chart.marker import Marker
from openpyxl.drawing.line import LineProperties
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

BASE = Path(__file__).resolve().parent
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # 被重定向到StringIO等场景

# ---- 设计令牌(与主模板一致的专业蓝) ----
PRIMARY, PRIMARY_LIGHT = "1B2A4A", "D6E4F0"
SECONDARY = PRIMARY_LIGHT
POS, NEG, WARN = "1B7D46", "C0392B", "D4820A"
N900, N600, N200, N100, N0 = "37352F", "8C8A84", "E9E9E8", "F7F7F5", "FFFFFF"
CHART_COLORS = [PRIMARY, POS, WARN, NEG, N600]
FONT_NAME = "微软雅黑" if platform.system() == "Windows" else "Noto Sans SC"
HEADER_BOLD = platform.system() != "Windows"  # 微软雅黑加粗清晰, Noto系列不加粗

CAPS = ["语言理解", "信息提取", "创造力", "指令遵循", "复杂推理", "约束处理",
        "结构化输出", "长文本组织", "上下文利用", "信息检索", "评估", "错误识别"]


def derive_dims(questions):
    """维度顺序: 12个常规维度在前, 新增维度(如文档解析)按出现顺序附后"""
    seen = []
    for q in questions:
        if q["dim"] not in seen:
            seen.append(q["dim"])
    ordered = [d for d in CAPS if d in seen] + [d for d in seen if d not in CAPS]
    return ordered


# ---- 样式工厂 ----
def f_title(): return Font(name=FONT_NAME, size=16, bold=HEADER_BOLD, color=PRIMARY)
def f_header(): return Font(name=FONT_NAME, size=11, bold=HEADER_BOLD, color="FFFFFF")
def f_sub(): return Font(name=FONT_NAME, size=11, bold=HEADER_BOLD, color=PRIMARY)
def f_body(): return Font(name=FONT_NAME, size=11, color=N900)
def f_cap(): return Font(name=FONT_NAME, size=9, color=N600)
def fill_head(): return PatternFill("solid", fgColor=PRIMARY)
def fill_total(): return PatternFill("solid", fgColor=SECONDARY)
def fill_row(i): return PatternFill("solid", fgColor=N0 if i % 2 == 0 else N100)
def b_head(): return Border(bottom=Side(style="thin", color=N200))
def b_total(): return Border(top=Side(style="medium", color=N200))
def a_num(): return Alignment(horizontal="right", vertical="center", wrap_text=True)
def a_txt(): return Alignment(horizontal="left", vertical="center", wrap_text=True)
def a_ctr(): return Alignment(horizontal="center", vertical="center", wrap_text=True)


def setup_sheet(ws, title, last_col):
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.row_dimensions[1].height = 15
    ws.row_dimensions[2].height = 32
    ws.row_dimensions[3].height = 8
    ws.merge_cells(start_row=2, start_column=2, end_row=2, end_column=last_col)
    c = ws.cell(row=2, column=2, value=title)
    c.font, c.alignment = f_title(), Alignment(horizontal="left", vertical="center")


def put_headers(ws, headers, row=4):
    for i, h in enumerate(headers, start=2):
        c = ws.cell(row=row, column=i, value=h)
        c.fill, c.font, c.alignment, c.border = fill_head(), f_header(), a_ctr(), b_head()
    ws.row_dimensions[row].height = 28


def style_row(ws, r, c0, c1, i, height=22):
    for col in range(c0, c1 + 1):
        c = ws.cell(row=r, column=col)
        c.fill, c.font = fill_row(i), f_body()
        c.alignment = a_txt()
    ws.row_dimensions[r].height = height


def set_widths(ws, d):
    for k, v in d.items():
        ws.column_dimensions[k].width = v


def section_title(ws, row, text, last_col):
    for col in range(2, last_col + 1):
        c = ws.cell(row=row, column=col)
        c.fill, c.font = fill_total(), f_sub()
    ws.cell(row=row, column=2, value=text)
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=last_col)
    ws.row_dimensions[row].height = 24


def caption(ws, row, text):
    c = ws.cell(row=row, column=2, value=text)
    c.font, c.alignment = f_cap(), Alignment(horizontal="left", vertical="center")


def clean(v):
    """去除 openpyxl 不允许的XML非法控制字符(基准语料中偶见)"""
    return ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v


def text_line(ws, row, label, text, last_col, i):
    for col in range(2, last_col + 1):
        c = ws.cell(row=row, column=col)
        c.fill, c.font = fill_row(i), f_body()
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
    ws.cell(row=row, column=2, value=label)
    ws.cell(row=row, column=3, value=text)
    ws.row_dimensions[row].height = 20


def _disp_w(s):
    if s is None:
        return 0
    return sum(1.7 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def fit_heights(ws, c0, c1, r0, r1, width_map):
    """按换行行数估算并加高数据行(上限409pt, Excel单行上限)。"""
    for r in range(r0, r1 + 1):
        lines = 1
        for col in range(c0, c1 + 1):
            v = ws.cell(row=r, column=col).value
            if v is None:
                continue
            w = width_map.get(get_column_letter(col), 11)
            need = math.ceil(_disp_w(v) / max(1.0, w * 0.9)) if w else 1
            need = max(need, str(v).count("\n") + 1)
            lines = max(lines, need)
        ws.row_dimensions[r].height = min(409, max(22, 22 + (lines - 1) * 15))


# ---------------------------------------------------------------- 数据加载
def load_all():
    from runner import load_questions, load_yaml
    cfg = load_yaml(BASE / "models.yaml")
    questions = sorted(load_questions(), key=lambda q: q["id"])
    scored = [json.loads(l) for l in (BASE / "results" / "scored.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    scored.sort(key=lambda r: (r["model_id"], r["question_id"], r.get("run", 1)))
    return cfg, questions, scored


def stem_of(q):
    parts = []
    for i, m in enumerate(q["messages"], 1):
        tag = "用户" if m["role"] == "user" else "助手"
        parts.append(f"【第{i}轮·{tag}】{m['content']}")
    stem = "\n".join(parts) if len(parts) > 1 else q["messages"][0]["content"]
    if q.get("images"):
        stem = "【多模态图片题，共" + str(len(q["images"])) + "张页面图】\n" + stem
    return stem


# ---------------------------------------------------------------- 各表构建
def sheet_readme(wb):
    ws = wb.create_sheet("使用说明")
    last = 3
    setup_sheet(ws, "LLM 大模型十二能力测试 · 自动评测报告", last)
    caption(ws, 4, "十二能力维度：" + " · ".join(CAPS))
    r = 6
    section_title(ws, r, "一、本报告由评测工具自动生成", last)
    rows = [
        ("数据来源", "models.yaml(模型清单) + questions/*.jsonl(题库) + results/scored.jsonl(评分结果)"),
        ("生成方式", "python workbook.py 每次整体重建本工作簿; 请勿手工编辑本报告, 改题库/配置后重新生成"),
        ("公式链", "测试记录表的模型名称/能力维度/参考答案/满分/得分率为公式; 能力评分汇总表整表公式自动计算"),
        ("查看建议", "Excel/WPS 打开后自动重算; 雷达图展示前4个模型, 需调整请右键图表→选择数据"),
        ("复测", "生成类题目已按题库标注的 runs 次数重复施测, 得分率自动平均"),
    ]
    for i, (a, b) in enumerate(rows):
        r += 1
        text_line(ws, r, a, b, last, i)
    r += 2
    section_title(ws, r, "二、命令速查（在本目录执行）", last)
    rows = [
        ("1 施测", "python runner.py            # 正式施测, 读 models.yaml, 输出 results/results.jsonl"),
        ("2 调试", "python runner.py --mock --limit 20   # 离线模拟20条, 验证链路不花钱"),
        ("3 评分", "python scorer.py            # 三层评分(规则/覆盖度/LLM评审), 输出 results/scored.jsonl"),
        ("4 报告", "python workbook.py          # 生成本报告 results/测试报告.xlsx"),
        ("5 过滤", "python runner.py --models M01 --dims 语言理解   # 只测部分模型/维度"),
    ]
    for i, (a, b) in enumerate(rows):
        r += 1
        text_line(ws, r, a, b, last, i)
    r += 2
    section_title(ws, r, "三、注意事项", last)
    rows = [
        ("评审模型", "models.yaml 中 judge 段为评审模型, 建议与被测模型不同厂商, 避免自偏好"),
        ("密钥安全", "api_key 支持 env:环境变量名 写法, 不必写明文"),
        ("题目防污染", "题库以权威题型改编为主(高考/考研/公考/Benchmark), 改编题已标注来源"),
        ("可比性", "所有模型使用相同温度与提示词; 修改参数后需整体重测才可比"),
        ("手动补测", "网页端 Agent 体验组结果请在主模板工作簿中登记, 勿混入本 API 报告"),
    ]
    for i, (a, b) in enumerate(rows):
        r += 1
        text_line(ws, r, a, b, last, i)
    set_widths(ws, {"B": 12, "C": 100})
    return ws


def sheet_models(wb, cfg, scored):
    ws = wb.create_sheet("模型信息")
    headers = ["模型ID", "模型名称", "厂商/提供商", "版本号(接口model)", "接入方式",
               "推理模式", "温度", "上下文长度(K)", "发布时间", "测试日期", "状态", "视觉",
               "备注"]
    setup_sheet(ws, "参评模型清单（来自 models.yaml）", len(headers) + 1)
    put_headers(ws, headers)
    defaults = cfg.get("defaults") or {}
    tested = {r["model_id"] for r in scored}
    today = time.strftime("%Y-%m-%d")
    models = cfg.get("models", [])
    for i, m in enumerate(models):
        r = 5 + i
        vals = [m["id"], m.get("name", ""), m.get("vendor", ""), m.get("model", ""),
                "API-OpenAI兼容", m.get("thinking", "未设置"),
                m.get("temperature", defaults.get("temperature", 0.7)),
                m.get("context_k", ""), "", today if m["id"] in tested else "",
                "已施测" if m["id"] in tested else "待测",
                "是" if m.get("vision") else "否",
                "自动写入" if m["id"] in tested else "models.yaml 中 enabled 未开启"]
        for j, v in enumerate(vals):
            ws.cell(row=r, column=2 + j, value=v)
        style_row(ws, r, 2, len(headers) + 1, i)
        ws.cell(row=r, column=8).number_format = "0.0"
        ws.cell(row=r, column=8).alignment = a_num()
        ws.cell(row=r, column=9).alignment = a_num()
    n = len(models)
    set_widths(ws, {"B": 8, "C": 16, "D": 13, "E": 22, "F": 15, "G": 11, "H": 7,
                    "I": 13, "J": 11, "K": 11, "L": 9, "M": 6, "N": 24})
    ws.freeze_panes = "C5"
    return n


def sheet_questions(wb, questions):
    ws = wb.create_sheet("题目信息")
    headers = ["题目ID", "能力维度", "考察子类", "来源类别", "来源详情", "题型", "难度",
               "题干", "参考答案/要点", "评分要点", "满分", "污染风险", "状态", "评分方式"]
    setup_sheet(ws, "十二能力题库（questions/*.jsonl 自动同步）", len(headers) + 1)
    put_headers(ws, headers)
    mode_name = {"mc": "规则-选项", "exact": "规则-答案匹配", "numeric": "规则-数值",
                 "json": "规则-结构校验", "constraints": "规则-约束", "anchor": "规则-锚定",
                 "llm": "LLM评审", "coverage": "LLM评审-覆盖度", "hybrid": "规则+LLM"}
    for i, q in enumerate(questions):
        r = 5 + i
        sc = q.get("scoring") or {}
        vals = [q["id"], q["dim"], q.get("sub", ""), q.get("src", ""), q.get("src_detail", ""),
                q.get("type", ""), q.get("diff", 3), stem_of(q), q.get("ref", ""),
                q.get("points", ""), q.get("score", 10), q.get("contam", "低·新编题"),
                "启用", mode_name.get(sc.get("mode", "llm"), sc.get("mode"))]
        for j, v in enumerate(vals):
            ws.cell(row=r, column=2 + j, value=clean(v))
        style_row(ws, r, 2, len(headers) + 1, i, height=22)
        ws.cell(row=r, column=8).number_format = "0"
        ws.cell(row=r, column=8).alignment = a_num()
        ws.cell(row=r, column=12).number_format = "0"
        ws.cell(row=r, column=12).alignment = a_num()
    n = len(questions)
    widths = {"B": 8, "C": 11, "D": 15, "E": 13, "F": 20, "G": 10, "H": 7, "I": 46,
              "J": 28, "K": 28, "L": 7, "M": 13, "N": 8, "O": 14}
    set_widths(ws, widths)
    fit_heights(ws, 2, len(headers) + 1, 5, 4 + n, widths)
    ws.freeze_panes = "D5"
    ws.auto_filter.ref = f"B4:O{4 + n}"
    return n


def sheet_records(wb, questions, scored):
    ws = wb.create_sheet("测试记录")
    headers = ["记录ID", "模型ID", "模型名称", "题目ID", "能力维度", "运行轮次",
               "模型原始回答", "参考答案", "得分", "满分", "得分率", "评分方式",
               "评语(判分明细)", "测试日期", "备注",
               "输入tokens", "输出tokens", "费用(元)", "评审输入tokens",
               "评审输出tokens", "评审费用(元)", "延迟(秒)"]
    setup_sheet(ws, "测试原始记录（runner+scorer 自动写入，含token与费用）", len(headers) + 1)
    put_headers(ws, headers)
    n_q = len(questions)
    q_last = 4 + n_q
    spare = 20
    n_rec = len(scored)
    r_last = 4 + n_rec + spare

    for i, rec in enumerate(scored):
        r = 5 + i
        ws.cell(row=r, column=2, value=f"R{i + 1:04d}")
        ws.cell(row=r, column=3, value=rec["model_id"])
        ws.cell(row=r, column=5, value=rec["question_id"])
        ws.cell(row=r, column=7, value=rec.get("run", 1))
        ws.cell(row=r, column=8, value=clean(rec.get("response", "")))
        ws.cell(row=r, column=10, value=rec.get("score", 0))
        ws.cell(row=r, column=13, value=rec.get("scoring_method", ""))
        ws.cell(row=r, column=14, value=clean(str(rec.get("detail", ""))[:300]))
        ws.cell(row=r, column=15, value=rec.get("timestamp", "")[:10])
        ws.cell(row=r, column=16, value="自动写入" + ("" if not rec.get("error") else "·调用失败"))
        ws.cell(row=r, column=17, value=rec.get("input_tokens"))
        ws.cell(row=r, column=18, value=rec.get("output_tokens"))
        ws.cell(row=r, column=19, value=rec.get("cost"))
        ws.cell(row=r, column=20, value=rec.get("judge_input_tokens"))
        ws.cell(row=r, column=21, value=rec.get("judge_output_tokens"))
        ws.cell(row=r, column=22, value=rec.get("judge_cost"))
        ws.cell(row=r, column=23, value=rec.get("latency_s"))
    # 公式列(含 spare 行): D 模型名称 / F 能力维度 / I 参考答案 / K 满分 / L 得分率
    for r in range(5, r_last + 1):
        ws.cell(row=r, column=4).value = (
            f'=IF($C{r}="","",IFERROR(VLOOKUP($C{r},模型信息!$B$5:$C$24,2,0),"未登记"))')
        ws.cell(row=r, column=6).value = (
            f'=IF($E{r}="","",IFERROR(VLOOKUP($E{r},题目信息!$B$5:$C${q_last},2,0),"未登记"))')
        ws.cell(row=r, column=9).value = (
            f'=IF($E{r}="","",IFERROR(VLOOKUP($E{r},题目信息!$B$5:$J${q_last},9,0),""))')
        ws.cell(row=r, column=11).value = (
            f'=IF($E{r}="","",IFERROR(VLOOKUP($E{r},题目信息!$B$5:$L${q_last},11,0),10))')
        ws.cell(row=r, column=12).value = (
            f'=IF(OR($J{r}="",$K{r}=""),"",$J{r}/$K{r})')
    for i, r in enumerate(range(5, r_last + 1)):
        style_row(ws, r, 2, len(headers) + 1, i)
        for col, fmt in ((7, "0"), (10, "0.0"), (11, "0"), (12, "0.0%"),
                         (17, "0"), (18, "0"), (19, "0.0000"),
                         (20, "0"), (21, "0"), (22, "0.0000"), (23, "0.0")):
            ws.cell(row=r, column=col).number_format = fmt
            ws.cell(row=r, column=col).alignment = a_num()
        ws.cell(row=r, column=15).alignment = a_ctr()
    set_widths(ws, {"B": 9, "C": 9, "D": 16, "E": 9, "F": 12, "G": 9, "H": 42,
                    "I": 28, "J": 8, "K": 7, "L": 10, "M": 13, "N": 28, "O": 12,
                    "P": 15, "Q": 10, "R": 10, "S": 10, "T": 10, "U": 10,
                    "V": 10, "W": 9})
    ws.freeze_panes = "D5"
    ws.auto_filter.ref = f"B4:W{r_last}"
    return r_last


def sheet_summary(wb, n_models, r_last, dims):
    ws = wb.create_sheet("能力评分汇总")
    ws.sheet_properties.tabColor = PRIMARY
    n_dim = len(dims)
    c_cap0 = 4                      # 首个能力列 = D
    c_all = c_cap0 + n_dim          # 综合得分率
    c_cnt = c_all + 1               # 测试记录数
    c_rank = c_all + 2              # 排名
    c_tin = c_all + 3               # 输入tokens合计
    c_tout = c_all + 4              # 输出tokens合计
    c_cost = c_all + 5              # 费用合计
    headers = ["模型ID", "模型名称"] + dims + \
        ["综合得分率", "测试记录数", "排名", "输入tokens合计", "输出tokens合计", "费用合计(元)"]
    setup_sheet(ws, "能力评分汇总（公式自动计算，无需手工填写）", len(headers) + 1)
    put_headers(ws, headers)
    m_last = max(23, 4 + n_models)
    A = lambda c: get_column_letter(c)
    for r in range(5, m_last + 1):
        ws.cell(row=r, column=2).value = f'=IF(模型信息!B{r}="","",模型信息!B{r})'
        ws.cell(row=r, column=3).value = (
            f'=IF($B{r}="","",IFERROR(VLOOKUP($B{r},模型信息!$B$5:$C$24,2,0),""))')
        for j in range(n_dim):
            cc = c_cap0 + j
            L = A(cc)
            ws.cell(row=r, column=cc).value = (
                f'=IF($B{r}="","",IFERROR(AVERAGEIFS(测试记录!$L$5:$L${r_last},'
                f'测试记录!$C$5:$C${r_last},$B{r},测试记录!$F$5:$F${r_last},{L}$4),""))')
        ws.cell(row=r, column=c_all).value = (
            f'=IF($B{r}="","",IFERROR(AVERAGEIFS(测试记录!$L$5:$L${r_last},'
            f'测试记录!$C$5:$C${r_last},$B{r}),""))')
        ws.cell(row=r, column=c_cnt).value = (
            f'=IF($B{r}="","",COUNTIFS(测试记录!$C$5:$C${r_last},$B{r}))')
        ws.cell(row=r, column=c_rank).value = (
            f'=IF(${A(c_all)}{r}="","",IFERROR(RANK(${A(c_all)}{r},'
            f'${A(c_all)}$5:${A(c_all)}${m_last}),""))')
        ws.cell(row=r, column=c_tin).value = (
            f'=IF($B{r}="","",SUMIFS(测试记录!$Q$5:$Q${r_last},'
            f'测试记录!$C$5:$C${r_last},$B{r}))')
        ws.cell(row=r, column=c_tout).value = (
            f'=IF($B{r}="","",SUMIFS(测试记录!$R$5:$R${r_last},'
            f'测试记录!$C$5:$C${r_last},$B{r}))')
        ws.cell(row=r, column=c_cost).value = (
            f'=IF($B{r}="","",SUMIFS(测试记录!$S$5:$S${r_last},'
            f'测试记录!$C$5:$C${r_last},$B{r})'
            f'+SUMIFS(测试记录!$V$5:$V${r_last},测试记录!$C$5:$C${r_last},$B{r}))')
    for i, r in enumerate(range(5, m_last + 1)):
        style_row(ws, r, 2, len(headers) + 1, i)
        for c in range(c_cap0, c_all + 1):
            ws.cell(row=r, column=c).number_format = "0.0%"
            ws.cell(row=r, column=c).alignment = a_num()
        for c, fmt in [(c_cnt, "0"), (c_rank, "0"), (c_tin, "0"),
                       (c_tout, "0"), (c_cost, "0.00")]:
            ws.cell(row=r, column=c).number_format = fmt
            ws.cell(row=r, column=c).alignment = a_num()
    ws.conditional_formatting.add(
        f"{A(c_cap0)}5:{A(c_all - 1)}{m_last}",
        ColorScaleRule(start_type="num", start_value=0, start_color="F8696B",
                       mid_type="num", mid_value=0.5, mid_color="FFEB84",
                       end_type="num", end_value=1, end_color="63BE7B"))
    set_widths(ws, {"B": 9, "C": 16})
    for j in range(n_dim):
        ws.column_dimensions[A(c_cap0 + j)].width = 11
    for c in (c_all, c_cnt, c_rank, c_tin, c_tout, c_cost):
        ws.column_dimensions[A(c)].width = 12
    ws.freeze_panes = "D5"
    caption(ws, m_last + 2, "注：本表全部由公式自动计算（按模型ID+能力维度匹配测试记录）；"
                            "雷达图默认展示前4个模型；费用列为按 models.yaml 单价的估算值"
                            "（施测+评审），不含缓存折扣等计费细节；文档解析维度仅统计视觉模型。")
    chart = RadarChart()
    chart.type = "standard"
    chart.width, chart.height = 22, 13
    chart.title = f"{n_dim}维能力雷达图（得分率）"
    last_series_row = min(8, 4 + max(2, n_models))
    data = Reference(ws, min_col=3, min_row=5, max_col=c_all - 1, max_row=last_series_row)
    cats = Reference(ws, min_col=c_cap0, min_row=4, max_col=c_all - 1, max_row=4)
    chart.add_data(data, titles_from_data=True, from_rows=True)
    chart.set_categories(cats)
    for i, s in enumerate(chart.series):
        color = CHART_COLORS[i % len(CHART_COLORS)]
        s.graphicalProperties.line = LineProperties(solidFill=color, w=22000)
        s.marker = Marker(symbol="circle", size=5)
    chart.y_axis.number_format = "0%"
    chart.y_axis.scaling.min = 0
    chart.y_axis.scaling.max = 1
    chart.legend.position = "b"
    ws.add_chart(chart, f"B{m_last + 4}")
    return ws


def sheet_rubric(wb):
    ws = wb.create_sheet("评分标准")
    last = 5
    setup_sheet(ws, "评分标准与 LLM 评审提示词", last)
    r = 4
    section_title(ws, r, "一、通用评分量表（每题默认满分10分）", last)
    r += 1
    put_headers(ws, ["分数段", "等级", "描述"], row=r)
    scale = [
        ("9~10", "优秀", "完全满足要求：答案正确、完整，产出质量高，无事实性或逻辑错误"),
        ("7~8", "良好", "主体正确：结论或产出基本正确，仅有轻微瑕疵（表述、格式、小遗漏），不影响使用"),
        ("5~6", "中等", "部分正确：关键内容约半数正确，或产出明显偏离要求但仍有可用成分"),
        ("3~4", "较差", "基本错误：仅有少量正确成分，或严重偏离题目要求"),
        ("0~2", "失败", "完全错误、答非所问、拒答，或严重违反题目约束"),
    ]
    for i, (a, b, c) in enumerate(scale):
        r += 1
        ws.cell(row=r, column=2, value=a)
        ws.cell(row=r, column=3, value=b)
        ws.cell(row=r, column=4, value=c)
        style_row(ws, r, 2, last, i)
    r += 1
    caption(ws, r, "客观选择题可简化为：答对10分、答错0分；规则判分由 scorer.py 自动完成，本表供 LLM 评审与人工仲裁使用。")

    r += 2
    section_title(ws, r, "二、各能力维度评分要点", last)
    r += 1
    put_headers(ws, ["能力维度", "考察点", "推荐题型与题源", "评分要点/通过标准"], row=r)
    dims = [
        ("语言理解", "词义辨析、病句、文言文、阅读理解深层含义", "高考语文题型改编；考研英语阅读", "客观题按对错计分；简答按要点覆盖度给分"),
        ("信息提取", "实体/数值/时间/因果关系的抽取与表格化", "合同、新闻、简历等真实文本改编", "字段全部正确得满分，漏、错一处扣1~2分"),
        ("创造力", "新颖性、流畅性、灵活性、文字表现力", "高考作文题型改编、命题写作", "按新颖性/切题度/结构/语言分项加权"),
        ("指令遵循", "多指令复合：格式+字数+语气+顺序", "IFEval类约束指令、多步任务", "逐条核对指令，满足一条记一分，按比例折算"),
        ("复杂推理", "多步数学、逻辑演绎、因果链条", "高考数学题型、GSM8K式应用题、考研逻辑", "过程与结论分开评：结论对但过程错最多6分"),
        ("约束处理", "硬约束不违反：禁用词、必含要素、长度上限", "多重约束写作题", "违反任一硬约束直接0~2分；软约束酌情扣分"),
        ("结构化输出", "JSON/YAML/表格的合法性、完整性、可解析性", "文本转JSON/Markdown表格任务", "程序校验：解析失败0分；字段缺失按比例扣分"),
        ("长文本组织", "长文摘要完整性、报告结构性、多篇融合", "长报告摘要、LongBench式任务", "按覆盖率+忠实度+结构性三个维度评"),
        ("上下文利用", "指代消解、多轮依赖、上文信息沿用", "多轮对话题、大海捞针变体", "必须依托上文信息才能答对才算通过"),
        ("信息检索", "给定材料内定位证据、多跳查找、标注出处", "提供文档集的问答（RAG式）", "答案须出自给定材料并给出处；编造0分"),
        ("评估", "对文本/作文/方案判优劣并给出理由", "作文互评、方案对比排序", "判断结论与理由质量各占一半权重"),
        ("错误识别", "病句、事实错误、逻辑漏洞、代码bug定位", "高考病句改错题型、植入错误的代码或短文", "找全且改对得满分；漏找、误报按比例扣分"),
    ]
    for i, row_vals in enumerate(dims):
        r += 1
        for j, v in enumerate(row_vals):
            ws.cell(row=r, column=2 + j, value=v)
        style_row(ws, r, 2, last, i)
    r += 2
    section_title(ws, r, "三、LLM评审提示词模板（scorer.py 已内置, 此处供人工仲裁复用）", last)
    prompt = [
        ("1", "你是严格且公正的阅卷评分官。请根据下面三部分信息，对【模型回答】打分："),
        ("2", "【题目】<粘贴题干与全部要求>"),
        ("3", "【参考答案/评分要点】<粘贴题目信息表中的参考答案与评分要点>"),
        ("4", "【模型回答】<粘贴测试记录表中的模型原始回答>"),
        ("5", "评分规则：满分10分。先逐条核对要求与评分要点，指出对错；再检查硬约束；最后给总分。"),
        ("6", "只按以下格式输出："),
        ("7", "分析：<逐条核对过程与扣分理由>"),
        ("8", "得分：<0-10的整数>"),
        ("9", "提示：不因冗长加分；约束类题目违反任一硬约束，总分不超过2分。"),
    ]
    for i, (label, line) in enumerate(prompt):
        r += 1
        text_line(ws, r, label, line, last, i)
    widths = {"B": 14, "C": 32, "D": 40, "E": 40}
    set_widths(ws, widths)
    fit_heights(ws, 2, last, 5, r, widths)
    return ws


def load_all():
    from runner import load_questions, load_yaml
    cfg = load_yaml(BASE / "models.yaml")
    questions = sorted(load_questions(), key=lambda q: q["id"])
    scored = [json.loads(l) for l in (BASE / "results" / "scored.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    scored.sort(key=lambda r: (r["model_id"], r["question_id"], r.get("run", 1)))
    # 模型清单 = models.yaml ∪ 记录中出现过的模型(未配置yaml也能出报告, 如内置模拟模型)
    seen = {}
    for m in cfg.get("models") or []:
        seen[m["id"]] = m
    for r in scored:
        mid = r.get("model_id")
        if mid and mid not in seen:
            seen[mid] = {"id": mid, "name": r.get("model_name", ""), "enabled": True}
        if r.get("dim") == "文档解析" and r.get("response") and mid in seen:
            seen[mid]["vision"] = True  # 答过图片题的模型标记视觉
    cfg["models"] = list(seen.values())
    return cfg, questions, scored


def sheet_classify(wb):
    """模型分类子表: 口径/公式/理由 + 梯队总表 + z画像明细 + 题目区分度"""
    import classify as cf_mod
    cf = cf_mod.classify()
    ws = wb.create_sheet("模型分类")
    last = 15  # B..O
    setup_sheet(ws, "模型分类报告（口径·公式·理由随表）", last)
    caption(ws, 4, "数据源: results/scored.jsonl · 生成时间 " + cf["generated"]
            + " · 应作答题数 " + str(cf["expected"]) + " · 本表为分析快照, 数值与「能力评分汇总」同源")

    r = 6
    section_title(ws, r, "一、计算口径 · 公式 · 理由", last)
    rules = [
        ("1 客观综合分",
         "公式 = 11个客观维度得分率的算术平均×100（不含创造力/文档解析）。",
         "理由: 客观题为程序判分、可复核; 创造力是LLM评审主观分(评审模型同时参赛有自偏好风险, "
         "且含14条评审失败记0), 故只列参考不进主公式。"),
        ("2 资格门",
         "完成率 = 有效作答/应作答; 错误率 = 错误记录/应作答; 完成率≥90% 且 错误率≤10% → ✓, 否则 ⚠(保留名次不除名)。",
         "理由: 答得少的模型的平均分不能与答全者直接比较, 缺考信息必须显性化而非惩罚性折算。"),
        ("3 梯队(固定带)",
         "T1 ≥75 / T2 65~75 / T3 <65（按客观综合分）。本场实际: T1四人、T2三人、T3空档。",
         "理由: 门槛先于数据立定, 不随本场分数挪动, 换模型/重跑可比; 三类对应决策语言——首选/够用/慎用。"),
        ("4 能力画像(z分数)",
         "z = (该模型该维度得分率 − 群体均值)/群体标准差; ◆相对优势 z≥+0.5; ▼相对短板 z≤−0.5; "
         "✖绝对短板 得分率<50%(可与相对标签并存)。",
         "理由: 梯队回答\"谁强\", 画像回答\"选谁干什么活\"; 相对+绝对双轨, 避免\"全场最差维度的第一名\"被误标为优势。"),
        ("5 性价比与象限",
         "性价比 = 客观综合分 / 施测费用(元, 不含评审费用); 象限以中位数切(本场 能力中位"
         + str(cf["cap_med"]) + " / 费用中位" + str(cf["cost_med"]) + "元)。",
         "理由: 能力与价格正交, 混入单一公式会得到\"便宜平庸=贵而强\"的失真; 分母用费用, 因为你付的是钱。"),
        ("6 题目区分度",
         "维度间标准差<5pp → 区分度不足; 均值≥90% 天花板 / <50% 地板。",
         "理由: 这层评题不评模型, 为下轮换题提供依据(本场: 结构化输出需换难题, 长文本保留)。"),
    ]
    for i, (a, b, c) in enumerate(rules):
        r += 1
        for col, v in ((2, a), (3, b), (9, c)):
            cell = ws.cell(row=r, column=col, value=clean(v))
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=8)
        ws.merge_cells(start_row=r, start_column=9, end_row=r, end_column=last)
        for col in range(2, last + 1):
            cc = ws.cell(row=r, column=col)
            cc.fill = fill_row(i)
            cc.font = f_sub() if col == 2 else f_body()
            cc.alignment = a_txt()
        ws.row_dimensions[r].height = 40

    # ---- 二、总表 ----
    r += 2
    section_title(ws, r, "二、模型分类总表（按客观综合分降序）", last)
    r += 1
    heads = ["模型ID", "模型名称", "客观综合分", "完成率", "错误率", "资格门", "梯队",
             "创造力(参考)", "施测费用(元)", "性价比(分/元)", "象限", "能力画像摘要"]
    put_headers(ws, heads, row=r)
    for i, row in enumerate(cf["rows"]):
        r += 1
        vals = [row["id"], row["name"], row["s_obj"], row["done"], row["err"],
                row["gate"], row["tier"], row["creativity"], row["cost"], row["cp"],
                row["quad"], clean("；".join(row["tags"]) or "—")]
        for j, v in enumerate(vals, 2):
            ws.cell(row=r, column=j, value=v)
        style_row(ws, r, 2, 2 + len(heads) - 1, i, height=30)
        for col, fmt in ((4, "0.0"), (5, "0.0%"), (6, "0.0%"), (10, "0.0000"), (11, "0.0")):
            ws.cell(row=r, column=col).number_format = fmt
            ws.cell(row=r, column=col).alignment = a_num()
        tcell = ws.cell(row=r, column=8)
        tcell.font = Font(name=FONT_NAME, size=11, color=POS if row["tier"] == "T1"
                          else NEG if row["tier"] == "T3" else N900)
        if row["gate"] == "⚠":
            ws.cell(row=r, column=7).font = Font(name=FONT_NAME, size=11, color=WARN)
    caption(ws, r + 1, "注: 创造力列为LLM评审参考分(不参与梯队); 费用为按所配单价的估算值; "
                       "画像摘要完整标注见下表。")

    # ---- 三、z画像明细 ----
    dims_list = cf["obj_dims"] + (["创造力"] if cf["rows"] and cf["rows"][0].get("creativity") is not None else [])
    r += 3
    section_title(ws, r, "三、能力画像明细（数值% + z分数 + 标签; 创造力列仅供参考不打标签）", last)
    r += 1
    put_headers(ws, ["模型", "名称"] + [d if d != "创造力" else "创造力(参考)" for d in dims_list], row=r)
    for i, zm in enumerate(cf["zmatrix"]):
        r += 1
        ws.cell(row=r, column=2, value=zm["id"])
        ws.cell(row=r, column=3, value=clean(zm["name"]))
        style_row(ws, r, 2, 3 + len(dims_list), i)
        for j, cell in enumerate(zm["cells"], 4):
            if cell["val"] is None:
                continue
            txt = f"{cell['val']}% (z{cell['z']:+.1f})" + (" " + cell["label"] if cell["label"] else "")
            c = ws.cell(row=r, column=j, value=txt)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if "◆" in cell["label"]:
                c.font = Font(name=FONT_NAME, size=10, color=POS)
            elif "✖" in cell["label"] or "▼" in cell["label"]:
                c.font = Font(name=FONT_NAME, size=10, color=NEG)
            else:
                c.font = Font(name=FONT_NAME, size=10, color=N600 if cell["dim"] == "创造力" else N900)

    # ---- 四、题目区分度 ----
    r += 2
    section_title(ws, r, "四、题目区分度（评题不评模型, 为下轮换题服务）", last)
    r += 1
    put_headers(ws, ["维度", "群体均值%", "标准差pp", "难度判定", "区分度判定", "下轮建议"], row=r)
    for i, d in enumerate(cf["disc"]):
        r += 1
        vals = [d["dim"] + ("(参考)" if d["subjective"] else ""), d["mean"], d["std"],
                d["hard"], d["disc"], clean(d["advice"])]
        for j, v in enumerate(vals, 2):
            ws.cell(row=r, column=j, value=v)
        style_row(ws, r, 2, 7, i)
        for col in (3, 4):
            ws.cell(row=r, column=col).alignment = a_num()
        if d["disc"] == "区分度不足":
            ws.cell(row=r, column=6).font = Font(name=FONT_NAME, size=11, color=WARN)

    set_widths(ws, {"B": 10, "C": 17, "D": 13, "E": 10, "F": 10, "G": 8, "H": 7,
                    "I": 11, "J": 12, "K": 12, "L": 12, "M": 40})
    ws.freeze_panes = "D5"
    return ws


# ---------------------------------------------------------------- main
def main():
    cfg, questions, scored = load_all()
    if not scored:
        sys.exit("results/scored.jsonl 为空: 请先运行 runner.py 与 scorer.py")
    print(f"模型配置 {len(cfg.get('models', []))} 个 | 题库 {len(questions)} 题 | 评分记录 {len(scored)} 条")

    wb = Workbook()
    wb.remove(wb.active)
    sheet_readme(wb)
    n_models = sheet_models(wb, cfg, scored)
    sheet_questions(wb, questions)
    r_last = sheet_records(wb, questions, scored)
    sheet_summary(wb, n_models, r_last, derive_dims(questions))
    sheet_classify(wb)
    sheet_rubric(wb)

    for ws in wb.worksheets:
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
    wb.calculation.fullCalcOnLoad = True  # 打开即重算, 公式结果即刻可见
    wb.properties.creator = "LLM-Eval Toolchain"
    out = BASE / "results" / "测试报告.xlsx"
    wb.save(out)
    print("已生成 " + str(out))
    try:  # 同步一份到桌面, 方便直接取用
        import shutil
        from runner import desktop_dir
        desk = desktop_dir() / "测试报告.xlsx"
        shutil.copyfile(out, desk)
        print("桌面副本 → " + str(desk))
    except Exception as e:
        print("桌面复制失败(不影响报告): " + str(e)[:80])


if __name__ == "__main__":
    main()
