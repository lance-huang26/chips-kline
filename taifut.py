#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台指期（TX）近月日 K，來自期交所「期貨每日交易行情」。

    POST /cht/3/futDataDown
    down_type=1&commodity_id=TX&queryStartDate=YYYY/MM/DD&queryEndDate=YYYY/MM/DD
    回傳 CSV・Big5，一次可以要一整個月。

挑「近月」的規則（實測 2026/08 結算日前後確認過）：

    * 契約 = TX
    * 交易時段 = 一般（8:45–13:45，和現貨盤對得起來；盤後那筆不用）
    * 到期月份是純 6 位數 —— 排除 202609/202610 這種「價差」列，
      那些列的價格是兩個月份的價差（三位數），誤抓的話 K 線會變成一堆 100~500 的數字
    * 同一天有多個月份時取最小的那個 = 近月

換月是自然發生的：結算日當天舊月份還在（例如 2026/08/19 仍看得到 202608），
隔天就從清單裡消失，最小月份自動變成下一個月。不需要寫死結算日。

代價：換月當天序列會有價差跳空。2026/08/19 收 44,612（202608）→
08/20 收 44,868（202609），但 202609 自己是從 44,528 漲到 44,868。
也就是連續近月序列在那天多了約 -84 點的人工落差。這是所有「近月連續」都有的性質，
本檔不做還原，只如實記錄；MA20 與乖離率會吸收到這個跳空，看的時候要有數。
"""

import calendar
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.taifex.com.tw"
PATH = "/cht/3/futDataDown"
UA = "Mozilla/5.0 (compatible; chips-kline/1.0)"

CONTRACT = "TX"
SESSION = "一般"
FRONT_MONTH_RE = re.compile(r"^\d{6}$")

COL = {
    "date": "交易日期",
    "contract": "契約",
    "expiry": "到期月份(週別)",
    "open": "開盤價",
    "high": "最高價",
    "low": "最低價",
    "close": "收盤價",
    "change": "漲跌價",
    "volume": "成交量",
    "session": "交易時段",
}


class TaifutError(Exception):
    pass


def month_range(month: str):
    y, m = int(month[:4]), int(month[4:])
    last = calendar.monthrange(y, m)[1]
    return "%04d/%02d/01" % (y, m), "%04d/%02d/%02d" % (y, m, last)


def fetch_month(month: str, retries: int = 3) -> str:
    a, b = month_range(month)
    body = urllib.parse.urlencode({
        "down_type": "1", "commodity_id": CONTRACT,
        "queryStartDate": a, "queryEndDate": b,
    }).encode("ascii")
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                BASE + PATH, data=body,
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
            return raw.decode("big5", errors="replace")
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * attempt)
    raise TaifutError("台指期 %s 連線失敗：%s" % (month, last))


def _num(s):
    s = (s or "").replace(",", "").replace("+", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse(csv_text: str, code: str = CONTRACT) -> list:
    """回傳 [{date, code, open, high, low, close, change, volume}]，每個交易日一筆（近月、一般時段）。"""
    lines = [l for l in csv_text.replace("\r", "").split("\n") if l.strip()]
    if not lines:
        raise TaifutError("台指期回傳空內容")

    header = [h.strip() for h in lines[0].split(",")]
    idx = {}
    for key, name in COL.items():
        if name not in header:
            raise TaifutError(
                "台指期 CSV 找不到欄位「%s」（表頭是 %s…）——期交所可能改版了，"
                "沒有寫入任何資料" % (name, ",".join(header[:6])))
        idx[key] = header.index(name)

    by_date = {}
    seen_contract = False
    for line in lines[1:]:
        c = [x.strip() for x in line.split(",")]
        if len(c) <= max(idx.values()):
            continue
        if c[idx["contract"]] != CONTRACT:
            continue
        seen_contract = True
        if c[idx["session"]] != SESSION:
            continue
        expiry = c[idx["expiry"]]
        if not FRONT_MONTH_RE.match(expiry):     # 排除價差列
            continue
        d = c[idx["date"]]
        cur = by_date.get(d)
        if cur is None or expiry < cur[0]:       # 取最小到期月份 = 近月
            by_date[d] = (expiry, c)

    # 「一筆 TX 都沒有」和「有 TX 但一筆都篩不出來」要分開處理：
    #   前者＝該月份還沒有資料（例如當月第一天就來抓），屬於正常，回空陣列讓上層當「尚未公布」；
    #   後者＝表格結構變了（時段名稱改了、到期月份格式變了…），必須吵出來，
    #        不然會安靜地少掉整段 K 線。
    if not by_date:
        if seen_contract:
            raise TaifutError(
                "台指期 CSV 有 %s 的列，但沒有任何「%s／純月份」的資料——"
                "期交所可能改版了，沒有寫入任何資料" % (CONTRACT, SESSION))
        return []

    out = []
    for d in sorted(by_date):
        expiry, c = by_date[d]
        out.append({
            "date": d, "code": code,
            "open": _num(c[idx["open"]]), "high": _num(c[idx["high"]]),
            "low": _num(c[idx["low"]]), "close": _num(c[idx["close"]]),
            "change": _num(c[idx["change"]]), "volume": _num(c[idx["volume"]]),
            "_expiry": expiry,
        })
    return out
