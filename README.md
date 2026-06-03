# tg-group-monitor

用個人 Telegram 帳號監聽指定群組，命中關鍵字的訊息即時記錄，並轉發到另一個 TG 對話與 / 或 Webhook（Discord、自家後台皆可）。

適用情境：你只是群組的**普通成員**，無法加 bot、也拿不到管理權限，但想即時擷取訊息。

> ⚠️ 這是以你個人帳號自動化登入（MTProto）。請遵守 Telegram 使用條款，僅用於你有權限存取的群組，避免大量爬取造成帳號被風控。

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
| `/help` | 顯示指令列表 |

非 admin 對 bot 講話只會收到「你的 chat_id 是 X」的提示，方便他們把 ID 給你開通。

## 長期掛在背景

簡單做法可用 `nohup`：

```bash
nohup python main.py > monitor.log 2>&1 &
```

之後若要做成開機自動啟動，可再加 `launchd`（macOS）設定。

## 部署到 VPS（7×24 長期監聽）

VPS 是無頭環境，無法手動輸驗證碼。流程是：**本機先登入產生 `.session` → scp 上傳 → VPS 用 systemd 掛背景**。完整步驟見 [deploy/DEPLOY.md](deploy/DEPLOY.md)，systemd 服務範本見 [deploy/tg-group-monitor.service](deploy/tg-group-monitor.service)。
