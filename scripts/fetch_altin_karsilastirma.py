#!/usr/bin/env python3
"""
Gram Altın Karşılaştırma — günlük fiyat çekme scripti.

  Kaynaklar (yalnızca gram altın, TL/gram):
    1. Garanti Bankası (BBVA)
    2. Yapı Kredi Bankası
    3. Merkez Bankası (TCMB)
    4. Serbest Piyasa (bankaya özel olmayan genel piyasa fiyatı)

  Her kaynak için öncelik sırası:
    a) Doğrudan HTTP taraması (doviz.com / canlialtinfiyatlari.com üzerinden
       o bankaya özel gram altın sayfası — JS gerekmez, hızlı ve ucuz)
    b) Claude web araması (fallback) — o bankanın SADECE bugünkü resmi
       gram altın kurunu, alış/satış makası mantıklı aralıkta olacak
       şekilde arar.

  Merkez Bankası için TCMB'nin herhangi bir bayi alış/satış makası
  yoktur — tek bir resmi referans kuru vardır (XAU çapraz kurundan
  gram'a çevrilir). Bu yüzden merkez_alis == merkez_satis olarak
  kaydedilir; bu kasıtlıdır, hata değildir.

  NOT (kaynak doğrulaması — İlk kurulumda mutlaka kontrol edin):
  Yapı Kredi ve Garanti için doviz.com/canlialtinfiyatlari URL slug'ları
  tahmine dayalıdır (İş Bankası scriptindeki gibi zamanla doğrulanmalı).
  İlk birkaç çalıştırmada Actions loglarını kontrol edip hangi kaynağın
  gerçekten işe yaradığını görün; gerekirse aday listelerini güncelleyin.

  Çıktı: data/altin-karsilastirma.json
"""

import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime

API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
OUT_FILE = "data/altin-karsilastirma.json"
TODAY    = date.today().isoformat()

GOLD_MIN, GOLD_MAX = 3000.0, 25000.0
# Bankaya özel gram altın alış/satış makası, tarihsel olarak ~%1.5-5 arası
# seyrediyor (bkz. İşBankası scripti dip notları). Serbest piyasa makası
# genelde daha dar (~%0.5-3) olabildiği için ayrı bir üst sınır tanımlıyoruz.
BANK_MIN_SPREAD, BANK_MAX_SPREAD = 0.010, 0.06
SERBEST_MIN_SPREAD, SERBEST_MAX_SPREAD = 0.0, 0.06

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


# ── YARDIMCILAR ────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def validate(val, vmin, vmax, label):
    if val is None or not (vmin <= val <= vmax):
        raise ValueError(f"{label} aralık dışı: {val} (beklenen {vmin}-{vmax})")
    return val

def load_json(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
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

def existing_entry(records, d):
    return next((r for r in records if r["date"] == d), None)

def parse_tr_number(s):
    s = s.strip().replace("\xa0", "").replace(" ", "")
    if re.match(r'^\d{1,3}(\.\d{3})+(,\d+)?$', s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def strip_html(raw):
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&nbsp;?", " ", text)
    return text


# ── DOĞRUDAN HTTP: doviz.com tipi "alış/satış" sayfası ─────────────────────

def fetch_direct_pair(url, min_spread, max_spread, label):
    """doviz.com/canlialtinfiyatlari tipi sayfalardan alış/satış çifti dener."""
    raw = http_get(url)
    text = strip_html(raw)
    m = re.search(
        r"(?:Al[ıi][şs]|bozdurma)\s*/?\s*(?:Sat[ıi][şs])?[^\d]{0,40}"
        r"(\d{1,2}[.,]\d{3}[.,]\d{1,4}|\d{4,5}[.,]\d{1,4})\s*/?\s*"
        r"(\d{1,2}[.,]\d{3}[.,]\d{1,4}|\d{4,5}[.,]\d{1,4})",
        text, re.I,
    )
    if m:
        a, b = parse_tr_number(m.group(1)), parse_tr_number(m.group(2))
        alis, satis = (a, b) if a < b else (b, a)
        if GOLD_MIN <= alis <= GOLD_MAX and GOLD_MIN <= satis <= GOLD_MAX \
                and min_spread <= (satis - alis) / alis <= max_spread:
            log(f"  ✅ {label} (direkt HTTP, alış/satış): {alis} / {satis}")
            return {"alis": round(alis, 2), "satis": round(satis, 2), "kaynak": f"{label} (direkt HTTP)"}

    # yedek: sayfa genelinde makul aralıkta iki sayı ara
    nums = []
    for mm in re.finditer(r'\b[3-9]\d{3}[.,]\d{1,4}\b|\b1\d{4}[.,]\d{1,4}\b', text):
        try:
            v = parse_tr_number(mm.group())
            if GOLD_MIN <= v <= GOLD_MAX:
                nums.append(v)
        except Exception:
            pass
    nums = sorted(set(nums))
    pairs = [(a, b) for a in nums for b in nums if b > a and min_spread <= (b - a) / a <= max_spread]
    if pairs:
        alis, satis = pairs[0]
        log(f"  ✅ {label} (direkt HTTP, metin tarama): {alis} / {satis}")
        return {"alis": round(alis, 2), "satis": round(satis, 2), "kaynak": f"{label} (direkt HTTP, metin)"}

    raise RuntimeError(f"{label}: sayfadan makul alış/satış çifti çıkarılamadı")


def fetch_bank_gold_direct(url_candidates, label, min_spread=BANK_MIN_SPREAD, max_spread=BANK_MAX_SPREAD):
    last_err = None
    for url in url_candidates:
        try:
            return fetch_direct_pair(url, min_spread, max_spread, label)
        except Exception as e:
            last_err = e
            log(f"  ⚠  {label} ({url}) başarısız: {e}")
    raise RuntimeError(f"{label}: tüm doğrudan HTTP adayları başarısız ({last_err})")


# ── CLAUDE WEB ARAMASI (fallback) ───────────────────────────────────────────

def claude_search(prompt):
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}],
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

