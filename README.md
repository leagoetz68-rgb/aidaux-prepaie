# Aid'Aux Pré-paie

Application de suivi des éléments variables de paie, protégée par le
système de comptes partagé AID'Aux (voir `auth.py`).

## Déploiement (Vercel, comme tes autres applis)

Variables d'environnement à définir dans Vercel (Project Settings → Environment Variables) :

- `SECRET_KEY` — une chaîne aléatoire (clé de session Flask)
- `DATABASE_URL` (ou `AUTH_DATABASE_URL`) — la même base Neon que les
  autres applis, pour partager les comptes (retire les paramètres
  `sslmode`/`channel_binding` de l'URL si tu utilises pg8000 ailleurs,
  psycopg2 ici les accepte)
- `BREVO_API_KEY` — la clé Brevo déjà utilisée sur les autres applis
- `EXPEDITEUR_EMAIL` (optionnel) — par défaut `lea.goetz@aidaux.fr`
- `AUTH_APP_NOM` (optionnel) — nom affiché dans les emails et sur les
  pages de connexion, par défaut "AID'Aux" (mettre par ex.
  "AID'Aux Pré-paie")
- `AUTH_ALLOWED_EMAILS` (optionnel) — pour restreindre/étendre la liste
  des emails autorisés à créer un compte ; sinon la liste par défaut
  dans `auth.py` s'applique (Magali Metz, Magali Fux, Léa Goetz,
  Marie Mischel, Tatiana Suplon, Jennifer Soulliez)

## Première connexion

Sur `/mot-de-passe-oublie`, chacune renseigne son email professionnel
pour recevoir un lien et choisir son propre mot de passe. Si un compte
existe déjà sur une autre appli AID'Aux (même base de comptes), il
fonctionne directement ici aussi.
