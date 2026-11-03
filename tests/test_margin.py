#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""margin.py 的解析與閘門測試。一次對外請求都不發，全部吃 tests/fixtures/。

fixture 是 2026/11/02 的真實回應，只是把個股列裁到幾檔、表尾合計改成那幾檔的
合計（不然自洽檢查一定過不了）。每個檔案的第一個 key `_fixture` 寫著哪些值
是原始的、哪些被改過。

這一組測試在意的是「來源改版 / 單位弄錯 / 資料缺一塊的時候，會不會安靜地
算出一個偏低的數字」——那是這個功能最糟糕的失敗方式，因為 170% 和 168%
看起來都很合理，沒有人會發現。所以每一個閘門都有一個對應的失敗測試。
"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import margin  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
DATE = "2026/11/02"

# 手算的期望值（fixture 裡的 4 + 5 檔）：
#   上市 00400A 14,327×1000×13.79 =    197,569,330
#        1101   37,267×1000×28.10 =  1,047,202,700
#        2330   35,219×1000×2330  = 82,060,270,000
#        1589   37 張，當天真的沒有行情列 → 跳過（0.0426% < 0.5%）
#        分子 83,305,042,030 ÷ 分母 48,500,000 仟元 = 171.76%
#   上櫃 8932 59,855×1000×89.20   =  5,339,066,000
#        6182 55,419×1000×94.10   =  5,214,927,900
#        1815 51,643×1000×139.00  =  7,178,377,000
#        3105 42,425×1000×588.00  = 24,945,900,000
#        4207 627 張，停牌沒有行情列 → 跳過（0.2986% < 0.5%）
#        分子 42,678,270,900 ÷ 分母 25,000,000 仟元 = 170.71%
#   合併 125,983,312,930 ÷ 73,500,000,000 = 171.41%
EXPECT = {"ratio": 171.41, "twse_ratio": 171.76, "otc_ratio": 170.71,
          "numerator": 125983312930, "denominator": 73500000000}

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def expect_raises(name, exc, fn):
    try:
        fn()
    except exc as e:
        print("  ok   %s  → %s" % (name, str(e)[:78]))
        return
    except Exception as e:                                        # noqa: BLE001
        print("  FAIL %s  丟的是 %s 而不是 %s：%s"
              % (name, type(e).__name__, exc.__name__, e))
        fails.append(name)
        return
    print("  FAIL %s  沒有丟 %s" % (name, exc.__name__))
    fails.append(name)


def load(n):
    with open(os.path.join(FIX, n), encoding="utf-8") as f:
        return json.load(f)


def payloads():
    return (load("margin_twse_20261102.json"), load("quotes_twse_20261102.json"),
            load("margin_tpex_20261102.json"), load("quotes_tpex_20261102.json"))


# ---------------------------------------------------------------- 1 正常路徑
print("1. 正常路徑（fixture 的 4+5 檔）")
a, b, c, d = payloads()
got = margin.compute(a, b, c, d, DATE)
for k, v in EXPECT.items():
    check("%s = %s" % (k, v), got[k] == v, "實際 %s" % got[k])
check("日期統一成斜線格式", got["date"] == DATE, got["date"])
check("上市張數 = 表尾 86,850", got["_detail"]["twse_lots"] == 86850)
check("上櫃張數 = 表尾 209,969", got["_detail"]["otc_lots"] == 209969)
check("上市缺價 37 張（1589）", got["_detail"]["twse_miss_lots"] == 37)
check("上櫃缺價 627 張（4207）", got["_detail"]["otc_miss_lots"] == 627)

# ---------------------------------------------------------------- 2 單位
print("\n2. 單位沒有弄錯")
# 維持率必須落在一個合理區間。單位錯一個數量級（仟元當成元、張當成股）
# 算出來會是 1,700% 或 0.17%，這個檢查就是專門擋那種錯的。
check("維持率在 100%~300% 之間", 100 < got["ratio"] < 300, "%s" % got["ratio"])
check("合併值介於兩個市場之間",
      min(got["twse_ratio"], got["otc_ratio"]) <= got["ratio"]
      <= max(got["twse_ratio"], got["otc_ratio"]))

