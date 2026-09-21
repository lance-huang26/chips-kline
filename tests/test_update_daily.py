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
import re
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
import taifut         # noqa: E402
import update_daily   # noqa: E402

check(os.path.dirname(os.path.abspath(store.__file__)) == proj,
      "測試沒有載到暫存目錄的模組，會弄髒真的歷史檔——中止")
if fails:
    print("❌", fails[0]); sys.exit(1)

cfg = update_daily.build_site.load_config()

# 「新的一天」要從現有歷史推出來，不能寫死日期——
# 歷史每天都在長，寫死的話總有一天會撞到已經存在的日子，測試就會莫名其妙壞掉。
import datetime as _dt
_last = store.read_chips()[-1]["date"]
_d = _dt.date(int(_last[:4]), int(_last[5:7]), int(_last[8:10])) + _dt.timedelta(days=1)
while _d.weekday() >= 5:                      # 跳過週末
    _d += _dt.timedelta(days=1)
NEW = _d.strftime("%Y/%m/%d")
NEW_ROC = "%d/%02d/%02d" % (_d.year - 1911, _d.month, _d.day)

CHIP = {"date": NEW, "foreign_fut": -81000, "top10_trader": 500,
        "top10_specific": -300,
        "top10_trader_front": -120, "top10_specific_front": -900,
        "foreign_opt": 120, "dealer_opt": -80,
        "_raw": {"fut": "<table></table>", "opt": "<table></table>",
                 "large": "日期,x\n%s,TX" % NEW},
        "_detail": {}}


# 台指期走另一條路（期交所 CSV），測試也要蓋到，否則會真的連網
_TX_HEAD = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,"
            "收盤價,漲跌價,漲跌%,成交量,交易時段")


def fake_tx_month(month, retries=3):
    y, m = int(month[:4]), int(month[4:])
    import calendar as _cal
    rows = [_TX_HEAD]
    for day in range(1, _cal.monthrange(y, m)[1] + 1):
        d = "%04d/%02d/%02d" % (y, m, day)
        rows.append("%s,TX,%s,47000,47500,46800,47100,50,0.11%%,50000,一般" % (d, month))
        rows.append("%s,TX,%s,47050,47600,46900,47200,60,0.13%%,20000,盤後" % (d, month))
    return "\n".join(rows) + "\n"


def fake_tx_empty(month, retries=3):
    return _TX_HEAD + "\n"          # 該月份還沒有資料


def fake_stock_day(code, month, retries=3):
    return {"stat": "OK", "title": "%s 測試" % code,
            "data": [[NEW_ROC, "1,000", "2,000", "100.00", "110.00",
                      "90.00", "105.00", "+5.00", "50", ""]]}


def snapshot():
    return (open(store.CHIPS_CSV, encoding="utf-8").read(),
            open(store.PRICES_CSV, encoding="utf-8").read())


def run(chip_fn, price_fn=fake_stock_day, tx_fn=fake_tx_month, **kw):
    taifex.fetch_day = chip_fn
    fetch_prices.fetch_raw = price_fn
    taifut.fetch_month = tx_fn
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


_cache_days = [_d + _dt.timedelta(days=k) for k in range(3)]
_cache_dates = [x.strftime("%Y/%m/%d") for x in _cache_days]


def counting(code, month, retries=3):
    calls.append((code, month))
    return {"stat": "OK", "title": "t", "data": [
        ["%d/%02d/%02d" % (x.year - 1911, x.month, x.day),
         "1", "2", "1.00", "1.00", "1.00", "1.00", "0.00", "1", ""]
        for x in _cache_days]}


tx_calls = []


def counting_tx(month, retries=3):
    tx_calls.append(month)
    return fake_tx_month(month)


