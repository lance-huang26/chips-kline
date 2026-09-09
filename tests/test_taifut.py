#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台指期近月解析的回歸測試。

fixture 是 2026/08/18–20 的真實回應，跨越 8 月結算日（08/19），
所以「換月」這件事有被實際涵蓋到。

跑法：python3 tests/test_taifut.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import taifut  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "taifut_TX_202608.csv")
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def expect_raises(exc, fn, msg):
    try:
        fn()
    except exc:
        return
    except Exception as e:
        fails.append("%s：丟出的是 %s（%s）" % (msg, type(e).__name__, e))
        return
    fails.append("%s：什麼都沒丟出來" % msg)


csv_text = open(FIX, encoding="utf-8").read()
_HEAD_ONLY = csv_text.split("\n")[0] + "\n"
rows = taifut.parse(csv_text)
by = {r["date"]: r for r in rows}

# ---------------------------------------------------------------- 每天只留一筆
check(len(rows) == 3, "三個交易日應該只回三筆，實得 %d" % len(rows))
check(sorted(by) == ["2026/08/18", "2026/08/19", "2026/08/20"],
      "日期不對：%s" % sorted(by))

# ---------------------------------------------------------------- 近月與換月
check(by["2026/08/18"]["_expiry"] == "202608", "08/18 近月應該是 202608")
check(by["2026/08/19"]["_expiry"] == "202608",
      "結算日當天舊月份還在交易，近月仍應是 202608，實得 %s" % by["2026/08/19"]["_expiry"])
check(by["2026/08/20"]["_expiry"] == "202609",
      "結算日隔天應該自動換到 202609，實得 %s" % by["2026/08/20"]["_expiry"])
print("  換月：08/18 202608 → 08/19（結算日）202608 → 08/20 202609，自動發生")

# ---------------------------------------------------------------- 取值正確
r = by["2026/08/20"]
check((r["open"], r["high"], r["low"], r["close"], r["volume"])
      == (44950, 45122, 44448, 44868, 53693),
      "08/20 的 OHLC/量不對：%s" % r)
check(by["2026/08/18"]["close"] == 45085, "08/18 收盤不對：%s" % by["2026/08/18"]["close"])

# ---------------------------------------------------------------- 排除盤後
# 08/18 盤後收 45,811；如果誤抓，收盤會變成那個數字
check(by["2026/08/18"]["close"] != 45811, "抓到盤後時段了（45,811），應該只取一般時段")
check(by["2026/08/19"]["close"] != 44527, "抓到盤後時段了（44,527）")
print("  時段：只取一般時段，盤後那筆沒有被誤抓")

# ---------------------------------------------------------------- 排除價差列
# 「202608/202609」那種列的價格是兩個月份的價差（兩三位數），
# 誤抓的話 K 線會變成一堆 58~343 的數字，而且和股價差好幾個數量級
for d, r2 in by.items():
    check(r2["close"] > 10000,
          "%s 收盤 %s 看起來像價差列的數字（價差列必須被排除）" % (d, r2["close"]))
check(all(taifut.FRONT_MONTH_RE.match(r2["_expiry"]) for r2 in rows),
      "到期月份應該都是純 6 位數")
print("  價差列：202608/202609 這類列已排除（不然收盤會變成 58~343 之類的數字）")

# ---------------------------------------------------------------- 換月跳空是已知代價
gap = by["2026/08/20"]["close"] - by["2026/08/19"]["close"]      # 連續序列看到的
own = by["2026/08/20"]["close"] - 44528                          # 202609 自己的變化
check(abs(gap - own) > 50,
      "這組 fixture 本來就該呈現換月落差，用來提醒序列在換月當天不連續")
print("  換月落差：連續序列 %+d 點，但 202609 自己走了 %+d 點——差 %d 點是換月造成的"
      % (gap, own, own - gap))

# ---------------------------------------------------------------- 失敗要吵
expect_raises(taifut.TaifutError, lambda: taifut.parse(""), "空內容應該報錯")
# 只有表頭、沒有任何 TX 列 = 該月份還沒公布，屬於正常，回空陣列讓上層當「尚未公布」，
# 不能報錯——不然排程在資料還沒出來的時候會整個 workflow 失敗
check(taifut.parse(_HEAD_ONLY) == [],
      "只有表頭時應該回空陣列（尚未公布），而不是報錯")
expect_raises(taifut.TaifutError,
              lambda: taifut.parse("交易日期,契約,亂七八糟\n2026/08/20,TX,x"),
              "表頭少欄位應該報錯（期交所改版）")
_lines = [l for l in csv_text.split("\n") if l.strip()]
only_spread = "\n".join(
    [_lines[0]] + [l for l in _lines[1:] if "/" in l.split(",")[2]])
expect_raises(taifut.TaifutError, lambda: taifut.parse(only_spread),
              "只剩價差列時應該報錯，而不是回一堆價差當 K 線")
print("  尚未公布（只有表頭）回空陣列；空內容 / 表頭改版 / 只剩價差列則報錯")

# ---------------------------------------------------------------- 月份範圍
check(taifut.month_range("202602") == ("2026/02/01", "2026/02/28"), "2 月底算錯")
check(taifut.month_range("202612") == ("2026/12/01", "2026/12/31"), "12 月底算錯")
check(taifut.month_range("202402") == ("2024/02/01", "2024/02/29"), "閏年 2 月算錯")

print()
if fails:
    print("❌ 失敗 %d 項：" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 台指期近月解析通過（換月、時段、價差列排除、失敗路徑）")
