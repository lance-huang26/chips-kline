#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取證交所每日收盤行情（STOCK_DAY），產生前端用的 data/prices.json。

用法：
    python3 fetch_prices.py                       # 預設抓 2026/08，股票 2330,2308,2368
    python3 fetch_prices.py --month 202609        # 換成 2026 年 9 月
    python3 fetch_prices.py --stocks 2330,2454    # 換股票
    python3 fetch_prices.py --use-cache           # 不連網，直接用 data/raw/ 既有回應重建 prices.json

行為說明：
  * 每檔股票對 STOCK_DAY 只打一次 API（一次回傳整個月）。
  * 原始回應會存到 data/raw/STOCK_DAY_<代號>_<年月>.json，方便日後稽核或離線重建。
  * 任何一檔抓不到資料（連線失敗、stat 非 OK、data 為空）就直接中止並印出錯誤，
    不會用估計值或其他來源補值。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
UA = "Mozilla/5.0 (compatible; chips-kline/1.0)"

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")

DEFAULT_STOCKS = ["2330", "2308", "2368"]
STOCK_NAMES = {"2330": "台積電", "2308": "台達電", "2368": "金像電"}

# 前端算乖離率用的均線天數。要算出「顯示月份第一天」的均線，
# 必須另外備妥前面 MA_PERIOD-1 個交易日的收盤價，所以要多抓前幾個月當暖身資料。
MA_PERIOD = 20


class FetchError(Exception):
    pass


def raw_path(stock_no: str, month: str) -> str:
    return os.path.join(RAW_DIR, "STOCK_DAY_%s_%s.json" % (stock_no, month))


def fetch_raw(stock_no: str, month: str, retries: int = 3) -> dict:
    """打一次 API 取回整個月的資料。失敗會丟 FetchError。"""
    url = "%s?date=%s01&stockNo=%s&response=json" % (API, month, stock_no)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("stat") != "OK":
                raise FetchError("%s 回傳 stat=%r（可能該月尚無資料）" % (stock_no, payload.get("stat")))
            if not payload.get("data"):
                raise FetchError("%s 回傳 data 為空" % stock_no)
            return payload
        except FetchError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(3 * attempt)
    raise FetchError("%s 連線失敗：%s" % (stock_no, last_err))


def load_cached(stock_no: str, month: str) -> dict:
    p = raw_path(stock_no, month)
    if not os.path.exists(p):
        raise FetchError("找不到快取檔 %s，請先在有網路的環境跑一次（不加 --use-cache）" % p)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def to_ad_date(roc: str) -> str:
    """民國 115/08/03 -> 西元 2026/08/03"""
    y, m, d = roc.strip().split("/")
    return "%04d/%s/%s" % (int(y) + 1911, m, d)


