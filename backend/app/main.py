"""
Construction Management API - Main Application Entry Point

FastAPI application for Horizon Services Immobiliers.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import api_router
from app.core.config import settings
from app.db.session import (
    close_db,
    ensure_assistant_tables,
    ensure_contrat_gestion_tables,
    ensure_critical_columns,
    ensure_esign_tables,
    ensure_invest_portal_tables,
    ensure_immobilier_aux_tables,
    ensure_project_corrections_tables,
    ensure_raci_tables,
    ensure_relance_tables,
    ensure_permissions_defaults_metier,
    ensure_role_permissions_tables,
    ensure_qbo_connections_table,
    ensure_timesheet_tables,
    ensure_validation_bancaire_tables,
    ensure_volets_whitelist_migration,
    init_db,
)

logger = logging.getLogger(__name__)


async def _run_startup_tasks() -> None:
    """Travail de démarrage : créations de tables idempotentes
    (create_all), colonnes critiques, backfills et seeders.

    Exécuté EN ARRIÈRE-PLAN (cf. ``lifespan``) pour ne PAS bloquer la
    liaison du port. uvicorn lance le startup AVANT de lier le socket :
    sur un cold start Render (BDD free qui se réveille), ce travail
    dépassait le délai de scan de port → « no open ports » → déploiement
    échoué. Tout est déjà best-effort (try/except par étape).
    """
    try:
        import app.models  # noqa: F401
        await init_db()
    except Exception as exc:
        logger.warning("init_db failed during startup: %s", exc)

    # Garantit les colonnes critiques HORS de la grosse transaction
    # init_db : si une étape d'init_db échoue, toute sa transaction est
    # annulée (y compris les ADD COLUMN). Ici chaque colonne est créée
    # dans sa propre transaction → garantie même si init_db a planté.
    try:
        await ensure_critical_columns()
    except Exception as exc:
        logger.warning("ensure_critical_columns failed during startup: %s", exc)

    # Garde-fou (incident 2026-09-08) : TOUTE colonne nullable déclarée
    # par un modèle et absente en base est ajoutée ici — plus besoin de
    # se souvenir de la lister à la main. Puis contrôle final : s'il en
    # manque encore, /health répond 503 et Render garde l'ancienne
    # version en ligne.
    try:
        from app.db.schema_check import ajouter_colonnes_manquantes, schema_ok

        await ajouter_colonnes_manquantes()
        ok, colonnes, _tables = await schema_ok(force=True)
        if not ok:
            logger.error(
                "DÉMARRAGE AVEC SCHÉMA INCOMPLET — /health répondra 503 : %s",
                ", ".join(colonnes),
            )
    except Exception as exc:
        logger.warning("schema_check failed during startup: %s", exc)

    # STAGING : premier compte owner sur base vide (no-op partout ailleurs).
    try:
        from app.services.bootstrap_admin import ensure_bootstrap_admin

        await ensure_bootstrap_admin()
    except Exception as exc:
        logger.warning("bootstrap admin failed during startup: %s", exc)

    # Tables RACI (Distribution des tâches) — créées dans leur propre
    # transaction pour survivre à un abort d'init_db.
    try:
        await ensure_raci_tables()
    except Exception as exc:
        logger.warning("ensure_raci_tables failed during startup: %s", exc)

    # Tables auxiliaires immobilier (relances de loyer) — idem, isolées.
    try:
        await ensure_immobilier_aux_tables()
    except Exception as exc:
        logger.warning(
            "ensure_immobilier_aux_tables failed during startup: %s", exc
        )

    # Table des actions de l'assistant IA (cartes à confirmer) — idem.
    try:
        await ensure_assistant_tables()
    except Exception as exc:
        logger.warning(
            "ensure_assistant_tables failed during startup: %s", exc
        )

    # Backfill borné (M9a, audit 2026-08-13) : chaque logement VACANT
    # sans dossier de relocation actif obtient son dossier — la création
    # ne vit plus dans le GET /locations/overview (un GET ne mute pas).
    # Best-effort, idempotent, borné à 500 créations par démarrage.
    try:
        from app.db.session import AsyncSessionLocal as _LocSession
        from app.services.locatif_depart import (
            ouvrir_dossiers_unites_vacantes,
        )

        async with _LocSession() as session:
            n = await ouvrir_dossiers_unites_vacantes(session, limite=500)
            if n:
                await session.commit()
                logger.info(
                    "Startup backfill: %d dossier(s) de relocation "
                    "créés pour les unités vacantes", n,
                )
    except Exception as exc:
        logger.warning(
            "backfill dossiers unites vacantes failed: %s", exc
        )

    # Purge 2026-08-27 (retour Phil) : les tâches QG de renouvellement
    # auto-générées quittent /entreprises/taches — la génération est
    # coupée (bail_renew_tasks) et les tâches auto NON terminées sont
    # supprimées. Best-effort, idempotent (plus rien ne les recrée).
    try:
        from sqlalchemy import delete as _delete

        from app.db.session import AsyncSessionLocal as _RenewSession
        from app.models.entreprise_tache import (
            EntrepriseTache as _ETache,
            TacheStatus as _TStatus,
        )

        async with _RenewSession() as session:
            res = await session.execute(
                _delete(_ETache).where(
                    _ETache.tags_json.like(
                        "%auto-bail-renouvellement%"
                    ),
                    _ETache.status != _TStatus.DONE.value,
                )
            )
            if res.rowcount:
                await session.commit()
                logger.info(
                    "Startup purge: %d tache(s) de renouvellement "
                    "auto-generees supprimees", res.rowcount,
                )
    except Exception as exc:
        logger.warning(
            "purge taches bail-renew failed: %s", exc
        )

    # Backfill 2026-08-17 : baux placeholder PlexFlow « terminés » par
    # erreur à l'import du 12 août alors que le locataire est en place
    # (paie encore, aucun successeur) — réactivés. Best-effort,
    # idempotent (voir reactiver_baux_termines_a_tort).
    try:
        from app.db.session import AsyncSessionLocal as _ReactSession
        from app.services.locatif_depart import (
            reactiver_baux_termines_a_tort,
        )

        async with _ReactSession() as session:
            n = await reactiver_baux_termines_a_tort(session)
            if n:
                await session.commit()
                logger.info(
                    "Startup backfill: %d bail (baux) réactivé(s) — "
                    "terminés par erreur à l'import", n,
                )
    except Exception as exc:
        logger.warning(
            "backfill reactivation baux termines failed: %s", exc
        )

    # Backfill 2026-08-17 (décision Phil) : baux placeholder PlexFlow
    # TERMINÉS mais restés à leur date de fin par défaut (2027-06-01) —
    # ils faisaient courir un loyer fantôme chaque mois. La fin est
    # ramenée à la veille de l'arrivée du successeur ; un paiement resté
    # sur un mois non couvert suit, si ce mois est libre. Best-effort,
    # idempotent (voir recaler_fins_baux_placeholder).
    try:
        from app.db.session import AsyncSessionLocal as _FinSession
        from app.services.locatif_depart import (
            recaler_fins_baux_placeholder,
        )

        async with _FinSession() as session:
            n = await recaler_fins_baux_placeholder(session)
            if n:
                await session.commit()
                logger.info(
                    "Startup backfill: %d bail (baux) recalé(s) sur "
                    "l'arrivée du locataire suivant", n,
                )
    except Exception as exc:
        logger.warning(
            "backfill recalage fins baux placeholder failed: %s", exc
        )

    # Correctif 2026-08-17 (retour Phil) : le backfill de réactivation
    # a ressuscité des baux dont le logement était en RELOCATION (unité
    # vacante) — « j'ai des unités vacantes, mais encore présentes dans
    # les baux ». On re-termine ces baux et on recale le logement.
    # Best-effort, idempotent (voir annuler_reactivations_erronees).
    try:
        from app.db.session import AsyncSessionLocal as _AnnulSession
        from app.services.locatif_depart import (
            annuler_reactivations_erronees,
        )

        async with _AnnulSession() as session:
            n = await annuler_reactivations_erronees(session)
            if n:
                await session.commit()
                logger.info(
                    "Startup backfill: %d réactivation(s) annulée(s) — "
                    "logement en relocation", n,
                )
    except Exception as exc:
        logger.warning(
            "backfill annulation reactivations failed: %s", exc
        )

    # Le statut d'un logement est DÉRIVÉ de ses baux mais STOCKÉ : il se
    # périme dès qu'une transition oublie de le recalculer. Constat du
    # 2026-08-19 — un logement affichait « réservé » alors que le bail
    # proposé qui le réservait avait une date de début passée et que le
    # candidat avait été retiré. Ce recalage global est le filet ; il ne
    # dispense pas d'appeler recaler_statut_logement au bon moment.
    try:
        from app.db.session import AsyncSessionLocal as _StatutSession
        from app.services.locatif_depart import (
            recaler_tous_les_statuts_logements,
        )

        async with _StatutSession() as session:
            n = await recaler_tous_les_statuts_logements(session)
            if n:
                logger.info(
                    "Startup backfill: %d statut(s) de logement recalé(s)", n
                )
    except Exception as exc:
        logger.warning("backfill statuts logements failed: %s", exc)

    # Tables Feuille de temps (Gestion d'entreprise) — transaction isolée.
    try:
        await ensure_timesheet_tables()
    except Exception as exc:
        logger.warning(
            "ensure_timesheet_tables failed during startup: %s", exc
        )

    # Table Connexions QuickBooks multi-compagnies — transaction isolée.
    try:
        await ensure_qbo_connections_table()
    except Exception as exc:
        logger.warning(
            "ensure_qbo_connections_table failed during startup: %s", exc
        )

    # Tables Validation bancaire des loyers (QBO lecture seule) —
    # transaction isolée.
    try:
        await ensure_validation_bancaire_tables()
    except Exception as exc:
        logger.warning(
            "ensure_validation_bancaire_tables failed during startup: %s",
            exc,
        )

    # Permissions v2 : reporte les volets des anciennes whitelists
    # d'emails dans volets_json (one-shot idempotent) — transaction isolée.
    try:
        await ensure_volets_whitelist_migration()
    except Exception as exc:
        logger.warning(
            "ensure_volets_whitelist_migration failed during startup: %s",
            exc,
        )

    # Table Corrections/améliorations de projet (Flux A) — transaction
    # isolée. Sans ce filet la table manque en prod → 500 sur l'ajout.
    try:
        await ensure_project_corrections_tables()
    except Exception as exc:
        logger.warning(
            "ensure_project_corrections_tables failed during startup: %s", exc
        )

    # Tables du moteur de relances (cadence + plans + relances par lead) —
    # transaction isolée. Sans ce filet les tables manquent en prod → 500
    # sur l'ajout d'une relance (« Ajout échoué (HTTP 500) »).
    try:
        await ensure_relance_tables()
    except Exception as exc:
        logger.warning(
            "ensure_relance_tables failed during startup: %s", exc
        )

    # Table des permissions configurables (Paramètres → Permissions) +
    # seed des défauts (= comportement actuel). Transaction isolée.
    try:
        await ensure_role_permissions_tables()
    except Exception as exc:
        logger.warning(
            "ensure_role_permissions_tables failed during startup: %s", exc
        )

    # Permissions v2 : seuils MÉTIER (immobilier + données financières
    # prospection → gestionnaire) sur les lignes encore au vieux défaut
    # « employé » — one-shot avec sentinelle, APRÈS le seed ci-dessus.
    try:
        await ensure_permissions_defaults_metier()
    except Exception as exc:
        logger.warning(
            "ensure_permissions_defaults_metier failed during startup: %s",
            exc,
        )

    # Tables du Contrat de gestion (onglet fiche immeuble) + seed du
    # gabarit par défaut. Transaction isolée.
    try:
        await ensure_contrat_gestion_tables()
    except Exception as exc:
        logger.warning(
            "ensure_contrat_gestion_tables failed during startup: %s", exc
        )

    # Tables du module eSign (signature électronique de documents,
    # pôle Gestion d'entreprise). Transaction isolée.
    try:
        await ensure_esign_tables()
    except Exception as exc:
        logger.warning(
            "ensure_esign_tables failed during startup: %s", exc
        )

    # Tables du Portail Investisseur v2 (participations par compagnie,
    # flux, réglages de publication, documents, jalons). Transaction
    # isolée.
    try:
        await ensure_invest_portal_tables()
    except Exception as exc:
        logger.warning(
            "ensure_invest_portal_tables failed during startup: %s", exc
        )

    # Backfill : crée le projet (+ facture d'acompte DRAFT) pour les
    # soumissions ACCEPTED qui n'en ont pas encore. Rattrape les
    # acceptations antérieures à l'auto-création (PR #45). Best-effort,
    # silencieux en cas d'échec — le service tourne quand même.
    try:
        from app.api.v1.endpoints.soumission_to_project import (
            backfill_accepted_soumissions,
        )
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            n = await backfill_accepted_soumissions(session)
            if n:
                logger.info(
                    "Startup backfill: %d project(s) created from "
                    "previously-accepted soumissions",
                    n,
                )
    except Exception as exc:
        logger.warning("backfill_accepted_soumissions failed: %s", exc)

    # Drive Conventions — seeder idempotent. Crée les 4 conventions
    # par défaut (Deal Pipeline, DevlogClient, DevlogProject,
    # ConstructionProject) si elles n'existent pas encore en BDD.
    # Toutes inactives par défaut, Phil les active une à une après
    # configuration du parent_folder_drive_id. Best-effort silencieux.
    try:
        from app.db.session import AsyncSessionLocal as _DriveSeedSession
        from app.services.drive_conventions_seed import (
            seed_default_drive_conventions,
        )

        async with _DriveSeedSession() as session:
            n = await seed_default_drive_conventions(session)
            if n:
                logger.info(
                    "Drive conventions seed: %d convention(s) creee(s)",
                    n,
                )
    except Exception as exc:
        logger.warning("drive_conventions seed failed: %s", exc)

    # Drive Page Modules — seeder idempotent Phase 7. Crée une ligne
    # inactive par type de page (ProspectionDeal, DevlogClient, ...) si
    # absente. Phil active chaque section Drive via /parametres/drive.
    # Best-effort silencieux.
    try:
        from app.db.session import AsyncSessionLocal as _DrivePageSeedSession
        from app.services.drive_page_modules_seed import (
            seed_default_drive_page_modules,
        )

        async with _DrivePageSeedSession() as session:
            n = await seed_default_drive_page_modules(session)
            if n:
                logger.info(
                    "Drive page modules seed: %d module(s) cree(s)",
                    n,
                )
    except Exception as exc:
        logger.warning("drive_page_modules seed failed: %s", exc)

    # Drive Auto-Upload — seeder idempotent Phase 6. Crée 5 règles
    # "document généré → sous-dossier Drive de l'entité" inactives par
    # défaut (fiche d'analyse, offre PPTX, NDA signé, soumission,
    # facture). Phil active chaque règle via /parametres/drive après
    # vérification. Best-effort silencieux.
    try:
        from app.db.session import AsyncSessionLocal as _DriveAutoUploadSession
        from app.services.drive_auto_upload_seed import (
            seed_default_drive_auto_uploads,
        )

        async with _DriveAutoUploadSession() as session:
            n = await seed_default_drive_auto_uploads(session)
            if n:
                logger.info(
                    "Drive auto-uploads seed: %d regle(s) creee(s)",
                    n,
                )
    except Exception as exc:
        logger.warning("drive_auto_uploads seed failed: %s", exc)

    # Nettoyage anti-spam rétroactif : reclasse en « spam » les demandes
    # NEUVES qui matchent les signaux (spams entrés avant le déploiement
    # du filtre ou pendant un redémarrage). Idempotent, best-effort.
    try:
        from app.db.session import AsyncSessionLocal as _SpamSession
        from app.services.contact_spam import sweep_spam_contact_requests

        async with _SpamSession() as session:
            n = await sweep_spam_contact_requests(session)
            await session.commit()
            if n:
                logger.info(
                    "Anti-spam sweep: %d demande(s) reclassée(s) en spam", n
                )
    except Exception as exc:
        logger.warning("anti-spam sweep failed: %s", exc)

    # Téléphonie — auto-bootstrap Twilio : si les credentials et le
    # numéro sont configurés en env, on s'assure que la ligne existe en
    # DB et que le webhook URL pointe sur ce backend. Idempotent ;
    # fast-path < 5 ms quand déjà bootstrapé (juste un SELECT).
    try:
        from app.scripts.twilio_bootstrap import bootstrap_twilio

        rc = await bootstrap_twilio()
        if rc == 0:
            logger.info("Twilio bootstrap OK")
    except Exception as exc:
        logger.warning("twilio bootstrap failed: %s", exc)



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lie le port IMMÉDIATEMENT et lance le travail de démarrage
    (migrations idempotentes + backfills + seeders) en ARRIÈRE-PLAN.

    Sinon, sur un cold start Render, ce travail bloque la liaison du port
    → Render ne détecte « aucun port ouvert » → le déploiement échoue.
    ``/health`` ne touche pas la BDD, donc la sonde de santé passe dès le
    bind. Filet : pendant quelques secondes après un déploiement, une
    requête pourrait tomber sur une colonne pas encore créée — négligeable
    sur une BDD déjà à jour (les migrations sont idempotentes / no-op)."""
    startup_task = asyncio.create_task(_run_startup_tasks())
    # Filets QBO AUTONOMES (aucune dépendance à un cron externe) :
    # 1ᵉʳ passage ~90 s après le boot (rattrape les factures/dépenses dont
    # le push à l'envoi a échoué en silence), puis toutes les heures.
    from app.services.qbo_nets import qbo_nets_loop

    qbo_nets_task = asyncio.create_task(qbo_nets_loop())
    try:
        yield
    finally:
        if not startup_task.done():
            startup_task.cancel()
        if not qbo_nets_task.done():
            qbo_nets_task.cancel()
        await close_db()


