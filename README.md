# policydesk

一個保險櫃台 agent：從方案、簽約、繳費、保額到理賠，全流程由同一個 agent 承接，
核保與理賠的准駁由人決定。

它報出的每一個金額都帶著出處。沒有出處的數字進不了畫面，因為型別擋著。

## 為什麼需要條款層

保單條款有幾種陷阱，只做關鍵字檢索的工具會踩進去。以下都出自 660 份真實合約，
不是舉例。

**等待期不一定叫等待期。** 660 份裡 119 份寫「等待期」字面，但 **51 份完全不用這個詞**，
把期間藏在「疾病」的定義裡：

> 本險「疾病」之定義：係指被保險人自本附約生效日起持續有效**三十日以後**或復效日起
> 所發生的疾病。

檢索「等待期」的工具找不到它，於是放行一筆生效第 20 天發病的申請。

**除外責任會在同一句話裡回復承保。** 全語料有 124 條這種條款：

> 一、美容手術、外科整型。**但為重建其基本功能所作之必要整型，不在此限**。

只撈到前半句就拒賠，只撈到後半句就照賠。判斷條件是「是否為重建基本功能」，那要看病歷，
所以它必須落到人工。

**同一家公司有兩套條號體系。** 舊核准的寫中文數字（第十七條），113 年起修正的印阿拉伯
數字（第 19 條）。只認一種會整批漏掉現售商品。

**金額不在條文裡，在附表。** 條文只寫「手術給付倍數 ╳ 住院醫療保險金日額」，倍數印在
附表 1 的表格中。抽文字層會把表格壓成一串失去欄位邊界的文字，所以附表要從 PDF 的版面
幾何重建。

## 設計

**解析是確定性的。** 條款索引切條號、判斷種類、把回復承保從除外責任裡拆出來、把偽裝成
定義的等待期還原成獨立條款。附表由 pdfplumber 依版面座標重建。這兩段都不呼叫模型。

**驗證是 prompt 做的，但判定要能被機器核對。** 驗證器回傳結構化判定，載明它引用了哪些
條號、引述了哪些欄位原文。之後由確定性層逐一比對：條號不存在、或原文在文件中查無，
整個判定作廢並轉人工。模型決定，資料庫裁決模型是不是在讀同一份文件。

**模型不碰數字。** 金額由計算機工具求值，運算子與函式白名單、`Decimal` 全程。模型寫算式，
工具算結果。

**模型不寫條款。** 模型工具只接受 `product_id` 與 `clause_id`，逐字內容由證據層從資料庫
重建。回覆中出現的條號會被抽出來逐一驗證，查無者整段標註未經確認。

**判不了的要說判不了。** `Standing.NEEDS_HUMAN` 用於證據不足、條款衝突無法解析，或判準
屬醫療而非契約。這類項目一律排除在總額之外。

**agent 不說保險公司才能說的話。** 判定是「擬申請／契約疑義／待人工」，不是「可給付／
拒賠」。核定權在核保理賠人員。

**拒絕是一個 join。** 資料庫裡沒有 `qualifies` 欄位。86 歲買不到健康險，是因為
`catalog_entry.issue_age_max` 是 75；拒保職業買不到，是因為 `member.occupation_class`
超過 `max_occupation`。核保人員讀得到系統用的同一批欄位。

## 目錄

| 路徑 | 職責 |
|---|---|
| `core/` | 領域模型、連線池、案件命令。`Money`、`ClaimItem`、`Stage` 的不變式在這裡 |
| `clauses/` | 條款索引。唯一能產生 `Citation` 的地方 |
| `ingest/` | 抓取合約、建立語料、重建附表 |
| `agent/` | 情境定義、確定性工具、回合執行器 |
| `llm/` | 模型接縫。Responses API 與腳本兩種實作 |
| `validation/` | prompt 驗證與判定核對 |
| `skills/` | 計算機。模型寫算式，這裡求值 |
| `gov/` | 政府服務 mock，照真實規則與失敗模式實作 |
| `synthetic/` | 合成保戶與保單，含由欄位導出的失格情況 |
| `web/` | 雙欄介面、兩個 socket、稽核與追蹤端點 |

## 跑起來

```bash
podman run -d --name policydesk-pg \
  -e POSTGRES_DB=policydesk -e POSTGRES_USER=policydesk -e POSTGRES_PASSWORD=policydesk \
  -p 5434:5432 docker.io/library/postgres:18-alpine
psql "postgres://policydesk:policydesk@127.0.0.1:5434/policydesk" -f infra/migrations/20260829000000_initial.sql

uv sync --extra dev
./scripts/fetch_fixtures.sh          # 條款 PDF 屬保險公司版權，不進 git
uv run pytest
uv run policydesk                    # http://localhost:8100
```

語料重建（1.19 GB，遵守 robots.txt）：

```bash
uv run python -c "import asyncio; from pathlib import Path; from policydesk.ingest.cathay import fetch_all; asyncio.run(fetch_all(Path('data/cathay')))"
```

既有語料新增來源分類時，先套用 `infra/migrations/20260905120000_document_kind.sql`，
再執行 `uv run python -m policydesk.ingest --source-kinds-only`，從本機 PDF 核對分類，
不改寫既有條款或保單。完整的 `policydesk-ingest` 重建也會寫入分類。
契約查詢與推薦只使用契約來源；商品說明書及未確認來源保留在原始資料中。
分類變更後須重建兩個檢索索引並重新啟動服務，才能更新記憶體中的索引。

升級目錄來源欄位時，另套用 `infra/migrations/20260905130000_catalog_origin.sql`。
目錄建置會將產生的費率、計價基數與資格標記為 `synthetic_demo`；這些不是正式費率表，
也不能用來判定契約的保險金額屬於日額或投保計畫。未標記來源的資料保留為 `unknown`，
缺少數值計價基數時不產生保費試算。更新後須重新啟動服務。

`.env` 放 `OPENAI_API_KEY` 才會有對話；沒有時 agent 會說接不到模型，不會編答案。
