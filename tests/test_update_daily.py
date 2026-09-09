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
    update_daily._PRICE_CACHE.clear()
    return update_daily.one_day(NEW, cfg, **kw)


# ---------------------------------------------------------------- 參數解析
# workflow 的輸入欄沒填就是空字串。以前 --to 空著會直接把空字串丟給 argparse 而爆掉。
cd = update_daily.choose_dates
T = "2026/09/09"                     # 星期三
check(cd(today=T) == [T], "沒給參數應該就是今天")
check(cd(date="2026/09/07", today=T) == ["2026/09/07"], "--date 應該只處理那一天")
check(cd(frm="2026/09/07", to="", today=T) == ["2026/09/07", "2026/09/08", "2026/09/09"],
      "--to 留空應該等於今天，實得 %s" % cd(frm="2026/09/07", to="", today=T))
check(cd(frm="2026/09/07", to=None, today=T) == cd(frm="2026/09/07", to="", today=T),
      "--to 給 None 與給空字串要一致")
check(cd(days=1, today=T) == [T], "--days 1 應該只有今天")
check(len(cd(days=60, today=T)) == 43,
      "--days 60 應該是 43 個工作日，實得 %d" % len(cd(days=60, today=T)))
check(cd(days=7, today=T)[0] == "2026/09/03" and cd(days=7, today=T)[-1] == T,
      "--days 7 的區間不對：%s" % cd(days=7, today=T))
# 週末要被跳掉
check("2026/09/05" not in cd(days=7, today=T) and "2026/09/06" not in cd(days=7, today=T),
      "週末不該出現在待處理清單裡")
# --from 優先於 --days
check(cd(frm="2026/09/08", days=60, today=T) == ["2026/09/08", "2026/09/09"],
      "--from 應該優先於 --days")
try:
    cd(to="2026/09/08", today=T)
    fails.append("只給 --to 應該報錯")
except ValueError:
    pass
try:
    cd(days=0, today=T)
    fails.append("--days 0 應該報錯")
except ValueError:
    pass
print("  參數解析：--to 留空 = 今天、--days N、--from 優先、只給 --to 會報錯")


# ---------------------------------------------------------------- 股價快取
# STOCK_DAY 一次回一整月，回填時同一個月份不該重複抓
calls = []


def counting(code, month, retries=3):
    calls.append((code, month))
    return {"stat": "OK", "title": "t", "data": [
        ["115/09/%02d" % d, "1", "2", "1.00", "1.00", "1.00", "1.00", "0.00", "1", ""]
        for d in range(1, 11)]}


update_daily._PRICE_CACHE.clear()
fetch_prices.fetch_raw = counting
codes = [s["code"] for s in cfg["stocks"]]
for day in ("2026/09/01", "2026/09/02", "2026/09/03"):
    got, why = update_daily.prices_for_date(day, codes)
    check(got is not None, "%s 應該抓得到股價（%s）" % (day, why))
check(len(calls) == len(codes),
      "三天只該抓 %d 次（每檔一個月份），實得 %d 次" % (len(codes), len(calls)))
check(len(set(calls)) == len(calls), "快取失效，同一個 (股票, 月份) 被抓了不只一次")
print("  股價快取：3 個日期共用 1 次月檔請求／檔（回填 60 天可省下約 120 次請求）")
update_daily._PRICE_CACHE.clear()


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
