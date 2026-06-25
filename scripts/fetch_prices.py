#!/usr/bin/env python3
"""
Günlük fiyat çekme scripti.
  - Altın : Claude web araması → İş Bankası gram altın bayi alış/satış
  - GBP   : TCMB XML (birincil, güvenilir) + Claude web araması (İşBankası bayi kuru)
Her ikisi de data/gold.json ve data/gbp.json'a eklenir/güncellenir.
Manuel revizyon için GitHub PR akışı kullanılır.
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

def prev_satis(records):
    vals = [r["satis"] for r in records[-2:] if "satis" in r]
    return sum(vals) / len(vals) if vals else None

def existing_entry(records, today):
    """JSON'da bugünün mevcut kaydını döndür (varsa)."""
    for r in records:
        if r["date"] == today:
            return r
    return None


# ── TCMB XML ─────────────────────────────────────────────────────────────────

def fetch_tcmb_gbp():
    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        tree = ET.parse(resp)
    root = tree.getroot()
    for cur in root.findall("Currency"):
        if cur.get("CurrencyCode") == "GBP":
            alis  = float(cur.findtext("ForexBuying")  or cur.findtext("BanknoteBuying")  or 0)
            satis = float(cur.findtext("ForexSelling") or cur.findtext("BanknoteSelling") or 0)
            validate(alis,  GBP_MIN, GBP_MAX, "TCMB GBP alış")
            validate(satis, GBP_MIN, GBP_MAX, "TCMB GBP satış")
            return {"alis": round(alis, 4), "satis": round(satis, 4)}
    raise ValueError("TCMB XML'de GBP bulunamadı")


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


# ── ALTIN ─────────────────────────────────────────────────────────────────────

def fetch_isbank_gold():
    prompt = (
        f"Bugün {TODAY} tarihinde İş Bankası gram altın BAYİ ALIŞ ve BAYİ SATIŞ fiyatı nedir? "
        "canlidoviz.com/altin-fiyatlari/is-bankasi/gram-altin sayfasını kontrol et. "
        "Yalnızca şu JSON'u döndür, başka metin yok: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık için nokta kullan."
    )
    raw = claude_search(prompt)
    d = extract_json(raw)
    return {
        "date":   d.get("tarih", TODAY),
        "alis":   validate(float(d["alis"]),  GOLD_MIN, GOLD_MAX, "Altın alış"),
        "satis":  validate(float(d["satis"]), GOLD_MIN, GOLD_MAX, "Altın satış"),
        "kaynak": "İşBankası (web)"
    }

def run_gold():
    records = load_json(GOLD_FILE)
    ref = prev_satis(records)
    prev = existing_entry(records, TODAY)

    try:
        entry = fetch_isbank_gold()
        if ref and abs(entry["satis"] - ref) / ref > 0.08:
            log(f"  ⚠  ALTIN: satış {entry['satis']} öncekinden >%8 sapıyor (ref={ref:.2f}) — lütfen kontrol et")
        # Manuel revizyon varsa kaynak bilgisini koru, değerleri güncelle
        if prev and prev.get("kaynak", "").endswith("(manuel)"):
            entry["kaynak"] = entry["kaynak"] + " (manuel üzerine)"
        action = upsert(records, entry)
        save_json(GOLD_FILE, records)
        log(f"  ✅ ALTIN {action}: alış={entry['alis']} satış={entry['satis']} [{entry['kaynak']}]")
    except Exception as e:
        log(f"  ❌ ALTIN HATA: {e}")
        if prev:
            log(f"  ℹ  ALTIN: mevcut kayıt korunuyor ({prev['date']})")


# ── GBP ──────────────────────────────────────────────────────────────────────

