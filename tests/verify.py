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
with open(os.path.join(ROOT, "data", "history", "chips.csv"), encoding="utf-8") as f:
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

with open(os.path.join(ROOT, "data", "site.json"), encoding="utf-8") as f:
    prices = json.load(f)          # 現在前端只讀 site.json

# ---------- 1b. Python 端獨立算 MA 與乖離率 ----------
MA_PERIOD = prices.get("maPeriod", 20)
BIAS_ALERT = 15.0

exp_bias = {}          # code -> {date: (ma, bias)}
for st in prices["stocks"]:
    # site.json 的 rows 已經含全部歷史（含均線暖身用的更早月份），不再有 warmup
    closes, byd = [], {}
    for r in st["rows"]:
        if not r["valid"]:
            continue
        closes.append(r["close"])
        if len(closes) < MA_PERIOD:
            byd[r["date"]] = (None, None)
        else:
            ma = sum(closes[-MA_PERIOD:]) / MA_PERIOD
            byd[r["date"]] = (ma, (r["close"] - ma) / ma * 100)
    exp_bias[st["code"]] = byd

_hot = [(c, d, b) for c, m in exp_bias.items() for d, (ma, b) in m.items()
        if b is not None and b > BIAS_ALERT]
print("Python 端：MA%d，正乖離 > %g%% 的有 %d 筆" % (MA_PERIOD, BIAS_ALERT, len(_hot)))
for c, d, b in sorted(_hot, key=lambda x: (x[0], x[1])):
    print("    %s %s  %+.2f%%" % (c, d, b))

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

    # 表單元件在「深色系統主題」下的可讀性（白字白底 bug 的回歸測試）
    CONTRAST_JS = """
    () => {
      function lum(c){
        const m = c.match(/[\\d.]+/g).map(Number);
        const f = m.slice(0,3).map(v => { v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); });
        return 0.2126*f[0] + 0.7152*f[1] + 0.0722*f[2];
      }
      function bgOf(el){
        for (let n = el; n; n = n.parentElement) {
          const b = getComputedStyle(n).backgroundColor;
          if (b && !/rgba\\(0, 0, 0, 0\\)|transparent/.test(b)) return b;
        }
        return 'rgb(255,255,255)';
      }
      return [...document.querySelectorAll('input:not([type=range]), button, select, textarea')]
        .map(el => {
          const fg = getComputedStyle(el).color, bg = bgOf(el);
          const a = lum(fg), b = lum(bg);
          return { sel: el.id || el.className || el.tagName,
                   fg, bg, ratio: +(((Math.max(a,b)+0.05)/(Math.min(a,b)+0.05)).toFixed(2)) };
        });
    }
    """
    contrast = {}
    for scheme in ("light", "dark"):
        pg.emulate_media(color_scheme=scheme)
        pg.wait_for_timeout(80)
        contrast[scheme] = pg.evaluate(CONTRAST_JS)
    pg.emulate_media(color_scheme="light")

    err_visible = pg.eval_on_selector("#loadErr", "e => !e.hidden")
    thr_note = pg.eval_on_selector("#thrNote", "e => e.textContent")
    stats = pg.eval_on_selector_all(".stat", "ns => ns.map(n => n.textContent.trim())")

    # 比較模式也切一次，確認不會爆
    pg.eval_on_selector("#compareChk", "e=>{e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}));}")
    pg.wait_for_timeout(150)
    cmp_opt = pg.evaluate("JSON.parse(JSON.stringify(window.__OPTS__[window.__OPTS__.length-1]))")

    # 關掉比較模式，改測「勾選狀態改變 + 非預設門檻」時副圖兩軸還對不對得齊
    pg.eval_on_selector("#compareChk", "e=>{e.checked=false;e.dispatchEvent(new Event('change',{bubbles:true}));}")
    toggle_cases = []
    for off, thr in ((["top10_trader", "top10_specific"], -86000),
                     (["foreign_opt", "dealer_opt", "top10_specific"], -79000),
                     ([], -90500)):
        pg.evaluate(
            """(names) => {
                 const labs = [...document.querySelectorAll('#chipChks label')];
                 const order = ['外資台指期未平倉','前十大交易人','前十大特定人','選擇權外資淨口數','選擇權自營商淨口數'];
                 const keys  = ['foreign_fut','top10_trader','top10_specific','foreign_opt','dealer_opt'];
                 labs.forEach((l,i) => {
                   const cb = l.querySelector('input');
                   const want = !names.includes(keys[i]);
                   if (cb.checked !== want) { cb.checked = want; cb.dispatchEvent(new Event('change',{bubbles:true})); }
                 });
               }""", off)
        set_thr(thr)
        toggle_cases.append((off, thr,
            pg.evaluate("JSON.parse(JSON.stringify(window.__OPTS__[window.__OPTS__.length-1]))")))

    # 區間切換：切到「最近 20 日」應該只剩 20 列，
    # 而且第一列的「本日操作」不能是 —（要跟歷史上的前一天比，不是跟視窗第一天比）
    win_cases = {}
    for n in (20, 0):
        pg.evaluate("""(n) => {
              const b = [...document.querySelectorAll('#winSeg button')]
                        .find(x => Number(x.dataset.win) === n);
              if (b) b.click();
            }""", n)
        pg.wait_for_timeout(150)
        t = read_table()
        # 表格由新到舊：t[0] 是最新，t[-1] 是視窗起點
        win_cases[n] = {"rows": len(t), "first_date": t[-1][0].split()[0],
                        "first_delta": t[-1][6], "last_date": t[0][0].split()[0]}
    pg.evaluate("""() => { const b = [...document.querySelectorAll('#winSeg button')]
                           .find(x => Number(x.dataset.win) === 0); if (b) b.click(); }""")
    pg.wait_for_timeout(150)

    # tooltip 實際輸出（formatter 是函式，JSON 序列化會掉，所以在頁面裡直接呼叫）
    set_thr(-83000)
    tips = pg.evaluate(
        """(idxs) => { const o = window.__OPTS__[window.__OPTS__.length-1];
                       return idxs.map(i => o.tooltip.formatter([{dataIndex: i}])); }""",
        [0, 5, 20])
    b.close()

