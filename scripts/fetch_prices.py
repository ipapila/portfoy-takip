#!/usr/bin/env python3
"""
Günlük fiyat çekme scripti.

  ALTIN (gram) — öncelik sırası:
    1. Selenium → İş Bankası isbank.com.tr/altin-fiyatlari (gerçek browser)
    2. Claude web araması → İş Bankası (fallback)
    3. TCMB XML → resmi kur (her zaman çekilir, karşılaştırma için)

  GBP/TRY — öncelik sırası:
    1. Selenium → İş Bankası isbank.com.tr/doviz-kurlari (gerçek browser)
    2. Claude web araması → İş Bankası (fallback)
    3. TCMB XML → resmi kur (birincil güvenilir kaynak)

  Her iki kaynak ayrı alan olarak kaydedilir.
  Çıktı: data/gold.json, data/gbp.json
"""

import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime

API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GOLD_FILE = "data/gold.json"
GBP_FILE  = "data/gbp.json"
TODAY     = date.today().isoformat()

GOLD_MIN, GOLD_MAX = 3000.0, 25000.0
GBP_MIN,  GBP_MAX  = 30.0,   200.0

# Selenium kullanılabilir mi?
SELENIUM_OK = False
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_OK = True
except ImportError:
    pass


# ── YARDIMCILAR ───────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def validate(val, vmin, vmax, label):
    if not (vmin <= val <= vmax):
        raise ValueError(f"{label} aralık dışı: {val} (beklenen {vmin}–{vmax})")
    return val

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def upsert(records, entry):
    for i, r in enumerate(records):
        if r["date"] == entry["date"]:
            records[i] = entry
            return "GÜNCELLENDİ"
    records.append(entry)
    records.sort(key=lambda x: x["date"])
    return "EKLENDİ"

def prev_val(records, field, n=2):
    vals = [r[field] for r in records[-n:] if field in r and r[field] is not None]
    return sum(vals) / len(vals) if vals else None

def existing_entry(records, today):
    for r in records:
        if r["date"] == today:
            return r
    return None

def parse_tr_number(s):
    """'6.531,13' veya '6531.13' → 6531.13"""
    s = s.strip().replace("\xa0", "").replace(" ", "")
    if re.match(r'^\d{1,3}(\.\d{3})+(,\d+)?$', s):
        # Türk formatı: 6.531,13
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)


# ── SELENIUM DRIVER ───────────────────────────────────────────────────────────

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    # GitHub Actions'da google-chrome-stable kurulu olur
    # Lokal geliştirmede PATH'teki Chrome kullanılır
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception:
        # chromedriver PATH'te yoksa webdriver-manager dene
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=opts
            )
        except Exception as e:
            raise RuntimeError(f"Chrome driver başlatılamadı: {e}")
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )
    return driver


# ── SELENIUM: İŞ BANKASI ALTIN ───────────────────────────────────────────────