def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Horizon Services Immobiliers API",
        description="API publique et interne pour Horizon Services Immobiliers.",
        version="0.2.1",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # CORS — allow the production domains, plus localhost for dev,
    # plus any *.onrender.com preview URL so the Render-assigned
    # temporary domain for h2-0-web can reach the API during setup.
    allowed_origins: list[str] = []
    if settings.is_development:
        allowed_origins = ["*"]
    else:
        raw = getattr(settings, "frontend_origins", "") or ""
        allowed_origins = [o.strip() for o in raw.split(",") if o.strip()]
        if not allowed_origins:
            allowed_origins = [
                "https://immohorizon.com",
                "https://www.immohorizon.com",
                "https://immohorizon.ca",
                "https://www.immohorizon.ca",
            ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        # Le regex couvre UNIQUEMENT les services Render `h2-0*` (API,
        # web, preview `-pr-…`, staging), localhost, et l'origin
        # `chrome-extension://<id>` de l'extension Horizon (P-05a : on ne
        # laisse plus n'importe quel *.onrender.com ni n'importe quelle
        # extension avec allow_credentials=True). L'ID exact de
        # l'extension reste à verrouiller (P-05f) : `[a-p]{32}` = format
        # d'ID Chrome valide, à remplacer par l'ID précis quand connu.
        allow_origin_regex=(
            r"^https://h2-0(-[a-z0-9]+)*\.onrender\.com$|"
            r"^http://localhost(:\d+)?$|"
            r"^chrome-extension://[a-p]{32}$"
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Journal d'événements automatique (« IA au courant de tout »,
    # GO Phil 2026-09-02) : chaque écriture API réussie → AuditLog.
    # Montage BEST-EFFORT : un import qui casse n'empêche jamais le
    # démarrage.
    try:
        from app.core.audit_middleware import AuditMiddleware

        app.add_middleware(AuditMiddleware)
    except Exception as _exc:  # noqa: BLE001
        logger.warning("AuditMiddleware non monté : %s", _exc)

    app.include_router(api_router, prefix="/api/v1")

    # ── Serveur MCP « remote » (connecteur custom Claude) ───────────
    # Montage BEST-EFFORT et TOTALEMENT ISOLÉ : tout import ou montage qui
    # échoue est loggué mais n'empêche JAMAIS le démarrage de l'app. Si ce
    # bloc lève, Kratos démarre normalement, simplement sans /mcp.
    # Le serveur MCP n'expose QUE l'activité en lecture seule, scopée à la
    # clé d'API krts_... passée dans l'URL (/mcp/{key}). Aucun lifespan ni
    # middleware global ajouté : c'est un simple APIRouter.
    try:
        from app.api.v1.endpoints.mcp_server import router as mcp_router

        # Montage direct sur l'app → URL backend : /mcp/{api_key}
        # (atteignable sur https://h2-0.onrender.com/mcp/{key}).
        app.include_router(mcp_router)
        # Montage AUSSI sous /api/v1 → URL sur le domaine propre :
        # https://immohorizon.com/api/v1/mcp/{key}. Le rewrite Next.js du
        # frontend ne proxifie QUE /api/*, donc ce second montage rend le
        # connecteur accessible via le domaine de production (plus robuste
        # côté réseau que *.onrender.com, parfois filtré par des ISP/iOS).
        app.include_router(mcp_router, prefix="/api/v1")
        logger.info(
            "MCP server mounted at /mcp/{api_key} and "
            "/api/v1/mcp/{api_key} (read-only)."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP server mount skipped (app starts normally): %s", exc)

    return app


app = create_application()


@app.get("/", tags=["root"])
async def root() -> dict:
    return {
        "message": "Horizon Services Immobiliers API",
        "version": "0.2.1",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["health"])
async def health_check(response: Response) -> dict:
    """Santé de l'API + contrôle de schéma (incident 2026-09-08) : si
    une colonne attendue par les modèles manque en base, l'API se
    déclare ``degraded`` (503) → Render ne bascule pas le trafic sur
    une version cassée, l'ancienne continue de servir."""
    from app.db.schema_check import schema_ok

    ok, colonnes, tables = await schema_ok()
    if not ok:
        response.status_code = 503
    return {
        "status": "healthy" if ok else "degraded",
        "environment": settings.env,
        "schema": {
            "ok": ok,
            "colonnes_manquantes": colonnes,
            "tables_absentes": tables,
        },
    }


@app.get("/api/v1/ping", tags=["health"])
async def api_ping() -> dict:
    """Alias under /api/v1 so uptime monitors pinging a single URL
    (e.g. immohorizon.com/api/v1/ping via the Next.js rewrite) wake up
    both the frontend (which serves /api/*) and the backend (this
    handler). Cheap: no DB, no I/O."""
    return {"status": "ok"}
