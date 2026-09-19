#!/usr/bin/env python3
"""DDP計算機の「時期で変わる割増」を公式サイトから取り直して rates.json を更新する。

取得元:
  FedEx 燃料割増(週次)     https://www.fedex.com/ja-jp/shipping/surcharges.html
  FedEx 混雑時割増(PDF)    https://www.fedex.com/ja-jp/shipping/surcharges/demand-surcharge.html
  DHL   燃料割増(週次)     https://mydhl.express.dhl/jp/ja/ship/surcharges.html
  DHL   繁忙期追加金        https://mydhl.express.dhl/jp/ja/ship/surcharges/demand-surcharge.html
  OC    Economy燃油        https://www.orangeconnex.jp/news

fedex.com はヘッドレスを弾くので、VPSでは `xvfb-run` + 実Chrome(channel=chrome) で動かす。
取れなかった項目は前回値を残し、errors に記録する（計算機側で警告表示）。
使い方: python update_rates.py <rates.jsonのパス>
"""
import base64
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def iso(y, m, d):
    return dt.date(int(y), int(m), int(d)).isoformat()


def merge(old, new):
    """[[開始日, 値], ...] を開始日で合成（新しい取得値を優先）して日付順に。"""
    d = {k: v for k, v in (old or [])}
    d.update({k: v for k, v in new})
    return [[k, d[k]] for k in sorted(d)]


def check(rows, lo, hi, name):
    if not rows:
        raise ValueError(f"{name}: 0件")
    for k, v in rows:
        if not (lo <= v <= hi):
            raise ValueError(f"{name}: 値が異常 {k}={v}")
    return rows


def pdf_text(pg, url):
    # fedex.com のページ内から fetch する（外からの直接取得はボット判定で弾かれる）
    b64 = pg.evaluate("""async (u) => {
      const r = await fetch(u, {credentials: 'include'});
      const a = new Uint8Array(await r.arrayBuffer()); let s = '';
      for (let i = 0; i < a.length; i += 8192) s += String.fromCharCode.apply(null, a.subarray(i, i + 8192));
      return btoa(s);
    }""", url)
    body = base64.b64decode(b64)
    if not body.startswith(b"%PDF"):
        raise ValueError(f"PDF取得失敗 {url}")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(body)
        f.flush()
        return subprocess.run(["pdftotext", "-layout", f.name, "-"],
                              capture_output=True, text=True, check=True).stdout


def fedex_fuel(pg):
    pg.goto("https://www.fedex.com/ja-jp/shipping/surcharges.html", timeout=90000)
    pg.wait_for_selector("table tr", timeout=60000)
    pg.wait_for_timeout(3000)
    rows = pg.eval_on_selector_all("table tr", "rs=>rs.map(r=>r.textContent.replace(/\\s+/g,' ').trim())")
    out = []
    for r in rows:  # 例: "21 9月, 2026 - 27 9月, 2026 $4.418 51.75%"
        m = re.match(r"(\d{1,2}) (\d{1,2})月, (\d{4}) - .*?([\d.]+)%$", r)
        if m:
            out.append([iso(m[3], m[2], m[1]), float(m[4])])
    return check(out, 0, 100, "FedEx燃料")


def fedex_peak(pg):
    pg.goto("https://www.fedex.com/ja-jp/shipping/surcharges/demand-surcharge.html", timeout=90000)
    pg.wait_for_timeout(4000)
    links = pg.eval_on_selector_all("a", "as=>as.map(a=>a.href)")
    ds = sorted({h for h in links if re.search(r"/fedex-ds-\d{4}-.*ja-jp\.pdf$", h)})
    nss = sorted({h for h in links if re.search(r"/fedex-nssds-\d{4}-.*ja-jp\.pdf$", h)})
    if not ds:
        raise ValueError("FedEx混雑時割増: PDFリンクが見つからない")
    peak, ends = [], []
    for url in ds:
        t = pdf_text(pg, url)
        m = re.search(r"(\d{4}) 年 (\d{1,2}) 月 (\d{1,2}) 日から", t)
        if not m:
            raise ValueError(f"開始日が読めない {url}")
        start = iso(m[1], m[2], m[3])
        e = re.search(r"から\s*(\d{4}) 年 (\d{1,2}) 月 (\d{1,2}) 日まで", t)
        line = next((l for l in t.splitlines() if "米国およびプエルトリコ" in l), None)
        if line is None:
            raise ValueError(f"米国の行がない {url}")
        after = line.split("米国およびプエルトリコ", 1)[1]
        p = re.search(r"プライオリティ\^?\s*(\d+)", after)          # 区分あり: プライオリティ(FICP含む)
        val = int(p[1]) if p else int(re.search(r"(\d+)", after)[1])  # 区分なし: 最初の数字=輸出
        peak.append([start, val])
        if e:
            ends.append((dt.date.fromisoformat(iso(e[1], e[2], e[3])) + dt.timedelta(days=1)).isoformat())
    # 終了日の翌日に次の期間が無ければ 0（公表なし）とする
    starts = {s for s, _ in peak}
    for nd in ends:
        if nd not in starts and not any(s > nd for s in starts):
            peak.append([nd, 0])
    nss_rows = []
    for url in nss:
        t = pdf_text(pg, url)
        m = re.search(r"(\d{4}) 年 (\d{1,2}) 月 (\d{1,2}) 日(?:より|から)", t)
        a = re.search(r"([\d,]+) 円", t)
        if m and a:
            nss_rows.append([iso(m[1], m[2], m[3]), int(a[1].replace(",", ""))])
    return check(sorted(peak), 0, 3000, "FedEx混雑時割増"), nss_rows


