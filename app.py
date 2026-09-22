import os

from flask import Flask, render_template, send_from_directory, session

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
