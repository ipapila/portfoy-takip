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
  yoktur — tek bir resmi referans kuru vardır (saat başı yayınlanan
  XAU 995/1000 gram altın fiyatı). Bu yüzden merkez_alis == merkez_satis
  olarak kaydedilir; bu kasıtlıdır, hata değildir.

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

    # Not: Eskiden burada "sayfa genelinde makul aralıkta iki sayı ara" adlı
    # bir yedek yöntem vardı. O yöntem, birincil regex eşleşmediğinde sayfadaki
    # HERHANGİ İKİ uyumlu sayıyı (gram altınla ilgisiz olabilecek gümüş,
    # çeyrek altın, geçmiş tarihli veri vb. dahil) yanlışlıkla alış/satış
    # çifti sanıp kabul ediyordu — bu, Garanti Bankası'nda günlerce tutarlı
    # biçimde yanlış (~%35 sapmalı) bir fiyatın kaydedilmesine yol açtı.
    # Güvenilirlik için kaldırıldı: birincil regex tutmazsa doğrudan Claude
    # web search fallback'ine geçiyoruz (bkz. get_bank_gold).
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
    matches = re.findall(r'\{[^{}]+\}', text)
    if not matches:
        raise ValueError(f"JSON bulunamadı: {text[:300]}")
    return json.loads(matches[-1])  # metinde ek açıklama varsa bile en sonuncu JSON bloğunu al

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


REF_TOLERANCE = 0.20  # bir bankanın satış fiyatı, referans (serbest piyasa) fiyatından en fazla ±%20 sapabilir

def plausible(result, ref_price, label):
    """Sonucu (varsa) referans fiyatla kıyaslar; aşırı sapan sonuçları eler."""
    if result is None or ref_price is None:
        return result
    satis = result["satis"]
    dev = abs(satis - ref_price) / ref_price
    if dev > REF_TOLERANCE:
        log(f"  ⚠  {label}: {satis} referans fiyattan (~{round(ref_price,2)}) %{dev*100:.0f} sapıyor — mantıksız, reddedildi")
        return None
    return result

def get_bank_gold(bank_name, direct_urls, min_spread=BANK_MIN_SPREAD, max_spread=BANK_MAX_SPREAD, ref_price=None):
    try:
        result = fetch_bank_gold_direct(direct_urls, bank_name, min_spread, max_spread)
        checked = plausible(result, ref_price, bank_name)
        if checked:
            return checked
        if result and not checked:
            raise RuntimeError("çapraz doğrulamayı geçemedi")
    except Exception as e:
        log(f"  ⚠  {bank_name} doğrudan HTTP tamamen başarısız: {e} — Claude'a geçiliyor")
    try:
        result = fetch_bank_gold_web(bank_name, direct_urls, min_spread, max_spread)
        checked = plausible(result, ref_price, bank_name)
        if checked:
            log(f"  ✅ {bank_name} (Claude): alış={checked['alis']} satış={checked['satis']}")
            return checked
        log(f"  ❌ {bank_name}: Claude sonucu da referanstan aşırı sapıyor, reddedildi")
        return None
    except Exception as e:
        log(f"  ❌ {bank_name}: Claude de başarısız: {e}")
        return None


# ── KAYNAK TANIMLARI ─────────────────────────────────────────────────────

def get_garanti(ref_price=None):
    return get_bank_gold(
        "Garanti BBVA",
        [
            "https://altin.doviz.com/garanti-bbva/gram-altin",
            "https://canlialtinfiyatlari.com/banka/garanti-bankasi.html",
        ],
        ref_price=ref_price,
    )

def get_yapikredi(ref_price=None):
    return get_bank_gold(
        "Yapı Kredi Bankası",
        [
            "https://altin.doviz.com/yapi-kredi/gram-altin",
            "https://altin.doviz.com/yapikredi/gram-altin",
            "https://canlialtinfiyatlari.com/banka/yapi-kredi-bankasi.html",
        ],
        ref_price=ref_price,
    )

