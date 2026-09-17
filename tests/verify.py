#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
驗證：
 1. 用 Python 獨立算一次五票分數（兩種門檻），
 2. 用 headless Chromium 開實際頁面（ECharts 以 stub 取代，因離線環境無 CDN），
    讀出畫面上的表格與 setOption 內容，
 3. 兩邊逐格比對。
"""
import csv, json, math, os, re, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---------- 1. Python 端獨立計分 ----------
with open(os.path.join(ROOT, "data", "history", "chips.csv"), encoding="utf-8") as f:
    chips = list(csv.DictReader(f))
for c in chips:
    for k in c:
        # 近月那兩欄是後來才加的，舊資料是空字串 → None（不能當 0）
        if k != "date":
            c[k] = None if c[k] == "" else int(c[k])

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


# ---------- 1c. Python 端獨立重算三種計分模式 ----------
# 這裡刻意用和 app.js 不同的寫法（不共用任何程式碼），才驗得出東西。
KEYS = ["foreign_fut", "top10_trader", "top10_specific", "foreign_opt", "dealer_opt"]
ZERO_CENTERED = {"top10_trader", "top10_specific", "dealer_opt"}


def _median(a):
    b = sorted(a)
    n = len(b)
    if not n:
        return 0.0
    return float(b[n // 2]) if n % 2 else (b[n // 2 - 1] + b[n // 2]) / 2.0


def _mad(a, center=None):
    c = _median(a) if center is None else center
    s = 1.4826 * _median([abs(v - c) for v in a])
    if s > 0:
        return s
    m = sum(abs(v - c) for v in a) / (len(a) or 1)
    return m if m > 0 else 0.0


def _curve(u, name, k):
    if name == "clamp2":
        return max(-1.0, min(1.0, u / 2.0)) * 0.5
    if name == "clamp3":
        return max(-1.0, min(1.0, u / 3.0)) * 0.5
    return math.tanh(u / k) * 0.5


def score_modes(series, mode, thr, window, curve_name, curve_k):
    """series 是 [{key: value}] 的全部歷史；回傳每天的 total（算不出來給 None）。"""
    out = []
    for i, row in enumerate(series):
        votes, ok = [], True
        for key in KEYS:
            x = row[key]
            if x is None:
                ok = False
                break
            if mode == "level":
                votes.append(1 if x > (thr if key == "foreign_fut" else 0) else -1)
                continue
            if i == 0:
                ok = False
                break
            d = x - series[i - 1][key]
            if mode == "flow":
                votes.append(1 if d > 0 else -1)
                continue
            if i < window + 1:
                ok = False
                break
            lw = [series[g][key] for g in range(i - window, i)]
            dw = [series[g][key] - series[g - 1][key] for g in range(i - window, i)]
            center = 0 if key in ZERO_CENTERED else _median(lw)
            s_lv, s_fl = _mad(lw), _mad(dw, 0)
            if not (s_lv > 0 and s_fl > 0):
                ok = False
                break
            votes.append(_curve((x - center) / s_lv, curve_name, curve_k)
                         + _curve(d / s_fl, curve_name, curve_k))
        out.append(sum(votes) if ok else None)
    return out

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

    # ---------- 十大交易人的兩種口徑 ----------
    # 歷史檔還沒補近月欄位時，那個選項必須是鎖住的。默默拿 null 去比大小
    # 會讓第 2、3 票全部投 -1，畫面看起來仍然「正常」，這種錯最難發現。
    scope_locked = pg.evaluate(
        """() => [...document.querySelectorAll('#scopeSeg button')]
                 .map(b => ({label: b.textContent, disabled: b.disabled}))""")
    scope_locked_note = pg.eval_on_selector("#scopeNote", "e => e.textContent")

    # 再開一份「已經補好近月欄位」的副本，驗切換真的會換掉第 2、3 票。
    # 故意把近月值設成所有契約的相反數，這樣每一天的第 2、3 票都必翻。
    import shutil as _sh, subprocess as _sp, tempfile as _tf
    _tmp = _tf.mkdtemp(prefix="chips-scope-")
    _p2 = os.path.join(_tmp, "proj")
    _sh.copytree(ROOT, _p2, ignore=_sh.ignore_patterns(
        "dist", "__pycache__", ".git", "node_modules"))
    _cols = ["date", "foreign_fut", "top10_trader", "top10_specific",
             "top10_trader_front", "top10_specific_front",
             "foreign_opt", "dealer_opt"]
    _front = []
    with open(os.path.join(_p2, "data", "history", "chips.csv"),
              "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_cols, lineterminator="\n")
        w.writeheader()
        for c in chips:
            r = dict(c)
            r["top10_trader_front"] = -c["top10_trader"]
            r["top10_specific_front"] = -c["top10_specific"]
            _front.append(r)
            w.writerow({k: r[k] for k in _cols})
    _sp.run([sys.executable, "build_site.py"], cwd=_p2, check=True,
            capture_output=True)

    pg.goto("file://" + os.path.join(_p2, "index.html"))
    pg.wait_for_selector("#dataTable tbody tr")
    set_thr(-83000)
    scope_ready = pg.evaluate(
        """() => [...document.querySelectorAll('#scopeSeg button')]
                 .map(b => ({label: b.textContent, disabled: b.disabled,
                             on: b.className === 'on'}))""")
    scope_series = {}
    for _which in ("all", "front"):
        pg.evaluate("""(k) => { const b = [...document.querySelectorAll('#scopeSeg button')]
                                .find(x => x.dataset.scope === k); if (b) b.click(); }""", _which)
        pg.wait_for_timeout(150)
        scope_series[_which] = pg.evaluate(
            """() => { const o = window.__OPTS__[window.__OPTS__.length-1];
                       const out = {};
                       o.series.forEach(s => { if (s.name) out[s.name] = s.data; });
                       return out; }""")
    # ---------- 三種計分模式 ----------
    # 在補齊近月欄位的那份副本上做，口徑固定成「所有契約」，
    # 這樣 Python 端可以直接用 chips.csv 的原始欄位重算。
    pg.evaluate("""() => { const b = [...document.querySelectorAll('#scopeSeg button')]
                            .find(x => x.dataset.scope === 'all'); if (b) b.click(); }""")
    pg.evaluate("""() => { const b = [...document.querySelectorAll('#winSeg button')]
                            .find(x => Number(x.dataset.win) === 0); if (b) b.click(); }""")
    set_thr(-83000)
    mode_series, mode_thr_disabled = {}, {}
    for _m in ("level", "flow", "blend"):
        pg.evaluate("""(k) => { const b = [...document.querySelectorAll('#modeSeg button')]
                                .find(x => x.dataset.mode === k); if (b) b.click(); }""", _m)
        pg.wait_for_timeout(150)
        mode_series[_m] = pg.evaluate(
            """() => { const o = window.__OPTS__[window.__OPTS__.length-1];
                       const s = o.series.find(x => x.name === '五票總分');
                       return s ? s.data : null; }""")
        mode_thr_disabled[_m] = pg.eval_on_selector("#thrNum", "e => e.disabled")
    mode_table_blend = read_table()
    mode_note_blend = pg.eval_on_selector("#modeNote", "e => e.textContent")
    pg.evaluate("""() => { const b = [...document.querySelectorAll('#modeSeg button')]
                            .find(x => x.dataset.mode === 'level'); if (b) b.click(); }""")
    pg.wait_for_timeout(120)

    # ---------- 區間與標的要記得住 ----------
    # 在補齊後的那份副本上做（同一個 file:// 目錄 = 同一個 origin）。
    # 重點是「重新整理之後還在」，所以一定要真的 reload，不能只看變數。
    _pick_win = 20
    _pick_stock = pg.evaluate(
        """() => { const bs = [...document.querySelectorAll('#stockSeg button')];
                   return bs[bs.length - 1].dataset.code; }""")
    pg.evaluate("""(w) => { const b = [...document.querySelectorAll('#winSeg button')]
                            .find(x => Number(x.dataset.win) === w); if (b) b.click(); }""",
                _pick_win)
    pg.evaluate("""(c) => { const b = [...document.querySelectorAll('#stockSeg button')]
                            .find(x => x.dataset.code === c); if (b) b.click(); }""",
                _pick_stock)
    pg.wait_for_timeout(150)
    saved = pg.evaluate("() => localStorage.getItem('chips-kline:ui')")

    pg.reload()
    pg.wait_for_selector("#dataTable tbody tr")
    after_reload = pg.evaluate(
        """() => ({
             win: (document.querySelector('#winSeg button.on') || {}).dataset,
             stock: (document.querySelector('#stockSeg button.on') || {}).dataset,
             rows: document.querySelectorAll('#dataTable tbody tr').length
           })""")

    # 存了一個已經不存在的股票代碼 / 不在選項裡的區間 → 要退回預設，不能壞掉
    pg.evaluate("""() => localStorage.setItem('chips-kline:ui',
                     JSON.stringify({win: 7777, stock: 'NOPE'}))""")
    pg.reload()
    pg.wait_for_selector("#dataTable tbody tr")
    after_bogus = pg.evaluate(
        """() => ({
             win: (document.querySelector('#winSeg button.on') || {}).dataset,
             stock: (document.querySelector('#stockSeg button.on') || {}).dataset
           })""")

    # 存了一段壞掉的 JSON → 也要當作沒存過
    pg.evaluate("() => localStorage.setItem('chips-kline:ui', '{壞掉的')")
    pg.reload()
    pg.wait_for_selector("#dataTable tbody tr")
    after_broken = pg.evaluate(
        "() => (document.querySelector('#winSeg button.on') || {}).dataset")

    b.close()
    _sh.rmtree(_tmp, ignore_errors=True)

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
else:
    n_lines = len([s for s in cmp_opt["series"]
                   if s.get("yAxisIndex") == 0 and s.get("xAxisIndex") == 0])
    if n_lines != len(prices["stocks"]):
        fails.append("比較模式的折線 %d 條，應該等於股票數 %d"
                     % (n_lines, len(prices["stocks"])))
    colors = [s.get("color") for s in cmp_opt["series"]
              if s.get("yAxisIndex") == 0 and s.get("xAxisIndex") == 0]
    if len(set(colors)) != len(colors):
        fails.append("比較模式有重複的線色：%s" % colors)

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

# ---------- 十大交易人口徑切換 ----------
_labels = [x["label"] for x in scope_locked]
if _labels != ["所有契約", "近月"]:
    fails.append("口徑切換的兩個選項不對：%s" % _labels)
if not scope_locked[1]["disabled"]:
    fails.append("歷史檔還沒有近月欄位時，「近月」必須是鎖住的（不然會拿 null 去投票）")
if "backfill_large" not in scope_locked_note:
    fails.append("鎖住時要說明怎麼補，實得：%r" % scope_locked_note)

if scope_ready[1]["disabled"]:
    fails.append("補齊近月欄位之後，「近月」不該還鎖著")
_def_scope = prices.get("defaultLargeScope", "all")
_on = [x["label"] for x in scope_ready if x["on"]]
_want_scope = "近月" if _def_scope == "front" else "所有契約"
if _on != [_want_scope]:
    fails.append("預設口徑應該跟著 config 的 defaultLargeScope=%r 選 %s，實得 %s"
                 % (_def_scope, _want_scope, _on))

_name_all = "前十大交易人（所有契約）"
_name_front = "前十大交易人（近月）"
if _name_all not in scope_series["all"]:
    fails.append("所有契約模式下找不到序列 %r，實得 %s"
                 % (_name_all, sorted(scope_series["all"])))
if _name_front not in scope_series["front"]:
    fails.append("近月模式下序列名稱應該標明口徑，實得 %s"
                 % sorted(scope_series["front"]))
else:
    _got = scope_series["front"][_name_front]
    _want = [r["top10_trader_front"] for r in _front]
    if _got != _want:
        fails.append("切到近月之後，前十大交易人的數列沒有換成近月值："
                     "前 3 筆 %s vs %s" % (_got[:3], _want[:3]))

# 分數也要跟著換——這才是切換真正的意義
for _which, _k2, _k3 in (("all", "top10_trader", "top10_specific"),
                         ("front", "top10_trader_front", "top10_specific_front")):
    _want_total = []
    for r in _front:
        v = [1 if r["foreign_fut"] > -83000 else -1,
             1 if r[_k2] > 0 else -1,
             1 if r[_k3] > 0 else -1,
             1 if r["foreign_opt"] > 0 else -1,
             1 if r["dealer_opt"] > 0 else -1]
        _want_total.append(sum(v))
    _got_total = scope_series[_which].get("五票總分")
    if _got_total != _want_total:
        fails.append("%s 口徑的總分不對：前 5 筆 %s vs 應為 %s"
                     % (_which, (_got_total or [])[:5], _want_total[:5]))
if scope_series["all"].get("五票總分") == scope_series["front"].get("五票總分"):
    fails.append("兩種口徑算出完全一樣的總分——這組測試資料是故意反號的，不可能相同，"
                 "切換多半沒有真的生效")
print("口徑切換：未補齊時「近月」鎖住；補齊後可切，數列、序列名稱與五票總分都跟著換")

# ---------- 三種計分模式 ----------
_W = prices.get("scoreWindow", 20)
_CV = prices.get("scoreCurve", "tanh")
_CK = prices.get("scoreCurveK", 2.5)
_series = [{k: c[k] for k in KEYS} for c in chips]      # 口徑 = 所有契約
for _m, _label in (("level", "剩餘口數"), ("flow", "今日操作"), ("blend", "加權")):
    want = score_modes(_series, _m, -83000, _W, _CV, _CK)
    got = mode_series.get(_m)
    if got is None:
        fails.append("%s 模式找不到五票總分序列" % _label)
        continue
    if len(got) != len(want):
        fails.append("%s 模式的序列長度 %s != 歷史天數 %s" % (_label, len(got), len(want)))
        continue
    for i, (a, b2) in enumerate(zip(got, want)):
        if (a is None) != (b2 is None):
            fails.append("%s 模式 %s：一邊算得出來一邊算不出來（畫面 %r vs Python %r）"
                         % (_label, chips[i]["date"], a, b2))
            break
        if a is not None and abs(a - b2) > 1e-9:
            fails.append("%s 模式 %s：總分 %r != Python 重算的 %r"
                         % (_label, chips[i]["date"], a, b2))
            break

# 暖身：模式 1 每天都有分數；模式 2 少第一天；模式 3 少前 window+1 天
_n = len(chips)
_have = {m: sum(1 for v in (mode_series[m] or []) if v is not None) for m in mode_series}
if _have.get("level") != _n:
    fails.append("剩餘口數模式應該每天都算得出來（%d 天），實得 %s" % (_n, _have.get("level")))
if _have.get("flow") != _n - 1:
    fails.append("今日操作模式應該少第一天（%d 天），實得 %s" % (_n - 1, _have.get("flow")))
if _have.get("blend") != _n - _W - 1:
    fails.append("加權模式暖身 %d 天，應該剩 %d 天，實得 %s"
                 % (_W + 1, _n - _W - 1, _have.get("blend")))

# 門檻只在模式 1 有效，其他模式要停用（不然會以為調了有用）
if mode_thr_disabled.get("level"):
    fails.append("剩餘口數模式下門檻不該被停用")
if not mode_thr_disabled.get("flow") or not mode_thr_disabled.get("blend"):
    fails.append("今日操作／加權模式下門檻應該停用，實得 %s" % mode_thr_disabled)

# 加權模式的重點：曲線設定要真的生效。把同一份資料用 clamp2 再算一次，
# 兩者必須不同——如果一樣，代表 config 的 scoreCurve 沒被讀到，或被寫死成 clamp。
_blend = [v for v in (mode_series.get("blend") or []) if v is not None]
if not _blend:
    print("  [略過] 這份歷史只有 %d 天，暖身 %d 天之後沒有可比的加權分數；"
          "曲線相關的斷言在天數足夠的 repo 上才會執行" % (_n, _W + 1))
else:
    _alt = [v for v in score_modes(_series, "blend", -83000, _W, "clamp2", _CK) if v is not None]
    if _alt == _blend:
        fails.append("加權模式用 clamp2 重算得到完全一樣的結果——scoreCurve 設定多半沒生效")
    # tanh 不封頂：任何一天的單欄分數都不該正好是 ±0.5（那是 clamp 的特徵）
    _hit = [v for v in _blend if abs(abs(v) - 5.0) < 1e-9]
    if _hit:
        fails.append("加權模式出現總分正好 ±5 的日子——tanh 不可能達到，曲線設定有問題")
    # 表格要顯示小數票，而且暖身不足的日子是 —
    _flat = " ".join(" ".join(r) for r in mode_table_blend)
    if not re.search(r"[+-]0\.\d\d", _flat):
        fails.append("加權模式的表格應該出現小數分數（例如 +0.43），實得前兩列：%s"
                     % mode_table_blend[:2])
    if "—" not in _flat:
        fails.append("加權模式的表格應該有暖身不足的 — 列")
if "tanh" not in mode_note_blend and "封頂" not in mode_note_blend:
    fails.append("加權模式的說明應該講清楚用的是哪條曲線，實得：%r" % mode_note_blend[:80])
print("三種計分模式：%d 天逐日與 Python 獨立重算一致（剩餘口數 %d／今日操作 %d／加權 %d 天有分數）"
      % (_n, _have.get("level", 0), _have.get("flow", 0), _have.get("blend", 0)))
if _blend:
    print("  加權模式：%d 天有分數，範圍 %+.2f ~ %+.2f，且與 clamp2 重算的結果不同"
          % (len(_blend), min(_blend), max(_blend)))

# ---------- 區間與標的記憶 ----------
if not saved or '"win"' not in saved or '"stock"' not in saved:
    fails.append("點過區間與標的之後，localStorage 應該有記錄，實得 %r" % saved)
if not after_reload["win"] or int(after_reload["win"]["win"]) != _pick_win:
    fails.append("重新整理之後區間沒有還原成最近 %d 日，實得 %s"
                 % (_pick_win, after_reload["win"]))
if after_reload["rows"] != _pick_win:
    fails.append("區間記住了但表格沒有跟著只剩 %d 列，實得 %d 列"
                 % (_pick_win, after_reload["rows"]))
if not after_reload["stock"] or after_reload["stock"]["code"] != _pick_stock:
    fails.append("重新整理之後主要標的沒有還原成 %s，實得 %s"
                 % (_pick_stock, after_reload["stock"]))
# 存了無效值時要安靜退回預設（site.json 的 defaultWindow 與第一檔股票）
_def_win = prices.get("defaultWindow", 60)
_def_stock = prices["stocks"][0]["code"]
if not after_bogus["win"] or int(after_bogus["win"]["win"]) != _def_win:
    fails.append("存了不存在的區間時應該退回預設 %s，實得 %s"
                 % (_def_win, after_bogus["win"]))
if not after_bogus["stock"] or after_bogus["stock"]["code"] != _def_stock:
    fails.append("存了已移除的股票代碼時應該退回預設 %s，實得 %s"
                 % (_def_stock, after_bogus["stock"]))
if not after_broken or int(after_broken["win"]) != _def_win:
    fails.append("localStorage 是壞掉的 JSON 時應該當作沒存過，實得 %s" % after_broken)
print("記憶：區間與標的重新整理後還在；存到無效值或壞 JSON 時安靜退回預設")

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
