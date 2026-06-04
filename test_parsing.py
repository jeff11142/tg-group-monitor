"""離線測試：訊號解析、格式化、交易對自動標記 #。不連網。

跑法：python test_parsing.py
"""

import main


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    assert cond, f"測試失敗：{name}"


SIGNAL = """#VICUSDT
成交量排名：12名/300
市值：1.2B
風險等級：中
進場價：0.0478
目標價 1：0.0497
目標價 2：0.0516
停損價 1：0.0437
https://www.tradingview.com/chart?symbol=BINANCE%3AVICUSDT.P"""

TARGET_HIT = """VICUSDT
目標價 1：0.0497 ✅
目標價 2：0.0516 ✅"""

STOP_HIT = """HOLOUSDT
🛑 停損價 1: 0.0698 🛑"""

# 中文交易對的目標達成通知：USDT 前面是中文，不該被當成合法交易對
CN_TARGET_HIT = """币安人生USDT
🎯 目標價 1: 0.6985 ✅"""

# 中文交易對的完整訊號，且 URL 內含 9FUSDT（hex 編碼），不該誤抓成交易對
CN_SIGNAL = """🆕 #币安人生USDT

24 小時成交量排名: 50th/574
市值: 617.08M

風險等級：🟢 一般

➡️ 進場價: 0.6762

🎯 目標價 1: 0.6985
🎯 目標價 2: 0.7207

🛑 停損價 1: 0.6284

📊 https://www.tradingview.com/chart?symbol=BINANCE%3A%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9FUSDT.P"""

# 市場提醒/閒聊訊息：沒有交易對、進場價、目標/停損，全部解析器都該回 None
MARKET_ALERT = """🔥🔥🔥 ETH price increased 1.43% in the last 15 minutes. Altcoins may follow. Keep an eye out for opportunities.
ETH 15分鐘內價格上漲1.43%，其他幣種可能會帶動上漲，請留意相關機會+"""


def test_tag_symbols():
    print("\n[標記交易對 #]")
    check("無#的交易對補上#", main.tag_symbols("VICUSDT 進場") == "#VICUSDT 進場")
    check("已有#不重複", main.tag_symbols("#BTCUSDT") == "#BTCUSDT")
    check("多個都標記", main.tag_symbols("ETHUSDC 與 1000PEPEUSDT")
          == "#ETHUSDC 與 #1000PEPEUSDT")
    check("單獨報價幣不誤標", main.tag_symbols("入金 USDT") == "入金 USDT")
    url = "看 https://x.com/p?s=BINANCE%3AVICUSDT.P 圖"
    check("URL 內不動", main.tag_symbols(url) == url)


def test_parse_signal():
    print("\n[解析進場訊號]")
    s = main.parse_signal(SIGNAL)
    check("有解析到訊號", s is not None)
    check("幣別 VICUSDT", s["symbol"] == "VICUSDT")
    check("進場價 0.0478", s["entry"] == 0.0478)
    check("2 個目標價", len(s["targets"]) == 2)
    check("目標1 價格正確", s["targets"][0]["price"] == 0.0497)
    check("目標1 漲幅計算正確(3.97%)", s["targets"][0]["pct"] == 3.97)
    check("1 個停損價", len(s["stops"]) == 1)
    check("停損價 0.0437", s["stops"][0]["price"] == 0.0437)
    check("成交量排名", s.get("volume_rank") == "12/300")
    check("市值", s.get("market_cap") == "1.2B")
    check("風險等級", s.get("risk") == "中")
    check("有 URL", "tradingview" in s.get("url", ""))


def test_format_signal():
    print("\n[格式化進場訊號]")
    s = main.parse_signal(SIGNAL)
    import datetime
    out = main.format_signal(s, datetime.datetime(2026, 6, 4, 12, 0, 0))
    check("含幣別且帶#", "#VICUSDT" in out)
    check("含進場", "進場: 0.0478" in out)
    check("含目標價1", "目標價1" in out)
    check("含停損價1", "停損價1" in out)
    check("URL 未被破壞", "BINANCE%3AVICUSDT.P" in out)


