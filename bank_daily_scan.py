#!/usr/bin/env python3
"""
银行股每日扫描 —— 基于「大佬刘战法一」的双估值买入点提示。

估值逻辑（来自文章）：
  1. PE 估值：追求 15% 年化收益 → 基准 PE = 1/15% = 6.67；
     优秀银行可给 1~3 年时间换空间溢价 → 7.67~10.14，超过 10PE 不考虑。
     PE 估值价 = 预估自然年 EPS × 目标 PE
  2. 股息率估值：国有大行 5%+，优秀股份行/城商行 3%+（配置文件中按银行微调）。
     股息率估值价 = 最近财年每股分红 ÷ 目标股息率
  3. 两者取低者 = 基准价；再考虑"即将派发的分红" → 参考上车价。

自然年 PE（线性预估）：
  取最近 4~6 个年度 EPS，最小二乘线性外推当年自然年 EPS；
  预估自然年 PE = 现价 ÷ 预估自然年 EPS。

输出：
  output/bank_scan_YYYYMMDD.csv   全量明细
  output/bank_scan_YYYYMMDD.xlsx  上车提示表（含多 sheet）
  output/ai_interpret_YYYYMMDD.md DeepSeek 解读（配置了 DEEPSEEK_API_KEY 时）

依赖：tushare / pandas / numpy / requests / openpyxl / python-dotenv
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_FILE = BASE_DIR / "bank_valuation_config.csv"

# 国有大行 / 股份行名单（名称关键词判断类型用）
BIG_BANKS = {"工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮储银行"}
JOINT_STOCK_BANKS = {
    "招商银行", "浦发银行", "民生银行", "兴业银行", "光大银行",
    "中信银行", "华夏银行", "平安银行", "浙商银行", "渤海银行",
}

# 线性外推用的最近年度 EPS 数量
EPS_REGRESSION_YEARS = 5


def get_token(name: str = "TUSHARE_TOKEN") -> str:
    token = os.getenv(name, "").strip()
    if not token or token == "your_token_here":
        print(
            f"请设置 {name}：复制 .env.example 为 .env 并填入真实 token",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


def get_pro() -> ts.pro_api:
    return ts.pro_api(get_token("TUSHARE_TOKEN"))


def latest_trade_date(pro) -> str:
    """取最近一个交易日（daily_basic 按 trade_date 拉全市场）。"""
    end = datetime.now()
    for _ in range(10):
        d = end.strftime("%Y%m%d")
        df = pro.daily_basic(trade_date=d, fields="ts_code")
        if df is not None and not df.empty:
            return d
        end -= timedelta(days=1)
    raise RuntimeError("无法获取最近交易日，请稍后重试")


# ---------------------------------------------------------------- 配置

def classify_bank(name: str) -> str:
    """按名称归类：国有大行 / 股份行 / 农商行 / 城商行。"""
    if name in BIG_BANKS:
        return "国有大行"
    if name in JOINT_STOCK_BANKS:
        return "股份行"
    if "农商" in name:
        return "农商行"
    return "城商行"


def type_defaults(bank_type: str) -> tuple[float, float]:
    """未在配置文件中列出的银行，按类型给默认目标PE和股息率%。"""
    return {
        "国有大行": (6.67, 5.5),
        "股份行": (6.67, 5.0),
        "城商行": (6.67, 4.5),
        "农商行": (6.67, 4.5),
    }.get(bank_type, (6.67, 4.5))


def load_valuation_config() -> pd.DataFrame:
    """读取估值配置 CSV；文件缺失则返回空表。"""
    if not CONFIG_FILE.exists():
        return pd.DataFrame(
            columns=[
                "ts_code", "name", "type", "target_pe",
                "target_div_yield", "quality", "note",
            ]
        )
    return pd.read_csv(CONFIG_FILE, dtype={"ts_code": str})


def build_params(
    banks: pd.DataFrame, config: pd.DataFrame
) -> pd.DataFrame:
    """合并配置与默认值，生成每只银行的估值参数。"""
    # ts_code 兼容 6 位与带后缀（600036 / 600036.SH）两种写法
    cfg_map = None
    if not config.empty:
        cfg = config.copy()
        cfg["code6"] = cfg["ts_code"].astype(str).str[:6]
        cfg_map = cfg.set_index("code6")

    rows = []
    for _, b in banks.iterrows():
        code = b["ts_code"]
        code6 = code[:6]
        if cfg_map is not None and code6 in cfg_map.index:
            c = cfg_map.loc[code6]
            rows.append(
                {
                    "ts_code": code,
                    "name": b["name"],
                    "type": c["type"] if pd.notna(c["type"]) else classify_bank(b["name"]),
                    "target_pe": float(c["target_pe"]),
                    "target_div_yield": float(c["target_div_yield"]),
                    "quality": str(c["quality"]) if pd.notna(c.get("quality")) else "一般",
                }
            )
        else:
            bank_type = classify_bank(b["name"])
            pe, dv = type_defaults(bank_type)
            rows.append(
                {
                    "ts_code": code,
                    "name": b["name"],
                    "type": bank_type,
                    "target_pe": pe,
                    "target_div_yield": dv,
                    "quality": "一般",
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 数据拉取

def fetch_bank_universe(pro) -> pd.DataFrame:
    basic = pro.stock_basic(
        list_status="L",
        fields="ts_code,symbol,name,industry,exchange",
    )
    banks = basic[basic["industry"] == "银行"].copy()
    if banks.empty:
        raise RuntimeError("未筛选到 industry=银行 的股票，请检查 stock_basic 数据")
    return banks.sort_values("ts_code").reset_index(drop=True)


def fetch_daily(pro, trade_date: str) -> pd.DataFrame:
    df = pro.daily_basic(
        trade_date=trade_date,
        fields="ts_code,close,pe,pe_ttm,pb,dv_ratio,dv_ttm,total_mv",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def fetch_dividends(pro, ts_code: str) -> pd.DataFrame:
    fields = (
        "ts_code,end_date,div_proc,cash_div,cash_div_tax,"
        "record_date,ex_date,pay_date"
    )
    df = pro.dividend(ts_code=ts_code, fields=fields)
    if df is None or df.empty:
        return pd.DataFrame()
    for col in ("end_date", "ex_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in ("cash_div", "cash_div_tax"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def fetch_annual_eps(pro, ts_code: str) -> list[tuple[int, float]]:
    """返回 [(报告年度, 基本每股收益)]，按年度升序，仅取年报 end_date=1231。"""
    df = pro.fina_indicator(
        ts_code=ts_code,
        fields="ts_code,end_date,eps",
    )
    if df is None or df.empty:
        return []
    df["end_date"] = pd.to_datetime(df["end_date"].astype(str), errors="coerce")
    df["eps"] = pd.to_numeric(df["eps"], errors="coerce")
    annual = df[
        df["end_date"].notna()
        & (df["end_date"].dt.month == 12)
        & (df["end_date"].dt.day == 31)
        & df["eps"].notna()
    ].copy()
    annual["year"] = annual["end_date"].dt.year
    # 同一年份只保留最新一条（若因重述出现多条）
    annual = (
        annual.sort_values("end_date")
        .groupby("year")["eps"]
        .last()
        .reset_index()
    )
    return list(zip(annual["year"].astype(int), annual["eps"].astype(float)))


# ---------------------------------------------------------------- 线性外推

def linear_estimate_eps(
    annual: list[tuple[int, float]], target_year: int
) -> float | None:
    """
    线性回归外推自然年 EPS：
    eps = a + b * (year - base_year)，取最近 EPS_REGRESSION_YEARS 个年度拟合。
    不足 2 个年度无法回归时返回 None（至少需要 2 个点）。
    """
    if len(annual) < 2:
        return None
    recent = annual[-EPS_REGRESSION_YEARS:]
    years = np.array([y for y, _ in recent], dtype=float)
    eps = np.array([e for _, e in recent], dtype=float)
    base = years[0]
    try:
        b, a = np.polyfit(years - base, eps, 1)
    except np.linalg.LinAlgError:
        return None
    pred = a + b * (target_year - base)
    return float(pred) if pred > 0 else None


def eps_growth_estimate(
    annual: list[tuple[int, float]], target_year: int
) -> float | None:
    """对照口径：最近年度EPS × (1 + 近N年平均同比增速)。"""
    if len(annual) < 2:
        return None
    recent = annual[-EPS_REGRESSION_YEARS:]
    last_year, last_eps = recent[-1]
    growths = []
    for (y0, e0), (y1, e1) in zip(recent[:-1], recent[1:]):
        if e0 > 0:
            growths.append((e1 - e0) / e0)
    if not growths:
        return None
    avg_g = float(np.mean(growths))
    pred = last_eps * (1 + avg_g) ** (target_year - last_year)
    return float(pred) if pred > 0 else None


# ---------------------------------------------------------------- 单只银行计算

def collect_stock_data(pro, ts_code: str) -> dict:
    """拉取单只银行的分红 + 年度EPS，供并行调用。"""
    div = fetch_dividends(pro, ts_code)
    eps = fetch_annual_eps(pro, ts_code)
    return {"div": div, "eps": eps}


# Tushare 分红状态优先级：同笔分红会出现 预案→股东大会通过→实施 多条记录
DIV_PROC_RANK = {"实施": 2, "股东大会通过": 1, "预案": 0}


def dps_recent_fiscal_year(div: pd.DataFrame) -> tuple[float | None, int | None]:
    """
    最近分红财年每股税前现金分红：
    - 排除「股东提议」等未生效状态；
    - 同一 end_date 笔次内只保留优先级最高的一条（实施 > 股东大会通过 > 预案）；
    - 取 end_date 财年最大的年度合计；若该年度无有效分红则逐年前溯。
    """
    if div.empty:
        return None, None
    d = div[div["end_date"].notna()].copy()
    if d.empty:
        return None, None
    d["year"] = d["end_date"].dt.year
    d["pri"] = d["div_proc"].map(DIV_PROC_RANK)
    d = d[d["pri"].notna()]
    if d.empty:
        return None, None
    # 同 end_date 去重：保留优先级最高的记录
    best = d.loc[d.groupby("end_date")["pri"].idxmax()]
    for y in sorted(best["year"].unique(), reverse=True):
        part = best[best["year"] == y]
        total = float(part["cash_div_tax"].sum())
        if total > 0:
            return total, int(y)
    return None, None


def evaluate_stock(row: pd.Series, data: dict, close: float | None) -> dict:
    """按文章战法计算估值、PE、买入点与上车信号。"""
    code = row["ts_code"]
    target_pe = row["target_pe"]
    target_dv = row["target_div_yield"]

    # 最近财年分红（每股税前现金分红合计）
    dps, fy = dps_recent_fiscal_year(data["div"])
    # 最近年度EPS（静态）
    last_eps = data["eps"][-1][1] if data["eps"] else None
    # 线性外推自然年EPS
    today_year = datetime.now().year
    eps_est = linear_estimate_eps(data["eps"], today_year)
    eps_growth = eps_growth_estimate(data["eps"], today_year)

    out: dict = {
        "ts_code": code,
        "name": row["name"],
        "type": row["type"],
        "quality": row["quality"],
        "close": close,
        "target_pe": target_pe,
        "target_div_yield": target_dv,
        "dps_recent_fy": dps,
        "dps_fiscal_year": fy,
        "eps_last_annual": last_eps,
        "eps_natural_year_est": eps_est,
        "eps_growth_est": eps_growth,
        "pe_natural_year": None,
        "pe_static": None,
        "pe_ttm_ref": None,
        "pe_price": None,
        "div_price": None,
        "base_price": None,
        "buy_price": None,
        "signal": "数据不足",
    }

    if not close:
        out["signal"] = "无行情"
        return out

    if dps is not None:
        out["div_price"] = round(dps / (target_dv / 100.0), 3)

    if eps_est is not None:
        out["eps_natural_year_est"] = round(eps_est, 4)
        out["pe_natural_year"] = round(close / eps_est, 2)
        out["pe_price"] = round(eps_est * target_pe, 3)
    elif last_eps is not None:
        # 外推失败时退回静态EPS
        out["pe_natural_year"] = round(close / last_eps, 2)
        out["pe_price"] = round(last_eps * target_pe, 3)

    # 取低者 + 即将派发分红加成
    if out["pe_price"] is not None and out["div_price"] is not None:
        out["base_price"] = round(min(out["pe_price"], out["div_price"]), 3)
    elif out["pe_price"] is not None:
        out["base_price"] = out["pe_price"]
    elif out["div_price"] is not None:
        out["base_price"] = out["div_price"]

    if out["base_price"] is not None:
        # 即将派发分红：有在途预案/实施，加最近财年DPS
        out["buy_price"] = round(out["base_price"] + (dps or 0.0), 3)

    # 上车信号
    if out["buy_price"] is None:
        out["signal"] = "数据不足"
    elif close <= out["base_price"]:
        out["signal"] = "射击范围"
    elif close <= out["buy_price"]:
        out["signal"] = "参考上车"
    else:
        drop = (close - out["buy_price"]) / close * 100
        out["signal"] = f"等待 · 需跌{drop:.1f}%"

    return out


# ---------------------------------------------------------------- DeepSeek 解读

def deepseek_interpret(df: pd.DataFrame, trade_date: str) -> str | None:
    """调用 DeepSeek 生成当日上车解读；未配置 key 时返回 None。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    import requests

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

    # 组装输入表：只挑关键列
    show_cols = [
        "name", "close", "eps_natural_year_est", "pe_natural_year",
        "target_pe", "dps_recent_fy", "target_div_yield", "div_price",
        "pe_price", "base_price", "buy_price", "signal",
    ]
    table_text = df[show_cols].to_string(index=False)

    prompt = f"""你是资深银行股价值投资者，遵循"大佬刘战法一"的双估值框架（PE估值与股息率估值取低者，加上即将派发的分红作为上车价）。

今天是{trade_date}，以下是我的银行股扫描结果：

{table_text}

请用中文输出一份简洁的每日上车提示（800字以内）：
1. 哪些银行已进入"射击范围"（现价低于基准双估值价）或"参考上车"区间，按优先级排序并给出理由；
2. 对接近但未达标的银行，提示还需跌多少才到参考上车价；
3. 结合银行质量（优秀/一般）给出仓位建议与风险提示（注意业绩增速、资产质量）。
语气平实，给出可执行的提示，不要输出表格以外的编造数据。"""

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是一位严谨的A股银行股价值投资分析助手。",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.6,
                "max_tokens": 1200,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[DeepSeek] 解读失败：{exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 主流程

def run(
    save_output: bool = True,
    with_ai: bool = True,
    max_workers: int = 8,
) -> pd.DataFrame:
    pro = get_pro()
    trade_date = latest_trade_date(pro)
    print(f"[1/5] 最近交易日：{trade_date}")

    banks = fetch_bank_universe(pro)
    print(f"[2/5] 银行股数量：{len(banks)}")

    config = load_valuation_config()
    params = build_params(banks, config)
    codes = banks["ts_code"].tolist()

    daily = fetch_daily(pro, trade_date)
    daily = daily[daily["ts_code"].isin(codes)].set_index("ts_code")
    print(f"[3/5] 当日行情已获取 {len(daily)} 只")

    token = get_token("TUSHARE_TOKEN")

    def _fetch(code: str) -> tuple[str, dict]:
        # 每线程独立 pro 实例，避免并发共享连接问题
        p = ts.pro_api(token)
        return code, collect_stock_data(p, code)

    print("[4/5] 拉取分红与财务数据（并行）...")
    stock_data: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch, c) for c in codes]
        for i, fut in enumerate(as_completed(futures), 1):
            code, data = fut.result()
            stock_data[code] = data
            if i % 10 == 0 or i == len(codes):
                print(f"      {i}/{len(codes)}")

    rows = []
    for _, p in params.iterrows():
        code = p["ts_code"]
        close = None
        if code in daily.index:
            close = float(daily.loc[code, "close"])
        r = evaluate_stock(
            p,
            stock_data.get(code, {"div": pd.DataFrame(), "eps": []}),
            close=close,
        )
        if code in daily.index:
            d = daily.loc[code]
            r["pe_static"] = None if pd.isna(d["pe"]) else round(float(d["pe"]), 2)
            r["pe_ttm_ref"] = None if pd.isna(d["pe_ttm"]) else round(float(d["pe_ttm"]), 2)
            r["dv_ttm_ref"] = None if pd.isna(d["dv_ttm"]) else round(float(d["dv_ttm"]), 2)
            r["pb_ref"] = None if pd.isna(d["pb"]) else round(float(d["pb"]), 2)
            r["total_mv_yi"] = (
                None if pd.isna(d["total_mv"]) else round(float(d["total_mv"]) / 1e4, 0)
            )
        rows.append(r)

    result = pd.DataFrame(rows)
    result.insert(0, "trade_date", trade_date)

    # 排序：射击范围/参考上车 在前
    def signal_rank(s: str) -> int:
        if s == "射击范围":
            return 0
        if s == "参考上车":
            return 1
        if s.startswith("等待"):
            return 2
        return 3

    result["_rank"] = result["signal"].map(signal_rank)
    result = result.sort_values(
        ["_rank", "close"], ascending=[True, True]
    ).drop(columns="_rank").reset_index(drop=True)

    if save_output:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        result.to_csv(
            OUTPUT_DIR / f"bank_scan_{trade_date}.csv",
            index=False,
            encoding="utf-8-sig",
        )
        print(f"[5/5] 明细已保存：{OUTPUT_DIR / f'bank_scan_{trade_date}.csv'}")

    if with_ai:
        md = deepseek_interpret(result, trade_date)
        if md:
            out_md = OUTPUT_DIR / f"ai_interpret_{trade_date}.md"
            out_md.write_text(
                f"# 银行股每日上车提示 · {trade_date}\n\n" + md + "\n",
                encoding="utf-8",
            )
            print(f"      AI 解读已保存：{out_md}")
        else:
            print("      未配置 DEEPSEEK_API_KEY，跳过 AI 解读")

    return result


