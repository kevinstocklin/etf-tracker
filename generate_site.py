#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_site.py
讀取 data/etf_holdings.db，產生一個靜態網頁 docs/index.html：
- 每檔ETF目前完整持股清單（依比例排序）
- 若有前一天資料，附上換股比對（新增／剔除／比例增減）

搭配GitHub Pages使用：repo設定 Settings > Pages > Source 選
"Deploy from a branch"，branch選main，資料夾選 /docs，
存好後會得到一個公開網址，之後每次GitHub Actions跑完都會自動更新這個網頁。

用法：
    python generate_site.py
"""

import sqlite3
from pathlib import Path
from datetime import datetime

import pandas as pd

from diff_report import (
    load_all_dates,
    load_snapshot,
    diff_two_snapshots,
    SIGNIFICANT_CHANGE_PCT,
    compute_overlap,
    load_latest_aum,
    compute_aggregate_holdings,
)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "etf_holdings.db"
OUT_DIR = BASE_DIR / "docs"
OUT_FILE = OUT_DIR / "index.html"

HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>台灣主動式ETF 每日換股追蹤</title>
<style>
  :root {
    --bg: #0f1115;
    --card: #171a21;
    --text: #e8e8e8;
    --muted: #9aa0a6;
    --up: #ff5c5c;
    --down: #4caf50;
    --accent: #6ea8fe;
    --border: #2a2e37;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
    margin: 0;
    padding: 24px 16px 80px;
  }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  .updated { color: var(--muted); font-size: 0.85rem; margin-bottom: 24px; }
  .etf-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 14px;
    overflow: hidden;
  }
  summary {
    cursor: pointer;
    padding: 14px 18px;
    font-weight: 600;
    font-size: 1.05rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  summary .meta { color: var(--muted); font-weight: 400; font-size: 0.85rem; }
  .content { padding: 0 18px 18px; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.9rem; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .tag { display: inline-block; padding: 1px 8px; border-radius: 20px; font-size: 0.78rem; }
  .tag.new { background: rgba(76,175,80,0.15); color: var(--down); }
  .tag.dropped { background: rgba(255,92,92,0.15); color: var(--up); }
  .delta-up { color: var(--up); }
  .delta-down { color: var(--down); }
  .section-title { margin: 14px 0 6px; font-size: 0.95rem; color: var(--accent); }
  .empty-note { color: var(--muted); font-size: 0.85rem; padding: 4px 0; }
  .overlap-card {
    background: var(--card);
    border: 1px solid var(--accent);
    border-radius: 10px;
    padding: 14px 18px 18px;
    margin-bottom: 20px;
  }
  .overlap-card h2 { margin: 0 0 4px; font-size: 1.05rem; color: var(--accent); }
  .overlap-card .hint { color: var(--muted); font-size: 0.82rem; margin-bottom: 4px; }
</style>
</head>
<body>
<h1>台灣主動式ETF 每日換股追蹤</h1>
<div class="updated">最後更新：__UPDATED_AT__</div>
"""

HTML_TAIL = """
</body>
</html>
"""


def load_fund_info(conn) -> dict:
    """回傳每檔ETF最新一次抓到的基金規模資訊：{etf_id: {etf_name, aum_million_twd, aum_date, constituent_count}}"""
    q = """
        SELECT etf_id, etf_name, aum_million_twd, aum_date, constituent_count
        FROM fund_info
        WHERE (etf_id, fetch_date) IN (
            SELECT etf_id, MAX(fetch_date) FROM fund_info GROUP BY etf_id
        )
    """
    try:
        df = pd.read_sql_query(q, conn)
    except Exception:
        return {}
    return {row.etf_id: row._asdict() for row in df.itertuples()}


def format_aum(aum_million_twd) -> str:
    if aum_million_twd is None or pd.isna(aum_million_twd):
        return "規模尚未公布"
    yi = aum_million_twd / 100  # 百萬台幣 -> 億台幣
    return f"規模 {yi:,.1f} 億"


