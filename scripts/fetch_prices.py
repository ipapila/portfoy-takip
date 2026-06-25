#!/usr/bin/env python3
"""
Günlük fiyat güncelleme scripti.
GitHub Actions tarafından her gün çalıştırılır.

Veri kaynakları:
  - TCMB XML  → https://www.tcmb.gov.tr/kurlar/today.xml  (GBP resmi kur)
  - Claude web search → İş Bankası gram altın + İş Bankası GBP kuru
  - Doğrulama: değerler makul aralıkta mı kontrol edilir
  - Fallback: İş Bankası başarısız olursa TCMB verisi kullanılır
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

# Makul aralıklar — bu dışına çıkan veri reddedilir
GOLD_MIN, GOLD_MAX = 3000.0, 20000.0
GBP_MIN,  GBP_MAX  = 30.0,   200.0


# ── YARDIMCI FONKSİYONLAR ────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def validate(value: float, vmin: float, vmax: float, label: str) -> float:
    if not (vmin <= value <= vmax):
        raise ValueError(f"{label} değeri aralık dışı: {value} (beklenen {vmin}–{vmax})")
    return value


def load_json(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def upsert(records: list, entry: dict) -> str:
    for i, r in enumerate(records):
        if r["date"] == entry["date"]:
            records[i] = entry
            return "GÜNCELLENDİ"
    records.append(entry)
    records.sort(key=lambda x: x["date"])
    return "EKLENDİ"


def prev_value(records: list, key: str) -> float | None:
    """Son iki kaydın ortalaması — ani sapma tespiti için referans."""
    vals = [r[key] for r in records[-2:] if key in r]
    return sum(vals) / len(vals) if vals else None


# ── TCMB XML (güvenilir, makine okunabilir) ──────────────────────────────────

def fetch_tcmb_gbp() -> dict:
    """TCMB günlük kur XML'inden GBP alış/satış çeker."""
    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        tree = ET.parse(resp)
    root = tree.getroot()
    for currency in root.findall("Currency"):
        if currency.get("CurrencyCode") == "GBP":
            alis  = float(currency.findtext("ForexBuying")  or currency.findtext("BanknoteBuying")  or "0")
            satis = float(currency.findtext("ForexSelling") or currency.findtext("BanknoteSelling") or "0")
            validate(alis,  GBP_MIN, GBP_MAX, "TCMB GBP alış")
            validate(satis, GBP_MIN, GBP_MAX, "TCMB GBP satış")
            return {"date": TODAY, "alis": round(alis, 4), "satis": round(satis, 4), "kaynak": "TCMB"}
    raise ValueError("TCMB XML'de GBP bulunamadı")


# ── CLAUDE WEB SEARCH ────────────────────────────────────────────────────────

def claude_fetch(prompt: str) -> str:
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


def extract_json(text: str) -> dict:
    m = re.search(r'\{[^}]+\}', text)
    if not m:
        raise ValueError(f"JSON bulunamadı: {text[:300]}")
    return json.loads(m.group())


def fetch_isbank_gold() -> dict:
    prompt = (
        f"Bugün {TODAY} tarihinde İş Bankası gram altın BAYİ ALIŞ ve BAYİ SATIŞ fiyatı nedir? "
        "Şu adresi kontrol et: canlidoviz.com/altin-fiyatlari/is-bankasi/gram-altin — "
        "sayfada 'BAYİ ALIŞ' ve 'BAYİ SATIŞ' etiketli değerleri bul. "
        "SADECE JSON döndür, başka metin yok: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık ayırıcı olarak nokta kullan."
    )
    raw = claude_fetch(prompt)
    d   = extract_json(raw)
    alis  = validate(float(d["alis"]),  GOLD_MIN, GOLD_MAX, "İşbank altın alış")
    satis = validate(float(d["satis"]), GOLD_MIN, GOLD_MAX, "İşbank altın satış")
    return {"date": d.get("tarih", TODAY), "alis": alis, "satis": satis, "kaynak": "İşBankası"}