def selenium_isbank_gold(driver):
    """
    İş Bankası altın fiyatları sayfasından Selenium ile gram altın alış/satış çek.
    Birden fazla selector stratejisi dener; hepsi başarısız olursa HTML dump'tan regex.
    """
    url = "https://www.isbank.com.tr/altin"
    log(f"  🌐 Selenium: {url}")
    driver.get(url)

    # Sayfanın yüklenmesini bekle — fiyat içeren bir sayısal element gelene dek
    wait = WebDriverWait(driver, 20)
    try:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass
    time.sleep(8)  # JS render + olası lazy-load için ek bekleme

    # 404 kontrolü — yanlış URL'e düşmüşsek hemen fallback URL'e geç
    try:
        body_text_check = driver.find_element(By.TAG_NAME, "body").text
        if "Aradığınız sayfaya ulaşılamıyor" in body_text_check or "404" in driver.title:
            log("  ⚠  /altin sayfası 404 döndü, /yatirim-fonu-ve-altin deneniyor")
            url = "https://www.isbank.com.tr/yatirim-fonu-ve-altin"
            driver.get(url)
            time.sleep(8)
    except Exception:
        pass

    # Strateji 1: data-* attribute ile (en güvenilir)
    selectors_alis = [
        "[data-currency='XAUTRY'][data-type='buying']",
        "[data-code='XAUTRY'] .buying",
        ".gold-price .buying-rate",
        ".altin-fiyat .alis",
        "tr:contains('Gram Altın') td:nth-child(2)",
    ]
    selectors_satis = [
        "[data-currency='XAUTRY'][data-type='selling']",
        "[data-code='XAUTRY'] .selling",
        ".gold-price .selling-rate",
        ".altin-fiyat .satis",
        "tr:contains('Gram Altın') td:nth-child(3)",
    ]

    # Strateji 2: sayfadaki tüm tabloları tara, "gram" + "altın" içereni bul
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "tr")
        for row in rows:
            text = row.text.lower()
            if "gram" in text and "altın" in text:
                cells = row.find_elements(By.CSS_SELECTOR, "td")
                nums = []
                for cell in cells:
                    try:
                        v = parse_tr_number(cell.text)
                        if GOLD_MIN <= v <= GOLD_MAX:
                            nums.append(v)
                    except Exception:
                        pass
                if len(nums) >= 2:
                    log(f"  ✅ Selenium Altın (tablo): alış={nums[0]} satış={nums[1]}")
                    return {"alis": nums[0], "satis": nums[1]}
    except Exception as e:
        log(f"  ⚠  Selenium tablo tarama: {e}")

    # Strateji 2b: sadece "altın" geçen herhangi bir satır/kart (tr, li, div) — daha geniş
    try:
        candidates = driver.find_elements(By.CSS_SELECTOR, "tr, li, div")
        for el in candidates:
            try:
                text = el.text.lower()
            except Exception:
                continue
            if "altın" in text and "gram" in text and len(text) < 300:
                nums = []
                for m in re.finditer(r'\b[5-9]\.\d{3}[,.]?\d{0,2}\b|\b1[0-9]\.\d{3}[,.]?\d{0,2}\b', el.text):
                    try:
                        v = parse_tr_number(m.group())
                        if GOLD_MIN <= v <= GOLD_MAX:
                            nums.append(v)
                    except Exception:
                        pass
                nums = sorted(set(nums))
                if len(nums) >= 2 and (nums[1] - nums[0]) / nums[0] < 0.20:
                    log(f"  ✅ Selenium Altın (kart/div): alış={nums[0]} satış={nums[1]}")
                    return {"alis": nums[0], "satis": nums[1]}
    except Exception as e:
        log(f"  ⚠  Selenium kart/div tarama: {e}")

    # Strateji 3: sayfanın tüm metninden fiyat büyüklüğünde sayıları topla
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        numbers = []
        for m in re.finditer(r'\b[5-9]\.\d{3}[,.]?\d{0,2}\b|\b[1][0-9]\.\d{3}[,.]?\d{0,2}\b', page_text):
            try:
                numbers.append(parse_tr_number(m.group()))
            except Exception:
                pass
        numbers = sorted(set(numbers))
        if len(numbers) >= 2:
            # En küçük = alış, bir büyüğü = satış (alış < satış)
            pairs = [(a, b) for a in numbers for b in numbers
                     if b > a and 0 < (b - a) / a < 0.20]
            if pairs:
                alis, satis = pairs[0]
                log(f"  ✅ Selenium Altın (metin): alış={alis} satış={satis}")
                return {"alis": alis, "satis": satis}
    except Exception as e:
        log(f"  ⚠  Selenium metin tarama: {e}")

    # Strateji 4: HTML kaynağından regex
    try:
        html = driver.page_source
        # JSON içinde gömülü veri ara
        matches = re.findall(
            r'"(?:buying|alis|buy)"[:\s]*"?([5-9]\d{3}(?:[,.]\d{1,2})?)"?'
            r'.*?"(?:selling|satis|sell)"[:\s]*"?([5-9]\d{3}(?:[,.]\d{1,2})?)"?',
            html, re.I | re.S
        )
        if matches:
            alis  = parse_tr_number(matches[0][0])
            satis = parse_tr_number(matches[0][1])
            validate(alis,  GOLD_MIN, GOLD_MAX, "Selenium Altın alış (HTML)")
            validate(satis, GOLD_MIN, GOLD_MAX, "Selenium Altın satış (HTML)")
            log(f"  ✅ Selenium Altın (HTML regex): alış={alis} satış={satis}")
            return {"alis": alis, "satis": satis}
    except Exception as e:
        log(f"  ⚠  Selenium HTML regex: {e}")

    # Hiçbiri tutmadıysa debug dump kaydet (Actions artifact için)
    try:
        os.makedirs("debug", exist_ok=True)
        with open("debug/altin_sayfa_metni.txt", "w", encoding="utf-8") as f:
            f.write(driver.find_element(By.TAG_NAME, "body").text)
        with open("debug/altin_sayfa_html.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log("  🐛 Debug dump kaydedildi: debug/altin_sayfa_metni.txt, debug/altin_sayfa_html.html")
    except Exception as e:
        log(f"  ⚠  Debug dump kaydedilemedi: {e}")

    raise RuntimeError("Selenium: İş Bankası altın fiyatı bulunamadı")


# ── SELENIUM: İŞ BANKASI GBP ─────────────────────────────────────────────────

def selenium_isbank_gbp(driver):
    """İş Bankası döviz kurları sayfasından Selenium ile GBP alış/satış çek."""
    url = "https://www.isbank.com.tr/doviz-kurlari"
    log(f"  🌐 Selenium: {url}")
    driver.get(url)
    time.sleep(5)

    # Strateji 1: tablo satırlarını tara, GBP içereni bul
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "tr")
        for row in rows:
            text = row.text
            if re.search(r'\bGBP\b|\bsterlin\b|\bİngiliz\b', text, re.I):
                cells = row.find_elements(By.CSS_SELECTOR, "td")
                nums = []
                for cell in cells:
                    try:
                        v = parse_tr_number(cell.text)
                        if GBP_MIN <= v <= GBP_MAX:
                            nums.append(v)
                    except Exception:
                        pass
                if len(nums) >= 2:
                    log(f"  ✅ Selenium GBP (tablo): alış={nums[0]} satış={nums[1]}")
                    return {"alis": nums[0], "satis": nums[1]}
    except Exception as e:
        log(f"  ⚠  Selenium GBP tablo: {e}")

    # Strateji 2: metin tarama
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        # GBP satırını bul
        for line in page_text.splitlines():
            if re.search(r'\bGBP\b|\bsterlin\b', line, re.I):
                nums = []
                for m in re.finditer(r'\b\d{2,3}[,.]\d{4}\b', line):
                    try:
                        v = parse_tr_number(m.group())
                        if GBP_MIN <= v <= GBP_MAX:
                            nums.append(v)
                    except Exception:
                        pass
                if len(nums) >= 2:
                    log(f"  ✅ Selenium GBP (metin): alış={nums[0]} satış={nums[1]}")
                    return {"alis": nums[0], "satis": nums[1]}
    except Exception as e:
        log(f"  ⚠  Selenium GBP metin: {e}")

    raise RuntimeError("Selenium: İş Bankası GBP kuru bulunamadı")


