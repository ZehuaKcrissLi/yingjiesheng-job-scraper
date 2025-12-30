import asyncio
import json
import random
import time
import argparse
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

import requests
import os
import re


# ======================
# Config (argparse)
# ======================

def _sanitize_filename(s: str) -> str:
    # English-only comments: make output filenames filesystem-safe
    s = (s or "").strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:120] if len(s) > 120 else s


def get_args():
    parser = argparse.ArgumentParser(description="Yingjiesheng scraper (UI-only, robust pagination)")
    parser.add_argument("--keyword", type=str, default="人力资源", help="Search keyword")
    parser.add_argument("--area-name", type=str, default="山东", help="Area name: 山东/山东省/青岛/全国")
    parser.add_argument("--state-path", type=str, default="yjs_state.json", help="Playwright storage_state path")

    parser.add_argument("--out-jobs-jsonl", type=str, help="Jobs JSONL output (auto if omitted)")
    parser.add_argument("--out-pages-jsonl", type=str, help="Pages JSONL output (auto if omitted)")

    parser.add_argument("--max-page-actions", type=int, default=20, help="Max next-page actions (approx pages-1)")
    parser.add_argument("--min-delay-s", type=float, default=8.0, help="Min delay between actions (sec)")
    parser.add_argument("--max-delay-s", type=float, default=16.0, help="Max delay between actions (sec)")
    parser.add_argument("--click-timeout-ms", type=int, default=3000, help="Click timeout (ms)")
    parser.add_argument("--no-progress-limit", type=int, default=6, help="Stop after N no-progress actions")

    # Your confirmed Next button info
    parser.add_argument(
        "--next-btn-selector",
        type=str,
        default="#list > div.search-list > div.search-list-pagination > div > button.btn-next",
        help="CSS selector for Next button",
    )
    parser.add_argument(
        "--next-btn-xpath",
        type=str,
        default="/html/body/div/div/div/div/div[1]/div[2]/div[3]/div[1]/div[2]/div[1]/div[21]/div/button[2]",
        help="XPath for Next button (fallback)",
    )

    # City dict
    parser.add_argument("--city-dict-path", type=str, help="Local dd_city.json path (auto if omitted)")
    parser.add_argument(
        "--city-dict-url",
        type=str,
        default="https://js.51jobcdn.com/in/js/2023/dd/dd_city.json",
        help="dd_city.json URL",
    )

    args = parser.parse_args()

    safe_kw = _sanitize_filename(args.keyword)
    safe_area = _sanitize_filename(args.area_name)

    if args.out_jobs_jsonl is None:
        args.out_jobs_jsonl = f"yingjiesheng_jobs_{safe_kw}_{safe_area}.jsonl"
    if args.out_pages_jsonl is None:
        args.out_pages_jsonl = f"yingjiesheng_pages_{safe_kw}_{safe_area}.jsonl"
    if args.city_dict_path is None:
        args.city_dict_path = str(Path(__file__).with_name("dd_city.json"))

    return args


ARGS = get_args()

KEYWORD = ARGS.keyword
STATE_PATH = ARGS.state_path
OUT_JOBS_JSONL = ARGS.out_jobs_jsonl
OUT_PAGES_JSONL = ARGS.out_pages_jsonl

MAX_PAGE_ACTIONS = ARGS.max_page_actions
MIN_DELAY_S = ARGS.min_delay_s
MAX_DELAY_S = ARGS.max_delay_s
CLICK_TIMEOUT_MS = ARGS.click_timeout_ms
NO_PROGRESS_LIMIT = ARGS.no_progress_limit

NEXT_BTN_SELECTOR = ARGS.next_btn_selector
NEXT_BTN_XPATH = ARGS.next_btn_xpath

CITY_DICT_URL = ARGS.city_dict_url
CITY_DICT_PATH = Path(ARGS.city_dict_path)
AREA_NAME = ARGS.area_name


# ======================
# City dict: area-name -> jobarea code
# ======================

