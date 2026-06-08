# VPS 部署指南（Linux / systemd）

目標：讓監聽程式 7×24 掛在 VPS，斷線自動重連、開機自動啟動。

## 步驟 0：本機先完成一次登入（重要）

VPS 是無頭環境，沒辦法手動輸入手機驗證碼。所以先在你**本機**登入產生 session 檔：

```bash
cd ~/Documents/個人專案/tg-group-monitor
source .venv/bin/activate
python main.py          # 輸入手機驗證碼（與兩步驗證密碼）完成登入
# 看到「已登入」就可以 Ctrl+C 了，此時已產生 tg_monitor.session
```

> 完成後請**停掉本機這支**，不要和 VPS 同時用同一個 session。

## 步驟 1：把程式與憑證傳到 VPS

程式碼用 git clone，機密檔（`.env`、`.session`）因為被 .gitignore 擋住、不在 repo 裡，要另外 scp 補上。

```bash
# 在 VPS 上：clone 程式碼（不含機密檔）
git clone https://github.com/jeff11142/tg-group-monitor.git /opt/tg-group-monitor

# 在本機上：把 git 帶不過去的兩個機密檔單獨補傳
cd ~/Documents/個人專案/tg-group-monitor
scp .env tg_monitor.session  user@你的VPS_IP:/opt/tg-group-monitor/
```

> 注意：`.env`（API 憑證）和 `*.session`（登入態）絕不進 git，所以 clone 完一定要記得補 scp，否則程式會因找不到 `.env` 而無法啟動。
>
> 之後 VPS 要更新程式碼，直接 `cd /opt/tg-group-monitor && git pull` 即可，機密檔不受影響。

## 步驟 2：VPS 上安裝環境

```bash
ssh user@你的VPS_IP

sudo mv /tmp/tg-group-monitor /opt/tg-group-monitor
sudo chown -R $USER:$USER /opt/tg-group-monitor
cd /opt/tg-group-monitor

# 收緊權限：憑證只有自己能讀
chmod 600 .env *.session

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 先手動跑一下確認能直接登入、不再要驗證碼
.venv/bin/python main.py     # 應直接顯示「已登入」、「開始監聽」，確認後 Ctrl+C
```

## 步驟 3：註冊成 systemd 服務（開機自動啟動 + 自動重啟）

```bash
# 編輯 service 檔，把 User 和路徑改成你 VPS 上的實際值
sudo cp /opt/tg-group-monitor/deploy/tg-group-monitor.service /etc/systemd/system/
sudo nano /etc/systemd/system/tg-group-monitor.service   # 確認 User= 與路徑正確

sudo systemctl daemon-reload
sudo systemctl enable --now tg-group-monitor

# 看狀態與即時日誌
systemctl status tg-group-monitor
journalctl -u tg-group-monitor -f
```

## 常用維運指令

```bash
sudo systemctl restart tg-group-monitor   # 改設定後重啟
sudo systemctl stop tg-group-monitor      # 停止
journalctl -u tg-group-monitor -n 100     # 看最近 100 行日誌
```

## 更新程式碼：永遠是「兩部分」

⚠️ **`.env` 被 .gitignore 擋住，`git pull` 不會帶設定**。所以每次更新都要分開做：

```bash
cd /opt/tg-group-monitor
git pull                                   # ① 程式碼
nano .env                                  # ② 設定（git 帶不過去，手改）
sudo systemctl restart tg-group-monitor    # ③ 重啟生效
journalctl -u tg-group-monitor -n 30       # 看啟動 log 確認設定有吃到
```

只做 ① 不做 ②，新功能常常不會生效（程式讀到的是 VPS 上的舊 `.env`）。

## 改了 .env 設定怎麼辦

直接在 VPS 編輯 `/opt/tg-group-monitor/.env`，然後 `sudo systemctl restart tg-group-monitor` 即可。

### 交易相關設定 key（合約自動下單）

改交易行為時，要同步 VPS `.env` 的這幾個 key：

| key | 說明 | 目前值 |
|---|---|---|
| `BINANCE_TESTNET` | 1=測試網假錢、0=正式網真錢（換 0 要同時換正式網金鑰）| — |
| `LEVERAGE` | 槓桿倍數 | 5 |
| `MARGIN_TYPE` | `ISOLATED` 逐倉（風險封頂用，勿改 CROSS）| ISOLATED |
| `MAX_OPEN_TRADES` | 最多同時持倉筆數 | 30 |
| `MARGIN_USDT` | **固定本金/筆**；名目 = 本金 × 槓桿 | 30 |
| `AUTO_MIN_AMOUNT` | 必須 `0`；設 1 會走舊的保證金放大邏輯 | 0 |
| `SL1_PCT` / `SL2_MULT` | SL1=min(訊號SL1, 上限%)；SL2=SL1×倍數（**0=不掛SL2，單一止損守全倉**）| 5 / 0 |
| `TP_RATIOS` | 分段止盈比例（自適應，倉位小會自動降級）| 30,30,20,20 |
| `TRADE_MONITOR_INTERVAL` | 保險絲對帳間隔（秒）；即時反應已交給 WS，這只當兜底 | 120（程式預設，未列於 .env 也生效）|

> 提醒：已開的舊倉位 SL/TP 已掛在交易所上，改設定只影響「重啟後進來的新訊號」。

## 自動交易運作架構（WebSocket + 保險絲）

下單後的倉位管理是「雙層」設計，避免逐筆輪詢 REST 把 IP 打到限流（`-1003`）：

```
            ┌ ① WebSocket（User Data Stream，主力）
進場成交 ──┤    訂單成交即時推播 → TP 成交把雙軌止損上移、倉位歸零收單
（掛好TP/SL）└ ② REST 對帳迴圈（保險絲，每 120 秒）
                批次抓帳戶級快照 → 補網/斷線重連/重啟回復/條件單沒觸發兜底
```

- 平常靠 ① 即時反應，幾乎不打 REST；② 只在 WS 漏訊息/斷線/重啟時兜底。
- 兩者共用同一套「雙軌止損上移」，有鎖序列化，不會重複上移。
- listenKey 保活由套件處理；WS 斷線會自動指數退避重連。

### 啟動後確認 WS 正常

`journalctl -u tg-group-monitor -f -o cat` 應看到：

```
[trader] 保險絲對帳迴圈啟動（每 120 秒）...
[trader] WebSocket 已連線，即時監聽訂單/倉位更新     ← 有這行才代表 WS 接上
```

運作中的正常 log：TP 成交時印「已實現 TPn → 止損上移」；倉位平掉印
「倉位已平 → CLOSED（WS 即時）」；**不該再出現 `-1003 Way too many requests`**。
若 WS 掉線會印「WS 中斷，Ns 後重連」再自動「已連線」。

## 安全提醒

- `.env`（API 憑證）和 `.session`（等同你的登入態）非常敏感，務必 `chmod 600`，別放到任何公開位置。
- session 外洩等於別人能用你帳號收發訊息；若懷疑外洩，到 Telegram App →「設定 → 隱私與安全 → 已連結的裝置」中撤銷該 session。
- 建議用低權限使用者跑，別用 root。