# ---------- 3. 比對 ----------
SMALL_KEYS = ["top10_trader", "top10_specific", "foreign_opt", "dealer_opt"]


def check_sub_axes(opt, thr, small_keys, tag):
    """副圖雙軸：右軸的門檻必須和左軸的 0 落在同一個高度，且兩軸格線重疊。"""
    bad = []
    yl, yr = opt["yAxis"][2], opt["yAxis"][3]
    for ax, nm in ((yl, "副圖左軸"), (yr, "副圖右軸")):
        for key in ("min", "max", "interval"):
            if ax.get(key) is None:
                bad.append("%s %s 沒有設 %s（沒設就會各自 auto-scale，對不齊）" % (tag, nm, key))
                return bad
        if ax.get("scale"):
            bad.append("%s %s 不該再開 scale" % (tag, nm))

    fl = (0 - yl["min"]) / (yl["max"] - yl["min"])          # 左軸 0 的相對高度
    fr = (thr - yr["min"]) / (yr["max"] - yr["min"])        # 右軸門檻的相對高度
    if abs(fl - fr) > 1e-9:
        bad.append("%s 對齊失敗：左軸 0 在 %.6f，右軸門檻在 %.6f" % (tag, fl, fr))

    nl = (yl["max"] - yl["min"]) / yl["interval"]
    nr = (yr["max"] - yr["min"]) / yr["interval"]
    if abs(nl - nr) > 1e-9:
        bad.append("%s 兩軸段數不同（%.3f vs %.3f），格線不會重疊" % (tag, nl, nr))
    if abs(nl - round(nl)) > 1e-9:
        bad.append("%s 段數不是整數：%.3f" % (tag, nl))
    # 對齊線必須落在某條格線上（而不是格子中間）
    if abs(fl * nl - round(fl * nl)) > 1e-9:
        bad.append("%s 對齊線沒有落在格線上" % tag)

    # 資料不能被切掉
    if small_keys:
        sv = [c[k] for c in chips for k in small_keys]
        if yl["min"] > min(sv) or yl["max"] < max(sv):
            bad.append("%s 左軸切到資料：軸 [%s, %s] vs 資料 [%s, %s]"
                       % (tag, yl["min"], yl["max"], min(sv), max(sv)))
    fv = [c["foreign_fut"] for c in chips]
    if yr["min"] > min(fv) or yr["max"] < max(fv):
        bad.append("%s 右軸切到資料：軸 [%s, %s] vs 資料 [%s, %s]"
                   % (tag, yr["min"], yr["max"], min(fv), max(fv)))

    g = [s for s in opt["series"] if s.get("name") == "__sub_guide"]
    if not g:
        bad.append("%s 副圖少了對齊基準線 __sub_guide" % tag)
    elif g[0].get("yAxisIndex") != 2 or g[0]["markLine"]["data"][0].get("yAxis") != 0:
        bad.append("%s 對齊基準線沒有畫在左軸 0" % tag)
    return bad


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
    shown = [r[0].split()[0] for r in tbl]
    if shown != sorted(shown, reverse=True):
        fails.append("thr=%s 逐日明細不是由新到舊排序：開頭 %s" % (thr, shown[:3]))
    if shown and shown[0] != chips[-1]["date"]:
        fails.append("thr=%s 表格第一列應該是最新的 %s，實得 %s"
                     % (thr, chips[-1]["date"], shown[0]))
    tbl = tbl[::-1]          # 之後的逐格比對沿用「由舊到新」的索引
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
        # MA 與乖離率（跟著目前選中的股票，預設 2330）
        want_ma, want_bias = exp_bias[prices["stocks"][0]["code"]][d]
        if want_ma is None:
            if row[3] != "—" or row[4] != "—":
                fails.append("thr=%s %s 均線不足時應顯示 —，卻是 %r / %r" % (thr, d, row[3], row[4]))
        else:
            got_ma = float(row[3].replace(",", ""))
            if abs(got_ma - want_ma) > 0.011:
                fails.append("thr=%s %s MA %s != %.2f" % (thr, d, row[3], want_ma))
            got_bias = float(row[4].replace("%", "").replace("+", ""))
            if abs(got_bias - want_bias) > 0.011:
                fails.append("thr=%s %s 乖離率 %s != %.2f%%" % (thr, d, row[4], want_bias))

        # 每個欄位三格：口數 / 本日操作 / 票
        for k, f in enumerate(["foreign_fut", "top10_trader", "top10_specific",
                               "foreign_opt", "dealer_opt"]):
            gv = row[5 + k * 3].replace(",", "")
            if int(gv) != chips[i][f]:
                fails.append("thr=%s %s %s 口數 %s != %s" % (thr, d, f, gv, chips[i][f]))

            gd = row[6 + k * 3].replace(",", "").replace("+", "")
            want_d = None if i == 0 else chips[i][f] - chips[i - 1][f]
            if want_d is None:
                if gd != "—":
                    fails.append("thr=%s 第一天 %s 本日操作應為 —，卻是 %r" % (thr, f, gd))
            elif int(gd) != want_d:
                fails.append("thr=%s %s %s 本日操作 %s != %d" % (thr, d, f, gd, want_d))

            gvote = int(row[7 + k * 3])
            if gvote != v[k]:
                fails.append("thr=%s %s %s 票 %d != %d" % (thr, d, f, gvote, v[k]))
        if int(row[20]) != fut:
            fails.append("thr=%s %s 期貨小計 %s != %d" % (thr, d, row[20], fut))
        if int(row[21]) != opt:
            fails.append("thr=%s %s 選擇權小計 %s != %d" % (thr, d, row[21], opt))
        if int(row[22]) != tot:
            fails.append("thr=%s %s 總分 %s != %d" % (thr, d, row[22], tot))
        if len(row) != 23:
            fails.append("thr=%s %s 欄數 %d != 23" % (thr, d, len(row)))

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
    # 背景色塊：兩支滿格 bar，每天的顏色要對應總分正負
    tots = [e[3] for e in exp[thr]]
    bgs = [s for s in o["series"] if str(s.get("name", "")).startswith("__bg_")]
    if len(bgs) != 2:
        fails.append("thr=%s 背景 bar 序列數 %d != 2" % (thr, len(bgs)))
    for b in bgs:
        if b.get("barWidth") != "100%" or b.get("barCategoryGap") != "0%" or b.get("barGap") != "-100%":
            fails.append("背景 bar 沒有設成滿格（barWidth/barCategoryGap/barGap）")
        if b.get("z") != 0 or not b.get("silent"):
            fails.append("背景 bar 應該 z=0 且 silent")
        if len(b["data"]) != len(tots):
            fails.append("thr=%s 背景 bar 天數 %d != %d" % (thr, len(b["data"]), len(tots)))
            continue
        for k, item in enumerate(b["data"]):
            col = item["itemStyle"]["color"]
            want = "up" if tots[k] > 0 else ("down" if tots[k] < 0 else "none")
            got = ("up" if "217,59,48" in col else
                   "down" if "18,153,107" in col else
                   "none" if col == "transparent" else "?" + col)
            if got != want:
                fails.append("thr=%s %s 背景色 %s != %s（總分 %d）"
                             % (thr, chips[k]["date"], got, want, tots[k]))
    if "markArea" in sser:
        fails.append("總分序列不該再有 markArea（category 軸座標會被取整）")
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
        # 圖上的第一根 K 棒對應的是「視窗第一天」，不是股價歷史的第一天
        first = ck[0]["data"][0]
        p0 = price_by_date[chips[0]["date"]]
        if first != [p0["open"], p0["close"], p0["low"], p0["high"]]:
            fails.append("K 棒 OHLC 順序不對：%s" % first)
    # 副圖：foreign_fut 獨立 y 軸
    chip_s = {s["name"]: s for s in o["series"]
              if s.get("xAxisIndex") == 1 and s.get("name") != "__sub_guide"}
    if len(chip_s) != 5:
        fails.append("副圖籌碼線數量 %d != 5" % len(chip_s))
    fa = chip_s.get("外資台指期未平倉", {}).get("yAxisIndex")
    others = set(s["yAxisIndex"] for n, s in chip_s.items()
                 if n not in ("外資台指期未平倉", "__sub_guide"))
    if fa is None or others != {2} or fa != 3:
        fails.append("foreign_fut 沒有用獨立 y 軸（fa=%s others=%s）" % (fa, others))
    fails += check_sub_axes(o, thr, SMALL_KEYS, "thr=%s" % thr)

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