# ---------------------------------------------------------------- 展示

def signal_short(s: str) -> str:
    if s == "射击范围":
        return "●射击范围"
    if s == "参考上车":
        return "○参考上车"
    if s.startswith("等待"):
        return "…等待"
    return "—"


def print_summary(df: pd.DataFrame) -> None:
    trade_date = df["trade_date"].iloc[0]
    in_range = df["signal"].eq("射击范围").sum()
    near = df["signal"].eq("参考上车").sum()
    waiting = df["signal"].str.startswith("等待").sum()

    print()
    print("=" * 90)
    print(f"银行股每日扫描 · 交易日 {trade_date} · 共 {len(df)} 只")
    print(
        f"射击范围 {in_range} 只 · 参考上车 {near} 只 · 等待 {waiting} 只"
    )
    print("=" * 90)

    display = pd.DataFrame(
        {
            "银行": df["name"],
            "现价": df["close"].round(2),
            "预估EPS": df["eps_natural_year_est"].round(3),
            "自然年PE": df["pe_natural_year"],
            "目标PE": df["target_pe"],
            "每股分红": df["dps_recent_fy"].round(3),
            "目标股息率%": df["target_div_yield"],
            "PE估值价": df["pe_price"].round(2),
            "股息率估值价": df["div_price"].round(2),
            "参考上车价": df["buy_price"].round(2),
            "信号": df["signal"].map(signal_short),
        }
    )
    # 只展示有估值或有信号的
    show = display[df["buy_price"].notna() | df["signal"].ne("数据不足")]

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 180)
    pd.set_option("display.unicode.east_asian_width", True)
    print(show.to_string(index=False))
    print()
    print("说明：")
    print("  自然年PE = 现价 ÷ 线性外推自然年EPS（最近5个年度EPS回归）")
    print("  PE估值价 = 预估自然年EPS × 目标PE（优秀银行7.67 / 一般6.67）")
    print("  股息率估值价 = 最近财年每股分红 ÷ 目标股息率")
    print("  基准价 = min(PE估值价, 股息率估值价)；参考上车价 = 基准价 + 即将派发分红")
    print("  射击范围 = 现价 ≤ 基准价；参考上车 = 现价 ≤ 参考上车价")