def fetch_isbank_gbp():
    prompt = (
        f"Bugün {TODAY} tarihinde İş Bankası İngiliz Sterlini (GBP) "
        "BAYİ ALIŞ ve BAYİ SATIŞ kuru nedir? "
        "canlidoviz.com/doviz-kurlari/is-bankasi/ingiliz-sterlini sayfasını kontrol et. "
        "Yalnızca şu JSON'u döndür, başka metin yok: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık için nokta kullan."
    )
    raw = claude_search(prompt)
    d = extract_json(raw)
    return {
        "alis":  validate(float(d["alis"]),  GBP_MIN, GBP_MAX, "GBP alış"),
        "satis": validate(float(d["satis"]), GBP_MIN, GBP_MAX, "GBP satış"),
    }

def run_gbp():
    records = load_json(GBP_FILE)
    ref = prev_satis(records)
    prev = existing_entry(records, TODAY)  # mevcut kaydı sakla

    # 1. TCMB — birincil, her zaman çalışır
    tcmb = None
    try:
        tcmb = fetch_tcmb_gbp()
        log(f"  ✅ GBP TCMB   : alış={tcmb['alis']} satış={tcmb['satis']}")
    except Exception as e:
        log(f"  ❌ GBP TCMB HATA: {e}")

    # 2. İşBankası bayi kuru — ikincil
    isbank = None
    try:
        isbank = fetch_isbank_gbp()
        if ref and abs(isbank["satis"] - ref) / ref > 0.08:
            log(f"  ⚠  GBP İşBankası: satış {isbank['satis']} öncekinden >%8 sapıyor")
        log(f"  ✅ GBP İşBankası: alış={isbank['alis']} satış={isbank['satis']}")
    except Exception as e:
        log(f"  ⚠  GBP İşBankası başarısız: {e}")

    if tcmb is None and isbank is None:
        log("  ❌ GBP: Her iki kaynak başarısız, kayıt atlanıyor.")
        return

    # Kaydı birleştir
    entry = {"date": TODAY, "kaynak": "İşBankası+TCMB"}

    isbank_s = isbank or tcmb
    entry["alis"]   = isbank_s["alis"]
    entry["satis"]  = isbank_s["satis"]

    # İşBankası başarısız olduysa — mevcut kayıttaki manuel değeri koru, null yazma
    if isbank:
        entry["isbank_alis"]  = isbank["alis"]
        entry["isbank_satis"] = isbank["satis"]
    elif prev:
        entry["isbank_alis"]  = prev.get("isbank_alis")
        entry["isbank_satis"] = prev.get("isbank_satis")
        # Manuel değer varsa onu ana alis/satis olarak da koru
        if entry["isbank_alis"] is not None:
            entry["alis"]  = entry["isbank_alis"]
            entry["satis"] = entry["isbank_satis"]
        entry["kaynak"] = "TCMB (İşBankası önceki korundu)"
        log(f"  ℹ  GBP İşBankası: önceki değer korundu (alış={entry['isbank_alis']} satış={entry['isbank_satis']})")
    else:
        entry["isbank_alis"]  = None
        entry["isbank_satis"] = None

    entry["tcmb_alis"]  = tcmb["alis"]  if tcmb else (prev.get("tcmb_alis")  if prev else None)
    entry["tcmb_satis"] = tcmb["satis"] if tcmb else (prev.get("tcmb_satis") if prev else None)

    if entry.get("isbank_satis") and entry.get("tcmb_satis"):
        entry["fark_satis"] = round(entry["isbank_satis"] - entry["tcmb_satis"], 4)
        entry["fark_alis"]  = round((entry["isbank_alis"] or 0) - entry["tcmb_alis"], 4)
        log(f"  📊 GBP Fark (İşBankası−TCMB): satış=+{entry['fark_satis']:.4f} alış=+{entry['fark_alis']:.4f}")

    action = upsert(records, entry)
    save_json(GBP_FILE, records)
    log(f"  💾 GBP {action}")


# ── ANA ───────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("HATA: ANTHROPIC_API_KEY eksik.", file=sys.stderr)
        sys.exit(1)
    log(f"=== Fiyat çekme başlıyor — {TODAY} ===")
    run_gold()
    run_gbp()
    log("=== Tamamlandı ===")

if __name__ == "__main__":
    main()
