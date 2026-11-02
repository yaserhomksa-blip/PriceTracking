"""Daily supermarket price check.

For every item in items.json and every store in stores.json, opens the store's
search page in a headless browser, collects product data from the page's own
data feeds (JSON responses, embedded JSON, JSON-LD) and, as a fallback, from
the visible page text. Products are matched against the item's "require"
groups and the cheapest match per store is written to data/prices.json.
"""
import asyncio
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

NAME_KEYS = ["name", "title", "productName", "product_name", "displayName",
             "display_name", "name_en", "nameEn", "en_name", "description_short"]
PRICE_KEYS = ["discountedPrice", "discounted_price", "salePrice", "sale_price",
              "specialPrice", "special_price", "offerPrice", "offer_price",
              "finalPrice", "final_price", "sellingPrice", "selling_price",
              "price", "currentPrice", "current_price", "mrp", "value"]
OLD_PRICE_KEYS = ["originalPrice", "original_price", "regularPrice",
                  "regular_price", "oldPrice", "old_price", "mrp", "listPrice",
                  "list_price", "wasPrice", "was_price"]
STOCK_TRUE = ["in_stock", "inStock", "isInStock", "is_in_stock", "available",
              "isAvailable", "is_available", "inventoryAvailable"]
URL_KEYS = ["url", "link", "productUrl", "product_url", "slug", "permalink", "href"]

PRICE_RE = re.compile(
    r"(?:SAR|SR|ر\.?\s?س|﷼)\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:SAR|SR|ر\.?\s?س|﷼)",
    re.I)


