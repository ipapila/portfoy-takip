#!/usr/bin/env python3
"""
Günlük fiyat çekme scripti.

  ALTIN (gram):
    1. TCMB XML → resmi gram altın alış/satış (birincil, güvenilir)
    2. Claude web araması → İş Bankası gram altın bayi alış/satış (ikincil)
    Her ikisi ayrı alan olarak kaydedilir; İşBankası varsa o, yoksa TCMB ana değer.

  GBP/TRY:
    1. TCMB XML → resmi GBP kuru (birincil)
    2. Claude web araması → İş Bankası GBP bayi kuru (ikincil)

Çıktı: data/gold.json, data/gbp.json
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime

API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GOLD_FILE = "data/gold.json"
GBP_FILE  = "data/gbp.json"
TODAY     = date.today().isoformat()

GOLD_MIN, GOLD_MAX = 3000.0, 25000.0
GBP_MIN,  GBP_MAX  = 30.0,  200.0


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


# ── TCMB XML ─────────────────────────────────────────────────────────────────

def fetch_tcmb():
    """TCMB'den GBP ve gram altın verisi çek."""
    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read()
    root = ET.fromstring(content)

    result = {}

    for cur in root.findall("Currency"):
        code = cur.get("CurrencyCode", "")

        # GBP
        if code == "GBP":
            alis  = float(cur.findtext("ForexBuying")  or cur.findtext("BanknoteBuying")  or 0)
            satis = float(cur.findtext("ForexSelling") or cur.findtext("BanknoteSelling") or 0)
            validate(alis,  GBP_MIN,  GBP_MAX,  "TCMB GBP alış")
            validate(satis, GBP_MIN,  GBP_MAX,  "TCMB GBP satış")
            result["gbp"] = {"alis": round(alis, 4), "satis": round(satis, 4)}

        # XAU — TCMB gram altın olarak yayınlar (birim: 1 gram = TRY)
        if code == "XAU":
            alis  = float(cur.findtext("ForexBuying")  or cur.findtext("BanknoteBuying")  or 0)
            satis = float(cur.findtext("ForexSelling") or cur.findtext("BanknoteSelling") or 0)
            # TCMB ons bazında yayınlıyorsa gram'a çevir (1 ons = 31.1035 gram)
            # Değer 3000+ ise gram, 100.000+ ise ons demektir
            if alis > 50000:
                alis  = round(alis  / 31.1035, 4)
                satis = round(satis / 31.1035, 4)
            validate(alis,  GOLD_MIN, GOLD_MAX, "TCMB Altın alış")
            validate(satis, GOLD_MIN, GOLD_MAX, "TCMB Altın satış")
            result["gold"] = {"alis": round(alis, 2), "satis": round(satis, 2)}

    # XAU yoksa USD üzerinden hesapla (fallback)
    if "gold" not in result:
        usd_alis = usd_satis = None
        for cur in root.findall("Currency"):
            if cur.get("CurrencyCode") == "USD":
                usd_alis  = float(cur.findtext("ForexBuying")  or 0)
                usd_satis = float(cur.findtext("ForexSelling") or 0)
                break
        if usd_alis:
            result["_usd"] = {"alis": usd_alis, "satis": usd_satis}
            log("  ℹ  TCMB: XAU bulunamadı, USD kaydedildi (gram altın için kullanılabilir)")

    return result


# ── CLAUDE WEB ARAMASI ───────────────────────────────────────────────────────

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


# ── İŞ BANKASI WEB (CLAUDE SEARCH) ──────────────────────────────────────────

def fetch_isbank_gold_web():
    """Claude web search ile İş Bankası resmi gram altın bayi kuru."""
    prompt = (
        f"Bugün {TODAY} tarihinde İş Bankası resmi sitesinde (isbank.com.tr) "
        "yayınlanan gram altın BAYİ ALIŞ ve BAYİ SATIŞ fiyatı nedir? "
        "Önce https://www.isbank.com.tr/altin-fiyatlari sayfasını kontrol et. "
        "Bu sayfada bulamazsan https://www.isbank.com.tr/doviz-kurlari adresine bak. "
        "Sonucu yalnızca aşağıdaki JSON formatında döndür, başka hiçbir metin ekleme: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık ayraç olarak nokta kullan. Virgül kullanma."
    )
    raw = claude_search(prompt)
    d = extract_json(raw)
    return {
        "alis":  validate(float(d["alis"]),  GOLD_MIN, GOLD_MAX, "İşBankası Altın alış"),
        "satis": validate(float(d["satis"]), GOLD_MIN, GOLD_MAX, "İşBankası Altın satış"),
    }

