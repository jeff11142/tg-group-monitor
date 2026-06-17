# tg-group-monitor

用個人 Telegram 帳號監聽指定群組，命中關鍵字的訊息即時記錄，並轉發到另一個 TG 對話與 / 或 Webhook（Discord、自家後台皆可）。

適用情境：你只是群組的**普通成員**，無法加 bot、也拿不到管理權限，但想即時擷取訊息。

> ⚠️ 這是以你個人帳號自動化登入（MTProto）。請遵守 Telegram 使用條款，僅用於你有權限存取的群組，避免大量爬取造成帳號被風控。

## 功能概觀

1. **群組監聽轉發**：監看指定群組，命中關鍵字即記錄、用 Bot 廣播給訂閱者、POST 到 Webhook。
2. **交易訊號解析**：自動把訊號訊息解析成結構化資料（進場價、目標價 TP1~4、止損價 SL1~2），重新排版後推送。
3. **TP/SL 達標監聽通知**：用行情 WebSocket 自行偵測價格是否觸及訊號的 TP/SL，達標即通知訂閱者（回覆引用各自的進場訊息）。
4. **自動交易（選用）**：依訊號在幣安合約限價進場，掛多段止盈 + 雙軌止損、動態鎖利、進場/持倉超時保護，全程由 WebSocket 即時管理 + 慢速對帳兜底。
5. **Bot `/config` 即時調參**：管理員可在 Telegram 直接調整槓桿、持倉上限、止損策略等，免重啟。

> 只想做「監聽轉發」就照下面跑即可；自動交易是**選用**功能，預設關閉（`TRADING_ENABLED=0`）。

## 不熟 Linux / 程式的人：看完整圖文教學

從零開始（申請 TG API、建 Bot、本機跑通、VPS 部署、日常維運、常見問題排除）的逐步說明，選你的作業系統：

- **Mac 使用者** → [docs/MAC_GUIDE.md](docs/MAC_GUIDE.md)
- **Windows 使用者** → [docs/WINDOWS_GUIDE.md](docs/WINDOWS_GUIDE.md)

底下的章節是給已經熟悉終端機 / Python / VPS 的人用的快速版。

## 安裝

