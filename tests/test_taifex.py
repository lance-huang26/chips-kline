#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期交所解析器的回歸測試。

fixture 是 2026/08/19 與 08/31 的真實回應，期望值是專案原本手工維護的 chips.csv。
期交所改版時，這裡會先紅，而不是等到資料已經寫髒了才發現。

跑法：python3 tests/test_taifex.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import taifex  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "taifex_202608.json")
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
        fails.append("%s：丟出的是 %s（%s），不是 %s" % (msg, type(e).__name__, e, exc.__name__))
        return
    fails.append("%s：什麼都沒丟出來" % msg)


# ---------------------------------------------------------------- 1. 五欄解析
with open(FIX, encoding="utf-8") as f:
    fixtures = json.load(f)

for date in sorted(fixtures):
    fx = fixtures[date]
    want = fx["expect"]

    got_fut = taifex.parse_fut(fx["fut_rows"], date)
    check(got_fut == want["foreign_fut"],
          "%s foreign_fut %s != %s" % (date, got_fut, want["foreign_fut"]))

    got_opt = taifex.parse_opt(fx["opt_rows"], date)
    for k in ("foreign_opt", "dealer_opt"):
        check(got_opt[k] == want[k], "%s %s %s != %s" % (date, k, got_opt[k], want[k]))

    got_lg = taifex.parse_large(fx["large_csv"], date)
    for k in ("top10_trader", "top10_specific"):
        check(got_lg[k] == want[k], "%s %s %s != %s" % (date, k, got_lg[k], want[k]))

    print("  %s  五欄全對：%s" % (date, ", ".join(
        "%s=%s" % (k, want[k]) for k in taifex.CHIP_FIELDS)))

# 選擇權是「買權淨額 − 賣權淨額」，把中間值也釘住，避免哪天被改成相加
d = "2026/08/31"
detail = taifex.parse_opt(fixtures[d]["opt_rows"], d)["_detail"]
check(detail["外資_買權"] == -4183 and detail["外資_賣權"] == 335,
      "%s 外資買賣權拆解不對：%s" % (d, detail))
check(detail["自營商_買權"] == 5957 and detail["自營商_賣權"] == 771,
      "%s 自營商買賣權拆解不對：%s" % (d, detail))
print("  選擇權拆解：外資 買權 -4,183 − 賣權 335 = -4,518；自營商 5,957 − 771 = 5,186")

# ---------------------------------------------------------------- 2. HTML 抽列
HTML = """
<table>
 <tr><th>序號</th><th>商品名稱</th><th>身份別</th><th>口數</th></tr>
 <tr><td>1</td><td rowspan="2">臺股期貨</td><td>自營商</td><td>1,609</td></tr>
 <tr><td>外資</td><td>-82,970</td></tr>
 <tr><td colspan="2">合計</td></tr>
</table>
"""
rows = taifex.table_rows(HTML)
check(rows[0] == ["序號", "商品名稱", "身份別", "口數"], "表頭抽取不對：%s" % rows[0])
check(rows[1] == ["1", "臺股期貨", "自營商", "1,609"], "第一列抽取不對：%s" % rows[1])
check(rows[2] == ["外資", "-82,970"], "rowspan 後的短列抽取不對：%s" % rows[2])
print("  HTML 抽列：rowspan 造成的短列有正確保留")

# ---------------------------------------------------------------- 3. 失敗要吵
# 非交易日：0 筆資料列 → NoDataForDate（正常，不算失敗）
expect_raises(taifex.NoDataForDate, lambda: taifex.parse_fut([], "2026/08/22"),
              "空表格應該當成『該日無資料』")
expect_raises(taifex.NoDataForDate, lambda: taifex.parse_opt([], "2026/08/22"),
              "空選擇權表格應該當成『該日無資料』")

# 有資料列但找不到臺股期貨 → TaifexError（改版了，要吵）
renamed = [r[:] for r in fixtures[d]["fut_rows"]]
for r in renamed:
    if len(r) >= 15 and r[1] == "臺股期貨":
        r[1] = "臺股期貨(新)"
expect_raises(taifex.TaifexError, lambda: taifex.parse_fut(renamed, d),
              "商品名稱改掉之後應該報錯，而不是回 0")

# 欄數變了 → 要吵
widened = [r[:] for r in fixtures[d]["fut_rows"]]
for r in widened:
    if len(r) >= 13 and r[-13] in taifex.IDENTITIES and len(r) == 15:
        r.insert(0, "多的一欄")
expect_raises(taifex.TaifexError, lambda: taifex.parse_fut(widened, d),
              "表格多一欄之後應該報錯")

# 大額交易人拿到錯誤頁（HTTP 200 但內容是 HTML）→ 要吵
expect_raises(taifex.TaifexError,
              lambda: taifex.parse_large("<!DOCTYPE HTML><html><body>...</body></html>", d),
              "大額交易人拿到 HTML 錯誤頁應該報錯")

# 大額交易人回傳的日期不對 → 要吵（絕不能寫進歷史檔）
wrong = fixtures["2026/08/19"]["large_csv"]
expect_raises(taifex.TaifexError, lambda: taifex.parse_large(wrong, "2026/08/31"),
              "CSV 日期與要求日期不符時應該報錯")

# 頁面印的日期不對 → 要吵
expect_raises(taifex.TaifexError,
              lambda: taifex._check_page_date("<p>日期 2026/08/19</p>", "2026/08/31", "測試"),
              "頁面日期與要求日期不符時應該報錯")

check(taifex.page_data_date("<td>日期</td><td>2026/08/31</td>") == "2026/08/31",
      "頁面日期抓不出來")
print("  失敗路徑：空表→無資料、改版→報錯、日期不符→報錯，全部符合預期")

# ---------------------------------------------------------------- 結果
print()
if fails:
    print("❌ 失敗 %d 項：" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 期交所解析器全部通過（2 個日期 × 5 欄，加 HTML 抽列與 7 條失敗路徑）")
