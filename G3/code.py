"""
shc_selenium_scraper.py
Selenium + BeautifulSoup scraper for Sindh High Court Case Search (G3).
Produces one JSON file per major court with structure:
{
  "metadata": {...},
  "cases": [ {...}, {...} ]
}

Notes:
- TLS: this script configures Chrome to ignore certificate errors and also keeps any requests() calls using verify=False,
  matching the TLS approach you requested.
- Configure HEADLESS=False to debug visually.
"""

import os
import json
import time
import logging
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
import certifi
from dateutil import parser as dateparser

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, ElementClickInterceptedException
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- CONFIG ----------
BASE_URL = "https://cases.shc.gov.pk/"
OUTPUT_DIR = "sinhc_selenium_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADLESS = True               # set False to see browser while debugging
PAGE_LOAD_TIMEOUT = 30
IMPLICIT_WAIT = 3
REQUESTS_VERIFY = False       # keep requests verify disabled as you had

# Court name mapping
COURT_NAMES = {
    'khi': 'Karachi',
    'hyd': 'Hyderabad',
    'suk': 'Sukkur',
    'lar': 'Larkana',
    'search-result_CasesSearch_CIRCUITCODE__13': 'Mirpurkhas'
}
# ----------------------------

# create a requests session (for downloading any PDFs or fallback GETs)
req_session = requests.Session()
req_session.verify = REQUESTS_VERIFY
req_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
})


def start_driver(headless: bool = True):
    options = webdriver.ChromeOptions()
    if headless:
        # new headless mode for modern Chrome; if issues, set headless=False while debugging
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    # TLS / cert options to suppress handshake failures in Chromium used by Selenium
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-insecure-localhost")
    options.set_capability("acceptInsecureCerts", True)
    # reduce noisy logs
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    from selenium.webdriver.chrome.service import Service
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.implicitly_wait(IMPLICIT_WAIT)
    return driver


def parse_date_try(s):
    if not s:
        return "NA"
    s = str(s).strip()
    if not s or s.upper() in ("NA", "-", ""):
        return "NA"
    try:
        d = dateparser.parse(s, dayfirst=True)
        return d.strftime("%d-%b-%Y")
    except Exception:
        return s


def try_int_or(val):
    try:
        if val in (None, "", "NA", "-"):
            return "NA"
        return int(val)
    except Exception:
        return val


def find_major_courts_selenium(driver):
    """
    Find major court cards on landing page and return list of dicts:
      {"name": human_readable_name, "href": absolute_href}
    We DO NOT return WebElement objects to avoid stale-element issues.
    """
    elements = []
    court_divs = driver.find_elements(By.CSS_SELECTOR, "div.col-md-2.mb-3")
    for div in court_divs:
        try:
            anchor = div.find_element(By.CSS_SELECTOR, "a[href]")
            href = anchor.get_attribute("href") or ""
            # try to extract a human friendly name from the card body text
            card_body = None
            try:
                card_body = div.find_element(By.CSS_SELECTOR, "div.card-body")
                body_text = card_body.text.strip()
            except Exception:
                # fallback: read anchor text
                body_text = anchor.text.strip()

            # split into lines and pick the first meaningful line that's not the "Select Location..." button label
            lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
            name_candidate = None
            for ln in lines:
                low = ln.lower()
                if "select" in low and "location" in low:
                    continue
                name_candidate = ln
                break
            if not name_candidate:
                # fallback to last segment of href: e.g., '/khi' -> 'khi'
                try:
                    parsed = urljoin(BASE_URL, href)
                    name_candidate = parsed.rstrip("/").split("/")[-1] or parsed
                except Exception:
                    name_candidate = href

            elements.append({"name": name_candidate, "href": href})
        except Exception:
            # skip malformed divs
            continue

    # dedupe by href (preserve order)
    seen = set()
    uniques = []
    for e in elements:
        key = (e["href"], e["name"])
        if key not in seen and e["href"]:
            seen.add(key)
            uniques.append(e)
    return uniques