# ── TCMB XML ─────────────────────────────────────────────────────────────────

def fetch_tcmb():
    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read()
    root = ET.fromstring(content)
    result = {}

    for cur in root.findall("Currency"):
        code = cur.get("CurrencyCode", "")

        if code == "GBP":
            alis  = float(cur.findtext("ForexBuying")  or cur.findtext("BanknoteBuying")  or 0)
            satis = float(cur.findtext("ForexSelling") or cur.findtext("BanknoteSelling") or 0)
            validate(alis,  GBP_MIN, GBP_MAX, "TCMB GBP alış")
            validate(satis, GBP_MIN, GBP_MAX, "TCMB GBP satış")
            result["gbp"] = {"alis": round(alis, 4), "satis": round(satis, 4)}

        if code == "XAU":
            alis  = float(cur.findtext("ForexBuying")  or cur.findtext("BanknoteBuying")  or 0)
            satis = float(cur.findtext("ForexSelling") or cur.findtext("BanknoteSelling") or 0)
            if alis > 50000:  # ons ise gram'a çevir
                alis  = round(alis  / 31.1035, 4)
                satis = round(satis / 31.1035, 4)
            validate(alis,  GOLD_MIN, GOLD_MAX, "TCMB Altın alış")
            validate(satis, GOLD_MIN, GOLD_MAX, "TCMB Altın satış")
            result["gold"] = {"alis": round(alis, 2), "satis": round(satis, 2)}

    return result


