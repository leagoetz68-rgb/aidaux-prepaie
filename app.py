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


# ---- Ximi : tickets restaurant calculés depuis le planning ----
# L'API Ximi (api/interventions/all) ne sait pas filtrer par date : comme dans
# Aid'Aux Suivi, on la lit page par page et on garde en base les interventions
# récentes (table prepaie_ximi_interventions). Chaque appel de
# /api/ximi/rafraichir lit des pages pendant quelques secondes et reprend là où
# le précédent s'est arrêté (Vercel coupe les requêtes trop longues).
#
# Règle des tickets (définie avec Léa) : par jour, on additionne
#   - les prestations faites ou prévues (pas les annulées, statut -3),
#   - les intervacations = le temps entre la fin d'une prestation et le début
#     de la suivante, si ce trou ne dépasse pas la limite réglable (sinon c'est
#     une coupure / un retour au domicile, qui ne compte pas).
# 6 h ou plus dans la journée = 1 ticket. Les formations ne sont pas lisibles
# par l'API (accès refusé) : elles restent à ajouter à la main.
XIMI_STATUT_ANNULE = -3
XIMI_JOURS_AVANT = 100   # on garde ~3 mois en arrière...
XIMI_JOURS_APRES = 62    # ... et 2 mois en avant (prestations prévues)
SEUIL_TICKET_MIN = 6 * 60
MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]


def init_ximi_db():
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS prepaie_ximi_interventions (
                    id BIGINT PRIMARY KEY,
                    agent_id BIGINT,
                    agent_nom TEXT,
                    debut TIMESTAMP,
                    fin TIMESTAMP,
                    statut INTEGER,
                    vu_le TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS prepaie_ximi_etat (cle TEXT PRIMARY KEY, valeur TEXT)"
            )
        conn.commit()
    finally:
        conn.close()


init_ximi_db()


def _etat(cur, cle, defaut=None):
    cur.execute("SELECT valeur FROM prepaie_ximi_etat WHERE cle = %s", (cle,))
    row = cur.fetchone()
    return row[0] if row else defaut


def _set_etat(cur, cle, valeur):
    cur.execute(
        """INSERT INTO prepaie_ximi_etat (cle, valeur) VALUES (%s, %s)
           ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur""",
        (cle, str(valeur)),
    )


def _heure_paris():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Paris"))


def _dt_ximi(texte):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(texte or "")[:19])
    except ValueError:
        return None


@app.route("/api/ximi/rafraichir", methods=["POST"])
def api_ximi_rafraichir():
    """Lit des pages d'interventions Ximi pendant ~7 s. À rappeler tant que
    « termine » vaut false. Si un tour complet date de moins de 20 min, rien à
    relire (sauf ?force=1)."""
    import time
    from datetime import datetime, timedelta
    from psycopg2.extras import execute_values
    import ximi

    t0 = time.time()
    offset = 0
    maintenant = datetime.now()
    bas = maintenant - timedelta(days=XIMI_JOURS_AVANT)
    haut = maintenant + timedelta(days=XIMI_JOURS_APRES)
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            offset = int(_etat(cur, "offset", 0))
            fini_le = _dt_ximi(_etat(cur, "dernier_tour_iso"))
            if offset == 0 and fini_le and not request.args.get("force") \
                    and maintenant - fini_le < timedelta(minutes=20):
                return jsonify({"ok": True, "termine": True, "lues": 0,
                                "dernier_tour": _etat(cur, "dernier_tour")})
            if offset == 0:
                _set_etat(cur, "debut_tour", maintenant.isoformat())
            termine, pages = False, 0
            # Vercel coupe une requête au bout de ~10 s : on ne commence pas de
            # nouvelle page après 4 s, et une page trop lente est abandonnée.
            while time.time() - t0 < 4:
                res = ximi.get("api/interventions/all", {"Top": 1000, "Offset": offset,
                                                          "ComputeHasMoreRows": "true"}, timeout=5)
                items = res.get("Results", []) if isinstance(res, dict) else []
                pages += 1
                lignes = []
                for it in items:
                    debut, fin = _dt_ximi(it.get("Start")), _dt_ximi(it.get("End"))
                    if not debut or not fin or not (bas <= debut <= haut):
                        continue
                    agent = it.get("Agent") or {}
                    lignes.append((it.get("Id"), agent.get("Id"),
                                   (agent.get("DisplayName") or "").strip(),
                                   debut, fin, it.get("Status")))
                if lignes:
                    execute_values(cur, """
                        INSERT INTO prepaie_ximi_interventions
                            (id, agent_id, agent_nom, debut, fin, statut)
                        VALUES %s
                        ON CONFLICT (id) DO UPDATE SET
                            agent_id = EXCLUDED.agent_id, agent_nom = EXCLUDED.agent_nom,
                            debut = EXCLUDED.debut, fin = EXCLUDED.fin,
                            statut = EXCLUDED.statut, vu_le = now()
                    """, lignes)
                offset += len(items)
                if not res.get("HasMoreRows") or not items:
                    termine = True
                    break
            if termine:
                # tour complet : on oublie ce qui n'existe plus dans Ximi et ce qui est trop vieux
                cur.execute("DELETE FROM prepaie_ximi_interventions WHERE vu_le < %s",
                            (_etat(cur, "debut_tour", maintenant.isoformat()),))
                cur.execute("DELETE FROM prepaie_ximi_interventions WHERE debut < %s", (bas,))
                _set_etat(cur, "dernier_tour", _heure_paris().strftime("%d/%m/%Y %H:%M"))
                _set_etat(cur, "dernier_tour_iso", maintenant.isoformat())
                _set_etat(cur, "offset", 0)
            else:
                _set_etat(cur, "offset", offset)
            dernier = _etat(cur, "dernier_tour")
        conn.commit()
    except Exception as e:
        # page Ximi trop lente ou refusée : on garde ce qui est lu, le navigateur réessaie
        with conn.cursor() as cur:
            _set_etat(cur, "offset", offset)
        conn.commit()
        return jsonify({"ok": False, "erreur": str(e)[:400], "lues": offset}), 502
    finally:
        conn.close()
    return jsonify({"ok": True, "termine": termine, "lues": offset, "dernier_tour": dernier})