def norm(s):
    s = str(s or "").lower()
    s = s.replace("ـ", "")
    s = re.sub(r"[^\w\s.\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def matches(name, item):
    n = norm(name)
    squashed = n.replace(" ", "")
    for group in item["require"]:
        ok = False
        for alt in group:
            a = norm(alt)
            if re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", n) or \
               (len(a) >= 3 and a.replace(" ", "") in squashed):
                ok = True
                break
        if not ok:
            return False
    return True


def to_price(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dict):
        for k in ("value", "amount", "centAmount", "formattedValue", "price"):
            if k in v:
                p = to_price(v[k])
                if p is not None:
                    if k == "centAmount":
                        p = p / 100
                    return p
        return None
    if isinstance(v, (int, float)):
        p = float(v)
    else:
        m = re.search(r"[0-9]+(?:[.,][0-9]+)?", str(v))
        if not m:
            return None
        p = float(m.group(0).replace(",", "."))
    if p > 5000 and p == int(p):  # probably halalas / cents
        p = p / 100
    return p if 0.25 <= p <= 5000 else None


def first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def walk_json(obj, out, depth=0):
    """Find product-like dicts (have a name and a price) anywhere in a JSON blob."""
    if depth > 14:
        return
    if isinstance(obj, dict):
        name = first(obj, NAME_KEYS)
        if isinstance(name, dict):
            name = first(name, ["en", "english", "value"]) or next(iter(name.values()), None)
        if isinstance(name, str) and 3 < len(name) < 250:
            price = None
            for k in PRICE_KEYS:
                if k in obj:
                    price = to_price(obj[k])
                    if price is not None:
                        break
            if price is None:
                # price often sits one level down (e.g. offers / prices / storeSpecificData)
                for sub in ("offers", "prices", "priceInfo", "pricing", "price_info",
                            "storeSpecificData", "store_specific_data", "variants"):
                    s = obj.get(sub)
                    if isinstance(s, list) and s:
                        s = s[0]
                    if isinstance(s, dict):
                        for k in PRICE_KEYS:
                            if k in s:
                                price = to_price(s[k])
                                if price is not None:
                                    break
                    if price is not None:
                        break
            if price is not None:
                old = None
                for k in OLD_PRICE_KEYS:
                    if k in obj:
                        old = to_price(obj[k])
                        if old:
                            break
                stock = None
                for k in STOCK_TRUE:
                    if k in obj and isinstance(obj[k], bool):
                        stock = obj[k]
                        break
                if stock is None:
                    st = str(obj.get("stock_status") or obj.get("stockStatus") or
                             obj.get("availability") or "").lower()
                    if st:
                        stock = not any(w in st for w in ("out", "unavailable", "no_stock"))
                url = first(obj, URL_KEYS)
                out.append({"name": name.strip(), "price": price,
                            "old_price": old if old and old > price else None,
                            "in_stock": stock,
                            "url": url if isinstance(url, str) else None,
                            "source": "data"})
        for v in obj.values():
            walk_json(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:400]:
            walk_json(v, out, depth + 1)


DOM_JS = r"""
() => {
  const priceRe = /(?:SAR|SR|ر\.?\s?س|﷼)\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:SAR|SR|ر\.?\s?س|﷼)/i;
  const out = [];
  const seen = new Set();
  const els = document.querySelectorAll('a, h1, h2, h3, h4, h5, p, span, div');
  for (const el of els) {
    if (el.children.length > 3) continue;
    const t = (el.innerText || '').trim();
    if (t.length < 6 || t.length > 160 || priceRe.test(t)) continue;
    let node = el, price = null, card = null;
    for (let i = 0; i < 6 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const txt = node.innerText || '';
      if (txt.length > 600) break;
      const m = txt.match(priceRe);
      if (m) { price = m[1] || m[2]; card = node; break; }
    }
    if (!price) continue;
    const key = t + '|' + price;
    if (seen.has(key)) continue;
    seen.add(key);
    const a = el.closest('a') || (card && card.querySelector('a'));
    const ctext = (card.innerText || '').toLowerCase();
    out.push({name: t, price: price, url: a ? a.href : null,
              out_of_stock: /out of stock|sold out|غير متوفر|نفذ/.test(ctext)});
  }
  return out.slice(0, 300);
}
"""


async def check_store(browser, store, item, debug):
    q = quote_plus(item.get("search") or item["name"])
    tried = []
    for tmpl in store["urls"]:
        url = tmpl.replace("{q}", q)
        candidates = []
        json_count = 0
        ctx = await browser.new_context(
            locale="en-US", timezone_id="Asia/Riyadh",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        pending = []

        async def on_response(resp):
            nonlocal json_count
            try:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = await resp.json()
                json_count += 1
                walk_json(body, candidates)
            except Exception:
                pass

        page.on("response", lambda r: pending.append(asyncio.ensure_future(on_response(r))))
        info = {"url": url}
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            info["status"] = resp.status if resp else None
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            for _ in range(3):
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(1500)
            info["final_url"] = page.url
            info["title"] = await page.title()
            # embedded JSON (Next.js / Nuxt / JSON-LD)
            scripts = await page.eval_on_selector_all(
                "script[type='application/ld+json'], script#__NEXT_DATA__, script[type='application/json']",
                "els => els.map(e => e.textContent).slice(0, 40)")
            for s in scripts:
                try:
                    walk_json(json.loads(s), candidates)
                except Exception:
                    pass
            for c in await page.evaluate(DOM_JS):
                p = to_price(c["price"])
                if p:
                    candidates.append({"name": c["name"], "price": p, "old_price": None,
                                       "in_stock": False if c["out_of_stock"] else None,
                                       "url": c["url"], "source": "page"})
            text = (await page.inner_text("body"))[:400]
            info["page_start"] = re.sub(r"\s+", " ", text)
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await ctx.close()

        hits = [c for c in candidates if matches(c["name"], item)]
        info.update({"json_responses": json_count, "products_seen": len(candidates),
                     "matches": len(hits),
                     "sample_names": sorted({c["name"] for c in candidates})[:15]})
        tried.append(info)
        if candidates or hits:
            debug[store["id"]] = tried
            return hits, info
    debug[store["id"]] = tried
    return None, tried[-1] if tried else {}


def absolutize(u, base):
    if not u:
        return None
    if u.startswith("http"):
        return u
    from urllib.parse import urljoin
    return urljoin(base, u)


async def main():
    items = json.loads((ROOT / "items.json").read_text(encoding="utf-8"))
    stores = json.loads((ROOT / "stores.json").read_text(encoding="utf-8"))
    only = sys.argv[1] if len(sys.argv) > 1 else None
    DATA.mkdir(exist_ok=True)
    prices_path = DATA / "prices.json"
    old = json.loads(prices_path.read_text(encoding="utf-8")) if prices_path.exists() else {}
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
    result = {"updated": now.isoformat(timespec="minutes"), "items": {}}
    debug = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        for item in items:
            if only and item["name"] != only:
                prev = old.get("items", {}).get(item["name"])
                if prev:
                    result["items"][item["name"]] = prev
                continue
            row = {}
            item_debug = {}
            for store in stores:
                if store.get("app_only"):
                    row[store["id"]] = {"status": "app_only"}
                    continue
                hits, info = await check_store(browser, store, item, item_debug)
                if hits is None:
                    row[store["id"]] = {"status": "unreachable"}
                elif not hits:
                    row[store["id"]] = {"status": "not_found"}
                else:
                    hits.sort(key=lambda h: (h["in_stock"] is False, h["price"]))
                    best = hits[0]
                    row[store["id"]] = {
                        "status": "out_of_stock" if best["in_stock"] is False else "found",
                        "price": round(best["price"], 2),
                        "old_price": round(best["old_price"], 2) if best["old_price"] else None,
                        "product": best["name"],
                        "url": absolutize(best["url"], info.get("final_url") or info.get("url")),
                    }
            result["items"][item["name"]] = row
            debug[item["name"]] = item_debug
        await browser.close()

    prices_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "debug.json").write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")

    # history: one entry per item/store/day
    hist_path = DATA / "history.json"
    hist = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
    day = now.date().isoformat()
    for name, row in result["items"].items():
        for sid, r in row.items():
            if r.get("price") is not None:
                h = hist.setdefault(name, {}).setdefault(sid, {})
                h[day] = r["price"]
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
