import os
import json
from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Supabase "utama" — dipakai KHUSUS buat nyimpen daftar profile/kredensial
# (bukan tempat data user kamu). Diambil dari Environment Variables di Vercel,
# bukan dari form, biar tidak pernah muncul ke browser.
MASTER_SUPABASE_URL = os.environ.get("MASTER_SUPABASE_URL")
MASTER_SUPABASE_KEY = os.environ.get("MASTER_SUPABASE_KEY")


def get_master_client():
    if not MASTER_SUPABASE_URL or not MASTER_SUPABASE_KEY:
        raise RuntimeError(
            "MASTER_SUPABASE_URL / MASTER_SUPABASE_KEY belum diset di Environment Variables Vercel."
        )
    return create_client(MASTER_SUPABASE_URL, MASTER_SUPABASE_KEY)


# ---------- Helper: koneksi ke Supabase (project tujuan backup) & Google Sheets ----------

def get_supabase_client(profile):
    return create_client(profile["supabase_url"], profile["supabase_key"])


def get_sheet_worksheet(profile):
    creds_dict = json.loads(profile["service_account_json"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(profile["sheet_id"])
    try:
        ws = sh.worksheet(profile["sheet_tab"])
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=profile["sheet_tab"], rows=1000, cols=26)
    return ws


# ---------- Routes: halaman utama ----------

@app.route("/")
def index():
    return render_template("index.html")


# ---------- Routes: CRUD profil (disimpan di tabel backup_profiles di Supabase utama) ----------

@app.route("/api/profiles", methods=["GET"])
def list_profiles():
    try:
        master = get_master_client()
        res = (
            master.table("backup_profiles")
            .select("id,name,supabase_url,supabase_table,unique_key,sheet_id,sheet_tab")
            .execute()
        )
        return jsonify(res.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles", methods=["POST"])
def create_profile():
    data = request.json
    required = [
        "name", "supabase_url", "supabase_key", "supabase_table",
        "unique_key", "sheet_id", "sheet_tab", "service_account_json",
    ]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Field belum diisi: {', '.join(missing)}"}), 400

    try:
        master = get_master_client()
        master.table("backup_profiles").insert(data).execute()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<int:profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    try:
        master = get_master_client()
        master.table("backup_profiles").delete().eq("id", profile_id).execute()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_profile_or_404(profile_id):
    master = get_master_client()
    res = master.table("backup_profiles").select("*").eq("id", profile_id).execute()
    return res.data[0] if res.data else None


# ---------- Routes: proses backup ----------

@app.route("/api/backup/sheet-to-supabase/<int:profile_id>", methods=["POST"])
def sheet_to_supabase(profile_id):
    try:
        profile = get_profile_or_404(profile_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not profile:
        return jsonify({"error": "Profil tidak ditemukan"}), 404

    try:
        ws = get_sheet_worksheet(profile)
        records = ws.get_all_records()
        if not records:
            return jsonify({"status": "ok", "message": "Sheet kosong, tidak ada data untuk dipindah.", "added_or_updated": 0})

        key_col = profile["unique_key"]
        clean_records = [r for r in records if str(r.get(key_col, "")).strip() != ""]

        sb = get_supabase_client(profile)
        sb.table(profile["supabase_table"]).upsert(clean_records, on_conflict=key_col).execute()

        return jsonify({
            "status": "ok",
            "message": f"Berhasil sinkron {len(clean_records)} baris dari Sheets ke Supabase.",
            "added_or_updated": len(clean_records),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/supabase-to-sheet/<int:profile_id>", methods=["POST"])
def supabase_to_sheet(profile_id):
    try:
        profile = get_profile_or_404(profile_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not profile:
        return jsonify({"error": "Profil tidak ditemukan"}), 404

    try:
        sb = get_supabase_client(profile)
        result = sb.table(profile["supabase_table"]).select("*").execute()
        supabase_rows = result.data
        if not supabase_rows:
            return jsonify({"status": "ok", "message": "Tabel Supabase kosong, tidak ada data untuk dibackup.", "added": 0, "updated": 0})

        key_col = profile["unique_key"]
        ws = get_sheet_worksheet(profile)
        existing_records = ws.get_all_records()
        headers = ws.row_values(1)

        if not headers:
            headers = list(supabase_rows[0].keys())
            ws.append_row(headers)
            existing_records = []

        existing_map = {}
        for idx, rec in enumerate(existing_records):
            k = str(rec.get(key_col, ""))
            if k != "":
                existing_map[k] = idx + 2

        updated = 0
        new_rows = []
        for row in supabase_rows:
            k = str(row.get(key_col, ""))
            row_values = [str(row.get(h, "")) for h in headers]
            if k in existing_map:
                ws.update(f"A{existing_map[k]}", [row_values])
                updated += 1
            else:
                new_rows.append(row_values)

        if new_rows:
            ws.append_rows(new_rows)

        return jsonify({
            "status": "ok",
            "message": f"Backup selesai. {updated} baris diupdate, {len(new_rows)} baris baru ditambahkan.",
            "added": len(new_rows),
            "updated": updated,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Vercel Python runtime otomatis mendeteksi variabel "app" ini sebagai WSGI app.
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