for off, thr, opt in toggle_cases:
    fails += check_sub_axes(opt, thr, [k for k in SMALL_KEYS if k not in off],
                            "關掉%s @門檻%s" % (off or ["無"], thr))
print("副圖雙軸對齊：%d 種門檻/勾選組合皆通過" % (2 + len(toggle_cases)))

# 區間切換
w20, wall = win_cases[20], win_cases[0]
if wall["rows"] != len(chips):
    fails.append("「全部」應該顯示 %d 列，實得 %d" % (len(chips), wall["rows"]))
if w20["rows"] != min(20, len(chips)):
    fails.append("「最近 20 日」應該顯示 %d 列，實得 %d" % (min(20, len(chips)), w20["rows"]))
if w20["last_date"] != chips[-1]["date"]:
    fails.append("視窗應該對齊到最新的一天，實得 %s" % w20["last_date"])
if len(chips) > 20:
    if w20["first_date"] != chips[len(chips) - 20]["date"]:
        fails.append("「最近 20 日」的起點不對：%s" % w20["first_date"])
    if w20["first_delta"] == "—":
        fails.append("切窗後第一列的本日操作不該是 —，"
                     "應該跟歷史上的前一個交易日比（%s）" % w20["first_date"])
    else:
        f = chips[len(chips) - 20]
        prev = chips[len(chips) - 21]
        want = f["foreign_fut"] - prev["foreign_fut"]
        got = int(w20["first_delta"].replace(",", "").replace("+", ""))
        if got != want:
            fails.append("切窗後第一列本日操作 %d != %d" % (got, want))
