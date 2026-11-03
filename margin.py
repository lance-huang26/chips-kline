#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盤融資維持率。

公式（由使用者指定）：

    分子 = Σ（每檔股票融資餘額張數 × 1000 × 當日收盤價）   ← 擔保品現值
    分母 = 上市 + 上櫃的「融資金額」合計                    ← 投資人實際借到的錢
    維持率 = 分子 / 分母 × 100%

直覺：分子是券商手上抱著的股票值多少錢，分母是借出去的錢。維持率往下掉代表
整體擔保品在縮水，接近 130% 就是全市場斷頭壓力的區域。注意這是**總量**的
維持率，不是任何一個人的維持率——個別帳戶的維持率分布比這個數字寬得多。

四個來源（都是官方，2026/11 實測）：

  上市融資張數＋融資金額
    GET https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN
            ?date=YYYYMMDD&selectType=ALL&response=json
    tables[0] = 信用交易統計（表尾合計），「融資(交易單位)」的「今日餘額」是總張數，
                「融資金額(仟元)」的「今日餘額」是分母。
    tables[1] = 融資融券彙總(全部)，每檔一列，「今日餘額」（第 7 欄）是該檔張數。

  上市收盤價
    GET https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX
            ?date=YYYYMMDD&type=ALLBUT0999&response=json
    取標題含「每日收盤行情」那張表：收盤價第 9 欄、最後揭示買價第 12 欄。
    有些個股當天沒有成交（停牌、全天無量），收盤價是空的，所以不能假設每檔都有價。

  上櫃融資張數＋融資金額
    GET https://www.tpex.org.tw/www/zh-tw/margin/balance?date=YYYY/MM/DD&response=json
    tables[0].data 每檔一列，「資餘額」第 7 欄；
    tables[0].summary 兩列：「合計(張)」與「融資金(仟元)」，各取第 7 欄（今日）。
    ——summary 是**和 data 分開的**欄位，不在 data 裡面，第一次寫的時候很容易漏掉。

  上櫃收盤價
    POST https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes
         date=YYYY/MM/DD&type=AL&response=json
    收盤第 3 欄、最後買價第 12 欄。回傳有兩張表（上櫃股票行情、管理股票），兩張都要掃。

四個閘門，任何一個不過就整天不寫（寧可明天補抓，也不要 publish 一個安靜偏低的數字）：

  1. 欄位名稱要和預期一致——來源改版時一定要吵出來，不能默默用錯欄位。
  2. 回傳的日期要等於我們要的日期。四個端點都會把日期原封不動回給你，
     不檢查的話「問 10/31 拿到 11/02」會變成一筆假資料。
  3. 個股張數合計要等於表尾合計。這是最強的檢查：單位、欄位、漏列、
     重複列，任何一種錯都會讓它對不上。實測 2026/11/02 上市 9,571,091、
     上櫃 2,308,916，兩邊都是分毫不差。
  4. 抓不到價格的張數佔比要小於 MISSING_LOTS_MAX。少數停牌／全天無量的個股
     本來就沒有收盤價（2026/11/02 上市有 1589、上櫃有 4207），
     硬是當 0 會讓分子偏低，所以先用同一天的「最後揭示買價」補，
     剩下真的補不到的才跳過；跳過的量一超過門檻就讓整天失敗。

非交易日不是錯誤：證交所會回 stat != "OK"，櫃買會回 stat == "ok" 但 data 是空的。
兩種都當「尚未公布」，回 None，讓呼叫端照常結束（和 taifex.NoDataForDate 同一個邏輯）。
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import netssl

UA = "Mozilla/5.0 (compatible; chips-kline/1.0)"

TWSE_MARGIN = ("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
               "?date=%s&selectType=ALL&response=json")