update_daily._PRICE_CACHE.clear()
fetch_prices.fetch_raw = counting
taifut.fetch_month = counting_tx
codes = [s["code"] for s in cfg["stocks"]]
twse_codes = [c for c in codes if fetch_prices.source_of(c, cfg) == "twse"]
tx_codes = [c for c in codes if fetch_prices.source_of(c, cfg) == "taifex"]
for day in _cache_dates:
    got, why = update_daily.prices_for_date(day, codes)
    check(got is not None, "%s 應該抓得到股價（%s）" % (day, why))
_months = {d[:4] + d[5:7] for d in _cache_dates}
check(len(calls) == len(twse_codes) * len(_months),
      "證交所：%d 天只該抓 %d 次（每檔每個月份一次），實得 %d 次"
      % (len(_cache_dates), len(twse_codes) * len(_months), len(calls)))
check(len(tx_calls) == len(tx_codes) * len(_months),
      "期交所：%d 天只該抓 %d 次，實得 %d 次"
      % (len(_cache_dates), len(tx_codes) * len(_months), len(tx_calls)))
check(len(set(calls)) == len(calls), "快取失效，同一個 (股票, 月份) 被抓了不只一次")
check(len(set(tx_calls)) == len(tx_calls), "台指期的月檔快取失效")
print("  股價快取：%d 個日期共用 %d 次證交所 + %d 次期交所月檔請求"
      % (len(_cache_dates), len(calls), len(tx_calls)))
update_daily._PRICE_CACHE.clear()


# 1) 已存在的日期：不重抓、不改檔
def boom(*a, **k):
    raise AssertionError("已存在的日期不應該再去抓")


before = snapshot()
taifex.fetch_day = boom
fetch_prices.fetch_raw = boom
taifut.fetch_month = boom
# 「完整的一天」= 籌碼有了、而且每個標的都有股價。只有這種日子才該完全不連外。
# （某個標的缺股價的日子會走補齊路徑，那是 7b 測的。）
_complete_codes = {r["code"] for r in store.read_prices() if r["date"] == _last}
_cfg_last = {"stocks": [x for x in cfg["stocks"] if x["code"] in _complete_codes]}
check(_cfg_last["stocks"], "%s 沒有任何標的的股價，測試前提不成立" % _last)
old_status = update_daily.one_day(_last, _cfg_last)   # 歷史最後一天，一定已存在
check(old_status[0] == "skipped",
      "完整的一天 %s 應該 skipped，實得 %s" % (_last, old_status))
check(snapshot() == before, "skipped 時歷史檔不該被改動")

# 2) 籌碼尚未公布 → pending，不是失敗
def no_chip(date, **k):
    raise taifex.NoDataForDate("尚未公布")


status, msg = run(no_chip)
check(status == "pending", "籌碼未公布應該是 pending，實得 %r" % status)
check(snapshot() == before, "pending 時歷史檔不該被改動")

# 3) 籌碼有了但股價還沒 → 仍然 pending，籌碼不可以先寫進去
def no_price(code, month, retries=3):
    # 回傳的月檔裡就是沒有 NEW 那天
    return {"stat": "OK", "title": "t", "data": []}


status, msg = run(lambda date, **k: dict(CHIP), no_price, fake_tx_empty)
check(status == "pending", "股價未公布應該是 pending，實得 %r" % status)

# 3b) 只有台指期還沒公布（個股都有了）→ 也要是 pending，不是失敗。
#     期交所的期貨行情如果那天還沒出來，整個 workflow 不該紅。
try:
    status, msg = run(lambda date, **k: dict(CHIP), fake_stock_day, fake_tx_empty)
    check(status == "pending",
          "只有台指期未公布時應該是 pending，實得 %r（%s）" % (status, msg))
except Exception as e:
    fails.append("只有台指期未公布時不該丟例外，卻丟出 %s：%s" % (type(e).__name__, e))
check(snapshot() == before, "台指期缺的時候其他標的也不可以先寫入")
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
evid = os.path.join(update_daily.EVID_DIR, NEW.replace("/", "-") + ".txt")
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

