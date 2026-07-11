#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diff_report.py
比對資料庫中每檔ETF「最新一次」與「前一次」的持股快照，
產出換股報告（新增／剔除／比例增減）。

用法：
    python diff_report.py                # 印出所有ETF的報告到終端機
    python diff_report.py --etf 00981A   # 只看單一檔
    python diff_report.py --md out.md    # 同時輸出成markdown檔案
"""

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "etf_holdings.db"

# 比例變動超過這個百分點才視為「顯著調整」，只是報告分類用，非強制篩選
SIGNIFICANT_CHANGE_PCT = 0.5


def load_all_dates(conn, etf_id):
    q = """
        SELECT DISTINCT snapshot_date FROM holdings
        WHERE etf_id = ?
        ORDER BY snapshot_date DESC
    """
    return [r[0] for r in conn.execute(q, (etf_id,)).fetchall()]


def load_snapshot(conn, etf_id, snapshot_date):
    q = """
        SELECT stock_code, stock_name, weight_pct, shares
        FROM holdings
        WHERE etf_id = ? AND snapshot_date = ?
    """
    return pd.read_sql_query(q, conn, params=(etf_id, snapshot_date))


def load_latest_aum(conn, etf_id):
    """取得該ETF最新一次抓到的「已知規模」（百萬台幣），供估算加減碼金額用。
    注意：ETF規模網站上通常是每月才更新一次，不是每天都有新數字，
    所以這裡算出來的金額是用「最新一次已知規模」估算，非當天精確數字。
    """
    try:
        row = conn.execute(
            """
            SELECT aum_million_twd FROM fund_info
            WHERE etf_id = ? AND aum_million_twd IS NOT NULL
            ORDER BY fetch_date DESC LIMIT 1
            """,
            (etf_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def diff_two_snapshots(today_df: pd.DataFrame, prev_df: pd.DataFrame, aum_million_twd: float | None = None):
    """比對兩次持股快照。
    aum_million_twd：該ETF最新已知規模（百萬台幣），若提供，會額外估算每檔股票的
    「加減碼金額」（weight_pct變化 × 規模，單位：萬元）。這是用比例反推的估算值，
    不是網站直接提供的精確持股金額（網站的持股明細頁面本身不含金額欄位）。
    """
    today_codes = set(today_df["stock_code"])
    prev_codes = set(prev_df["stock_code"])

    new_in = today_codes - prev_codes       # 新增持股
    dropped = prev_codes - today_codes      # 剔除持股
    common = today_codes & prev_codes       # 兩次都持有，比較比例／股數變化

    today_idx = today_df.set_index("stock_code")
    prev_idx = prev_df.set_index("stock_code")

    def amount_wan(weight_pct):
        """weight_pct(%) 換算成估算金額，單位：萬元"""
        if weight_pct is None or pd.isna(weight_pct) or aum_million_twd is None:
            return None
        return weight_pct / 100 * aum_million_twd * 100  # 百萬台幣 -> 萬元，乘100

    changes = []
    for code in common:
        today_w = today_idx.loc[code, "weight_pct"]
        prev_w = prev_idx.loc[code, "weight_pct"]
        today_shares = today_idx.loc[code, "shares"]
        prev_shares = prev_idx.loc[code, "shares"]

        delta = None
        if pd.notna(today_w) and pd.notna(prev_w):
            delta = round(today_w - prev_w, 2)

        shares_delta = None
        shares_pct_change = None
        lots_delta = None
        if pd.notna(today_shares) and pd.notna(prev_shares):
            shares_delta = today_shares - prev_shares
            lots_delta = shares_delta / 1000
            if prev_shares:
                shares_pct_change = round(shares_delta / prev_shares * 100, 2)

        amount_delta_wan = None
        if delta is not None:
            amt_today = amount_wan(today_w)
            amt_prev = amount_wan(prev_w)
            if amt_today is not None and amt_prev is not None:
                amount_delta_wan = round(amt_today - amt_prev, 1)

        changes.append(
            {
                "stock_code": code,
                "stock_name": today_idx.loc[code, "stock_name"],
                "prev_weight": prev_w,
                "today_weight": today_w,
                "delta": delta,
                "prev_shares": prev_shares,
                "today_shares": today_shares,
                "shares_delta": shares_delta,
                "shares_pct_change": shares_pct_change,
                "lots_delta": lots_delta,
                "amount_delta_wan": amount_delta_wan,
            }
        )
    changes_df = pd.DataFrame(changes)
    if not changes_df.empty:
        changes_df = changes_df.sort_values("delta", ascending=False, key=lambda s: s.abs())

    new_in_df = today_df[today_df["stock_code"].isin(new_in)][
        ["stock_code", "stock_name", "weight_pct", "shares"]
    ].copy()
    new_in_df["lots"] = new_in_df["shares"] / 1000
    new_in_df["amount_wan"] = new_in_df["weight_pct"].apply(amount_wan)
    new_in_df = new_in_df.sort_values("weight_pct", ascending=False)

    dropped_df = prev_df[prev_df["stock_code"].isin(dropped)][
        ["stock_code", "stock_name", "weight_pct", "shares"]
    ].copy()
    dropped_df["lots"] = dropped_df["shares"] / 1000
    dropped_df["amount_wan"] = dropped_df["weight_pct"].apply(amount_wan)
    dropped_df = dropped_df.sort_values("weight_pct", ascending=False)

    return new_in_df, dropped_df, changes_df


def format_report(etf_id, today_date, prev_date, new_in_df, dropped_df, changes_df, has_aum: bool = False) -> str:
    lines = []
    lines.append(f"## {etf_id}　換股報告（{prev_date} → {today_date}）\n")
    if not has_aum:
        lines.append("_（尚無ETF規模資料，金額欄位無法估算，只顯示股數／張數）_\n")

    if new_in_df.empty and dropped_df.empty:
        lines.append("- 無新增或剔除的持股\n")
    else:
        if not new_in_df.empty:
            lines.append("**新增持股：**\n")
            for _, r in new_in_df.iterrows():
                shares_txt = f"，{r['shares']:,.0f} 股（{r['lots']:,.1f} 張）" if pd.notna(r["shares"]) else ""
                amt_txt = f"，估計 {r['amount_wan']:,.0f} 萬元" if pd.notna(r.get("amount_wan")) else ""
                lines.append(f"- {r['stock_name']}({r['stock_code']})：{r['weight_pct']}%{shares_txt}{amt_txt}")
            lines.append("")
        if not dropped_df.empty:
            lines.append("**剔除持股：**\n")
            for _, r in dropped_df.iterrows():
                amt_txt = f"，原估計 {r['amount_wan']:,.0f} 萬元" if pd.notna(r.get("amount_wan")) else ""
                lines.append(f"- {r['stock_name']}({r['stock_code']})：原持有 {r['weight_pct']}%{amt_txt}")
            lines.append("")

    if changes_df.empty:
        return "\n".join(lines)

    sig = changes_df[changes_df["delta"].abs() >= SIGNIFICANT_CHANGE_PCT]
    if not sig.empty:
        lines.append(f"**比例顯著調整（變動 ≥ {SIGNIFICANT_CHANGE_PCT} 個百分點）：**\n")
        lines.append("| 股票 | 前次比例 | 最新比例 | 比例變動 | 股數變動 | 張數變動 | 金額變動（萬元） |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in sig.iterrows():
            arrow = "🔺加碼" if r["delta"] > 0 else "🔻減碼"
            if pd.notna(r["shares_delta"]):
                pct_txt = f"（{r['shares_pct_change']:+.1f}%）" if pd.notna(r["shares_pct_change"]) else ""
                shares_txt = f"{r['shares_delta']:+,.0f}{pct_txt}"
                lots_txt = f"{r['lots_delta']:+,.1f}"
            else:
                shares_txt = "N/A"
                lots_txt = "N/A"
            amt_txt = f"{r['amount_delta_wan']:+,.0f}" if pd.notna(r["amount_delta_wan"]) else "N/A"
            lines.append(
                f"| {r['stock_name']}({r['stock_code']}) | {r['prev_weight']}% | "
                f"{r['today_weight']}% | {r['delta']:+.2f} {arrow} | {shares_txt} | {lots_txt} | {amt_txt} |"
            )
        lines.append("")

    return "\n".join(lines)


def compute_overlap(conn, etf_ids: list[str], min_etfs: int = 2) -> pd.DataFrame:
    """找出「同一天被2檔以上ETF同時加碼（含新增持股）」的個股，
    回傳依「加碼ETF檔數」由多到少排序的表格。
    """
    rows = []
    for etf_id in etf_ids:
        dates = load_all_dates(conn, etf_id)
        if len(dates) < 2:
            continue
        today_date, prev_date = dates[0], dates[1]
        today_df = load_snapshot(conn, etf_id, today_date)
        prev_df = load_snapshot(conn, etf_id, prev_date)
        aum = load_latest_aum(conn, etf_id)
        new_in_df, _, changes_df = diff_two_snapshots(today_df, prev_df, aum_million_twd=aum)

        # 新增持股視為「從0加碼到目前比例／股數／金額」
        for _, r in new_in_df.iterrows():
            rows.append(
                {
                    "etf_id": etf_id,
                    "stock_code": r["stock_code"],
                    "stock_name": r["stock_name"],
                    "delta": r["weight_pct"],
                    "today_weight": r["weight_pct"],
                    "shares_delta": r["shares"],
                    "shares_pct_change": None,  # 新增持股沒有「前次股數」可算百分比
                    "amount_delta_wan": r.get("amount_wan"),
                    "type": "新增",
                }
            )
        if not changes_df.empty:
            for _, r in changes_df.iterrows():
                if pd.notna(r["delta"]) and r["delta"] > 0:
                    rows.append(
                        {
                            "etf_id": etf_id,
                            "stock_code": r["stock_code"],
                            "stock_name": r["stock_name"],
                            "delta": r["delta"],
                            "today_weight": r["today_weight"],
                            "shares_delta": r["shares_delta"],
                            "shares_pct_change": r["shares_pct_change"],
                            "amount_delta_wan": r["amount_delta_wan"],
                            "type": "加碼",
                        }
                    )

    if not rows:
        return pd.DataFrame(
            columns=[
                "stock_code", "stock_name", "etf_count", "etfs",
                "avg_delta", "avg_shares_pct_change", "total_amount_delta_wan",
            ]
        )

    df = pd.DataFrame(rows)
    grouped = (
        df.groupby(["stock_code", "stock_name"])
        .agg(
            etf_count=("etf_id", "nunique"),
            etfs=("etf_id", lambda s: "、".join(sorted(set(s)))),
            avg_delta=("delta", "mean"),
            avg_shares_pct_change=("shares_pct_change", "mean"),
            total_amount_delta_wan=("amount_delta_wan", lambda s: s.sum(skipna=True) if s.notna().any() else None),
        )
        .reset_index()
    )
    grouped = grouped[grouped["etf_count"] >= min_etfs].sort_values(
        ["etf_count", "avg_delta"], ascending=[False, False]
    )
    return grouped


def format_overlap_report(overlap_df: pd.DataFrame) -> str:
    if overlap_df.empty:
        return "## 同步加碼觀察表\n\n- 目前沒有被2檔以上ETF同時加碼的個股\n"

    lines = ["## 同步加碼觀察表\n"]
    lines.append("| 股票 | 同時加碼ETF數 | 加碼的ETF | 平均比例加碼幅度 | 平均股數加碼力道 | 合計估計加碼金額（萬元） |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for _, r in overlap_df.iterrows():
        shares_txt = (
            f"{r['avg_shares_pct_change']:+.1f}%" if pd.notna(r["avg_shares_pct_change"]) else "N/A（含新增持股）"
        )
        amt_txt = f"{r['total_amount_delta_wan']:+,.0f}" if pd.notna(r["total_amount_delta_wan"]) else "N/A"
        lines.append(
            f"| {r['stock_name']}({r['stock_code']}) | {r['etf_count']} | {r['etfs']} | "
            f"+{r['avg_delta']:.2f} | {shares_txt} | {amt_txt} |"
        )
    return "\n".join(lines) + "\n"


def compute_aggregate_holdings(conn, etf_ids: list[str]) -> pd.DataFrame:
    """把所有ETF「目前」的持股加總，看這些主動式ETF整體最集中壓在哪些個股，
    並估算每檔個股在這些ETF裡合計的部位規模（金額）。
    """
    rows = []
    for etf_id in etf_ids:
        dates = load_all_dates(conn, etf_id)
        if not dates:
            continue
        today_date = dates[0]
        today_df = load_snapshot(conn, etf_id, today_date)
        aum = load_latest_aum(conn, etf_id)
        for _, r in today_df.iterrows():
            amount_wan = None
            if aum is not None and pd.notna(r["weight_pct"]):
                amount_wan = r["weight_pct"] / 100 * aum * 100
            rows.append(
                {
                    "etf_id": etf_id,
                    "stock_code": r["stock_code"],
                    "stock_name": r["stock_name"],
                    "weight_pct": r["weight_pct"],
                    "shares": r["shares"],
                    "amount_wan": amount_wan,
                }
            )

    if not rows:
        return pd.DataFrame(
            columns=[
                "stock_code", "stock_name", "etf_count", "etfs",
                "total_shares", "total_amount_wan", "avg_weight_pct",
            ]
        )

    df = pd.DataFrame(rows)
    grouped = (
        df.groupby(["stock_code", "stock_name"])
        .agg(
            etf_count=("etf_id", "nunique"),
            etfs=("etf_id", lambda s: "、".join(sorted(set(s)))),
            total_shares=("shares", lambda s: s.sum(skipna=True) if s.notna().any() else None),
            total_amount_wan=("amount_wan", lambda s: s.sum(skipna=True) if s.notna().any() else None),
            avg_weight_pct=("weight_pct", "mean"),
        )
        .reset_index()
    )
    grouped = grouped.sort_values("total_amount_wan", ascending=False, na_position="last")
    return grouped


def format_aggregate_report(agg_df: pd.DataFrame) -> str:
    if agg_df.empty:
        return "## 全部ETF個股加總持股\n\n- 目前沒有資料\n"

    lines = ["## 全部ETF個股加總持股（依合計估計金額排序）\n"]
    lines.append("| 股票 | 被幾檔ETF持有 | 持有的ETF | 合計持有張數 | 合計估計金額（萬元） | 平均權重 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for _, r in agg_df.iterrows():
        lots_txt = f"{r['total_shares']/1000:,.1f}" if pd.notna(r["total_shares"]) else "N/A"
        amt_txt = f"{r['total_amount_wan']:,.0f}" if pd.notna(r["total_amount_wan"]) else "N/A"
        lines.append(
            f"| {r['stock_name']}({r['stock_code']}) | {r['etf_count']} | {r['etfs']} | "
            f"{lots_txt} | {amt_txt} | {r['avg_weight_pct']:.2f}% |"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--etf", help="只看單一ETF代碼，例如 00981A")
    parser.add_argument("--md", help="同時輸出成markdown檔案的路徑")
    parser.add_argument(
        "--min-etfs", type=int, default=2, help="同步加碼觀察表的門檻ETF檔數（預設2檔）"
    )
    parser.add_argument(
        "--aggregate", action="store_true", help="只印出全部ETF個股加總持股總表，不印換股報告"
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.etf:
        etf_codes = [args.etf.upper()]
    else:
        etf_codes = [
            r[0] for r in conn.execute("SELECT DISTINCT etf_id FROM holdings ORDER BY etf_id").fetchall()
        ]

    if args.aggregate:
        agg_df = compute_aggregate_holdings(conn, etf_codes)
        report = format_aggregate_report(agg_df)
        print(report)
        if args.md:
            Path(args.md).write_text(report, encoding="utf-8")
            print(f"\n已輸出至 {args.md}")
        conn.close()
        return

    overlap_df = compute_overlap(conn, etf_codes, min_etfs=args.min_etfs)
    all_reports = [format_overlap_report(overlap_df)]
    for etf_id in etf_codes:
        dates = load_all_dates(conn, etf_id)
        if len(dates) < 2:
            all_reports.append(f"## {etf_id}\n- 目前只有 {len(dates)} 天資料，尚無法比對，請明天再跑一次\n")
            continue

        today_date, prev_date = dates[0], dates[1]
        today_df = load_snapshot(conn, etf_id, today_date)
        prev_df = load_snapshot(conn, etf_id, prev_date)
        aum = load_latest_aum(conn, etf_id)

        new_in_df, dropped_df, changes_df = diff_two_snapshots(today_df, prev_df, aum_million_twd=aum)
        report = format_report(etf_id, today_date, prev_date, new_in_df, dropped_df, changes_df, has_aum=aum is not None)
        all_reports.append(report)

    full_report = "\n---\n\n".join(all_reports)
    print(full_report)

    if args.md:
        Path(args.md).write_text(full_report, encoding="utf-8")
        print(f"\n已輸出至 {args.md}")

    conn.close()


if __name__ == "__main__":
    main()
