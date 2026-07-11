# 台灣主動式ETF 每日換股追蹤工具

自動每天抓取MoneyDJ的ETF「全部持股」頁面，存成歷史紀錄，並比對出每天的加碼／減碼／新增／剔除。

## 檔案說明

| 檔案 | 用途 |
| --- | --- |
| `etf_list.txt` | 要追蹤的ETF代碼清單，一行一個代碼 |
| `scraper.py` | 抓取當天持股，存入 `data/etf_holdings.db`（SQLite） |
| `diff_report.py` | 比對最新兩天資料，輸出換股報告 |
| `.github/workflows/daily_scrape.yml` | GitHub Actions排程設定，自動每天雲端執行 |

## 第一步：本機測試

```bash
pip install -r requirements.txt
python scraper.py          # 抓取一次目前資料
python diff_report.py      # 這時只有1天資料，會顯示「尚無法比對」
```

隔天（或手動改資料庫測試）再跑一次 `scraper.py`，接著跑：

```bash
python diff_report.py
```

就能看到換股報告了。範例輸出：

```
## 00981A　換股報告（2026-05-13 → 2026-05-14）

**新增持股：**
- 旺矽(6223)：5.08%

**剔除持股：**
- 京元電子(2449)：原持有 1.50%

**比例顯著調整（變動 ≥ 0.5 個百分點）：**
| 股票 | 前次比例 | 最新比例 | 變動 |
| --- | --- | --- | --- |
| 台積電(2330) | 9.23% | 9.86% | +0.63 🔺加碼 |
```

## 第二步：補齊ETF清單

`etf_list.txt` 目前只放了幾檔範例。請自行到以下來源之一補齊完整清單（代碼末碼為A即為股票型主動式ETF）：
- https://www.etfinfo.tw/active
- 各投信官網公告
- TWSE / 各發行公司公開說明書

清單會隨時間增加新掛牌ETF，建議每隔一陣子回來更新這個檔案。

## 第三步：設定每天自動執行

### 方法A：GitHub Actions（推薦，免費、不需要自己的電腦開機）

1. 把這個資料夾推上你的GitHub repo（可以是私有repo）
2. GitHub會依 `.github/workflows/daily_scrape.yml` 的設定，在**週一到週五台灣時間下午2:30**自動執行，抓取資料並把結果commit回repo
3. 想手動測試，可以到repo的Actions頁籤，選這個workflow按「Run workflow」
4. 每天的資料庫與報告會累積在repo裡，你隨時可以拉下來看

### 方法B：Windows工作排程器（本機執行）

1. 開始功能表搜尋「工作排程器」
2. 建立基本工作 → 觸發程序選「每天」→ 動作選「啟動程式」
3. 程式/指令碼填 `python`，引數填 `scraper.py`，起始位置填這個資料夾的完整路徑
4. 建議另外再排一個工作執行 `python diff_report.py --md report_latest.md`，時間排在scraper之後幾分鐘

## 第四步：架設網站（GitHub Pages，免費）

`generate_site.py` 會把資料庫轉成 `docs/index.html`，只要開啟GitHub Pages，就能有一個公開網址可以打開看，每天GitHub Actions跑完會自動更新這個網頁。

1. 先照上面「GitHub Actions」的步驟，把整個資料夾推上你的GitHub repo（記得設Private避免資料被公開看到，不過Private repo也能開Pages，只是網址需要登入或設定才能看，如果想要任何人都能打開，repo就要設Public）
2. 進到repo的 **Settings > Pages**
3. **Source** 選「Deploy from a branch」，**Branch** 選 `main`，資料夾選 `/docs`，按Save
4. 等GitHub Actions第一次成功執行完（會產生 `docs/index.html`），過幾分鐘Settings > Pages頁面上方就會顯示你的網址，格式類似：
   ```
   https://你的帳號.github.io/repo名稱/
   ```
5. 之後每天Actions自動跑完，這個網址內容就會自動更新，點開每檔ETF可以看目前完整持股清單，以及跟前一天比對的新增／剔除／比例增減

### 本機也可以先看看網站長怎樣

不想等GitHub Pages，也可以先在本機產生看看：
```cmd
python generate_site.py
```
跑完後打開資料夾裡的 `docs\index.html`（直接用瀏覽器打開這個檔案）就能預覽畫面。

## 新功能：ETF規模＋同步加碼觀察表

- `scraper.py` 現在會額外抓取每檔ETF的「ETF規模」（存進 `fund_info` 資料表），網站上每檔ETF標題旁會顯示規模（單位：億台幣）
  - 注意：規模資料**不是每天更新**，MoneyDJ通常是每月更新一次（標題會附上資料日期），這是網站本身的限制，不是程式的問題
  - 若看到終端機顯示「ETF規模尚未公布」（INFO層級，非警告），代表該ETF剛上市，網站上該欄位本身就是「N/A」，不是程式或網站結構出錯；只有顯示「找不到ETF規模欄位」（WARNING層級）才代表網站結構可能真的變動了，需要我協助調整程式
- 持股清單與換股報告現在都會附上「持有股數」，不只看比例
- **加碼力道**：換股報告與同步加碼觀察表現在會額外算「股數變動百分比」，這個指標比「比例變動」更能反映真實買進力道——因為ETF淨值與比例會受股價漲跌影響，就算完全沒加碼，股價漲比例也會自然上升；股數是否真的增加，才是判斷經理人是否真的在買進的依據
- **張數／金額欄位**：換股報告與同步加碼觀察表現在也會顯示「張數變動」（股數/1000）跟「估計金額變動」（單位：萬元）
  - 金額是用「比例變動 × 最新已知ETF規模」反推估算出來的，**不是網站直接公布的精確持股金額**（MoneyDJ的持股明細頁面本身沒有金額欄位），加上ETF規模通常是每月才更新一次，所以金額數字僅供參考，抓大概量級用，不是精確數字
  - 如果該ETF還沒抓到規模資料（例如剛上市），金額欄位會顯示N/A，股數／張數欄位不受影響仍會正常顯示
- **全部ETF個股加總持股**：網站最上方（同步加碼觀察表下面）現在會有一張總表，把所有主動式ETF「目前」的持股全部加總，依「合計估計金額」排序，讓你一眼看出這些主動式ETF整體最集中壓在哪些個股、有幾檔ETF同時持有、合計張數與估計金額
  - 只想在終端機看這張總表，不要換股報告，可以用：
    ```cmd
    python diff_report.py --aggregate
    python diff_report.py --aggregate --md aggregate.md
    ```
- 如果只想看某檔ETF的換股報告，加上 `--etf` 參數：
  ```cmd
  python diff_report.py --etf 00981A
  ```
- 想調整同步加碼的門檻（預設2檔以上），用 `--min-etfs`：
  ```cmd
  python diff_report.py --min-etfs 3
  ```

## 重要提醒

- 資料來源是MoneyDJ網站的公開頁面，僅供**個人研究參考**使用，請勿大量重複抓取或商業散布，程式中已內建抓取間隔（`REQUEST_DELAY_SEC`）避免造成網站負擔
- ETF持股比例是**估算的市值權重**，不代表基金實際成交價格與交易時間，正式數字仍以投信公告的公開說明書/月報為準
- 若MoneyDJ調整網頁結構，`scraper.py` 的表格解析可能會失效，屆時需要更新 `parse_holdings_page()` 函式
