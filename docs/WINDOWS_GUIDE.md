# Windows 完整使用說明 — Telegram 群組監聽工具

> 這份文件給「沒寫過程式、只用過 Word / 記事本」的人看。
> 每一個步驟都會說明:**做什麼、為什麼這樣做、應該看到什麼、出錯怎麼辦**。

---

## 這個工具能做什麼?

你是某個 Telegram 群組的**普通成員**(沒有管理權限、也不能加機器人到群裡),
但你希望群裡有訊息時,**你的個人 TG 立刻收到通知**(透過你自己的機器人轉發給你)。
這個工具會 24 小時跑在一台「雲端伺服器(VPS)」上,**你電腦關機也不影響**。

---

## 開始前準備清單

請先確認你都備齊了:

- [ ] 一個 **Telegram 帳號**(有開兩步驗證的話請記得密碼)
- [ ] 一台 **Windows 電腦**(Windows 10 或 11)
- [ ] 一個 **VPS**(雲端伺服器,Ubuntu 系統。Vultr / DigitalOcean / Linode 都可以,月費約 5-10 美金)
- [ ] VPS 的 **IP 位址** 和可以登入的 **root 密碼**(雲端商家信箱會寄)
- [ ] 大約 **60-90 分鐘**時間

> 💡 「VPS」就是一台你租來的、一直開機的電腦,只能用「終端機」黑黑的視窗操作。

---

## 目錄

