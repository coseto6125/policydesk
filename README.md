# policydesk

一個保險櫃台 agent：從方案、簽約、繳費、保額到理賠，全流程由同一個 agent 承接，
核保與理賠的准駁由人決定。

它報出的每一個金額都帶著出處。沒有出處的數字進不了畫面，因為型別擋著。

> 2026/09 黑客松的 demo 專案，賽事結束因此不再維護。

---

## 跑起來

### 1. 資料庫

```bash
podman run -d --name policydesk-pg \
  -e POSTGRES_DB=policydesk -e POSTGRES_USER=policydesk -e POSTGRES_PASSWORD=policydesk \
  -p 5434:5432 docker.io/library/postgres:18-alpine

psql "postgres://policydesk:policydesk@127.0.0.1:5434/policydesk" \
  -f infra/migrations/20260829000000_initial.sql
```

5434 是為了避開本機其他 Postgres。用 `docker compose up postgres` 也可以。

### 2. 依賴與語料

```bash
uv sync --extra dev
./scripts/fetch_fixtures.sh     # 條款 PDF 屬保險公司版權，不進 git
uv run pytest
```

### 3. 兩個外部服務

對話與檢索各需要一組憑證。兩個都不設也跑得起來，但會安靜地少一半能力——
**服務照常啟動，只是答得比較差，而且不會報錯**，所以值得先設好。

| 變數 | 給誰用 | 不設會怎樣 |
|---|---|---|
| `ANTHROPIC_OAUTH_CREDS_PATH` | 回話的模型 | 退到 `OPENAI_API_KEY`，再退到本機登入的 codex CLI |
| `CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_AUTH_TOKEN` | 語意檢索的 embedding | 向量通道不開，只剩 BM25 關鍵字檢索 |

**模型**預設讀 Claude Code 的訂閱憑證（`~/.claude/.credentials.json`）。
`ANTHROPIC_OAUTH_CREDS_PATH` 指向另一份同步過去的複本，是給沒有登入 Claude Code
的部署主機用的。這個路徑唯讀：refresh token 單次輪替，在這裡更新會讓本機的
Claude Code 登入失效。

選型順序是 `POLICYDESK_PROVIDER` → Anthropic 憑證檔 → `OPENAI_API_KEY` → codex CLI，
四種都接不到就說接不到模型，不會編答案。

**embedding** 走 Cloudflare Workers AI 的 `@cf/baai/bge-m3`，1024 維。
兩個變數都吃逗號分隔的多組帳號，其中一組的每日免費額度用完會自動輪到下一組。

### 4. 建索引

```bash
uv run policydesk-index      # BM25 兩分鐘，向量六分鐘
uv run policydesk            # http://localhost:8100
```

向量索引記錄了是哪個 encoder 建的。用 A 建、用 B 查會安靜地變差，所以
manifest 對不上時它會直接報錯而不是照跑。換 encoder 就要重建整份索引。

### 5. 部署

`docker-compose.oci.yml` 是給雲端主機的覆蓋檔：不掛 ONNX 模型（那是 1.1 GB，
而且 ARM 上跑本地 encoder 只有 121 chunks/s），embedding 與 LLM 都走託管 API。

```bash
docker compose -f docker-compose.yml -f docker-compose.oci.yml up -d
```

主機需要 `.env` 帶上面那三個變數，加一份同步過去的 token 檔。向量索引直接複製過去，
不在主機上重建——那只是拿 Workers AI 的額度重算已經有的位元組。

臨時對外網址用 quick tunnel，不需要網域：

```bash
cloudflared tunnel --url http://127.0.0.1:8100
```

---

## 兩頁怎麼用

右邊是客戶對話，左邊是後台。同一份案件快照餵兩邊，所以兩頁不會各說各話。

首次進入輸入一個顯示名稱就會生出一位合成保戶與他的保單。想看完整流程的話，
從「我有哪些保單」開始，接著問理賠要準備什麼，再走到文件與簽署那段。

後台看得到該保戶的個資、案件階段、文件狀態與每一次模型呼叫的全文。

---

## 為什麼需要條款層

保單條款有幾種陷阱，只做關鍵字檢索的工具會踩進去。以下都出自 660 份真實合約，
不是舉例。

