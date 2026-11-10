#!/usr/bin/env python3
"""
timotxt-downloader — 提莫書屋 (timotxt.com) 小說離線下載工具

提莫書屋用「自訂字體反爬」：把 ~150 個常用字替換成諺文等罕用碼位，
靠網頁的 @font-face 自訂字體 (bqg) 在瀏覽器渲染回正確字形。
直接抓 HTML 會得到一堆亂碼。

本工具下載該字體，用字形比對自動還原映射（或用內建已驗證映射表），
解碼後把整本書輸出成乾淨 txt。

用法:
    python3 timotxt_dl.py <book_id> [-o output.txt]

book_id 是書本網址 /<book_id>/dir 裡的那串數字，例如:
    https://www.timotxt.com/0704594194/dir  ->  book_id = 0704594194
"""
import argparse
import gzip
import html as _html
import json
import os
import re
import sys
import time
import urllib.request

BASE = "https://www.timotxt.com"
FONT_BASE = "https://www.timotxt.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(url, referer=None, tries=5, binary=False):
    last = None
    for i in range(tries):
        try:
            headers = {
                "User-Agent": UA,
                "Accept-Language": "zh-TW,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*",
            }
            if referer:
                headers["Referer"] = referer
            req = urllib.request.Request(url, headers=headers)
            r = urllib.request.urlopen(req, timeout=40)
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 + i * 3)
    raise last


def get_chapter_list(book_id):
    """回傳 [(chapter_num, title), ...]，以及書名、作者。"""
    html = fetch(f"{BASE}/{book_id}/dir", referer=f"{BASE}/")
    title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    book_title = re.sub("<[^>]+>", "", title_m.group(1)).strip() if title_m else book_id
    book_title = re.sub(r"\s*章節列表\s*$", "", book_title)
    author_m = re.search(r"作者\s*/\s*([^<\n]+)", html)
    author = author_m.group(1).strip() if author_m else "佚名"
    # 全部章節連結 /<book_id>/<n>.html
    nums = []
    seen = set()
    for m in re.finditer(rf"/{book_id}/(\d+)\.html", html):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            nums.append(n)
    nums.sort()
    return book_title, author, nums


def find_font_url(html, book_id):
    """從頁面 CSS 找出反爬字體的 URL。"""
    # 頁面載入的 CSS
    css_m = re.search(r'href="([^"]+\.css)"', html)
    css_urls = re.findall(r'href="([^"]+\.css)"', html)
    for cu in css_urls:
        if cu.startswith("//"):
            cu = "https:" + cu
        elif cu.startswith("/"):
            cu = BASE + cu
        try:
            css = fetch(cu, referer=f"{BASE}/{book_id}/")
        except Exception:  # noqa: BLE001
            continue
        fm = re.search(r"font-family:\s*'?bqg'?;[^}]*?url\(([^)]+?\.woff2?)\)", css)
        if not fm:
            # 退而求其次：任何 /fonts/*.woff2
            fm = re.search(r"url\((/fonts/[^)]+?\.woff2)\)", css)
        if fm:
            fu = fm.group(1).strip("'\" ")
            if fu.startswith("//"):
                fu = "https:" + fu
            elif fu.startswith("/"):
                fu = FONT_BASE + fu
            return fu
    return None


def build_mapping(font_bytes):
    """
    建立 碼位 -> 真實中文字 的映射表。
    1) 若字體與內建已驗證映射 (map.json) 相同 (woff2 位元組一致)，直接用。
    2) 否則用字形像素比對 Noto CJK 自動還原 (需 fonttools + pillow + numpy)。
    """
    import hashlib
    h = hashlib.md5(font_bytes).hexdigest()
    builtin = os.path.join(HERE, "map.json")
    builtin_font = os.path.join(HERE, "f24092.woff2")
    if os.path.exists(builtin) and os.path.exists(builtin_font):
        if hashlib.md5(open(builtin_font, "rb").read()).hexdigest() == h:
            m = json.load(open(builtin))
            print("[font] 使用內建已驗證映射 (f24092)", file=sys.stderr)
            return {int(k, 16): v for k, v in m.items()}
    print("[font] 字體不符內建表，改用像素比對自動還原…", file=sys.stderr)
    return _auto_mapping(font_bytes)