1. [申請 Telegram API 憑證](#part-1-申請-telegram-api-憑證)
2. [建立你的 Telegram 機器人](#part-2-建立你的-telegram-機器人)
3. [取得你自己的 chat_id](#part-3-取得你自己的-chat_id)
4. [Windows 安裝必要軟體](#part-4-windows-安裝必要軟體)
5. [下載專案到 Windows](#part-5-下載專案到-windows)
6. [第一次填寫設定檔](#part-6-第一次填寫設定檔)
7. [找出要監聽的群組 ID](#part-7-找出要監聽的群組-id)
8. [本機首次登入(產生登入檔)](#part-8-本機首次登入產生登入檔)
9. [本機快速測試](#part-9-本機快速測試)
10. [VPS 部署](#part-10-vps-部署)
11. [管理接收者（用 Bot 指令）](#part-11-管理接收者用-bot-指令)
12. [日常維運](#part-12-日常維運)
13. [常見問題排除](#part-13-常見問題排除)
14. [安全提醒](#part-14-安全提醒)

---

## Part 1 申請 Telegram API 憑證

Telegram 為了讓自動化程式登入,需要你先在他們官網申請一組「API 憑證」。
這組憑證就像你帳號的「程式版鑰匙」。

### 步驟

1. 用瀏覽器(Chrome、Edge 都可以)打開 **https://my.telegram.org**
2. 輸入你的**手機號碼**(含國碼,例如 `+886912345678`),按 Next
3. 你的 Telegram App 會收到一則登入驗證碼(**不是簡訊**,是 TG 訊息),輸入驗證碼
4. 進入網站後,點上方的 **API development tools**
5. 在表單中填:
   - **App title**:隨便填,例如 `tg-monitor`
   - **Short name**:隨便填英數,例如 `tgmonitor`
   - **Platform**:選 **Other**
   - 其他欄位都不用填
6. 按 **Create application**
7. 頁面上會出現一個 **App api_id**(純數字,例如 `12345678`)和 **App api_hash**(一串英數字,例如 `abcdef1234567890abcdef1234567890`)

### 必須記下來的兩個值

打開 Windows 的「**記事本**」(開始選單搜尋「記事本」),把這兩個值複製貼上,
**先存起來,等等會用到**:

```
API_ID = 12345678
API_HASH = abcdef1234567890abcdef1234567890
PHONE = +886912345678
```

> ⚠️ **api_hash 等於你帳號的程式版密碼**,**不要把它貼到公開的地方**(GitHub、Facebook、群組等)。

---

## Part 2 建立你的 Telegram 機器人

機器人(Bot)是 Telegram 內建的功能,你建一個,讓它幫你「轉發」群組訊息給你自己。
這樣做的好處:**你的個人帳號完全不會做任何發送動作,避免被 TG 風控**。

### 步驟

1. 打開 Telegram App,搜尋 **@BotFather**(藍勾勾官方認證)
2. 點進去,按下方 **START**
3. 在對話框輸入指令 `/newbot` 送出
4. BotFather 會問你「Bot 的名字」(顯示名稱,可中文,例如 `我的群組監聽`),輸入並送出
5. 接著問你「Bot 的 username」,**必須用英文且以 `bot` 結尾**,例如 `jeff_tg_monitor_bot`。如果重複會被擋,改一個再試
6. 成功後 BotFather 會給你一段訊息,中間有:
   ```
   Use this token to access the HTTP API:
   123456789:AAEhBP0nvb4PhqWxxxxxxxxxxxxxxxxxxxx
   ```
7. **把這個 token 複製到記事本**(就是上面那串 `123456789:AAE...`)

### 必須記下來

```
BOT_TOKEN = 123456789:AAEhBP0nvb4PhqWxxxxxxxxxxxxxxxxxxxx
```

### 重要:先跟你的 Bot 打招呼

回到 Telegram,搜尋你剛剛建的 Bot username(例如 `@jeff_tg_monitor_bot`),點進去按 **START**。
**這一步不做的話,Bot 之後沒辦法私訊你**(TG 規定:必須使用者先互動,Bot 才能發訊息)。

---

## Part 3 取得你自己的 chat_id

`chat_id` 就是你個人帳號的「數字 ID」,Bot 要把訊息傳給你就靠這個。

### 步驟

1. 打開 Telegram,搜尋 **@userinfobot**(藍勾勾)
2. 點進去按 **START**
3. 它會立刻回你一則訊息,類似:
   ```
   Id: 987654321
   First: Jeff
   Lang: zh-hans
   ```
4. **把 Id 後面那串數字記下來**

### 必須記下來

```
BOT_TARGET = 987654321
```

---

## Part 4 Windows 安裝必要軟體

我們需要兩個工具:**Python**(跑程式)和 **Git**(下載專案)。

Windows 10/11 內建有一個假的 `python` 指令(其實是 Microsoft Store 安裝引導器,不能真的用),
我們**不要用那個**,改用 **winget**(Windows 10/11 內建的軟體安裝工具)裝官方版。

### 4-1. 開啟 PowerShell

按 **Windows 鍵**(鍵盤左下方那顆),輸入 `PowerShell`,
**右鍵點「Windows PowerShell」→「以系統管理員身分執行」**。

跳出 UAC 確認框 → 按「是」。

你會看到藍藍的視窗,長這樣:
```
PS C:\WINDOWS\system32>
```

`>` 後面就是你輸入指令的地方。

### 4-2. 用 winget 裝 Python 和 Git

把這兩行**分別**複製貼到 PowerShell,按 Enter(一次貼一行,等第一行跑完再貼第二行):

```powershell
winget install Python.Python.3.12
```

```powershell
winget install Git.Git
```

中間可能會跳 UAC 確認框 → 按「是」。會看到下載進度條,等 1-5 分鐘。

### 4-3. 完全關掉 PowerShell,重新打開

裝完後 PATH 環境變數要重新讀取,**請完全關閉 PowerShell 視窗**(右上角 X),
再按 Windows 鍵搜尋「PowerShell」**重新打開**(這次不用以系統管理員身分,**用一般權限**就好)。

### 4-4. 驗證安裝成功

```powershell
python --version
git --version
```

應該分別看到 `Python 3.12.x` 和 `git version 2.x.x.windows`。

> ⚠️ **如果 `python --version` 跳出 Microsoft Store 的視窗或顯示沒裝**:
> 代表你的 PATH 還在用內建假版本。執行 `where.exe python` 確認:
> - 看到的第一行是 `C:\Users\你的名字\AppData\Local\Programs\Python\Python312\python.exe` → 正確
> - 看到 `C:\Users\你的名字\AppData\Local\Microsoft\WindowsApps\python.exe` → 錯的
> 開「設定 → 應用程式 → 應用程式執行別名」,**關掉**所有 `python.exe` / `python3.exe` 的別名,
> 然後重開 PowerShell 再試。

---

## Part 5 下載專案到 Windows

### 步驟

```powershell
cd $HOME
git clone https://github.com/jeff11142/tg-group-monitor.git
cd tg-group-monitor
ls
```

逐行說明:
- `cd $HOME` 切換到你的「使用者資料夾」(`C:\Users\你的名字`)
- `git clone ...` 把整個專案下載成 `tg-group-monitor` 資料夾
- `cd tg-group-monitor` 進入該資料夾
- `ls` 列出資料夾內容

你應該看到:
```
README.md
config.example.env
deploy
docs
main.py
requirements.txt
```

> 看不到的話:確認 `git clone` 那行有沒有錯字,網路是否通。

### 建立隔離的 Python 環境

```powershell
python -m venv .venv
```

接下來要「啟用」這個環境,但 PowerShell 預設禁止跑 `.ps1` 腳本,**先放行(只本次 session 有效)**:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

然後啟用環境(注意 `.\` 開頭很重要,不可省略):

```powershell
.\.venv\Scripts\Activate.ps1
```

**啟用成功的話,prompt 最前面會多出 `(.venv)` 字樣**:
```
(.venv) PS C:\Users\你的名字\tg-group-monitor>
```

裝套件:

```powershell
python -m pip install -r requirements.txt
```

> ⚠️ 為什麼用 `python -m pip` 而不是直接 `pip`?
> Windows 11 有「Smart App Control」會擋掉某些未簽章的小程式(`pip.exe` 就是其中之一),
> 用 `python -m pip` 透過 Python 本體呼叫就不會被擋。

會看到一堆 `Collecting...`、`Installing...`,等 1-3 分鐘。

---

## Part 6 第一次填寫設定檔

專案有個範本檔 `config.example.env`,我們複製一份成 `.env` 後填值。
`.env` 就是你的所有設定的家。

### 步驟

```powershell
Copy-Item config.example.env .env
notepad .env
```

`notepad .env` 會用 Windows 內建的「**記事本**」打開 `.env`。

**第一階段先只填這幾欄**(其他先空著或保持原樣):

```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
PHONE=+886912345678
SESSION_NAME=tg_monitor

# 下面這幾欄先留空,後面會回來填
SOURCE_CHAT=
KEYWORDS=

BOT_TOKEN=123456789:AAEhBP0nvb4PhqWxxxxxxxxxxxxxxxxxxxx
BOT_TARGET=987654321
ADMIN_CHAT_ID=987654321

WEBHOOK_URL=
LOG_TO_FILE=1
LOG_FILE=messages.jsonl
LIST_DIALOGS=1
```

> 💡 `ADMIN_CHAT_ID` 通常**和 `BOT_TARGET` 一樣**（你自己的 chat_id），
> 這代表「能對 Bot 下管理指令的人是你」，其他人對 Bot 講話只會被回覆 chat_id 提示。

**特別注意 `LIST_DIALOGS=1`**:這個設定讓程式跑起來「只列出你的對話清單,然後結束」,
方便我們找出要監聽的群組 ID。

存檔(`Ctrl + S`),關掉記事本。

> ⚠️ 存檔時若記事本問「編碼」,選 **UTF-8**(不要 ANSI)。

---

## Part 7 找出要監聽的群組 ID

### 步驟

確認你還在 `(.venv) PS ...tg-group-monitor>` 的 prompt,執行:

```powershell
python main.py
```

第一次跑會要登入:

```
Please enter your phone (or bot token):
```
→ 按 Enter(因為 `.env` 已經設了 PHONE,通常會自動帶,但若它再問一次,直接按 Enter 或輸入你的手機號)

```
Please enter the code you received:
```
→ 打開你手機 Telegram App,**會收到一則訊息附 5 位數驗證碼**(注意:**是 TG 訊息不是簡訊**),輸入後 Enter

```
Please enter your password:
```
(若你有開兩步驗證才會問)
→ 輸入你的**兩步驗證密碼**(就是你在 TG 設定的雲端密碼,大小寫敏感,輸入時不會顯示)

登入成功後會印出你所有的對話清單,類似:

```
=== 你的對話清單(用下面的 id 填入 SOURCE_CHAT)===
[群組] '幣圈訊號分享群'  id=-1001234567890
[群組] '朋友聚會'  id=-1009876543210
[私訊] 'Mom'  id=123456789
[頻道] 'XX 新聞台'  id=-1001111111111
...
```

**找到你要監聽的群組那一行,把 `id=` 後面的數字記下來**(含負號)。

程式列完會自動結束,回到 prompt。

### 把群組 ID 填回 .env

```powershell
notepad .env
```

修改兩個欄位:

```env
SOURCE_CHAT=-1001234567890     # ← 換成你的群組 ID
LIST_DIALOGS=0                  # ← 從 1 改成 0(否則之後每次跑都只列清單)
```

存檔關掉。

---

## Part 8 本機首次登入(產生登入檔)

剛才登入時其實已經產生了 `tg_monitor.session` 檔。確認一下:

```powershell
ls *.session
```

應該看到 `tg_monitor.session`(這個檔等同你 TG 帳號的登入態,**非常敏感**)。

---

## Part 9 本機快速測試

跑起來看是否正常監聽:

```powershell
python main.py
```

預期輸出:
```
已登入：Jeff (@your_username)
開始監聽：-1001234567890
關鍵字：（無,全部訊息）
Bot 轉發：開 → 987654321  | Webhook：關
```

**測試**:用 TG 在那個群組丟一則任意訊息,終端機應該幾秒內印出 `[命中] ...`,
同時你的 Bot 會私訊你格式化後的訊息。

確認 OK 後按 **Ctrl + C** 停掉。

### 在進入 VPS 部署前

**先停掉本機這支程式**(剛才已 Ctrl+C 過就好)。
之後 VPS 開始跑後,**本機絕對不能同時再跑**,否則同一個 session 互相搶會被 TG 強制登出。

---

## Part 10 VPS 部署

接下來要把同樣這個程式裝到 VPS,讓它 24 小時跑。

### 10-1. 用 PowerShell 連到你的 VPS

Windows 10 / 11 內建有 ssh 指令,不用額外裝。

```powershell
ssh root@你的VPS_IP
```

例如 VPS IP 是 `123.45.67.89`:
```powershell
ssh root@123.45.67.89
```

第一次連會問:
```
Are you sure you want to continue connecting (yes/no)?
```
打 `yes` 按 Enter。

接著輸入 VPS 的 root 密碼(雲端商家信箱寄給你的那個,**輸入時不顯示**)。

成功會看到類似:
```
root@vultr:~#
```

**`#` 結尾代表你現在是 root(最高權限),小心打指令。**

> 💡 如果你的 PowerShell 沒有 `ssh` 指令(很舊的 Windows 10),
> 改用 **Windows Terminal**(Microsoft Store 免費裝)或 **PuTTY**(google 下載)。

### 10-2. 系統更新

```bash
apt update && apt upgrade -y
```

等 1-5 分鐘。中間若跳出紫色畫面問要不要 keep 某個檔,**按 Enter 接受預設**就好。

### 10-3. 裝 Python 和 Git

```bash
apt install -y python3 python3-venv python3-pip git
```

### 10-4. 建低權限使用者(安全)

直接用 root 跑長期服務有風險,我們建一個 `tgmon` 帳號專門跑這支程式。

```bash
adduser --gecos "" tgmon
```

接下來:
- 會問你**新使用者密碼**:**請設一個強密碼**(大小寫+數字+符號,12 字以上),
  例如 `Tgmon-Vps-2026!Safe`(請改成你自己想的,別照抄)
- 系統會檢查密碼強度,太簡單會被擋,改強一點再試
- 要輸入兩次確認(畫面不顯示)

> ⚠️ **這個密碼要記住**,之後 `sudo` 或 SSH 切換到 tgmon 都會用到。
> 記到你 Windows 記事本裡。

加入 sudo 群組(讓他可以暫時提權):
```bash
usermod -aG sudo tgmon
```

### 10-5. 給 tgmon 可以用 SSH 登入

```bash
mkdir -p /home/tgmon/.ssh
cp /root/.ssh/authorized_keys /home/tgmon/.ssh/ 2>/dev/null || true
chown -R tgmon:tgmon /home/tgmon/.ssh
chmod 700 /home/tgmon/.ssh
chmod 600 /home/tgmon/.ssh/authorized_keys 2>/dev/null || true
```

### 10-6. 開放 /opt 給 tgmon 用

```bash
mkdir -p /opt && chown tgmon:tgmon /opt
```

### 10-7. 切換到 tgmon、下載程式碼

```bash
su - tgmon
cd /opt
git clone https://github.com/jeff11142/tg-group-monitor.git
cd tg-group-monitor
ls
```

看到 `main.py / requirements.txt / deploy / docs / README.md` 表示成功。

> ⚠️ 若 `git clone` 報「Authentication failed」,代表這個 repo 被設成 private,
> 請在你 Windows 瀏覽器登入 GitHub,把 repo 改回 public(Settings → 最底 Danger Zone → Change visibility)。

### 10-8. 從 Windows 上傳機密檔到 VPS

**這一步要回到你的 Windows**,**開新的 PowerShell 視窗**(不要關掉 SSH 那邊):

```powershell
cd $HOME\tg-group-monitor
scp .env tg_monitor.session tgmon@你的VPS_IP:/opt/tg-group-monitor/
```

例如:
```powershell
scp .env tg_monitor.session tgmon@123.45.67.89:/opt/tg-group-monitor/
```

第一次會問 yes,然後輸入 tgmon 密碼(就是剛剛步驟 10-4 設的那個)。

完成後**回到 SSH 那個 PowerShell**(tgmon 身分),確認:
```bash
ls -la .env tg_monitor.session
```
兩個檔都該存在。

### 10-9. 在 VPS 上裝環境並測試

```bash
chmod 600 .env tg_monitor.session
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

應該直接看到:
```
已登入：Jeff (@your_username)
開始監聽：-1001234567890
Bot 轉發：開 → 987654321  | Webhook：關
```

→ 用 TG 在源群組丟訊息,Bot 應該立刻通知你。

確認後 **Ctrl + C** 停掉。

### 10-10. 設定 systemd 開機自動啟動

systemd 是 Linux 內建的「服務管理器」,讓程式在背景跑、重開機後自動啟動、當機自動重啟。

先回到 root 身分:
```bash
exit
```
prompt 變回 `root@vultr:~#`。

**編輯服務檔**,把預設的使用者改成 tgmon:
```bash
nano /opt/tg-group-monitor/deploy/tg-group-monitor.service
```

找到 `User=ubuntu` 那行,改成:
```ini
User=tgmon
```

存檔離開:`Ctrl + O` → `Enter` → `Ctrl + X`。

**安裝服務並啟動**:
```bash
cp /opt/tg-group-monitor/deploy/tg-group-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tg-group-monitor
```

**確認狀態**:
```bash
systemctl status tg-group-monitor
```

要看到:
```
● tg-group-monitor.service - Telegram 群組關鍵字監聽轉發
     Loaded: loaded ...
     Active: active (running) since ...
```

按 `q` 離開。

**即時看 log**(確認真的在收訊息):
```bash
journalctl -u tg-group-monitor -f
```

→ TG 群組再丟一則訊息測試,終端機應該即時印出 `[命中] ...`,Bot 也會收到。

按 `Ctrl + C` 離開 log 視窗(**不會停掉服務,只是不看 log 了**)。

### 10-11. 測試重開機後自動啟動

```bash
reboot
```

VPS 會斷線(約 30-60 秒)。等一下重新 SSH 進來:
```powershell
ssh root@你的VPS_IP
systemctl status tg-group-monitor
```

仍然 `active (running)` 就代表設定成功。

**到這裡,部署完成!你可以關掉電腦,程式會繼續在 VPS 跑。**

---

## Part 11 管理接收者（用 Bot 指令）

服務啟動後，**直接在 TG 跟你的 Bot 對話**就能管理「誰會收到通知」。完全不用 SSH、不用碰程式碼。

### 可用指令（只有你能用，因為你是 admin）

| 指令 | 範例 | 行為 |
|------|------|------|
| `/list` | `/list` | 列出所有接收者，含啟用狀態 |
| `/add` | `/add 123456789 Alice` | 新增接收者；name 可省略 |
| `/remove` | `/remove 123456789` | 從清單移除 |
| `/enable` | `/enable 123456789` | 啟用某接收者 |
| `/disable` | `/disable 123456789` | 暫停某接收者（不刪資料、可恢復） |
| `/myid` | `/myid` | 回你自己的 chat_id |
| `/help` | `/help` | 顯示指令清單 |

### 怎麼幫朋友開通接收？

1. **告訴朋友先對 Bot 按 START**（搜尋你的 Bot username，例如 `@jeff_tg_monitor_bot`）
2. 朋友對 Bot 隨便講一句話（例如「你好」）
3. Bot 會自動回他「你的 chat_id 是 12345678」
4. 朋友把這個數字傳給你
5. 你在自己跟 Bot 的對話打 `/add 12345678 朋友的名字`
6. 從此朋友會跟著收到所有訊號通知

### 接收者資料存在哪？

VPS 上的 `/opt/tg-group-monitor/recipients.db`（SQLite 檔）。
這個檔**不會進 GitHub**，每台機器自己一份。

> ⚠️ 第一次跑時，程式會自動把 `BOT_TARGET`（也就是你自己的 chat_id）加進清單當第一筆，
> 所以剛部署完畢，你已經會收到通知，不用先 `/add` 自己。

---

## Part 12 日常維運

以下指令都在 **VPS 上以 root 身分執行**(SSH 進去後直接打)。

### 看最近的 log

```bash
journalctl -u tg-group-monitor -n 200
```
(最近 200 行)

### 即時看 log(像看 LINE 對話)

```bash
journalctl -u tg-group-monitor -f
```
按 `Ctrl + C` 離開。

### 重啟服務(例如改了 .env 之後)

```bash
systemctl restart tg-group-monitor
```

### 暫時停止監聽

```bash
systemctl stop tg-group-monitor
```

### 重新開啟監聽

```bash
systemctl start tg-group-monitor
```

### 改設定檔 .env

```bash
nano /opt/tg-group-monitor/.env
```
編輯 → 存檔(Ctrl+O Enter Ctrl+X)→ 重啟:
```bash
systemctl restart tg-group-monitor
```

### 更新程式碼(當你或工程師朋友又改了 GitHub 上的程式)

```bash
sudo -u tgmon -i bash -c 'cd /opt/tg-group-monitor && git pull'
systemctl restart tg-group-monitor
```

### 查看訊息歷史紀錄

所有命中關鍵字的訊息都寫在 `messages.jsonl`(VPS 上),每行一筆 JSON:

```bash
tail -n 20 /opt/tg-group-monitor/messages.jsonl
```
(看最近 20 筆)

---

## Part 13 常見問題排除

### Q1. PowerShell 跳「`.venv` could not be loaded」或「ApplicationFailedException」

**`.venv\Scripts\Activate.ps1` 前面要加 `.\`**:
```powershell
.\.venv\Scripts\Activate.ps1
```
如果還是不行,先放行腳本執行:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```
再 activate。

### Q2. `pip install` 報「Application Control policy has blocked」

Windows 11 的 Smart App Control 擋掉 pip.exe。**改用 `python -m pip`**:
```powershell
python -m pip install -r requirements.txt
```

### Q3. `python main.py` 顯示 `PasswordHashInvalidError`

**兩步驗證密碼(雲端密碼)輸錯**了。重跑 `python main.py`,
在 `Please enter your password:` 時**仔細輸入**(大小寫敏感,輸入不會顯示)。

若連舊密碼都忘了:TG App → 設定 → 隱私與安全 → 兩步驗證 → 變更密碼(可走 email 重設)。

### Q4. SSH 連 VPS 一直問密碼還是進不去

- IP 對嗎?雲端商家後台確認一次
- 你是不是用 `ssh root@` 開頭?新買的 VPS 通常只有 root 能用密碼登入
- 密碼是不是複製貼上時多了空格?手動打一次

### Q5. VPS adduser 時報「BAD PASSWORD: too simplistic」

密碼太簡單被擋了。改強一點:
- 至少 12 字元
- 大小寫英文 + 數字 + 符號都有
- 不要常見單字、不要 `qwerty` `12345` 這類

例如 `Tgm0n@Vps-Run!2026`(請改成你自己的)。

### Q6. VPS 上 `chown /opt` 報「Operation not permitted」

代表你目前不是 root,先 `exit` 回到 root 身分(prompt 是 `root@xxx:~#`),
再執行 root 才能做的指令。

### Q7. Bot 沒收到訊息

- 你有沒有在 TG 找你的 Bot 按 START?**沒按就不能私訊你**
- `.env` 的 `BOT_TOKEN` 和 `BOT_TARGET` 是否正確
- 群組 ID 是不是負號開頭(`-1001234567890`),而不是正數
- 看 log:`journalctl -u tg-group-monitor -n 100`,有沒有錯誤訊息

### Q8. log 顯示 `bot 轉發失敗: chat not found`

`BOT_TARGET`(你的 chat_id)填錯,或你沒對 Bot 按 START。

### Q9. systemd 服務啟動失敗

```bash
systemctl status tg-group-monitor
journalctl -u tg-group-monitor -n 50
```
看錯誤訊息。常見:
- `.env` 路徑或檔名錯 → 確認 `/opt/tg-group-monitor/.env` 存在
- `tg_monitor.session` 沒上傳 → 重新從 Windows scp 一次
- Python 套件沒裝 → `cd /opt/tg-group-monitor && .venv/bin/pip install -r requirements.txt`

### Q10. 我想新增監聽的群組

目前一個程式只能監聽一個群組。要監聽多個群組,得改 `.env` 的 `SOURCE_CHAT` 後重啟,
或請工程師朋友幫你改成支援多群組的版本。

### Q11. 我想用關鍵字過濾,只在出現特定字才通知

編輯 `.env`:
```env
KEYWORDS=BTC,ETH,做多
```
逗號分隔,只要訊息含任一就會通知。重啟服務生效:
```bash
systemctl restart tg-group-monitor
```

---

## Part 14 安全提醒

1. **`.env` 和 `tg_monitor.session` 絕對不要外流**
   - `.env` 含 API 憑證和 Bot Token
   - `tg_monitor.session` 等於你 TG 帳號的登入態,**外洩等於別人能用你帳號收發訊息**
   - 兩個都已用 `chmod 600` 鎖住,只有 tgmon 自己能讀

2. **懷疑 session 外洩怎麼辦?**
   - 打開 Telegram App → 設定 → 隱私與安全 → 已連結的裝置
   - 找到 `Telethon` 或類似的程式 session → 撤銷
   - 重新在本機跑 `python main.py` 重新登入,產生新的 session,再上傳到 VPS

3. **VPS root 密碼**
   - 建議改用 SSH 金鑰登入,**完全停掉密碼登入**(進階,可請朋友幫忙)
   - 或至少:用很強的 root 密碼、別在公開電腦輸入

4. **不要把 .env / .session 提交到 GitHub**
   - 專案的 `.gitignore` 已經擋住,正常用 `git push` 不會洩漏
   - 但如果你手動加進去,還是會洩漏,**任何時候 `git status` 看到這兩個檔出現在追蹤清單,立刻停手**

5. **遵守 Telegram 使用條款**
   - 只用在你有權限的群組
   - 不要大量爬取、不要轉發給未授權的人
   - 違反條款可能被永久封鎖帳號

---

## 完成了!

從這裡開始,程式會 24 小時跑在 VPS 上。
你不需要再開電腦、不需要再做任何事,直到:
- 想改設定(改 `.env`)
- 想更新程式碼(`git pull`)
- 或想關掉(`systemctl stop tg-group-monitor`)

有任何問題回到 **Part 13 常見問題** 找答案,或把錯誤訊息貼給工程師朋友看。
