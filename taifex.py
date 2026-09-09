#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期交所（TAIFEX）籌碼五欄的抓取與解析。

五個欄位、三個端點：

  foreign_fut     三大法人-區分各期貨契約     POST /cht/3/futContractsDate    HTML・UTF-8
                  臺股期貨 → 外資 → 未平倉多空淨額（口數）

  top10_trader    期貨大額交易人未沖銷部位     POST /cht/3/largeTraderFutDown  CSV・Big5
  top10_specific  契約 TX、到期月份 999999、交易人類別 0 / 1
                  → 前十大交易人買方 − 前十大交易人賣方
                  （期交所在 TX 底下publish 的已經是 TX+MTX/4+TMF/20 合併口徑，
                    CSV 的商品名稱欄就直接寫著，不需要自己加權）

  foreign_opt     三大法人-區分各選擇權契約   POST /cht/3/callsAndPutsDate    HTML・UTF-8
  dealer_opt      臺指選擇權 → 外資 / 自營商
                  → 買權未平倉淨額 − 賣權未平倉淨額

實測過的行為（2026/09）：
  * 非交易日與未來日期都回「0 筆資料列」，不會默默給前一個交易日的數字。
  * HTML 查詢頁的回應裡一定含有 alert( 字樣（版型的 noscript 樣板），
    成功時也有，所以不能拿它當失敗訊號；要看的是有沒有資料列。
  * largeTraderFutDown 用 queryStartDate / queryEndDate，不是 queryDate；
    少帶表單欄位會回 HTTP 200 加一頁錯誤 HTML，所以要檢查回應開頭是不是 CSV 表頭。
"""

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = "https://www.taifex.com.tw"
UA = "Mozilla/5.0 (compatible; chips-kline/1.0)"

# 三大法人查詢頁共用的表單欄位，少一個就會被導去錯誤頁
FORM_BASE = {"queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": ""}

FUT_COMMODITY = "臺股期貨"
OPT_COMMODITY = "臺指選擇權"
LARGE_CONTRACT = "TX"
LARGE_ALL_MONTHS = "999999"      # 所有契約
IDENTITIES = ("自營商", "投信", "外資")
SIDES = ("買權", "賣權")


class TaifexError(Exception):
    """抓取或解析失敗——要讓流程停下來的那種錯。"""


class NoDataForDate(Exception):
    """該日期沒有資料（非交易日，或尚未公布）——正常情況，不算失敗。"""


# --------------------------------------------------------------- HTML → 表格列
class _TableRows(HTMLParser):
    """把 HTML 裡所有 <tr> 抽成 [[儲存格文字, ...], ...]。

    只用標準庫，這樣 GitHub Actions 不需要裝任何套件。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", "", "".join(self._cell))
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if len(self._row) > 1:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def table_rows(html: str) -> list:
    p = _TableRows()
    p.feed(html)
    return p.rows


def to_int(s: str) -> int:
    s = str(s).replace(",", "").strip()
    if s in ("", "-", "--"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        raise TaifexError("數字解析失敗：%r" % s)


def page_data_date(html: str):
    """回傳頁面上印的「日期 YYYY/MM/DD」，找不到回 None。"""
    text = re.sub(r"<[^>]+>", " ", html)
    m = re.search(r"日期[^0-9]{0,8}(\d{4}/\d{2}/\d{2})", text)
    return m.group(1) if m else None


# --------------------------------------------------------------- 解析：三大法人期貨
def parse_fut(rows: list, date: str) -> int:
    """臺股期貨 → 外資 → 未平倉多空淨額（口數）。

    資料列的形狀有兩種（序號與商品名稱有 rowspan）：
        15 欄：[序號, 商品名稱, 身份別, ...12 個數字]
        13 欄：[身份別, ...12 個數字]
    所以身份別固定是倒數第 13 欄，未平倉淨額口數固定是倒數第 2 欄。
    """
    data = [r for r in rows if len(r) >= 13 and r[-13] in IDENTITIES]
    if not data:
        raise NoDataForDate("三大法人（期貨）%s 沒有資料列" % date)

    commodity, found = None, []
    for cells in data:
        extra = cells[:-13]
        if len(extra) == 2:            # 序號 + 商品名稱
            commodity = extra[1]
        elif len(extra) != 0:
            raise TaifexError("期貨表格欄數不認得（前綴 %r），期交所可能改版了" % (extra,))
        if commodity == FUT_COMMODITY and cells[-13] == "外資":
            found.append(to_int(cells[-2]))

    if len(found) != 1:
        raise TaifexError(
            "在 %s 的期貨表格裡找到 %d 筆「%s → 外資」，預期 1 筆。"
            "期交所可能改版，或商品名稱變了——沒有寫入任何資料。"
            % (date, len(found), FUT_COMMODITY))
    return found[0]


# --------------------------------------------------------------- 解析：三大法人選擇權
def parse_opt(rows: list, date: str) -> dict:
    """臺指選擇權 外資／自營商：買權未平倉淨額 − 賣權未平倉淨額。

    資料列形狀（序號、商品名稱、權別都可能有 rowspan）：
        16 欄：[序號, 商品名稱, 權別, 身份別, ...12]
        14 欄：[權別, 身份別, ...12]
        13 欄：[身份別, ...12]
    """
    data = [r for r in rows if len(r) >= 13 and r[-13] in IDENTITIES]
    if not data:
        raise NoDataForDate("三大法人（選擇權）%s 沒有資料列" % date)

    commodity, side = None, None
    got = {}
    for cells in data:
        extra = cells[:-13]
        if len(extra) == 3:            # 序號 + 商品名稱 + 權別
            commodity, side = extra[1], extra[2]
        elif len(extra) == 1:          # 只有權別
            side = extra[0]
        elif len(extra) != 0:
            raise TaifexError("選擇權表格欄數不認得（前綴 %r），期交所可能改版了" % (extra,))

        who = cells[-13]
        if commodity == OPT_COMMODITY and side in SIDES and who in ("外資", "自營商"):
            key = (who, side)
            if key in got:
                raise TaifexError("%s 的選擇權表格出現重複的 %s，沒有寫入任何資料" % (date, key))
            got[key] = to_int(cells[-2])

    need = [(w, s) for w in ("外資", "自營商") for s in SIDES]
    missing = [k for k in need if k not in got]
    if missing:
        raise TaifexError(
            "在 %s 的選擇權表格裡找不到 %s（%s）——期交所可能改版，沒有寫入任何資料"
            % (date, missing, OPT_COMMODITY))

    return {
        "foreign_opt": got[("外資", "買權")] - got[("外資", "賣權")],
        "dealer_opt": got[("自營商", "買權")] - got[("自營商", "賣權")],
        "_detail": {"%s_%s" % k: v for k, v in got.items()},
    }


# --------------------------------------------------------------- 解析：大額交易人
LARGE_HEADER_HINT = "前十大交易人買方"


def parse_large(csv_text: str, date: str) -> dict:
    """TX、到期月份 999999、交易人類別 0（全體）與 1（特定法人）。

    前十大交易人買方 − 前十大交易人賣方。
    """
    lines = [l for l in csv_text.replace("\r", "").split("\n") if l.strip()]
    if not lines or LARGE_HEADER_HINT not in lines[0]:
        raise TaifexError(
            "大額交易人回傳的不是預期的 CSV（表頭是 %r）。"
            "多半是表單欄位少帶被導去錯誤頁——注意 HTTP 200 不代表成功。"
            % (lines[0][:80] if lines else ""))

    got = {}
    seen_date = set()
    for line in lines[1:]:
        c = [x.strip() for x in line.split(",")]
        if len(c) < 9 or c[1] != LARGE_CONTRACT:
            continue
        seen_date.add(c[0])
        if c[3] != LARGE_ALL_MONTHS:
            continue
        got[c[4]] = to_int(c[7]) - to_int(c[8])

    if not seen_date:
        raise NoDataForDate("大額交易人 %s 沒有 %s 的資料" % (date, LARGE_CONTRACT))
    if seen_date != {date}:
        raise TaifexError(
            "大額交易人回傳的日期是 %s，不是要的 %s——沒有寫入任何資料"
            % (sorted(seen_date), date))
    for k in ("0", "1"):
        if k not in got:
            raise TaifexError(
                "大額交易人 %s 找不到 %s / %s / 交易人類別 %s，期交所可能改版了"
                % (date, LARGE_CONTRACT, LARGE_ALL_MONTHS, k))

    return {"top10_trader": got["0"], "top10_specific": got["1"]}


# --------------------------------------------------------------- 連線
def _post(path: str, form: dict, encoding: str, retries: int = 3) -> str:
    body = urllib.parse.urlencode(form).encode("ascii")
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                BASE + path, data=body,
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
            return raw.decode(encoding, errors="replace")
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * attempt)
    raise TaifexError("連線 %s 失敗：%s" % (path, last))


def fetch_day(date: str, sleep: float = 3.0) -> dict:
    """抓某一天的五個欄位。

    非交易日／尚未公布 → 丟 NoDataForDate（呼叫端應該安靜跳過）。
    解析不出來        → 丟 TaifexError（呼叫端應該讓流程失敗）。
    """
    if not re.match(r"^\d{4}/\d{2}/\d{2}$", date):
        raise TaifexError("日期格式要是 YYYY/MM/DD，收到 %r" % date)

    form = dict(FORM_BASE, queryDate=date, commodityId="")

    fut_html = _post("/cht/3/futContractsDate", form, "utf-8")
    _check_page_date(fut_html, date, "三大法人（期貨）")
    foreign_fut = parse_fut(table_rows(fut_html), date)
    time.sleep(sleep)

    opt_html = _post("/cht/3/callsAndPutsDate", form, "utf-8")
    _check_page_date(opt_html, date, "三大法人（選擇權）")
    opt = parse_opt(table_rows(opt_html), date)
    time.sleep(sleep)

    large_csv = _post("/cht/3/largeTraderFutDown",
                      dict(FORM_BASE, queryStartDate=date, queryEndDate=date,
                           contractId=""), "big5")
    large = parse_large(large_csv, date)

    return {
        "date": date,
        "foreign_fut": foreign_fut,
        "top10_trader": large["top10_trader"],
        "top10_specific": large["top10_specific"],
        "foreign_opt": opt["foreign_opt"],
        "dealer_opt": opt["dealer_opt"],
        "_raw": {"fut": fut_html, "opt": opt_html, "large": large_csv},
        "_detail": opt["_detail"],
    }


def _check_page_date(html: str, date: str, what: str):
    """頁面上印的資料日期必須就是要的那天。

    實測非交易日會回 0 筆資料（不會給前一日），這裡是再上一道保險：
    萬一哪天期交所改成回最近一個交易日，這裡會擋下來而不是寫錯資料。
    """
    shown = page_data_date(html)
    if shown and shown != date:
        raise TaifexError("%s 回傳的資料日期是 %s，不是要的 %s——沒有寫入任何資料"
                          % (what, shown, date))


CHIP_FIELDS = ("foreign_fut", "top10_trader", "top10_specific",
               "foreign_opt", "dealer_opt")


def evidence(fut_html: str, opt_html: str, large_csv: str, date: str) -> str:
    """把「當初真的看到什麼」濃縮成一小段可存檔的文字。

    完整 HTML 一天約 1 MB，每天 commit 一年就是好幾百 MB，不值得。
    這裡只留實際用來算那五個數字的那幾列——要回頭查某天的數字時，
    需要的就是這些，而且一天只有 2 KB 左右。
    """
    lines = ["# %s  chips-kline 取數依據（只保留實際使用的列）" % date]

    lines.append("\n## 三大法人-期貨 / %s" % FUT_COMMODITY)
    commodity = None
    for cells in table_rows(fut_html):
        if len(cells) < 13 or cells[-13] not in IDENTITIES:
            continue
        extra = cells[:-13]
        if len(extra) == 2:
            commodity = extra[1]
        if commodity == FUT_COMMODITY:
            lines.append("\t".join(cells))

    lines.append("\n## 三大法人-選擇權 / %s" % OPT_COMMODITY)
    commodity, side = None, None
    for cells in table_rows(opt_html):
        if len(cells) < 13 or cells[-13] not in IDENTITIES:
            continue
        extra = cells[:-13]
        if len(extra) == 3:
            commodity, side = extra[1], extra[2]
        elif len(extra) == 1:
            side = extra[0]
        if commodity == OPT_COMMODITY:
            lines.append("\t".join(cells))

    lines.append("\n## 大額交易人 / %s" % LARGE_CONTRACT)
    for line in large_csv.replace("\r", "").split("\n"):
        c = [x.strip() for x in line.split(",")]
        if line.startswith("日期,") or (len(c) > 1 and c[1] == LARGE_CONTRACT):
            lines.append(line.rstrip())

    return "\n".join(lines) + "\n"