def dhl_fuel(pg):
    pg.goto("https://mydhl.express.dhl/jp/ja/ship/surcharges.html", timeout=90000)
    pg.wait_for_timeout(5000)
    rows = pg.eval_on_selector_all(
        "table tr", "rs=>rs.map(r=>[...r.children].map(c=>c.textContent.replace(/\\s+/g,' ').trim()).join(' | '))")
    out = []
    for r in rows:  # 例: "2026 9月 28-10月 4 | 46.25%"
        m = re.match(r"(\d{4}) (\d{1,2})月 (\d{1,2})-.*\|\s*([\d.]+)%$", r)
        if m:
            out.append([iso(m[1], m[2], m[3]), float(m[4])])
    return check(out, 0, 100, "DHL燃料")


def dhl_peak(pg):
    pg.goto("https://mydhl.express.dhl/jp/ja/ship/surcharges/demand-surcharge.html", timeout=90000)
    pg.wait_for_timeout(5000)
    body = pg.inner_text("body")
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日から、\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", body)
    if not m:
        raise ValueError("DHL繁忙期: 期間が読めない")
    start = iso(m[1], m[2], m[3])
    end_next = (dt.date.fromisoformat(iso(m[4], m[5], m[6])) + dt.timedelta(days=1)).isoformat()
    # Time Definite International の表: 行=発送側ゾーン、列=受取側ゾーン。日本は「アジア各国」
    val = pg.evaluate("""() => {
      for (const t of document.querySelectorAll('table')) {
        const rows = [...t.querySelectorAll('tr')].map(r => [...r.children].map(c => c.textContent.replace(/\\s+/g,' ').trim()));
        const head = rows.find(r => r.includes('アメリカ地域'));
        const row = rows.find(r => r[0] === 'アジア各国');
        if (head && row && row.length === head.length) return parseInt(row[head.indexOf('アメリカ地域')], 10);
      }
      return null;
    }""")
    if val is None or not (0 <= val <= 3000):
        raise ValueError(f"DHL繁忙期: 金額が読めない {val}")
    return [[start, val], [end_next, 0]]


def oc_fuel(pg):
    pg.goto("https://www.orangeconnex.jp/news", timeout=90000)
    pg.wait_for_timeout(4000)
    items = pg.get_by_text(re.compile("Economy.*燃油サーチャージ")).all()
    if not items:
        raise ValueError("OC燃油: お知らせが見つからない")
    items[0].click()
    pg.wait_for_timeout(4000)
    t = pg.inner_text("body")
    d = re.search(r"発効日時[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日", t)
    r = re.search(r"改定後の料率[：:]\s*([\d.]+)\s*%", t)
    if not (d and r):
        raise ValueError("OC燃油: 料率/発効日が読めない")
    return check([[iso(d[1], d[2], d[3]), float(r[1])]], 0, 60, "OC燃油")


def main(path):
    try:
        data = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    errors = {}
    got = {}
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=False,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="ja-JP", user_agent=UA)
        pg = ctx.new_page()
        for key, fn in [("fedex_fuel", lambda: fedex_fuel(pg)),
                        ("fedex_peak", lambda: fedex_peak(pg)),
                        ("dhl_fuel", lambda: dhl_fuel(pg)),
                        ("dhl_peak", lambda: dhl_peak(pg)),
                        ("eco_fuel", lambda: oc_fuel(pg))]:
            try:
                got[key] = fn()
            except Exception as e:  # 1項目の失敗で全体を止めない
                errors[key] = str(e)[:300]
        b.close()

    fx = data.setdefault("fedex", {})
    dh = data.setdefault("dhl", {})
    ec = data.setdefault("eco", {})
    if "fedex_fuel" in got:
        fx["fuel"] = merge(fx.get("fuel"), got["fedex_fuel"])
    if "fedex_peak" in got:
        peak, nss = got["fedex_peak"]
        fx["peak"] = merge(fx.get("peak"), peak)
        if nss:
            fx["nss"] = merge(fx.get("nss"), nss)
    if "dhl_fuel" in got:
        dh["fuel"] = merge(dh.get("fuel"), got["dhl_fuel"])
    if "dhl_peak" in got:
        dh["peak"] = merge(dh.get("peak"), got["dhl_peak"])
    if "eco_fuel" in got:
        ec["fuel"] = merge(ec.get("fuel"), got["eco_fuel"])

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    data["checked"] = now
    data["errors"] = errors
    if len(errors) < 5:
        data["updated"] = now
    text = json.dumps(data, ensure_ascii=False, indent=1)
    text = re.sub(r'\[\s+("[\d-]+"),\s+([\d.]+)\s+\]', r'[\1,\2]', text)   # [日付,値] は1行に
    open(path, "w", encoding="utf-8").write(text + "\n")
    print(json.dumps({"errors": errors, "got": list(got)}, ensure_ascii=False))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
