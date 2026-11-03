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

with open(os.path.join(ROOT, "data", "site.json"), encoding="utf-8") as f:
    prices = json.load(f)          # 現在前端只讀 site.json

# 畫面預設用哪一種十大口徑，Python 端就要跟著用哪一種，不然比的是兩份不同的數字。
FRONT_READY = bool(chips) and all(
    c.get("top10_trader_front") is not None and c.get("top10_specific_front") is not None
    for c in chips)
PAGE_SCOPE = prices.get("defaultLargeScope", "all")
if PAGE_SCOPE == "front" and FRONT_READY:
    for c in chips:
        c["top10_trader"] = c["top10_trader_front"]
        c["top10_specific"] = c["top10_specific_front"]
else:
    PAGE_SCOPE = "all"


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

# ECharts 用 stub 取代（離線環境沒有 CDN）。幾何相關的 API 也要給，
# 不然 app.js 綁 zrender 的 click 時會直接丟例外。
# convertFromPixel 故意回傳 offsetX 本身，所以測試裡「點第 N 天」就是傳 offsetX=N。
# 代價：真正的像素→索引換算沒被驗到（那是 ECharts 的事）；
# 這裡驗的是釘住的開關、以日期為鍵、切區間之後的行為。
STUB = """
window.__OPTS__ = [];
window.__ZR__ = { handlers: {} };
window.echarts = {
  init: function(){ return {
    setOption: function(o){ window.__OPTS__.push(o); },
    resize: function(){},
    getZr: function(){ return { on: function(ev, fn){ window.__ZR__.handlers[ev] = fn; } }; },
    containPixel: function(f){ return f.gridIndex === 0; },
    convertFromPixel: function(f, x){ return x; }
  };}
};
"""