def export_excel(df: pd.DataFrame) -> Path:
    """输出带格式的 Excel：上车提示表 + 全量明细 + 估值参数。"""
    import openpyxl  # noqa: F401

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trade_date = df["trade_date"].iloc[0]
    path = OUTPUT_DIR / f"bank_scan_{trade_date}.xlsx"

    up = df[df["signal"].isin(["射击范围", "参考上车"])].copy()
    up_cols = [
        "name", "close", "eps_natural_year_est", "pe_natural_year",
        "target_pe", "dps_recent_fy", "target_div_yield",
        "pe_price", "div_price", "base_price", "buy_price", "signal",
    ]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df[up_cols].to_excel(writer, sheet_name="全量", index=False)
        up[up_cols].to_excel(writer, sheet_name="上车提示", index=False)
        config = load_valuation_config()
        if not config.empty:
            config.to_excel(writer, sheet_name="估值参数", index=False)
    return path


def update_dashboard(df: pd.DataFrame) -> Path:
    """把扫描数据注入 dashboard.html 模板，生成根目录 index.html（Pages 入口）。"""
    import json

    template = BASE_DIR / "dashboard.html"
    if not template.exists():
        print(f"  [跳过] 面板模板不存在: {template}")
        return template
    keep_cols = [
        "name", "type", "trade_date", "close", "pe_natural_year",
        "eps_natural_year_est", "dps_recent_fy", "pe_price",
        "div_price", "base_price", "buy_price", "signal",
    ]
    rows = []
    for _, r in df.iterrows():
        item = {}
        for c in keep_cols:
            if c in r and pd.notna(r[c]):
                item[c] = (
                    float(r[c]) if isinstance(r[c], (int, float, np.floating)) else r[c]
                )
        rows.append(item)
    data_json = json.dumps(rows, ensure_ascii=False)

    # AI 解读：读取最近的 ai_interpret_*.md
    ai_text = ""
    md_files = sorted(OUTPUT_DIR.glob("ai_interpret_*.md"), reverse=True)
    if md_files:
        ai_text = md_files[0].read_text(encoding="utf-8").strip()

    html = template.read_text(encoding="utf-8")
    html = html.replace("__DATA_PLACEHOLDER__", data_json)
    # 注入 AI 文本（转义为 JS 字符串）
    ai_js = json.dumps(ai_text, ensure_ascii=False)
    html = html.replace("__AI_PLACEHOLDER__", ai_js)
    out = BASE_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"  Dashboard 已更新：{out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="银行股每日双估值上车扫描")
    parser.add_argument("--no-save", action="store_true", help="不保存文件")
    parser.add_argument("--no-ai", action="store_true", help="跳过 DeepSeek 解读")
    parser.add_argument(
        "--workers", type=int, default=8, help="并行拉取线程数（默认8）"
    )
    args = parser.parse_args()

    df = run(
        save_output=not args.no_save,
        with_ai=not args.no_ai,
        max_workers=args.workers,
    )
    print_summary(df)
    if not args.no_save:
        path = export_excel(df)
        print(f"Excel 已保存：{path}")
    update_dashboard(df)


if __name__ == "__main__":
    main()