def render_overlap_table(overlap_df: pd.DataFrame) -> str:
    if overlap_df is None or overlap_df.empty:
        return """
        <div class="overlap-card">
          <h2>同步加碼觀察表</h2>
          <div class="hint">目前沒有被2檔以上ETF同時加碼的個股</div>
        </div>
        """
    rows = ""
    for r in overlap_df.itertuples():
        shares_txt = (
            f"{r.avg_shares_pct_change:+.1f}%" if pd.notna(r.avg_shares_pct_change) else "N/A（含新增持股）"
        )
        amt_txt = f"{r.total_amount_delta_wan:+,.0f} 萬" if pd.notna(r.total_amount_delta_wan) else "N/A"
        rows += (
            f"<tr><td>{r.stock_name}({r.stock_code})</td><td>{r.etf_count}</td>"
            f"<td>{r.etfs}</td><td class='delta-up'>+{r.avg_delta:.2f}</td>"
            f"<td class='delta-up'>{shares_txt}</td><td class='delta-up'>{amt_txt}</td></tr>"
        )
    return f"""
    <div class="overlap-card">
      <h2>同步加碼觀察表</h2>
      <div class="hint">最新一天被2檔以上ETF同時加碼（含新增持股）的個股，股數加碼力道比比例變動更能反映真實買進力道；金額為用最新已知規模估算，非精確數字</div>
      <table>
        <tr><th>股票</th><th>同時加碼檔數</th><th>加碼的ETF</th><th>平均比例加碼</th><th>平均股數加碼力道</th><th>合計估計加碼金額</th></tr>
        {rows}
      </table>
    </div>
    """


def render_holdings_table(df: pd.DataFrame) -> str:
    df = df.sort_values("weight_pct", ascending=False)
    rows = "".join(
        f"<tr><td>{r.stock_name}</td><td>{r.stock_code}</td>"
        f"<td>{'' if pd.isna(r.weight_pct) else f'{r.weight_pct:.2f}%'}</td>"
        f"<td>{'' if pd.isna(r.shares) else f'{r.shares:,.0f}'}</td>"
        f"<td>{'' if pd.isna(r.shares) else f'{r.shares/1000:,.1f}'}</td></tr>"
        for r in df.itertuples()
    )
    return f"""
    <div class="section-title">目前完整持股（共 {len(df)} 檔）</div>
    <table>
      <tr><th>股票</th><th>代碼</th><th>權重</th><th>持有股數</th><th>張數</th></tr>
      {rows}
    </table>
    """


def render_diff_section(new_in_df, dropped_df, changes_df, prev_date, today_date) -> str:
    parts = [f'<div class="section-title">與前次（{prev_date} → {today_date}）比對</div>']

    if new_in_df.empty and dropped_df.empty:
        parts.append('<div class="empty-note">無新增或剔除的持股</div>')
    else:
        if not new_in_df.empty:
            tags = "".join(
                f'<span class="tag new">+ {r.stock_name}({r.stock_code}) {r.weight_pct:.2f}%'
                f'{"" if pd.isna(r.shares) else f"／{r.shares:,.0f}股"}</span> '
                for r in new_in_df.itertuples()
            )
            parts.append(f"<div>{tags}</div>")
        if not dropped_df.empty:
            tags = "".join(
                f'<span class="tag dropped">- {r.stock_name}({r.stock_code}) {r.weight_pct:.2f}%</span> '
                for r in dropped_df.itertuples()
            )
            parts.append(f"<div>{tags}</div>")

    sig = changes_df[changes_df["delta"].abs() >= SIGNIFICANT_CHANGE_PCT] if not changes_df.empty else changes_df
    if sig is not None and not sig.empty:
        rows = ""
        for r in sig.itertuples():
            cls = "delta-up" if r.delta > 0 else "delta-down"
            arrow = "▲" if r.delta > 0 else "▼"
            if pd.notna(r.shares_delta):
                pct_txt = f"（{r.shares_pct_change:+.1f}%）" if pd.notna(r.shares_pct_change) else ""
                shares_txt = f"{r.shares_delta:+,.0f}股{pct_txt}"
                lots_txt = f"{r.lots_delta:+,.1f}張"
            else:
                shares_txt = "N/A"
                lots_txt = "N/A"
            amt_txt = f"{r.amount_delta_wan:+,.0f}萬" if pd.notna(r.amount_delta_wan) else "N/A"
            rows += (
                f"<tr><td>{r.stock_name}({r.stock_code})</td><td>{r.prev_weight:.2f}%</td>"
                f"<td>{r.today_weight:.2f}%</td>"
                f"<td class='{cls}'>{arrow} {r.delta:+.2f}</td>"
                f"<td class='{cls}'>{shares_txt}</td>"
                f"<td class='{cls}'>{lots_txt}</td>"
                f"<td class='{cls}'>{amt_txt}</td></tr>"
            )
        parts.append(f"""
        <table>
          <tr><th>股票</th><th>前次</th><th>最新</th><th>比例變動</th><th>股數變動</th><th>張數變動</th><th>金額變動</th></tr>
          {rows}
        </table>
        """)

    return "".join(parts)