# 切到「全部」區間。預設是 60 天，歷史比 60 天長時表格只會有 60 列。
WIN_ALL_JS = """() => { const b = [...document.querySelectorAll('#winSeg button')]
                        .find(x => Number(x.dataset.win) === 0); if (b) b.click(); }"""

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

    # 主要比對要涵蓋全部歷史，所以先切到「全部」。
    # （預設視窗是 60 天，歷史比 60 天長的時候表格只會有 60 列。）
    pg.evaluate("""() => { const b = [...document.querySelectorAll('#winSeg button')]
                            .find(x => Number(x.dataset.win) === 0); if (b) b.click(); }""")
    pg.wait_for_timeout(150)

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
                 // 只動五票那五個；最後那個「大盤融資維持率」不投票，另外測
                 const labs = [...document.querySelectorAll('#chipChks label:not(.chk-margin)')];
                 const keys  = ['foreign_fut','top10_trader','top10_specific','foreign_opt','dealer_opt'];
                 if (labs.length !== keys.length) {
                   throw new Error('五票的勾選框應該有 ' + keys.length + ' 個，實得 ' + labs.length);
                 }
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

    # ---------- 點 K 線釘住當日明細 ----------
    def zr_click(i):
        pg.evaluate("(i) => window.__ZR__.handlers.click({offsetX: i, offsetY: 100})", i)
        pg.wait_for_timeout(120)

    def pin_state():
        return pg.evaluate("""() => {
            const box = document.getElementById('pinned');
            const o = window.__OPTS__[window.__OPTS__.length - 1];
            return { hidden: box.hidden,
                     text: box.hidden ? '' : box.textContent.slice(0, 80),
                     tooltipShown: o.tooltip.show !== false };
        }""")

    pg.evaluate("""() => { const b = [...document.querySelectorAll('#winSeg button')]
                            .find(x => Number(x.dataset.win) === 0); if (b) b.click(); }""")
    pg.wait_for_timeout(120)
    pin_before = pin_state()
    _pin_i = len(chips) - 3
    _pin_date = chips[_pin_i]["date"]
    zr_click(_pin_i)
    pin_open = pin_state()
    zr_click(_pin_i)                      # 再點同一天 → 收起來
    pin_toggled = pin_state()
    zr_click(_pin_i)
    pg.eval_on_selector("#pinClose", "e => e.click()")   # 按 × → 收起來
    pg.wait_for_timeout(120)
    pin_closed = pin_state()
    zr_click(_pin_i)
    zr_click(len(chips) - 1)               # 點別天 → 換成那天，不是關掉
    pin_moved = pin_state()
    pin_moved_date = chips[len(chips) - 1]["date"]
    # 釘著第 N-3 天，然後切到「最近 20 日」——如果那天落在視窗外，面板要收起來
    zr_click(_pin_i)
    pg.evaluate("""() => { const b = [...document.querySelectorAll('#winSeg button')]
                            .find(x => Number(x.dataset.win) === 20); if (b) b.click(); }""")
    pg.wait_for_timeout(150)
    pin_after_win = pin_state()
    pin_after_win_visible = _pin_date in set(c["date"] for c in chips[-20:])
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(120)
    pin_after_esc = pin_state()
    pg.evaluate("""() => { const b = [...document.querySelectorAll('#winSeg button')]
                            .find(x => Number(x.dataset.win) === 0); if (b) b.click(); }""")
    pg.wait_for_timeout(120)

    # ---------- 分頁切換 ----------
    # TradingView 是第三方、而且離線測試環境連不到，所以這裡不驗它畫不畫得出圖，
    # 只驗我們自己負責的部分：分頁切換、控制列收合、script 真的是延遲載入的、
    # 以及切回來之後有重畫（ECharts 在隱藏容器裡會量到 0 寬）。
    pg.route("**s3.tradingview.com**", lambda r: r.abort())

    def tab_state():
        return pg.evaluate("""() => ({
            chips: !document.getElementById('tabChips').hidden,
            macro: !document.getElementById('tabMacro').hidden,
            controls: !document.getElementById('controls').hidden,
            on: [...document.getElementById('tabs').children]
                  .filter(b => b.className === 'on').map(b => b.dataset.tab),
            selected: [...document.getElementById('tabs').children]
                  .map(b => b.getAttribute('aria-selected')),
            tvScripts: document.querySelectorAll('script[src*="tradingview"]').length,
            tvBoxes: document.querySelectorAll('#tabMacro .tv-box').length
          })""")

    def click_tab(name):
        pg.evaluate("(n) => { const b = [...document.getElementById('tabs').children]"
                    ".find(x => x.dataset.tab === n); if (b) b.click(); }", name)
        pg.wait_for_timeout(250)

    tab_initial = tab_state()
    click_tab('macro')
    tab_macro = tab_state()
    opts_before_back = pg.evaluate("() => window.__OPTS__.length")
    click_tab('chips')
    tab_back = tab_state()
    opts_after_back = pg.evaluate("() => window.__OPTS__.length")
    tab_rows_after_back = len(read_table())

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

    # ---------- 大盤融資維持率 ----------
    # 再開第三份副本，這次多一個 data/history/margin.csv。
    # 刻意讓它比籌碼短（前面幾天沒有）而且中間缺一天：
    #   短 —— 剛上線時只回填了近 90 天，籌碼卻有一整年，前端必須照日期對齊；
    #   缺 —— 連假或來源當天掛掉會留一個洞，線要斷開而不是接成一條假的水平線。
    _p3 = os.path.join(_tmp, "proj3")
    _sh.copytree(ROOT, _p3, ignore=_sh.ignore_patterns(
        "dist", "__pycache__", ".git", "node_modules"))
    _m_skip_head = 3                       # 前 3 天沒有維持率
    _m_gap = len(chips) // 2               # 中間挖掉這一天
    _m_want, _m_rows = [], []
    for _i, _c in enumerate(chips):
        if _i < _m_skip_head or _i == _m_gap:
            _m_want.append(None)
            continue
        _v = round(165 + (_i % 11) * 0.73, 2)
        _m_want.append(_v)
        _m_rows.append({"date": _c["date"], "ratio": _v,
                        "twse_ratio": _v, "otc_ratio": _v,
                        "numerator": 1000, "denominator": 1000})
    with open(os.path.join(_p3, "data", "history", "margin.csv"),
              "w", encoding="utf-8", newline="") as f:
        _w = csv.DictWriter(f, fieldnames=["date", "ratio", "twse_ratio",
                                           "otc_ratio", "numerator",
                                           "denominator"], lineterminator="\n")
        _w.writeheader()
        for _r in _m_rows:
            _w.writerow(_r)
    _sp.run([sys.executable, "build_site.py"], cwd=_p3, check=True,
            capture_output=True)

    pg.goto("file://" + os.path.join(_p3, "index.html"))
    pg.wait_for_selector("#dataTable tbody tr")
    pg.evaluate(WIN_ALL_JS)
    pg.wait_for_timeout(150)

    MARGIN_JS = """() => {
        const o = window.__OPTS__[window.__OPTS__.length - 1];
        const ser = o.series.find(s => s.name === '大盤融資維持率') || null;
        const lab = document.querySelector('#chipChks label.chk-margin');
        const cb = lab && lab.querySelector('input');
        return {
          ser: ser && { y: ser.yAxisIndex, x: ser.xAxisIndex, type: ser.type,
                        dash: ser.lineStyle && ser.lineStyle.type,
                        connect: ser.connectNulls, data: ser.data },
          axisShown: o.yAxis[4] ? o.yAxis[4].show : null,
          axisName: o.yAxis[4] ? o.yAxis[4].name : null,
          gridRight: o.grid.map(g => g.right),
          legend: o.legend[1].data,
          votes: o.series.filter(s => s.xAxisIndex === 1 && s.name
                    && !s.name.startsWith('__') && s.name !== '大盤融資維持率').length,
          total: (o.series.find(s => s.name === '五票總分') || {}).data,
          tip: o.tooltip.formatter([{dataIndex: 10}]),
          cb: cb && { checked: cb.checked, disabled: cb.disabled,
                      text: lab.textContent, title: lab.title }
        };
      }"""

    margin_on = pg.evaluate(MARGIN_JS)
    pg.evaluate("""() => { const cb = document.querySelector('#chipChks label.chk-margin input');
                           cb.checked = false;
                           cb.dispatchEvent(new Event('change', {bubbles:true})); }""")
    pg.wait_for_timeout(150)
    margin_off = pg.evaluate(MARGIN_JS)

    # 沒有 margin.csv 的那一份（_p2）：勾選框必須是鎖住的，而且完全沒有那條線。
    # 不能默默畫一條空線或把缺的日子當 0——0% 維持率是個荒謬但「看起來有資料」的值。
    pg.goto("file://" + os.path.join(_p2, "index.html"))
    pg.wait_for_selector("#dataTable tbody tr")
    pg.evaluate(WIN_ALL_JS)
    pg.wait_for_timeout(150)
    margin_absent = pg.evaluate(MARGIN_JS)

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

    # 資料不能被切掉。範圍要從「這張圖真正畫出來的序列」取——
    # 拿整份歷史來比會誤判：視窗只顯示最近 N 天，軸本來就只需要涵蓋那一段。
    def axis_vals(idx):
        out = []
        for ser in opt["series"]:
            if ser.get("yAxisIndex") != idx or ser.get("type") != "line":
                continue
            if not ser.get("name") or ser["name"].startswith("__"):
                continue
            out += [v for v in ser.get("data", []) if v is not None]
        return out

    sv = axis_vals(2)
    if sv and (yl["min"] > min(sv) or yl["max"] < max(sv)):
        bad.append("%s 左軸切到資料：軸 [%s, %s] vs 資料 [%s, %s]"
                   % (tag, yl["min"], yl["max"], min(sv), max(sv)))
    fv = axis_vals(3)
    if fv and (yr["min"] > min(fv) or yr["max"] < max(fv)):
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
        # 第一個交易日沒有漲跌價（來源就沒給），畫面應該顯示 — 而不是 0%
        if pr["change"] is None or (pr["close"] - pr["change"]) == 0:
            if row[2] != "—":
                fails.append("thr=%s %s 沒有漲跌價，應該顯示 —，實得 %r" % (thr, d, row[2]))
        else:
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
              if s.get("xAxisIndex") == 1 and s.get("name") != "__sub_guide"
              and s.get("name") != "大盤融資維持率"}
    if len(chip_s) != 5:
        fails.append("副圖籌碼線（不含融資維持率）數量 %d != 5" % len(chip_s))
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
# 日期太多時畫面刻意不逐一列出（改叫人看表格），只有 12 天以內才逐日比對
if len(diff_days) <= 12:
    for d in diff_days:
        if d not in thr_note:
            fails.append("門檻比較說明沒列出 %s" % d)