def fetch_isbank_gbp_web():
    """Claude web search ile İş Bankası GBP bayi kuru."""
    prompt = (
        f"Bugün {TODAY} tarihinde İş Bankası resmi sitesinde (isbank.com.tr) "
        "yayınlanan İngiliz Sterlini (GBP) BAYİ ALIŞ ve BAYİ SATIŞ kuru nedir? "
        "Önce https://www.isbank.com.tr/doviz-kurlari sayfasını kontrol et. "
        "Sonucu yalnızca aşağıdaki JSON formatında döndür, başka hiçbir metin ekleme: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık ayraç olarak nokta kullan. Virgül kullanma."
    )
    raw = claude_search(prompt)
    d = extract_json(raw)
    return {
        "alis":  validate(float(d["alis"]),  GBP_MIN, GBP_MAX, "İşBankası GBP alış"),
        "satis": validate(float(d["satis"]), GBP_MIN, GBP_MAX, "İşBankası GBP satış"),
    }


# ── ALTIN ─────────────────────────────────────────────────────────────────────

def run_gold(tcmb_data):
    records = load_json(GOLD_FILE)
    ref  = prev_val(records, "satis")
    prev = existing_entry(records, TODAY)

    tcmb_gold  = tcmb_data.get("gold")   # {"alis": x, "satis": x} veya None
    isbank_gold = None

    # İş Bankası web (ikincil)
    try:
        isbank_gold = fetch_isbank_gold_web()
        if ref and abs(isbank_gold["satis"] - ref) / ref > 0.08:
            log(f"  ⚠  ALTIN İşBankası: satış {isbank_gold['satis']} öncekinden >%8 sapıyor (ref={ref:.2f})")
        log(f"  ✅ ALTIN İşBankası: alış={isbank_gold['alis']} satış={isbank_gold['satis']}")
    except Exception as e:
        log(f"  ⚠  ALTIN İşBankası başarısız: {e}")

    if tcmb_gold is None and isbank_gold is None:
        log("  ❌ ALTIN: Her iki kaynak başarısız, kayıt atlanıyor.")
        return

    # Ana alis/satis: İşBankası varsa o, yoksa TCMB
    primary = isbank_gold or tcmb_gold
    entry = {
        "date":  TODAY,
        "alis":  primary["alis"],
        "satis": primary["satis"],
    }

    # İşBankası alanları
    if isbank_gold:
        entry["isbank_alis"]  = isbank_gold["alis"]
        entry["isbank_satis"] = isbank_gold["satis"]
    elif prev:
        entry["isbank_alis"]  = prev.get("isbank_alis")
        entry["isbank_satis"] = prev.get("isbank_satis")
        if entry["isbank_alis"]:
            entry["alis"]  = entry["isbank_alis"]
            entry["satis"] = entry["isbank_satis"]
        log(f"  ℹ  ALTIN İşBankası: önceki değer korundu")
    else:
        entry["isbank_alis"]  = None
        entry["isbank_satis"] = None

    # TCMB alanları
    entry["tcmb_alis"]  = tcmb_gold["alis"]  if tcmb_gold else (prev.get("tcmb_alis")  if prev else None)
    entry["tcmb_satis"] = tcmb_gold["satis"] if tcmb_gold else (prev.get("tcmb_satis") if prev else None)

    # Fark
    if entry.get("isbank_satis") and entry.get("tcmb_satis"):
        entry["fark_satis"] = round(entry["isbank_satis"] - entry["tcmb_satis"], 2)
        entry["fark_alis"]  = round((entry["isbank_alis"] or 0) - (entry["tcmb_alis"] or 0), 2)
        log(f"  📊 ALTIN Fark (İşBankası−TCMB): satış=+{entry['fark_satis']} alış=+{entry['fark_alis']}")

    # Kaynak belirle
    if isbank_gold and tcmb_gold:
        entry["kaynak"] = "İşBankası+TCMB"
    elif isbank_gold:
        entry["kaynak"] = "İşBankası (TCMB yok)"
    else:
        entry["kaynak"] = "TCMB (İşBankası başarısız)"

    # Manuel revizyon koruma
    if prev and prev.get("kaynak", "").endswith("(manuel)"):
        entry["kaynak"] += " (manuel üzerine)"

    action = upsert(records, entry)
    save_json(GOLD_FILE, records)
    log(f"  ✅ ALTIN {action}: alış={entry['alis']} satış={entry['satis']} [{entry['kaynak']}]")


