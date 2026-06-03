# tg-group-monitor

用個人 Telegram 帳號監聽指定群組，命中關鍵字的訊息即時記錄，並轉發到另一個 TG 對話與 / 或 Webhook（Discord、自家後台皆可）。

適用情境：你只是群組的**普通成員**，無法加 bot、也拿不到管理權限，但想即時擷取訊息。

> ⚠️ 這是以你個人帳號自動化登入（MTProto）。請遵守 Telegram 使用條款，僅用於你有權限存取的群組，避免大量爬取造成帳號被風控。

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
| `FORWARD_TO` | 轉發目標 TG：@username、數字 ID、或 `me`（自己的 Saved Messages） |
| `WEBHOOK_URL` | 接收 JSON 的 URL；自動偵測 Discord webhook 並改用其格式 |
| `LOG_TO_FILE` | `1`＝同時寫入 `LOG_FILE`（JSON Lines） |

## 長期掛在背景

簡單做法可用 `nohup`：

```bash
nohup python main.py > monitor.log 2>&1 &
```

之後若要做成開機自動啟動，可再加 `launchd`（macOS）設定。