elif "請看下方表格" not in thr_note:
    fails.append("差異有 %d 天（超過 12），說明應該改成請看表格，實得：%r"
                 % (len(diff_days), thr_note[:120]))

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

# tooltip 只放籌碼：票數要在最上面，個股的行情與乖離率不應該出現在這裡。
# （那些在下方的逐日明細表格裡查得到；把六檔股票塞進這張卡片會把五票擠下去。）
for idx, tip in zip([0, 5, 20], tips):
    d = chips[idx]["date"]
    txt = _html.unescape(re.sub(r"<[^>]+>", "|", tip))

    # 票數必須排在日期之後的第一個區塊
    p_date = txt.find(d)
    p_vote = txt.find("本日操作")
    if p_date < 0:
        fails.append("tooltip(%s) 沒有日期" % d)
    elif not (0 <= p_date < p_vote):
        fails.append("tooltip(%s) 票數區塊沒有排在最上面（日期 %d、票數 %d）"
                     % (d, p_date, p_vote))

    for word in ("乖離率", "MA%d" % MA_PERIOD):
        if word in tip:
            fails.append("tooltip(%s) 不該再出現「%s」——個股資訊已經移出 hover" % (d, word))
    for st in prices["stocks"]:
        label = st["code"] + " " + st["name"]
        if label in tip:
            fails.append("tooltip(%s) 不該再出現個股 %s" % (d, label))

    # 小計與總分仍要在，而且排在五票之後
    for word in ("期貨小計", "選擇權小計", "總分", "計分模式"):
        if word not in tip:
            fails.append("tooltip(%s) 少了「%s」" % (d, word))
    if txt.find("總分") < p_vote:
        fails.append("tooltip(%s) 總分跑到五票前面了" % d)