def num(s):
    """清掉千分位逗號與前後空白；'--' / 'X' / 空字串 視為缺值回傳 None。"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "-", "X", "x"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse(payload: dict, stock_no: str) -> list:
    """
    data 欄位順序：日期、成交股數、成交金額、開盤價、最高價、最低價、收盤價、漲跌價差、成交筆數
    """
    rows = []
    for r in payload["data"]:
        o, h, l, c = num(r[3]), num(r[4]), num(r[5]), num(r[6])
        rows.append({
            "date": to_ad_date(r[0]),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "change": num(r[7]),      # 漲跌價差，可能是 None
            "volume": num(r[1]),      # 成交股數
            # 缺任一價格就算無交易 → 前端 K 棒留空，不補前一日
            "valid": None not in (o, h, l, c),
        })
    rows.sort(key=lambda x: x["date"])
    return rows


def prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[4:])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return "%04d%02d" % (y, m)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="202608", help="YYYYMM，例如 202609")
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS), help="逗號分隔的股票代號")
    ap.add_argument("--use-cache", action="store_true", help="不連網，改用 data/raw 既有回應")
    ap.add_argument("--warmup-months", type=int, default=1,
                    help="額外往前抓幾個月當均線暖身資料（預設 1，MA20 夠用）")
    args = ap.parse_args()

    month = args.month.strip()
    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]

    os.makedirs(RAW_DIR, exist_ok=True)

    warm_months = []
    m = month
    for _ in range(max(args.warmup_months, 0)):
        m = prev_month(m)
        warm_months.append(m)
    warm_months.reverse()  # 由舊到新

    def get(stock_no, mm, required=True):
        """回傳 (payload, 來源)。required=False 時抓不到只警告不中止。"""
        try:
            if args.use_cache:
                return load_cached(stock_no, mm), "快取"
            payload = fetch_raw(stock_no, mm)
            with open(raw_path(stock_no, mm), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            return payload, "API"
        except FetchError as e:
            if required:
                print("\n[中止] 取不到 %s %s 的股價資料：%s" % (stock_no, mm, e), file=sys.stderr)
                print("       沒有寫出 prices.json。請確認網路 / 月份後重跑，"
                      "不要用其他來源的估計值填補。", file=sys.stderr)
                sys.exit(1)
            print("  [注意] %s %s 的暖身資料取不到（%s），該股前幾天的均線會顯示 —"
                  % (stock_no, mm, e), file=sys.stderr)
            return None, None

    out = {"month": month, "maPeriod": MA_PERIOD, "warmupMonths": warm_months, "stocks": []}
    for i, stock_no in enumerate(stocks):
        payload, src = get(stock_no, month, required=True)

        # 從 title 取股名，例如 "115年08月 2330 台積電   各日成交資訊"
        name = STOCK_NAMES.get(stock_no, "")
        title = payload.get("title", "")
        parts = title.split()
        if len(parts) >= 3:
            name = parts[2]

        rows = parse(payload, stock_no)

        # 暖身：只留日期與收盤價，前端接在 rows 前面算均線
        warm = []
        for mm in warm_months:
            wp, _ = get(stock_no, mm, required=False)
            if not wp:
                continue
            for r in parse(wp, stock_no):
                if r["valid"]:
                    warm.append({"date": r["date"], "close": r["close"]})
            if not args.use_cache:
                time.sleep(3)
        warm.sort(key=lambda x: x["date"])

        need = MA_PERIOD - 1
        if len(warm) < need:
            print("  [注意] %s 暖身只有 %d 筆，不足 %d 筆，%s 月初幾天算不出 MA%d"
                  % (stock_no, len(warm), need, month, MA_PERIOD), file=sys.stderr)

        out["stocks"].append({"code": stock_no, "name": name, "rows": rows, "warmup": warm})
        print("  %s %s：%d 筆（%s）＋ 暖身 %d 筆" % (stock_no, name, len(rows), src, len(warm)))

        if not args.use_cache and i < len(stocks) - 1:
            time.sleep(3)  # 對證交所客氣一點

    dest = os.path.join(DATA_DIR, "prices.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("已寫出 %s" % dest)

    # 直接用 file:// 開啟 index.html 時 fetch() 會被瀏覽器 CORS 擋掉，
    # 因此同時輸出一份 .js 版本（用 <script> 載入不受限制）作為備援。
    with open(os.path.join(DATA_DIR, "prices.js"), "w", encoding="utf-8") as f:
        f.write("window.__PRICES__ = ")
        json.dump(out, f, ensure_ascii=False)
        f.write(";\n")
    chips_csv = os.path.join(DATA_DIR, "chips.csv")
    if os.path.exists(chips_csv):
        with open(chips_csv, "r", encoding="utf-8") as f:
            csv_text = f.read()
        with open(os.path.join(DATA_DIR, "chips.js"), "w", encoding="utf-8") as f:
            f.write("window.__CHIPS_CSV__ = ")
            json.dump(csv_text, f, ensure_ascii=False)
            f.write(";\n")
        print("已寫出 %s/prices.js 與 %s/chips.js（file:// 直開用的備援）" % (DATA_DIR, DATA_DIR))
    else:
        print("  [注意] 找不到 data/chips.csv，未產生 chips.js", file=sys.stderr)

    # 交易日一致性檢查：三檔之間、以及與 chips.csv
    date_sets = {s["code"]: [r["date"] for r in s["rows"]] for s in out["stocks"]}
    base_code, base = next(iter(date_sets.items()))
    for code, ds in date_sets.items():
        if ds != base:
            print("  [注意] %s 的交易日與 %s 不一致：%s" %
                  (code, base_code, sorted(set(ds) ^ set(base))), file=sys.stderr)

    chips = os.path.join(DATA_DIR, "chips.csv")
    if os.path.exists(chips):
        with open(chips, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()][1:]
        cdates = [l.split(",")[0] for l in lines]
        only_price = sorted(set(base) - set(cdates))
        only_chip = sorted(set(cdates) - set(base))
        if only_price or only_chip:
            print("  [注意] 股價與籌碼交易日不一致："
                  "只有股價有 %s；只有籌碼有 %s" % (only_price, only_chip), file=sys.stderr)
        else:
            print("  交易日檢查：股價與 chips.csv 完全對齊（%d 天）" % len(base))


if __name__ == "__main__":
    main()