def extract_cases_from_html(html, major_name, subcourt_name=None):
    """
    Use BeautifulSoup to parse the case table from html.
    Returns list of case dicts and parallel list of detail link hrefs (may be None).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    cases = []
    detail_links = []
    if not table:
        return [], []

    # get header texts as keys
    header_row = table.find("thead").find("tr") if table.find("thead") else table.find("tr")
    header_cells = [th.get_text(" ", strip=True) for th in header_row.find_all("th")]
    rows = table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")[1:]
    for row in rows:
        tds = row.find_all("td")
        if not tds:
            continue
        entry = {}
        for i, td in enumerate(tds):
            key = header_cells[i] if i < len(header_cells) else f"col_{i}"
            # get all text, including spans
            val = td.get_text(" ", strip=True)
            entry[key] = val
        # add court and subcourt info
        entry["court"] = major_name
        entry["circuit_code"] = subcourt_name or major_name
        # find detail link in Actions column (usually last)
        a = tds[-1].find("a", href=True) if tds else None
        detail_links.append(a["href"] if a else None)
        cases.append(entry)
    return cases, detail_links


def extract_case_detail_from_html(html):
    """
    Parse a case detail page (HTML) and return a details dict containing:
    profile, last_hearing, parties, advocates, documents and summary/tagline
    """
    soup = BeautifulSoup(html, "html.parser")

    def pick_one(selectors):
        for s in selectors:
            el = soup.select_one(s)
            if el and el.get_text(strip=True):
                return el.get_text(" ", strip=True)
        return "NA"

    summary = pick_one(["div#Summary", ".summary", "p.summary", ".case-summary", "#divSummary"])
    tagline = pick_one([".tagline", "span.tagline", "p.tagline", "#Tagline"])
    # parties / advocates heuristics
    parties = []
    advocates = {"applicant": [], "respondent": []}

    # look for tables that mention Petitioner/Respondent
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True)
        if any(x in txt for x in ("Petitioner", "Respondent", "Appellant", "Respondent")):
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds: continue
                line = " - ".join([td.get_text(" ", strip=True) for td in tds if td.get_text(strip=True)])
                if line:
                    parties.append({"name": line})
            break

    # find small profile fields by label heuristics
    profile = {}
    labels = ["Case ID", "Institution Date", "Disposal Date", "Disposal Bench", "Nature Of Disposal"]
    for lab in labels:
        el = soup.find(string=lambda t: t and lab.lower() in t.lower())
        if el:
            try:
                parent = el.parent
                # try next sibling
                val = parent.find_next_sibling(text=True)
                if not val:
                    val = parent.get_text(" ", strip=True)
                profile[lab.lower().replace(" ", "_")] = val.strip() if val else "NA"
            except Exception:
                profile[lab.lower().replace(" ", "_")] = "NA"

    # document links
    documents = {"petition_memo": "NA", "judgement_order": "NA"}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        txt = a.get_text(" ", strip=True).lower()
        if href.lower().endswith(".pdf"):
            if "memo" in txt or "petition" in txt:
                documents["petition_memo"] = urljoin(BASE_URL, href)
            elif "judgement" in txt or "judgment" in txt or "order" in txt:
                documents["judgement_order"] = urljoin(BASE_URL, href)

    details_obj = {
        "profile": profile or {},
        "last_hearing": {
            "date": pick_one([".last-hearing .date", ".last-hearing", ".hearing-date", "li.hearing-date"]),
            "list": pick_one([".last-hearing .list", ".hearing-list"]),
            "stage": pick_one([".last-hearing .stage", ".hearing-stage"]),
            "bench": pick_one([".last-hearing .bench", ".hearing-bench"]),
            "remarks": pick_one([".last-hearing .remarks", ".remarks"])
        },
        "parties": parties,
        "advocates": advocates,
        "documents": documents
    }

    return {"summary": summary, "tagline": tagline if tagline != "NA" else "NA", "details": details_obj}


def scrape_major_court(driver, major):
    """
    major: {"name": str, "href": str}
    Returns metadata, all_cases
    This version NAVIGATES using major['href'] (no WebElement usage).
    """
    major_name = major.get("name")
    href = major.get("href")
    logging.info(f"Processing major court: {major_name}  (href={href})")

    # navigate to the court page using the saved href
    try:
        if href:
            # ensure absolute URL
            url = href if href.startswith("http") else urljoin(BASE_URL, href)
            driver.get(url)
        else:
            # fallback: go to base and try to click by visible text (rare)
            driver.get(BASE_URL)
            WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.col-md-2.mb-3")))
            el = driver.find_element(By.XPATH, f"//a[contains(., '{major_name.split()[-1]}')]")
            driver.execute_script("arguments[0].click();", el)
    except Exception as e:
        logging.warning(f"Could not navigate to major court page for {major_name}: {e}")
        return {
            "file_name": f"SindhCourt_{sanitize_filename(major_name)}.json",
            "created_on": datetime.utcnow().strftime("%Y-%m-%d"),
            "source": "Sindh High Court Case Search Portal",
            "url": href or BASE_URL,
            "description": f"Failed to open major court: {e}"
        }, []

    # let the page JS load a bit
    time.sleep(1.0)

    # Always click the search/submit button to load cases table
    try:
        search_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'].btn-success"))
        )
        driver.execute_script("arguments[0].click();", search_btn)
        time.sleep(1.0)
    except Exception as e:
        logging.warning(f"Could not find or click search button for {major_name}: {e}")

    # try find the subcourt select
    sub_select = None
    try:
        # common id on site is 'ddlCourt' but allow fallback
        try:
            sub_select = Select(driver.find_element(By.ID, "ddlCourt"))
        except Exception:
            # first <select> element fallback
            sel_el = driver.find_element(By.TAG_NAME, "select")
            sub_select = Select(sel_el)
    except Exception:
        logging.info("No subcourt select found; will attempt to parse page table directly.")
        sub_select = None

    all_cases = []
    sr_no = 1

    subcourt_texts = []
    if sub_select:
        opts = sub_select.options
        for o in opts:
            txt = o.text.strip()
            val = o.get_attribute("value")
            if txt and "Select" not in txt:
                subcourt_texts.append((txt, val))

    if not subcourt_texts:
        # try to parse cases on the page directly (no dropdown)
        html = driver.page_source
        cases, detail_links = extract_cases_from_html(html, major_name, None)
        # open detail links if present
        for i, c in enumerate(cases):
            # attach details
            detail_href = detail_links[i] if i < len(detail_links) else None
            if detail_href:
                # open detail in new tab to preserve list page
                full = detail_href if detail_href.startswith("http") else urljoin(BASE_URL, detail_href)
                driver.execute_script("window.open('');")
                driver.switch_to.window(driver.window_handles[-1])
                try:
                    driver.get(full)
                    time.sleep(0.8)
                    detail_data = extract_case_detail_from_html(driver.page_source)
                    c["tagline"] = detail_data.get("tagline", c.get("tagline", "NA"))
                    c["details"] = detail_data["details"]
                    c["details"].setdefault("summary", detail_data.get("summary", "NA"))
                except Exception as e:
                    logging.debug(f"Failed to fetch detail {full}: {e}")
                finally:
                    driver.close()
                    driver.switch_to.window(driver.window_handles[0])
            else:
                c["details"] = {"profile": {}, "last_hearing": {}, "parties": [], "advocates": {}, "documents": {}}
            c["sr_no"] = sr_no
            sr_no += 1
            all_cases.append(c)
    else:
        # iterate subcourts
        for sub_text, sub_val in subcourt_texts:
            logging.info(f" Subcourt: {sub_text} (value={sub_val})")
            # select the option
            try:
                # re-find the select element (select object may become stale)
                try:
                    sub_select = Select(driver.find_element(By.ID, "ddlCourt"))
                except Exception:
                    sel_el = driver.find_element(By.TAG_NAME, "select")
                    sub_select = Select(sel_el)
                sub_select.select_by_visible_text(sub_text)
                time.sleep(0.6)
            except Exception:
                # try selecting by value
                try:
                    sub_select.select_by_value(sub_val)
                    time.sleep(0.6)
                except Exception:
                    logging.warning(f"Couldn't select subcourt {sub_text}; skipping.")
                    # mark as JS-needed placeholder
                    all_cases.append({
                        "sr_no": sr_no,
                        "court": major_name,
                        "case_name": f"__SUBCOURT_NEEDS_JS__:{sub_text}",
                        "case_no": "NA",
                        "case_year": "NA",
                        "bench": "NA",
                        "circuit_code": sub_text,
                        "case_title": "NA",
                        "matter": "NA",
                        "status": "NA",
                        "last_hearing": "NA",
                        "next_date": "NA",
                        "disposal_date": "NA",
                        "tagline": "NA",
                        "details": {"note": "Could not select subcourt; needs manual/Selenium fallback."}
                    })
                    sr_no += 1
                    continue

            # find and click Search button
            search_btn = None
            try:
                # common id
                try:
                    search_btn = driver.find_element(By.ID, "btnSearch")
                except Exception:
                    # try button with Search text
                    search_btn = driver.find_element(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'search')]")
            except Exception:
                search_btn = None

            if search_btn:
                try:
                    driver.execute_script("arguments[0].click();", search_btn)
                except Exception:
                    try:
                        search_btn.click()
                    except Exception:
                        logging.debug("Search click failed; proceeding to scrape page.")
                # wait for table to appear (or small wait)
                try:
                    WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "table")))
                except TimeoutException:
                    logging.debug("Table not present after search; maybe results loaded by different means.")
            else:
                logging.debug("Search button not found; trying to parse page as-is.")

            # pagination loop
            while True:
                html = driver.page_source
                cases_page, detail_links = extract_cases_from_html(html, major_name, sub_text)
                if not cases_page:
                    logging.info(f"  No static cases found for subcourt {sub_text}; it likely requires more JS/AJAX handling.")
                    # add placeholder
                    all_cases.append({
                        "sr_no": sr_no,
                        "court": major_name,
                        "case_name": f"__SUBCOURT_NEEDS_JS__:{sub_text}",
                        "case_no": "NA",
                        "case_year": "NA",
                        "bench": "NA",
                        "circuit_code": sub_text,
                        "case_title": "NA",
                        "matter": "NA",
                        "status": "NA",
                        "last_hearing": "NA",
                        "next_date": "NA",
                        "disposal_date": "NA",
                        "tagline": "NA",
                        "details": {"note": "Subcourt results appear via JS/AJAX and were not available as static HTML."}
                    })
                    sr_no += 1
                else:
                    for i, c in enumerate(cases_page):
                        # fetch details if link exists
                        detail_href = detail_links[i] if i < len(detail_links) else None
                        if detail_href:
                            full = detail_href if detail_href.startswith("http") else urljoin(BASE_URL, detail_href)
                            # open in new tab
                            driver.execute_script("window.open('');")
                            driver.switch_to.window(driver.window_handles[-1])
                            try:
                                driver.get(full)
                                time.sleep(0.8)
                                detail_data = extract_case_detail_from_html(driver.page_source)
                                c["tagline"] = detail_data.get("tagline", c.get("tagline", "NA"))
                                c["details"] = detail_data["details"]
                                c["details"].setdefault("summary", detail_data.get("summary", "NA"))
                            except Exception as e:
                                logging.debug(f"Failed to fetch detail {full}: {e}")
                            finally:
                                driver.close()
                                driver.switch_to.window(driver.window_handles[0])
                        else:
                            c["details"] = {"profile": {}, "last_hearing": {}, "parties": [], "advocates": {}, "documents": {}}

                        c["sr_no"] = sr_no
                        sr_no += 1
                        all_cases.append(c)

                # try find 'Next' pagination
                next_found = False
                try:
                    # check for the pagination list
                    pagination = driver.find_element(By.CSS_SELECTOR, ".pagination")
                    li_elements = pagination.find_elements(By.TAG_NAME, "li")
                    
                    # look for next button in pagination
                    for li in li_elements:
                        try:
                            a = li.find_element(By.TAG_NAME, "a")
                            # check for data-page attribute
                            page_num = a.get_attribute("data-page")
                            if page_num and "next" in li.get_attribute("class").lower():
                                driver.execute_script("arguments[0].click();", a)
                                time.sleep(1.0)
                                next_found = True
                                break
                        except NoSuchElementException:
                            continue
                except Exception:
                    # fallback: try other pagination patterns
                    try:
                        next_btn = driver.find_element(By.CSS_SELECTOR, "a.next:not(.disabled), button.next:not(.disabled), li.next a:not(.disabled)")
                        if next_btn and not "disabled" in (next_btn.get_attribute("class") or "").lower():
                            driver.execute_script("arguments[0].click();", next_btn)
                            time.sleep(1.0)
                            next_found = True
                    except Exception:
                        next_found = False

                if not next_found:
                    break

    metadata = {
        "file_name": f"SindhCourt_{sanitize_filename(major_name)}.json",
        "created_on": datetime.utcnow().strftime("%Y-%m-%d"),
        "source": "Sindh High Court Case Search Portal",
        "url": BASE_URL,
        "description": f"Cases extracted for major court: {major_name}"
    }
    return metadata, all_cases


def sanitize_filename(s):
    safe = "".join([c if c.isalnum() or c in (" ", "_", "-") else "_" for c in s])
    return safe.replace(" ", "_")


def main():
    driver = start_driver(headless=HEADLESS)
    try:
        driver.get(BASE_URL)
    except Exception as e:
        logging.error(f"Failed to open base url: {e}")
        driver.quit()
        return

    time.sleep(1.0)  # let JS initialize
    majors = find_major_courts_selenium(driver)
    logging.info(f"Found major courts: {[m['name'] for m in majors]}")

    for major in majors:
        logging.info(f"Starting scrape for: {major['name']} (href={major['href']})")
        # Always navigate fresh via href (no stale element references)
        meta, cases = scrape_major_court(driver, major)
        # If scrape_major_court returned None as part of the placeholder, your function should proceed to fill meta/cases.
        if meta is None and cases is None:
            # if you used the placeholder return in the patch above, proceed to call the original scraping body:
            # In your file the rest of scrape_major_court follows navigation; ensure it executes and returns meta,cases
            logging.warning("scrape_major_court returned placeholder; ensure combined function continues after navigation.")
            # Fallback: skip writing
            continue

        out = {"metadata": meta, "cases": cases}
        outfname = os.path.join(OUTPUT_DIR, meta["file_name"])
        # incremental save for safety
        with open(outfname, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        logging.info(f"Wrote {len(cases)} case entries to {outfname}")

    driver.quit()
    logging.info("All done.")



if __name__ == "__main__":
    main()
