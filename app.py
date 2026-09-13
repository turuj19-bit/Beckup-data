import os
import json
import sqlite3
from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "profiles.db")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ---------- Database (penyimpanan profil kredensial) ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            supabase_url TEXT NOT NULL,
            supabase_key TEXT NOT NULL,
            supabase_table TEXT NOT NULL,
            unique_key TEXT NOT NULL DEFAULT 'id',
            sheet_id TEXT NOT NULL,
            sheet_tab TEXT NOT NULL,
            service_account_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- Helper: koneksi ke Supabase & Google Sheets ----------

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


# ---------- Routes: CRUD profil ----------

@app.route("/api/profiles", methods=["GET"])
def list_profiles():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, supabase_url, supabase_table, unique_key, sheet_id, sheet_tab FROM profiles"
    ).fetchall()
    conn.close()
    # Catatan: key/credential sensitif TIDAK dikirim balik ke frontend demi keamanan
    return jsonify([dict(r) for r in rows])


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

    conn = get_db()
    conn.execute(
        """INSERT INTO profiles
           (name, supabase_url, supabase_key, supabase_table, unique_key, sheet_id, sheet_tab, service_account_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"], data["supabase_url"], data["supabase_key"], data["supabase_table"],
            data["unique_key"], data["sheet_id"], data["sheet_tab"], data["service_account_json"],
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/profiles/<int:profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    conn = get_db()
    conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


def get_profile_or_404(profile_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- Routes: proses backup ----------

@app.route("/api/backup/sheet-to-supabase/<int:profile_id>", methods=["POST"])
def sheet_to_supabase(profile_id):
    profile = get_profile_or_404(profile_id)
    if not profile:
        return jsonify({"error": "Profil tidak ditemukan"}), 404

    try:
        ws = get_sheet_worksheet(profile)
        records = ws.get_all_records()  # list of dict, header row jadi key
        if not records:
            return jsonify({"status": "ok", "message": "Sheet kosong, tidak ada data untuk dipindah.", "added_or_updated": 0})

        key_col = profile["unique_key"]
        # Buang baris yang tidak punya nilai di kolom kunci
        clean_records = [r for r in records if str(r.get(key_col, "")).strip() != ""]

        sb = get_supabase_client(profile)
        # upsert otomatis: kalau id sudah ada -> update, kalau belum -> insert baru
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
    profile = get_profile_or_404(profile_id)
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

        # Kalau sheet masih benar-benar kosong (belum ada header), buat header dari data supabase
        if not headers:
            headers = list(supabase_rows[0].keys())
            ws.append_row(headers)
            existing_records = []

        # Map baris sheet yang sudah ada, berdasarkan kolom kunci -> nomor baris (row 1 = header)
        existing_map = {}
        for idx, rec in enumerate(existing_records):
            k = str(rec.get(key_col, ""))
            if k != "":
                existing_map[k] = idx + 2  # +2 karena row 1 = header, index mulai dari 0

        updated = 0
        new_rows = []
        for row in supabase_rows:
            k = str(row.get(key_col, ""))
            row_values = [str(row.get(h, "")) for h in headers]
            if k in existing_map:
                # update baris yang sudah ada (biar tidak dobel)
                cell_range = f"A{existing_map[k]}:{gspread.utils.rowcol_to_a1(1, len(headers))[0:]}{existing_map[k]}"
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


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
