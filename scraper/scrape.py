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


SIZE_ONLY = re.compile(r"^[0-9.,x\s]*(l|ml|g|gm|kg|oz|ltr|lt|litre|liter|pcs|pc|pack)?$", re.I)


def walk_json(obj, out, depth=0, parent=None):
    """Find product-like dicts (have a name and a price) anywhere in a JSON blob."""
    if depth > 14:
        return
    if isinstance(obj, dict):
        name = first(obj, NAME_KEYS)
        if isinstance(name, dict):
            name = first(name, ["en", "english", "value"]) or next(iter(name.values()), None)
        own_name = name if isinstance(name, str) and len(name) > 3 and not SIZE_ONLY.match(name.strip()) else None
        if isinstance(name, str) and SIZE_ONLY.match(name.strip()) and parent:
            name = f"{parent} {name}"   # variant named only by size, e.g. "1.89L"
        elif isinstance(name, str) and SIZE_ONLY.match(name.strip()):
            name = None
        if isinstance(name, str) and 3 < len(name) < 250:
            brand = obj.get("brand") or obj.get("brandName") or obj.get("brand_name")
            if isinstance(brand, dict):
                brand = first(brand, ["name", "title", "en"])
            if isinstance(brand, str) and brand.strip() and brand.lower() not in name.lower():
                name = f"{brand.strip()} {name}"
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
            walk_json(v, out, depth + 1, own_name or parent)
    elif isinstance(obj, list):
        for v in obj[:400]:
            walk_json(v, out, depth + 1, parent)


DOM_JS = r"""
() => {
  const curRe = /(?:SAR|SR|ر\.?\s?س|﷼|⃁)\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:SAR|SR|ر\.?\s?س|﷼|⃁)/i;
  const bareRe = /(?:^|\s)([0-9]{1,4}[.,][0-9]{2})(?:\s|$)/;
  const out = [];
  const seen = new Set();
  const els = document.querySelectorAll('a, h1, h2, h3, h4, h5, p, span, div');
  for (const el of els) {
    if (el.children.length > 3) continue;
    const t = (el.innerText || '').trim();
    if (t.length < 6 || t.length > 160 || t.includes('\n') || /[0-9]{1,4}[.,][0-9]{2}/.test(t) ||
        !/[a-z؀-ۿ]{3}/i.test(t)) continue;
    let node = el, price = null, card = null;
    for (let i = 0; i < 6 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const txt = node.innerText || '';
      if (txt.length > 600) break;
      let m = txt.match(curRe);
      if (m) { price = m[1] || m[2]; card = node; break; }
      m = txt.match(bareRe);
      if (m && node.querySelector('img')) { price = m[1]; card = node; break; }
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


SEARCH_BOX = ("input[type='search'], input[placeholder*='earch' i], input[aria-label*='earch' i], "
              "input[name*='search' i], input[name='q'], input[placeholder*='بحث'], input[id*='search' i], "
              "input[class*='search' i], header input[type='text'], nav input[type='text']")


async def visit(browser, store, item, mode, target):
    q_text = item.get("search") or item["name"]
    candidates, json_urls = [], []
    ctx = await browser.new_context(
        locale="en-US", timezone_id="Asia/Riyadh",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        viewport={"width": 1366, "height": 900})
    page = await ctx.new_page()
    pending = []

    async def on_response(resp):
        try:
            if "json" not in resp.headers.get("content-type", ""):
                return
            body = await resp.json()
            json_urls.append(resp.url[:160])
            walk_json(body, candidates)
        except Exception:
            pass

    page.on("response", lambda r: pending.append(asyncio.ensure_future(on_response(r))))
    info = {"mode": mode, "url": target}
    try:
        resp = await page.goto(target, wait_until="domcontentloaded", timeout=45000)
        info["status"] = resp.status if resp else None
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        if mode == "box":
            candidates.clear()   # ignore home-page products, keep only search results
            await page.wait_for_timeout(4000)
            info["inputs"] = await page.eval_on_selector_all(
                "input", "els => els.slice(0,15).map(e => [e.type, e.name, e.id, e.placeholder, e.className.toString().slice(0,40), e.offsetParent !== null])")
            box = None
            for el in await page.query_selector_all(SEARCH_BOX):
                if await el.is_visible():
                    box = el
                    break
            if box is None:
                # some sites hide the field behind a search icon
                for icon in await page.query_selector_all("[aria-label*='earch' i], [class*='search' i], [data-testid*='search' i]"):
                    if await icon.is_visible():
                        await icon.click()
                        await page.wait_for_timeout(1200)
                        for el in await page.query_selector_all(SEARCH_BOX):
                            if await el.is_visible():
                                box = el
                                break
                        break
            if box is None:
                info["error"] = "no search box found"
            else:
                await box.click()
                await box.fill("")
                await box.type(q_text, delay=60)
                await page.wait_for_timeout(1500)
                await box.press("Enter")
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
        await page.wait_for_timeout(4000)
        for _ in range(3):
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(1500)
        info["final_url"] = page.url
        info["title"] = await page.title()
        scripts = await page.eval_on_selector_all(
            "script[type='application/ld+json'], script#__NEXT_DATA__, script[type='application/json']",
            "els => els.map(e => e.textContent).slice(0, 40)")
        for sc in scripts:
            try:
                walk_json(json.loads(sc), candidates)
            except Exception:
                pass
        for c in await page.evaluate(DOM_JS):
            p = to_price(c["price"])
            if p:
                candidates.append({"name": c["name"], "price": p, "old_price": None,
                                   "in_stock": False if c["out_of_stock"] else None,
                                   "url": c["url"], "source": "page"})
        info["page_start"] = re.sub(r"\s+", " ", (await page.inner_text("body"))[:300])
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await ctx.close()
    info["json_urls"] = json_urls[:20]
    return candidates, info


def blocked(info):
    t = (info.get("title") or "") + " " + (info.get("page_start") or "")
    return bool(info.get("error") and "search box" not in info["error"]) or \
        (info.get("status") or 0) in (401, 403, 429) or \
        bool(re.search(r"blocked|attention required|access denied|captcha|cloudflare", t, re.I))


async def check_store(browser, store, item, debug):
    q = quote_plus(item.get("search") or item["name"])
    attempts = [("url", u.replace("{q}", q)) for u in store.get("urls", [])]
    if store.get("home"):
        attempts.append(("box", store["home"]))
    tried, reachable, info = [], False, {}
    first_group = item["require"][0] if item["require"] else []
    for mode, target in attempts:
        candidates, info = await visit(browser, store, item, mode, target)
        hits = [c for c in candidates if matches(c["name"], item)]
        relevant = [c for c in candidates if any(norm(a) in norm(c["name"]) for a in first_group)]
        info.update({"products_seen": len(candidates), "relevant": len(relevant), "matches": len(hits),
                     "sample_names": sorted({c["name"] for c in (relevant or candidates)})[:15]})
        tried.append(info)
        if not blocked(info):
            reachable = True
        if hits or relevant:
            debug[store["id"]] = tried
            return hits, info
    debug[store["id"]] = tried
    return ([] if reachable else None), info


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
        browser = await pw.chromium.launch(args=["--disable-blink-features=AutomationControlled", "--disable-http2"])
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