def fetch_bank_gold_web(bank_name, hint_urls, min_spread=BANK_MIN_SPREAD, max_spread=BANK_MAX_SPREAD):
    ts = int(time.time())
    hints = " ".join(f"{i+1}) {u}" for i, u in enumerate(hint_urls))
    prompt = (
        f"Bugünün tarihi {TODAY}, Unix timestamp {ts}. "
        f"Türkiye'de {bank_name}'nın BUGÜN ({TODAY}) yayınladığı gram altın "
        "ALIŞ ve SATIŞ fiyatını (kendi bayi/gişe kuru, genel piyasa referansı DEĞİL) bul. "
        f"Şu sayfaları canlı kontrol et: {hints} "
        f"Sadece {TODAY} tarihli veriyi kullan, önbellek/eski veri kullanma. "
        f"Makul alış-satış makası yaklaşık %{min_spread*100:.1f}-%{max_spread*100:.1f} "
        "arasındadır; bunun dışına çıkan bir eşleşme bulursan başka kaynağa bak. "
        'Sonucu YALNIZCA şu JSON formatında döndür, başka metin ekleme: '
        '{"alis": <sayi>, "satis": <sayi>} Ondalık için nokta kullan.'
    )
    d = extract_json(claude_search(prompt))
    return {
        "alis":  validate(round(float(d["alis"]), 2),  GOLD_MIN, GOLD_MAX, f"Claude {bank_name} alış"),
        "satis": validate(round(float(d["satis"]), 2), GOLD_MIN, GOLD_MAX, f"Claude {bank_name} satış"),
        "kaynak": "Claude web search",
    }


def get_bank_gold(bank_name, direct_urls, min_spread=BANK_MIN_SPREAD, max_spread=BANK_MAX_SPREAD):
    try:
        return fetch_bank_gold_direct(direct_urls, bank_name, min_spread, max_spread)
    except Exception as e:
        log(f"  ⚠  {bank_name} doğrudan HTTP tamamen başarısız: {e} — Claude'a geçiliyor")
    try:
        result = fetch_bank_gold_web(bank_name, direct_urls, min_spread, max_spread)
        log(f"  ✅ {bank_name} (Claude): alış={result['alis']} satış={result['satis']}")
        return result
    except Exception as e:
        log(f"  ❌ {bank_name}: Claude de başarısız: {e}")
        return None


# ── KAYNAK TANIMLARI ─────────────────────────────────────────────────────

def get_garanti():
    return get_bank_gold(
        "Garanti BBVA",
        [
            "https://altin.doviz.com/garanti-bbva/gram-altin",
            "https://canlialtinfiyatlari.com/banka/garanti-bankasi.html",
        ],
    )

def get_yapikredi():
    return get_bank_gold(
        "Yapı Kredi Bankası",
        [
            "https://altin.doviz.com/yapi-kredi/gram-altin",
            "https://altin.doviz.com/yapikredi/gram-altin",
            "https://canlialtinfiyatlari.com/banka/yapi-kredi-bankasi.html",
        ],
    )