# ── CLAUDE WEB ARAMASI (fallback) ────────────────────────────────────────────

def claude_search(prompt):
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read())
    return " ".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")

def extract_json(text):
    m = re.search(r'\{[^}]+\}', text)
    if not m:
        raise ValueError(f"JSON bulunamadı: {text[:300]}")
    return json.loads(m.group())

def fetch_isbank_gold_web():
    prompt = (
        f"Bugün {TODAY} tarihinde İş Bankası resmi sitesinde (isbank.com.tr) "
        "yayınlanan gram altın BAYİ ALIŞ ve BAYİ SATIŞ fiyatı nedir? "
        "Önce https://www.isbank.com.tr/altin-fiyatlari sayfasını kontrol et. "
        "Sonucu YALNIZCA şu JSON formatında döndür, başka metin ekleme: "
        '{"alis": <sayi>, "satis": <sayi>} '
        "Ondalık için nokta kullan."
    )
    d = extract_json(claude_search(prompt))
    return {
        "alis":  validate(float(d["alis"]),  GOLD_MIN, GOLD_MAX, "Claude Altın alış"),
        "satis": validate(float(d["satis"]), GOLD_MIN, GOLD_MAX, "Claude Altın satış"),
    }

def fetch_isbank_gbp_web():
    prompt = (
        f"Bugün {TODAY} tarihinde İş Bankası resmi sitesinde (isbank.com.tr) "
        "yayınlanan İngiliz Sterlini (GBP) BAYİ ALIŞ ve BAYİ SATIŞ kuru nedir? "
        "Önce https://www.isbank.com.tr/doviz-kurlari sayfasını kontrol et. "
        "Sonucu YALNIZCA şu JSON formatında döndür, başka metin ekleme: "
        '{"alis": <sayi>, "satis": <sayi>} '
        "Ondalık için nokta kullan."
    )
    d = extract_json(claude_search(prompt))
    return {
        "alis":  validate(float(d["alis"]),  GBP_MIN, GBP_MAX, "Claude GBP alış"),
        "satis": validate(float(d["satis"]), GBP_MIN, GBP_MAX, "Claude GBP satış"),
    }


# ── İŞ BANKASI VERİSİ AL (Selenium → Claude fallback) ───────────────────────

def get_isbank_gold(driver):
    if driver:
        try:
            result = selenium_isbank_gold(driver)
            validate(result["alis"],  GOLD_MIN, GOLD_MAX, "Selenium Altın alış")
            validate(result["satis"], GOLD_MIN, GOLD_MAX, "Selenium Altın satış")
            result["kaynak_detay"] = "Selenium"
            return result
        except Exception as e:
            log(f"  ⚠  Selenium Altın başarısız, Claude'a geçiliyor: {e}")
    try:
        result = fetch_isbank_gold_web()
        result["kaynak_detay"] = "Claude web search"
        log(f"  ✅ Claude Altın: alış={result['alis']} satış={result['satis']}")
        return result
    except Exception as e:
        log(f"  ❌ Claude Altın de başarısız: {e}")
        return None