# 7b) 新增標的之後的自我補齊。
#     這是 2026/09 真的踩到的 bug：one_day 只看「這天有沒有籌碼」就決定 skip，
#     所以新加的標的在所有既有籌碼日永遠補不到股價，歷史斷在加標的的那天。
#     現在 skip 的條件是「籌碼有了 **而且** 每個標的都有股價」。
#     作法：把某個已存在的 (日期, 標的) 股價挖掉，看它會不會自己補回來。
_have = {}
for _r in store.read_prices():
    _have.setdefault(_r["date"], set()).add(_r["code"])
_chip_dates = [c["date"] for c in store.read_chips()]
_hole_date = next((d for d in _chip_dates if _have.get(d)), None)
check(_hole_date is not None, "找不到任何『有籌碼也有股價』的日子，測試前提不成立")

if _hole_date:
    # 只拿「那天本來就有股價」的標的來測，測試才不會被 repo 目前補到哪裡影響
    _cfg2 = {"stocks": [x for x in cfg["stocks"] if x["code"] in _have[_hole_date]]}
    _hole_code = sorted(_have[_hole_date])[0]
    _hole_src = fetch_prices.source_of(_hole_code, cfg)
    _kept = [r for r in store.read_prices()
             if not (r["date"] == _hole_date and r["code"] == _hole_code)]
    _rows = []
    for _r in _kept:
        _o = dict(_r)
        for _c in ("open", "high", "low", "close", "change", "volume"):
            _o[_c] = "" if _o[_c] is None else store._num(_o[_c])
        _rows.append(_o)
    store._write_atomic(store.PRICES_CSV, store.PRICE_COLS, _rows)

    _roc = "%d/%s/%s" % (int(_hole_date[:4]) - 1911, _hole_date[5:7], _hole_date[8:10])

    def _hole_stock_day(code, month, retries=3):
        return {"stat": "OK", "title": "t",
                "data": [[_roc, "1,000", "2,000", "100.00", "110.00", "90.00",
                          "105.00", "+5.00", "50", ""]]}

    def _hole_tx(month, retries=3):
        return "%s\n%s,TX,%s,47000,47500,46800,47100,50,0.11%%,50000,一般\n" % (
            _TX_HEAD, _hole_date, month)

    taifex.fetch_day = boom            # 籌碼已經有了，絕對不可以再去抓
    fetch_prices.fetch_raw = _hole_stock_day
    taifut.fetch_month = _hole_tx
    update_daily._PRICE_CACHE.clear()
    _n_chips = len(store.read_chips())
    _st, _msg = update_daily.one_day(_hole_date, _cfg2)
    check(_st == "filled",
          "缺股價的既有籌碼日應該回 filled 去補，實得 %r（%s）" % (_st, _msg))
    _after = {(r["date"], r["code"]) for r in store.read_prices()}
    check((_hole_date, _hole_code) in _after,
          "%s 的 %s 股價應該被補回來" % (_hole_date, _hole_code))
    check(len(store.read_chips()) == _n_chips, "補股價不該動到籌碼")

    # 補齊之後再跑一次 → 這次才該是 skipped，而且完全不再連外
    update_daily._PRICE_CACHE.clear()
    fetch_prices.fetch_raw = boom
    taifut.fetch_month = boom
    _st2, _ = update_daily.one_day(_hole_date, _cfg2)
    check(_st2 == "skipped", "補齊之後應該 skipped，實得 %r" % _st2)
    print("  新標的自我補齊：既有籌碼日缺 %s 的股價 → filled 補上（不重抓籌碼），"
          "補完再跑才 skipped" % _hole_code)

# 8) config 新增了股票、但還沒補抓它的股價 → build_site 只能警告，不能中止。
#    workflow 是「先跑測試（會呼叫 build_site）再補抓股價」，
#    這裡如果中止，那些股價就永遠補不到了（死鎖）。
cfg_path = os.path.join(proj, "config.json")
with open(cfg_path, encoding="utf-8") as f:
    raw = json.load(f)