print("tooltip：只剩籌碼——票數緊接在日期之後，個股行情與乖離率已移除，小計/總分在最後")

# ---------- 大盤融資維持率 ----------
# 這一條的重點是「它不是第六票」。最容易出錯的方向是讓它影響到總分，
# 或是把缺的日子當 0（0% 維持率看起來像有資料，但荒謬到沒有人會去懷疑那是缺值）。
_mo = margin_on
if not _mo["cb"]:
    fails.append("副圖籌碼線那一排少了融資維持率的勾選框")
else:
    if _mo["cb"]["disabled"]:
        fails.append("有 margin.csv 時勾選框不該是鎖住的")
    if not _mo["cb"]["checked"]:
        fails.append("有 margin.csv 時維持率應該預設勾選")
    if "不投票" not in _mo["cb"]["text"]:
        fails.append("勾選框要寫明它不投票，實得 %r" % _mo["cb"]["text"])

if not _mo["ser"]:
    fails.append("有 margin.csv 時副圖應該要有「大盤融資維持率」這條線")
else:
    _s = _mo["ser"]
    if _s["y"] != 4 or _s["x"] != 1:
        fails.append("維持率應該畫在副圖（xAxisIndex 1）的第 5 根 y 軸，實得 x=%s y=%s"
                     % (_s["x"], _s["y"]))
    if _s["type"] != "line":
        fails.append("維持率應該是折線，實得 %s" % _s["type"])
    if _s["dash"] != "dashed":
        fails.append("維持率要用虛線（和五票的實線區分開），實得 %r" % _s["dash"])
    if _s["connect"] is not False:
        fails.append("維持率的 connectNulls 必須是 false，不然缺的日子會連成一條假線")
    if _s["data"] != _m_want:
        _diff = [(i, a, b2) for i, (a, b2) in enumerate(zip(_s["data"], _m_want))
                 if a != b2][:5]
        fails.append("維持率的數列和 margin.csv 對不上（照日期對齊才對），前幾個差異 %s"
                     % _diff)
    else:
        _nulls = [i for i, v in enumerate(_s["data"]) if v is None]
        if _nulls != list(range(_m_skip_head)) + [_m_gap]:
            fails.append("維持率缺值的位置不對：%s" % _nulls)

if _mo["axisShown"] is not True:
    fails.append("有維持率時第 5 根 y 軸要顯示，實得 show=%r" % _mo["axisShown"])
if _mo["axisName"] and "%" not in _mo["axisName"]:
    fails.append("維持率那根軸的名稱要標單位（%%），實得 %r" % _mo["axisName"])
if _mo["votes"] != 5:
    fails.append("副圖的五票線應該還是 5 條（維持率不算），實得 %d" % _mo["votes"])
if "大盤融資維持率" not in (_mo["legend"] or []):
    fails.append("副圖圖例少了維持率：%s" % (_mo["legend"],))
