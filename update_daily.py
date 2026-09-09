#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日更新：抓取 → 驗證閘門 → append 歷史檔 → build site.json。

用法：
    python3 update_daily.py                        # 今天（台北時間）
    python3 update_daily.py --date 2026/09/08
    python3 update_daily.py --from 2026/09/01 --to 2026/09/08   # 回填
    python3 update_daily.py --max-stale-days 5     # 太久沒有新資料就當失敗（給排程用）
    python3 update_daily.py --dry-run              # 只抓不寫

離開碼：
    0  正常（含「今天還沒有資料」——非交易日或尚未公布都算正常）
    1  抓取或解析失敗，或驗證閘門沒過（歷史檔完全沒有被修改）
    2  太久沒有新資料（--max-stale-days 觸發），管線可能已經壞掉

設計重點：
  * 不看時鐘，只看資料。每個來源回傳的內容都帶自己的日期，對不上就當作尚未公布。
    因此也不需要維護台股行事曆——非交易日來源就是沒有資料。
  * 五欄同進退。任何一項缺了，當天完全不寫入，寧可隔天補抓。
  * 冪等。已存在的日期預設跳過，所以補跑 / 手動觸發 / 兩個排程重疊都安全。
"""

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_site  # noqa: E402
import fetch_prices  # noqa: E402
import store  # noqa: E402
import taifex  # noqa: E402

TPE = dt.timezone(dt.timedelta(hours=8))   # 台灣沒有日光節約，固定 +8
EVID_DIR = os.path.join(store.DATA, "raw", "chips")


def today_tpe():
    return dt.datetime.now(TPE).strftime("%Y/%m/%d")


def daterange(a, b):
    d0 = dt.date(int(a[:4]), int(a[5:7]), int(a[8:10]))
    d1 = dt.date(int(b[:4]), int(b[5:7]), int(b[8:10]))
    out = []
    while d0 <= d1:
        if d0.weekday() < 5:            # 週末直接跳過，省下沒必要的請求
            out.append(d0.strftime("%Y/%m/%d"))
        d0 += dt.timedelta(days=1)
    return out


# STOCK_DAY 一次回傳一整個月，所以同一個月份只要抓一次。
# 回填兩個月（約 43 個工作日）時，這個快取讓股價請求從 129 次降到 6 次。
_PRICE_CACHE = {}


def stock_day(code, month, use_cache=False):
    key = (code, month)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]
    source = fetch_prices.source_of(code)
    payload = fetch_prices.month_payload(code, month, source, use_cache)
    _PRICE_CACHE[key] = (payload, source)
    return _PRICE_CACHE[key]


def choose_dates(date=None, frm=None, to=None, days=None, today=None):
    """決定這次要處理哪些日期。優先順序：--from > --days > --date > 今天。

    --to 留空就是今天（空字串也算留空——workflow 的輸入欄沒填就是空字串，
    以前這裡會把空字串直接傳給 argparse 而爆掉）。
    """
    today = today or today_tpe()
    frm = (frm or "").strip() or None
    to = (to or "").strip() or None
    date = (date or "").strip() or None

    if frm:
        return daterange(frm, to or today)
    if to:
        raise ValueError("只給 --to 沒有 --from；要回填請給 --from，或改用 --days N")
    if days is not None:
        if days < 1:
            raise ValueError("--days 要是正整數，收到 %r" % days)
        end = dt.date(int(today[:4]), int(today[5:7]), int(today[8:10]))
        start = end - dt.timedelta(days=days - 1)
        return daterange(start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d"))
    return [date or today]


def prices_for_date(date, codes, use_cache=False):
    """回傳 {code: row}；任何一檔缺當天資料就回 None（代表尚未公布）。"""
    month = date[:4] + date[5:7]
    got = {}
    for code in codes:
        payload, source = stock_day(code, month, use_cache)
        rows = {r["date"]: r for r in fetch_prices.parse_month(payload, code, source)}
        if date not in rows:
            return None, "%s 還沒有 %s 的收盤資料" % (code, date)
        got[code] = rows[date]
    return got, None


def one_day(date, cfg, force=False, dry_run=False, use_cache=False):
    """回傳 ('written' | 'skipped' | 'pending', 說明)。失敗會丟例外。"""
    codes = [s["code"] for s in cfg["stocks"]]

    existing = {r["date"] for r in store.read_chips()}
    if date in existing and not force:
        return "skipped", "已經有 %s 的籌碼資料（要重抓請加 --force）" % date

    # --- 抓籌碼（五欄同進退，任何一欄拿不到就整天不寫）---
    try:
        chip = taifex.fetch_day(date)
    except taifex.NoDataForDate as e:
        return "pending", str(e)

    # --- 抓股價 ---
    prices, why = prices_for_date(date, codes, use_cache=use_cache)
    if prices is None:
        return "pending", why

    # --- 驗證閘門 ---
    missing = [f for f in taifex.CHIP_FIELDS if chip.get(f) is None]
    if missing:
        raise taifex.TaifexError("%s 缺欄位 %s，沒有寫入任何資料" % (date, missing))
    for code, r in prices.items():
        if None in (r["open"], r["high"], r["low"], r["close"]):
            raise taifex.TaifexError(
                "%s %s 的 OHLC 有缺值 %s，沒有寫入任何資料" % (date, code, r))

    if dry_run:
        return "written", "（dry-run，沒有寫檔）%s" % _fmt(chip, prices)

    # --- 寫入 ---
    store.upsert_chips([chip], force=force)
    store.upsert_prices(list(prices.values()), force=force)

    os.makedirs(EVID_DIR, exist_ok=True)
    with open(os.path.join(EVID_DIR, date.replace("/", "-") + ".txt"),
              "w", encoding="utf-8") as f:
        f.write(taifex.evidence(chip["_raw"]["fut"], chip["_raw"]["opt"],
                                chip["_raw"]["large"], date))

    return "written", _fmt(chip, prices)


def _fmt(chip, prices):
    a = "  ".join("%s=%s" % (f, format(chip[f], ",")) for f in taifex.CHIP_FIELDS)
    b = "  ".join("%s收%s" % (c, format(int(r["close"]), ",")) if r["close"] == int(r["close"])
                  else "%s收%s" % (c, r["close"]) for c, r in sorted(prices.items()))
    return a + "\n           " + b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--from", dest="frm", help="回填起日 YYYY/MM/DD")
    ap.add_argument("--to", help="回填迄日，留空 = 今天")
    ap.add_argument("--days", type=int,
                    help="回填最近 N 個日曆日（60 ≒ 兩個月、120 ≒ 四個月）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--use-cache", action="store_true",
                    help="股價改用 data/raw 既有回應（離線測試用）")
    ap.add_argument("--max-stale-days", type=int, default=0,
                    help="最新籌碼日期比今天舊超過這麼多天就以離開碼 2 結束")
    args = ap.parse_args()

    cfg = build_site.load_config()

    try:
        dates = choose_dates(args.date, args.frm, args.to, args.days)
    except ValueError as e:
        ap.error(str(e))

    if not dates:
        print("指定的區間裡沒有任何工作日，沒事可做。")
        return

    if len(dates) > 1:
        print("要處理 %d 個工作日：%s ~ %s\n" % (len(dates), dates[0], dates[-1]))

    written = pending = skipped = 0
    for d in dates:
        try:
            status, msg = one_day(d, cfg, args.force, args.dry_run, args.use_cache)
        except (taifex.TaifexError, fetch_prices.FetchError) as e:
            print("\n[失敗] %s：%s" % (d, e), file=sys.stderr)
            print("       歷史檔沒有被修改。", file=sys.stderr)
            sys.exit(1)
        icon = {"written": "＋", "skipped": "・", "pending": "…"}[status]
        print("%s %s  %s" % (icon, d, msg))
        if status == "written":
            written += 1
        elif status == "skipped":
            skipped += 1
        else:
            pending += 1

    if len(dates) > 1:
        print("\n共 %d 天：新增 %d、已存在略過 %d、無資料 %d"
              % (len(dates), written, skipped, pending))

    if written and not args.dry_run:
        print("\n重建 site.json：")
        build_site.build()
    elif not written:
        print("\n沒有新資料，site.json 不動。")

    if args.max_stale_days:
        latest = store.latest_chip_date()
        if latest:
            last = dt.date(int(latest[:4]), int(latest[5:7]), int(latest[8:10]))
            age = (dt.datetime.now(TPE).date() - last).days
            if age > args.max_stale_days:
                print("\n[告警] 最新籌碼資料是 %s，已經 %d 天沒有更新——"
                      "管線可能壞了，或來源改版了。" % (latest, age), file=sys.stderr)
                sys.exit(2)


if __name__ == "__main__":
    main()
