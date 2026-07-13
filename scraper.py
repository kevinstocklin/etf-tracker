#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper.py
每日抓取台灣主動式ETF「全部持股」頁面（MoneyDJ Basic0007B），
存入本機SQLite資料庫，累積歷史紀錄供後續比對換股狀況使用。

用法：
    python scraper.py

會讀取 etf_list.txt 中的代碼清單，逐一抓取，寫入 data/etf_holdings.db
"""

import re
import time
import sqlite3
import logging
import html as html_module
from pathlib import Path
from datetime import date

import requests
import pandas as pd
from io import StringIO

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "etf_holdings.db"
ETF_LIST_PATH = BASE_DIR / "etf_list.txt"

URL_TEMPLATE = "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={etf_id}.TW"
FUND_INFO_URL_TEMPLATE = "https://www.moneydj.com/ETF/X/Basic/Basic0004.xdjhtm?etfid={etf_id}.TW"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.moneydj.com/",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}

# 用同一個session發送所有請求，行為更接近瀏覽器（正確處理cookie），
# 也比每次都開新連線更不容易被誤判成快取／機器人流量
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 抓取間隔秒數，避免對網站造成負擔（請勿調得過短）
REQUEST_DELAY_SEC = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def load_etf_list() -> list[str]:
    codes = []
    with open(ETF_LIST_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            codes.append(line.upper())
    return codes


def init_db(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS holdings (
            etf_id TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,   -- 網站標示的「資料日期」
            fetch_date TEXT NOT NULL,      -- 程式實際抓取的日期
            stock_code TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            weight_pct REAL,
            shares REAL,
            PRIMARY KEY (etf_id, snapshot_date, stock_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fund_info (
            etf_id TEXT NOT NULL,
            fetch_date TEXT NOT NULL,      -- 程式實際抓取的日期
            etf_name TEXT,
            aum_million_twd REAL,          -- ETF規模（百萬台幣）
            aum_date TEXT,                 -- 網站標示的規模資料日期（通常是月更新）
            constituent_count INTEGER,     -- 成分股數
            PRIMARY KEY (etf_id, fetch_date)
        )
        """
    )
    conn.commit()