if _mo["gridRight"][0] != _mo["gridRight"][1]:
    fails.append("主圖和副圖的右邊距要一樣，不然兩張圖的 x 軸會錯開：%s"
                 % (_mo["gridRight"],))
if not (_mo["gridRight"][0] > margin_off["gridRight"][0]):
    fails.append("多一根軸就要多留右邊距，實得 開 %s / 關 %s"
                 % (_mo["gridRight"][0], margin_off["gridRight"][0]))

# tooltip：有勾才出現，而且要寫「不投票」
_tip_on = re.sub(r"<[^>]+>", "|", _mo["tip"])
if "大盤融資維持率" not in _mo["tip"]:
    fails.append("勾起來時 tooltip 應該要有維持率那一行")
elif "%.2f" % _m_want[10] not in _tip_on.replace(",", ""):
    fails.append("tooltip 裡的維持率數值不對，應該是 %.2f%%：%r"
                 % (_m_want[10], _tip_on[-160:]))
if "不投票" not in _mo["tip"]:
    fails.append("tooltip 的維持率要註明不投票，免得被當成第六票")
# 五票的部分一個字都不該被它擠掉
for _w in ("本日操作", "期貨小計", "選擇權小計", "總分", "計分模式"):
    if _w not in _mo["tip"]:
        fails.append("加了維持率之後 tooltip 少了「%s」" % _w)
if _tip_on.find("總分") > _tip_on.find("大盤融資維持率"):
    fails.append("維持率跑到總分前面了——它只是參考線，要排在五票與總分之後")

# 取消勾選：線、軸、tooltip 那一行都要消失，而且總分完全不變
if margin_off["ser"]:
    fails.append("取消勾選後不該還有維持率那條線")
if margin_off["axisShown"] is not False:
    fails.append("取消勾選後第 5 根 y 軸要隱藏，實得 show=%r" % margin_off["axisShown"])
if "大盤融資維持率" in margin_off["tip"]:
    fails.append("取消勾選後 tooltip 不該還有維持率那一行")
if margin_off["total"] != _mo["total"]:
    fails.append("勾選維持率改變了五票總分——它不該參與投票")
if margin_off["votes"] != 5:
    fails.append("取消勾選後副圖的五票線應該還是 5 條，實得 %d" % margin_off["votes"])

# 沒有 margin.csv 的情況
if margin_absent["ser"]:
    fails.append("沒有 margin.csv 時不該畫出維持率那條線")
if margin_absent["axisShown"] is not False:
    fails.append("沒有 margin.csv 時第 5 根 y 軸要隱藏")
if "大盤融資維持率" in margin_absent["tip"]:
    fails.append("沒有 margin.csv 時 tooltip 不該提到維持率")
if not margin_absent["cb"]:
    fails.append("沒有 margin.csv 時勾選框還是要在（鎖住並說明怎麼補）")
else:
    if not margin_absent["cb"]["disabled"]:
        fails.append("沒有 margin.csv 時勾選框必須鎖住")
    if margin_absent["cb"]["checked"]:
        fails.append("沒有 margin.csv 時勾選框不該是勾起來的")
    if "--only-margin" not in (margin_absent["cb"]["title"] or ""):
        fails.append("鎖住時要說清楚跑什麼指令補，實得 %r"
                     % margin_absent["cb"]["title"])
if margin_absent["gridRight"][0] != margin_off["gridRight"][0]:
    fails.append("沒有維持率時右邊距應該和取消勾選時一樣")

print("大盤融資維持率：照日期對齊（%d 天有值、%d 天留空且線斷開）、"
      "虛線獨立第 5 軸、不改變五票總分、沒有歷史檔時鎖住"
      % (len(_m_rows), len(_m_want) - len(_m_rows)))

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
if FRONT_READY:
    if scope_locked[1]["disabled"]:
        fails.append("歷史檔已經有近月欄位了，「近月」不該還鎖著")
else:
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

# ---------- 分頁切換 ----------
if not (tab_initial["chips"] and not tab_initial["macro"]):
    fails.append("預設應該停在籌碼 K 線分頁，實得 %s" % tab_initial)
if not tab_initial["controls"]:
    fails.append("籌碼分頁時控制列應該是顯示的")
if tab_initial["tvScripts"] != 0:
    fails.append("還沒切到總經分頁就載了 TradingView 的 script（應該延遲載入），實得 %d 支"
                 % tab_initial["tvScripts"])
