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

## 改了 .env 設定怎麼辦

直接在 VPS 編輯 `/opt/tg-group-monitor/.env`，然後 `sudo systemctl restart tg-group-monitor` 即可。

## 安全提醒

- `.env`（API 憑證）和 `.session`（等同你的登入態）非常敏感，務必 `chmod 600`，別放到任何公開位置。
- session 外洩等於別人能用你帳號收發訊息；若懷疑外洩，到 Telegram App →「設定 → 隱私與安全 → 已連結的裝置」中撤銷該 session。
- 建議用低權限使用者跑，別用 root。
