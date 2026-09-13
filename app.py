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


def get_supabase_client(data):
    return create_client(data["supabase_url"], data["supabase_key"])


def get_sheet_worksheet(data):
    creds_dict = json.loads(data["service_account_json"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(data["sheet_id"])
    try:
        ws = sh.worksheet(data["sheet_tab"])
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=data["sheet_tab"], rows=1000, cols=26)
    return ws


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/backup/sheet-to-supabase", methods=["POST"])
def sheet_to_supabase():
    data = request.json
    required = ["supabase_url", "supabase_key", "supabase_table", "unique_key", "sheet_id", "sheet_tab", "service_account_json"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Field belum diisi: {', '.join(missing)}"}), 400

    try:
        ws = get_sheet_worksheet(data)
        records = ws.get_all_records()
        if not records:
            return jsonify({"status": "ok", "message": "Sheet kosong, tidak ada data untuk dipindah."})

        key_col = data["unique_key"]
        clean_records = [r for r in records if str(r.get(key_col, "")).strip() != ""]

        sb = get_supabase_client(data)
        sb.table(data["supabase_table"]).upsert(clean_records, on_conflict=key_col).execute()

        return jsonify({
            "status": "ok",
            "message": f"Berhasil sinkron {len(clean_records)} baris dari Sheets ke Supabase.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/supabase-to-sheet", methods=["POST"])
def supabase_to_sheet():
    data = request.json
    required = ["supabase_url", "supabase_key", "supabase_table", "unique_key", "sheet_id", "sheet_tab", "service_account_json"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Field belum diisi: {', '.join(missing)}"}), 400

    try:
        sb = get_supabase_client(data)
        result = sb.table(data["supabase_table"]).select("*").execute()
        supabase_rows = result.data
        if not supabase_rows:
            return jsonify({"status": "ok", "message": "Tabel Supabase kosong, tidak ada data untuk dibackup."})

        key_col = data["unique_key"]
        ws = get_sheet_worksheet(data)
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
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