def ensure_city_dict(path: Path, allow_download: bool = True) -> None:
    # English-only comments: ensure dd_city.json exists locally
    if path.exists():
        return
    if not allow_download:
        raise FileNotFoundError(f"City dict not found: {path}")
    r = requests.get(CITY_DICT_URL, timeout=30)
    r.raise_for_status()
    path.write_text(r.text, encoding="utf-8")


def load_city_dict(path: Path) -> dict:
    # English-only comments: load dd_city.json
    return json.loads(path.read_text(encoding="utf-8"))


def collect_code_name_pairs(node, out: list[tuple[str, str]]) -> None:
    # English-only comments: recursively collect (code, value) pairs
    if isinstance(node, dict):
        if "code" in node and "value" in node:
            out.append((str(node["code"]), str(node["value"])))
        for v in node.values():
            collect_code_name_pairs(v, out)
    elif isinstance(node, list):
        for x in node:
            collect_code_name_pairs(x, out)


def build_name_to_codes(city_dict: dict) -> dict[str, list[str]]:
    # English-only comments: build mapping from name -> list of codes
    pairs: list[tuple[str, str]] = []
    collect_code_name_pairs(city_dict, pairs)
    m: dict[str, list[str]] = {}
    for code, name in pairs:
        m.setdefault(name, []).append(code)
    return m


def normalize_area_name(name: str) -> str:
    # English-only comments: normalize common user inputs
    name = (name or "").strip()
    if name in ("全国", "全中国", "中国", "不限", "全部"):
        return "全国"
    return name


def resolve_jobarea_code(area_name: str, name_to_codes: dict[str, list[str]]) -> str:
    """
    Resolve a user-facing area name to the Yingjiesheng "jobarea" code.

    Rules
    -----
    - "全国" (and common aliases) maps to empty string "" (no jobarea filter).
    - If `name_to_codes[area_name]` contains duplicate entries, they are de-duplicated;
      duplicate codes do NOT imply ambiguity.
    - Ambiguity is reported only when there are multiple distinct codes after de-duplication.
    - If `area_name` has no administrative suffix, the resolver will probe `area_name + "省"`.

    Parameters
    ----------
    area_name:
        User input area name, e.g. "山东", "深圳", "全国".
    name_to_codes:
        Mapping from area display name to a list of candidate codes (may include duplicates).

    Returns
    -------
    str
        A single resolved jobarea code. Empty string "" means nationwide.

    Raises
    ------
    ValueError
        If the name is not found, or truly ambiguous (multiple distinct codes).
    """
    # English-only comments: resolve user-facing area name to jobarea code
    area_name = normalize_area_name(area_name)
    if area_name == "全国":
        return ""

    if area_name in name_to_codes:
        codes = name_to_codes[area_name]
        unique_codes = sorted(set(codes))
        if len(unique_codes) == 1:
            return unique_codes[0]
        raise ValueError(f"Ambiguous area name '{area_name}', candidates: {unique_codes}")

    if not area_name.endswith(("省", "市", "自治区", "特别行政区")):
        probe = area_name + "省"
        if probe in name_to_codes:
            codes = name_to_codes[probe]
            unique_codes = sorted(set(codes))
            if len(unique_codes) == 1:
                return unique_codes[0]
            raise ValueError(f"Ambiguous area name '{probe}', candidates: {unique_codes}")

    hits = [(n, c) for n, cs in name_to_codes.items() for c in cs if area_name in n]
    hits = sorted(set(hits))[:50]
    raise ValueError(f"Area name '{area_name}' not found. Similar matches: {hits}")


ensure_city_dict(CITY_DICT_PATH, allow_download=True)
_city_dict = load_city_dict(CITY_DICT_PATH)
_name_to_codes = build_name_to_codes(_city_dict)
JOBAREA = resolve_jobarea_code(AREA_NAME, _name_to_codes)
print(f"[area] AREA_NAME={AREA_NAME} -> JOBAREA={JOBAREA!r}")


# ======================
# Utilities
# ======================