def get_isbank_gbp(driver):
    if driver:
        try:
            result = selenium_isbank_gbp(driver)
            validate(result["alis"],  GBP_MIN, GBP_MAX, "Selenium GBP alış")
            validate(result["satis"], GBP_MIN, GBP_MAX, "Selenium GBP satış")
            result["kaynak_detay"] = "Selenium"
            return result
        except Exception as e:
            log(f"  ⚠  Selenium GBP başarısız, Claude'a geçiliyor: {e}")
    try:
        result = fetch_isbank_gbp_web()
        result["kaynak_detay"] = "Claude web search"
        log(f"  ✅ Claude GBP: alış={result['alis']} satış={result['satis']}")
        return result
    except Exception as e:
        log(f"  ❌ Claude GBP de başarısız: {e}")
        return None


# ── ALTIN KAYDET ──────────────────────────────────────────────────────────────

def run_gold(tcmb_data, isbank_gold):
    records = load_json(GOLD_FILE)
    ref  = prev_val(records, "satis")
    prev = existing_entry(records, TODAY)

    tcmb_gold = tcmb_data.get("gold")

    if tcmb_gold is None and isbank_gold is None:
        log("  ❌ ALTIN: Her iki kaynak başarısız, kayıt atlanıyor.")
        return

    if isbank_gold and ref and abs(isbank_gold["satis"] - ref) / ref > 0.08:
        log(f"  ⚠  ALTIN: satış {isbank_gold['satis']} öncekinden >%8 sapıyor (ref={ref:.2f}), TCMB ile çapraz kontrol")

    primary = isbank_gold or tcmb_gold
    entry = {
        "date":  TODAY,
        "alis":  primary["alis"],
        "satis": primary["satis"],
    }

    if isbank_gold:
        entry["isbank_alis"]  = isbank_gold["alis"]
        entry["isbank_satis"] = isbank_gold["satis"]
    elif prev:
        entry["isbank_alis"]  = prev.get("isbank_alis")
        entry["isbank_satis"] = prev.get("isbank_satis")
        if entry["isbank_alis"]:
            entry["alis"]  = entry["isbank_alis"]
            entry["satis"] = entry["isbank_satis"]
        log("  ℹ  ALTIN İşBankası: önceki değer korundu")
    else:
        entry["isbank_alis"]  = None
        entry["isbank_satis"] = None

    entry["tcmb_alis"]  = tcmb_gold["alis"]  if tcmb_gold else (prev.get("tcmb_alis")  if prev else None)
    entry["tcmb_satis"] = tcmb_gold["satis"] if tcmb_gold else (prev.get("tcmb_satis") if prev else None)

    if entry.get("isbank_satis") and entry.get("tcmb_satis"):
        entry["fark_satis"] = round(entry["isbank_satis"] - entry["tcmb_satis"], 2)
        entry["fark_alis"]  = round((entry["isbank_alis"] or 0) - (entry["tcmb_alis"] or 0), 2)
        log(f"  📊 ALTIN Fark: satış=+{entry['fark_satis']} alış=+{entry['fark_alis']}")

    detay = isbank_gold.get("kaynak_detay", "") if isbank_gold else ""
    if isbank_gold and tcmb_gold:
        entry["kaynak"] = f"İşBankası+TCMB ({detay})"
    elif isbank_gold:
        entry["kaynak"] = f"İşBankası ({detay}, TCMB yok)"
    else:
        entry["kaynak"] = "TCMB (İşBankası başarısız)"

    if prev and prev.get("kaynak", "").endswith("(manuel)"):
        entry["kaynak"] += " (manuel üzerine)"

    action = upsert(records, entry)
    save_json(GOLD_FILE, records)
    log(f"  💾 ALTIN {action}: alış={entry['alis']} satış={entry['satis']} [{entry['kaynak']}]")


# ── GBP KAYDET ────────────────────────────────────────────────────────────────