```bash
cd ~/Documents/個人專案/tg-group-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 設定

1. 到 https://my.telegram.org → **API development tools** 申請，取得 `API_ID` 與 `API_HASH`。
2. 複製設定範本並填寫：

```bash
cp config.example.env .env
```

3. 編輯 `.env`，至少填 `API_ID`、`API_HASH`、`PHONE`。

## 找出要監聽的群組 ID

不知道群組 ID 時，先把 `.env` 裡的 `LIST_DIALOGS` 設成 `1`，執行一次會列出你所有對話與其 id：

```bash
python main.py
```

把目標群組的 `id` 填到 `SOURCE_CHAT`，再把 `LIST_DIALOGS` 改回 `0`。

## 開始監聽

```bash
python main.py
```

- 第一次會要求輸入手機收到的**驗證碼**（若有兩步驗證還需輸入密碼），成功後產生 `.session` 檔，之後免重複登入。
- 命中關鍵字的訊息會：印在終端機、寫入 `messages.jsonl`、轉發到 `FORWARD_TO`、POST 到 `WEBHOOK_URL`（依你設定而定）。
- 結束按 `Ctrl+C`。

## 設定項說明

| 變數 | 說明 |
|------|------|
| `SOURCE_CHAT` | 要監聽的群組：@username、數字 ID（如 `-1001234567890`） |
| `KEYWORDS` | 逗號分隔，包含任一即命中；**留空＝全部訊息** |
| `BOT_TOKEN` | TG Bot token（@BotFather 申請）；命中訊息會用此 bot 廣播給接收者清單 |
| `BOT_TARGET` | 第一次跑時自動加入接收者清單的「初始接收者」chat_id；之後動態管理請對 bot 用指令 |
| `ADMIN_CHAT_ID` | 能對 bot 下管理指令的 chat_id（通常 = `BOT_TARGET`），其他人對 bot 講話只會被回覆 chat_id |
| `WEBHOOK_URL` | 接收 JSON 的 URL；自動偵測 Discord webhook 並改用其格式 |
| `LOG_TO_FILE` | `1`＝同時寫入 `LOG_FILE`（JSON Lines） |

接收者清單存在 `recipients.db`（SQLite），透過 bot 指令動態管理：

| 指令（只有 admin 能用） | 行為 |
|------|------|
| `/list` | 列出所有接收者與啟用狀態 |
| `/add <chat_id> [name]` | 新增接收者 |
| `/remove <chat_id>` | 移除接收者 |
| `/enable <chat_id>` / `/disable <chat_id>` | 暫停/恢復某接收者，不刪資料 |
| `/myid` | 回你自己的 chat_id |
| `/config` | 開啟交易參數面板（見下方「自動交易」），用按鈕即時調整 |
| `/help` | 顯示指令列表 |

非 admin 對 bot 講話只會收到「你的 chat_id 是 X」的提示，方便他們把 ID 給你開通。

## 訊號解析與 TP/SL 監聽通知

當群組訊息符合訊號格式時，會被解析成結構化資料並重新排版後推送給訂閱者：

- 解析欄位：**進場價、目標價 TP1~4、止損價 SL1~2**（單字元代號如 `4USDT` 也支援）。
- 推送的止損取決於模式：
  - **過濾重算（預設）**：止損換成自算的單一 SL1（`min(訊號SL1距離, SL1_PCT 上限)`）。
  - **原始訊號（`RAW_SIGNAL_MODE=1`）**：保留訊號原始 SL1/SL2。
- 推送後由**行情 WebSocket（主網標記價）自行偵測**該訊號是否觸及 TP/SL：
  - 價達某個 TP → 通知「🎯 TPn 達標」（每層只通知一次）。
  - 價觸 SL → 通知「🛑 SL 觸發」並停止監聽該訊號。
- 收到的每則原文都會落地（`messages.jsonl`），即使解析失敗也留底，方便日後補規則或稽核。

> 這套「監聽 + 通知」不需要金鑰、不論有無開自動交易都會運作。

## 自動交易（選用）

設 `TRADING_ENABLED=1` 並填好幣安 API 金鑰後，收到訊號會在你的幣安帳號實際下單：

- **合約**：限價進場 → 成交後掛多段止盈（reduceOnly）+ **雙軌半倉止損**（上軌 SL1、下軌 SL2）。
- **動態止損**：每段 TP 成交後，止損逐段往上爬（保本 → 鎖利）。
- **保險絲**：條件單失靈時，慢速對帳迴圈會主動補平 / 收單 / 重啟回復。
- **進場超時**（`ENTRY_TIMEOUT_MIN`）：限價進場單久未成交就撤單、釋放名額。
- **持倉超時**（`MAX_HOLD_HOURS`，預設 24h，`0`=不限）：ACTIVE 倉超時仍未觸發 TP/SL → 主動市價平倉、釋放名額，並**私訊管理員真實損益**（回查 Binance income：已實現損益＋手續費＋資金費）。

> ⚠️ **真錢風險**：自動交易會用你的資金實際下單，請先用測試網（`BINANCE_TESTNET=1`）驗證，並從小額、低槓桿開始。本工具不對交易結果負責。

### `/config` 即時調整（管理員）

在 Telegram 對 bot 送 `/config`，用按鈕即時調整（皆寫回 `.env`，新進場立即生效）：

- 數值：`leverage`、`min_amount_mult`、`max_open_trades`、`entry_timeout_min`、`max_hold_hours`
- 開關：二段止損（SL2）、動態止損（TP 後止損上移）、**原始訊號做單**、測試網／正式網切換

### 主要交易設定（`.env`）

完整清單見 `config.example.env`；重點：

| 變數 | 說明 |
|------|------|
| `TRADING_ENABLED` | `1`＝啟用自動交易（預設 `0`） |
| `BINANCE_TESTNET` | `1`＝測試網（建議先用）；`0`＝正式網（真錢） |
| `BINANCE_*_API_KEY` / `_SECRET` | 幣安 API 金鑰（測試網／正式網分開填） |
| `FUTURES` / `LEVERAGE` / `MARGIN_TYPE` | 合約模式、槓桿、保證金模式 |
| `MAX_OPEN_TRADES` | 最多同時持倉筆數（滿了就略過新訊號） |
| `SL1_PCT` / `SL2_MULT` | 止損上限%、SL2 倍數（`0`=單一止損守全倉） |
| `BREAKEVEN_AFTER_TP1` | `1`＝TP 後動態止損上移鎖利 |
| `RAW_SIGNAL_MODE` | `1`＝直接用訊號原始 SL1/SL2 掛單，不重算 |
| `ENTRY_TIMEOUT_MIN` | 進場限價單超時（分）未成交就撤單（`0`=不限） |
| `MAX_HOLD_HOURS` | 持倉超時（小時）自動平倉（`0`=不限） |

## 長期掛在背景

簡單做法可用 `nohup`：

```bash
nohup python main.py > monitor.log 2>&1 &
```

之後若要做成開機自動啟動，可再加 `launchd`（macOS）設定。

## 部署到 VPS（7×24 長期監聽）

VPS 是無頭環境，無法手動輸驗證碼。流程是：**本機先登入產生 `.session` → scp 上傳 → VPS 用 systemd 掛背景**。完整步驟見 [deploy/DEPLOY.md](deploy/DEPLOY.md)，systemd 服務範本見 [deploy/tg-group-monitor.service](deploy/tg-group-monitor.service)。
