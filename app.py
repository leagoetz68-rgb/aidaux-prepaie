import json
import os

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, session

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
            # Mois ajoutés au fur et à mesure depuis l'interface
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS prepaie_mois (
                    mois TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


init_sheets_db()


@app.route("/api/mois", methods=["GET"])
def api_get_mois():
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mois FROM prepaie_mois")
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"mois": [r[0] for r in rows]})


@app.route("/api/mois", methods=["POST"])
def api_add_mois():
    body = request.get_json(silent=True) or {}
    mois = (body.get("mois") or "").strip()
    if not mois:
        return jsonify({"ok": False}), 400
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO prepaie_mois (mois) VALUES (%s) ON CONFLICT DO NOTHING",
                (mois,),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


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


# (L'ancienne adresse /api/admin/reset-sheets, qui effaçait toutes les feuilles
# d'un simple clic, a été retirée.)


# ---- Ximi : diagnostic du planning (étape 1 des tickets restaurant) ----
# Cherche dans la documentation de l'API Ximi les adresses qui donnent le
# planning d'un intervenant (interventions, trajets, absences, formations...)
# et montre à quoi ressemblent les données pour un intervenant et un jour.
@app.route("/admin/ximi-planning")
def ximi_planning_diagnostic():
    """Diagnostic n°2 : lit les interventions d'un jour dans Ximi et montre
    leur structure (noms de champs, horaires, statuts), sans données personnelles."""
    from concurrent.futures import ThreadPoolExecutor
    import requests as rq
    import ximi

    nom = (request.args.get("intervenant") or "").strip().lower()
    prenom = (request.args.get("prenom") or "").strip().lower()
    jour = (request.args.get("date") or "").strip()  # aaaa-mm-jj
    res = {"aide": "?intervenant=AMIRI&prenom=Saeideh&date=2026-10-20", "date": jour}
    try:
        entetes = ximi._headers()
    except Exception as e:
        return jsonify({"erreur": "Clés Ximi absentes ou invalides : " + str(e)[:300]})
    base = ximi.BASE_URL
    jeton = entetes["Api-Key"]
    DELAI = 8

    # Intervenant : nom exact (+ prénom)
    agent_id = None
    try:
        for a in ximi.get_all("api/agents"):
            if str(a.get("LastName") or "").strip().lower() == nom and \
                    (not prenom or str(a.get("FirstName") or "").strip().lower() == prenom):
                agent_id = a.get("Id")
                res["intervenant"] = f"{a.get('LastName')} {a.get('FirstName')} (Id {agent_id})"
                break
    except Exception as e:
        res["intervenant_erreur"] = str(e)[:300]

    PERSO = ("name", "phone", "email", "address", "street", "city", "zip", "social", "birth",
             "comment", "note", "data", "label", "title", "latitude", "longitude")

    def masquer(v, cle=""):
        """Garde la structure, les dates, nombres, statuts ; masque les textes personnels."""
        if isinstance(v, dict):
            return {k: masquer(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [masquer(x, cle) for x in v[:3]]
        if isinstance(v, str) and any(m in cle.lower() for m in PERSO) and "type" not in cle.lower():
            return "…" if v else v
        return v

    def contient(v, valeur):
        if isinstance(v, dict):
            return any(contient(x, valeur) for x in v.values())
        if isinstance(v, list):
            return any(contient(x, valeur) for x in v)
        return v == valeur

    debut, fin = jour + "T00:00:00", jour + "T23:59:59"
    essais = {
        "interventions/all filter.start": ("api/interventions/all",
            {"filter.start": debut, "filter.end": fin, "Top": 1000}, {}),
        "interventions/all Filter.Start": ("api/interventions/all",
            {"Filter.Start": debut, "Filter.End": fin, "Top": 1000}, {}),
        "interventions/all request.filter.start": ("api/interventions/all",
            {"request.filter.start": debut, "request.filter.end": fin, "request.top": 1000}, {}),
        "interventions Bearer": ("api/interventions",
            {"startDate": debut, "endDate": fin, "Top": 1000}, {"Authorization": "Bearer " + jeton}),
        "interventions brut": ("api/interventions",
            {"startDate": debut, "endDate": fin, "Top": 1000}, {"Authorization": jeton}),
        "agents/events Bearer": ("api/agents/events",
            {"StartDate": debut, "EndDate": fin, "Top": 1000}, {"Authorization": "Bearer " + jeton}),
        "agentAbsences Bearer": ("api/agentAbsences",
            {"startDate": debut, "endDate": fin, "Top": 1000}, {"Authorization": "Bearer " + jeton}),
    }

    def essayer(item):
        nom_essai, (chemin, params, extra) = item
        try:
            r = rq.get(f"{base}/{chemin}", headers={**entetes, **extra}, params=params, timeout=DELAI)
            e = {"code": r.status_code}
            try:
                j = r.json()
            except ValueError:
                e["reponse"] = r.text[:200]
                return nom_essai, e
            lignes = j.get("Results", j) if isinstance(j, dict) else j
            if not isinstance(lignes, list):
                e["reponse"] = str(j)[:300]
                return nom_essai, e
            e["nb"] = len(lignes)
            if lignes:
                e["champs"] = sorted(lignes[0].keys()) if isinstance(lignes[0], dict) else []
            a_lui = [l for l in lignes if agent_id and contient(l, agent_id)]
            e["nb_pour_intervenant"] = len(a_lui)
            e["exemples_intervenant"] = [masquer(l) for l in a_lui[:8]]
            if not a_lui:
                e["exemple_quelconque"] = masquer(lignes[0]) if lignes else None
            return nom_essai, e
        except Exception as ex:
            return nom_essai, {"erreur": str(ex)[:200]}

    with ThreadPoolExecutor(max_workers=7) as ex:
        res["essais"] = dict(ex.map(essayer, essais.items()))
    return Response(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                    mimetype="application/json")


# --- Application installable (écran d'accueil iPhone / Android) ---
MANIFEST = {
    "name": "Aid'Aux Pré-paie",
    "short_name": "Aid'Aux Pré-paie",
    "description": "Pré-paie AID'Aux",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#ffffff",
    "theme_color": "#1B2A6B",
    "lang": "fr",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Service worker minimal : sans cache, tout passe par le réseau (données toujours à jour).
SERVICE_WORKER = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


@app.route("/manifest.webmanifest")
def manifest():
    return Response(json.dumps(MANIFEST, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    return Response(SERVICE_WORKER, mimetype="application/javascript", headers={"Cache-Control": "no-cache"})


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