**等待期不一定叫等待期。** 660 份裡 119 份寫「等待期」字面，但 **51 份完全不用這個詞**，
把期間藏在「疾病」的定義裡：

> 本險「疾病」之定義：係指被保險人自本附約生效日起持續有效**三十日以後**或復效日起
> 所發生的疾病。

檢索「等待期」的工具找不到它，於是放行一筆生效第 20 天發病的申請。

**金額常常不在條文裡。** 手術倍數印在最後的附表，條文只寫「依附表所列倍數給付」。

**一句話前半說不賠，後半又說某些情況要賠。** 除外責任接著除外的除外，
兩段分開讀都會讀錯。

---

## 核心理念：Business Harness

現在多數 agent 框架把工具全部掛上去，再用 prompt 交代規矩；碰到危險動作跳出來問人。
自己用沒問題，因為你就坐在旁邊。對外營業的服務不行：沒有人守在每一通對話旁邊，
而 prompt 交代的規矩，模型多數時候會聽，「多數時候」不是保證。

**這裡的答案是：不給它那把工具，就不用煩惱那把工具的權限。權限不寫在 prompt 裡，
做進框架裡。**

設計理由、每一層與一般做法的對照、流程圖，以及還沒補完的缺口：
**[Business Agent Harness](https://claude.ai/code/artifact/3349899b-8600-4647-aaac-aadeb76700d9)**

### 三種收斂

這套 harness 的可靠性不來自任何單一功能，來自三個方向同時收斂。

**決策權收斂——先問這一輪要不要讓模型決定。** 情境上有一個欄位說明答案從哪來。
標成範本的直接把查到的資料列填進固定句型，不叫模型寫回覆；標成模型的才進到寫作那一輪。
這不是「限制模型能用什麼工具」，是連「模型可能做出非預期行為」這個風險本身都消掉，
因為那一輪根本沒有模型在做決策。計算機同理，是情境上的開關而不是全域工具，預設關。

**邊界收斂——判斷寫在框架裡，設定改不動。** 權限標記黏在碰資料的那個函式上，
情境的權限由它宣告的工具推導出來，不用另外維護白名單。把一個已標記的工具加進情境，
權限跟著它走。檢查用 `vars(fn)` 而不是 `getattr`——後者讀不到會回預設值，
於是「有人決定它是公開的」跟「沒人想過這件事」長得一模一樣。

空值一律解讀成「沒有額外授權」，不是「不受限制」。名字在兩處都查不到的工具，
當成需要身分核對，所以會犯的錯是多問一次身分證字號，不是少問。

**攻擊面收斂——不該存在的能力就不要有。** 工具集合裡沒有任意程式碼執行這一類。
客服場景的正當需求是呼叫受控的業務查詢，輸入來源卻是不受信任的匿名訪客；
帶著程式碼執行工具的 agent，本質上是一個等著被正確 prompt 觸發的攻擊面。
金額由計算機求值，運算子與函式走 AST 白名單、`Decimal` 全程，白名單沒列的名稱直接拋錯，
所以一個算式要嘛是純算術要嘛是錯誤，不會有副作用。

### 四層各擋什麼

| 層 | 擋什麼 | 怎麼擋 |
|---|---|---|
| 選情境 | 手上有哪些工具 | 路由只挑情境不回答，攤出去的清單先照流程階段過濾，派工也只認同一份清單 |
| 權限檢查 | 能不能讀這個人 | 標記在函式上，情境權限由工具推導；被擋下的工具連查詢都不會被組出來 |
| 回答格式 | 只能引用這一輪撈到的依據 | 條號清單當場組成 `enum`，不在清單裡的在解碼那一步就打不出來 |
| 送出前覆核 | 講了不該講的話 | 引用回資料庫查、原文逐字比對、承諾偵測；任一不過就整段扣住轉專人 |

第三層值得多說一句：約束從 prompt 移到了型別。不是叫模型「不要編造」，
是讓它打不出來。一條依據都沒撈到時，清單鎖成空。

### 為什麼是這個技術棧

選型都繞著同一件事：這套收斂設計的運行成本要壓得夠低，對外服務才付得起。

| 選擇 | 取代了什麼 | 換到什麼 |
|---|---|---|
| Sanic 原生伺服器 | ASGI 協議轉譯層 | 簡單 JSON 負載下的吞吐量，代價是放棄 ASGI 中介軟體生態 |
| msgspec | Pydantic 與標準庫 json | 更快的序列化與驗證，`Struct` 直接支援 free-threading |
| psqlpy | asyncpg | Rust 實作的原生驅動，實測吞吐較高；生態新，非直覺行為封裝在共用基礎類別裡 |
| Python 3.14 | 3.12／3.13 | 版本本身的效能紅利，承擔追新的相容性風險 |

檢索也是同一個邏輯：embedding 走 Cloudflare Workers AI 而不是本機 ONNX，
因為雲端 ARM 核心跑本地 encoder 只有 121 chunks/s，而托管端跑在專用硬體上。

### 幾條落在程式碼上的規則

**模型不碰數字。** 金額由計算機工具求值，運算子與函式白名單、`Decimal` 全程。
模型寫算式，工具算結果，而且計算機是情境上的開關、預設關。

**模型不寫條款。** 模型工具只接受 `product_id` 與 `clause_id`，逐字內容由證據層從
資料庫重建。回覆中出現的條號會被抽出來逐一驗證。

**判不了的要說判不了。** `Standing.NEEDS_HUMAN` 用於證據不足、條款衝突無法解析，
或判準屬醫療而非契約。這類項目一律排除在總額之外。

**agent 不說保險公司才能說的話。** 判定是「擬申請／契約疑義／待人工」，
不是「可給付／拒賠」。核定權在核保理賠人員。

**拒絕是一個 join。** 資料庫裡沒有 `qualifies` 欄位。86 歲買不到健康險，是因為
`catalog_entry.issue_age_max` 是 75；拒保職業買不到，是因為 `member.occupation_class`
超過 `max_occupation`。核保人員讀得到系統用的同一批欄位。

---

## 目錄

| 路徑 | 職責 |
|---|---|
| `core/` | 領域模型、連線池、案件命令。`Money`、`ClaimItem`、`Stage` 的不變式在這裡 |
| `clauses/` | 條款索引。唯一能產生 `Citation` 的地方 |
| `ingest/` | 抓取合約、建立語料、重建附表 |
| `agent/` | 情境定義、確定性工具、回合執行器 |
| `retrieval/` | BM25、向量與重排序三個檢索通道 |
| `llm/` | 模型接縫。Anthropic、OpenAI、codex CLI 與腳本四種實作 |
| `validation/` | prompt 驗證與判定核對 |
| `skills/` | 計算機。模型寫算式，這裡求值 |
| `gov/` | 政府服務 mock，照真實規則與失敗模式實作 |
| `synthetic/` | 合成保戶與保單，含由欄位導出的失格情況 |
| `web/` | 雙欄介面、兩個 socket、稽核與追蹤端點 |

---

## 語料維護

重建語料（1.19 GB，遵守 robots.txt）：

```bash
uv run python -c "import asyncio; from pathlib import Path; from policydesk.ingest.cathay import fetch_all; asyncio.run(fetch_all(Path('data/cathay')))"
```

既有語料新增來源分類時，先套用 `infra/migrations/20260905120000_document_kind.sql`，
再執行 `uv run python -m policydesk.ingest --source-kinds-only`，從本機 PDF 核對分類，
不改寫既有條款或保單。契約查詢與推薦只使用契約來源；商品說明書及未確認來源保留在
原始資料中。分類變更後須重建兩個檢索索引並重新啟動服務。

升級目錄來源欄位時，另套用 `infra/migrations/20260905130000_catalog_origin.sql`。
目錄建置會將產生的費率、計價基數與資格標記為 `synthetic_demo`；這些不是正式費率表，
也不能用來判定契約的保險金額屬於日額或投保計畫。未標記來源的資料保留為 `unknown`，
缺少數值計價基數時不產生保費試算。

---

## 授權與版權

程式碼採 MIT 授權，見 [LICENSE](LICENSE)。

保險商品與條款素材、法規條文、合成資料的來源與使用條件寫在 [NOTICE.md](NOTICE.md)。
這些內容不隨 MIT 授權轉授，再散布前請先讀它。
