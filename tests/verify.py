#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
驗證：
 1. 用 Python 獨立算一次五票分數（兩種門檻），
 2. 用 headless Chromium 開實際頁面（ECharts 以 stub 取代，因離線環境無 CDN），
    讀出畫面上的表格與 setOption 內容，
 3. 兩邊逐格比對。
"""
import csv, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---------- 1. Python 端獨立計分 ----------
with open(os.path.join(ROOT, "data", "chips.csv"), encoding="utf-8") as f:
    chips = list(csv.DictReader(f))
for c in chips:
    for k in c:
        if k != "date":
            c[k] = int(c[k])

def score(c, thr):
    v = [1 if c["foreign_fut"] > thr else -1,
         1 if c["top10_trader"] > 0 else -1,
         1 if c["top10_specific"] > 0 else -1,
         1 if c["foreign_opt"] > 0 else -1,
         1 if c["dealer_opt"] > 0 else -1]
    return v, v[0]+v[1]+v[2], v[3]+v[4], sum(v)

exp = {}
for thr in (-83000, -80000):
    exp[thr] = [score(c, thr) for c in chips]

diff_days = [chips[i]["date"] for i in range(len(chips))
             if exp[-83000][i][3] != exp[-80000][i][3]]
print("Python 端：-83000 vs -80000 總分不同的天數 =", len(diff_days), diff_days)

with open(os.path.join(ROOT, "data", "prices.json"), encoding="utf-8") as f:
    prices = json.load(f)

# ---------- 2. 瀏覽器端 ----------
from playwright.sync_api import sync_playwright

STUB = """
window.__OPTS__ = [];
window.echarts = {
  init: function(){ return {
    setOption: function(o){ window.__OPTS__.push(o); },
    resize: function(){}
  };}
};
"""

errors = []
with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                           args=["--no-sandbox", "--allow-file-access-from-files"])
    pg = b.new_page()
    pg.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
    pg.on("console", lambda m: errors.append("console.%s: %s" % (m.type, m.text))
          if m.type == "error" else None)
    pg.add_init_script(STUB)
    pg.route("**cdnjs.cloudflare.com**", lambda r: r.abort())
    pg.goto("file://" + os.path.join(ROOT, "index.html"))
    pg.wait_for_selector("#dataTable tbody tr")

    def read_table():
        return pg.eval_on_selector_all(
            "#dataTable tbody tr",
            "rs => rs.map(r => Array.from(r.cells).map(c => c.textContent.trim()))")

    def set_thr(v):
        pg.eval_on_selector("#thrNum",
            "(el,v)=>{el.value=v; el.dispatchEvent(new Event('input',{bubbles:true}));}", str(v))
        pg.wait_for_timeout(120)

    results = {}
    for thr in (-83000, -80000):
        set_thr(thr)
        results[thr] = read_table()
        results[str(thr) + "_opt"] = pg.evaluate("JSON.parse(JSON.stringify(window.__OPTS__[window.__OPTS__.length-1]))")

    err_visible = pg.eval_on_selector("#loadErr", "e => !e.hidden")
    thr_note = pg.eval_on_selector("#thrNote", "e => e.textContent")
    stats = pg.eval_on_selector_all(".stat", "ns => ns.map(n => n.textContent.trim())")

    # 比較模式也切一次，確認不會爆
    pg.eval_on_selector("#compareChk", "e=>{e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}));}")
    pg.wait_for_timeout(150)
    cmp_opt = pg.evaluate("JSON.parse(JSON.stringify(window.__OPTS__[window.__OPTS__.length-1]))")
    b.close()

# ---------- 3. 比對 ----------
fails = []
if err_visible:
    fails.append("頁面顯示了 #loadErr 錯誤區塊")
# 測試時刻意 abort 掉 CDN 請求（離線環境），這條網路錯誤不算失敗
real_errors = [e for e in errors if "Failed to load resource" not in e]
if real_errors:
    fails.append("瀏覽器錯誤：%s" % real_errors[:5])

price_by_date = {r["date"]: r for r in prices["stocks"][0]["rows"]}

for thr in (-83000, -80000):
    tbl = results[thr]
    if len(tbl) != len(chips):
        fails.append("thr=%s 表格列數 %d != %d" % (thr, len(tbl), len(chips)))
        continue
    for i, row in enumerate(tbl):
        v, fut, opt, tot = exp[thr][i]
        d = chips[i]["date"]
        # 欄位：日期, 收盤, 漲跌幅, (數值,票)x5, 期貨小計, 選擇權小計, 總分
        if not row[0].startswith(d):
            fails.append("thr=%s 第%d列日期 %r != %s" % (thr, i, row[0], d))
        pr = price_by_date[d]
        want_close = "{:,.2f}".format(pr["close"])
        if row[1] != want_close:
            fails.append("thr=%s %s 收盤 %r != %r" % (thr, d, row[1], want_close))
        want_pct = pr["change"] / (pr["close"] - pr["change"]) * 100
        got_pct = float(row[2].replace("%", "").replace("+", ""))
        if abs(got_pct - want_pct) > 0.011:
            fails.append("thr=%s %s 漲跌幅 %r != %.2f%%" % (thr, d, row[2], want_pct))
        for k, f in enumerate(["foreign_fut", "top10_trader", "top10_specific",
                               "foreign_opt", "dealer_opt"]):
            gv = row[3 + k * 2].replace(",", "")
            if int(gv) != chips[i][f]:
                fails.append("thr=%s %s %s 數值 %s != %s" % (thr, d, f, gv, chips[i][f]))
            gvote = int(row[4 + k * 2])
            if gvote != v[k]:
                fails.append("thr=%s %s %s 票 %d != %d" % (thr, d, f, gvote, v[k]))
        if int(row[13]) != fut:
            fails.append("thr=%s %s 期貨小計 %s != %d" % (thr, d, row[13], fut))
        if int(row[14]) != opt:
            fails.append("thr=%s %s 選擇權小計 %s != %d" % (thr, d, row[14], opt))
        if int(row[15]) != tot:
            fails.append("thr=%s %s 總分 %s != %d" % (thr, d, row[15], tot))

    # 圖上的總分序列
    o = results[str(thr) + "_opt"]
    sser = [s for s in o["series"] if s.get("name") == "五票總分"][0]
    if sser["data"] != [e[3] for e in exp[thr]]:
        fails.append("thr=%s 圖表總分序列與預期不符" % thr)
    if sser.get("step") != "middle":
        fails.append("總分不是 step line")
    ya = o["yAxis"][1]
    if (ya.get("min"), ya.get("max")) != (-5, 5):
        fails.append("總分軸範圍不是 -5~5")
    # markArea 區段數
    runs, i2 = 0, 0
    tots = [e[3] for e in exp[thr]]
    while i2 < len(tots):
        if tots[i2] == 0:
            i2 += 1; continue
        j = i2
        while j + 1 < len(tots) and (tots[j+1] > 0) == (tots[i2] > 0) and tots[j+1] != 0:
            j += 1
        runs += 1; i2 = j + 1
    if len(sser["markArea"]["data"]) != runs:
        fails.append("thr=%s markArea 區段數 %d != %d" % (thr, len(sser["markArea"]["data"]), runs))
    ml = sser["markLine"]["data"]
    if not any(m.get("yAxis") == 0 for m in ml):
        fails.append("缺少 y=0 分界線")
    if not any(m.get("xAxis") == "2026/08/19" for m in ml):
        fails.append("缺少 2026/08/19 結算日標記線")
    # K 棒
    ck = [s for s in o["series"] if s.get("type") == "candlestick"]
    if len(ck) != 1:
        fails.append("主圖沒有唯一的 K 棒序列")
    else:
        if ck[0]["itemStyle"]["color"].lower() != "#d93b30":
            fails.append("漲的顏色不是紅色")
        if ck[0]["itemStyle"]["color0"].lower() != "#12996b":
            fails.append("跌的顏色不是綠色")
        first = ck[0]["data"][0]
        p0 = prices["stocks"][0]["rows"][0]
        if first != [p0["open"], p0["close"], p0["low"], p0["high"]]:
            fails.append("K 棒 OHLC 順序不對：%s" % first)
    # 副圖：foreign_fut 獨立 y 軸
    chip_s = {s["name"]: s for s in o["series"] if s.get("xAxisIndex") == 1}
    if len(chip_s) != 5:
        fails.append("副圖籌碼線數量 %d != 5" % len(chip_s))
    fa = chip_s.get("外資台指期未平倉", {}).get("yAxisIndex")
    others = set(s["yAxisIndex"] for n, s in chip_s.items() if n != "外資台指期未平倉")
    if fa is None or others != {2} or fa != 3:
        fails.append("foreign_fut 沒有用獨立 y 軸（fa=%s others=%s）" % (fa, others))

# 門檻差異天數
m = re.search(r"總分不同的有\s*(\d+)\s*天", thr_note)
if not m or int(m.group(1)) != len(diff_days):
    fails.append("門檻比較說明的天數與 Python 算的不符：%r" % thr_note)
for d in diff_days:
    if d not in thr_note:
        fails.append("門檻比較說明沒列出 %s" % d)

if cmp_opt is None or not any(s.get("type") == "line" and s.get("yAxisIndex") == 0
                              for s in cmp_opt["series"]):
    fails.append("比較模式沒有產生指數化折線")
elif len([s for s in cmp_opt["series"] if s.get("yAxisIndex") == 0 and s.get("xAxisIndex") == 0]) != 3:
    fails.append("比較模式的折線不是 3 條")

print("\n統計卡：", stats)
print("門檻說明：", thr_note.strip()[:160])
print()
if fails:
    print("❌ 失敗 %d 項：" % len(fails))
    for f in fails[:40]:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通過：21 天 × 2 門檻 × 16 欄，表格 / 圖表序列 / markArea / markLine / 顏色 / 軸設定 皆與 Python 獨立計算一致")