# type=ALLBUT0999 是「全部（不含權證、牛熊證、可展延牛熊證）」。
# 用它而不是 type=ALL 的理由：ALL 有四萬多列、5.1 MB，ALLBUT0999 只有 1,389 列、
# 232 KB（小 22 倍），而被排除掉的那三類商品本來就不能融資。
# 2026/11/02 實測兩者算出的分子完全相同（1,069,199,007,460），
# 抓不到價格的個股也是同一檔。回填 90 天時這個差別是 20 MB 對 460 MB。
TWSE_QUOTES = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
               "?date=%s&type=ALLBUT0999&response=json")
TPEX_MARGIN = ("https://www.tpex.org.tw/www/zh-tw/margin/balance"
               "?date=%s&response=json")
TPEX_QUOTES = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"

# 抓不到價格的張數最多可以佔總張數的多少。0.5% 是實測值的 10 倍以上
# （2026/11/02 上市 0.0004%、上櫃 0.03%），所以平常絕對不會踩到；
# 真的踩到就代表收盤行情那張表缺了一大塊，那時候寧可整天不寫。
MISSING_LOTS_MAX = 0.005

TWSE_MARGIN_SUM_FIELDS = ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"]
TWSE_MARGIN_ROW_HEAD = ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額"]
TWSE_QUOTE_HEAD = ["證券代號", "證券名稱"]
TWSE_QUOTE_CLOSE = "收盤價"
TWSE_QUOTE_BID = "最後揭示買價"

TPEX_MARGIN_HEAD = ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額"]
TPEX_QUOTE_HEAD = ["代號", "名稱", "收盤"]
TPEX_QUOTE_CLOSE = "收盤"
TPEX_QUOTE_BID = "最後買價"


class MarginError(Exception):
    """抓取或驗證失敗。呼叫端應該讓整天不寫入。"""


class NoMarginForDate(Exception):
    """那天沒有資料（非交易日，或還沒公布）。不是錯誤。"""


# ------------------------------------------------------------------ 連線
def _get(url, body=None, retries=3, label=""):
    data = body.encode("ascii") if body else None
    headers = {"User-Agent": UA}
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60,
                                        context=netssl.context()) as resp:
                raw = resp.read()
            break
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * attempt)
    else:
        raise MarginError("連線 %s 失敗：%s%s" % (label or url, last,
                                                netssl.explain(last)))
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        raise MarginError("%s 回的不是 JSON（前 120 字：%r）——"
                          "來源可能改版或正在維護" % (label or url, text[:120]))


