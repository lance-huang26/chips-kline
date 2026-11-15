/* 法人籌碼五票 × 個股 K 線疊圖
 * 所有分數都在瀏覽器端即時計算，改門檻不會重新載入頁面。
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- 設定
  // 以下都是預設值，載入 data/site.json 之後會被裡面的設定覆蓋。
  // 真正的設定在 config.json，由 build_site.py 寫進 site.json。
  var SETTLEMENT = [];     // 台指期結算日（build_site.py 依每月第三個星期三推算）
  var BASE_DATE = null;    // 預設基準點取自哪一天（config.json 的 baselineDate）
  var BASE_DEFAULT = {};   // 那一天的七個欄位值（含兩種十大口徑）
  var BASE_PRESETS = {};   // 某幾票的快捷按鈕，例如 {foreign_fut: [-83000, -80000]}
  var FULL_DEFAULT = {};   // 每票的「滿分口數」預設值（build_site 從歷史推的）
  var FULL_QUANTILE = 90;  // 那個預設值取的是 |偏離| 的第幾百分位（只拿來寫說明）
  var MA_PERIOD = 20;      // 乖離率的均線天數（月線）
  var BIAS_ALERT = 15;     // 正乖離超過這個百分比就標紅
  var WINDOW_OPTIONS = [20, 60, 120, 0];   // 0 = 全部
  var DEFAULT_WINDOW = 60;

  var CHIP_FIELDS = [
    { key: 'foreign_fut',    label: '外資台指期未平倉',   short: '外資期貨', color: '#2a78d6' },
    { key: 'top10_trader',   base: '前十大交易人', label: '前十大交易人', short: '前十大交易人', color: '#eb6834' },
    { key: 'top10_specific', base: '前十大特定人', label: '前十大特定人', short: '前十大特定人', color: '#1baf7a' },
    { key: 'foreign_opt',    label: '選擇權外資淨口數',   short: '選擇權外資', color: '#4a3aa7' },
    { key: 'dealer_opt',     label: '選擇權自營商淨口數', short: '選擇權自營', color: '#e87ba4' }
  ];

  // 大盤融資維持率。不是五票的一員，也不投票——它是一條「市場整體擔保品
  // 水位」的參考線，所以顏色刻意離五票的色盤遠一點，線型也用虛線，
  // 一眼就看得出它和那五條是不同性質的東西。
  var MARGIN_COLOR = '#8a6d1f';
  var MARGIN_LABEL = '大盤融資維持率';

  // chips.csv 的七個欄位（十大兩種口徑各兩欄）。基準點就是照這七個鍵存的。
  var CHIP_COLS = ['foreign_fut', 'top10_trader', 'top10_specific',
                   'top10_trader_front', 'top10_specific_front',
                   'foreign_opt', 'dealer_opt'];

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
    base: {},              // 五票各自的基準點。鍵是 chips.csv 的欄位名（七個，
                           // 十大兩種口徑各存一份），取值一律走 baseOf()
    full: {},              // 每票的「滿分口數」，鍵同上，取值走 fullOf()
    baseFrom: null,        // 目前這組基準來自哪一天；手動調過就變 null（＝自訂）
    scope: 'all',          // 十大交易人口徑：'all' 所有契約 / 'front' 近月
    mode: 'level',         // 計分模式：'level' 剩餘口數 / 'flow' 今日操作 / 'blend' 加權
    tab: 'chips',          // 目前分頁：'chips' 籌碼 K 線 / 'macro' 總經
    pinned: null,          // 釘住的那天（存日期字串，不是索引——切區間索引會位移）
    pinAt: null,           // 釘住時點的位置 {x, y}，用來擺面板
    shown: {},             // key -> bool
    margin: {},            // date -> 融資維持率(%)；來源和籌碼不同，只能照日期對齊
    showMargin: true,      // 副圖要不要畫那條維持率
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
  // 剩餘口數 / 今日操作的票是整數（+1 / -1），類比 / 加權是連續值（+0.43）。
  // 統一從這裡出去，免得有的地方顯示 0.4299999999999999。
  function fmtScore(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var r = Math.round(v * 100) / 100;
    return (r > 0 ? '+' : (r === 0 ? '' : '')) + (Number.isInteger(r) ? r : r.toFixed(2));
  }
  // 分數是連續值的模式（圖上不畫階梯線、點畫小一點、數字要帶小數）
  function isAnalog() { return S.mode === 'levelc' || S.mode === 'blend'; }
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

  /* 分頁切換。
   *
   * 兩個刻意的作法：
   *
   * 1. 控制列只屬於籌碼分頁。切到總經時要一起收起來，不然畫面上會有一排
   *    調了也沒反應的控制項。
   * 2. ECharts 在隱藏的容器裡量不到寬度，切回來時尺寸會變成 0 或殘留舊值，
   *    所以切回籌碼分頁一定要 resize()。這個 bug 只有在「切走再切回來」
   *    才會出現，第一次載入看不到。
   */
  var TV_SRC = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
  var tvLoaded = false;

  function showTab(name) {
    S.tab = name;
    el('tabChips').hidden = (name !== 'chips');
    el('tabMacro').hidden = (name !== 'macro');
    el('controls').hidden = (name !== 'chips');
    Array.prototype.forEach.call(el('tabs').children, function (b) {
      var on = (b.dataset.tab === name);
      b.className = on ? 'on' : '';
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    savePref('tab', name);
    if (name === 'macro') mountTradingView();
    else if (chart) { chart.resize(); render(); }   // 見上面第 2 點
  }

  /* TradingView 的 widget 到切進總經分頁才載入。
   *
   * 不在頁面載入時就掛，有兩個理由：籌碼分頁根本用不到，沒必要為它多拉一支
   * 第三方 script；而且 widget 在隱藏的容器裡會量到 0 高度，畫出來是空的。
   *
   * 它是第三方服務，隨時可能改版或停掉，所以給一個逾時提示——不然使用者
   * 只會看到一塊永遠空白的區域，不知道是在載入還是壞了。
   */
  function mountTradingView() {
    if (tvLoaded) return;
    tvLoaded = true;
    var boxes = document.querySelectorAll('#tabMacro .tv-box');
    Array.prototype.forEach.call(boxes, function (box) {
      var hint = document.createElement('div');
      hint.className = 'tv-loading';
      hint.textContent = '載入 ' + box.dataset.tvTitle + ' 圖表中…';
      box.appendChild(hint);

      var container = document.createElement('div');
      container.className = 'tradingview-widget-container';
      container.style.height = '100%';
      var inner = document.createElement('div');
      inner.className = 'tradingview-widget-container__widget';
      inner.style.height = '100%';
      container.appendChild(inner);

      var sc = document.createElement('script');
      sc.type = 'text/javascript';
      sc.async = true;
      sc.src = TV_SRC;
      sc.text = JSON.stringify({
        symbol: box.dataset.tvSymbol,
        interval: 'D',
        timezone: 'Asia/Taipei',
        theme: 'light',
        style: '1',                  // 1 = K 棒
        locale: 'zh_TW',
        width: '100%',
        height: 420,
        hide_side_toolbar: true,
        allow_symbol_change: false,
        save_image: false,
        withdateranges: true
      });
      container.appendChild(sc);
      box.appendChild(container);

      // widget 自己會塞一個 iframe 進來；有 iframe 就當作載到了
      var t0 = Date.now();
      var poll = setInterval(function () {
        if (box.querySelector('iframe')) { clearInterval(poll); hint.remove(); return; }
        if (Date.now() - t0 > 12000) {
          clearInterval(poll);
          hint.remove();
          var box2 = el('tvErr');
          box2.hidden = false;
          box2.innerHTML = '<b>TradingView 的圖表沒有載入成功。</b>' +
            '這一頁是直接嵌入 TradingView 的免費 widget（<code>' + TV_SRC + '</code>），' +
            '所以需要網路，而且如果對方改版、停止提供或擋掉這個來源，這裡就會是空白。' +
            '籌碼 K 線那一頁不受影響——它讀的是本專案自己的 <code>data/site.json</code>。';
        }
      }, 400);
    });
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
  // 維持率的歷史檔還是空的（新功能剛加、或回填還沒跑）時，不要放一個
  // 永遠沒有線的勾選框在那裡，直接說要跑什麼補。
  function marginReady() {
    for (var k in S.margin) {
      if (Object.prototype.hasOwnProperty.call(S.margin, k)) return true;
    }
    return false;
  }
  function marginAt(date) {
    var v = S.margin[date];
    return (v === undefined || v === null) ? null : v;
  }
  function marginSeriesData() {
    return S.dates.map(function (d) { return marginAt(d); });
  }

  /* ------------------------------------------------------------ 基準點
   *
   * 每一票都有自己的基準點，投票看的是「今天的剩餘口數離自己的基準多遠」：
   * 在基準之上投 +1、之下投 -1、剛好等於基準投 0。
   *
   * 預設那組取自 config.json 的 baselineDate（目前是 2026/09/09）。
   * 所以選 9/9 當基準時，9/9 那天五票必然全部是 0、總分 0——
   * 那是這個設計的定義，也是測試裡釘住的不變量。
   *
   * 第 2、3 票有兩種口徑，基準點必須分開記：9/9 的所有契約是 4,426 / 3,009，
   * 近月是 7,441 / 6,024，差了三千口。共用一個數字的話切到近月時基準就錯了，
   * 而且畫面上看起來完全正常——這種錯最難發現。
   * 所以 S.base 的鍵用 chips.csv 的欄位名（七個），baseOf() 再依 S.scope 挑。
   */
  function baseKey(key) {
    if (S.scope === 'front' && (key === 'top10_trader' || key === 'top10_specific')) {
      return key + '_front';
    }
    return key;
  }
  function baseOf(key) {
    var v = S.base[baseKey(key)];
    return (v === undefined || v === null) ? 0 : v;
  }
  /* 「滿分口數」：剩餘口數·類比模式用的尺度。偏離到這個量 = 0.90 分。
   * 和基準一樣，十大那兩票兩種口徑各記一份（近月和所有契約的波動幅度不一樣）。
   */
  function fullOf(key) {
    var v = S.full[baseKey(key)];
    return (v > 0) ? v : 0;          // 0 代表算不出來，計分時那天直接作廢
  }
  function setFull(key, v) {
    var n = Math.round(Number(v));
    if (!(n > 0)) return false;      // 0 或負數會讓整票失效，直接拒絕
    S.full[baseKey(key)] = n;
    return true;
  }
  function sameFull() {
    for (var i = 0; i < CHIP_COLS.length; i++) {
      if (S.full[CHIP_COLS[i]] !== FULL_DEFAULT[CHIP_COLS[i]]) return false;
    }
    return true;
  }

  function setBase(key, v) {
    S.base[baseKey(key)] = Math.round(Number(v));
    // 調完之後重新問一次「這組基準對應到哪一天」，而不是直接標成自訂。
    // 這樣兩個介面才是真的雙向：把滑桿拉回某一天的值，日期選單會自己跳回去，
    // 不會停在「自訂」卻其實正好等於某一天。
    S.baseFrom = matchingBaseDate();
  }
  // 目前這組基準正好等於哪一天的剩餘口數？沒有就回 null（＝自訂）。
  function matchingBaseDate() {
    var chips = S.site.chips;
    for (var i = chips.length - 1; i >= 0; i--) {     // 新的先找，命中機率高
      var c = chips[i], hit = true;
      for (var j = 0; j < CHIP_COLS.length; j++) {
        if (c[CHIP_COLS[j]] !== S.base[CHIP_COLS[j]]) { hit = false; break; }
      }
      if (hit) return c.date;
    }
    return null;
  }
  // 某一天的七個欄位值。給「用某一天的數值當基準」那個介面用。
  function baselinesAt(date) {
    for (var i = 0; i < S.site.chips.length; i++) {
      if (S.site.chips[i].date !== date) continue;
      var c = S.site.chips[i], out = {}, ok = true;
      CHIP_COLS.forEach(function (k) {
        var v = c[k];
        if (v === null || v === undefined) { ok = false; return; }
        out[k] = v;
      });
      return ok ? out : null;      // 近月欄位還沒補的舊日期 → 不給，不要用 0 代替
    }
    return null;
  }
  function useBaselinesFrom(date) {
    var b = baselinesAt(date);
    if (!b) return false;
    S.base = b;
    S.baseFrom = date;
    return true;
  }
  // 偏離基準的量。副圖畫的是這個，投票也是看這個的正負。
  function devOf(chip, key) {
    var x = chip[key];
    if (x === null || x === undefined) return null;
    return x - baseOf(key);
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
    { key: 'level',  label: '剩餘口數', note: '五張票各看剩餘口數離自己基準點的正負，±1（剛好等於基準是 0）。' },
    { key: 'levelc', label: '剩餘口數·類比',
      note: '同樣看離基準多遠，但分數連續：偏離到「滿分口數」得 0.90 分，再遠繼續逼近 1 但不封頂。' },
    { key: 'flow',  label: '今日操作', note: '五張票各看今日操作（今日 − 昨日）的正負，±1。' },
    { key: 'blend', label: '加權',     note: '每欄拆成水位 0.5 ＋ 操作 0.5，分數連續。' }
  ];

  /* 模式 3 的算法（用 2026/05~09 的實際資料調出來的，理由見 README）：
   *
   *   u    = (今天的值 − 基準點) ÷ 窗內穩健標準差      ← 「離基準幾倍平常波動」
   *   水位分 = curve(u) × 0.5
   *   操作分 = curve(今日變動 ÷ 變動的穩健標準差) × 0.5
   *
   * 中性點＝五票各自的基準點，和剩餘口數模式用的是同一組數字。
   * 原本是「分欄設定」：外資期貨與選擇權外資在歷史上幾乎沒有正值過
   * （前者 87 天裡一天都沒有），用 0 當中性點水位分會永遠卡在 -0.41~-0.50、
   * 標準差只有 0.026，那是常數不是分數；所以那兩欄改用窗內中位數。
   * 現在有了明確的基準點，就不需要那套例外了——基準點本身就是中性點。
   *
   * 換成固定基準會不會讓分數長期飽和？量過：以 9/9 當基準、tanh/2.5、20 日窗，
   * 97 天裡「超過 99% 滿分」的比例是 0~1%，和原本的分欄設定（0~2.1%）一樣，
   * |u| 的中位數也差不多（0.85~1.50 對 0.88~1.61）。
   * 但這個結論只在資料還沒遠離基準時成立——真的漂走幾個月的話，
   * 用「基準來源日」那個選單把基準往前搬就是解法。
   *
   * 尺度用 MAD×1.4826 而不是標準差：籌碼資料有厚尾，而且結算換月會跳空，
   * 一天的極端值就會把標準差拉大、讓之後 N 天的分數全部縮水。
   */
  var SCORE_WINDOW = 20;
  var SCORE_CURVE = 'tanh';    // 'tanh' | 'clamp2' | 'clamp3'
  var CURVE_K = 2.5;           // tanh 的緩和係數

  /* 剩餘口數·類比（levelc）的算法：
   *
   *   分數 = tanh( 偏離基準 ÷ 滿分口數 × atanh(0.9) )      ∈ (-1, +1)
   *
   * atanh(0.9) = 1.4722… 這個係數只是在「校準」：它讓「偏離剛好等於滿分口數」
   * 時分數剛好是 0.90。沒有它的話 tanh(1) = 0.76，滿分口數這個名字就名不副實。
   * 不封頂是刻意的——偏離 1.5 倍（0.986）和 2.0 倍（0.998）仍然分得出大小，
   * 換成線性封頂的話現有資料裡約 10% 的天數會擠在同一個分數上。
   *
   * 和加權模式的水位分差在分母：這裡是「你設定的固定口數」，
   * 加權是「最近 20 日的穩健標準差」。兩個回答的是不同問題——
   *   類比：離基準幾口（絕對）
   *   加權：離基準幾倍平常波動（相對）
   * 所以兩種並存，不是誰取代誰。實測差距可以很大：2026/09/16 偏離只有 4,715 口，
   * 但前 20 天非常安靜（σ=1,541），加權的水位分給 +0.84，類比只給 +0.30。
   */
  var FULL_K = 1.4722194895832204;     // = atanh(0.9)

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
  function scoreHistory() {
    var n = S.allChips.length, out = new Array(n), i, j, f;
    var W = SCORE_WINDOW;

    for (i = 0; i < n; i++) {
      var c = S.allChips[i], v = [], ok = true;

      for (j = 0; j < CHIP_FIELDS.length; j++) {
        f = CHIP_FIELDS[j];
        var x = c[f.key];
        if (x === null || x === undefined) { ok = false; break; }
        var b = baseOf(f.key);

        if (S.mode === 'level') {
          // 在基準之上 +1、之下 -1、剛好等於基準 0。
          // 0 不是「算不出來」（那是 null，顯示成 —），是「完全沒有偏離基準」。
          v.push(x > b ? 1 : (x < b ? -1 : 0));
          continue;
        }
        if (S.mode === 'levelc') {
          var fs = fullOf(f.key);
          if (!(fs > 0)) { ok = false; break; }   // 滿分口數壞掉時整天作廢，不給 0
          v.push(Math.tanh((x - b) / fs * FULL_K));
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
        // 中性點＝這一票的基準點，和剩餘口數模式完全同一個數字
        var sLv = madSigma(lw), sFl = madSigma(dw, 0);
        if (!(sLv > 0) || !(sFl > 0)) { ok = false; break; }
        v.push(curve((x - b) / sLv) + curve(d / sFl));
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
  function scoreAll() {
    return scoreHistory().slice(S.startIdx);
  }
  // 用另一組設定算一遍（統計卡要比「你調過的」和「預設」差幾天）
  function scoreWithSettings(base, full) {
    var kb = S.base, kf = S.full, kFrom = S.baseFrom;
    S.base = base; S.full = full;
    try { return scoreAll(); }
    finally { S.base = kb; S.full = kf; S.baseFrom = kFrom; }
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

  /* 副圖只有一根軸，畫的是「偏離自己的基準點多少口」。
   *
   * 原本這裡是一套雙軸對齊的機器：foreign_fut 在 -81,000 附近、其他四項在
   * 數千，量級差 20 倍，所以 foreign_fut 得用獨立右軸，再硬把右軸的門檻
   * 和左軸的 0 排到同一條格線上，讀圖的人才知道「這條線以上就是投 +1」。
   *
   * 五票各自有基準點之後那套就不需要了，而且是量過的：扣掉自己的基準之後，
   * 五票的波動幅度彼此只差 3.3~3.6 倍（20 / 60 / 118 天三個視窗、兩種口徑都量過），
   * 不是原本的 20 倍。差 20 倍的其實只是「位置」不是「波動」，
   * 減掉基準就把位置拿掉了。
   *
   * 所以：一根軸、一條 0 線，0 線以上投 +1、以下投 -1，五條線讀法完全一致。
   * 代價是看不到絕對口數——那在 hover 卡片和下方的逐日明細表格裡都查得到。
   */
  var AXIS_SPLITS = 4;
  function subAxisRange() {
    var N = AXIS_SPLITS, vals = [];
    CHIP_FIELDS.forEach(function (f) {
      if (!S.shown[f.key]) return;
      S.chips.forEach(function (c) {
        var d = devOf(c, f.key);
        if (d !== null) vals.push(d);
      });
    });
    var mn = vals.length ? Math.min.apply(null, vals) : -1;
    var mx = vals.length ? Math.max.apply(null, vals) : 1;
    var pad = 0.08 * Math.max(mx - mn, 1);
    var lo = Math.min(mn - pad, 0), hi = Math.max(mx + pad, 0);

    // 把 0 擺在某一條格線上（由下往上數第 k 條），格線才會落在整數刻度上，
    // 而且 0 線不會是一條夾在格子中間的孤線。
    var best = null;
    for (var k = 0; k <= N; k++) {
      if (k === 0 && lo < 0) continue;
      if (k === N && hi > 0) continue;
      var need = Math.max(k > 0 ? -lo / k : 0, k < N ? hi / (N - k) : 0);
      var iv = niceStep(need);
      if (!best || iv < best.iv) best = { k: k, iv: iv };
    }
    return {
      k: best.k, n: N,
      min: -best.k * best.iv,
      max: (N - best.k) * best.iv,
      interval: best.iv
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
      // 這一塊只有需要水位的模式用得到，其他模式直接停用，免得以為調了有效
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

    // 維持率的勾選框和那五條放在一起，但多一條分隔與說明——
    // 它不投票，勾起來只是多一條參考線，不會改變任何一票或總分。
    var mReady = marginReady();
    var mlab = document.createElement('label');
    mlab.className = 'chk-margin';
    var mcb = document.createElement('input');
    mcb.type = 'checkbox';
    mcb.checked = S.showMargin && mReady;
    mcb.disabled = !mReady;
    mcb.onchange = function () { S.showMargin = mcb.checked; render(); };
    var msw = document.createElement('span');
    msw.className = 'swatch dashed';   // 顏色由 style.css 的 border-top 畫
    mlab.appendChild(mcb); mlab.appendChild(msw);
    mlab.appendChild(document.createTextNode(MARGIN_LABEL + '（不投票）'));
    if (!mReady) mlab.title = '歷史檔還沒有維持率資料，跑 python3 update_daily.py --only-margin --days 130 補齊';
    g.appendChild(mlab);

    buildBaseControls();
    render();                // 原本是靠 setThr() 順便觸發的，門檻控制項拿掉之後要自己來

    el('compareChk').onchange = function () { S.compare = this.checked; render(); };

    Array.prototype.forEach.call(el('tabs').children, function (b) {
      b.onclick = function () { showTab(b.dataset.tab); };
    });
  }

  /* 基準點的兩個設定介面。
   *
   * 介面一（#baseRows）：五票各自一個滑桿 + 數字框。滑桿的範圍不是寫死的，
   *   是從那一欄的全部歷史抓 min/max 再往外放寬，不然新資料一跑出範圍就拉不到。
   *   某幾票可以在 config.json 的 baselinePresets 給快捷按鈕
   *   （目前只有第 1 票的 -83000 / -80000，那是原本的門檻預設值）。
   *
   * 介面二（#baseDateSel）：一個日期選單，選哪天就把五票的基準一次填成那天的
   *   剩餘口數。預設停在 config.json 的 baselineDate（2026/09/09）。
   *   這個介面才是常用的那一個——「以某一天的狀態當基準」本來就是這個功能的原意，
   *   而且資料哪天真的漂走了，把基準往前搬一天就解決，不必手動調五個數字。
   *
   * 兩個介面共用同一份 S.base，所以會互相反映：拉過滑桿之後選單會變成「自訂」，
   * 重新選一個日期則會把五個滑桿一起拉回那天的值。
   *
   * 基準點刻意不寫進 localStorage。和門檻、十大口徑一樣的理由：它會改變判讀結果，
   * 應該以 config.json 為準。不小心點過一次就永久記住的話，
   * 之後每次打開看到的都是自己都忘了設過的東西。
   */
  function baseRange(key) {
    var col = baseKey(key), vals = [];
    S.site.chips.forEach(function (c) {
      var v = c[col];
      if (v !== null && v !== undefined) vals.push(v);
    });
    if (!vals.length) return { min: -1000, max: 1000, step: 100 };
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    var pad = 0.25 * Math.max(mx - mn, 1);          // 往外放寬，給新資料留空間
    var step = Math.max(1, niceStep((mx - mn) / 200));
    return {
      min: Math.floor((mn - pad) / step) * step,
      max: Math.ceil((mx + pad) / step) * step,
      step: step
    };
  }

  function buildBaseControls() {
    var box = el('baseRows');
    box.innerHTML = '';
    CHIP_FIELDS.forEach(function (f) {
      var r = baseRange(f.key);
      var row = document.createElement('div');
      row.className = 'base-row';
      row.dataset.key = f.key;

      var name = document.createElement('div');
      name.className = 'base-name';
      name.innerHTML = '<span class="swatch" style="background:' + f.color + '"></span>' +
                       '<span class="base-label">' + f.label + '</span>';
      row.appendChild(name);

      var rng = document.createElement('input');
      rng.type = 'range'; rng.className = 'base-range';
      // step 固定 1：滑桿只是拖曳用，真正的粗略刻度給數字框的上下鍵。
      // 如果這裡跟著 r.step（例如 250），把 -81,066 寫回滑桿會被吸附成 -81,000，
      // 兩個輸入框就對不起來了。
      rng.min = r.min; rng.max = r.max; rng.step = 1;
      rng.setAttribute('aria-label', f.label + ' 的基準點（滑桿）');

      var num = document.createElement('input');
      num.type = 'number'; num.className = 'base-num';
      num.step = r.step;
      num.setAttribute('aria-label', f.label + ' 的基準點（數字）');

      row.appendChild(rng);
      row.appendChild(num);

      var unit = document.createElement('span');
      unit.className = 'unit'; unit.textContent = '口';
      row.appendChild(unit);

      // 快捷按鈕包成一格（即使是空的），五列的欄位才對得齊——
      // 只有第 1 票有按鈕，不包起來的話那一列的「最新／偏離」會被擠到下一行。
      var pbox = document.createElement('div');
      pbox.className = 'base-presets';
      (BASE_PRESETS[f.key] || []).forEach(function (v) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'preset base-preset';
        b.dataset.key = f.key; b.dataset.v = v;
        b.textContent = fmt(v);
        b.onclick = function () { setBase(f.key, v); syncBaseControls(); render(); };
        pbox.appendChild(b);
      });
      row.appendChild(pbox);

      // 滿分口數。只有「剩餘口數·類比」模式用得到，所以其他模式整欄收起來
      // （留在 DOM 裡但不顯示），免得控制列上有一堆調了沒反應的東西。
      var fbox = document.createElement('span');
      fbox.className = 'base-full';
      var flab = document.createElement('span');
      flab.className = 'base-full-label'; flab.textContent = '滿分';
      var fnum = document.createElement('input');
      fnum.type = 'number'; fnum.className = 'base-fullnum';
      fnum.min = 1; fnum.step = Math.max(1, r.step);
      fnum.setAttribute('aria-label', f.label + ' 的滿分口數');
      fnum.oninput = function () {
        if (fnum.value === '' || !setFull(f.key, fnum.value)) return;
        syncBaseControls(); render();
      };
      fbox.appendChild(flab); fbox.appendChild(fnum);
      row.appendChild(fbox);

      var now = document.createElement('span');
      now.className = 'base-now';
      row.appendChild(now);

      rng.oninput = function () { setBase(f.key, rng.value); syncBaseControls(); render(); };
      num.oninput = function () {
        if (num.value === '' || isNaN(Number(num.value))) return;
        setBase(f.key, num.value); syncBaseControls(); render();
      };
      box.appendChild(row);
    });

    var sel = el('baseDateSel');
    sel.innerHTML = '';
    var custom = document.createElement('option');
    custom.value = ''; custom.textContent = '自訂（手動調過）';
    sel.appendChild(custom);
    // 新的在上面，和逐日明細表格一致
    S.site.chips.slice().reverse().forEach(function (c) {
      var o = document.createElement('option');
      o.value = c.date;
      o.textContent = c.date + (c.date === BASE_DATE ? '（預設）' : '');
      // 近月欄位還沒補的舊日期不能選——拿不到那兩個基準，而用 0 代替
      // 會讓第 2、3 票整段投錯，畫面上還完全正常
      if (!baselinesAt(c.date)) { o.disabled = true; o.textContent += '（近月欄位未補齊）'; }
      sel.appendChild(o);
    });
    sel.onchange = function () {
      if (!sel.value) { syncBaseControls(); return; }     // 「自訂」不是一個動作
      if (!useBaselinesFrom(sel.value)) { syncBaseControls(); return; }
      syncBaseControls(); render();
    };

    el('baseReset').onclick = function () {
      S.base = {}; S.full = {};
      CHIP_COLS.forEach(function (k) {
        S.base[k] = BASE_DEFAULT[k];
        S.full[k] = FULL_DEFAULT[k];
      });
      S.baseFrom = BASE_DATE;
      syncBaseControls(); render();
    };

    syncBaseControls();
  }

  // 把 S.base 的現況寫回兩個介面（滑桿、數字框、快捷按鈕、日期選單、說明）
  function syncBaseControls() {
    var box = el('baseRows');
    if (!box || !box.children.length) return;    // 還沒建好（第一次 render 在建控制列之前）
    var last = S.chips.length ? S.chips[S.chips.length - 1] : null;
    Array.prototype.forEach.call(box.children, function (row) {
      var key = row.dataset.key;
      var v = baseOf(key);
      var rng = row.querySelector('.base-range'), num = row.querySelector('.base-num');
      // 滑桿的範圍是依口徑算的，切口徑時要跟著換（近月和所有契約差三千口）
      var r = baseRange(key);
      rng.min = r.min; rng.max = r.max; rng.step = 1; num.step = r.step;
      if (Number(rng.value) !== v) rng.value = v;
      if (Number(num.value) !== v) num.value = v;
      Array.prototype.forEach.call(row.querySelectorAll('.base-preset'), function (b) {
        b.classList.toggle('on', Number(b.dataset.v) === v);
      });
      var fnum = row.querySelector('.base-fullnum');
      var fv = fullOf(key);
      fnum.step = Math.max(1, r.step);
      if (Number(fnum.value) !== fv) fnum.value = fv;

      var now = row.querySelector('.base-now');
      if (!last) { now.textContent = ''; return; }
      var d = devOf(last, key);
      now.innerHTML = '最新 ' + fmt(last[key]) + '　偏離 <b class="' +
        (d === null ? '' : cls(d)) + '">' + fmtDelta(d) + '</b>' +
        // 類比模式才顯示這天實際得幾分——調滿分口數時馬上看得到效果
        (S.mode === 'levelc' && fv > 0 && d !== null
          ? '　→ <b class="' + cls(d) + '">' +
            fmtScore(Math.tanh(d / fv * FULL_K)) + ' 分</b>'
          : '');
    });
    el('baseRows').classList.toggle('show-full', S.mode === 'levelc');

    // 標題要跟著模式走：類比模式下「投 +1 / −1 的分界」是錯的說法
    // 標題要跟著模式走：類比模式下「投 +1 / −1 的分界」是錯的說法，
    // 今日操作模式根本不看水位（基準只剩下決定副圖畫在哪裡）
    var BASE_ROLE = { level: '（投 +1 / −1 的分界）', levelc: '（分數的零點）',
                      blend: '（水位分的中性點）',
                      flow: '（今日操作模式不看水位，基準只影響副圖）' };
    el('baseBlockLabel').textContent = '五票的基準點' + (BASE_ROLE[S.mode] || '');

    var sel = el('baseDateSel');
    sel.value = S.baseFrom || '';
    var same = sameBase(S.base, BASE_DEFAULT) && sameFull();
    el('baseNote').innerHTML = (S.baseFrom
      ? '基準＝<b>' + S.baseFrom + '</b> 那天的剩餘口數' +
        (same ? '（就是預設值）' : '') + '。副圖畫的是每天離這組基準多少口，' +
        '<b>0 線以上投 +1</b>。'
      : '基準是<b>手動調過的</b>，不對應任何一天。要回到 ' + BASE_DATE +
        ' 按「重設為預設」，或直接從選單挑一天。') +
      (S.mode === 'levelc'
        ? '　<b>滿分口數</b>＝偏離多少口算 0.90 分（預設是全部歷史 |偏離| 的第 ' +
          FULL_QUANTILE + ' 百分位），再遠會繼續逼近 1 但不封頂。'
        : '');
  }
  function sameBase(a, b) {
    for (var i = 0; i < CHIP_COLS.length; i++) {
      if (a[CHIP_COLS[i]] !== b[CHIP_COLS[i]]) return false;
    }
    return true;
  }

  // ---------------------------------------------------------------- 統計條 / 基準點比較
  function renderStats(sc) {
    // 算不出來的日子（今日操作的第一天、加權暖身不足）一律不計入統計，
    // 不能當成 0——0 是「多空剛好抵銷」，和「算不出來」是兩件事。
    var have = sc.filter(function (s) { return s; });
    var pos = 0, neg = 0, zero = 0;
    have.forEach(function (s) { if (s.total > 0) pos++; else if (s.total < 0) neg++; else zero++; });

    /* 「你調過的基準」和「預設基準」差幾天。
     *
     * 這一格原本是比兩個門檻（-83000 vs -80000）總分差幾天。五票都有自己的基準
     * 之後那個問題就不對了——可調的旋鈕從一個變成五個，只比第 1 票沒有意義。
     * 改成比「現在這組」對「預設那組」：沒調過就是 0 天，調得越多差得越多，
     * 直接告訴你判讀對基準有多敏感。這是原本那一格的推廣，不是拿掉。
     *
     * 三種模式都適用：基準點現在同時是剩餘口數模式的分界和加權模式的中性點。
     */
    var atDefault = sameBase(S.base, BASE_DEFAULT) && sameFull();
    var diff = [];
    if (!atDefault) {
      var a = sc, b = scoreWithSettings(BASE_DEFAULT, FULL_DEFAULT);
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
      (atDefault
        ? card('可計分天數', have.length + '<small>/ ' + sc.length + '</small>',
               have.length === sc.length ? '' : 'pending')
        : card('vs 預設設定', diff.length + '<small> 天不同</small>',
               diff.length ? 'pending' : ''))
    ].join('');

    var warm = have.length < sc.length
      ? ' 有 <b>' + (sc.length - have.length) + '</b> 天因為暖身不足算不出分數，顯示為 —。'
      : '';
    if (atDefault) {
      el('baseDiffNote').innerHTML =
        '基準' + (S.mode === 'levelc' ? '與滿分口數' : '') +
        '都是預設的那一組（' + BASE_DATE + '），所以沒有對照可比。' +
        '調過之後這裡會告訴你和預設差幾天。' + warm;
      return;
    }
    var few = diff.length <= 12;
    el('baseDiffNote').innerHTML = (diff.length
      ? '和預設設定（基準 <b>' + BASE_DATE + '</b>' +
        (sameFull() ? '' : '＋你改過的滿分口數') + '）相比，「' + modeLabel() +
        '」模式的總分不同的有 <b>' + diff.length + '</b> 天' +
        (few ? '：' + diff.join('、') : '（區間較長，日期請看下方表格）') + '。'
      : '雖然設定調過了，但在本區間內「' + modeLabel() +
        '」模式的總分和預設完全相同。') + warm;
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
      // 剩餘口數 / 今日操作的總分是整數、階梯線才對；類比 / 加權是連續值，階梯會很怪
      type: 'line', step: (isAnalog() ? false : 'middle'),
      xAxisIndex: 0, yAxisIndex: 1,
      data: sc.map(function (s) { return s ? s.total : null; }),
      symbol: 'circle', symbolSize: isAnalog() ? 5 : 8, showSymbol: true,
      lineStyle: { width: 2, color: SCORE_COLOR },
      itemStyle: { color: SCORE_COLOR, borderColor: '#fcfcfb', borderWidth: 2 },
      connectNulls: false,          // 暖身不足的那段要斷開，不要連成一條假線
      z: 6,
      markLine: { silent: true, symbol: 'none', data: mlData, animation: false }
    };

    var sub = subAxisRange();
    var showMargin = S.showMargin && marginReady();
    // 副圖只剩一根左軸，所以勾了維持率也只需要一排右側刻度
    var subRight = showMargin ? 76 : 62;

    // 副圖的 0 線＝每一票自己的基準點。五條線都已經減掉各自的基準，
    // 所以這一條線同時是五票的分界：在線上投 +1、在線下投 -1。
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
            formatter: '0 ＝ 各自的基準點（在線上投 +1）'
          }
        }]
      }
    };

    var chipSeries = CHIP_FIELDS.filter(function (f) { return S.shown[f.key]; }).map(function (f) {
      return {
        name: f.label,
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 2,                 // 五票共用一根軸（都是「偏離基準」的口數）
        data: S.chips.map(function (c) { return devOf(c, f.key); }),
        symbol: 'circle', symbolSize: 5, showSymbol: false,
        lineStyle: { width: 2, color: f.color },
        itemStyle: { color: f.color },
        emphasis: { focus: 'series' }
      };
    });

    // 維持率（%）和口數不是同一個量級也不是同一個單位，所以給它自己的右軸。
    // 刻意不去對齊格線——副圖的 0 線代表「各票的基準點」，
    // 維持率沒有對應的基準，硬是對齊只會讓人以為那條格線對它也代表什麼。
    if (S.showMargin && marginReady()) {
      chipSeries = chipSeries.concat([{
        name: MARGIN_LABEL,
        type: 'line', xAxisIndex: 1, yAxisIndex: 3,
        data: marginSeriesData(),
        symbol: 'circle', symbolSize: 4, showSymbol: false,
        lineStyle: { width: 1.8, color: MARGIN_COLOR, type: 'dashed' },
        itemStyle: { color: MARGIN_COLOR },
        connectNulls: false,       // 沒有維持率的那幾天要斷開，不要連成假線
        emphasis: { focus: 'series' }
      }]);
    }

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
        // 右邊距要跟著副圖軸數調整：多一條維持率軸就多一排刻度要擺。
        { left: 72, right: subRight, top: 46, height: 470 },
        { left: 72, right: subRight, top: 580, height: 150 }
      ],
      axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#52514e' } },
      tooltip: {
        // 已經釘住一天時就不要再跟著游標冒出第二張卡片
        show: S.pinned === null,
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
        { // 副圖：五票共用這一根，畫的是「偏離自己的基準點幾口」。
          // min/max/interval 由 subAxisRange() 算，0 一定落在某一條格線上。
          type: 'value', gridIndex: 1, name: '口（偏離基準）',
          min: sub.min, max: sub.max, interval: sub.interval,
          nameTextStyle: { color: '#807e78', fontSize: 10.5, align: 'left' }, nameGap: 12,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#807e78', fontSize: 10.5 },
          splitLine: { lineStyle: { color: '#efefec' } }
        },
        { // 副圖右：大盤融資維持率（%）。自己 auto-scale，不參與格線對齊
          type: 'value', gridIndex: 1, position: 'right',
          scale: true, show: showMargin,
          name: showMargin ? '%（融資維持率）' : '',
          nameTextStyle: { color: MARGIN_COLOR, fontSize: 10.5, align: 'right' },
          nameGap: 12,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: MARGIN_COLOR, fontSize: 10.5,
                       formatter: function (v) { return v.toFixed(0) + '%'; } },
          splitLine: { show: false }
        }
      ],
      series: [bgSeries('__bg_up', bgUp), bgSeries('__bg_dn', bgDn)]
        .concat(priceSeries, [scoreSeries, subGuide], chipSeries)
    };
  }

  function shortDate(d) { return d.slice(5); }

  /* hover / 釘住時顯示的當日明細。
   *
   * 只放籌碼：五票、本日操作、小計、總分。
   * 個股的 OHLC 與乖離率刻意不放——這張卡片是用來讀「今天籌碼怎麼投」的，
   * 把六檔股票的行情混進來會把真正重要的那幾行擠到下面去。
   * 個股那些數字在下方的逐日明細表格裡都查得到（收盤、漲跌幅、MA20、乖離率）。
   *
   * 大盤融資維持率是唯一的例外，而且只在那條線有勾起來的時候才出現，
   * 擺在總分下面、和五票明顯分開：它是全市場的數字（不是個股），
   * 而且不投票，所以不能混進五票那張表裡。
   */
  function tooltipHtml(idx, sc) {
    var d = S.dates[idx], c = S.chips[idx], s = sc[idx];
    var isSettle = SETTLEMENT.indexOf(d) >= 0;

    var h = '<div style="padding:9px 11px 4px;border-bottom:1px solid #ececE8">' +
            '<b style="font-size:12.5px">' + d + '</b>' +
            (isSettle ? '<span style="margin-left:7px;font-size:11px;color:#8a6d1f;background:#fdf3d8;padding:1px 5px;border-radius:3px">結算日</span>' : '') +
            '</div>';

    // 五票
    h += '<div style="padding:7px 11px 3px"><table style="font-size:11.5px;border-spacing:0">';
    h += '<tr style="color:#a09e97;font-size:10.5px"><td></td>' +
         '<td style="padding:0 8px 2px 0;text-align:right">口數</td>' +
         '<td style="padding:0 8px 2px 0;text-align:right">偏離基準</td>' +
         '<td style="padding:0 8px 2px 0;text-align:right">本日操作</td>' +
         '<td style="padding:0 0 2px;text-align:right">票</td></tr>';
    CHIP_FIELDS.forEach(function (f, i) {
      var v = s ? s.votes[i] : null, dd = dayDelta(idx, f.key);
      var dv = devOf(c, f.key);
      h += '<tr>' +
        '<td style="padding:1px 8px 1px 0"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:' +
          f.color + ';margin-right:6px"></span>' + f.label + '</td>' +
        '<td style="padding:1px 8px 1px 0;text-align:right;font-variant-numeric:tabular-nums">' + fmt(c[f.key]) + '</td>' +
        '<td style="padding:1px 8px 1px 0;text-align:right;font-variant-numeric:tabular-nums;font-weight:600;color:' +
          (dv === null ? '#a09e97' : dv > 0 ? UP : dv < 0 ? DOWN : '#52514e') + '">' + fmtDelta(dv) + '</td>' +
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

    // 大盤融資維持率（有勾才顯示，而且不投票）
    if (S.showMargin && marginReady()) {
      var mv = marginAt(d);
      h += '<div style="padding:4px 11px 7px;border-top:1px solid #ececE8;font-size:11.5px">' +
        '<span style="display:inline-block;width:8px;height:2px;border-top:2px dashed ' +
          MARGIN_COLOR + ';vertical-align:middle;margin-right:6px"></span>' +
        '<span style="color:#807e78">' + MARGIN_LABEL + '</span> <b style="color:' +
          (mv === null ? '#a09e97' : MARGIN_COLOR) + '">' +
          (mv === null ? '—' : mv.toFixed(2) + '%') + '</b>' +
        '<span style="color:#a09e97;font-size:10.5px;margin-left:6px">' +
          (mv === null ? '這天沒有維持率資料' : '不投票') + '</span>' +
        '</div>';
    }

    h += '<div style="padding:0 11px 8px;font-size:10.5px;color:#a09e97">計分模式：' + modeLabel() +
         '　基準：' + (S.baseFrom ? S.baseFrom : '自訂') +
         (S.mode === 'levelc' ? '（偏離到滿分口數 ＝ 0.90 分）' : '') +
         (s ? '' : '（暖身不足，這天算不出分數）') + '</div>';
    return h;
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
    CHIP_FIELDS.forEach(function (f) {
      h1 += '<th class="grp" colspan="3">' + f.short +
            '<span class="th-base">基準 ' + fmt(baseOf(f.key)) + '</span></th>';
    });
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

  /* 點 K 線把當日明細釘住。
   *
   * 釘住的是「日期」而不是索引：切區間之後同一個索引會指到別天，
   * 存索引的話面板會安靜地換成另一天的資料，那種錯很難發現。
   * 釘住的那天不在目前視窗內時，面板收起來而不是顯示錯的資料。
   *
   * 內容直接重用 tooltipHtml()，所以釘住的和 hover 看到的永遠一致，
   * 不會有兩份各自演化的排版。
   */
  function togglePin(date, x, y) {
    if (S.pinned === date) { S.pinned = null; S.pinAt = null; }
    else { S.pinned = date; S.pinAt = { x: x, y: y }; }
    render();
  }
  function renderPinned(sc) {
    var box = el('pinned');
    var i = S.pinned === null ? -1 : S.dates.indexOf(S.pinned);
    if (i < 0) {                       // 沒釘，或釘的那天已經不在視窗內
      box.hidden = true;
      return;
    }
    el('pinnedBody').innerHTML = tooltipHtml(i, sc);
    box.hidden = false;

    // 擺在點擊處附近，但不能超出圖表範圍
    var wrap = box.parentNode;
    var maxX = wrap.clientWidth - box.offsetWidth - 8;
    var maxY = wrap.clientHeight - box.offsetHeight - 8;
    var at = S.pinAt || { x: 12, y: 12 };
    box.style.left = Math.max(8, Math.min(at.x + 14, Math.max(8, maxX))) + 'px';
    box.style.top = Math.max(8, Math.min(at.y + 14, Math.max(8, maxY))) + 'px';
  }

  function render() {
    var sc = scoreAll();
    renderHeader();
    syncBaseControls();      // 切口徑或切區間之後，滑桿範圍與「最新／偏離」要跟著更新
    renderStats(sc);
    chart.setOption(buildOption(sc), true);
    renderTable(sc);
    renderPinned(sc);
  }

  // ---------------------------------------------------------------- 啟動
  function boot(site) {
    S.site = site;
    S.prices = { stocks: site.stocks };
    S.primary = site.stocks[0].code;
    if (site.defaultLargeScope) S.scope = site.defaultLargeScope;
    if (S.scope === 'front' && !frontReady()) S.scope = 'all';
    applyScope();

    // 融資維持率和籌碼是兩套獨立來源，天數不保證一樣（剛上線時只有近 90 天，
    // 籌碼卻有一整年），所以存成 date -> ratio 的查表，畫圖時照日期對齊；
    // 對不到的那天就是 null，線會斷開而不是接成一條假的水平線。
    S.margin = {};
    (site.margin || []).forEach(function (r) { S.margin[r.date] = r.ratio; });
    if (!marginReady()) S.showMargin = false;

    if (site.maPeriod) MA_PERIOD = site.maPeriod;
    if (site.biasAlert) BIAS_ALERT = site.biasAlert;
    if (site.settlements) SETTLEMENT = site.settlements;
    if (site.baselinePresets) BASE_PRESETS = site.baselinePresets;
    if (site.windowOptions) WINDOW_OPTIONS = site.windowOptions;
    if (site.defaultWindow !== undefined) { DEFAULT_WINDOW = site.defaultWindow; S.win = DEFAULT_WINDOW; }
    // 基準點。site.json 一定會帶（build_site.py 從 config.json 的 baselineDate 解析），
    // 缺了就直接讓它炸在 fail() 裡，不要默默用 0——用 0 的話五票會整段投錯。
    if (!site.baselines || !site.baselineDate) {
      throw new Error('site.json 少了 baselines / baselineDate：'
                      + '跑一次 python3 build_site.py');
    }
    if (!site.fullScales) {
      throw new Error('site.json 少了 fullScales：跑一次 python3 build_site.py');
    }
    BASE_DATE = site.baselineDate;
    BASE_DEFAULT = site.baselines;
    FULL_DEFAULT = site.fullScales;
    if (site.fullScaleQuantile) FULL_QUANTILE = site.fullScaleQuantile;
    CHIP_COLS.forEach(function (k) {
      S.base[k] = BASE_DEFAULT[k];
      S.full[k] = FULL_DEFAULT[k];
    });
    S.baseFrom = BASE_DATE;
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
    if (pref.tab === 'chips' || pref.tab === 'macro') S.tab = pref.tab;
    if (WINDOW_OPTIONS.indexOf(pref.win) >= 0) S.win = pref.win;
    if (site.stocks.some(function (s) { return s.code === pref.stock; })) {
      S.primary = pref.stock;
    }

    applyWindow();
    S.bias = computeBias();      // 均線用全部歷史算，不受視窗影響
    checkAlignment();

    chart = echarts.init(el('chart'), null, { renderer: 'canvas' });
    window.addEventListener('resize', function () {
      chart.resize();
      // 只有釘住時才需要重畫——不然每次拖視窗大小都做一次完整 setOption 會卡
      if (S.pinned !== null) render();
    });

    // 點圖上任何一天都可以釘住——用 zrender 的 click 而不是 series 的 click，
    // 不然只有正好點在 K 棒實體上才有反應，點到棒子中間的空隙會沒事發生。
    chart.getZr().on('click', function (e) {
      var pt = [e.offsetX, e.offsetY];
      var gi = chart.containPixel({ gridIndex: 0 }, pt) ? 0
             : (chart.containPixel({ gridIndex: 1 }, pt) ? 1 : -1);
      if (gi < 0) return;                       // 點在圖表外面
      var i = Math.round(chart.convertFromPixel({ xAxisIndex: gi }, pt[0]));
      if (!(i >= 0 && i < S.dates.length)) return;
      togglePin(S.dates[i], e.offsetX, e.offsetY);
    });
    el('pinClose').onclick = function () { S.pinned = null; S.pinAt = null; render(); };
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && S.pinned !== null) {
        S.pinned = null; S.pinAt = null; render();
      }
    });

    buildControls();   // 內含一次 render()
    // render() 之後才切分頁：ECharts 要先在看得見的容器裡量過一次寬度，
    // 否則一開始就停在總經分頁時，切回籌碼會是一張 0 寬的圖。
    if (S.tab !== 'chips') showTab(S.tab);
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