print("區間切換：全部 %d 列 / 最近 20 日 %d 列，切窗後第一列仍與歷史前一日相比"
      % (wall["rows"], w20["rows"]))

# tooltip：口數與票數之間要出現「本日操作」，順序不能跑掉
import html as _html
for idx, tip in zip([0, 5, 20], tips):
    txt = re.sub(r"<[^>]+>", "|", tip)
    txt = _html.unescape(txt)
    if "本日操作" not in tip:
        fails.append("tooltip(第%d天) 沒有本日操作欄" % idx)
    for f in ["foreign_fut", "top10_trader", "top10_specific", "foreign_opt", "dealer_opt"]:
        val = "{:,}".format(chips[idx][f])
        want = "—" if idx == 0 else "{}{:,}".format("+" if chips[idx][f] > chips[idx-1][f] else "",
                                                   chips[idx][f] - chips[idx-1][f])
        # 口數 → 本日操作 → 票，三者必須依序出現
        p_val = txt.find("|" + val + "|")
        p_dlt = txt.find("|" + want + "|", p_val if p_val >= 0 else 0)
        if p_val < 0:
            fails.append("tooltip(第%d天) 找不到 %s 的口數 %s" % (idx, f, val))
        elif p_dlt < 0:
            fails.append("tooltip(第%d天) %s 的本日操作 %s 沒有緊接在口數後面" % (idx, f, want))