def _num(s):
    """「1,234.5」→ 1234.5；「--」「」「 」→ None。"""
    s = str(s if s is not None else "").replace(",", "").replace("+", "").strip()
    if s in ("", "-", "--", "---", "X", "x"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _idx(fields, name, label):
    if name not in fields:
        raise MarginError("%s 找不到欄位「%s」（表頭是 %s…）——"
                          "來源改版了，沒有寫入任何資料"
                          % (label, name, "、".join(fields[:6])))
    return fields.index(name)


def _head_ok(fields, expect, label):
    got = [f.strip() for f in fields[:len(expect)]]
    if got != expect:
        raise MarginError("%s 的表頭變了：預期 %s，實際 %s——"
                          "來源改版了，沒有寫入任何資料" % (label, expect, got))


def _roc_to_iso(s):
    """「115/11/02」→「2026/11/02」。已經是西元就原樣回傳。"""
    s = (s or "").strip()
    parts = s.split("/")
    if len(parts) == 3 and len(parts[0]) == 3 and parts[0].isdigit():
        return "%04d/%s/%s" % (int(parts[0]) + 1911, parts[1], parts[2])
    return s


# ------------------------------------------------------------------ 上市
def parse_twse_margin(payload, want_compact):
    """回傳 ({code: 張數}, 表尾張數, 融資金額仟元)。沒資料丟 NoMarginForDate。"""
    if str(payload.get("stat", "")).upper() != "OK":
        raise NoMarginForDate("證交所融資彙總回「%s」——%s 應該不是交易日，"
                              "或還沒公布" % (payload.get("stat"), want_compact))
    got = str(payload.get("date", "")).strip()
    if got and got != want_compact:
        raise MarginError("證交所融資彙總要的是 %s，回的是 %s——沒有寫入任何資料"
                          % (want_compact, got))

    tables = payload.get("tables") or []
    if len(tables) < 2:
        raise NoMarginForDate("證交所融資彙總只有 %d 張表，%s 應該還沒公布"
                              % (len(tables), want_compact))

    _head_ok(tables[0]["fields"], TWSE_MARGIN_SUM_FIELDS, "證交所信用交易統計")
    today = _idx(tables[0]["fields"], "今日餘額", "證交所信用交易統計")
    foot_lots = foot_amount = None
    for row in tables[0]["data"]:
        item = str(row[0]).strip()
        if item.startswith("融資(交易單位)"):
            foot_lots = _num(row[today])
        elif item.startswith("融資金額(仟元)"):
            foot_amount = _num(row[today])
    if foot_lots is None or foot_amount is None:
        raise MarginError("證交所信用交易統計裡找不到「融資(交易單位)」或"
                          "「融資金額(仟元)」的今日餘額——沒有寫入任何資料")

    _head_ok(tables[1]["fields"], TWSE_MARGIN_ROW_HEAD, "證交所融資融券彙總")
    col = TWSE_MARGIN_ROW_HEAD.index("今日餘額")
    lots = {}
    for row in tables[1]["data"]:
        code = str(row[0]).strip()
        v = _num(row[col])
        if not code or not v or v <= 0:
            continue
        lots[code] = lots.get(code, 0) + int(round(v))
    if not lots:
        raise NoMarginForDate("證交所融資融券彙總一檔都沒有，%s 應該還沒公布"
                              % want_compact)
    return lots, int(round(foot_lots)), int(round(foot_amount))


def parse_twse_quotes(payload, want_compact):
    """回傳 {code: (收盤價, 最後揭示買價)}；兩個都可能是 None。"""
    if str(payload.get("stat", "")).upper() != "OK":
        raise NoMarginForDate("證交所收盤行情回「%s」——%s 應該不是交易日"
                              % (payload.get("stat"), want_compact))
    got = str(payload.get("date", "")).strip()
    if got and got != want_compact:
        raise MarginError("證交所收盤行情要的是 %s，回的是 %s——沒有寫入任何資料"
                          % (want_compact, got))

    table = None
    for t in payload.get("tables") or []:
        if "每日收盤行情" in str(t.get("title") or ""):
            table = t
            break
    if table is None:
        raise MarginError("證交所回傳裡找不到「每日收盤行情」那張表（有 %s）——"
                          "來源改版了，沒有寫入任何資料"
                          % [str(t.get("title"))[:14] for t in
                             (payload.get("tables") or [])][:6])

    _head_ok(table["fields"], TWSE_QUOTE_HEAD, "證交所每日收盤行情")
    ci = _idx(table["fields"], TWSE_QUOTE_CLOSE, "證交所每日收盤行情")
    bi = _idx(table["fields"], TWSE_QUOTE_BID, "證交所每日收盤行情")
    out = {}
    for row in table["data"]:
        code = str(row[0]).strip()
        if code:
            out[code] = (_num(row[ci]), _num(row[bi]))
    return out


# ------------------------------------------------------------------ 上櫃
def parse_tpex_margin(payload, want_compact):
    """回傳 ({code: 張數}, 表尾張數, 融資金仟元)。"""
    tables = payload.get("tables") or []
    if not tables:
        raise NoMarginForDate("櫃買融資餘額一張表都沒有，%s 應該還沒公布"
                              % want_compact)
    t = tables[0]
    rows = t.get("data") or []
    # 櫃買在非交易日也回 stat:"ok"，只是 data 是空的 —— 不能只看 stat。
    if not rows:
        raise NoMarginForDate("櫃買融資餘額是空的——%s 應該不是交易日，或還沒公布"
                              % want_compact)

    got = str(payload.get("date", "")).strip()
    if got and got != want_compact:
        raise MarginError("櫃買融資餘額要的是 %s，回的是 %s——沒有寫入任何資料"
                          % (want_compact, got))
    # tables[0].date 是民國年，和頂層 date 分開放，兩個都要對
    tdate = _roc_to_iso(t.get("date"))
    want_slash = "%s/%s/%s" % (want_compact[:4], want_compact[4:6], want_compact[6:])
    if tdate and tdate != want_slash:
        raise MarginError("櫃買融資餘額表上寫的是 %s，我們要的是 %s——"
                          "沒有寫入任何資料" % (tdate, want_slash))

    _head_ok(t["fields"], TPEX_MARGIN_HEAD, "櫃買融資餘額")
    col = TPEX_MARGIN_HEAD.index("資餘額")
    lots = {}
    for row in rows:
        code = str(row[0]).strip()
        v = _num(row[col])
        if not code or not v or v <= 0:
            continue
        lots[code] = lots.get(code, 0) + int(round(v))

    foot_lots = foot_amount = None
    for row in t.get("summary") or []:
        label = str(row[1]).strip() if len(row) > 1 else ""
        if label.startswith("合計(張)"):
            foot_lots = _num(row[col])
        elif label.startswith("融資金"):
            foot_amount = _num(row[col])
    if foot_lots is None or foot_amount is None:
        raise MarginError("櫃買融資餘額的 summary 裡找不到「合計(張)」或"
                          "「融資金(仟元)」——來源改版了，沒有寫入任何資料")
    if not lots:
        raise NoMarginForDate("櫃買融資餘額一檔都沒有，%s 應該還沒公布" % want_compact)
    return lots, int(round(foot_lots)), int(round(foot_amount))


def parse_tpex_quotes(payload, want_compact):
    """回傳 {code: (收盤, 最後買價)}。上櫃行情和管理股票兩張表都要掃。"""
    tables = payload.get("tables") or []
    if not tables:
        raise NoMarginForDate("櫃買收盤行情一張表都沒有，%s 應該還沒公布"
                              % want_compact)
    got = str(payload.get("date", "")).strip()
    if got and got != want_compact:
        raise MarginError("櫃買收盤行情要的是 %s，回的是 %s——沒有寫入任何資料"
                          % (want_compact, got))

    out = {}
    seen_rows = False
    for t in tables:
        fields = t.get("fields") or []
        if not fields:
            continue
        _head_ok(fields, TPEX_QUOTE_HEAD, "櫃買收盤行情（%s）" % t.get("title"))
        ci = _idx(fields, TPEX_QUOTE_CLOSE, "櫃買收盤行情")
        bi = _idx(fields, TPEX_QUOTE_BID, "櫃買收盤行情")
        for row in (t.get("data") or []):
            code = str(row[0]).strip()
            if code:
                seen_rows = True
                out[code] = (_num(row[ci]), _num(row[bi]))
    if not seen_rows:
        raise NoMarginForDate("櫃買收盤行情是空的——%s 應該不是交易日" % want_compact)
    return out


# ------------------------------------------------------------------ 計算
def one_market(lots, foot_lots, foot_amount, quotes, label):
    """回傳 (分子, 分母, 缺價張數, 缺價清單)。閘門不過丟 MarginError。"""
    total = sum(lots.values())
    if total != foot_lots:
        raise MarginError(
            "%s 個股融資張數加總 %s 和表尾合計 %s 對不上（差 %s 張）——"
            "欄位或單位可能錯了，沒有寫入任何資料"
            % (label, format(total, ","), format(foot_lots, ","),
               format(total - foot_lots, ",")))
    if foot_amount <= 0:
        raise MarginError("%s 的融資金額合計是 %s，不是正數——沒有寫入任何資料"
                          % (label, foot_amount))

    numerator = 0.0
    miss_lots = 0
    missing = []
    for code, n in lots.items():
        close, bid = quotes.get(code, (None, None))
        price = close if (close and close > 0) else bid
        if not price or price <= 0:
            miss_lots += n
            missing.append((code, n))
            continue
        numerator += n * 1000.0 * price

    share = miss_lots / float(total)
    if share > MISSING_LOTS_MAX:
        missing.sort(key=lambda x: -x[1])
        raise MarginError(
            "%s 有 %s 張（%.3f%%，超過上限 %.1f%%）抓不到收盤價也抓不到最後買價，"
            "分子會偏低，所以整天不寫。量最大的幾檔：%s"
            % (label, format(miss_lots, ","), share * 100,
               MISSING_LOTS_MAX * 100,
               "、".join("%s %s張" % (c, format(n, ",")) for c, n in missing[:8])))

    return numerator, foot_amount * 1000.0, miss_lots, missing


def compute(twse_margin, twse_quotes, tpex_margin, tpex_quotes, date):
    """四份 payload → 一筆 {date, ratio, twse_ratio, otc_ratio, numerator, denominator}。"""
    compact = date.replace("/", "")
    slash = "%s/%s/%s" % (compact[:4], compact[4:6], compact[6:])

    a_lots, a_foot, a_amt = parse_twse_margin(twse_margin, compact)
    a_q = parse_twse_quotes(twse_quotes, compact)
    b_lots, b_foot, b_amt = parse_tpex_margin(tpex_margin, compact)
    b_q = parse_tpex_quotes(tpex_quotes, compact)

    a_num, a_den, a_miss, _ = one_market(a_lots, a_foot, a_amt, a_q, "上市")
    b_num, b_den, b_miss, _ = one_market(b_lots, b_foot, b_amt, b_q, "上櫃")

    num, den = a_num + b_num, a_den + b_den
    return {
        "date": slash,
        "ratio": round(num / den * 100, 2),
        "twse_ratio": round(a_num / a_den * 100, 2),
        "otc_ratio": round(b_num / b_den * 100, 2),
        "numerator": int(round(num)),
        "denominator": int(round(den)),
        "_detail": {
            "twse_lots": a_foot, "otc_lots": b_foot,
            "twse_miss_lots": a_miss, "otc_miss_lots": b_miss,
        },
    }


def fetch_day(date):
    """抓某一天並算出維持率。非交易日丟 NoMarginForDate，其他問題丟 MarginError。"""
    compact = date.replace("/", "")
    slash = "%s/%s/%s" % (compact[:4], compact[4:6], compact[6:])
    a = _get(TWSE_MARGIN % compact, label="證交所融資彙總 %s" % date)
    b = _get(TWSE_QUOTES % compact, label="證交所收盤行情 %s" % date)
    c = _get(TPEX_MARGIN % urllib.parse.quote(slash, safe="/"),
             label="櫃買融資餘額 %s" % date)
    d = _get(TPEX_QUOTES,
             body=urllib.parse.urlencode({"date": slash, "type": "AL",
                                          "response": "json"}),
             label="櫃買收盤行情 %s" % date)
    return compute(a, b, c, d, date)


def main():
    ap = argparse.ArgumentParser(description="算某一天的大盤融資維持率（只印，不寫檔）")
    ap.add_argument("--date", help="YYYY/MM/DD，留空 = 今天（台北時間）")
    args = ap.parse_args()
    date = args.date or dt.datetime.now(
        dt.timezone(dt.timedelta(hours=8))).strftime("%Y/%m/%d")
    try:
        r = fetch_day(date)
    except NoMarginForDate as e:
        print("… %s" % e)
        return
    except MarginError as e:
        print("[失敗] %s" % e, file=sys.stderr)
        sys.exit(1)
    d = r.pop("_detail")
    print("%s  合併 %.2f%%（上市 %.2f%% / 上櫃 %.2f%%）"
          % (r["date"], r["ratio"], r["twse_ratio"], r["otc_ratio"]))
    print("    分子 %s 元 ÷ 分母 %s 元"
          % (format(r["numerator"], ","), format(r["denominator"], ",")))
    print("    張數 上市 %s / 上櫃 %s；抓不到價格 上市 %s / 上櫃 %s 張"
          % (format(d["twse_lots"], ","), format(d["otc_lots"], ","),
             format(d["twse_miss_lots"], ","), format(d["otc_miss_lots"], ",")))


if __name__ == "__main__":
    main()
