# Bank Statement Reader — akun Google Edition

Flask app untuk membaca mutasi rekening PDF/gambar, meninjau dan mengedit transaksi, lalu mengekspor Excel/CSV. Login menggunakan Google OAuth dan dibatasi ke akun Google `semua alamat Gmail/Google terverifikasi`.

## Fitur
- Login dengan akun akun Google kantor (domain dibatasi server-side).
- Upload PDF/JPG/PNG/TIFF maksimal 30 MB.
- OCR dan parsing mutasi bank yang didukung parser.
- Review & Edit tampil langsung di halaman hasil ekstraksi yang sama; tidak perlu membuka halaman baru.
- Ekspor hasil ke Excel dan CSV.
- Hasil tiap pekerjaan dikaitkan dengan email akun yang mengunggahnya.

## 1. Siapkan Google OAuth

1. Buka [Google Cloud Console](https://console.cloud.google.com/), pilih atau buat project yang dikelola organisasi kantor.
2. Buka **Google Auth Platform** (atau halaman OAuth consent screen pada antarmuka lama) dan konfigurasi branding/audience. Untuk penggunaan internal, pilih **Internal** jika opsi itu tersedia. Project perlu berada di organisasi akun Google yang benar; bila opsi Internal tidak tersedia, minta administrator Workspace/Cloud memeriksa organisasi dan izin project.
3. Buka **Clients / Credentials** lalu buat OAuth client dengan tipe **Web application**.
4. Tambahkan **Authorized redirect URI** persis:

   `https://mutasi-bank.onrender.com/auth/google/callback`

   Jika nama domain Render berubah, gunakan domain yang benar. URI ini harus sama persis dengan `GOOGLE_REDIRECT_URI` dan konfigurasi di Google Cloud.
5. Salin **Client ID** dan **Client Secret**. Jangan masukkan secret ke GitHub atau ke file kode.

Aplikasi ini hanya meminta scope `openid email profile`; aplikasi tidak meminta akses ke Gmail, Drive, atau data Google lainnya.

## 2. Atur Environment Variables di Render

Buka Render Dashboard → service aplikasi → **Environment**. Tambahkan:

- `SECRET_KEY` = string acak panjang dan rahasia (minimal 32 karakter; buat yang baru, jangan gunakan contoh publik).
- `GOOGLE_CLIENT_ID` = Client ID dari Google Cloud.
- `GOOGLE_CLIENT_SECRET` = Client Secret dari Google Cloud.
- `GOOGLE_WORKSPACE_DOMAIN` = `semua alamat Gmail/Google terverifikasi`
- `GOOGLE_REDIRECT_URI` = `https://mutasi-bank.onrender.com/auth/google/callback`

Simpan perubahan lalu lakukan **Manual Deploy → Deploy latest commit**. Jika Render otomatis deploy setelah perubahan Git, tunggu sampai status deploy berhasil.

## 3. Unggah kode ke GitHub

1. Ekstrak ZIP ini.
2. Unggah **isi folder `work_bank`** ke root repository GitHub yang terhubung ke Render, sehingga `app.py`, `requirements.txt`, `templates/`, `ocr_engine.py`, `transaction_parser.py`, `exporter.py`, dan `Dockerfile` berada di root repository (bukan di dalam folder tambahan `work_bank/work_bank`).
3. Pastikan `templates/index.html` dan `templates/login.html` ikut terunggah. `templates/review.html` masih disertakan sebagai template lama, tetapi alur utama Review & Edit sekarang berada di halaman index.
4. Commit/push perubahan, kemudian deploy di Render.

Runtime yang disarankan adalah **Docker**, menggunakan Dockerfile yang disertakan. Dockerfile memasang Tesseract OCR dan dependency Python.

## 4. Uji setelah deploy

1. Buka `https://mutasi-bank.onrender.com` dalam jendela incognito atau logout dari sesi lama.
2. Halaman harus menampilkan tombol **Sign in with Google**.
3. Login dengan akun `@semua alamat Gmail/Google terverifikasi` yang aktif.
4. Pastikan akun Google di luar domain tersebut ditolak meskipun pengguna mencoba memulai login dengan URL langsung.
5. Upload file uji, gulir ke bagian Review & Edit di halaman hasil yang sama, ubah satu nilai, klik Simpan Perubahan, lalu unduh ulang Excel/CSV.
6. Uji logout dan pastikan halaman utama meminta login kembali.

## Catatan keamanan dan operasional

- `hd` claim dari ID token Google diverifikasi dan email harus terverifikasi serta berakhiran `@semua alamat Gmail/Google terverifikasi`. Parameter `hd` pada permintaan login hanya petunjuk UX; pembatasan sebenarnya dilakukan di server.
- Agar opsi OAuth **Internal** tersedia, project Google Cloud harus terkait dengan organisasi Workspace yang mengelola domain kantor. Pembatasan domain di kode tetap diterapkan.
- Jangan menyimpan Client Secret atau `SECRET_KEY` di repository.
- Aplikasi menyimpan metadata pekerjaan/hasil di folder `jobs/` dan ekspor di `output/`. Pada hosting tanpa persistent disk, file lokal dapat hilang saat restart/redeploy. Untuk penggunaan rutin, konfigurasi persistent disk atau penyimpanan privat yang sesuai dan jadwalkan pembersihan file.
- Sebelum memproses rekening asli, lakukan security review dan pertimbangkan CSRF protection, pembatasan percobaan login, audit log, serta kebijakan retensi/penghapusan data. Batasi akses ke service dan environment variables hanya kepada admin yang berwenang.
- Kode sudah diperiksa sintaksnya, tetapi alur OAuth harus diuji setelah Client ID/Secret dan redirect URI kantor dikonfigurasi; saya tidak dapat menguji kredensial Google milik kantor dari sini.

## Jalankan lokal

Install dependency Python dan pastikan Tesseract OCR tersedia di sistem. Atur environment variables OAuth. Untuk pengujian lokal, tambahkan redirect URI `http://localhost:5000/auth/google/callback` di OAuth client, lalu set `GOOGLE_REDIRECT_URI` ke nilai itu. Jalankan:

```bash
python -m pip install -r requirements.txt
python app.py
```

Jangan gunakan HTTP callback lokal untuk deployment publik; Render harus memakai HTTPS.


### Status review
Kolom status review menggunakan dua pilihan: `Approve` dan `Perlu diperiksa`. Data parser lama yang bernilai `OK` akan ditampilkan sebagai `Approve`; status lainnya ditampilkan sebagai `Perlu diperiksa`.


### Preservasi nominal mutasi
Format tampilan nominal menggunakan pemisah ribuan Indonesia dan mempertahankan digit desimal dari nilai sumber. Hindari pembulatan nominal transaksi; tetap periksa hasil ekspor Excel/CSV dengan dokumen asli, terutama untuk nilai desimal berpresisi tinggi.


### Status review default
Status awal untuk transaksi yang belum memiliki status adalah `Perlu diperiksa`. Status `Approve` yang sudah dipilih tidak diubah.


### Review per transaksi
Status ditampilkan statis sebagai `Approved` atau `Need review`. Setiap baris memiliki aksi `Simpan` dan `Approve`. Saat `Approve`, transaksi dikunci dan kolom tidak dapat diedit lagi. Nominal diproses menggunakan Decimal di backend untuk mengurangi risiko pembulatan floating-point.


### Perbaikan simpan per baris
Input nominal yang diformat di tabel dikonversi ke angka standar sebelum submit, dan backend menerima format Indonesia seperti `1.234.567,89` tanpa pembulatan.


### Mode simpan per baris
Kolom status review dan aksi approve dihapus. Setiap transaksi hanya memiliki tombol `Simpan`. Setelah disimpan, transaksi tetap dapat diedit dan disimpan kembali.
\n\nCheckbox pemeriksaan: checkbox per baris mengunci input saat dicentang dan membuka kembali saat dilepas. Check all menandai seluruh transaksi. Format nominal menggunakan gaya US, misalnya 1,000,000.00.\n

### Autosave
Perubahan pada input transaksi otomatis disimpan setelah jeda singkat ketika mengetik, ketika input berubah/ditinggalkan, atau saat menekan Enter. Status penyimpanan ditampilkan per baris.


Nominal menerima `5000000`, `5,000,000`, `5,000,000.00`, serta `5.000.000,00`; tampilan dinormalisasi menjadi format US `5,000,000.00`.


Perbaikan Check all: setiap status checkbox disimpan melalui request berurutan untuk menghindari tabrakan penulisan file transaksi; kegagalan simpan akan membatalkan centang baris terkait.


Toolbar pemeriksaan menyediakan tombol `Check all` dan `Uncheck all`. Keduanya menyimpan status per baris secara berurutan.