def _now_iso():
    # English-only comments: local timestamp for traceability
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _json_dumps(obj):
    # English-only comments: compact JSON for JSONL
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _safe_json_loads(s):
    # English-only comments: parse JSON string fields like "property"
    if not isinstance(s, str) or not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _list_to_str(v, sep="|"):
    # English-only comments: normalize list to a CSV-friendly string
    if isinstance(v, list):
        return sep.join([str(x) for x in v if x is not None])
    if v is None:
        return ""
    return str(v)


def _normalize_job(item: dict, keyword: str, pageno: str, page_request_id: str, source_url: str) -> dict:
    # English-only comments: flatten fields for easy CSV conversion
    prop = _safe_json_loads(item.get("property")) or {}
    labels = item.get("sesameLabelList") or []
    job_tags = item.get("jobTags") or []

    job_id = str(item.get("jobid") or item.get("jobId") or prop.get("jobId") or "").strip()
    company_id = str(item.get("coid") or item.get("companyId") or prop.get("companyId") or "").strip()

    return {
        "capturedAt": _now_iso(),
        "keyword": keyword,
        "pageno": str(pageno),
        "pageRequestId": page_request_id or "",
        "sourceUrl": source_url,

        "jobId": job_id,
        "companyId": company_id,

        "jobTitle": item.get("jobname") or item.get("jobTitle") or prop.get("jobTitle") or "",
        "companyName": item.get("coname") or item.get("companyName") or prop.get("companyName") or "",

        "jobArea": item.get("jobarea") or "",
        "salary": item.get("providesalary") or item.get("monthSalary") or prop.get("monthSalary") or "",
        "jobTerm": item.get("jobterm") or "",
        "jobTermCode": item.get("jobtermCode") or "",
        "workYear": item.get("workyear") or "",
        "degree": item.get("degree") or "",
        "coType": item.get("cotype") or "",
        "coSize": item.get("cosize") or "",
        "industry": item.get("indtype") or "",

        "issueDate": item.get("issuedate") or "",
        "lastUpdate": item.get("lastupdate") or "",

        "jobDetailUrl": item.get("jumpUrlHttp") or "",

        "jobTags": _list_to_str(job_tags, sep="|"),
        "jobTags_json": _json_dumps(job_tags) if isinstance(job_tags, list) else "[]",
        "sesameLabels": _list_to_str([x.get("labelName", "") for x in labels if isinstance(x, dict)], sep="|"),
        "sesameLabels_json": _json_dumps(labels) if isinstance(labels, list) else "[]",

        "lat": item.get("lat") or "",
        "lon": item.get("lon") or "",

        "funcType1": item.get("funcType1") or "",
        "funcType1Str": item.get("funcType1Str") or "",
        "isAd": item.get("isad") or item.get("isAd") or "",

        "hrName": item.get("hrName") or "",
        "hrPosition": item.get("hrPosition") or "",
        "hrActiveStatus": item.get("hrActiveStatus") or "",

        "property_json": _json_dumps(prop) if isinstance(prop, dict) else "{}",
    }


async def sleep_with_progress(seconds: float, prefix: str = ""):
    # English-only comments: show countdown
    start = time.time()
    while True:
        elapsed = time.time() - start
        left = max(0, int(round(seconds - elapsed)))
        msg = f"{prefix} sleeping {seconds:.1f}s (remaining {left:02d}s)"
        print("\r" + msg, end="", flush=True)
        if elapsed >= seconds:
            break
        await asyncio.sleep(1)
    print()