def get_merkez():
    """TCMB — tek referans kur (bayi alış/satış makası yok)."""
    try:
        url = "https://www.tcmb.gov.tr/kurlar/today.xml"
        raw = http_get(url, timeout=15)
        root = ET.fromstring(raw)
        for cur in root.findall("Currency"):
            if cur.get("CurrencyCode") == "XAU":
                satis = float(cur.findtext("ForexSelling") or cur.findtext("BanknoteSelling") or 0)
                if satis > 50000:  # ons ise grama çevir
                    satis = satis / 31.1035
                satis = round(satis, 2)
                validate(satis, GOLD_MIN, GOLD_MAX, "TCMB Merkez Bankası")
                log(f"  ✅ Merkez Bankası (TCMB XML): {satis}")
                return {"alis": satis, "satis": satis, "kaynak": "TCMB XML"}
        raise RuntimeError("TCMB XML'de XAU kodu yok")
    except Exception as e:
        log(f"  ⚠  Merkez Bankası TCMB XML başarısız: {e} — Claude'a geçiliyor")
    try:
        ts = int(time.time())
        prompt = (
            f"Bugünün tarihi {TODAY}, Unix timestamp {ts}. "
            f"Türkiye Cumhuriyet Merkez Bankası'nın (TCMB) BUGÜN ({TODAY}) yayınladığı "
            "gram altın (has altın / XAU) referans fiyatını TL cinsinden bul "
            "(tcmb.gov.tr veya EVDS üzerinden). Tek bir referans fiyattır, alış/satış "
            "makası yoktur — aynı sayıyı hem alış hem satış olarak döndür. "
            'Sonucu YALNIZCA şu JSON formatında döndür: {"alis": <sayi>, "satis": <sayi>} '
            "(iki değer de aynı olmalı). Ondalık için nokta kullan."
        )
        d = extract_json(claude_search(prompt))
        v = round(float(d.get("satis") or d.get("alis")), 2)
        validate(v, GOLD_MIN, GOLD_MAX, "Claude Merkez Bankası")
        log(f"  ✅ Merkez Bankası (Claude): {v}")
        return {"alis": v, "satis": v, "kaynak": "Claude web search"}
    except Exception as e:
        log(f"  ❌ Merkez Bankası: Claude de başarısız: {e}")
        return None

def get_serbest():
    return get_bank_gold(
        "Serbest Piyasa (genel gram altın)",
        [
            "https://altin.doviz.com/gram-altin",
            "https://bigpara.hurriyet.com.tr/altin/",
        ],
        min_spread=SERBEST_MIN_SPREAD,
        max_spread=SERBEST_MAX_SPREAD,
    )


# ── KAYDET ───────────────────────────────────────────────────────────────

SOURCES = [("garanti", get_garanti), ("yapikredi", get_yapikredi),
           ("merkez", get_merkez), ("serbest", get_serbest)]

def run():
    records = load_json(OUT_FILE)
    prev = existing_entry(records, TODAY)

    entry = {"date": TODAY}
    any_ok = False
    for key, fn in SOURCES:
        result = fn()
        if result:
            entry[f"{key}_alis"]   = result["alis"]
            entry[f"{key}_satis"]  = result["satis"]
            entry[f"{key}_kaynak"] = result["kaynak"]
            any_ok = True
        elif prev and prev.get(f"{key}_satis") is not None:
            entry[f"{key}_alis"]   = prev.get(f"{key}_alis")
            entry[f"{key}_satis"]  = prev.get(f"{key}_satis")
            entry[f"{key}_kaynak"] = "önceki değer korundu (bugün çekilemedi)"
            log(f"  ℹ  {key}: bugün çekilemedi, önceki değer korundu")
        else:
            entry[f"{key}_alis"]   = None
            entry[f"{key}_satis"]  = None
            entry[f"{key}_kaynak"] = None

    if not any_ok:
        log("  ❌ Dört kaynağın hiçbiri çekilemedi, kayıt atlanıyor.")
        return

    # manuel düzeltme korunuyor mu?
    if prev and any(str(prev.get(f"{k}_kaynak", "")).endswith("(manuel)") for k, _ in SOURCES):
        log("  ℹ  Bugün için manuel kayıt var, otomatik veri üzerine yazılmıyor.")
        for k, _ in SOURCES:
            if str(prev.get(f"{k}_kaynak", "")).endswith("(manuel)"):
                entry[f"{k}_alis"]   = prev.get(f"{k}_alis")
                entry[f"{k}_satis"]  = prev.get(f"{k}_satis")
                entry[f"{k}_kaynak"] = prev.get(f"{k}_kaynak")

    action = upsert(records, entry)
    save_json(OUT_FILE, records)
    ozet = " | ".join(f"{k}={entry.get(k+'_satis')}" for k, _ in SOURCES)
    log(f"  💾 {action}: {ozet}")


def main():
    if not API_KEY:
        print("HATA: ANTHROPIC_API_KEY eksik.", file=sys.stderr)
        sys.exit(1)
    log(f"=== Gram Altın Karşılaştırma çekme başlıyor — {TODAY} ===")
    run()
    log("=== Tamamlandı ===")

if __name__ == "__main__":
    main()
