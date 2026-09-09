#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓證交所每日收盤行情（STOCK_DAY），append 進 data/history/prices.csv。

STOCK_DAY 一次回傳一整個月，所以每天更新時只要打當月份、把整個月 upsert 進去，
既拿到今天的資料，也順便自我修補之前漏掉的日子。

用法：
    python3 fetch_prices.py                      # 當月
    python3 fetch_prices.py --month 202608       # 指定月份
    python3 fetch_prices.py --months-back 3      # 往回抓 3 個月（回填歷史用）
    python3 fetch_prices.py --use-cache          # 不連網，用 data/raw 既有回應重建
    python3 fetch_prices.py --force              # 允許覆寫已存在的日期

抓不到就中止，不會用估計值或其他來源補值。
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import store   # noqa: E402
import taifut  # noqa: E402

API = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
UA = "Mozilla/5.0 (compatible; chips-kline/1.0)"
RAW_DIR = os.path.join(store.DATA, "raw")


class FetchError(Exception):
    pass


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def source_of(code, cfg=None):
    """每檔標的的資料來源：twse（證交所個股）或 taifex（台指期近月）。"""
    cfg = cfg or load_config()
    for s in cfg["stocks"]:
        if s["code"] == code:
            return s.get("source", "twse")
    return "twse"


def raw_path(code, month, source="twse"):
    if source == "taifex":
        return os.path.join(RAW_DIR, "TAIFUT_%s_%s.csv" % (code, month))
    return os.path.join(RAW_DIR, "STOCK_DAY_%s_%s.json" % (code, month))


def month_payload(code, month, source="twse", use_cache=False):
    """抓（或讀快取）某檔標的某個月的原始回應。兩種來源共用的入口。"""
    p = raw_path(code, month, source)
    if use_cache:
        if not os.path.exists(p):
            raise FetchError("找不到快取檔 %s" % p)
        with open(p, encoding="utf-8") as f:
            return f.read() if source == "taifex" else json.load(f)

    try:
        payload = taifut.fetch_month(month) if source == "taifex" else fetch_raw(code, month)
    except taifut.TaifutError as e:
        raise FetchError(str(e))
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        if source == "taifex":
            f.write(payload)
        else:
            json.dump(payload, f, ensure_ascii=False)
    return payload


def parse_month(payload, code, source="twse"):
    try:
        return taifut.parse(payload, code) if source == "taifex" else parse(payload, code)
    except taifut.TaifutError as e:
        raise FetchError(str(e))


def fetch_raw(stock_no, month, retries=3):
    url = "%s?date=%s01&stockNo=%s&response=json" % (API, month, stock_no)
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("stat") != "OK":
                raise FetchError("%s %s 回傳 stat=%r（該月可能還沒有資料）"
                                 % (stock_no, month, payload.get("stat")))
            if not payload.get("data"):
                raise FetchError("%s %s 回傳 data 為空" % (stock_no, month))
            return payload
        except FetchError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * attempt)
    raise FetchError("%s %s 連線失敗：%s" % (stock_no, month, last))


def load_cached(stock_no, month):
    p = raw_path(stock_no, month)
    if not os.path.exists(p):
        raise FetchError("找不到快取檔 %s" % p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def to_ad_date(roc):
    y, m, d = roc.strip().split("/")
    return "%04d/%s/%s" % (int(y) + 1911, m, d)


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "-", "X", "x"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse(payload, code):
    """欄位順序：日期、成交股數、成交金額、開盤價、最高價、最低價、收盤價、漲跌價差、成交筆數"""
    out = []
    for r in payload["data"]:
        out.append({
            "date": to_ad_date(r[0]), "code": code,
            "open": num(r[3]), "high": num(r[4]), "low": num(r[5]),
            "close": num(r[6]), "change": num(r[7]), "volume": num(r[1]),
        })
    return out


def months_ending(month, back):
    y, m = int(month[:4]), int(month[4:])
    out = []
    for _ in range(back):
        out.append("%04d%02d" % (y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(out)


def run(months, codes, use_cache=False, force=False, quiet=False):
    os.makedirs(RAW_DIR, exist_ok=True)
    cfg = load_config()
    rows, errors = [], []
    for month in months:
        for i, code in enumerate(codes):
            source = source_of(code, cfg)
            try:
                payload = month_payload(code, month, source, use_cache)
                src = "快取" if use_cache else ("期交所" if source == "taifex" else "證交所")
            except FetchError as e:
                errors.append(str(e))
                continue
            got = parse_month(payload, code, source)
            rows.extend(got)
            if not quiet:
                print("  %s %s：%d 筆（%s）" % (code, month, len(got), src))
            if not use_cache and i < len(codes) - 1:
                time.sleep(3)
    if errors:
        raise FetchError("；".join(errors))
    if not rows:
        raise FetchError("什麼都沒抓到")
    a, u, s = store.upsert_prices(rows, force=force)
    if not quiet:
        print("  prices.csv：新增 %d / 覆寫 %d / 已存在略過 %d" % (a, u, s))
    return a, u, s


def main():
    cfg = load_config()
    today = dt.datetime.now().strftime("%Y%m")
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default=today, help="YYYYMM，預設當月")
    ap.add_argument("--months-back", type=int, default=1, help="含當月往回抓幾個月")
    ap.add_argument("--stocks", default=",".join(s["code"] for s in cfg["stocks"]))
    ap.add_argument("--use-cache", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    months = months_ending(args.month, max(args.months_back, 1))
    codes = [c.strip() for c in args.stocks.split(",") if c.strip()]
    try:
        run(months, codes, args.use_cache, args.force, args.quiet)
    except FetchError as e:
        print("\n[中止] %s" % e, file=sys.stderr)
        print("       歷史檔沒有被修改。請確認網路 / 月份後重跑，"
              "不要用其他來源的估計值填補。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
