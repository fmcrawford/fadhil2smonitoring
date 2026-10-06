# 48Group 2-Shot Monitor — Deplexo Edition

Starter project untuk memindahkan bot terminal menjadi web service multi-user + multi-webhook.

## Fitur yang sudah ada

- JKT48 2-Shot + AKB48 2-Shot menggunakan endpoint yang sama dengan bot lama.
- Polling API default setiap 15 detik.
- Restock = state sebelumnya `stock <= 0`, state baru `stock > 0`.
- Restock langsung broadcast ke semua webhook aktif yang subscribe grup terkait.
- Restock mengirim `@everyone` dengan `allowed_mentions` Discord.
- Rekap otomatis 08:00, 12:00, 20:00 WIB.
- Scheduled report tidak `@everyone`.
- Akun/login; setiap user dapat menambahkan lebih dari satu webhook.
- Webhook dapat memilih JKT48, AKB48, atau keduanya.
- Test / enable-disable / delete webhook dari dashboard.
- Webhook URL dienkripsi dengan Fernet sebelum masuk SQLite.
- State stok + history restock disimpan di database.
- Persistent scheduled-run key mencegah rekap dobel setelah restart pada menit jadwal.
- `/health` untuk mengecek worker/API.

## 0. WAJIB: revoke webhook lama

Webhook yang pernah ada di source code, chat, screenshot, atau repository harus dianggap bocor.
Hapus/revoke webhook lama di Discord dan buat webhook baru.

Jangan pernah commit URL webhook baru ke GitHub.

## 1. Menjalankan lokal (Windows / PowerShell)

Install Python 3.12, lalu dari folder project:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Generate dua secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set environment variable untuk terminal tersebut:

```powershell
$env:APP_SECRET="PASTE_APP_SECRET"
$env:WEBHOOK_ENCRYPTION_KEY="PASTE_FERNET_KEY"
$env:DATABASE_PATH="./data/monitor.db"
$env:CHECK_INTERVAL="15"
$env:COOKIE_SECURE="false"
uvicorn app.main:app --host 0.0.0.0 --port 3000 --workers 1
```

Buka:

```text
http://localhost:3000
```

Daftar akun, kemudian tambahkan **webhook baru** dari dashboard.

## 2. Test yang harus dilakukan lokal

Saat webhook ditambahkan, Discord harus menerima:

1. `TEST WEBHOOK BERHASIL`;
2. `48GROUP MONITOR ACTIVATED`.

Dashboard harus menunjukkan:

- status `LIVE`;
- `Last API check` terus berubah sekitar setiap 15 detik;
- jumlah slot JKT48/AKB48;
- daftar slot yang tersedia.

Endpoint diagnostik:

```text
http://localhost:3000/health
```

Contoh sehat:

```json
{
  "ok": true,
  "monitor_running": true,
  "last_check": "...",
  "last_success": "...",
  "last_error": null
}
```

## 3. Permission Discord untuk @everyone

Agar mention benar-benar melakukan ping, webhook/channel/role Discord harus mengizinkan
`Mention @everyone, @here, and All Roles` sesuai konfigurasi permission server Anda.

Aplikasi ini mengirim payload `allowed_mentions` untuk `everyone`, tetapi Discord tetap
menghormati permission channel/server.

## 4. Push ke GitHub

Buat repository kosong, lalu:

```bash
git init
git add .
git commit -m "Initial 48Group Deplexo monitor"
git branch -M main
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

Periksa:

```bash
git status
git ls-files
```

Pastikan `.env`, database, dan webhook URL tidak ikut ter-commit.

## 5. Deploy di Deplexo

Project sudah memiliki `Dockerfile` dan `deplexo.yaml`.

Di dashboard Deplexo:

1. Create/Deploy Application.
2. Connect GitHub dan pilih repository project ini.
3. Root directory = root repository.
4. Build = Dockerfile.
5. Tambahkan environment variables:

```text
PORT=3000
DATABASE_PATH=/data/monitor.db
CHECK_INTERVAL=15
APP_SECRET=<secret-yang-tadi-dibuat>
WEBHOOK_ENCRYPTION_KEY=<fernet-key-yang-tadi-dibuat>
COOKIE_SECURE=true
```

6. Aktifkan persistent storage dengan mount path `/data`.
7. Deploy.
8. Buka URL aplikasi yang diberikan Deplexo.
9. Buka `/health` dan pastikan `monitor_running=true` serta `last_success` terisi.
10. Register/login dan tambahkan webhook Discord baru.

**Jangan mengganti `WEBHOOK_ENCRYPTION_KEY` setelah webhook tersimpan**, kecuali Anda siap
menambahkan ulang seluruh webhook. Key berbeda tidak bisa mendekripsi data lama.

## 6. Jadwal

Timezone di project dikunci ke `Asia/Jakarta`.

- 08:00 WIB
- 12:00 WIB
- 20:00 WIB

Rekap memakai window dua menit pertama agar polling 15 detik tidak melewatkan jadwal.
Database menyimpan `scheduled_runs`, jadi restart container dalam window yang sama tidak
membuat rekap terkirim dua kali.

## 7. Kenapa Uvicorn hanya `--workers 1`?

Monitoring background saat ini berada di aplikasi web yang sama. Jika Anda menjalankan 2+
worker, masing-masing worker dapat memulai monitor sendiri dan berpotensi menggandakan request
API/notifikasi.

Untuk versi skala besar, pecah menjadi:

```text
Web service (dashboard/API)
        |
        +---- PostgreSQL
        |
Worker monitor tunggal
```

Versi starter ini sengaja 1 worker agar aman untuk MVP.

## 8. Struktur

```text
48group-deplexo-monitor/
├── app/
│   ├── main.py        # login, dashboard, webhook routes
│   ├── monitor.py     # polling, restock, scheduler Discord
│   ├── db.py          # SQLite + persistence
│   ├── security.py    # password/session/webhook encryption
│   ├── config.py
│   ├── templates/
│   └── static/
├── Dockerfile
├── deplexo.yaml
├── requirements.txt
├── start.sh
└── .env.example
```

## 9. Sebelum membuka dashboard untuk publik luas

Starter ini sudah layak untuk private/community MVP, tetapi untuk public SaaS tambahkan:

- CSRF protection;
- rate limiting login/register/webhook test;
- email verification + reset password;
- CAPTCHA/anti-abuse bila registrasi bebas;
- PostgreSQL eksternal;
- worker terpisah;
- audit log/admin panel;
- backup database;
- observability/alert saat polling API gagal berulang.
