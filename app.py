import json
import os

from flask import Flask, jsonify, render_template, request, send_from_directory, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-moi-en-prod")

# Système de comptes partagé (login individuel par email + mot de passe,
# lien "définir mon mot de passe" envoyé par email). Voir auth.py — copié
# tel quel depuis les autres applis AID'Aux, base de comptes commune via
# AUTH_DATABASE_URL (ou DATABASE_URL).
import auth  # noqa: E402

auth.init_auth_db()
app.register_blueprint(auth.bp)
auth.proteger(app)


# ---- Stockage partagé du tableau (une ligne par agence + mois) ----
# Remplace l'ancien localStorage : toutes les personnes connectées voient
# et modifient les mêmes données, stockées dans la même base Neon que les
# comptes.
def init_sheets_db():
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS prepaie_sheets (
                    agence TEXT NOT NULL,
                    mois TEXT NOT NULL,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (agence, mois)
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


init_sheets_db()


@app.route("/api/sheet", methods=["GET"])
def api_get_sheet():
    agence = request.args.get("agence", "")
    mois = request.args.get("mois", "")
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM prepaie_sheets WHERE agence = %s AND mois = %s",
                (agence, mois),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({"rows": None}), 404
    return jsonify({"rows": row[0]})


@app.route("/api/sheet", methods=["PUT", "POST"])
def api_put_sheet():
    agence = request.args.get("agence", "")
    mois = request.args.get("mois", "")
    body = request.get_json(silent=True) or {}
    rows = body.get("rows", [])
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prepaie_sheets (agence, mois, data, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (agence, mois)
                DO UPDATE SET data = EXCLUDED.data, updated_at = now()
                """,
                (agence, mois, json.dumps(rows)),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/reset-sheets")
def api_reset_sheets():
    # Remise à zéro ponctuelle : supprime toutes les feuilles enregistrées
    # (utilisé une seule fois pour repartir sur une base saine après un bug
    # de test). À retirer une fois utilisé.
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prepaie_sheets")
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "supprimees": n})


@app.route("/")
def index():
    return render_template("index.html", user_email=session.get("user_email", ""))


# favicon.ico et logo.png restent à la racine du repo (pas besoin de les
# déplacer dans un dossier static/) : on les sert explicitement d'ici.
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.root_path, "favicon.ico")


@app.route("/logo.png")
def logo():
    return send_from_directory(app.root_path, "logo.png")


if __name__ == "__main__":
    app.run(debug=True)