print("tooltip：口數 → 本日操作 → 票 的順序與數值正確（第 1／6／21 天）")

# tooltip 的乖離率區塊：三檔都要有，且 >15% 的要被標紅
for idx, tip in zip([0, 5, 20], tips):
    d = chips[idx]["date"]
    if "乖離率" not in tip:
        fails.append("tooltip(第%d天) 沒有乖離率區塊" % idx)
        continue
    for st in prices["stocks"]:
        ma, bias = exp_bias[st["code"]].get(d, (None, None))
        want = "—" if bias is None else "%s%.2f%%" % ("+" if bias > 0 else "", bias)
        if want not in tip:
            fails.append("tooltip(%s) 缺 %s 的乖離率 %s" % (d, st["code"], want))
        if bias is not None and bias > BIAS_ALERT:
            # 標紅 = 用漲色 #d93b30 且加粗
            seg = tip[max(tip.find(want) - 260, 0): tip.find(want) + len(want) + 4]
            if "d93b30" not in seg or "font-weight:700" not in seg:
                fails.append("tooltip(%s) %s 乖離 %s 超過 %g%% 但沒有標紅"
                             % (d, st["code"], want, BIAS_ALERT))
print("tooltip：三檔乖離率皆列出，超過 %g%% 的有標紅" % BIAS_ALERT)

for scheme, items in contrast.items():
    for it in items:
        if it["ratio"] < 4.5:
            fails.append("%s 模式下 %s 文字對比只有 %s:1（fg %s / bg %s）"
                         % (scheme, it["sel"], it["ratio"], it["fg"], it["bg"]))
print("表單對比（深色系統主題）：",
      ", ".join("%s %s:1" % (i["sel"], i["ratio"]) for i in contrast["dark"]))

print("\n統計卡：", stats)
print("門檻說明：", thr_note.strip()[:160])
print()
if fails:
    print("❌ 失敗 %d 項：" % len(fails))
    for f in fails[:40]:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通過：21 天 × 2 門檻 × 23 欄（含本日操作、MA20 與乖離率），表格 / tooltip / 圖表序列 / "
      "背景色塊 / markLine / 副圖雙軸對齊 / 顏色與對比 皆與 Python 獨立計算一致")