raw["stocks"].append({"code": "9999", "name": "測試新股"})
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False)
try:
    site2 = build_site.build(quiet=True)
    newbie = [s for s in site2["stocks"] if s["code"] == "9999"]
    check(len(newbie) == 1 and newbie[0]["rows"] == [],
          "新股票應該以空 rows 出現在 site.json，實得 %s" % newbie)
    check(len(site2["chips"]) > 0, "新增股票不該影響籌碼資料")
except Exception as e:
    fails.append("config 新增還沒補股價的股票時 build_site 不該中止，卻丟出 %s：%s"
                 % (type(e).__name__, e))
print("  新增股票：還沒補抓股價時 build_site 只警告不中止（不然 workflow 會死鎖）")

# 9) 排程改成回看一週（--days 7）而不是只抓當天，因為 GitHub 可能整天不派送排程，
#    只抓當天的話那天就永遠是個洞。這一條釘住「回看一週幾乎不花成本」——
#    已經完整的日子必須一個請求都不發，否則每天多打幾十次外部 API 就不划算了。
with open(cfg_path, "w", encoding="utf-8") as f:       # 先把 8) 加的測試股票拿掉
    raw["stocks"] = [s for s in raw["stocks"] if s["code"] != "9999"]
    json.dump(raw, f, ensure_ascii=False)
cfg9 = build_site.load_config()

_today = store.read_chips()[-1]["date"]
_week = update_daily.choose_dates(days=7, today=_today)
check(0 < len(_week) <= 7 and _week[-1] == _today,
      "--days 7 應該回最近 7 個日曆日裡的工作日、以今天結尾，實得 %s" % _week)
check(all(d.replace("/", "") <= _today.replace("/", "") for d in _week),
      "--days 7 不該產生未來的日期：%s" % _week)

# 先把這一週補成「完整」。不能假設真實歷史剛好是完整的——
# 這條測的是「完整 ⇒ 不連外」，不是「歷史目前完不完整」。
_have9 = {(r["date"], r["code"]) for r in store.read_prices()}
_chips9 = {r["date"] for r in store.read_chips()}
_fill = [{"date": d, "code": s["code"], "open": 100.0, "high": 110.0,
          "low": 90.0, "close": 105.0, "change": 5.0, "volume": 50}
         for d in _week if d in _chips9
         for s in cfg9["stocks"] if (d, s["code"]) not in _have9]
if _fill:
    store.upsert_prices(_fill)
_week = [d for d in _week if d in _chips9]      # 沒籌碼的日子（假日）本來就該去抓
check(_week, "這一週應該至少有一天是有籌碼的，否則這條測試沒測到東西")

taifex.fetch_day = boom            # 這一輪完全不可以連外
fetch_prices.fetch_raw = boom
taifut.fetch_month = boom
update_daily._PRICE_CACHE.clear()
before9 = snapshot()
_sweep = []
for _d9 in _week:
    try:
        _sweep.append(update_daily.one_day(_d9, cfg9)[0])
    except Exception as e:                                   # noqa: BLE001
        fails.append("完整的 %s 不該發出任何請求，卻打到抓取層：%s" % (_d9, e))
        _sweep.append("EXPLODED")
check(set(_sweep) <= {"skipped"},
      "歷史已經完整時，回看一週應該每天都是 skipped，實得 %s" % _sweep)
check(snapshot() == before9, "只是掃過已完整的日子，不該動到任何歷史檔")
print("  排程回看一週：%d 個工作日全部 skipped，對外請求 0 次（所以 --days 7 幾乎免費）"
      % len(_week))