def _html_to_text(html: str) -> str:
    """把HTML標籤全部拿掉，轉成一長串純文字，用來做穩健的關鍵字比對。
    比起依賴pandas解析表格欄位結構，這種方式不受表格colspan、巢狀表格、
    div排版等網頁結構差異影響，只要「ETF規模」這幾個字跟後面的數值在
    畫面上相鄰，就找得到。
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text


def parse_fund_info(html: str) -> dict:
    """從「基本資料」頁面解析出ETF名稱與ETF規模。
    直接在純文字內容中找「ETF規模」關鍵字後面接的數值，不依賴表格欄位結構。
    """
    info = {
        "etf_name": None,
        "aum_million_twd": None,
        "aum_date": None,
        "constituent_count": None,
        "aum_raw_text": None,
    }

    # 中文ETF名稱，從<title>取得較穩定，格式例如「主動統一台股增長-00981A.TW-ETF基本資料」
    m_title = re.search(r"<title>\s*([^<]+?)-\d{4,6}[A-Z]?\.TW", html)
    if m_title:
        info["etf_name"] = m_title.group(1).strip()

    text = _html_to_text(html)

    # ETF規模：238,945.40(百萬台幣)(2026/04/30)，容許標籤與數值間有空白
    m_aum = re.search(r"ETF規模\s*([\d,]+\.?\d*)\s*\(\s*百萬台幣\s*\)\s*\(\s*(\d{4}/\d{2}/\d{2})\s*\)", text)
    if m_aum:
        info["aum_million_twd"] = float(m_aum.group(1).replace(",", ""))
        info["aum_date"] = m_aum.group(2).replace("/", "-")
        info["aum_raw_text"] = f"{m_aum.group(1)}(百萬台幣)({m_aum.group(2)})"
    else:
        # 找不到完整數值格式，檢查是不是顯示N/A（通常代表剛上市、規模尚未公布）
        m_na = re.search(r"ETF規模\s*(N/?A|[-—])", text, re.I)
        if m_na:
            info["aum_raw_text"] = "N/A"
        elif "ETF規模" in text:
            # 有找到標籤但值的格式跟預期不同，記下實際內容方便除錯
            m_raw = re.search(r"ETF規模\s*([^\s]{1,40})", text)
            info["aum_raw_text"] = m_raw.group(1) if m_raw else "格式未知"

    m_count = re.search(r"成分股數\s*(\d+)", text)
    if m_count:
        info["constituent_count"] = int(m_count.group(1))

    if not info["etf_name"]:
        m_name = re.search(r"ETF名稱\s*([^\s]{1,20})", text)
        if m_name:
            info["etf_name"] = m_name.group(1)

    return info


def fetch_fund_info(etf_id: str) -> dict | None:
    url = FUND_INFO_URL_TEMPLATE.format(etf_id=etf_id) + f"&_ts={int(time.time())}"
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except requests.RequestException as e:
        log.error("抓取 %s 基本資料失敗：%s", etf_id, e)
        return None

    info = parse_fund_info(resp.text)
    if info["aum_million_twd"] is not None:
        log.info(
            "%s：ETF規模 %.1f 百萬台幣（%s）",
            etf_id, info["aum_million_twd"], info["aum_date"],
        )
    elif info["aum_raw_text"] is not None:
        # 有找到「ETF規模」欄位，但值是N/A或其他非數字格式，通常是剛上市尚未公布規模
        log.info("%s：ETF規模尚未公布（網站顯示：%s）", etf_id, info["aum_raw_text"])
    else:
        log.warning("%s：找不到ETF規模欄位，網站結構可能已變動", etf_id)
    return info


def save_fund_info(conn: sqlite3.Connection, etf_id: str, info: dict):
    fetch_date = date.today().isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO fund_info
            (etf_id, fetch_date, etf_name, aum_million_twd, aum_date, constituent_count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            etf_id,
            fetch_date,
            info.get("etf_name"),
            info.get("aum_million_twd"),
            info.get("aum_date"),
            info.get("constituent_count"),
        ),
    )
    conn.commit()


def fetch_one(etf_id: str) -> tuple[str, list[dict]] | None:
    url = URL_TEMPLATE.format(etf_id=etf_id) + f"&_ts={int(time.time())}"
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except requests.RequestException as e:
        log.error("抓取 %s 失敗：%s", etf_id, e)
        return None

    try:
        snapshot_date, records = parse_holdings_page(resp.text)
    except ValueError as e:
        log.error("解析 %s 失敗：%s", etf_id, e)
        return None

    log.info("%s：資料日期 %s，共 %d 檔持股", etf_id, snapshot_date, len(records))
    return snapshot_date, records


def parse_holdings_page(html: str):
    """從頁面HTML解析出資料日期與持股明細表格"""
    # 資料日期，例如「資料日期：2026/05/14」
    m = re.search(r"資料日期[：:]\s*(\d{4}/\d{2}/\d{2})", html)
    snapshot_date = m.group(1).replace("/", "-") if m else date.today().isoformat()

    tables = pd.read_html(StringIO(html))
    holdings_df = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        if any("個股名稱" in c for c in cols) and any("投資比例" in c for c in cols):
            holdings_df = t
            break

    if holdings_df is None:
        raise ValueError("找不到持股明細表格，網站結構可能已變動，請檢查程式")

    holdings_df = holdings_df.rename(
        columns=lambda c: {
            "個股名稱": "raw_name",
        }.get(str(c).split("(")[0].strip(), str(c))
    )

    records = []
    for _, row in holdings_df.iterrows():
        raw_name = str(row.iloc[0])
        # 格式類似「台積電(2330.TW)」，取出代碼與名稱
        code_match = re.search(r"\((\d{4,6})\.TW\)", raw_name)
        stock_code = code_match.group(1) if code_match else ""
        stock_name = re.sub(r"\(\d{4,6}\.TW\)", "", raw_name).strip()

        try:
            weight = float(row.iloc[1])
        except (ValueError, TypeError):
            weight = None
        try:
            shares = float(str(row.iloc[2]).replace(",", ""))
        except (ValueError, TypeError):
            shares = None

        if not stock_code:
            continue

        records.append(
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "weight_pct": weight,
                "shares": shares,
            }
        )

    return snapshot_date, records


def save_records(conn: sqlite3.Connection, etf_id: str, snapshot_date: str, records: list[dict]):
    fetch_date = date.today().isoformat()
    cur = conn.cursor()
    for r in records:
        cur.execute(
            """
            INSERT OR REPLACE INTO holdings
                (etf_id, snapshot_date, fetch_date, stock_code, stock_name, weight_pct, shares)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                etf_id,
                snapshot_date,
                fetch_date,
                r["stock_code"],
                r["stock_name"],
                r["weight_pct"],
                r["shares"],
            ),
        )
    conn.commit()


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # 先訪問首頁一次，讓session跟真實瀏覽器一樣拿到正常的cookie，
    # 避免網站對「沒有cookie的直接請求」回傳快取過的舊內容
    try:
        SESSION.get("https://www.moneydj.com/", timeout=15)
    except requests.RequestException as e:
        log.warning("預熱首頁請求失敗（不影響後續抓取）：%s", e)

    etf_codes = load_etf_list()
    log.info("共 %d 檔ETF待抓取", len(etf_codes))

    for i, etf_id in enumerate(etf_codes, 1):
        result = fetch_one(etf_id)
        if result:
            snapshot_date, records = result
            if records:
                save_records(conn, etf_id, snapshot_date, records)
        time.sleep(REQUEST_DELAY_SEC)

        info = fetch_fund_info(etf_id)
        if info:
            save_fund_info(conn, etf_id, info)
        if i < len(etf_codes):
            time.sleep(REQUEST_DELAY_SEC)

    conn.close()
    log.info("完成，資料庫位置：%s", DB_PATH)


if __name__ == "__main__":
    main()
