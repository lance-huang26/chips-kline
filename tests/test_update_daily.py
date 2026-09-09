#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日更新流程的行為測試（不連網，用假的抓取層）。

驗的是這些性質，而不是數字：
  * 冪等：已存在的日期不重抓也不改檔
  * 五欄同進退：股價還沒公布時，籌碼也不能先寫進去
  * 尚未公布 / 非交易日 = 正常，不是失敗
  * 解析失敗 = 失敗，而且歷史檔一個位元組都不能動

作法是把整個專案複製到暫存目錄再跑，所以不會弄髒真的歷史檔。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


work = tempfile.mkdtemp(prefix="chips-kline-test-")
proj = os.path.join(work, "proj")
shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns(
    "dist", "__pycache__", ".git", "node_modules"))

sys.path.insert(0, proj)
for m in ("store", "taifex", "fetch_prices", "build_site", "update_daily"):
    sys.modules.pop(m, None)
import store          # noqa: E402
import taifex         # noqa: E402
import fetch_prices   # noqa: E402
import update_daily   # noqa: E402

check(os.path.dirname(os.path.abspath(store.__file__)) == proj,
      "測試沒有載到暫存目錄的模組，會弄髒真的歷史檔——中止")
if fails:
    print("❌", fails[0]); sys.exit(1)

cfg = update_daily.build_site.load_config()
NEW = "2026/09/01"
CHIP = {"date": NEW, "foreign_fut": -81000, "top10_trader": 500,
        "top10_specific": -300, "foreign_opt": 120, "dealer_opt": -80,
        "_raw": {"fut": "<table></table>", "opt": "<table></table>",
                 "large": "日期,x\n%s,TX" % NEW},
        "_detail": {}}


def fake_stock_day(code, month, retries=3):
    return {"stat": "OK", "title": "115年09月 %s 測試" % code,
            "data": [["115/09/01", "1,000", "2,000", "100.00", "110.00",
                      "90.00", "105.00", "+5.00", "50", ""]]}


def snapshot():
    return (open(store.CHIPS_CSV, encoding="utf-8").read(),
            open(store.PRICES_CSV, encoding="utf-8").read())


def run(chip_fn, price_fn=fake_stock_day, **kw):
    taifex.fetch_day = chip_fn
    fetch_prices.fetch_raw = price_fn
    return update_daily.one_day(NEW, cfg, **kw)


# 1) 已存在的日期：不重抓、不改檔
def boom(*a, **k):
    raise AssertionError("已存在的日期不應該再去抓")


before = snapshot()
taifex.fetch_day = boom
fetch_prices.fetch_raw = boom
old_status = update_daily.one_day("2026/08/31", cfg)
check(old_status[0] == "skipped", "已存在的日期應該 skipped，實得 %s" % (old_status,))
check(snapshot() == before, "skipped 時歷史檔不該被改動")

# 2) 籌碼尚未公布 → pending，不是失敗
def no_chip(date, **k):
    raise taifex.NoDataForDate("尚未公布")


status, msg = run(no_chip)
check(status == "pending", "籌碼未公布應該是 pending，實得 %r" % status)
check(snapshot() == before, "pending 時歷史檔不該被改動")

# 3) 籌碼有了但股價還沒 → 仍然 pending，籌碼不可以先寫進去
def no_price(code, month, retries=3):
    return {"stat": "OK", "title": "t", "data": [
        ["115/08/31", "1", "2", "1.00", "1.00", "1.00", "1.00", "0.00", "1", ""]]}


status, msg = run(lambda date, **k: dict(CHIP), no_price)
check(status == "pending", "股價未公布應該是 pending，實得 %r" % status)
check(snapshot() == before, "股價缺的時候籌碼不可以先寫入——這是半套資料")

# 4) 解析失敗 → 例外，歷史檔不動
def broken(date, **k):
    raise taifex.TaifexError("表格改版了")


try:
    run(broken)
    fails.append("解析失敗應該往上丟例外")
except taifex.TaifexError:
    pass
check(snapshot() == before, "解析失敗時歷史檔不該被改動")

# 5) 正常寫入
n_chips_before = len(store.read_chips())
n_prices_before = len(store.read_prices())
status, msg = run(lambda date, **k: dict(CHIP))
check(status == "written", "正常情況應該 written，實得 %r（%s）" % (status, msg))
chips = store.read_chips()
prices = store.read_prices()
check(len(chips) == n_chips_before + 1, "籌碼應該多一筆")
check(len(prices) == n_prices_before + len(cfg["stocks"]),
      "股價應該多 %d 筆" % len(cfg["stocks"]))
row = [c for c in chips if c["date"] == NEW]
check(len(row) == 1 and row[0]["foreign_fut"] == -81000, "寫入的籌碼數值不對：%s" % row)
check(chips == sorted(chips, key=lambda r: r["date"]), "歷史檔必須維持日期排序")
evid = os.path.join(update_daily.EVID_DIR, "2026-09-01.txt")
check(os.path.exists(evid), "取數依據檔沒有產生：%s" % evid)

# 6) 再跑一次同一天 → skipped，內容完全不變
after_first = snapshot()
status, _ = run(boom, boom)
check(status == "skipped", "重跑應該 skipped，實得 %r" % status)
check(snapshot() == after_first, "重跑不該改變任何內容（冪等）")

# 7) build_site 能吃新資料
import build_site  # noqa: E402
site = build_site.build(quiet=True)
check(site["chips"][-1]["date"] == NEW, "site.json 應該含有新的一天")
check(NEW in [r["date"] for r in site["stocks"][0]["rows"]],
      "site.json 的股價應該含有新的一天")

shutil.rmtree(work, ignore_errors=True)

print()
if fails:
    print("❌ 失敗 %d 項：" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 每日更新流程通過："
      "冪等、五欄同進退、尚未公布不算失敗、解析失敗不動歷史檔、寫入後 build 正常")