# 10) 近月十大那兩欄是後來才加的。舊的 chips.csv 沒有這兩欄，必須：
#     讀得進來（而且是 None，不是 0——0 看起來太合理，混進去不可能被發現），
#     而且 patch_chips 只能動這兩欄，其他欄位一個位元組都不准改。
old_csv = ("date,foreign_fut,top10_trader,top10_specific,foreign_opt,dealer_opt\n"
           "2026/01/05,-80000,100,200,300,400\n"
           "2026/01/06,-81000,110,210,310,410\n")
with open(store.CHIPS_CSV, "w", encoding="utf-8") as f:
    f.write(old_csv)
old_rows = store.read_chips()
check(len(old_rows) == 2, "舊格式的 chips.csv 應該讀得進來，實得 %d 列" % len(old_rows))
check(old_rows[0]["top10_trader_front"] is None
      and old_rows[0]["top10_specific_front"] is None,
      "舊檔沒有的欄位應該是 None，不能默默變成 0，實得 %s" % old_rows[0])
check(old_rows[0]["foreign_fut"] == -80000 and old_rows[1]["dealer_opt"] == 410,
      "舊欄位的值不該被動到：%s" % old_rows)

changed, missing = store.patch_chips([
    {"date": "2026/01/05", "top10_trader_front": 549,
     "top10_specific_front": -1790},
    {"date": "2099/12/31", "top10_trader_front": 1},     # 不存在的日期
])
check((changed, missing) == (1, 1),
      "patch_chips 應該補 1 天、找不到 1 天，實得 %s" % ((changed, missing),))
after = {r["date"]: r for r in store.read_chips()}
check(after["2026/01/05"]["top10_trader_front"] == 549
      and after["2026/01/05"]["top10_specific_front"] == -1790,
      "近月欄位沒補上：%s" % after["2026/01/05"])
check(after["2026/01/05"]["foreign_fut"] == -80000
      and after["2026/01/05"]["top10_trader"] == 100,
      "patch_chips 不該動到其他欄位：%s" % after["2026/01/05"])
check(after["2026/01/06"]["top10_trader_front"] is None,
      "沒有 patch 到的那天應該維持空的：%s" % after["2026/01/06"])
_txt = open(store.CHIPS_CSV, encoding="utf-8").read()
check(_txt.splitlines()[-1].endswith(",,310,410"),
      "還沒補的那天，近月欄位應該是空字串而不是 0：%s" % _txt.splitlines()[-1])
print("  回補近月欄位：舊檔讀得進來（缺的是 None 不是 0），patch 只動那兩欄")

# 11) 排程的分鐘數不可以是 0。整點是 GitHub 最壅塞的一分鐘，
#     而且本 repo 實測延遲四到五小時，靠的就是「撒一排、分鐘錯開」去命中想要的時段。
#     這種意圖很容易在之後整理設定時被順手改回整點，所以釘住。
_wf = os.path.join(ROOT, ".github", "workflows", "daily.yml")
if os.path.exists(_wf):
    _crons = re.findall(r'cron:\s*"([^"]+)"', open(_wf, encoding="utf-8").read())
    check(len(_crons) >= 3, "排程至少要有 3 個 cron，實得 %d 個" % len(_crons))
    _oclock = [c for c in _crons if c.split()[0] in ("0", "00")]
    check(not _oclock, "這些 cron 落在整點：%s——整點最壅塞，分鐘數要錯開" % _oclock)
    _dup = len(_crons) != len(set(_crons))
    check(not _dup, "有重複的 cron：%s" % _crons)
    for _c in _crons:
        check(len(_c.split()) == 5, "cron 欄位數不對：%r" % _c)
    print("  排程：%d 個 cron，分鐘數全部避開整點" % len(_crons))

shutil.rmtree(work, ignore_errors=True)

print()
if fails:
    print("❌ 失敗 %d 項：" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 每日更新流程通過："
      "冪等、五欄同進退、尚未公布不算失敗、解析失敗不動歷史檔、寫入後 build 正常")