async def goto_with_retries(page, url: str, attempts: int = 3, timeout_ms: int = 90000):
    # English-only comments: use domcontentloaded to avoid SPA networkidle deadlocks
    last_err = None
    for i in range(1, attempts + 1):
        try:
            print(f"[nav] goto attempt {i}/{attempts}: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except PlaywrightTimeoutError as e:
            last_err = e
            print(f"[nav] timeout (attempt {i}/{attempts})")
        except Exception as e:
            last_err = e
            print(f"[nav] error (attempt {i}/{attempts}): {e}")
        await page.wait_for_timeout(min(30, 5 * i) * 1000)
    raise last_err


# ======================
# Login state
# ======================

async def ensure_login_state(p, state_path: str, force: bool = False):
    # English-only comments: manual login then save storage_state
    if (not force) and Path(state_path).exists():
        print(f"[state] Found existing state: {state_path}")
        return

    if force:
        print(f"[state] Force relogin enabled. Will overwrite: {state_path}")
    else:
        print("[state] No state found. Launching browser for manual login...")

    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()

    await page.goto("https://q.yingjiesheng.com/", wait_until="domcontentloaded", timeout=90000)

    print("Please complete login in the opened browser window.")
    print("After login is done, press Enter here to continue.")
    input()

    await context.storage_state(path=state_path)
    print(f"[state] Saved state to: {state_path}")
    await browser.close()


# ======================
# Critical: force blur + kill autocomplete popper (JS you asked for)
# ======================

ASYNC_CLOSE_OVERLAY_JS = """
() => {
  // Blur focused element (usually the search input)
  try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch(e) {}

  // Hide ElementUI autocomplete/select poppers that intercept clicks
  const selectors = [
    '.el-autocomplete-suggestion',
    '.el-autocomplete-suggestion.el-popper',
    '.el-autocomplete-suggestion.el-popper *',
    '.el-select-dropdown',
    '.el-select-dropdown.el-popper',
    '.el-select-dropdown.el-popper *',
    '.popper__arrow'
  ];

  for (const s of selectors) {
    document.querySelectorAll(s).forEach(el => {
      el.style.display = 'none';
      el.style.visibility = 'hidden';
      el.style.pointerEvents = 'none';
    });
  }
}
"""


async def force_defocus_and_hide_overlay(page):
    """
    Force-blur the currently focused element and hide known ElementUI popper overlays.

    This is used to reduce click interception from autocomplete/select dropdown poppers.

    Parameters
    ----------
    page:
        Playwright Page object.

    Returns
    -------
    None
    """
    # English-only comments: deterministic close of autocomplete overlay
    try:
        await page.evaluate(ASYNC_CLOSE_OVERLAY_JS)
    except Exception:
        pass
    try:
        await page.mouse.click(5, 5)
    except Exception:
        pass


async def escape_and_defocus(page) -> None:
    """
    Close transient UI poppers via Escape, then blur/hide known overlays.

    This is designed to handle common dropdown/autocomplete poppers that may be open
    after interacting with the search filters, and which can intercept pagination clicks.

    Parameters
    ----------
    page:
        Playwright Page object.

    Returns
    -------
    None
    """
    await page.keyboard.press("Escape")
    await force_defocus_and_hide_overlay(page)


async def center_locator_with_offset(page, locator, y_offset_px: int) -> None:
    """
    Scroll a locator into view (centered) and then scroll up by a fixed offset.

    Rationale: the page may have fixed/sticky footers or floating widgets near the
    bottom edge which can cover pagination controls. Centering and applying an
    upward offset keeps the target away from that cover zone.

    Parameters
    ----------
    page:
        Playwright Page object.
    locator:
        Playwright Locator for the element to bring into view.
    y_offset_px:
        Positive integer pixels to scroll upward after centering (e.g., 160).

    Returns
    -------
    None
    """
    await locator.evaluate(
        "(el) => el.scrollIntoView({block: 'center', inline: 'center'})"
    )
    await page.wait_for_timeout(80)
    await page.evaluate("(dy) => window.scrollBy(0, -dy)", int(y_offset_px))
    await page.wait_for_timeout(80)


async def _resolve_next_button_locator(page):
    """
    Resolve a reliable "Next page" button locator.

    The page may contain multiple matches for the configured selector/XPath (e.g., hidden
    templates, duplicated pagination controls). This resolver returns the first element
    that is:
    - visible
    - has a non-empty bounding box (width/height > 2 px)

    Parameters
    ----------
    page:
        Playwright Page object.

    Returns
    -------
    Locator | None
        A Locator pointing to the best candidate Next button element, or None if no
        suitable candidate is found.
    """
    candidates = []
    css = page.locator(NEXT_BTN_SELECTOR)
    if await css.count() > 0:
        candidates.append(css)
    xp = page.locator(f"xpath={NEXT_BTN_XPATH}")
    if await xp.count() > 0:
        candidates.append(xp)

    for group in candidates:
        n = await group.count()
        # English-only comments: guard against unexpected huge match sets
        n = min(n, 30)
        for i in range(n):
            el = group.nth(i)
            if not await el.is_visible():
                continue
            box = await el.bounding_box()
            if not box:
                continue
            if box.get("width", 0) <= 2 or box.get("height", 0) <= 2:
                continue
            return el

    return None


async def hit_test_next_button(page) -> dict:
    """
    Hit-test the Next pagination button using elementFromPoint at its visual center.

    This detects cases where a fixed/sticky overlay intercepts clicks at the button's
    center. The test is performed against the locator resolved from NEXT_BTN_SELECTOR
    (CSS), falling back to NEXT_BTN_XPATH when needed.

    Returns
    -------
    dict
        A dictionary with fixed keys:
        - found (bool): whether the Next button locator exists
        - disabled (bool): whether the Next button is disabled (attribute or class)
        - center (dict): {"x": float, "y": float} viewport coordinates of the button center
        - rect (dict): {"width": float, "height": float} button rect size in px
        - viewport (dict): {"width": float, "height": float} viewport size in px
        - hit_ok (bool): whether elementFromPoint(center) is inside the Next button DOM subtree
        - top_summary (str): summary of the top element at the center point (tag#id.class)
    """
    btn = await _resolve_next_button_locator(page)
    if btn is None:
        return {
            "found": False,
            "disabled": False,
            "center": {"x": 0.0, "y": 0.0},
            "rect": {"width": 0.0, "height": 0.0},
            "viewport": {"width": 0.0, "height": 0.0},
            "hit_ok": False,
            "top_summary": "",
        }

    res = await btn.evaluate(
        """
        (el) => {
          const disabled = el.hasAttribute('disabled') || el.classList.contains('is-disabled');
          const r = el.getBoundingClientRect();
          const cx = r.left + r.width / 2;
          const cy = r.top + r.height / 2;
          const tops = document.elementsFromPoint(cx, cy) || [];
          const top = tops.length ? tops[0] : null;
          const hit_ok = !!top && (top === el || el.contains(top));
          const tag = top && top.tagName ? top.tagName.toLowerCase() : '';
          const id = top && top.id ? top.id : '';
          const cls = top && top.className ? String(top.className).trim().replace(/\\s+/g, '.') : '';
          const top_summary = tag ? `${tag}${id ? '#' + id : ''}${cls ? '.' + cls : ''}` : '';
          return {
            disabled,
            center: { x: cx, y: cy },
            rect: { width: r.width, height: r.height },
            viewport: { width: window.innerWidth, height: window.innerHeight },
            hit_ok,
            top_summary
          };
        }
        """
    )
    return {
        "found": True,
        "disabled": bool(res.get("disabled")),
        "center": res.get("center") or {"x": 0.0, "y": 0.0},
        "rect": res.get("rect") or {"width": 0.0, "height": 0.0},
        "viewport": res.get("viewport") or {"width": 0.0, "height": 0.0},
        "hit_ok": bool(res.get("hit_ok")),
        "top_summary": str(res.get("top_summary") or ""),
    }


async def disable_click_interceptor_at_next_center(page) -> dict:
    """
    Disable pointer-events on a likely click-interceptor overlay at Next button center.

    This function should be called only when hit_test_next_button() reports hit_ok=False.
    It locates the element at the Next button center via elementFromPoint, then walks up
    the DOM to find a probable overlay container (fixed/sticky or high z-index) and sets
    style.pointerEvents='none' on that container.

    Returns
    -------
    dict
        - did_disable (bool): whether pointer-events was disabled on a candidate element
        - target_summary (str): summary of the modified element (tag#id.class)
        - reason (str): selection reason ('fixed_or_sticky', 'high_z', 'top_element', or '')
    """
    btn = await _resolve_next_button_locator(page)
    if btn is None:
        return {"did_disable": False, "target_summary": "", "reason": ""}

    res = await btn.evaluate(
        """
        (el) => {
          const r = el.getBoundingClientRect();
          const cx = r.left + r.width / 2;
          const cy = r.top + r.height / 2;
          const tops = document.elementsFromPoint(cx, cy) || [];
          const top = tops.length ? tops[0] : null;
          if (!top) return { did_disable: false, target_summary: '', reason: '' };
          if (top === el || el.contains(top)) return { did_disable: false, target_summary: '', reason: '' };
          const topTag = top.tagName ? top.tagName.toLowerCase() : '';
          if (topTag === 'html' || topTag === 'body') return { did_disable: false, target_summary: '', reason: '' };

          const summarize = (node) => {
            const tag = node && node.tagName ? node.tagName.toLowerCase() : '';
            const id = node && node.id ? node.id : '';
            const cls = node && node.className ? String(node.className).trim().replace(/\\s+/g, '.') : '';
            return tag ? `${tag}${id ? '#' + id : ''}${cls ? '.' + cls : ''}` : '';
          };

          let node = top;

          for (let i = 0; i < 10 && node; i++) {
            const tag = node.tagName ? node.tagName.toLowerCase() : '';
            if (tag === 'html' || tag === 'body') break;
            const style = window.getComputedStyle(node);
            const pos = style.position;
            const z = parseInt(style.zIndex || '0', 10);
            if (pos === 'fixed' || pos === 'sticky') {
              node.style.pointerEvents = 'none';
              return { did_disable: true, target_summary: summarize(node), reason: 'fixed_or_sticky' };
            }
            if (!Number.isNaN(z) && z >= 1000) {
              node.style.pointerEvents = 'none';
              return { did_disable: true, target_summary: summarize(node), reason: 'high_z' };
            }
            node = node.parentElement;
          }

          return { did_disable: false, target_summary: '', reason: '' };
        }
        """
    )
    return {
        "did_disable": bool(res.get("did_disable")),
        "target_summary": str(res.get("target_summary") or ""),
        "reason": str(res.get("reason") or ""),
    }


# ======================
# Click Next (XPath + mouse down/up)
# ======================

async def click_next_strict(page) -> bool:
    """
    Click the Next pagination button in a way that is robust to overlays.

    Strategy (A+B+C):
    - Escape + blur/hide known poppers
    - Scroll Next into viewport center with upward offset (avoid fixed footers)
    - Hit-test with elementFromPoint at center
    - If intercepted, disable pointer-events on a likely overlay container
    - Click via locator.click(force=True)

    Returns
    -------
    bool
        True if the Next button is found (and not disabled) and we dispatched a click via
        locator.click(force=True). False only when the button is not found or disabled.
    """
    await escape_and_defocus(page)

    btn = await _resolve_next_button_locator(page)
    if btn is None:
        return False

    disabled_attr = await btn.get_attribute("disabled")
    cls = (await btn.get_attribute("class")) or ""
    if disabled_attr is not None or "is-disabled" in cls:
        return False

    await center_locator_with_offset(page, btn, y_offset_px=160)

    ht = await hit_test_next_button(page)
    print(
        f"[ui] hit_test next: hit_ok={ht['hit_ok']} disabled={ht['disabled']} "
        f"top={ht['top_summary']} rect={ht['rect']} center={ht['center']} viewport={ht['viewport']}"
    )
    if not ht["hit_ok"]:
        dis = await disable_click_interceptor_at_next_center(page)
        print(
            f"[ui] interceptor: did_disable={dis['did_disable']} reason={dis['reason']} target={dis['target_summary']}"
        )

        await escape_and_defocus(page)
        await center_locator_with_offset(page, btn, y_offset_px=160)

        ht2 = await hit_test_next_button(page)
        print(
            f"[ui] hit_test retry: hit_ok={ht2['hit_ok']} disabled={ht2['disabled']} "
            f"top={ht2['top_summary']} rect={ht2['rect']} center={ht2['center']} viewport={ht2['viewport']}"
        )

    await btn.click(timeout=CLICK_TIMEOUT_MS, force=True)
    return True


# ======================
# Crawl
# ======================

async def crawl(p, state_path: str) -> bool:
    print("[mode] UI-only pagination (XPath + overlay-safe).")

    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context(storage_state=state_path)
    page = await context.new_page()

    seen_pages: set[str] = set()
    seen_jobs: set[str] = set()
    no_progress = 0

    first_page_ok = asyncio.Event()
    page_arrived: dict[str, asyncio.Event] = {}

    f_jobs = open(OUT_JOBS_JSONL, "a", encoding="utf-8")
    f_pages = open(OUT_PAGES_JSONL, "a", encoding="utf-8")

    async def log_bad_response(url: str, resp, hint: str = ""):
        try:
            status = resp.status
            headers = await resp.all_headers()
            ct = headers.get("content-type", "")
            body = await resp.text()
            with open("bad_responses.log", "a", encoding="utf-8") as fb:
                fb.write(_json_dumps({
                    "ts": _now_iso(),
                    "hint": hint,
                    "url": url,
                    "status": status,
                    "contentType": ct,
                    "bodyHead": body[:500],
                }) + "\n")
        except Exception:
            pass

    def process_search_json(data: dict, url: str, pageno: str) -> int:
        nonlocal no_progress

        if str(data.get("status")) != "1":
            msg = data.get("message", "") or ""
            no_progress += 1
            with open("bad_responses.log", "a", encoding="utf-8") as fb:
                fb.write(_json_dumps({
                    "ts": _now_iso(),
                    "hint": "status_not_1",
                    "url": url,
                    "statusField": str(data.get("status")),
                    "message": msg,
                }) + "\n")
            return 0

        rb = data.get("resultbody", {}) or {}
        page_request_id = rb.get("requestId", "") or ""
        joblist = (((rb.get("searchData") or {}).get("joblist") or {}).get("items")) or []
        total_count = (((rb.get("searchData") or {}).get("joblist") or {}).get("totalCount")) or ""

        page_meta = {
            "capturedAt": _now_iso(),
            "keyword": KEYWORD,
            "jobarea": JOBAREA,
            "pageno": str(pageno),
            "pageRequestId": page_request_id,
            "n_items": len(joblist),
            "totalCount": str(total_count),
            "url": url,
        }
        f_pages.write(_json_dumps(page_meta) + "\n")
        f_pages.flush()

        new_jobs = 0
        for item in joblist:
            job_id = str(item.get("jobid") or item.get("jobId") or "").strip()
            if not job_id:
                continue
            if job_id in seen_jobs:
                continue

            row = _normalize_job(item, KEYWORD, str(pageno), page_request_id, url)
            row["jobareaCode"] = JOBAREA
            f_jobs.write(_json_dumps(row) + "\n")
            f_jobs.flush()

            seen_jobs.add(job_id)
            new_jobs += 1

        seen_pages.add(str(pageno))
        ev = page_arrived.get(str(pageno))
        if ev is not None:
            ev.set()

        no_progress = 0 if new_jobs > 0 else (no_progress + 1)

        if str(pageno) == "1" and len(joblist) > 0:
            first_page_ok.set()

        print(f"SAVED page={pageno}, items={len(joblist)}, newJobs={new_jobs}, totalSeenJobs={len(seen_jobs)}")
        return new_jobs

    async def on_response(resp):
        nonlocal no_progress
        url = resp.url
        if "youngapi.yingjiesheng.com/open/noauth/job/search" not in url:
            return

        qs = parse_qs(urlparse(url).query)
        kw = qs.get("keyword", [""])[0]
        pageno = qs.get("pageno", [""])[0]
        jobarea = qs.get("jobarea", [""])[0]

        if kw not in (KEYWORD, ""):
            return
        if JOBAREA != "" and jobarea != "" and jobarea != JOBAREA:
            return
        if not pageno:
            return
        if pageno in seen_pages:
            return

        try:
            data = await resp.json()
        except Exception:
            no_progress += 1
            await log_bad_response(url, resp, hint="json_parse_failed")
            return

        process_search_json(data, url, pageno)

    page.on("response", on_response)

    # Navigate
    kw_enc = quote(KEYWORD, safe="")
    search_url = f"https://q.yingjiesheng.com/jobs/search?keyword={kw_enc}"
    if JOBAREA != "":
        search_url = f"https://q.yingjiesheng.com/jobs/search/?jobarea={JOBAREA}&keyword={kw_enc}"

    try:
        await goto_with_retries(page, search_url, attempts=3, timeout_ms=90000)
    except Exception:
        await context.storage_state(path=STATE_PATH)
        f_jobs.close()
        f_pages.close()
        await browser.close()
        return False

    # *** Required by you: before pagination loop, force one deterministic defocus + hide overlay ***
    await force_defocus_and_hide_overlay(page)

    # Wait for page 1
    try:
        await asyncio.wait_for(first_page_ok.wait(), timeout=25)
    except asyncio.TimeoutError:
        await context.storage_state(path=STATE_PATH)
        f_jobs.close()
        f_pages.close()
        await browser.close()
        return False

    # Pagination loop
    for step in range(1, MAX_PAGE_ACTIONS + 1):
        if no_progress >= NO_PROGRESS_LIMIT:
            print(f"STOP: no progress for {NO_PROGRESS_LIMIT} actions.")
            break

        current = max([int(x) for x in seen_pages if x.isdigit()], default=1)
        expected_next = current + 1

        print(f"[ui] step={step}/{MAX_PAGE_ACTIONS} current={current} -> expected_next={expected_next}")

        page_arrived[str(expected_next)] = asyncio.Event()
        ev = page_arrived[str(expected_next)]

        ok_click = await click_next_strict(page)
        if not ok_click:
            no_progress += 1
            print(f"[ui] click next failed, no_progress={no_progress}")
        else:
            print(f"[ui] clicked next, waiting page_arrived[{expected_next}] ...")
            try:
                await asyncio.wait_for(ev.wait(), timeout=15)
                print(f"[ui] advanced to page={expected_next}")
            except asyncio.TimeoutError:
                no_progress += 1
                print(f"[ui] timeout waiting page={expected_next}, no_progress={no_progress}")
                os.makedirs("debug", exist_ok=True)
                await page.screenshot(path=f"debug/timeout_p{expected_next}_{int(time.time())}.png", full_page=True)

        delay = random.uniform(MIN_DELAY_S, MAX_DELAY_S)
        if no_progress > 0:
            delay += min(60.0, 10.0 * no_progress)
        print(f"[wait] sleeping {delay:.1f}s (no_progress={no_progress})")
        await sleep_with_progress(delay, prefix="[wait]")

    if max([int(x) for x in seen_pages if x.isdigit()], default=1) <= 1:
        await context.storage_state(path=STATE_PATH)
        f_jobs.close()
        f_pages.close()
        await browser.close()
        return False

    await context.storage_state(path=STATE_PATH)
    f_jobs.close()
    f_pages.close()
    await browser.close()
    return True


# ======================
# main: relogin fallback
# ======================

async def main():
    async with async_playwright() as p:
        await ensure_login_state(p, STATE_PATH, force=False)

        ok = await crawl(p, STATE_PATH)
        if ok:
            return

        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        bak = f"{STATE_PATH}.bak.{ts}"
        try:
            if Path(STATE_PATH).exists():
                Path(STATE_PATH).rename(bak)
                print(f"[state] Backed up invalid state to: {bak}")
        except Exception:
            pass

        await ensure_login_state(p, STATE_PATH, force=True)

        ok2 = await crawl(p, STATE_PATH)
        if not ok2:
            print("[fatal] Still cannot capture/use page 1 after relogin. Check debug screenshots.")


if __name__ == "__main__":
    asyncio.run(main())