def _auto_mapping(font_bytes):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from fontTools.ttLib import TTFont
    import io

    tmp = os.path.join(HERE, "_tmp_font.woff2")
    open(tmp, "wb").write(font_bytes)
    ft = TTFont(tmp)
    cps = sorted(ft.getBestCmap().keys())

    ref_path = None
    for p in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]:
        if os.path.exists(p):
            ref_path = p
            break
    if not ref_path:
        raise RuntimeError("找不到 Noto/文泉驛 CJK 字體，無法自動還原；請安裝 fonts-noto-cjk")

    SZ = 40

    def vec(font, ch):
        img = Image.new("L", (SZ, SZ), 0)
        dr = ImageDraw.Draw(img)
        bb = dr.textbbox((0, 0), ch, font=font)
        w, hh = bb[2] - bb[0], bb[3] - bb[1]
        if w <= 0 or hh <= 0:
            return None
        dr.text(((SZ - w) / 2 - bb[0], (SZ - hh) / 2 - bb[1]), ch, font=font, fill=255)
        a = np.asarray(img, dtype=np.float32).ravel()
        n = np.linalg.norm(a)
        return a / n if n > 0 else None

    ref = ImageFont.truetype(ref_path, 34)
    bqg = ImageFont.truetype(tmp, 34)
    cands = [chr(c) for c in range(0x4E00, 0xA000)]
    M, keep = [], []
    for ch in cands:
        v = vec(ref, ch)
        if v is not None:
            M.append(v)
            keep.append(ch)
    M = np.array(M)
    mapping = {}
    for cp in cps:
        v = vec(bqg, chr(cp))
        if v is None:
            continue
        mapping[cp] = keep[int(np.argmax(M @ v))]
    os.remove(tmp)
    print(f"[font] 自動還原 {len(mapping)} 字 (像素比對，可能有少數簡繁誤差)", file=sys.stderr)
    return mapping


def decode(text, mapping):
    text = _html.unescape(text)
    return "".join(mapping.get(ord(c), c) for c in text)


def get_chapter(book_id, n, mapping):
    html = fetch(f"{BASE}/{book_id}/{n}.html", referer=f"{BASE}/{book_id}/dir")
    tm = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    title = decode(re.sub("<[^>]+>", "", tm.group(1)).strip(), mapping) if tm else f"第{n}章"
    m = re.search(r'<div class="content">(.*?)</div>\s*<div', html, re.S)
    if not m:
        m = re.search(r'<div class="content">(.*?)</div>', html, re.S)
    seg = m.group(1) if m else ""
    lines = []
    for p in re.findall(r"<p>(.*?)</p>", seg, re.S):
        txt = decode(re.sub("<[^>]+>", "", p), mapping).strip()
        if not txt or "溫馨提示" in txt or "網站即將改版" in txt:
            continue
        lines.append(txt)
    return title, lines


def main():
    ap = argparse.ArgumentParser(description="提莫書屋小說離線下載 (破字體反爬)")
    ap.add_argument("book_id", help="書本 ID，例: 0704594194")
    ap.add_argument("-o", "--output", help="輸出 txt 檔名 (預設用書名)")
    ap.add_argument("--delay", type=float, default=0.6, help="每章間隔秒數 (預設 0.6)")
    args = ap.parse_args()

    book_id = args.book_id.strip("/").split("/")[-1]
    print(f"[1/4] 取章節列表…", file=sys.stderr)
    book_title, author, nums = get_chapter_list(book_id)
    print(f"      《{book_title}》 作者:{author}  共 {len(nums)} 章", file=sys.stderr)

    print(f"[2/4] 取字體並建立解碼表…", file=sys.stderr)
    first_html = fetch(f"{BASE}/{book_id}/{nums[0]}.html", referer=f"{BASE}/{book_id}/dir")
    font_url = find_font_url(first_html, book_id)
    if not font_url:
        print("      找不到反爬字體，假設頁面未加密。", file=sys.stderr)
        mapping = {}
    else:
        print(f"      字體: {font_url}", file=sys.stderr)
        mapping = build_mapping(fetch(font_url, referer=f"{BASE}/{book_id}/", binary=True))

    out = args.output or f"{book_title}.txt"
    print(f"[3/4] 下載 {len(nums)} 章 -> {out}", file=sys.stderr)
    chapters = []
    for i, n in enumerate(nums, 1):
        title, lines = get_chapter(book_id, n, mapping)
        chapters.append((title, lines))
        if i % 10 == 0 or i == len(nums):
            print(f"      {i}/{len(nums)} {title}", file=sys.stderr)
        time.sleep(args.delay)

    print(f"[4/4] 合成 txt…", file=sys.stderr)
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"{book_title}\n作者：{author}\n共 {len(nums)} 章\n來源：提莫書屋 timotxt.com\n")
        f.write("=" * 40 + "\n\n")
        for title, lines in chapters:
            f.write(title + "\n\n")
            for line in lines:
                f.write("　　" + line + "\n")
            f.write("\n\n")
    total = sum(len(l) for _, ls in chapters for l in ls)
    print(f"完成：{out}  ({len(nums)} 章, 約 {total} 字)", file=sys.stderr)


if __name__ == "__main__":
    main()