# ---------------------------------------------------------------- 3 缺價補最後買價
print("\n3. 沒有收盤價時用同一天的最後揭示買價")
a, b, c, d = payloads()
rows = b["tables"][1]["data"]
for r in rows:
    if r[0] == "1101":
        r[8] = "--"            # 收盤價空掉，最後揭示買價 28.05 還在
got2 = margin.compute(a, b, c, d, DATE)
# 1101 從 28.10 換成 28.05 → 分子少 37,267×1000×0.05 = 1,863,350
check("改用最後買價，分子剛好少 1,863,350",
      got["numerator"] - got2["numerator"] == 1863350,
      "差 %s" % (got["numerator"] - got2["numerator"]))
check("1101 沒有被當成缺價", got2["_detail"]["twse_miss_lots"] == 37)

# ---------------------------------------------------------------- 4 缺價閘門
print("\n4. 缺價太多就整天不寫（不補估計值）")
a, b, c, d = payloads()
b["tables"][1]["data"] = [r for r in b["tables"][1]["data"] if r[0] != "2330"]
expect_raises("2330（35,219 張 = 40%）沒有價格 → MarginError",
              margin.MarginError, lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
d["tables"][0]["data"] = [r for r in d["tables"][0]["data"] if r[0] != "8932"]
expect_raises("上櫃 8932（28%）沒有價格 → MarginError",
              margin.MarginError, lambda: margin.compute(a, b, c, d, DATE))

# 剛好在門檻之下要過得去：4207 的 627 張是 0.2986%
a, b, c, d = payloads()
check("0.2986% 的缺價仍然算得出來",
      margin.compute(a, b, c, d, DATE)["otc_ratio"] == 170.71)

# ---------------------------------------------------------------- 5 張數自洽
print("\n5. 個股張數合計要等於表尾合計")
a, b, c, d = payloads()
a["tables"][1]["data"] = [r for r in a["tables"][1]["data"] if r[0] != "1101"]
expect_raises("漏掉一列個股 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
a["tables"][0]["data"][0][5] = "9,571,091"     # 表尾改回全市場的值
expect_raises("表尾和個股對不上 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
c["tables"][0]["summary"][0][6] = "2,308,916"
expect_raises("上櫃表尾對不上 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

# ---------------------------------------------------------------- 6 改版
print("\n6. 來源改版要吵出來，不能默默用錯欄位")
a, b, c, d = payloads()
a["tables"][1]["fields"][6] = "本日餘額"
expect_raises("證交所個股表頭改了 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
b["tables"][1]["fields"][8] = "收盤"
expect_raises("證交所收盤欄位改名 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
b["tables"][1]["title"] = "115年11月02日 盤後行情"
expect_raises("找不到「每日收盤行情」那張表 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
c["tables"][0]["fields"][6] = "融資餘額"
expect_raises("櫃買表頭改了 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
c["tables"][0]["summary"] = []
expect_raises("櫃買 summary 不見了 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
d["tables"][0]["fields"][11] = "最佳買價"
expect_raises("櫃買最後買價改名 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

# ---------------------------------------------------------------- 7 日期
print("\n7. 回傳的日期一定要是我們要的那一天")
a, b, c, d = payloads()
expect_raises("證交所回別的日期 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, "2026/10/31"))

a, b, c, d = payloads()
c["tables"][0]["date"] = "115/10/30"           # 頂層 date 對、表上的日期不對
expect_raises("櫃買表上的民國日期不對 → MarginError", margin.MarginError,
              lambda: margin.compute(a, b, c, d, DATE))

check("民國轉西元 115/11/02 → 2026/11/02",
      margin._roc_to_iso("115/11/02") == "2026/11/02")
check("已經是西元就不動", margin._roc_to_iso("2026/11/02") == "2026/11/02")

# ---------------------------------------------------------------- 8 非交易日
print("\n8. 非交易日是「尚未公布」，不是錯誤")
a, b, c, d = payloads()
a["stat"] = "很抱歉，沒有符合條件的資料"
a["tables"] = []
expect_raises("證交所 stat 不是 OK → NoMarginForDate", margin.NoMarginForDate,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
b["stat"] = "很抱歉，沒有符合條件的資料!"
b["tables"] = []
expect_raises("證交所收盤行情沒資料 → NoMarginForDate", margin.NoMarginForDate,
              lambda: margin.compute(a, b, c, d, DATE))

# 櫃買在非交易日回的是 stat:"ok" 但 data 空的 —— 只看 stat 會以為有資料
a, b, c, d = payloads()
c["tables"][0]["data"] = []
check("櫃買非交易日時 stat 還是 ok", c["stat"] == "ok")
expect_raises("櫃買 data 空的 → NoMarginForDate", margin.NoMarginForDate,
              lambda: margin.compute(a, b, c, d, DATE))

a, b, c, d = payloads()
for t in d["tables"]:
    t["data"] = []
expect_raises("櫃買行情 data 空的 → NoMarginForDate", margin.NoMarginForDate,
              lambda: margin.compute(a, b, c, d, DATE))

# ---------------------------------------------------------------- 9 _num
print("\n9. 數字解析")
check("千分位", margin._num("1,069,200") == 1069200)
check("小數", margin._num("2,330.00") == 2330.0)
for junk in ("", " ", "--", "-", "X"):
    check("「%s」→ None" % junk, margin._num(junk) is None)
check("None → None", margin._num(None) is None)

# ---------------------------------------------------------------- 10 端點
print("\n10. 端點設定")
check("證交所收盤行情用 ALLBUT0999（不是 ALL，小 22 倍且結果相同）",
      "type=ALLBUT0999" in margin.TWSE_QUOTES)
check("缺價上限 0.5%", margin.MISSING_LOTS_MAX == 0.005)
src = open(os.path.join(os.path.dirname(HERE), "margin.py"), encoding="utf-8").read()
check("每個 urlopen 都帶 context=（憑證設定不可以漏掉）",
      src.count("urlopen(") == src.count("context=netssl.context()"),
      "urlopen %d 次、context %d 次"
      % (src.count("urlopen("), src.count("context=netssl.context()")))

# ---------------------------------------------------------------- 11 store
print("\n11. 歷史檔讀寫")
import tempfile  # noqa: E402
import store  # noqa: E402

tmp = tempfile.mkdtemp()
orig = store.MARGIN_CSV
store.MARGIN_CSV = os.path.join(tmp, "margin.csv")
try:
    row = {c: got[c] for c in store.MARGIN_COLS}
    check("第一次寫入 → added 1", store.upsert_margin([row]) == (1, 0, 0))
    check("同一天再寫 → skipped（冪等）", store.upsert_margin([row]) == (0, 0, 1))
    back = store.read_margin()
    check("讀回來一筆", len(back) == 1)
    check("ratio 是 float 且相等", back[0]["ratio"] == EXPECT["ratio"])
    check("numerator 是 int", isinstance(back[0]["numerator"], int))
    check("latest_margin_date", store.latest_margin_date() == DATE)
    row2 = dict(row); row2["ratio"] = 165.0
    check("--force 才會覆寫", store.upsert_margin([row2], force=True) == (0, 1, 0))
    check("覆寫後讀到新值", store.read_margin()[0]["ratio"] == 165.0)
    check("日期永遠排序",
          [r["date"] for r in (lambda: (store.upsert_margin(
              [{**row, "date": "2026/10/30"}]), store.read_margin())[1])()]
          == ["2026/10/30", "2026/11/02"])
finally:
    store.MARGIN_CSV = orig

print("\n" + ("全部通過" if not fails else "有 %d 項失敗：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