def run_gbp(tcmb_data, isbank_gbp):
    records = load_json(GBP_FILE)
    ref  = prev_val(records, "satis")
    prev = existing_entry(records, TODAY)

    tcmb_gbp = tcmb_data.get("gbp")

    if tcmb_gbp is None and isbank_gbp is None:
        log("  ❌ GBP: Her iki kaynak başarısız, kayıt atlanıyor.")
        return

    if isbank_gbp and ref and abs(isbank_gbp["satis"] - ref) / ref > 0.08:
        log(f"  ⚠  GBP: satış {isbank_gbp['satis']} öncekinden >%8 sapıyor")

    primary = isbank_gbp or tcmb_gbp
    entry = {
        "date":  TODAY,
        "alis":  primary["alis"],
        "satis": primary["satis"],
    }

    if isbank_gbp:
        entry["isbank_alis"]  = isbank_gbp["alis"]
        entry["isbank_satis"] = isbank_gbp["satis"]
    elif prev:
        entry["isbank_alis"]  = prev.get("isbank_alis")
        entry["isbank_satis"] = prev.get("isbank_satis")
        if entry["isbank_alis"]:
            entry["alis"]  = entry["isbank_alis"]
            entry["satis"] = entry["isbank_satis"]
        log("  ℹ  GBP İşBankası: önceki değer korundu")
    else:
        entry["isbank_alis"]  = None
        entry["isbank_satis"] = None

    entry["tcmb_alis"]  = tcmb_gbp["alis"]  if tcmb_gbp else (prev.get("tcmb_alis")  if prev else None)
    entry["tcmb_satis"] = tcmb_gbp["satis"] if tcmb_gbp else (prev.get("tcmb_satis") if prev else None)

    if entry.get("isbank_satis") and entry.get("tcmb_satis"):
        entry["fark_satis"] = round(entry["isbank_satis"] - entry["tcmb_satis"], 4)
        entry["fark_alis"]  = round((entry["isbank_alis"] or 0) - (entry["tcmb_alis"] or 0), 4)
        log(f"  📊 GBP Fark: satış=+{entry['fark_satis']:.4f} alış=+{entry['fark_alis']:.4f}")

    detay = isbank_gbp.get("kaynak_detay", "") if isbank_gbp else ""
    if isbank_gbp and tcmb_gbp:
        entry["kaynak"] = f"İşBankası+TCMB ({detay})"
    elif isbank_gbp:
        entry["kaynak"] = f"İşBankası ({detay}, TCMB yok)"
    else:
        entry["kaynak"] = "TCMB (İşBankası başarısız)"

    action = upsert(records, entry)
    save_json(GBP_FILE, records)
    log(f"  💾 GBP {action}: alış={entry['alis']} satış={entry['satis']} [{entry['kaynak']}]")


# ── ANA ───────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("HATA: ANTHROPIC_API_KEY eksik.", file=sys.stderr)
        sys.exit(1)

    log(f"=== Fiyat çekme başlıyor — {TODAY} ===")
    log(f"  Selenium: {'✅ kurulu' if SELENIUM_OK else '❌ kurulu değil (Claude fallback kullanılacak)'}")

    # TCMB — her zaman çek
    tcmb_data = {}
    try:
        tcmb_data = fetch_tcmb()
        log(f"  ✅ TCMB XML: GBP={'gbp' in tcmb_data}")
        if "gold" not in tcmb_data:
            log("  ℹ  TCMB XML'de altın (XAU) kodu yok — bu normal, TCMB günlük altın kuru yayınlamıyor olabilir. İşBankası/Selenium ana kaynak.")
    except Exception as e:
        log(f"  ❌ TCMB HATA: {e}")

    # Selenium driver — tek seferinde aç, her iki sayfa için kullan
    driver = None
    if SELENIUM_OK:
        try:
            driver = make_driver()
            log("  ✅ Chrome driver başlatıldı")
        except Exception as e:
            log(f"  ⚠  Chrome driver başlatılamadı: {e} — Claude fallback'e geçiliyor")

    try:
        isbank_gold = get_isbank_gold(driver)
        isbank_gbp  = get_isbank_gbp(driver)
    finally:
        if driver:
            driver.quit()
            log("  🔒 Chrome driver kapatıldı")

    run_gold(tcmb_data, isbank_gold)
    run_gbp(tcmb_data, isbank_gbp)

    log("=== Tamamlandı ===")

if __name__ == "__main__":
    main()
