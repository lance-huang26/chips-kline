/* 法人籌碼五票 × 個股 K 線疊圖
 * 所有分數都在瀏覽器端即時計算，改門檻不會重新載入頁面。
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- 設定
  // 以下都是預設值，載入 data/site.json 之後會被裡面的設定覆蓋。
  // 真正的設定在 config.json，由 build_site.py 寫進 site.json。
  var SETTLEMENT = [];     // 台指期結算日（build_site.py 依每月第三個星期三推算）
  var DEFAULT_THRESHOLD = -83000;
  var THRESHOLD_PRESETS = [-83000, -80000];
  var MA_PERIOD = 20;      // 乖離率的均線天數（月線）
  var BIAS_ALERT = 15;     // 正乖離超過這個百分比就標紅
  var WINDOW_OPTIONS = [20, 60, 120, 0];   // 0 = 全部
  var DEFAULT_WINDOW = 60;

  var CHIP_FIELDS = [
    { key: 'foreign_fut',    label: '外資台指期未平倉',   short: '外資期貨', color: '#2a78d6', axis: 'big'   },
    { key: 'top10_trader',   base: '前十大交易人', label: '前十大交易人', short: '前十大交易人', color: '#eb6834', axis: 'small' },
    { key: 'top10_specific', base: '前十大特定人', label: '前十大特定人', short: '前十大特定人', color: '#1baf7a', axis: 'small' },
    { key: 'foreign_opt',    label: '選擇權外資淨口數',   short: '選擇權外資', color: '#4a3aa7', axis: 'small' },
    { key: 'dealer_opt',     label: '選擇權自營商淨口數', short: '選擇權自營', color: '#e87ba4', axis: 'small' }
  ];

  var UP = '#d93b30', DOWN = '#12996b';
  var UP_SOFT = 'rgba(217,59,48,0.09)', DOWN_SOFT = 'rgba(18,153,107,0.09)';
  var SCORE_COLOR = '#2a78d6';
  var COMPARE_COLORS = ['#eb6834', '#1baf7a', '#eda100', '#4a3aa7', '#e87ba4', '#2a78d6'];

  // ---------------------------------------------------------------- 狀態
  var S = {
    site: null,            // data/site.json 的完整內容
    prices: null,          // {stocks:[{code,name,rows:[]}]}（rows 是全部歷史）
    allChips: [],          // 全部歷史的籌碼
    chips: [],             // 目前視窗內的籌碼（allChips 的尾段）
    startIdx: 0,           // 視窗起點在 allChips 裡的位置
    win: DEFAULT_WINDOW,   // 視窗天數，0 = 全部
    dates: [],
    primary: null,
    compare: false,
    threshold: DEFAULT_THRESHOLD,
    scope: 'all',          // 十大交易人口徑：'all' 所有契約 / 'front' 近月
    mode: 'level',         // 計分模式：'level' 剩餘口數 / 'flow' 今日操作 / 'blend' 加權
    shown: {},             // key -> bool
    bias: {}               // code -> { date: {ma, bias} }
  };
  CHIP_FIELDS.forEach(function (f) { S.shown[f.key] = true; });

  var chart = null;

  // ---------------------------------------------------------------- 工具
  function fmt(n, d) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('en-US', {
      minimumFractionDigits: d || 0, maximumFractionDigits: d || 0
    });
  }
  function sign(n) { return (n > 0 ? '+' : '') + n; }
  // 模式 1、2 的票是整數（+1 / -1），模式 3 是連續值（+0.43）。
  // 統一從這裡出去，免得有的地方顯示 0.4299999999999999。
  function fmtScore(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var r = Math.round(v * 100) / 100;
    return (r > 0 ? '+' : (r === 0 ? '' : '')) + (Number.isInteger(r) ? r : r.toFixed(2));
  }
  function modeLabel() {
    for (var i = 0; i < MODES.length; i++) if (MODES[i].key === S.mode) return MODES[i].label;
    return S.mode;
  }

  // 本日操作＝今日未平倉／淨口數 減 昨日。
  // 比較的對象是「歷史上的前一個交易日」而不是「視窗裡的前一列」，
  // 所以把區間切成最近 20 天時，第一天照樣算得出來。
  function dayDelta(i, key) {
    var g = S.startIdx + i;
    if (g === 0) return null;            // 整個歷史的第一天，真的沒有前一日
    return S.allChips[g][key] - S.allChips[g - 1][key];
  }
  function fmtDelta(d) {
    if (d === null) return '—';
    return (d > 0 ? '+' : '') + fmt(d);
  }
  function cls(n) { return n > 0 ? 'pos' : (n < 0 ? 'neg' : ''); }
  function el(id) { return document.getElementById(id); }

  /* 記住上次選的區間與標的。
   *
   * localStorage 在幾種情況下會直接丟例外（Safari 的無痕模式、瀏覽器設定擋掉
   * 網站資料、用 file:// 直開時的 opaque origin），而且讀回來的值可能是
   * 上一版留下的舊選項。所以讀寫都包 try/catch，而且讀回來的值一律要
   * 通過驗證才採用——存過的股票代碼可能已經從 config 移除了。
   *
   * 只記「區間」和「主要標的」這兩個純畫面偏好。門檻和十大口徑不記：
   * 那兩個會改變判讀結果，應該以 config.json 為準，不然哪天不小心點過一次，
   * 之後每次打開看到的都是自己都忘了設過的東西。
   */
  var PREF_KEY = 'chips-kline:ui';
  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(PREF_KEY)) || {};
    } catch (e) { return {}; }
  }
  function savePref(k, v) {
    try {
      var p = loadPrefs();
      p[k] = v;
      localStorage.setItem(PREF_KEY, JSON.stringify(p));
    } catch (e) { /* 存不了就算了，不影響任何功能 */ }
  }

  /* 十大交易人的兩種口徑。
   *
   * 期交所的大額交易人表格對 TX 會出好幾列，差在「到期月份」：
   *   999999 = 所有契約（含遠月）
   *   最小的真實月份 = 近月（市面上看盤 App 顯示的就是這個）
   *
   * 兩者差很多，2026/09/14 連正負號都相反（所有契約 +4,557 / 特定人 +1,705，
   * 近月 +549 / 特定人 -1,790）。所以兩份都存進歷史檔，由這裡決定畫面上用哪一份。
   *
   * 作法是在切視窗之前就把選中的那一份「蓋」到 top10_trader / top10_specific 上，
   * 後面的計分、副圖、表格、hover、本日操作一律不必知道有兩種口徑這回事。
   */
  var SCOPES = [
    { key: 'all',   label: '所有契約', note: '期交所「到期月份 999999」，含遠月部位。' },
    { key: 'front', label: '近月',     note: '當天最小的到期月份，和一般看盤 App 顯示的一致；結算後自動換月。' }
  ];
  function applyScope() {
    var front = (S.scope === 'front');
    // 標籤要跟著換。圖例、tooltip、表頭都只寫「前十大交易人」的話，
    // 截圖出去或隔天回來看，根本分不出當時看的是哪一種口徑。
    CHIP_FIELDS.forEach(function (f) {
      if (f.base) {
        f.label = f.base + (front ? '（近月）' : '（所有契約）');
        f.short = f.base + (front ? '·近月' : '·全部');
      }
    });
    S.allChips = S.site.chips.map(function (c) {
      if (!front) return c;
      var o = {};
      for (var k in c) { if (Object.prototype.hasOwnProperty.call(c, k)) o[k] = c[k]; }
      o.top10_trader = c.top10_trader_front;
      o.top10_specific = c.top10_specific_front;
      return o;
    });
  }
  // 近月欄位是後來才加的。舊資料沒有的話不能默默當 0——0 看起來很合理，
  // 混進去幾乎不可能被發現——所以直接把那個選項鎖起來，並說要跑什麼補。
  function frontReady() {
    return S.site.chips.length > 0 && S.site.chips.every(function (c) {
      return c.top10_trader_front !== null && c.top10_trader_front !== undefined &&
             c.top10_specific_front !== null && c.top10_specific_front !== undefined;
    });
  }

  // 依目前的視窗天數，從全部歷史裡切出要顯示的那一段
  function applyWindow() {
    var n = S.allChips.length;
    var keep = (S.win && S.win > 0) ? Math.min(S.win, n) : n;
    S.startIdx = n - keep;
    S.chips = S.allChips.slice(S.startIdx);
    S.dates = S.chips.map(function (c) { return c.date; });
  }

  // ---------------------------------------------------------------- 計分
  /* 三種計分模式，分數尺度一致：每欄 -1 ~ +1、總分 -5 ~ +5。
   * 尺度相同是刻意的——圖表的軸、背景色塊、統計卡都不必因為換模式而改。
   *
   *   level  水位：看剩餘口數的正負（原本的算法）
   *   flow   操作：看今日操作（今日 − 昨日）的正負
   *   blend  權重：每欄拆成水位 0.5 + 操作 0.5
   */
  var MODES = [
    { key: 'level', label: '剩餘口數', note: '五張票各看剩餘口數的正負，±1。' },
    { key: 'flow',  label: '今日操作', note: '五張票各看今日操作（今日 − 昨日）的正負，±1。' },
    { key: 'blend', label: '加權',     note: '每欄拆成水位 0.5 ＋ 操作 0.5，分數連續。' }
  ];

  /* 模式 3 的算法（用 2026/05~09 的實際資料調出來的，理由見 README）：
   *
   *   u    = (今天的值 − 中性點) ÷ 窗內穩健標準差      ← 「是平常波動的幾倍」
   *   水位分 = curve(u) × 0.5
   *   操作分 = curve(今日變動 ÷ 變動的穩健標準差) × 0.5
   *
   * 中性點分欄設定，這是關鍵：外資期貨在 87 天裡「一天都沒有正過」
   * （平均 -75,407），選擇權外資也只有 5%。對這兩欄用 0 當中性點的話，
   * 水位分會永遠卡在 -0.41 ~ -0.50，標準差只有 0.026——那是常數不是分數，
   * 那一票等於完全失效。所以它們改用窗內中位數當中性點。
   *
   * 尺度用 MAD×1.4826 而不是標準差：籌碼資料有厚尾，而且結算換月會跳空，
   * 一天的極端值就會把標準差拉大、讓之後 N 天的分數全部縮水。
   */
  var SCORE_WINDOW = 20;
  var SCORE_CURVE = 'tanh';    // 'tanh' | 'clamp2' | 'clamp3'
  var CURVE_K = 2.5;           // tanh 的緩和係數
  // 中性點用 0 的欄位＝實際上會繞著 0 擺動的（>0 的天數約 48%~63%）。
  // 沒列在這裡的用窗內中位數。
  var ZERO_CENTERED = { top10_trader: 1, top10_specific: 1, dealer_opt: 1 };

  function curve(u) {
    if (!isFinite(u)) return 0;
    if (SCORE_CURVE === 'clamp2') return Math.max(-1, Math.min(1, u / 2)) * 0.5;
    if (SCORE_CURVE === 'clamp3') return Math.max(-1, Math.min(1, u / 3)) * 0.5;
    return Math.tanh(u / CURVE_K) * 0.5;      // 不封頂：4.5 倍和 3.3 倍仍分得出來
  }
  function median(a) {
    var b = a.slice().sort(function (x, y) { return x - y; }), n = b.length;
    if (!n) return 0;
    return n % 2 ? b[(n - 1) / 2] : (b[n / 2 - 1] + b[n / 2]) / 2;
  }
  function madSigma(a, center) {
    var c = (center === undefined) ? median(a) : center;
    var s = 1.4826 * median(a.map(function (v) { return Math.abs(v - c); }));
    if (s > 0) return s;
    // 窗內超過一半的值相同時 MAD 會是 0，退回平均絕對差；再不行就放棄這一天
    var m = a.reduce(function (t, v) { return t + Math.abs(v - c); }, 0) / (a.length || 1);
    return m > 0 ? m : 0;
  }

  // 對整個歷史算分（模式 2、3 需要更早的資料當暖身，不能只看視窗內）
  function scoreHistory(threshold) {
    var n = S.allChips.length, out = new Array(n), i, j, f;
    var W = SCORE_WINDOW;

    for (i = 0; i < n; i++) {
      var c = S.allChips[i], v = [], ok = true;

      for (j = 0; j < CHIP_FIELDS.length; j++) {
        f = CHIP_FIELDS[j];
        var x = c[f.key];
        if (x === null || x === undefined) { ok = false; break; }

        if (S.mode === 'level') {
          v.push(x > (f.key === 'foreign_fut' ? threshold : 0) ? 1 : -1);
          continue;
        }
        if (i === 0) { ok = false; break; }          // 沒有前一日就沒有「今日操作」
        var d = x - S.allChips[i - 1][f.key];

        if (S.mode === 'flow') { v.push(d > 0 ? 1 : -1); continue; }

        // --- 模式 3 ---
        if (i < W + 1) { ok = false; break; }        // 暖身不足
        var lw = [], dw = [], g;
        for (g = i - W; g < i; g++) {
          lw.push(S.allChips[g][f.key]);
          dw.push(S.allChips[g][f.key] - S.allChips[g - 1][f.key]);
        }
        var center = ZERO_CENTERED[f.key] ? 0 : median(lw);
        var sLv = madSigma(lw), sFl = madSigma(dw, 0);
        if (!(sLv > 0) || !(sFl > 0)) { ok = false; break; }
        v.push(curve((x - center) / sLv) + curve(d / sFl));
      }

      out[i] = ok ? {
        votes: v,
        fut: v[0] + v[1] + v[2],
        opt: v[3] + v[4],
        total: v[0] + v[1] + v[2] + v[3] + v[4]
      } : null;
    }
    return out;
  }
  function scoreAll(threshold) {
    return scoreHistory(threshold).slice(S.startIdx);
  }

  // 取一個「好看的」刻度間距：1/1.5/2/2.5/3/4/5/6/8 ×10^k 之中，>= x 的最小值
  var NICE = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
  function niceStep(x) {
    if (!(x > 0)) return 1;
    var e = Math.pow(10, Math.floor(Math.log(x) / Math.LN10));
    var m = x / e;
    for (var i = 0; i < NICE.length; i++) {
      if (m <= NICE[i] + 1e-9) return NICE[i] * e;
    }
    return 10 * e;
  }

  /* 副圖雙軸對齊。
   * foreign_fut 的量級（約 -9 萬）和其他四項（數千）差太多，必須用獨立右軸；
   * 但兩軸各自 auto-scale 的話，「外資是否站上門檻」和「其他項是否為正」
   * 在畫面上會落在不同高度，看起來對不起來。
   *
   * 這裡把兩軸都切成固定 N 段，並讓右軸的「門檻」和左軸的「0」
   * 落在同一條格線（由下往上數第 k 條）上，所以：
   *   - 兩軸格線完全重疊
   *   - 那條線以上 = 該票投 +1，以下 = -1，五條線讀法一致
   * k 由左軸資料的正負比例決定（挑總範圍最小的那個），右軸再跟著它算。
   */
  var AXIS_SPLITS = 4;
  function subAxisRanges() {
    var N = AXIS_SPLITS;

    // 左軸：目前有勾選的「數千」等級欄位
    var lVals = [];
    CHIP_FIELDS.forEach(function (f) {
      if (f.axis === 'small' && S.shown[f.key]) {
        S.chips.forEach(function (c) { lVals.push(c[f.key]); });
      }
    });
    var lMin = lVals.length ? Math.min.apply(null, lVals) : -1;
    var lMax = lVals.length ? Math.max.apply(null, lVals) : 1;
    var lPad = 0.08 * Math.max(lMax - lMin, 1);
    var lo = Math.min(lMin - lPad, 0), hi = Math.max(lMax + lPad, 0);

    var best = null;
    for (var k = 1; k <= N - 1; k++) {
      var iv = niceStep(Math.max(-lo / k, hi / (N - k)));
      if (!best || iv * N < best.iv * N) best = { k: k, iv: iv };
    }

    // 右軸：門檻擺在同一條格線上，往上往下各留足夠容納資料的空間
    var rVals = S.chips.map(function (c) { return c.foreign_fut; });
    var rMin = Math.min.apply(null, rVals), rMax = Math.max.apply(null, rVals);
    var rPad = 0.08 * Math.max(rMax - rMin, 1);
    var below = Math.max(S.threshold - rMin, 0) + rPad;
    var above = Math.max(rMax - S.threshold, 0) + rPad;
    var ivR = niceStep(Math.max(below / best.k, above / (N - best.k)));

    return {
      k: best.k, n: N,
      left:  { min: -best.k * best.iv, max: (N - best.k) * best.iv, interval: best.iv },
      right: { min: S.threshold - best.k * ivR, max: S.threshold + (N - best.k) * ivR, interval: ivR }
    };
  }

  /* 乖離率 = (收盤 − MA) / MA × 100%
   * MA 用「往前推 MA_PERIOD 個有交易的收盤價」算，所以 8/3 的 MA20 需要 7 月的資料，
   * prices.json 裡每檔股票的 warmup 就是為此準備的（只有日期與收盤價）。
   * 停牌／無交易的日子不佔均線的位置，也不會產生乖離率。
   */
  function computeBias() {
    var out = {};
    S.prices.stocks.forEach(function (st) {
      var closes = [], dates = [];
      (st.warmup || []).forEach(function (w) {
        if (w.close !== null && w.close !== undefined) { closes.push(w.close); dates.push(w.date); }
      });
      var byDate = {};
      st.rows.forEach(function (r) {
        if (!r.valid) return;
        closes.push(r.close); dates.push(r.date);
        var n = closes.length;
        if (n < MA_PERIOD) { byDate[r.date] = { ma: null, bias: null }; return; }
        var sum = 0;
        for (var i = n - MA_PERIOD; i < n; i++) sum += closes[i];
        var ma = sum / MA_PERIOD;
        byDate[r.date] = { ma: ma, bias: ma ? (r.close - ma) / ma * 100 : null };
      });
      out[st.code] = byDate;
    });
    return out;
  }
  function biasAt(code, date) {
    var b = S.bias[code];
    return (b && b[date]) ? b[date] : { ma: null, bias: null };
  }
  function biasClass(bias) {
    if (bias === null || bias === undefined) return '';
    return bias > BIAS_ALERT ? 'hot' : '';
  }
  function fmtBias(bias) {
    if (bias === null || bias === undefined) return '—';
    return (bias > 0 ? '+' : '') + bias.toFixed(2) + '%';
  }

  function stockByCode(code) {
    for (var i = 0; i < S.prices.stocks.length; i++) {
      if (S.prices.stocks[i].code === code) return S.prices.stocks[i];
    }
    return null;
  }
  // 依 S.dates 對齊，缺的日子回傳 null（K 棒留空，不補值）
  function alignedRows(stock) {
    var map = {};
    stock.rows.forEach(function (r) { map[r.date] = r; });
    return S.dates.map(function (d) {
      var r = map[d];
      return (r && r.valid) ? r : null;
    });
  }

  // ---------------------------------------------------------------- 載入
  function loadData() {
    return fetch('data/site.json')
      .then(function (r) { if (!r.ok) throw new Error('site'); return r.json(); })
      .catch(function () {
        // file:// 直開時 fetch 會被 CORS 擋掉 → 用 data/site.js 的離線備援
        if (window.__SITE__) return window.__SITE__;
        throw new Error('NO_DATA');
      });
  }

  // ---------------------------------------------------------------- 交易日檢查
  function checkAlignment() {
    // 只檢查「有籌碼的那些日子有沒有對應的股價」。
    // 股價會比籌碼多出前面幾個月（均線暖身用），那是正常的，不算對不上。
    //
    // 兩種情況要分開看：
    //   完全沒有股價 → 多半是剛加進 config、還沒補抓，屬於已知的過渡狀態，
    //                  用淡色提示就好，不要跳紅色錯誤框。
    //   有股價但缺幾天 → 這才是真的資料不一致，要顯眼。
    var chipDates = S.allChips.map(function (c) { return c.date; });
    var msgs = [], pending = [];
    S.prices.stocks.forEach(function (st) {
      if (!st.rows.length) { pending.push(st.code + ' ' + st.name); return; }
      var pd = {};
      st.rows.forEach(function (r) { pd[r.date] = true; });
      var onlyChip = chipDates.filter(function (d) { return !pd[d]; });
      if (onlyChip.length) {
        msgs.push(st.code + ' ' + st.name + '：有籌碼但缺股價的日子 [' +
                  onlyChip.slice(0, 8).join(', ') +
                  (onlyChip.length > 8 ? ' …共 ' + onlyChip.length + ' 天' : '') + ']');
      }
    });

    var note = el('dataNote');
    if (note) {
      note.textContent = pending.length
        ? '尚未補抓股價：' + pending.join('、') +
          '（跑 fetch_prices.py --months-back 6，或在 Actions 手動觸發並填 prices_months）'
        : '';
    }
    if (msgs.length) {
      var box = el('loadErr');
      box.hidden = false;
      box.innerHTML = '<b>交易日對不上：</b><br>' + msgs.join('<br>');
    }
    return msgs;
  }

  // ---------------------------------------------------------------- 控制列
  function buildControls() {
    var wseg = el('winSeg');
    wseg.innerHTML = '';
    WINDOW_OPTIONS.forEach(function (n) {
      var b = document.createElement('button');
      b.textContent = n > 0 ? ('最近 ' + n + ' 日') : '全部';
      b.dataset.win = n;
      b.className = (n === S.win) ? 'on' : '';
      b.onclick = function () {
        S.win = n;
        savePref('win', n);
        Array.prototype.forEach.call(wseg.children, function (c) {
          c.className = (Number(c.dataset.win) === S.win) ? 'on' : '';
        });
        applyWindow();
        render();
      };
      wseg.appendChild(b);
    });

    var seg = el('stockSeg');
    seg.innerHTML = '';
    S.prices.stocks.forEach(function (st) {
      var b = document.createElement('button');
      b.textContent = st.code + ' ' + st.name;
      b.dataset.code = st.code;
      b.className = (st.code === S.primary) ? 'on' : '';
      b.onclick = function () {
        S.primary = st.code;
        savePref('stock', st.code);
        Array.prototype.forEach.call(seg.children, function (c) {
          c.className = (c.dataset.code === S.primary) ? 'on' : '';
        });
        render();
      };
      seg.appendChild(b);
    });

    var modeSeg = el('modeSeg'), modeNote = el('modeNote');
    modeSeg.innerHTML = '';
    MODES.forEach(function (m) {
      var b = document.createElement('button');
      b.textContent = m.label;
      b.dataset.mode = m.key;
      b.className = (S.mode === m.key) ? 'on' : '';
      b.onclick = function () {
        S.mode = m.key;
        Array.prototype.forEach.call(modeSeg.children, function (c) {
          c.className = (c.dataset.mode === S.mode) ? 'on' : '';
        });
        showModeNote();
        render();
      };
      modeSeg.appendChild(b);
    });
    function showModeNote() {
      var m = MODES.filter(function (x) { return x.key === S.mode; })[0];
      var txt = m.note;
      if (S.mode === 'blend') {
        txt += ' 水位分與操作分都先除以該欄近 ' + SCORE_WINDOW +
               ' 日的穩健標準差（MAD×1.4826），再經 ' +
               (SCORE_CURVE === 'tanh' ? 'tanh（不封頂，越極端加分越少）'
                                       : SCORE_CURVE.replace('clamp', '') + 'σ 封頂') +
               ' 換成分數。外資期貨與選擇權外資幾乎不會由負翻正，中性點改用窗內中位數；' +
               '其餘三欄用 0。前 ' + (SCORE_WINDOW + 1) + ' 天暖身不足，顯示 —。';
      }
      // 門檻只有模式 1 會用到，其他模式直接把整塊停用，免得以為調了有效
      var off = (S.mode !== 'level');
      var blk = el('thrBlock');
      if (blk) {
        blk.style.opacity = off ? '0.45' : '';
        Array.prototype.forEach.call(blk.querySelectorAll('input,button'), function (c) {
          c.disabled = off;
        });
      }
      modeNote.innerHTML = txt;
    }
    showModeNote();

    var scopeSeg = el('scopeSeg'), scopeNote = el('scopeNote'), ready = frontReady();
    scopeSeg.innerHTML = '';
    SCOPES.forEach(function (sp) {
      var b = document.createElement('button');
      b.textContent = sp.label;
      b.dataset.scope = sp.key;
      b.className = (S.scope === sp.key) ? 'on' : '';
      if (sp.key === 'front' && !ready) {
        b.disabled = true;
        b.title = '歷史檔還沒有近月欄位，先跑 python3 backfill_large.py';
      }
      b.onclick = function () {
        S.scope = sp.key;
        Array.prototype.forEach.call(scopeSeg.children, function (c) {
          c.className = (c.dataset.scope === S.scope) ? 'on' : '';
        });
        applyScope();
        applyWindow();
        showScopeNote();
        render();
      };
      scopeSeg.appendChild(b);
    });
    function showScopeNote() {
      var sp = SCOPES.filter(function (x) { return x.key === S.scope; })[0];
      var txt = sp.note;
      if (!ready) {
        txt += ' 近月欄位在歷史檔裡還是空的——跑 <code>python3 backfill_large.py</code> 補齊後才能切換。';
      } else {
        var last = S.site.chips[S.site.chips.length - 1];
        txt += ' 最新一天（' + last.date + '）兩種口徑：所有契約 ' +
               sign(last.top10_trader) + ' / ' + sign(last.top10_specific) +
               '，近月 ' + sign(last.top10_trader_front) + ' / ' +
               sign(last.top10_specific_front) + '（全體／特定人）。';
      }
      scopeNote.innerHTML = txt;
    }
    showScopeNote();

    var g = el('chipChks');
    g.innerHTML = '';
    CHIP_FIELDS.forEach(function (f) {
      var lab = document.createElement('label');
      var cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = true;
      cb.onchange = function () { S.shown[f.key] = cb.checked; render(); };
      var sw = document.createElement('span');
      sw.className = 'swatch'; sw.style.background = f.color;
      lab.appendChild(cb); lab.appendChild(sw);
      lab.appendChild(document.createTextNode(f.label));
      g.appendChild(lab);
    });

    var rng = el('thrRange'), num = el('thrNum');
    function setThr(v, from) {
      v = Math.round(Number(v));
      if (isNaN(v)) return;
      S.threshold = v;
      if (from !== 'range') rng.value = v;
      if (from !== 'num') num.value = v;
      Array.prototype.forEach.call(document.querySelectorAll('button.preset'), function (b) {
        b.classList.toggle('on', Number(b.dataset.v) === v);
      });
      render();
    }
    rng.oninput = function () { setThr(rng.value, 'range'); };
    num.oninput = function () { setThr(num.value, 'num'); };
    Array.prototype.forEach.call(document.querySelectorAll('button.preset'), function (b) {
      b.onclick = function () { setThr(b.dataset.v, 'preset'); };
    });
    setThr(DEFAULT_THRESHOLD, 'init');

    el('compareChk').onchange = function () { S.compare = this.checked; render(); };
  }

  // ---------------------------------------------------------------- 統計條 / 門檻比較
  function renderStats(sc) {
    // 算不出來的日子（模式 2 的第一天、模式 3 暖身不足）一律不計入統計，
    // 不能當成 0——0 是「多空剛好抵銷」，和「算不出來」是兩件事。
    var have = sc.filter(function (s) { return s; });
    var pos = 0, neg = 0, zero = 0;
    have.forEach(function (s) { if (s.total > 0) pos++; else if (s.total < 0) neg++; else zero++; });

    var p0 = THRESHOLD_PRESETS[0], p1 = THRESHOLD_PRESETS[1];
    var diff = [];
    if (S.mode === 'level') {          // 門檻只影響第 1 票，其他模式根本用不到
      var a = scoreAll(p0), b = scoreAll(p1);
      for (var i = 0; i < a.length; i++) {
        if (a[i] && b[i] && a[i].total !== b[i].total) diff.push(S.dates[i]);
      }
    }

    var st = stockByCode(S.primary);
    // 報酬率只看目前視窗內的期間，不是全部歷史
    var rows = alignedRows(st).filter(function (r) { return r; });
    var ret = rows.length > 1
      ? (rows[rows.length - 1].close / rows[0].close - 1) * 100 : null;

    el('stats').innerHTML = [
      card('總分 > 0 天數', pos + '<small>/ ' + have.length + '</small>', pos ? 'pos' : ''),
      card('總分 < 0 天數', neg + '<small>/ ' + have.length + '</small>', neg ? 'neg' : ''),
      card('總分 = 0 天數', String(zero), ''),
      card('平均總分',
           have.length ? (have.reduce(function (t, s) { return t + s.total; }, 0) / have.length).toFixed(2) : '—',
           have.length ? cls(have.reduce(function (t, s) { return t + s.total; }, 0)) : ''),
      card(st.code + ' ' + st.name + ' 區間報酬',
           (ret === null ? '—' : (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%'),
           ret === null ? '' : cls(ret)),
      (S.mode === 'level'
        ? card('門檻 ' + fmt(p0) + ' vs ' + fmt(p1), diff.length + '<small> 天不同</small>', '')
        : card('可計分天數', have.length + '<small>/ ' + sc.length + '</small>',
               have.length === sc.length ? '' : 'pending'))
    ].join('');

    if (S.mode !== 'level') {
      el('thrNote').innerHTML =
        '目前是「' + modeLabel() + '」模式，<b>門檻只影響「剩餘口數」模式的第 1 票</b>，這裡不會用到。' +
        (have.length < sc.length
          ? ' 另外有 <b>' + (sc.length - have.length) + '</b> 天因為暖身不足算不出分數，顯示為 —。'
          : '');
      return;
    }
    var few = diff.length <= 12;
    el('thrNote').innerHTML = diff.length
      ? '目前 <b>' + fmt(p0) + '</b> 與 <b>' + fmt(p1) + '</b> 兩個門檻，總分不同的有 <b>' +
        diff.length + '</b> 天' +
        (few ? '：' + diff.join('、') : '（區間較長，日期請看下方表格）') +
        '（差異全部來自第 1 票的正負翻轉）。'
      : '<b>' + fmt(p0) + '</b> 與 <b>' + fmt(p1) + '</b> 兩個門檻在本區間內判讀完全相同。';
  }
  function card(k, v, c) {
    return '<div class="stat"><div class="k">' + k + '</div><div class="v ' + (c || '') + '">' + v + '</div></div>';
  }

  // ---------------------------------------------------------------- 圖表
  function buildOption(sc) {
    var dates = S.dates;
    var priceSeries = [];
    var st = stockByCode(S.primary);

    if (!S.compare) {
      var rows = alignedRows(st);
      priceSeries.push({
        name: st.code + ' ' + st.name,
        type: 'candlestick',
        xAxisIndex: 0, yAxisIndex: 0,
        data: rows.map(function (r) { return r ? [r.open, r.close, r.low, r.high] : ['-', '-', '-', '-']; }),
        itemStyle: {                       // 台股慣例：紅漲綠跌
          color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN, borderWidth: 1
        },
        z: 4
      });
    } else {
      S.prices.stocks.forEach(function (s2, i) {
        var rows = alignedRows(s2), base = null;
        var data = rows.map(function (r) {
          if (!r) return null;
          if (base === null) base = r.close;
          return +(r.close / base * 100).toFixed(2);
        });
        priceSeries.push({
          name: s2.code + ' ' + s2.name,
          type: 'line', xAxisIndex: 0, yAxisIndex: 0,
          data: data, showSymbol: false, connectNulls: false,
          lineStyle: { width: 2 },
          // 五個顏色都驗過色盲可辨（最差相鄰 ΔE 9.2 deutan）；
          // 藍色刻意排最後，前四檔才不會撞到右軸總分線的藍
          color: COMPARE_COLORS[i % COMPARE_COLORS.length],
          emphasis: { focus: 'series' }, z: 4
        });
      });
    }

    // 總分為正 → 淡紅底；為負 → 淡綠底。
    // 註：category 軸上 markArea 的座標會被四捨五入到整數格、只能中心對中心，
    //     做不出「剛好一整格」的區塊，所以背景改用兩支滿格 bar（上下各半）來畫。
    var bgUp = [], bgDn = [];
    sc.forEach(function (s) {
      var col = !s ? 'transparent'
              : (s.total > 0 ? UP_SOFT : (s.total < 0 ? DOWN_SOFT : 'transparent'));
      bgUp.push({ value: 5, itemStyle: { color: col } });
      bgDn.push({ value: -5, itemStyle: { color: col } });
    });
    function bgSeries(name, data) {
      return {
        name: name, type: 'bar', xAxisIndex: 0, yAxisIndex: 1, data: data,
        barWidth: '100%', barCategoryGap: '0%', barGap: '-100%',
        silent: true, z: 0, animation: false, emphasis: { disabled: true }
      };
    }

    var mlData = [{
      yAxis: 0, label: { show: false },
      lineStyle: { type: 'dashed', color: '#8b8a84', width: 1.5 }
    }];
    SETTLEMENT.forEach(function (d) {
      if (dates.indexOf(d) >= 0) {
        mlData.push({
          xAxis: d,
          label: { show: true, formatter: '結算日', position: 'insideEndTop',
                   color: '#8a6d1f', fontSize: 11, backgroundColor: 'rgba(255,255,255,.85)', padding: [2, 4] },
          lineStyle: { type: 'dashed', color: '#d9a520', width: 1.5 }
        });
      }
    });

    var scoreSeries = {
      name: '五票總分',
      // 模式 1、2 的總分是整數、階梯線才對；模式 3 是連續值，階梯會看起來很怪
      type: 'line', step: (S.mode === 'blend' ? false : 'middle'),
      xAxisIndex: 0, yAxisIndex: 1,
      data: sc.map(function (s) { return s ? s.total : null; }),
      symbol: 'circle', symbolSize: S.mode === 'blend' ? 5 : 8, showSymbol: true,
      lineStyle: { width: 2, color: SCORE_COLOR },
      itemStyle: { color: SCORE_COLOR, borderColor: '#fcfcfb', borderWidth: 2 },
      connectNulls: false,          // 暖身不足的那段要斷開，不要連成一條假線
      z: 6,
      markLine: { silent: true, symbol: 'none', data: mlData, animation: false }
    };

    var sub = subAxisRanges();

    // 副圖的對齊基準線：左軸 0 == 右軸門檻，兩者在同一個高度
    var subGuide = {
      name: '__sub_guide', type: 'line', xAxisIndex: 1, yAxisIndex: 2,
      data: S.dates.map(function () { return null; }),
      silent: true, showSymbol: false, z: 0,
      markLine: {
        silent: true, symbol: 'none', animation: false,
        data: [{
          yAxis: 0,
          lineStyle: { type: 'dashed', color: '#8b8a84', width: 1.5 },
          label: {
            show: true, position: 'insideStartTop', fontSize: 10.5, color: '#6e6c66',
            backgroundColor: 'rgba(252,252,251,.85)', padding: [1, 4],
            formatter: '左軸 0 ＝ 右軸門檻 ' + fmt(S.threshold)
          }
        }]
      }
    };

    var chipSeries = CHIP_FIELDS.filter(function (f) { return S.shown[f.key]; }).map(function (f) {
      return {
        name: f.label,
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: f.axis === 'big' ? 3 : 2,
        data: S.chips.map(function (c) { return c[f.key]; }),
        symbol: 'circle', symbolSize: 5, showSymbol: false,
        lineStyle: { width: 2, color: f.color },
        itemStyle: { color: f.color },
        emphasis: { focus: 'series' }
      };
    });

    var legendData = priceSeries.map(function (s) { return s.name; })
      .concat(['五票總分']);

    return {
      backgroundColor: '#fcfcfb',
      animation: false,
      textStyle: { fontFamily: '"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif' },
      legend: [
        { data: legendData, top: 6, left: 12, itemGap: 16, textStyle: { fontSize: 12, color: '#52514e' } },
        { data: chipSeries.map(function (s) { return s.name; }), top: 546, left: 12,
          itemGap: 14, textStyle: { fontSize: 11.5, color: '#52514e' } }
      ],
      grid: [
        { left: 72, right: 62, top: 46, height: 470 },
        { left: 72, right: 62, top: 580, height: 150 }
      ],
      axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#52514e' } },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: 'rgba(255,255,255,.97)',
        borderColor: '#d8d8d3', borderWidth: 1,
        padding: 0,
        textStyle: { color: '#0b0b0b', fontSize: 12 },
        extraCssText: 'box-shadow:0 4px 16px rgba(0,0,0,.14);border-radius:6px;',
        formatter: function (params) {
          var p = Array.isArray(params) ? params[0] : params;
          if (!p) return '';
          return tooltipHtml(p.dataIndex, sc);
        }
      },
      xAxis: [
        { type: 'category', gridIndex: 0, data: dates, boundaryGap: true,
          axisLine: { lineStyle: { color: '#c9c9c3' } },
          axisTick: { show: false },
          axisLabel: { color: '#807e78', fontSize: 11, formatter: shortDate },
          splitLine: { show: false } },
        { type: 'category', gridIndex: 1, data: dates, boundaryGap: true,
          axisLine: { lineStyle: { color: '#c9c9c3' } },
          axisTick: { show: false },
          axisLabel: { color: '#807e78', fontSize: 11, formatter: shortDate },
          splitLine: { show: false } }
      ],
      yAxis: [
        { // 左：股價 / 指數化收盤
          type: 'value', gridIndex: 0, scale: true,
          name: S.compare ? '收盤指數（基準=100）' : '股價',
          nameTextStyle: { color: '#807e78', fontSize: 11, align: 'left' },
          nameGap: 14,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#807e78', fontSize: 11 },
          splitLine: { lineStyle: { color: '#efefec' } }
        },
        { // 右：五票總分
          type: 'value', gridIndex: 0, min: -5, max: 5, interval: 1,
          name: '五票總分', position: 'right',
          nameTextStyle: { color: SCORE_COLOR, fontSize: 11, align: 'right' },
          nameGap: 14,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: SCORE_COLOR, fontSize: 11 },
          splitLine: { show: false }
        },
        { // 副圖左：其他四項（口）。min/max/interval 由 subAxisRanges() 算，和右軸共用格線
          type: 'value', gridIndex: 1, name: '口（前十大／選擇權）',
          min: sub.left.min, max: sub.left.max, interval: sub.left.interval,
          nameTextStyle: { color: '#807e78', fontSize: 10.5, align: 'left' }, nameGap: 12,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#807e78', fontSize: 10.5 },
          splitLine: { lineStyle: { color: '#efefec' } }
        },
        { // 副圖右：外資台指期。門檻對齊左軸的 0，段數與左軸相同故格線重疊
          type: 'value', gridIndex: 1, position: 'right',
          min: sub.right.min, max: sub.right.max, interval: sub.right.interval,
          name: '口（外資期貨，門檻對齊左軸 0）',
          nameTextStyle: { color: '#2a78d6', fontSize: 10.5, align: 'right' }, nameGap: 12,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#2a78d6', fontSize: 10.5 },
          splitLine: { show: false }
        }
      ],
      series: [bgSeries('__bg_up', bgUp), bgSeries('__bg_dn', bgDn)]
        .concat(priceSeries, [scoreSeries, subGuide], chipSeries)
    };
  }

  function shortDate(d) { return d.slice(5); }

  function tooltipHtml(idx, sc) {
    var d = S.dates[idx], c = S.chips[idx], s = sc[idx];
    var st = stockByCode(S.primary);
    var rows = alignedRows(st), r = rows[idx];
    var isSettle = SETTLEMENT.indexOf(d) >= 0;

    var h = '<div style="padding:9px 11px 4px;border-bottom:1px solid #ececE8">' +
            '<b style="font-size:12.5px">' + d + '</b>' +
            (isSettle ? '<span style="margin-left:7px;font-size:11px;color:#8a6d1f;background:#fdf3d8;padding:1px 5px;border-radius:3px">結算日</span>' : '') +
            '</div>';

    // 價格
    h += '<div style="padding:7px 11px;border-bottom:1px solid #ececE8">' +
         '<div style="color:#807e78;font-size:11px;margin-bottom:3px">' + st.code + ' ' + st.name + '</div>';
    if (r) {
      var chg = (r.change === null || r.change === undefined) ? null : r.change;
      var prev = (chg === null) ? null : r.close - chg;
      var pct = (prev && prev !== 0) ? (chg / prev * 100) : null;
      h += '<table style="font-size:11.5px;border-spacing:0"><tr>' +
           tdKV('開', r.open) + tdKV('高', r.high) + tdKV('低', r.low) +
           '<td style="padding:0 6px 0 0"><span style="color:#807e78">收</span> <b style="color:' +
             (chg > 0 ? UP : chg < 0 ? DOWN : '#0b0b0b') + '">' + fmt(r.close, 2) + '</b></td>' +
           '</tr></table>' +
           (pct === null ? '' : '<div style="font-size:11.5px;color:' + (chg > 0 ? UP : chg < 0 ? DOWN : '#52514e') +
             '">' + (chg > 0 ? '+' : '') + fmt(chg, 2) + '（' + (pct > 0 ? '+' : '') + pct.toFixed(2) + '%）</div>');
    } else {
      h += '<div style="font-size:11.5px;color:#807e78">當日無交易資料（K 棒留空）</div>';
    }
    h += '</div>';

    // 三檔的乖離率（相對 MA20），正乖離超過門檻標紅
    h += '<div style="padding:7px 11px;border-bottom:1px solid #ececE8">' +
         '<div style="color:#807e78;font-size:11px;margin-bottom:3px">乖離率（MA' + MA_PERIOD +
         '，>' + BIAS_ALERT + '% 標紅）</div>' +
         '<table style="font-size:11.5px;border-spacing:0">' +
         '<tr style="color:#a09e97;font-size:10.5px"><td></td>' +
         '<td style="padding:0 8px 2px 0;text-align:right">收盤</td>' +
         '<td style="padding:0 8px 2px 0;text-align:right">MA' + MA_PERIOD + '</td>' +
         '<td style="padding:0 0 2px;text-align:right">乖離率</td></tr>';
    S.prices.stocks.forEach(function (s2) {
      var r2 = alignedRows(s2)[idx];
      var bi = biasAt(s2.code, d);
      var hot = bi.bias !== null && bi.bias > BIAS_ALERT;
      h += '<tr' + (s2.code === S.primary ? ' style="font-weight:600"' : '') + '>' +
        '<td style="padding:1px 8px 1px 0">' + s2.code + ' ' + s2.name + '</td>' +
        '<td style="padding:1px 8px 1px 0;text-align:right;font-variant-numeric:tabular-nums">' +
          (r2 ? fmt(r2.close, 2) : '—') + '</td>' +
        '<td style="padding:1px 8px 1px 0;text-align:right;font-variant-numeric:tabular-nums;color:#807e78">' +
          (bi.ma === null ? '—' : fmt(bi.ma, 2)) + '</td>' +
        '<td style="padding:1px 0;text-align:right;font-variant-numeric:tabular-nums;' +
          (hot ? 'color:' + UP + ';font-weight:700'
               : 'color:' + (bi.bias === null ? '#a09e97' : bi.bias < 0 ? DOWN : '#52514e')) + '">' +
          fmtBias(bi.bias) + (hot ? ' ▲' : '') + '</td>' +
        '</tr>';
    });
    h += '</table></div>';

    // 五票
    h += '<div style="padding:7px 11px 3px"><table style="font-size:11.5px;border-spacing:0">';
    h += '<tr style="color:#a09e97;font-size:10.5px"><td></td>' +
         '<td style="padding:0 8px 2px 0;text-align:right">口數</td>' +
         '<td style="padding:0 8px 2px 0;text-align:right">本日操作</td>' +
         '<td style="padding:0 0 2px;text-align:right">票</td></tr>';
    CHIP_FIELDS.forEach(function (f, i) {
      var v = s ? s.votes[i] : null, dd = dayDelta(idx, f.key);
      h += '<tr>' +
        '<td style="padding:1px 8px 1px 0"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:' +
          f.color + ';margin-right:6px"></span>' + f.label + '</td>' +
        '<td style="padding:1px 8px 1px 0;text-align:right;font-variant-numeric:tabular-nums">' + fmt(c[f.key]) + '</td>' +
        '<td style="padding:1px 8px 1px 0;text-align:right;font-variant-numeric:tabular-nums;color:' +
          (dd === null ? '#a09e97' : dd > 0 ? UP : dd < 0 ? DOWN : '#52514e') + '">' + fmtDelta(dd) + '</td>' +
        '<td style="padding:1px 0;text-align:right;font-weight:600;color:' +
          (v === null ? '#a09e97' : v > 0 ? UP : v < 0 ? DOWN : '#52514e') + '">' + fmtScore(v) + '</td>' +
        '</tr>';
    });
    h += '</table></div>';

    // 小計
    h += '<div style="padding:5px 11px 9px;border-top:1px solid #ececE8;font-size:11.5px;display:flex;gap:14px">' +
      kv('期貨小計', s ? s.fut : null) + kv('選擇權小計', s ? s.opt : null) +
      '<span><span style="color:#807e78">總分</span> <b style="font-size:13px;color:' +
        (!s ? '#a09e97' : s.total > 0 ? UP : s.total < 0 ? DOWN : '#0b0b0b') + '">' +
        fmtScore(s ? s.total : null) + '</b></span>' +
      '</div>';

    h += '<div style="padding:0 11px 8px;font-size:10.5px;color:#a09e97">計分模式：' + modeLabel() +
         (S.mode === 'level' ? '（第 1 票門檻 ' + fmt(S.threshold) + ' 口）'
                             : (s ? '' : '（暖身不足，這天算不出分數）')) + '</div>';
    return h;
  }
  function tdKV(k, v) {
    return '<td style="padding:0 10px 0 0"><span style="color:#807e78">' + k + '</span> ' + fmt(v, 2) + '</td>';
  }
  function kv(k, v) {
    if (v === null || v === undefined) {
      return '<span><span style="color:#807e78">' + k + '</span> <b style="color:#a09e97">—</b></span>';
    }
    return '<span><span style="color:#807e78">' + k + '</span> <b style="color:' +
      (v > 0 ? UP : v < 0 ? DOWN : '#0b0b0b') + '">' + fmtScore(v) + '</b></span>';
  }

  // ---------------------------------------------------------------- 表格
  function renderTable(sc) {
    var st = stockByCode(S.primary);
    var rows = alignedRows(st);
    var thead = el('dataTable').tHead, tbody = el('dataTable').tBodies[0];

    var h1 = '<tr><th class="date" rowspan="2">日期</th>' +
             '<th rowspan="2">收盤</th><th rowspan="2">漲跌幅</th>' +
             '<th rowspan="2">MA' + MA_PERIOD + '</th><th rowspan="2">乖離率</th>';
    CHIP_FIELDS.forEach(function (f) { h1 += '<th class="grp" colspan="3">' + f.short + '</th>'; });
    h1 += '<th class="sep" rowspan="2">期貨小計</th><th rowspan="2">選擇權小計</th><th rowspan="2">總分</th></tr>';
    var h2 = '<tr>';
    CHIP_FIELDS.forEach(function () {
      h2 += '<th class="sep">口數</th><th>本日操作</th><th>票</th>';
    });
    h2 += '</tr>';
    thead.innerHTML = h1 + h2;

    // 表格由新到舊（最新的在最上面），但圖表維持時間軸方向（左舊右新）。
    // i 一律是「由舊到新」的索引，dayDelta 與計分都靠它，所以只反轉輸出順序。
    var body = '';
    var order = S.dates.map(function (_, i) { return i; }).reverse();
    order.forEach(function (i) {
      var d = S.dates[i];
      var c = S.chips[i], s = sc[i], r = rows[i];
      var chg = r && r.change !== null && r.change !== undefined ? r.change : null;
      var prev = (r && chg !== null) ? r.close - chg : null;
      var pct = (prev && prev !== 0) ? (chg / prev * 100) : null;

      body += '<tr class="' + (SETTLEMENT.indexOf(d) >= 0 ? 'settle' : '') + '">' +
        '<td class="date">' + d + (SETTLEMENT.indexOf(d) >= 0 ? ' <span style="font-size:10.5px;color:#8a6d1f">結算</span>' : '') + '</td>' +
        '<td>' + (r ? fmt(r.close, 2) : '—') + '</td>' +
        '<td class="' + (pct === null ? '' : cls(pct)) + '">' +
          (pct === null ? '—' : (pct > 0 ? '+' : '') + pct.toFixed(2) + '%') + '</td>';

      var bi = biasAt(S.primary, d);
      body += '<td class="ma">' + (bi.ma === null ? '—' : fmt(bi.ma, 2)) + '</td>' +
              '<td class="bias ' + biasClass(bi.bias) +
                (bi.bias !== null && bi.bias < 0 ? ' neg' : '') + '">' +
                fmtBias(bi.bias) + '</td>';

      CHIP_FIELDS.forEach(function (f, k) {
        var v = s ? s.votes[k] : null, dd = dayDelta(i, f.key);
        body += '<td class="sep">' + fmt(c[f.key]) + '</td>' +
                '<td class="delta ' + (dd === null ? '' : cls(dd)) + '">' + fmtDelta(dd) + '</td>' +
                '<td class="vote ' + (v === null ? '' : cls(v)) + '">' + fmtScore(v) + '</td>';
      });

      body += '<td class="sep ' + (s ? cls(s.fut) : '') + '">' + fmtScore(s ? s.fut : null) + '</td>' +
              '<td class="' + (s ? cls(s.opt) : '') + '">' + fmtScore(s ? s.opt : null) + '</td>' +
              '<td class="total ' + (s ? cls(s.total) : '') + '">' + fmtScore(s ? s.total : null) + '</td></tr>';
    });
    tbody.innerHTML = body;
  }

  // ---------------------------------------------------------------- render
  function renderHeader() {
    var n = S.dates.length, all = S.allChips.length;
    el('rangeLabel').textContent = n
      ? S.dates[0] + ' – ' + S.dates[n - 1] + '（' + n + ' 個交易日' +
        (n < all ? '，歷史共 ' + all + ' 天' : '') + '）'
      : '沒有資料';
    var g = S.site && S.site.generated;
    el('genLabel').textContent = g ? ('資料更新於 ' + g.replace('T', ' ').replace('Z', ' UTC')) : '';
  }

  function render() {
    var sc = scoreAll(S.threshold);
    renderHeader();
    renderStats(sc);
    chart.setOption(buildOption(sc), true);
    renderTable(sc);
  }

  // ---------------------------------------------------------------- 啟動
  function boot(site) {
    S.site = site;
    S.prices = { stocks: site.stocks };
    S.primary = site.stocks[0].code;
    if (site.defaultLargeScope) S.scope = site.defaultLargeScope;
    if (S.scope === 'front' && !frontReady()) S.scope = 'all';
    applyScope();

    if (site.maPeriod) MA_PERIOD = site.maPeriod;
    if (site.biasAlert) BIAS_ALERT = site.biasAlert;
    if (site.settlements) SETTLEMENT = site.settlements;
    if (site.thresholdPresets) THRESHOLD_PRESETS = site.thresholdPresets;
    if (site.windowOptions) WINDOW_OPTIONS = site.windowOptions;
    if (site.defaultWindow !== undefined) { DEFAULT_WINDOW = site.defaultWindow; S.win = DEFAULT_WINDOW; }
    if (site.defaultThreshold !== undefined) { DEFAULT_THRESHOLD = site.defaultThreshold; }
    if (site.scoreWindow) SCORE_WINDOW = site.scoreWindow;
    if (site.scoreCurve) SCORE_CURVE = site.scoreCurve;
    if (site.scoreCurveK) CURVE_K = site.scoreCurveK;
    if (site.defaultScoreMode &&
        MODES.some(function (m) { return m.key === site.defaultScoreMode; })) {
      S.mode = site.defaultScoreMode;
    }

    // 上次選的區間與標的。存過的值可能已經不存在了（config 改過區間選項、
    // 移掉某檔股票），所以一律驗過才用，驗不過就當作沒存過、回到預設。
    var pref = loadPrefs();
    if (WINDOW_OPTIONS.indexOf(pref.win) >= 0) S.win = pref.win;
    if (site.stocks.some(function (s) { return s.code === pref.stock; })) {
      S.primary = pref.stock;
    }

    applyWindow();
    S.bias = computeBias();      // 均線用全部歷史算，不受視窗影響
    checkAlignment();

    chart = echarts.init(el('chart'), null, { renderer: 'canvas' });
    window.addEventListener('resize', function () { chart.resize(); });

    buildControls();   // 內含一次 render()
  }

  function fail(msg) {
    var box = el('loadErr');
    box.hidden = false;
    box.innerHTML = msg;
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (typeof echarts === 'undefined') {
      fail('<b>ECharts 沒有載入成功。</b>本頁先從 cdnjs、失敗再從 jsdelivr 載入 ECharts，' +
           '兩個都沒成功。請確認網路，或自行下載 <code>echarts.min.js</code> 放在同一層，' +
           '並把 index.html 裡的 script 網址改成本機檔名。');
      return;
    }
    loadData().then(boot).catch(function (e) {
      fail('<b>資料載入失敗。</b>請先執行 <code>python3 fetch_prices.py</code> 產生 ' +
           '<code>data/prices.json</code>，或改用 <code>python3 -m http.server</code> ' +
           '在本機起一個伺服器後再開啟本頁。<br><small>' + (e && e.message) + '</small>');
    });
  });
})();
