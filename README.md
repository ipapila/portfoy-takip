# Portföy Takip — İş Bankası Gram Altın & GBP/TRY

Günlük otomatik veri güncellemesiyle çalışan, statik HTML portföy takip uygulaması.

## Portföyler

| Varlık | Başlangıç Tarihi | Miktar | Başlangıç Fiyatı |
|--------|-----------------|--------|-----------------|
| İş Bankası Gram Altın | 27 Mart 2026 | 45 XAU | 6.689,22 TL/gr (≈ 301.015 TL) |
| GBP/TRY Serbest Piyasa | 23 Şubat 2026 | 700 GBP | 57,45 TL/GBP |

## Nasıl Çalışır?

```
GitHub Actions (her gün 18:00 TR)
       ↓
scripts/fetch_prices.py
       ↓  (Anthropic API + web search)
data/gold.json  +  data/gbp.json  güncellenir
       ↓
git commit & push
       ↓
GitHub Pages otomatik yayınlar
```

## Kurulum

### 1. Repoyu fork'la / klonla

```bash
git clone https://github.com/KULLANICI/portfoy-takip.git
cd portfoy-takip
```

### 2. GitHub Secret ekle

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| İsim | Değer |
|------|-------|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |

### 3. GitHub Pages'i etkinleştir

Repo → **Settings → Pages → Source: Deploy from a branch → Branch: main → / (root)**

Birkaç dakika sonra `https://KULLANICI.github.io/portfoy-takip/` adresinde yayında olur.

### 4. İlk çalıştırma (opsiyonel)

Actions sekmesinden **"Günlük Fiyat Güncelleme"** → **"Run workflow"** ile manuel tetikle.

## Dosya Yapısı

```
portfoy-takip/
├── index.html              # Ana uygulama (saf HTML/JS, bağımlılıksız)
├── data/
│   ├── gold.json           # İş Bankası gram altın geçmişi
│   └── gbp.json            # GBP/TRY serbest piyasa geçmişi
├── scripts/
│   └── fetch_prices.py     # Günlük fiyat çekme scripti
└── .github/workflows/
    └── daily-update.yml    # GitHub Actions zamanlayıcısı
```

## Veri Formatı

**gold.json** ve **gbp.json** aynı şemayı kullanır:

```json
[
  { "date": "2026-03-27", "alis": 6520.00, "satis": 6689.22 },
  ...
]
```

Tarihe göre artan sırada tutulur. Script yeni kaydı ekler veya aynı tarihi günceller.

## Lokal Geliştirme

```bash
# Basit HTTP sunucusu (fetch() için gerekli)
python3 -m http.server 8080
# → http://localhost:8080
```

Scripti lokal çalıştırmak için:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 scripts/fetch_prices.py
```

## Lisans

MIT
