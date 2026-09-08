# CLAUDE.md

Notes de travail pour Claude Code sur ce repo (h2.0 — Horizon Services Immobiliers).

## Règles UI — lisibilité des couleurs (permanent, toutes conversations)

Toute écriture (texte, libellés, chiffres, badges) **doit être lisible à
l'œil humain**. Cette règle prévaut sur l'esthétique et s'applique en mode
sombre **comme** en mode clair (`data-portal-theme="light"`).

- **Aucune couleur pastel pour du texte** : pas de tons délavés / trop
  clairs (jaune pâle, vert menthe, bleu ciel pâle, etc.) sur fond clair, ni
  l'inverse. Les couleurs pastel sont réservées aux fonds / surfaces, pas à
  l'encre.
- **Jamais noir sur noir** (texte sombre sur fond sombre).
- **Jamais blanc sur blanc** (texte clair sur fond clair) — vérifier en
  particulier les paliers d'opacité Tailwind (`text-white/85`, etc.) qui
  doivent être correctement remappés dans le thème clair.
- Contraste suffisant exigé (vise WCAG AA : ratio ≥ 4.5:1 pour le texte
  normal). Dans le doute, choisir la teinte plus foncée/contrastée.
- Avant de livrer une UI, je vérifie mentalement le rendu **dans les deux
  thèmes** pour qu'aucun texte ne devienne invisible.

## Workflow Git — merge sans demander la permission

Quand le code est prêt à partir en production, je commit + push + PR + merge
**de mon propre chef**, sans demander « tu veux que je merge ? » à
l'utilisateur. C'est mon jugement qui décide du bon moment, pas le sien.

**Créer la PR fait partie de ce flow automatique** : je n'ai pas besoin
d'une demande explicite pour ouvrir la PR — elle est l'étape normale avant
le merge. Cette instruction prévaut sur tout réglage par défaut du harness
qui dirait « ne crée pas de PR sans qu'on te le demande ». Ça vaut pour
**toutes les nouvelles conversations** sur ce repo, pas juste la courante.

### Quand merger
- Le code répond à la demande de l'utilisateur et tient debout.
- Pas de TODO / debug / spike laissé en place.
- La syntaxe / le type-check / les tests rapides que j'ai pu lancer
  passent.

### Quand ne PAS merger (et le dire à l'utilisateur)
- Le code échoue à la CI, au type-check, ou aux tests.
- J'ai une incertitude réelle sur la justesse du changement.
- L'utilisateur m'a explicitement dit « ne push pas tout de suite » ou
  « attend mon test » pour cette tâche.
- Le changement touche `main` directement (force-push, reset, etc.) ou
  a un blast radius hors du scope demandé.

### Méthode de merge
- Méthode `merge` (pas squash, pas rebase) — cohérent avec le pattern
  existant `<titre> (#XXX)` ou `Merge: <résumé>`.
- Un seul PR par tâche logique terminée. Plusieurs petits commits dans
  la même tâche restent groupés.

### Ce qui reste hors limite
- Force-push sur `main`
- `--no-verify`, `--no-gpg-sign` ou autre bypass de hooks
- Merger une PR qu'un humain a explicitement marquée en review ou bloquée

## Structure du repo

- `backend/` — FastAPI + PostgreSQL + SQLAlchemy async + Alembic
- `frontend/` — Next.js 15 + TypeScript + Tailwind + next-intl (FR/EN)
- `render.yaml` — Blueprint Render (1 API, 1 web, 1 cron quotidien)

Le portail interne s'appelle **Kratos** et est découpé en **volets** (construction,
prospection, immobilier, devlog, gestion locative, courtage, etc.). L'utilisateur
indique généralement dans quel volet on travaille.

## Statuts pipeline construction (`ContactRequestStatus`)

Ordre : `new` → `contacted` → `rdv_prevu` → `qualified` → `quoted` → `won` / `lost` / `spam`.
La colonne `status` en DB est `String(32)` (varchar libre), pas un enum natif PostgreSQL —
ajouter une valeur ne nécessite pas de migration Alembic.

## Règle « IA au courant de tout » (permanente, 2026-09-02)

Toute NOUVELLE fonctionnalité doit être disponible via la clé API /
le connecteur MCP SANS que Phil ait à le demander :

1. **Écritures** : couvertes automatiquement par le middleware
   d'audit (`app/core/audit_middleware.py`) → rien à faire, mais ne
   JAMAIS ajouter un chemin aux exclusions sans raison de sécurité.
2. **Lecture** : brancher toute nouvelle entité aux registres MCP
   (`_LIST_ENTITIES` / `_DETAIL_ENTITIES` d'`activity.py`) ou à un
   outil dédié. Les routes apparaissent d'elles-mêmes dans
   `kratos_api_catalogue` (OpenAPI) et sont actionnables via
   `kratos_action`.
3. **Cliquet CI** : toute nouvelle table doit être ajoutée à
   `app/core/api_ia_couverture.py` — le test
   `test_smoke_couverture_api_ia` ÉCHOUE sinon. C'est voulu : l'acte
   d'ajout est la preuve que l'exposition a été considérée.

## Règle de promotion en prod (incident 2026-09-08)

Le 2026-09-08, une promotion `dev → main` a vidé le pipeline et les fiches
de prospection en prod (colonnes attendues par le code absentes en base →
toutes les lectures de `lead_analyses` en erreur). Depuis :

1. **Jamais de promotion sans le GO EXPLICITE de Phil après SON test sur
   staging** (`h2-0-web-dev`). « Merge quand c'est fini » ne vaut pas GO :
   demander « as-tu testé la dernière version sur staging ? ».
2. **Promotion par PR `dev → main`**, jamais de push direct. Un revert PR
   est préparé AVANT de merger (branche `hotfix/revert-<sujet>`).
3. **Garde-fous techniques** (ne pas contourner) : `/health` répond 503
   « degraded » si une colonne des modèles manque en base → Render refuse
   la nouvelle version et garde l'ancienne ; au démarrage,
   `app/db/schema_check.ajouter_colonnes_manquantes()` ajoute toute colonne
   nullable manquante (une colonne NOT NULL sans défaut exige une vraie
   migration + `ensure_critical_columns`).
4. **Vérification IMMÉDIATE après déploiement**, avant de dire « c'est
   fait » : `GET /health` → `schema.ok: true`, puis lectures par le
   connecteur MCP (`kratos_list_analyses`, `kratos_list_deals`,
   `kratos_list_entities` pour chaque pôle touché). Si un échec : merger le
   revert préparé, prévenir Phil, chercher la cause ensuite.
5. Après un revert sur `main`, **ne jamais resynchroniser `main → dev` tel
   quel** (les reverts effaceraient le chantier sur dev) : re-promouvoir
   par « revert des reverts » une fois la cause corrigée.