# ── GBP ──────────────────────────────────────────────────────────────────────

def run_gbp(tcmb_data):
    records = load_json(GBP_FILE)
    ref  = prev_val(records, "satis")
    prev = existing_entry(records, TODAY)

    tcmb_gbp   = tcmb_data.get("gbp")
    isbank_gbp = None

    # İş Bankası web (ikincil)
    try:
        isbank_gbp = fetch_isbank_gbp_web()
        if ref and abs(isbank_gbp["satis"] - ref) / ref > 0.08:
            log(f"  ⚠  GBP İşBankası: satış {isbank_gbp['satis']} öncekinden >%8 sapıyor")
        log(f"  ✅ GBP İşBankası: alış={isbank_gbp['alis']} satış={isbank_gbp['satis']}")
    except Exception as e:
        log(f"  ⚠  GBP İşBankası başarısız: {e}")

    if tcmb_gbp is None and isbank_gbp is None:
        log("  ❌ GBP: Her iki kaynak başarısız, kayıt atlanıyor.")
        return

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
        entry["kaynak"] = "TCMB (İşBankası önceki korundu)"
        log(f"  ℹ  GBP İşBankası: önceki değer korundu")
    else:
        entry["isbank_alis"]  = None
        entry["isbank_satis"] = None

    entry["tcmb_alis"]  = tcmb_gbp["alis"]  if tcmb_gbp else (prev.get("tcmb_alis")  if prev else None)
    entry["tcmb_satis"] = tcmb_gbp["satis"] if tcmb_gbp else (prev.get("tcmb_satis") if prev else None)

    if entry.get("isbank_satis") and entry.get("tcmb_satis"):
        entry["fark_satis"] = round(entry["isbank_satis"] - entry["tcmb_satis"], 4)
        entry["fark_alis"]  = round((entry["isbank_alis"] or 0) - (entry["tcmb_alis"] or 0), 4)
        log(f"  📊 GBP Fark (İşBankası−TCMB): satış=+{entry['fark_satis']:.4f} alış=+{entry['fark_alis']:.4f}")

    if isbank_gbp and tcmb_gbp:
        entry["kaynak"] = "İşBankası+TCMB"
    elif isbank_gbp:
        entry["kaynak"] = "İşBankası (TCMB yok)"
    else:
        entry["kaynak"] = "TCMB (İşBankası başarısız)"

    action = upsert(records, entry)
    save_json(GBP_FILE, records)
    log(f"  💾 GBP {action}")


# ── ANA ───────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("HATA: ANTHROPIC_API_KEY eksik.", file=sys.stderr)
        sys.exit(1)

    log(f"=== Fiyat çekme başlıyor — {TODAY} ===")

    # TCMB'yi bir kez çek, her ikisi için kullan
    tcmb_data = {}
    try:
        tcmb_data = fetch_tcmb()
        gold_ok = "tcmb_alis" in str(tcmb_data.get("gold", ""))
        log(f"  ✅ TCMB XML: GBP={'gbp' in tcmb_data} Altın={'gold' in tcmb_data}")
    except Exception as e:
        log(f"  ❌ TCMB HATA: {e}")

    run_gold(tcmb_data)
    run_gbp(tcmb_data)

    log("=== Tamamlandı ===")

if __name__ == "__main__":
    main()