def fetch_isbank_gbp() -> dict:
    prompt = (
        f"Bugün {TODAY} tarihinde Türkiye İş Bankası'nın İngiliz Sterlini (GBP) "
        "BAYİ ALIŞ ve BAYİ SATIŞ kuru nedir? "
        "Şu adresi kontrol et: canlidoviz.com/doviz-kurlari/is-bankasi/ingiliz-sterlini — "
        "sayfada 'BAYİ ALIŞ' ve 'BAYİ SATIŞ' etiketli TL değerlerini bul. "
        "SADECE JSON döndür, başka metin yok: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık ayırıcı olarak nokta kullan. Örnek: {\"alis\": 61.50, \"satis\": 63.80, \"tarih\": \"2026-06-24\"}"
    )
    raw = claude_fetch(prompt)
    d   = extract_json(raw)
    alis  = validate(float(d["alis"]),  GBP_MIN, GBP_MAX, "İşbank GBP alış")
    satis = validate(float(d["satis"]), GBP_MIN, GBP_MAX, "İşbank GBP satış")
    return {"date": d.get("tarih", TODAY), "alis": alis, "satis": satis, "kaynak": "İşBankası"}


# ── ALTIN ────────────────────────────────────────────────────────────────────

def run_gold():
    records = load_json(GOLD_FILE)
    ref = prev_value(records, "satis")

    try:
        entry = fetch_isbank_gold()
        # Ani sapma kontrolü: önceki değerden %8'den fazla fark varsa uyar
        if ref and abs(entry["satis"] - ref) / ref > 0.08:
            log(f"  ⚠  ALTIN: satış {entry['satis']} önceki referanstan ({ref:.2f}) >%8 sapıyor, yine de kaydediliyor")
        action = upsert(records, entry)
        save_json(GOLD_FILE, records)
        log(f"  ✅ ALTIN {action} [{entry['kaynak']}]: alış={entry['alis']} satış={entry['satis']}")
    except Exception as e:
        log(f"  ❌ ALTIN HATA: {e}")


# ── GBP ──────────────────────────────────────────────────────────────────────

def run_gbp():
    records = load_json(GBP_FILE)
    ref = prev_value(records, "satis")

    # 1. İş Bankası dene
    isbank_entry = None
    try:
        isbank_entry = fetch_isbank_gbp()
        if ref and abs(isbank_entry["satis"] - ref) / ref > 0.08:
            log(f"  ⚠  GBP İşbank: satış {isbank_entry['satis']} referanstan ({ref:.2f}) >%8 sapıyor")
        log(f"  ✅ GBP İşBankası: alış={isbank_entry['alis']} satış={isbank_entry['satis']}")
    except Exception as e:
        log(f"  ⚠  GBP İşBankası başarısız ({e}), TCMB'ye geçiliyor...")

    # 2. TCMB her zaman çek (karşılaştırma için)
    tcmb_entry = None
    try:
        tcmb_entry = fetch_tcmb_gbp()
        log(f"  ✅ GBP TCMB    : alış={tcmb_entry['alis']} satış={tcmb_entry['satis']}")
    except Exception as e:
        log(f"  ❌ GBP TCMB HATA: {e}")

    # 3. Kaydı oluştur: İşbank varsa onu kullan, yoksa TCMB
    primary = isbank_entry or tcmb_entry
    if primary is None:
        log("  ❌ GBP: Her iki kaynak da başarısız, kayıt atlanıyor.")
        return

    # 4. Fark bilgisini ekle
    entry = dict(primary)
    entry["date"] = TODAY
    if isbank_entry and tcmb_entry:
        entry["isbank_satis"] = isbank_entry["satis"]
        entry["isbank_alis"]  = isbank_entry["alis"]
        entry["tcmb_satis"]   = tcmb_entry["satis"]
        entry["tcmb_alis"]    = tcmb_entry["alis"]
        entry["fark_satis"]   = round(isbank_entry["satis"] - tcmb_entry["satis"], 4)
        entry["fark_alis"]    = round(isbank_entry["alis"]  - tcmb_entry["alis"],  4)
        log(f"  📊 GBP Fark (İşbank−TCMB): satış={entry['fark_satis']:+.4f}, alış={entry['fark_alis']:+.4f}")

    action = upsert(records, entry)
    save_json(GBP_FILE, records)
    log(f"  💾 GBP {action} [{entry['kaynak']}]")


# ── ANA ───────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("HATA: ANTHROPIC_API_KEY ortam değişkeni eksik.", file=sys.stderr)
        sys.exit(1)

    log(f"=== Güncelleme başlıyor — {TODAY} ===")
    run_gold()
    run_gbp()
    log("=== Tamamlandı ===")


if __name__ == "__main__":
    main()
