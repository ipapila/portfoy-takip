#!/usr/bin/env python3
"""
Günlük fiyat güncelleme scripti.
GitHub Actions tarafından her gün çalıştırılır.
ANTHROPIC_API_KEY ortam değişkeni gereklidir.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import date, datetime

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOLD_FILE = "data/gold.json"
GBP_FILE = "data/gbp.json"
TODAY = date.today().isoformat()


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
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    text = " ".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    return text


def extract_json(text: str) -> dict:
    import re
    m = re.search(r'\{[^}]+\}', text)
    if not m:
        raise ValueError(f"JSON bulunamadı: {text[:200]}")
    return json.loads(m.group())


def load(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path: str, data: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def upsert(records: list, entry: dict) -> bool:
    for i, r in enumerate(records):
        if r["date"] == entry["date"]:
            records[i] = entry
            return False  # updated
    records.append(entry)
    records.sort(key=lambda x: x["date"])
    return True  # inserted


def fetch_gold() -> dict:
    prompt = (
        "İş Bankası gram altın bugünkü alış ve satış fiyatı nedir? "
        "canlidoviz.com/altin-fiyatlari/is-bankasi/gram-altin veya anlikaltinfiyatlari.com/banka/is-bankasi "
        "adresinden kontrol et. "
        "SADECE şu JSON formatında cevap ver, başka hiçbir metin ekleme: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık için nokta kullan, virgül kullanma."
    )
    raw = claude_fetch(prompt)
    d = extract_json(raw)
    return {
        "date": d.get("tarih", TODAY),
        "alis": float(d["alis"]),
        "satis": float(d["satis"]),
    }


def fetch_gbp() -> dict:
    prompt = (
        "Türkiye İş Bankası İngiliz Sterlini (GBP) alış ve satış kuru bugün nedir? "
        "canlidoviz.com/doviz-kurlari/is-bankasi/ingiliz-sterlini veya "
        "anlikaltinfiyatlari.com/banka/is-bankasi adresinden kontrol et. "
        "Bayi alış ve bayi satış fiyatlarını bul. "
        "SADECE şu JSON formatında cevap ver, başka hiçbir metin ekleme: "
        '{"alis": <sayi>, "satis": <sayi>, "tarih": "<YYYY-MM-DD>"} '
        "Ondalık için nokta kullan, virgül kullanma. "
        "Örnek: {\"alis\": 61.50, \"satis\": 63.20, \"tarih\": \"2026-06-24\"}"
    )
    raw = claude_fetch(prompt)
    d = extract_json(raw)
    return {
        "date": d.get("tarih", TODAY),
        "alis": float(d["alis"]),
        "satis": float(d["satis"]),
    }


def main():
    if not API_KEY:
        print("HATA: ANTHROPIC_API_KEY ortam değişkeni eksik.", file=sys.stderr)
        sys.exit(1)

    print(f"[{datetime.now().isoformat()}] Güncelleme başlıyor — tarih: {TODAY}")

    # Gold
    try:
        gold_records = load(GOLD_FILE)
        gold_entry = fetch_gold()
        added = upsert(gold_records, gold_entry)
        save(GOLD_FILE, gold_records)
        action = "EKLENDİ" if added else "GÜNCELLENDİ"
        print(f"  ALTIN {action}: alış={gold_entry['alis']} satış={gold_entry['satis']} tarih={gold_entry['date']}")
    except Exception as e:
        print(f"  ALTIN HATA: {e}", file=sys.stderr)

    # GBP
    try:
        gbp_records = load(GBP_FILE)
        gbp_entry = fetch_gbp()
        added = upsert(gbp_records, gbp_entry)
        save(GBP_FILE, gbp_records)
        action = "EKLENDİ" if added else "GÜNCELLENDİ"
        print(f"  GBP/TRY {action}: alış={gbp_entry['alis']} satış={gbp_entry['satis']} tarih={gbp_entry['date']}")
    except Exception as e:
        print(f"  GBP HATA: {e}", file=sys.stderr)

    print(f"[{datetime.now().isoformat()}] Tamamlandı.")


if __name__ == "__main__":
    main()
