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
MOTS_PLANNING = ("intervention", "event", "planning", "schedule", "absence", "unavail",
                 "indispo", "training", "formation", "travel", "trajet", "agent", "employee",
                 "appointment", "mission", "activity")


@app.route("/admin/ximi-planning")
def ximi_planning_diagnostic():
    import re
    from concurrent.futures import ThreadPoolExecutor
    import requests as rq
    import ximi

    nom = (request.args.get("intervenant") or "").strip()
    jour = (request.args.get("date") or "").strip()  # aaaa-mm-jj
    res = {"aide": "Ajouter ?intervenant=NOM&date=2026-10-20 à l'adresse",
           "intervenant": nom, "date": jour}
    try:
        entetes = ximi._headers()
    except Exception as e:
        return jsonify({"erreur": "Clés Ximi absentes ou invalides : " + str(e)[:300]})
    base = ximi.BASE_URL
    DELAI = 7

    # 1) Documentation Swagger : adresses en lien avec le planning
    spec = None
    for doc in ("swagger/docs/v1", "swagger/docs/V1", "swagger/docs/v2", "swagger/v1/swagger.json"):
        try:
            r = rq.get(f"{base}/{doc}", headers=entetes, timeout=DELAI)
            if r.status_code < 400 and r.text.lstrip().startswith("{"):
                spec = r.json()
                res["swagger"] = doc
                break
        except Exception:
            continue
    chemins = {}
    for chemin, ops in ((spec or {}).get("paths") or {}).items():
        if not any(m in chemin.lower() for m in MOTS_PLANNING):
            continue
        for verbe, op in ops.items():
            if verbe.lower() != "get" or not isinstance(op, dict):
                continue
            chemins[chemin] = [p.get("name") for p in op.get("parameters") or [] if isinstance(p, dict)]
    res["adresses_planning"] = chemins or "documentation illisible"

    # 2) Intervenant
    agent_id = None
    if nom:
        try:
            agents = ximi.get_all("api/agents")
            cle = nom.lower().split()[0]
            trouves = [a for a in agents if cle in (str(a.get("LastName") or a.get("Name") or "")
                                                     + " " + str((a.get("Contact") or {}).get("LastName") or "")).lower()]
            res["intervenants_trouves"] = [{"Id": a.get("Id"), "Nom": a.get("LastName") or (a.get("Contact") or {}).get("LastName"),
                                           "Prenom": a.get("FirstName") or (a.get("Contact") or {}).get("FirstName")}
                                          for a in trouves[:10]]
            res["champs_intervenant"] = sorted(agents[0].keys()) if agents else []
            if trouves:
                agent_id = trouves[0].get("Id")
        except Exception as e:
            res["intervenants_erreur"] = str(e)[:300]

    # 3) Essai de chaque adresse de liste pour cet intervenant et ce jour
    if agent_id and jour:
        def essayer(chemin):
            params_doc = chemins.get(chemin) or []
            p = {"Top": 50}
            for n in params_doc:
                ln = (n or "").lower()
                if "agent" in ln or "employee" in ln:
                    p[n] = agent_id
                elif any(m in ln for m in ("start", "from", "begin", "debut", "min")):
                    p[n] = jour + "T00:00:00"
                elif any(m in ln for m in ("end", "to", "fin", "max")) and "top" != ln:
                    p[n] = jour + "T23:59:59"
            url = base.replace("/Ximi3", "") + chemin if chemin.startswith("/Ximi3") else f"{base}/{chemin.lstrip('/')}"
            try:
                r = rq.get(url, headers=entetes, params=p, timeout=DELAI)
                entree = {"code": r.status_code, "parametres": p}
                try:
                    j = r.json()
                    lignes = j.get("Results", j) if isinstance(j, dict) else j
                    if isinstance(lignes, list):
                        entree["nb"] = len(lignes)
                        entree["exemples"] = lignes[:3]
                    else:
                        entree["reponse"] = str(j)[:400]
                except ValueError:
                    entree["reponse"] = r.text[:200]
                return chemin, entree
            except Exception as e:
                return chemin, {"erreur": str(e)[:150]}
        a_tester = [c for c in chemins if "{" not in c][:25]
        with ThreadPoolExecutor(max_workers=10) as ex:
            res["essais"] = dict(ex.map(essayer, a_tester))
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
