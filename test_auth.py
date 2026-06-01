from flask import Flask, render_template, jsonify, request, redirect, url_for, session
import sqlite3
import datetime
import hashlib
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)
DB_PATH = "nova_tickets.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def init_auth_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS admins 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
         username TEXT UNIQUE, 
         password TEXT,
         created_at TEXT)""")
    # أدمن افتراضي: admin / admin123
    try:
        db.execute("INSERT INTO admins (username, password, created_at) VALUES (?, ?, ?)",
            ("admin", hash_password("admin123"), datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    except:
        pass
    db.commit()
    db.close()

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "admin" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        db = get_db()
        row = db.execute("SELECT * FROM admins WHERE username=? AND password=?",
            (username, hash_password(password))).fetchone()
        db.close()
        if row:
            session["admin"] = username
            return redirect(url_for("index"))
        error = "يوزر أو باسورد غلط"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/stats")
@login_required
def stats():
    db = get_db()
    try:
        total = db.execute("SELECT count FROM ticket_count WHERE id = 1").fetchone()
        total_tickets = total["count"] if total else 0
        staff_rows = db.execute("SELECT staff_id, claimed, closed FROM staff_stats ORDER BY closed DESC").fetchall()
        staff_list = [{"staff_id": str(r["staff_id"]), "claimed": r["claimed"], "closed": r["closed"]} for r in staff_rows]
        blacklist_count = db.execute("SELECT COUNT(*) as cnt FROM blacklist").fetchone()["cnt"]
        notes = []
        try:
            notes_rows = db.execute("SELECT staff_id, ticket_name, note, created_at FROM staff_notes ORDER BY created_at DESC LIMIT 20").fetchall()
            notes = [{"staff_id": str(r["staff_id"]), "ticket_name": r["ticket_name"], "note": r["note"], "created_at": r["created_at"]} for r in notes_rows]
        except:
            pass
        return jsonify({
            "total_tickets": total_tickets,
            "staff": staff_list,
            "blacklist_count": blacklist_count,
            "notes": notes,
            "last_updated": datetime.datetime.now().strftime("%H:%M:%S")
        })
    finally:
        db.close()

@app.route("/api/admins")
@login_required
def get_admins():
    db = get_db()
    rows = db.execute("SELECT id, username, created_at FROM admins").fetchall()
    db.close()
    return jsonify([{"id": r["id"], "username": r["username"], "created_at": r["created_at"]} for r in rows])

@app.route("/api/admins/add", methods=["POST"])
@login_required
def add_admin():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"success": False, "message": "يوزر أو باسورد فاضي"})
    db = get_db()
    try:
        db.execute("INSERT INTO admins (username, password, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
        db.commit()
        return jsonify({"success": True})
    except:
        return jsonify({"success": False, "message": "اليوزر موجود مسبقاً"})
    finally:
        db.close()

@app.route("/api/admins/delete/<int:admin_id>", methods=["DELETE"])
@login_required
def delete_admin(admin_id):
    db = get_db()
    # لا تحذف الأدمن الوحيد
    count = db.execute("SELECT COUNT(*) as cnt FROM admins").fetchone()["cnt"]
    if count <= 1:
        db.close()
        return jsonify({"success": False, "message": "لا يمكن حذف الأدمن الوحيد"})
    db.execute("DELETE FROM admins WHERE id=?", (admin_id,))
    db.commit()
    db.close()
    return jsonify({"success": True})

if __name__ == "__main__":
    init_auth_db()
    print("🌐 الداشبورد يشتغل على: http://localhost:5000")
    app.run(debug=True, port=5000)