if tab_initial["on"] != ["chips"] or tab_initial["selected"] != ["true", "false"]:
    fails.append("分頁按鈕的選中狀態不對：%s / %s"
                 % (tab_initial["on"], tab_initial["selected"]))

if not (tab_macro["macro"] and not tab_macro["chips"]):
    fails.append("切到總經之後應該只顯示總經分頁，實得 %s" % tab_macro)
if tab_macro["controls"]:
    fails.append("總經分頁時控制列應該收起來（那些控制項對這一頁沒有作用）")
if tab_macro["tvBoxes"] != 2:
    fails.append("總經分頁應該有兩個圖表容器（匯率、原油），實得 %d" % tab_macro["tvBoxes"])
if tab_macro["tvScripts"] != 2:
    fails.append("切到總經之後應該為兩個容器各掛一支 widget script，實得 %d"
                 % tab_macro["tvScripts"])
if tab_macro["on"] != ["macro"] or tab_macro["selected"] != ["false", "true"]:
    fails.append("切到總經後按鈕狀態不對：%s / %s"
                 % (tab_macro["on"], tab_macro["selected"]))

if not (tab_back["chips"] and not tab_back["macro"] and tab_back["controls"]):
    fails.append("切回籌碼分頁之後狀態不對：%s" % tab_back)
if opts_after_back <= opts_before_back:
    fails.append("切回籌碼分頁時應該重畫一次（隱藏容器會讓 ECharts 量到 0 寬），"
                 "但 setOption 沒有再被呼叫")
if tab_rows_after_back == 0:
    fails.append("切回籌碼分頁之後表格是空的")
print("分頁切換：預設籌碼頁、TradingView script 延遲到切過去才載（2 支）、"
      "總經頁收起控制列、切回來會重畫（setOption %d → %d 次）"
      % (opts_before_back, opts_after_back))

# ---------- 釘住當日明細 ----------
if not pin_before["hidden"]:
    fails.append("還沒點任何一天，釘住面板就已經出現了")
if not pin_before["tooltipShown"]:
    fails.append("沒釘住時 hover 的浮動提示不該被關掉")
if pin_open["hidden"]:
    fails.append("點了 K 線之後釘住面板應該出現")
elif _pin_date not in pin_open["text"]:
    fails.append("釘住面板顯示的不是點到的那天（期望 %s，實得開頭 %r）"
                 % (_pin_date, pin_open["text"][:40]))
if pin_open["tooltipShown"]:
    fails.append("釘住時應該關掉 hover 的浮動提示，不然兩張卡片會疊在一起")
if not pin_toggled["hidden"]:
    fails.append("再點同一天應該把釘住面板收起來")
if pin_toggled["tooltipShown"] is False:
    fails.append("取消釘住之後 hover 的浮動提示要回來")
if not pin_closed["hidden"]:
    fails.append("按 × 應該把釘住面板收起來")
if pin_moved["hidden"]:
    fails.append("點另一天應該改釘那天，而不是關掉")
elif pin_moved_date not in pin_moved["text"]:
    fails.append("點另一天之後面板沒換成那天（期望 %s）" % pin_moved_date)
if pin_after_win_visible:
    if pin_after_win["hidden"]:
        fails.append("%s 仍在最近 20 日的視窗內，切區間後不該收起來" % _pin_date)
    elif _pin_date not in pin_after_win["text"]:
        fails.append("切區間之後面板變成別天了——釘住的應該是日期而不是索引")
elif not pin_after_win["hidden"]:
    fails.append("%s 已經不在最近 20 日的視窗內，面板應該收起來而不是顯示別天的資料"
                 % _pin_date)
if not pin_after_esc["hidden"]:
    fails.append("按 Esc 應該把釘住面板收起來")
print("釘住當日明細：點開／再點收起／× 關閉／Esc 關閉／點別天換那天／"
      "切區間後落在視窗外就收起（釘的是日期不是索引）")

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
print("✅ 全部通過：%d 天 × 2 門檻 × 3 種計分模式（口徑 %s），表格 / tooltip / 圖表序列 / 背景色塊 / "
      "markLine / 副圖雙軸對齊 / 顏色與對比 / 偏好記憶 皆與 Python 獨立計算一致"
      % (len(chips), "近月" if PAGE_SCOPE == "front" else "所有契約"))