def _cle_nom(nom):
    """'AMIRI, Saeideh' / 'Amiri Saeïdeh' -> 'amiri saeideh' (mots triés)."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", str(nom or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(sorted(re.findall(r"[a-z]+", t)))


def _mois_bornes(mois):
    """'Octobre 2026' -> (datetime 1er octobre, datetime 1er novembre)."""
    from datetime import datetime
    parties = (mois or "").strip().lower().split()
    if len(parties) != 2 or parties[0] not in MOIS_FR or not parties[1].isdigit():
        return None
    m, a = MOIS_FR.index(parties[0]) + 1, int(parties[1])
    return datetime(a, m, 1), datetime(a + (m == 12), m % 12 + 1, 1)


def _minutes_du_jour(creneaux, limite_min):
    """Prestations + intervacations (trous <= limite) d'une journée, en minutes."""
    creneaux = sorted(creneaux)
    fusion = []
    for d, f in creneaux:  # deux prestations qui se chevauchent ne comptent qu'une fois
        if fusion and d <= fusion[-1][1]:
            fusion[-1][1] = max(fusion[-1][1], f)
        else:
            fusion.append([d, f])
    total = sum((f - d).total_seconds() / 60 for d, f in fusion)
    for (_, f1), (d2, _) in zip(fusion, fusion[1:]):
        trou = (d2 - f1).total_seconds() / 60
        if 0 < trou <= limite_min:
            total += trou
    return round(total)


@app.route("/api/ximi/tickets")
def api_ximi_tickets():
    """Tickets proposés pour un mois : {cle_nom: {nom, tickets, jours: [...]}}."""
    bornes = _mois_bornes(request.args.get("mois"))
    if not bornes:
        return jsonify({"ok": False, "erreur": "Mois illisible"}), 400
    try:
        limite = max(0, min(600, int(request.args.get("limite", 120))))
    except ValueError:
        limite = 120
    conn = auth._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT agent_nom, debut, fin FROM prepaie_ximi_interventions
                   WHERE debut >= %s AND debut < %s AND statut IS DISTINCT FROM %s
                     AND agent_nom <> ''""",
                (bornes[0], bornes[1], XIMI_STATUT_ANNULE),
            )
            rows = cur.fetchall()
            dernier = _etat(cur, "dernier_tour")
    finally:
        conn.close()
    par_agent = {}
    for nom, debut, fin in rows:
        if fin <= debut:
            continue
        a = par_agent.setdefault(_cle_nom(nom), {"nom": nom, "jours": {}})
        a["jours"].setdefault(debut.date().isoformat(), []).append((debut, fin))
    resultat = {}
    for cle, a in par_agent.items():
        jours = []
        for jour, creneaux in sorted(a["jours"].items()):
            minutes = _minutes_du_jour(creneaux, limite)
            jours.append({"date": jour, "minutes": minutes, "ticket": minutes >= SEUIL_TICKET_MIN})
        resultat[cle] = {"nom": a["nom"], "tickets": sum(j["ticket"] for j in jours), "jours": jours}
    return jsonify({"ok": True, "agents": resultat, "dernier_tour": dernier, "limite": limite})


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