def render_aggregate_table(agg_df: pd.DataFrame) -> str:
    if agg_df is None or agg_df.empty:
        return """
        <details class="etf-card">
          <summary>全部ETF個股加總持股</summary>
          <div class="content"><div class="empty-note">目前沒有資料</div></div>
        </details>
        """
    rows = ""
    for r in agg_df.itertuples():
        lots_txt = f"{r.total_shares/1000:,.1f}" if pd.notna(r.total_shares) else "N/A"
        amt_txt = f"{r.total_amount_wan:,.0f}" if pd.notna(r.total_amount_wan) else "N/A"
        rows += (
            f"<tr><td>{r.stock_name}({r.stock_code})</td><td>{r.etf_count}</td>"
            f"<td>{r.etfs}</td><td>{lots_txt}</td><td>{amt_txt}</td><td>{r.avg_weight_pct:.2f}%</td></tr>"
        )
    return f"""
    <details class="etf-card" open>
      <summary>全部ETF個股加總持股（共 {len(agg_df)} 檔個股）
        <span class="meta">依合計估計金額排序</span>
      </summary>
      <div class="content">
        <div class="empty-note">把所有主動式ETF目前的持股加總，看整體最集中壓在哪些個股；金額為估算值，用最新已知規模反推，非精確數字</div>
        <table>
          <tr><th>股票</th><th>被幾檔ETF持有</th><th>持有的ETF</th><th>合計張數</th><th>合計估計金額（萬元）</th><th>平均權重</th></tr>
          {rows}
        </table>
      </div>
    </details>
    """


def build_etf_card(conn, etf_id: str, fund_info: dict) -> str:
    dates = load_all_dates(conn, etf_id)
    if not dates:
        return ""

    today_date = dates[0]
    today_df = load_snapshot(conn, etf_id, today_date)

    diff_html = ""
    if len(dates) >= 2:
        prev_date = dates[1]
        prev_df = load_snapshot(conn, etf_id, prev_date)
        aum = load_latest_aum(conn, etf_id)
        new_in_df, dropped_df, changes_df = diff_two_snapshots(today_df, prev_df, aum_million_twd=aum)
        diff_html = render_diff_section(new_in_df, dropped_df, changes_df, prev_date, today_date)
    else:
        diff_html = '<div class="empty-note">尚無前一天資料可比對</div>'

    holdings_html = render_holdings_table(today_df)

    info = fund_info.get(etf_id, {})
    etf_name = info.get("etf_name") or ""
    aum_text = format_aum(info.get("aum_million_twd"))
    aum_date = info.get("aum_date")
    aum_suffix = f"（{aum_date}）" if aum_date else ""

    return f"""
    <details class="etf-card">
      <summary>{etf_id} {etf_name}
        <span class="meta">資料日期 {today_date}｜{aum_text}{aum_suffix}</span>
      </summary>
      <div class="content">
        {diff_html}
        {holdings_html}
      </div>
    </details>
    """


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    etf_ids = [
        r[0] for r in conn.execute("SELECT DISTINCT etf_id FROM holdings ORDER BY etf_id").fetchall()
    ]

    fund_info = load_fund_info(conn)
    overlap_df = compute_overlap(conn, etf_ids, min_etfs=2)
    overlap_html = render_overlap_table(overlap_df)

    agg_df = compute_aggregate_holdings(conn, etf_ids)
    aggregate_html = render_aggregate_table(agg_df)

    cards = "".join(build_etf_card(conn, etf_id, fund_info) for etf_id in etf_ids)

    html = HTML_HEAD.replace(
        "__UPDATED_AT__", datetime.now().strftime("%Y-%m-%d %H:%M")
    ) + overlap_html + aggregate_html + cards + HTML_TAIL

    OUT_FILE.write_text(html, encoding="utf-8")
    conn.close()
    print(f"網頁已產生：{OUT_FILE}")


if __name__ == "__main__":
    main()