def get_merkez():
    """TCMB — saat başı yayınlanan resmi gram altın (XAU 995/1000) fiyatı.

    ÖNEMLİ: TCMB'nin kurlar/today.xml dosyası SADECE döviz kurlarını içerir,
    altın (XAU/XAS) hiç yayınlanmaz — o yüzden eski sürüm bu dosyada XAU
    arayıp her seferinde başarısız oluyordu. TCMB altını ayrı bir sayfada,
    "Saat Başı Belirlenen Döviz Kurları ve Altın Fiyatları" başlığı altında,
    hafta içi 10:00-15:00 arası saat başı yayınlıyor. Bu veri TEK bir
    referans fiyattır — bayi alış/satış makası yoktur — bu yüzden
    merkez_alis == merkez_satis olması kasıtlıdır, hata değildir.
    """
    try:
        url = "https://anlikaltinfiyatlari.com/altin/merkez-bankasi"
        raw = http_get(url, timeout=15)
        text = strip_html(raw)
        # "Gram Altın (XAU) 995/1000  -  6686.33  -  -  -  -" tipi satırı bul,
        # saat başı hücrelerindeki (10:00→15:00) son DOLU değeri al —
        # günün en son yayınlanan saatidir.
        m = re.search(r"Gram\s*Alt[ıi]n\s*\(?XAU\)?\s*995\s*/\s*1000\s*((?:[-\d.,]+\s+){1,8})", text, re.I)
        if not m:
            raise RuntimeError("sayfada XAU 995/1000 saat başı satırı bulunamadı")
        tokens = m.group(1).split()
        nums = []
        for t in tokens:
            if t == "-" or not re.match(r'^[\d.,]+$', t):
                continue
            try:
                v = parse_tr_number(t)
                if GOLD_MIN <= v <= GOLD_MAX:
                    nums.append(v)
            except Exception:
                pass
        if not nums:
            raise RuntimeError("satırda geçerli bir sayısal değer yok (bugün henüz yayınlanmamış olabilir)")
        satis = round(nums[-1], 2)  # günün en son yayınlanan saat değeri
        validate(satis, GOLD_MIN, GOLD_MAX, "TCMB Merkez Bankası (saat başı)")
        log(f"  ✅ Merkez Bankası (TCMB saat başı, direkt HTTP): {satis}")
        return {"alis": satis, "satis": satis, "kaynak": "TCMB saat başı altın fiyatı (direkt HTTP)"}
    except Exception as e:
        log(f"  ⚠  Merkez Bankası doğrudan kaynak başarısız: {e} — Claude'a geçiliyor")
    try:
        ts = int(time.time())
        prompt = (
            f"Bugünün tarihi {TODAY}, Unix timestamp {ts}. "
            "TCMB'nin (Türkiye Cumhuriyet Merkez Bankası) BUGÜN "
            f"({TODAY}) saat başı (10:00-15:00 arası) yayınladığı "
            "GRAM ALTIN (XAU, 995/1000 ayar) referans fiyatını TL cinsinden bul. "
            "Şu sayfaları kontrol et: "
            "1) anlikaltinfiyatlari.com/altin/merkez-bankasi "
            "2) tcmb.gov.tr 'Saat Başı Belirlenen Döviz Kurları ve Altın Fiyatları' sayfası "
            "3) canlialtinfiyatlari.com veya benzeri bir kaynakta 'merkez bankası altın' araması. "
            "Günün en son yayınlanan saatindeki değeri kullan; TCMB bugün için henüz "
            "yayınlamamışsa dünkü (bir önceki iş günü) değeri kullan. "
            "TCMB bu veriyi tek bir referans fiyat olarak yayınlar, alış/satış "
            "makası yoktur — aynı sayıyı hem alış hem satış olarak döndür. "
            "ÇOK ÖNEMLİ: Kesin doğrulayamasan bile arama sonuçlarında gördüğün EN "
            "MAKUL rakamı kullan — asla açıklama, özür veya gerekçe yazma, sadece "
            "aşağıdaki JSON'u döndür. Hiçbir şekilde düz metin yanıt verme. "
            'Sonucu YALNIZCA şu JSON formatında döndür: {"alis": <sayi>, "satis": <sayi>} '
            "(iki değer de aynı olmalı). Ondalık için nokta kullan. Başka hiçbir metin ekleme."
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
# Not: serbest piyasa fiyatı ÖNCE çekilir; diğer üç kaynağın sonucu bu
# fiyata göre makul bir toleransla (±%20) çapraz doğrulanır. Bu, bir
# kaynağın scraping hatası sonucu tamamen alakasız bir rakam (gümüş,
# çeyrek altın, geçmiş veri vb.) yakalayıp sessizce kaydedilmesini önler.

def run():
    records = load_json(OUT_FILE)
    prev = existing_entry(records, TODAY)

    entry = {"date": TODAY}
    any_ok = False

    serbest_result = get_serbest()
    ref_price = serbest_result["satis"] if serbest_result else \
        (prev.get("serbest_satis") if prev else None)  # bugün çekilemezse dünkü fiyatı referans al

    ordered = [
        ("serbest",   lambda: serbest_result),
        ("garanti",   lambda: get_garanti(ref_price=ref_price)),
        ("yapikredi", lambda: get_yapikredi(ref_price=ref_price)),
        ("merkez",    get_merkez),  # TCMB tek referans fiyat; farklı bir mantıkla çalışıyor, çapraz doğrulanmıyor
    ]

    for key, fn in ordered:
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

    SOURCE_KEYS = ["garanti", "yapikredi", "merkez", "serbest"]

    # manuel düzeltme korunuyor mu?
    if prev and any(str(prev.get(f"{k}_kaynak", "")).endswith("(manuel)") for k in SOURCE_KEYS):
        log("  ℹ  Bugün için manuel kayıt var, otomatik veri üzerine yazılmıyor.")
        for k in SOURCE_KEYS:
            if str(prev.get(f"{k}_kaynak", "")).endswith("(manuel)"):
                entry[f"{k}_alis"]   = prev.get(f"{k}_alis")
                entry[f"{k}_satis"]  = prev.get(f"{k}_satis")
                entry[f"{k}_kaynak"] = prev.get(f"{k}_kaynak")

    action = upsert(records, entry)
    save_json(OUT_FILE, records)
    ozet = " | ".join(f"{k}={entry.get(k+'_satis')}" for k in SOURCE_KEYS)
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
