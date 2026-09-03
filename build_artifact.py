#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 index.html / style.css / app.js / data 打包成單一 HTML（可直接分享或發布）。

用法：python3 build_artifact.py  ->  產生 dist/single.html
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))

def read(*p):
    with open(os.path.join(HERE, *p), encoding="utf-8") as f:
        return f.read()

html = read("index.html")
body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)
# 拿掉外部 css / data / app 的引用，改成內嵌
body = re.sub(r'<script src="(data/[^"]+|app\.js)"></script>', "", body)
body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)

prices = json.loads(read("data", "prices.json"))
chips = read("data", "chips.csv")

out = []
out.append("<title>%s</title>" % title)
out.append("<style>\n%s</style>" % read("style.css"))
out.append(body.strip())  # body 末尾已含 echarts CDN 標籤 + jsdelivr 備援，順序不能動
out.append("<script>window.__PRICES__ = %s;\nwindow.__CHIPS_CSV__ = %s;</script>"
           % (json.dumps(prices, ensure_ascii=False), json.dumps(chips, ensure_ascii=False)))
out.append("<script>\n%s</script>" % read("app.js"))

os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
dest = os.path.join(HERE, "dist", "single.html")
with open(dest, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
print("已寫出 %s（%.0f KB）" % (dest, os.path.getsize(dest) / 1024))