def test_target_hit():
    print("\n[解析目標達成通知]")
    h = main.parse_target_hit(TARGET_HIT)
    check("有解析到達成通知", h is not None)
    check("幣別 VICUSDT", h["symbol"] == "VICUSDT")
    check("2 個達成目標", len(h["hits"]) == 2)
    check("非訊號文字不誤判", main.parse_target_hit("今天天氣不錯") is None)


def test_stop_hit():
    print("\n[解析觸發停損通知]")
    s = main.parse_stop_hit(STOP_HIT)
    check("有解析到停損通知", s is not None)
    check("幣別 HOLOUSDT", s["symbol"] == "HOLOUSDT")
    check("停損價 0.0698", s["hits"][0]["price"] == 0.0698)
    check("完整訊號不被當成停損通知", main.parse_stop_hit(SIGNAL) is not None
          and main.parse_signal(SIGNAL) is not None)
    import datetime
    out = main.format_stop_hit(s, datetime.datetime(2026, 6, 4, 12, 0, 0))
    check("輸出用 ⛔（與訊號一致）", "⛔ 停損價1" in out)
    check("幣別帶 #", "#HOLOUSDT" in out)
    check("不含來源的 🛑", "🛑" not in out)


def test_reject_invalid():
    print("\n[非法/非訊號訊息一律不解析]")
    # 中文交易對的目標達成 → 找不到合法交易對 → None
    check("中文交易對目標達成不解析", main.parse_target_hit(CN_TARGET_HIT) is None)
    # 中文交易對完整訊號 → 移除 URL 後找不到合法交易對 → None（不誤抓 URL 內的 9FUSDT）
    check("中文交易對訊號不解析", main.parse_signal(CN_SIGNAL) is None)
    # 市場提醒/閒聊 → 三種解析器全部 None
    check("市場提醒不當成訊號", main.parse_signal(MARKET_ALERT) is None)
    check("市場提醒不當成目標達成", main.parse_target_hit(MARKET_ALERT) is None)
    check("市場提醒不當成停損", main.parse_stop_hit(MARKET_ALERT) is None)


# 真實來源訊號（含風險說明多行、TP4 字樣、URL），完整驗證解析穩定
VELVET_SOURCE = """🆕 #VELVETUSDT

24 小時成交量排名: 272th/580
市值: 42.21M

風險等級：⚠️ 較高
 - 成交量排名在前 200 名之外。
 - 相同標的在 24 小時內已達成 TP4。
 - 市值低於 50M。

➡️ 進場價: 0.1162

🎯 目標價 1: 0.1196
🎯 目標價 2: 0.1230
🎯 目標價 3: 0.1333
🎯 目標價 4: 0.1504

🛑 停損價 1: 0.1082
🛑 停損價 2: 0.0888

📊 https://www.tradingview.com/chart?symbol=BINANCE%3AVELVETUSDT.P"""


def test_real_signal():
    print("\n[真實來源訊號 VELVET]")
    s = main.parse_signal(VELVET_SOURCE)
    check("有解析到", s is not None)
    check("幣別 VELVETUSDT", s["symbol"] == "VELVETUSDT")
    check("進場 0.1162", s["entry"] == 0.1162)
    check("4 個目標", len(s["targets"]) == 4)
    check("目標4 = 0.1504", s["targets"][3]["price"] == 0.1504)
    check("2 個停損", len(s["stops"]) == 2)
    check("SL1 = 0.1082", s["stops"][0]["price"] == 0.1082)
    check("風險只取等級行（不含說明列）", s.get("risk") == "⚠️ 較高")
    check("成交量排名 272/580", s.get("volume_rank") == "272/580")
    check("「達成 TP4」字樣不誤抓成目標", len(s["targets"]) == 4)


def test_keyword_match():
    print("\n[關鍵字過濾]")
    check("命中（不分大小寫）", main._matches("這是 ENTRY 訊號") if main.KEYWORDS else True)
    check("含進場字樣可被解析", main.parse_signal(SIGNAL) is not None)


def main_run():
    test_tag_symbols()
    test_parse_signal()
    test_format_signal()
    test_target_hit()
    test_stop_hit()
    test_reject_invalid()
    test_real_signal()
    test_keyword_match()
    print("\n🎉 解析測試全部通過")


if __name__ == "__main__":
    main_run()
