"""
Database session configuration for async SQLAlchemy.

Provides:
- Async engine configuration
- Session factory
- Dependency injection for FastAPI
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


# Create async engine
# Use async_database_url to ensure postgresql+asyncpg:// format
engine = create_async_engine(
    settings.async_database_url,
    echo=settings.is_development,  # Log SQL in development
    pool_pre_ping=True,  # Verify connections before use
    pool_size=5,
    max_overflow=10,
)

# Session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency that provides an async database session.

    Usage in FastAPI:
        @app.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...

    Yields:
        AsyncSession: Database session that auto-closes after use
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def _rotate_receipt_blob_cw90(blob: bytes, content_type: str) -> Optional[bytes]:
    """Fait pivoter de 90° HORAIRE (vers la droite) un reçu stocké.
    Délègue au service partagé. Retourne None si format inconnu / échec."""
    from app.services.receipt_rotate import rotate_receipt_blob

    return rotate_receipt_blob(blob, content_type, clockwise=True)


async def _rotate_existing_receipts_cw90(conn) -> int:
    """Pivote (une seule fois) tous les reçus d'achat déjà stockés.
    Traite un reçu à la fois pour limiter la mémoire. Retourne le
    nombre de reçus effectivement pivotés."""
    from sqlalchemy import text

    ids = (
        await conn.execute(
            text(
                "SELECT id FROM achats WHERE receipt_image IS NOT NULL"
            )
        )
    ).all()
    rotated = 0
    for (rid,) in ids:
        # Chaque reçu est indépendant : une erreur isolée ne doit pas
        # interrompre le passage (sinon des reçus seraient pivotés sans
        # que le marqueur soit posé → double rotation au boot suivant).
        try:
            row = (
                await conn.execute(
                    text(
                        "SELECT receipt_image, receipt_image_content_type "
                        "FROM achats WHERE id = :id"
                    ),
                    {"id": rid},
                )
            ).first()
            if row is None or row[0] is None:
                continue
            new_blob = _rotate_receipt_blob_cw90(bytes(row[0]), row[1] or "")
            if new_blob is None:
                continue
            await conn.execute(
                text("UPDATE achats SET receipt_image = :img WHERE id = :id"),
                {"img": new_blob, "id": rid},
            )
            rotated += 1
        except Exception:
            continue
    return rotated


async def ensure_critical_columns() -> None:
    """Crée — chacune dans SA PROPRE transaction — les colonnes dont
    l'absence casse un endpoint critique.

    À appeler APRÈS `init_db` et INDÉPENDAMMENT de lui : `init_db`
    s'exécute dans une seule grosse transaction, donc si une étape y
    échoue, tout est annulé (y compris les `ADD COLUMN`). Ici chaque
    `ALTER` est isolé et committé seul → la colonne critique est garantie
    même si `init_db` a planté. Idempotent (`ADD COLUMN IF NOT EXISTS`).
    """
    import logging
    from sqlalchemy import text

    log = logging.getLogger("db.ensure_critical_columns")

    # Filet de sécurité : (re)crée les TABLES manquantes dans une
    # transaction dédiée, indépendamment d'init_db. Si init_db a échoué ou
    # n'a pas créé une nouvelle table (ex. cadence_steps, relance_items),
    # on la crée ici. create_all est idempotent (checkfirst).
    try:
        from app.db.base import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_critical_columns create_all failed: %s", exc)

    critical_columns = (
        # Suivi des ouvertures/signatures dans l'historique Communications
        # (audit 2026-08-19). Sans elle, GET /immobilier/communications
        # plante sur une base créée avant l'ajout.
        ("imm_communications", "document_id", "INTEGER"),
        # Refus d'un document signable (consentement notamment).
        ("imm_documents", "refuse_le", "TIMESTAMP WITH TIME ZONE"),
        ("imm_documents", "refuse_par", "VARCHAR(255)"),
        # Exception « bail sans document » (audit 2026-08-19) : sans
        # ces colonnes, la page Baux et le bandeau plantent.
        # Depot de garantie : date de reception et detenteur
        # (import manuel 2026-08-19).
        ("imm_baux", "depot_recu_le", "DATE"),
        ("imm_baux", "depot_detenteur", "VARCHAR(120)"),
        ("imm_baux", "sans_document_motif", "VARCHAR(255)"),
        ("imm_baux", "sans_document_par", "VARCHAR(255)"),
        ("imm_baux", "sans_document_le", "TIMESTAMP WITH TIME ZONE"),
        # Sans cette colonne, GET /api/v1/contact (pipeline construction)
        # plante → « Impossible de charger les prospects » (régression #785).
        ("contact_requests", "lost_reason", "VARCHAR(120)"),
        # Rappel planifié sur un prospect (badge « à rappeler » dans le CRM).
        ("contact_requests", "rappel_at", "TIMESTAMP WITH TIME ZONE"),
        # Coffre Abonnements : quantité (prix unitaire × N). La table
        # existait avant l'ajout du champ → create_all ne l'ajoute pas.
        ("subscriptions", "quantite", "INTEGER NOT NULL DEFAULT 1"),
        # RACI : les colonnes sont des comptes Kratos (table preexistante).
        ("raci_people", "user_id", "INTEGER"),
        ("raci_activities", "subsection", "VARCHAR(120) NOT NULL DEFAULT ''"),
        # Téléphonie Léa : sans ces colonnes, un SELECT sur la table
        # plante et casse tout le flux d'appel entrant. On les met ici
        # (transaction par colonne) pour survivre à un abort d'init_db.
        ("projects", "responsible_user_id", "INTEGER"),
        # Punch sur un CLIENT directement (gestion admin des punchs) —
        # la table préexiste, create_all ne pose pas la colonne.
        ("punches", "client_id", "INTEGER"),
        # Type de projet ; DEFAULT backfille les lignes existantes en
        # 'construction' (Postgres) → les projets actuels restent visibles.
        ("projects", "kind", "VARCHAR(32) NOT NULL DEFAULT 'construction'"),
        # ID du Job QuickBooks (sous-client) lié au projet — clé
        # d'idempotence de la synchro QBO.
        ("projects", "qbo_job_id", "VARCHAR(64)"),
        # Id du Payment QBO d'une facture payée (dédup des paiements).
        ("factures", "qbo_payment_id", "VARCHAR(64)"),
        # Id de la Purchase (dépense) QB importée comme Achat.
        ("achats", "qbo_purchase_id", "VARCHAR(64)"),
        ("voice_calls", "dial_state_json", "TEXT"),
        # Hub Automatisations : config éditable (cadence, etc.). La table
        # a été créée sans cette colonne au 1er déploiement → on l'ajoute.
        ("automation_settings", "config_json", "TEXT"),
        # Bon de travail unifié : assignable + demande interne sans signature.
        ("bons_travail", "assignee_user_id", "INTEGER"),
        ("bons_travail", "requires_signature", "BOOLEAN NOT NULL DEFAULT true"),
        # Classement par adresse + nature du montant (garantie / T&M).
        ("bons_travail", "address", "VARCHAR(500)"),
        ("bons_travail", "bon_type", "VARCHAR(32) NOT NULL DEFAULT 'temps_materiel'"),
        # Signature électronique en ligne (soumissions + bons de travail).
        # Ces colonnes étaient seulement dans le bloc transactionnel
        # `additive_columns` d'init_db : si init_db abort en cours de route,
        # elles ne sont jamais créées et /accept plante en HTTP 500 au
        # moment d'écrire la signature (`signature_image` est `deferred`,
        # donc invisible en lecture — le bug ne se déclenche qu'à la
        # signature). On les isole ici pour qu'elles existent toujours.
        ("soumissions", "signature_token", "VARCHAR(64)"),
        ("soumissions", "signed_name", "VARCHAR(255)"),
        ("soumissions", "signed_ip", "VARCHAR(64)"),
        ("soumissions", "signature_image", "BYTEA"),
        ("soumissions", "signature_image_content_type", "VARCHAR(100)"),
        # Archivage (colonne « Archivée » du tableau des soumissions) —
        # posé quand le projet lié est livré/terminé, ou à la main.
        ("soumissions", "archived_at", "TIMESTAMP WITH TIME ZONE"),
        # Accusé de lecture des soumissions devlog (le client a-t-il ouvert
        # le lien public ?). Colonnes ajoutées 2026-06 ; la table existait
        # avant → create_all ne les pose pas.
        ("devlog_soumissions", "opened_at", "TIMESTAMP WITH TIME ZONE"),
        ("devlog_soumissions", "last_opened_at", "TIMESTAMP WITH TIME ZONE"),
        ("devlog_soumissions", "open_count", "INTEGER NOT NULL DEFAULT 0"),
        # Refonte projet devlog 2026-06 : budget + heures importés de la
        # soumission acceptée (phase = module). Tables préexistantes →
        # create_all ne pose pas les colonnes.
        ("devlog_project_phases", "source_module_id", "INTEGER"),
        ("devlog_project_phases", "budget_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("devlog_project_phases", "heures_dev_prevues",
         "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("devlog_project_phases", "heures_manager_prevues",
         "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("devlog_projects", "budget_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("devlog_projects", "heures_dev_prevues",
         "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("devlog_projects", "heures_manager_prevues",
         "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("devlog_projects", "taux_horaire_defaut", "DOUBLE PRECISION"),
        ("devlog_projects", "notes", "TEXT"),
        ("bons_travail", "signature_token", "VARCHAR(64)"),
        ("bons_travail", "signed_at", "TIMESTAMP WITH TIME ZONE"),
        ("bons_travail", "signed_by_name", "VARCHAR(255)"),
        ("bons_travail", "signature_ip", "VARCHAR(64)"),
        ("bons_travail", "signature_image", "BYTEA"),
        ("bons_travail", "signature_image_content_type", "VARCHAR(100)"),
        # Signature de la FACTURE FINALE (page publique /facture/{token}).
        # Même piège que les soumissions ci-dessus : ces colonnes n'étaient
        # que dans le bloc additif d'init_db → `signature_image` (deferred,
        # donc jamais SELECTée) manquait en prod et le POST /sign plantait
        # en HTTP 500 à l'écriture (retour Phil 2026-07-10).
        ("factures", "is_final", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("factures", "signature_token", "VARCHAR(64)"),
        ("factures", "signed_name", "VARCHAR(255)"),
        ("factures", "signed_ip", "VARCHAR(64)"),
        ("factures", "signed_at", "TIMESTAMP WITH TIME ZONE"),
        ("factures", "signature_image", "BYTEA"),
        ("factures", "signature_image_content_type", "VARCHAR(100)"),
        # Refonte Bon de travail 2026-06-30 : bon INTERNE (entretien de nos
        # immeubles) + moteur de refacturation. Ces colonnes n'étaient que
        # dans `additive_columns` (init_db) → si init_db abort, elles ne sont
        # jamais créées et le SELECT des bons plante en HTTP 500 (« Impossible
        # de charger les bons de travail » sur tout /app/bons). On les
        # garantit ici, comme pour la signature plus haut.
        ("bons_travail", "kind", "VARCHAR(16) NOT NULL DEFAULT 'construction'"),
        ("bons_travail", "owner_entreprise_id", "INTEGER"),
        ("bons_travail", "immeuble_id", "INTEGER"),
        ("bons_travail", "logement_id", "INTEGER"),
        ("bons_travail", "executant_type", "VARCHAR(16)"),
        ("bons_travail", "sous_traitant_id", "INTEGER"),
        ("bons_travail", "marge_pct", "NUMERIC(5, 2) NOT NULL DEFAULT 0"),
        ("bons_travail", "work_notes", "TEXT"),
        ("bons_travail", "is_urgent", "BOOLEAN NOT NULL DEFAULT false"),
        # Créateur du bon (2026-08-10) — affiché sur les cartes.
        ("bons_travail", "created_by_user_id", "INTEGER"),
        (
            "projects",
            "correction_status",
            "VARCHAR(16) NOT NULL DEFAULT 'a_planifier'",
        ),
        ("bon_items", "item_type", "VARCHAR(16) NOT NULL DEFAULT 'materiel'"),
        ("bon_items", "cost_rate", "NUMERIC(12, 2)"),
        ("bon_items", "bill_rate", "NUMERIC(12, 2)"),
        ("bon_items", "marge_pct", "NUMERIC(5, 2)"),
        ("bon_items", "cost_total", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
        ("bon_items", "employe_id", "INTEGER"),
        ("bon_items", "sous_traitant_id", "INTEGER"),
        ("punches", "bon_travail_id", "INTEGER"),
        # Mapping comptes QBO (table qbo_account_maps existait avant l'ajout
        # de ces colonnes → create_all ne les pose pas sur une table déjà
        # créée). On les ajoute ici, idempotent.
        # Lien facture_item → soumission_item (facturation progressive
        # par item). Table préexistante → create_all ne l'ajoute pas.
        ("facture_items", "soumission_item_id", "INTEGER"),
        ("qbo_account_maps", "labour_expense_account", "VARCHAR(255)"),
        ("qbo_account_maps", "labour_clearing_account", "VARCHAR(255)"),
        # Feuille de temps QBO liée à un punch (heures → suivi de projet QB
        # sans écriture comptable). Table préexistante → create_all ne la
        # pose pas.
        ("punches", "qbo_time_activity_id", "VARCHAR(64)"),
        # Dernière erreur de synchro QBO d'une facture — affichée sur la
        # fiche pour rendre les échecs de push visibles sans les logs.
        ("factures", "qbo_sync_error", "VARCHAR(500)"),
        # Reçu sans projet/BT rattaché directement à un CLIENT (CustomerRef
        # QB = client mère).
        ("achats", "client_id", "INTEGER"),
        # Gestion externe d'un immeuble (compagnie tierce) : sans ces
        # colonnes, tout SELECT sur imm_immeubles plante → 500 sur tout
        # le volet immobilier. Table préexistante → create_all ne les
        # pose pas ; on les garantit ici (transaction par colonne).
        ("imm_immeubles", "gestion_externe", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("imm_immeubles", "gestionnaire_externe_nom", "VARCHAR(255)"),
        ("imm_immeubles", "gestionnaire_externe_contact", "VARCHAR(255)"),
        # Gestion externe mais maintenance par NOS hommes (2026-07-22).
        (
            "imm_immeubles",
            "maintenance_interne",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        ),
        # Frais de gestion mensuels (page /immobilier/frais-gestion,
        # 2026-07-22) : contrat actif, %, client QuickBooks du proprio.
        (
            "imm_immeubles",
            "frais_gestion_actif",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        ),
        ("imm_immeubles", "frais_gestion_pct", "NUMERIC(5,2)"),
        ("imm_immeubles", "frais_gestion_depuis", "DATE"),
        ("imm_immeubles", "qbo_customer_id", "VARCHAR(64)"),
        ("imm_immeubles", "qbo_customer_name", "VARCHAR(255)"),
        # Frais de gestion — ligne « complément » (loyers payés en retard
        # après la facturation du mois, 2026-07-23).
        (
            "imm_factures_gestion",
            "est_complement",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        ),
        # Facturation immo v5 (2026-07-23) : frais de relocation au
        # contrat (tarif logement/chambre) + lignes relocation/manuel.
        ("imm_immeubles", "frais_relocation_logement", "NUMERIC(10,2)"),
        ("imm_immeubles", "frais_relocation_chambre", "NUMERIC(10,2)"),
        (
            "imm_factures_gestion",
            "type_ligne",
            "VARCHAR(16) NOT NULL DEFAULT 'gestion'",
        ),
        ("imm_factures_gestion", "relocation_dossier_id", "INTEGER"),
        ("imm_factures_gestion", "libelle", "VARCHAR(255)"),
        # Finances immobilier 2026-07 : composition des intérêts d'une
        # hypothèque ('semi'|'mensuelle'), évaluation de référence pour
        # l'équité, dépenses en % des loyers + taxables (TPS/TVQ).
        # Tables préexistantes → create_all ne pose pas les colonnes ;
        # sans elles, tout SELECT sur ces tables plante → 500.
        ("imm_hypotheques", "composition_interets", "VARCHAR(16)"),
        ("imm_evaluations", "is_reference", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("immeuble_depenses", "is_pourcentage",
         "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("immeuble_depenses", "taxable", "BOOLEAN NOT NULL DEFAULT FALSE"),
        # Dépôts de garantie opérationnels 2026-07 : date de remise du
        # dépôt au locataire (page Dépôts → « Marquer rendu »).
        ("imm_baux", "depot_rendu_le", "DATE"),
        # Bail AU MOIS (chambres) : reconduction auto, jamais d'avis de
        # renouvellement, loyers qui courent sans egard a date_fin.
        ("imm_baux", "au_mois", "BOOLEAN"),
        # Jour d'echeance du loyer (bail TAL « Ou le ___ ») : 1 par
        # defaut, jusqu'a 28. Pilote le seuil de retard par bail.
        ("imm_baux", "jour_echeance", "INTEGER NOT NULL DEFAULT 1"),
        # Hub Rencontres v2 : source d'import + fichiers archives Drive.
        ("rencontres", "source", "VARCHAR(16)"),
        ("rencontres", "drive_links_json", "TEXT"),
        # Optimisation v2 : type d'entente de la négo locataire.
        ("optimisation_negos", "type_entente", "VARCHAR(24)"),
        # Optimisation v5 : objectifs annuels, financement/comptant par
        # enveloppe, compte bancaire du projet, recherche locataire.
        ("optimisation_projets", "objectif_revenus_annuels", "NUMERIC(12,2)"),
        ("optimisation_projets", "objectif_depenses_annuelles", "NUMERIC(12,2)"),
        ("optimisation_projets", "qbo_bank_account_id", "VARCHAR(64)"),
        ("optimisation_projets", "qbo_bank_account_name", "VARCHAR(255)"),
        ("optimisation_budget_lignes", "qbo_financement_accounts_json", "TEXT"),
        ("optimisation_budget_lignes", "comptant_disponible", "NUMERIC(12,2)"),
        ("optimisation_negos", "recherche_json", "TEXT"),
        # v9 : point de départ du calcul de rentabilité de la compagnie.
        ("optimisation_projets", "rentabilite_depuis", "DATE"),
        # v10 : enveloppe « Budget de détention » (mode de la ligne).
        ("optimisation_budget_lignes", "mode", "VARCHAR(24)"),
        # v13 : compte d'hypothèque du cashflow + comptes des avances
        # des actionnaires.
        ("optimisation_projets", "qbo_hypotheque_account_id", "VARCHAR(64)"),
        ("optimisation_projets", "qbo_hypotheque_account_name", "VARCHAR(255)"),
        ("optimisation_projets", "avances_accounts_json", "TEXT"),
        # Renouvellement v3 : réponse du locataire + application au bail.
        ("imm_bail_renouvellements", "reponse_le", "DATE"),
        ("imm_bail_renouvellements", "refus_motif", "TEXT"),
        ("imm_bail_renouvellements", "applique_le", "DATE"),
        # Documents 2026-07-27 (démêlage communications / dossier) :
        # « document courant » du bail et de l'avis de renouvellement
        # (celui qui s'ouvre au clic ; le remplacer archive l'ancien),
        # + origine/rattachement des documents pour l'import manuel.
        ("imm_baux", "document_id", "INTEGER"),
        ("imm_bail_renouvellements", "document_id", "INTEGER"),
        ("imm_documents", "logement_id", "INTEGER"),
        ("imm_documents", "source",
         "VARCHAR(16) NOT NULL DEFAULT 'genere'"),
        ("imm_documents", "filename", "VARCHAR(255)"),
        ("imm_documents", "remplace_document_id", "INTEGER"),
        # Drive ↔ documents locatifs (v17b) : copie Drive du PDF signé.
        ("imm_documents", "drive_file_id", "VARCHAR(128)"),
        # Locations v2 2026-07 : enquêtes de prélocation + candidat retenu
        # sur les visites (table créée au déploiement précédent SANS ces
        # colonnes → additif obligatoire ici).
        ("imm_location_visites", "enquete_credit", "BOOLEAN"),
        ("imm_location_visites", "enquete_references", "BOOLEAN"),
        ("imm_location_visites", "enquete_emploi", "BOOLEAN"),
        ("imm_location_visites", "enquete_notes", "TEXT"),
        ("imm_location_visites", "retenu", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("imm_location_visites", "candidat_email", "VARCHAR(320)"),
        ("imm_location_visites", "candidat_phone", "VARCHAR(50)"),
        ("imm_location_dossiers", "nouveau_bail_id", "INTEGER"),
        # Prélèvement préautorisé — colonnes INERTES (fonctionnalité
        # retirée le 2026-08-19, en attente de Rotessa) : conservées
        # pour ne pas casser une base existante.
        # (Règle H1 Paiements Canada, perception Desjardins).
        (
            "imm_locataires",
            "dpa_statut",
            "VARCHAR(16) NOT NULL DEFAULT 'aucun'",
        ),
        ("imm_locataires", "dpa_envoye_le", "DATE"),
        ("imm_locataires", "dpa_signe_le", "DATE"),
        # Gestion externe 2026-08-14 : loyer attendu FIGÉ au moment du
        # « marquer payé » — les mois déjà réglés gardent leur montant
        # historique quand le loyer du logement change ensuite.
        ("imm_paiements_externes", "loyer_attendu", "NUMERIC(10,2)"),
        # Retours Steven 2026-07-20 : confirmation annuelle d'assurance
        # locataire + logement loué en chambres.
        ("imm_locataires", "assurance_confirmee_le", "DATE"),
        # Feuille de temps : compagnie non refacturable (2026-07-22).
        (
            "timesheet_companies",
            "refacturable",
            "BOOLEAN NOT NULL DEFAULT TRUE",
        ),
        # Feuille de temps : la grille a un bloc d'heures NON refacturables
        # par semaine (2026-07-22) — le swap de contrainte unique se fait
        # dans ensure_timesheet_tables.
        (
            "timesheet_entries",
            "refacturable",
            "BOOLEAN NOT NULL DEFAULT TRUE",
        ),
        # Feuille de temps : client QuickBooks associé à une compagnie
        # (facturation de la refacturation, 2026-07-22).
        ("timesheet_companies", "qbo_customer_id", "VARCHAR(64)"),
        ("timesheet_companies", "qbo_customer_name", "VARCHAR(255)"),
        (
            "imm_logements",
            "location_en_chambres",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        ),
        # Contrat de gestion 2026-07 : accusé de lecture du MANDATAIRE,
        # distinct de celui du Mandant (opened_at/open_count) — sinon
        # l'ouverture du lien mandataire est attribuée au mandant.
        (
            "contrats_gestion",
            "mandataire_opened_at",
            "TIMESTAMP WITH TIME ZONE",
        ),
        (
            "contrats_gestion",
            "mandataire_last_opened_at",
            "TIMESTAMP WITH TIME ZONE",
        ),
        (
            "contrats_gestion",
            "mandataire_open_count",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        # Hub fiche entreprise (2026-08-10) : infos légales de la INC
        # + coordonnées des partenaires/actionnaires.
        ("entreprises", "tps_number", "VARCHAR(32)"),
        ("entreprises", "tvq_number", "VARCHAR(32)"),
        ("entreprises", "siege_social", "VARCHAR(500)"),
        ("entreprises", "date_constitution", "DATE"),
        ("entreprises", "notes_legales", "TEXT"),
        ("entreprise_partners", "partner_adresse", "VARCHAR(500)"),
        ("entreprise_partners", "partner_naissance", "DATE"),
        ("entreprise_partners", "partner_telephone", "VARCHAR(32)"),
        # 2e passe hub INC (2026-08-10) : identifiants fiscaux +
        # clicSÉQUR + partenaires personnes morales.
        ("entreprises", "arc_business_number", "VARCHAR(32)"),
        ("entreprises", "rq_identification_number", "VARCHAR(32)"),
        ("entreprises", "cnesst_number", "VARCHAR(32)"),
        ("entreprises", "regime_constitution", "VARCHAR(64)"),
        ("entreprises", "fin_annee_financiere", "VARCHAR(32)"),
        ("entreprises", "clicsequr_details", "TEXT"),
        (
            "entreprise_partners",
            "is_personne_morale",
            "BOOLEAN NOT NULL DEFAULT false",
        ),
        ("entreprise_partners", "partner_neq", "VARCHAR(32)"),
        # Interconnexion pôle entreprises (2026-08-10) : personne morale
        # liée à une de NOS INCs (fiche = source de vérité).
        ("entreprise_partners", "partner_entreprise_id", "INTEGER"),
        ("entreprises", "contact_email", "VARCHAR(320)"),
        ("entreprises", "contact_telephone", "VARCHAR(32)"),
        # Organigramme v2 (2026-08-10) : versions nommées + quotes-parts
        # de détention par flèche.
        ("org_nodes", "version_id", "INTEGER"),
        ("org_nodes", "ownership_json", "TEXT"),
        # Validation bancaire v2 (2026-08-14) : un compte QBO couvre
        # plusieurs immeubles (ou tous — fiducie), classification par
        # TYPE d'écriture (sens + raison d'ignorance), payeur extrait du
        # mémo Interac, paiement multi-mois (mois_couvert_fin). Tables
        # créées au déploiement v1 SANS ces colonnes → additif ici.
        (
            "qbo_comptes_loyers",
            "tous_les_immeubles",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        ),
        ("qbo_comptes_loyers", "suggestion_immeubles_json", "TEXT"),
        (
            "qbo_comptes_loyers",
            "suggestion_tous",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        ),
        (
            "qbo_transactions_loyers",
            "sens",
            "VARCHAR(8) NOT NULL DEFAULT 'entree'",
        ),
        ("qbo_transactions_loyers", "payeur", "VARCHAR(255)"),
        ("qbo_transactions_loyers", "ignore_raison", "VARCHAR(64)"),
        ("qbo_transactions_loyers", "mois_couvert_fin", "DATE"),
        # v7 — suggestion IA (pré-sélection à confirmer, jamais auto).
        ("qbo_transactions_loyers", "suggestion_bail_id", "INTEGER"),
        ("qbo_transactions_loyers", "suggestion_confiance", "DOUBLE PRECISION"),
    )
    for table, column, col_type in critical_columns:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN IF NOT EXISTS {column} {col_type}"
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ensure_critical_columns %s.%s failed: %s",
                table,
                column,
                exc,
            )

    # Élargissements de colonnes critiques (transaction par colonne →
    # résilient même si init_db a aborté). Les cibles de transfert
    # téléphonique acceptent plusieurs numéros séparés par virgule, donc
    # VARCHAR(20) (un seul numéro) est trop court.
    widen_columns = (
        # Descriptions d'items (facture + soumission) : texte long
        # multi-lignes (listes).
        ("facture_items", "description", "TEXT"),
        ("soumission_items", "description", "TEXT"),
        # Quantité à 6 décimales : la facturation progressive porte
        # l'avancement sur la quantité — à 3 décimales, un gros prix
        # unitaire dérivait de plusieurs dollars (devis dépassé de
        # 4,04 $ sur « 29 Besner »).
        ("facture_items", "quantity", "NUMERIC(12,6)"),
        ("voice_phone_numbers", "forward_to_e164", "VARCHAR(255)"),
        ("voice_phone_numbers", "urgency_forward_e164", "VARCHAR(255)"),
        ("voice_phone_numbers", "closer_forward_e164", "VARCHAR(255)"),
        ("voice_phone_numbers", "followup_forward_e164", "VARCHAR(255)"),
    )
    for table, column, new_type in widen_columns:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} "
                        f"ALTER COLUMN {column} TYPE {new_type}"
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ensure_critical_columns widen %s.%s failed: %s",
                table,
                column,
                exc,
            )

    # Annuaire fournisseurs (retour Phil 2026-08-18) : la section
    # Fournisseurs n'est PAS un miroir des Vendors QuickBooks — c'est
    # l'annuaire téléphonique du chargé de projet. Ajout ONE-SHOT de la
    # colonne in_directory + backfill : les fiches importées de QB sans
    # aucune info d'annuaire (ni téléphone, ni contact, ni notes)
    # sortent de l'annuaire (le lien avec leurs achats est conservé).
    # Le backfill ne tourne QU'À la création de la colonne — remettre
    # une fiche dans l'annuaire ensuite n'est jamais écrasé.
    try:
        async with engine.begin() as conn:
            exists = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name='fournisseurs' "
                        "AND column_name='in_directory'"
                    )
                )
            ).scalar()
            if not exists:
                await conn.execute(
                    text(
                        "ALTER TABLE fournisseurs ADD COLUMN "
                        "in_directory BOOLEAN NOT NULL DEFAULT TRUE"
                    )
                )
                await conn.execute(
                    text(
                        "UPDATE fournisseurs SET in_directory = FALSE "
                        "WHERE qbo_vendor_id IS NOT NULL "
                        "AND COALESCE(phone, '') = '' "
                        "AND COALESCE(contact_name, '') = '' "
                        "AND COALESCE(notes, '') = ''"
                    )
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure fournisseurs.in_directory failed: %s", exc)

    # Backfill 2026-07 : les anciens bons de travail (créés avant la refonte,
    # non liés à une correction de projet) passent en bon INTERNE et leurs
    # statuts legacy sont mappés vers le nouveau cycle → ils apparaissent
    # dans le kanban unifié. Idempotent : plus aucun bon « construction » non
    # correction n'est créé (le formulaire crée des « interne », le bon de
    # correction porte origin='correction'), donc les runs suivants ne
    # touchent plus rien.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE bons_travail SET "
                    "kind = 'interne', "
                    "status = CASE "
                    "WHEN status = 'signed' THEN 'complete_a_refacturer' "
                    "WHEN status = 'sent' THEN 'accepte_a_planifier' "
                    "ELSE status END "
                    "WHERE (kind IS NULL OR kind = 'construction') "
                    "AND (origin IS NULL OR origin <> 'correction')"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill bons legacy->interne failed: %s", exc)

    # Renommage 2026-07 : préfixe unifié « BT- » pour TOUTES les références
    # de bons de travail. Les bons créés depuis la Gestion immobilière
    # portaient « BON-AAAAMMJJ-HHMMSS » ; on garde le corps de la référence
    # (traçabilité) et on remplace seulement le préfixe. Idempotent (plus
    # aucune ligne ne matche après le premier run) ; le NOT EXISTS protège
    # la contrainte UNIQUE dans le cas improbable d'une collision.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE bons_travail SET "
                    "reference = 'BT-' || substring(reference FROM 5) "
                    "WHERE reference LIKE 'BON-%' "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM bons_travail b2 "
                    "  WHERE b2.reference = "
                    "    'BT-' || substring(bons_travail.reference FROM 5)"
                    ")"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill bons BON- -> BT- failed: %s", exc)

    # Backfill 2026-08-10 : créateur des bons, recopié du journal
    # d'audit (seuls les bons créés côté Construction y ont une trace ;
    # les autres restent NULL et s'affichent sans « créé par »).
    # Idempotent grâce au IS NULL.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE bons_travail SET created_by_user_id = a.user_id "
                    "FROM ("
                    "  SELECT DISTINCT ON (entity_id) entity_id, user_id "
                    "  FROM audit_logs "
                    "  WHERE action = 'bons-travail.created' "
                    "    AND entity_id IS NOT NULL "
                    "    AND user_id IS NOT NULL "
                    "  ORDER BY entity_id, created_at ASC"
                    ") a "
                    "WHERE bons_travail.id = a.entity_id "
                    "AND bons_travail.created_by_user_id IS NULL"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill bons created_by failed: %s", exc)

    # Nettoyage 2026-08-10 (retour Phil) : les descriptions « Importée
    # depuis Monday » héritées de l'ancien import polluent les fiches
    # d'entreprises — effacées quand la description EST cette mention
    # (one-shot idempotent, ne touche pas les descriptions manuelles).
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE entreprises SET description = NULL "
                    "WHERE description ~* "
                    "'^\\s*importée?s? (automatiquement )?"
                    "(de|depuis) monday'"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("nettoyage descriptions Monday failed: %s", exc)

    # Backfill 2026-08-11 (retour Phil) : les lignes partenaires saisies
    # AVANT l'interconnexion et qui portent exactement le nom d'une de
    # nos INCs sont LIÉES à leur fiche (partner_entreprise_id), puis
    # leurs coordonnées remontent sur la fiche quand elle ne les a pas
    # (fill-only). Récupère les saisies « MGV actionnaire » d'avant le
    # fix. Idempotent.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE entreprise_partners p "
                    "SET partner_entreprise_id = e.id, "
                    "    is_personne_morale = true "
                    "FROM entreprises e "
                    "WHERE p.partner_entreprise_id IS NULL "
                    "  AND p.partner_name IS NOT NULL "
                    "  AND LOWER(TRIM(p.partner_name)) = LOWER(TRIM(e.name)) "
                    "  AND e.id <> p.entreprise_id"
                )
            )
            for col_src, col_dst in (
                ("partner_email", "contact_email"),
                ("partner_telephone", "contact_telephone"),
                ("partner_adresse", "siege_social"),
                ("partner_neq", "neq"),
            ):
                await conn.execute(
                    text(
                        f"UPDATE entreprises e SET {col_dst} = p.{col_src} "
                        "FROM entreprise_partners p "
                        "WHERE p.partner_entreprise_id = e.id "
                        f"  AND e.{col_dst} IS NULL "
                        f"  AND p.{col_src} IS NOT NULL"
                    )
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill partenaires liés aux INCs failed: %s", exc)

    # Backfill 2026-08-11 (organigramme) : les nœuds « compagnie » créés
    # à la main avec exactement le nom d'une de nos INCs sont liés à
    # leur fiche (entreprise_id) → bonne couleur, bouton « Ouvrir la
    # fiche », plus de faux « compagnie externe ». On ne lie que s'il
    # n'existe pas déjà un nœud de cette entreprise dans la même
    # version (pas de doublon silencieux). Idempotent.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE org_nodes n SET entreprise_id = e.id "
                    "FROM entreprises e "
                    "WHERE n.kind = 'company' "
                    "  AND n.entreprise_id IS NULL "
                    "  AND LOWER(TRIM(n.label)) = LOWER(TRIM(e.name)) "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM org_nodes n2 "
                    "    WHERE n2.entreprise_id = e.id "
                    "      AND n2.kind = 'company' "
                    "      AND COALESCE(n2.version_id, -1) = "
                    "          COALESCE(n.version_id, -1)"
                    "  )"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill org_nodes liés aux INCs failed: %s", exc)


async def ensure_raci_tables() -> None:
    """Crée les tables RACI dans leur PROPRE transaction.

    ``init_db`` lance ``create_all`` dans une seule grosse transaction qui
    peut abort (et tout annuler, y compris la création de tables). Les
    tables RACI étant récentes, on les (re)crée ici de façon isolée et
    idempotente — elles existent ainsi toujours, même si ``init_db`` a
    planté.
    """
    import logging

    log = logging.getLogger("db.ensure_raci_tables")
    try:
        from app.db.base import Base
        from app.models.raci import (  # noqa: F401
            RaciActivity,
            RaciCell,
            RaciPerson,
            RaciPole,
            RaciSubsection,
        )

        tables = [
            RaciPole.__table__,
            RaciSubsection.__table__,
            RaciPerson.__table__,
            RaciActivity.__table__,
            RaciCell.__table__,
        ]
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=tables)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_raci_tables failed: %s", exc)


async def ensure_immobilier_aux_tables() -> None:
    """Crée les tables immobilier récentes (relances de loyer) dans leur
    propre transaction, pour survivre à un abort d'``init_db``."""
    import logging

    log = logging.getLogger("db.ensure_immobilier_aux_tables")
    try:
        from sqlalchemy import text

        from app.db.base import Base
        from app.models.immobilier import (  # noqa: F401
            FactureExterne,
            FactureGestion,
            FraisLocatif,
            FraisManuelGestion,
            ImmCommunication,
            ImmDocPersoModele,
            ImmDocTemplate,
            ImmDocument,
            PaiementExterne,
            LocataireCommunication,
            LocationAnnonce,
            LocationDossier,
            LocationVisite,
            RelanceLoyer,
            Releve31,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        RelanceLoyer.__table__,
                        ImmCommunication.__table__,
                        LocataireCommunication.__table__,
                        LocationDossier.__table__,
                        LocationAnnonce.__table__,
                        LocationVisite.__table__,
                        ImmDocument.__table__,
                        ImmDocTemplate.__table__,
                        ImmDocPersoModele.__table__,
                        Releve31.__table__,
                        FraisLocatif.__table__,
                        PaiementExterne.__table__,
                        FactureExterne.__table__,
                        FactureGestion.__table__,
                        FraisManuelGestion.__table__,
                    ],
                )
            )
            # Frais de gestion « complément » (2026-07-23) : un loyer payé
            # en retard après la facturation du mois crée une 2e ligne sur
            # le même (immeuble, mois) → l'ancienne contrainte unique doit
            # sauter sur les bases existantes.
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'uq_facture_gestion_mois'
                        ) THEN
                            ALTER TABLE imm_factures_gestion
                                DROP CONSTRAINT uq_facture_gestion_mois;
                        END IF;
                    END $$;
                    """
                )
            )
        # Relevés 31 : un relevé PAR LOCATAIRE et non par logement
        # (2026-08-13). Deux locataires successifs dans la même année sur
        # le même logement ont chacun droit au leur — l'ancienne
        # contrainte (annee, logement_id) en masquait un. ``bail_id``
        # étant nullable et Postgres ne considérant jamais deux NULL
        # comme égaux, l'unicité passe par un index sur COALESCE.
        # Transaction SÉPARÉE : un échec ici ne doit pas annuler la
        # création des tables ci-dessus (même précaution que
        # ``uq_timesheet_entry_cell_v2``).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                      ALTER TABLE imm_releves31
                        DROP CONSTRAINT IF EXISTS
                        uq_releve31_annee_logement;
                      IF NOT EXISTS (
                        SELECT 1 FROM pg_class
                        WHERE relname = 'uq_releve31_annee_logement_bail'
                      ) THEN
                        CREATE UNIQUE INDEX
                          uq_releve31_annee_logement_bail
                          ON imm_releves31
                          (annee, logement_id, COALESCE(bail_id, 0));
                      END IF;
                    END $$;
                    """
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_immobilier_aux_tables failed: %s", exc)


async def ensure_assistant_tables() -> None:
    """Crée la table des actions de l'assistant IA (cartes d'action à
    confirmer) dans sa propre transaction, pour survivre à un abort
    d'``init_db`` — même filet que ``ensure_immobilier_aux_tables``.
    Idempotent (``create_all`` ne touche pas une table existante)."""
    import logging

    log = logging.getLogger("db.ensure_assistant_tables")
    try:
        from app.db.base import Base
        from app.models.assistant import AssistantAction  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c, tables=[AssistantAction.__table__]
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_assistant_tables failed: %s", exc)


async def ensure_volets_whitelist_migration() -> None:
    """Migration one-shot (permissions v2, 2026-07-24) : les whitelists
    d'emails codées en dur qui AJOUTAIENT les volets entreprises /
    immobilier / investisseur ont été retirées de ``User.volets`` — on
    reporte ces volets dans ``volets_json`` pour que les comptes visés
    gardent exactement le même accès, désormais géré depuis l'app.
    Idempotent (fusion d'ensembles) ; sans effet pour owner/admin (tous
    les volets d'office) ou si le compte n'existe pas."""
    import json
    import logging

    from sqlalchemy import func, select

    log = logging.getLogger("db.ensure_volets_whitelist_migration")
    legacy: dict[str, list[str]] = {
        "stevengiguere1993@gmail.com": [
            "entreprises", "immobilier", "investisseur",
        ],
        "sgiguere@immohorizon.com": [
            "entreprises", "immobilier", "investisseur",
        ],
        "pmeuser@immohorizon.com": [
            "entreprises", "immobilier", "investisseur",
        ],
        "philippe.meuser@immohorizon.com": [
            "entreprises", "immobilier", "investisseur",
        ],
        "mvilliard@immohorizon.com": ["entreprises"],
    }
    try:
        from app.models.user import DEFAULT_VOLETS, User

        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(User).where(
                        func.lower(User.email).in_(list(legacy.keys()))
                    )
                )
            ).scalars().all()
            changed = False
            for u in rows:
                extras = legacy.get((u.email or "").strip().lower(), [])
                if not extras or u.role in ("owner", "admin"):
                    continue
                try:
                    base = json.loads(u.volets_json) if u.volets_json else None
                    if not isinstance(base, list):
                        base = None
                except Exception:  # noqa: BLE001
                    base = None
                if base is None:
                    base = list(DEFAULT_VOLETS)
                merged = sorted(set(base) | set(extras))
                if sorted(set(base)) != merged:
                    u.volets_json = json.dumps(merged)
                    changed = True
                    log.info(
                        "volets whitelist -> volets_json pour %s : %s",
                        u.email, merged,
                    )
            if changed:
                await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_volets_whitelist_migration failed: %s", exc)


async def ensure_qbo_connections_table() -> None:
    """Crée les tables QuickBooks récentes dans leur propre transaction :
    ``qbo_connections`` (multi-compagnies, scopes entreprise/immobilier)
    et ``qbo_monthly_invoices`` (facture mensuelle ouverte par client,
    2026-07-27). La table historique ``qbo_tokens`` (Construction) n'est
    pas touchée."""
    import logging

    log = logging.getLogger("db.ensure_qbo_connections_table")
    try:
        from app.db.base import Base
        from app.models.qbo_connection import QboConnection  # noqa: F401
        from app.models.qbo_monthly_invoice import (  # noqa: F401
            QboMonthlyInvoice,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        QboConnection.__table__,
                        QboMonthlyInvoice.__table__,
                    ],
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_qbo_connections_table failed: %s", exc)


async def ensure_validation_bancaire_tables() -> None:
    """Crée les tables de la VALIDATION BANCAIRE des loyers (2026-08-14)
    dans leur propre transaction : ``qbo_comptes_loyers`` (compte du plan
    comptable), ``qbo_compte_immeubles`` (lien N-N compte ↔ immeubles —
    v2, un compte QBO peut couvrir plusieurs immeubles),
    ``qbo_transactions_loyers`` (écritures QBO publiées, importées en
    lecture seule) et ``qbo_alias_payeurs`` (alias appris à la
    confirmation d'un rapprochement ambigu). Idempotent (create_all
    checkfirst) — survit à un abort d'``init_db``.

    Migration v2 (idempotente) : les liens 1-1 ``immeuble_id`` déjà
    confirmés par Phil en prod sont RECOPIÉS dans la table de liens puis
    la colonne legacy est remise à NULL (rerun = no-op). Aucune
    confirmation n'est perdue."""
    import logging

    from sqlalchemy import text

    log = logging.getLogger("db.ensure_validation_bancaire_tables")
    try:
        from app.db.base import Base
        from app.models.qbo_loyers import (  # noqa: F401
            QboAliasPayeur,
            QboCompteImmeuble,
            QboCompteLoyer,
            QboTransactionLoyer,
            QboVerifManuelle,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        QboCompteLoyer.__table__,
                        QboCompteImmeuble.__table__,
                        QboTransactionLoyer.__table__,
                        QboAliasPayeur.__table__,
                        QboVerifManuelle.__table__,
                    ],
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_validation_bancaire_tables failed: %s", exc)
        return

    # ── Migration 1-1 → N-N (transaction dédiée, idempotente) ──────────
    # Recopie les mappings confirmés (immeuble_id NOT NULL) dans
    # qbo_compte_immeubles, puis neutralise la colonne legacy : au boot
    # suivant, plus rien à migrer. ON CONFLICT = rejouable sans doublon.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO qbo_compte_immeubles (compte_id, "
                    "immeuble_id, created_at) "
                    "SELECT c.id, c.immeuble_id, NOW() "
                    "FROM qbo_comptes_loyers c "
                    "WHERE c.immeuble_id IS NOT NULL "
                    "ON CONFLICT (compte_id, immeuble_id) DO NOTHING"
                )
            )
            await conn.execute(
                text(
                    "UPDATE qbo_comptes_loyers SET immeuble_id = NULL "
                    "WHERE immeuble_id IS NOT NULL"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ensure_validation_bancaire_tables migration 1-1→N-N "
            "failed: %s", exc,
        )


async def ensure_timesheet_tables() -> None:
    """Crée les tables Feuille de temps (Gestion d'entreprise) dans leur
    propre transaction, pour survivre à un abort d'``init_db``."""
    import logging

    log = logging.getLogger("db.ensure_timesheet_tables")
    try:
        from app.db.base import Base
        from app.models.timesheet import (  # noqa: F401
            Timesheet,
            TimesheetCompany,
            TimesheetEntry,
            TimesheetReglement,
            TimesheetUserRate,
        )

        tables = [
            TimesheetCompany.__table__,
            Timesheet.__table__,
            TimesheetEntry.__table__,
            TimesheetUserRate.__table__,
            TimesheetReglement.__table__,
        ]
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=tables)
            )
        # La cellule est maintenant (feuille, compagnie, jour, refacturable) :
        # sur une base existante il faut ajouter la colonne PUIS remplacer
        # l'ancienne contrainte unique à 3 colonnes (sinon violation dès
        # qu'une même case a des heures refact + non refact).
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                      ALTER TABLE timesheet_entries
                        ADD COLUMN IF NOT EXISTS refacturable
                        BOOLEAN NOT NULL DEFAULT TRUE;
                      -- Heures NR permises par compagnie : création +
                      -- backfill ONE-SHOT (MGV Développement seulement),
                      -- pour ne pas écraser les réglages ultérieurs.
                      IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'timesheet_companies'
                          AND column_name = 'heures_nr_autorisees'
                      ) THEN
                        ALTER TABLE timesheet_companies
                          ADD COLUMN heures_nr_autorisees
                          BOOLEAN NOT NULL DEFAULT FALSE;
                        UPDATE timesheet_companies
                          SET heures_nr_autorisees = TRUE
                          WHERE label ILIKE 'MGV D_veloppement';
                      END IF;
                      IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'uq_timesheet_entry_cell_v2'
                      ) THEN
                        ALTER TABLE timesheet_entries
                          DROP CONSTRAINT IF EXISTS uq_timesheet_entry_cell;
                        ALTER TABLE timesheet_entries
                          ADD CONSTRAINT uq_timesheet_entry_cell_v2
                          UNIQUE (timesheet_id, company_id, day_index,
                                  refacturable);
                      END IF;
                    END $$;
                    """
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_timesheet_tables failed: %s", exc)


async def ensure_project_corrections_tables() -> None:
    """Crée la table `project_corrections` (corrections/améliorations d'un
    projet, Flux A) dans sa propre transaction, pour survivre à un abort
    d'``init_db``. Le PR d'origine avait ajouté la colonne
    ``projects.correction_status`` à ``ensure_critical_columns`` mais avait
    omis ce filet pour la NOUVELLE table → « relation project_corrections
    does not exist » en prod (POST /projects/{id}/corrections en 500)."""
    import logging

    log = logging.getLogger("db.ensure_project_corrections_tables")
    try:
        from app.db.base import Base
        from app.models.project_correction import (  # noqa: F401
            ProjectCorrection,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c, tables=[ProjectCorrection.__table__]
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_project_corrections_tables failed: %s", exc)


async def ensure_relance_tables() -> None:
    """Crée les tables du moteur de relances (séquence de cadence) dans leur
    PROPRE transaction, pour survivre à un abort d'``init_db``.

    ``init_db`` lance ``create_all`` dans une seule grosse transaction : si
    une étape échoue, tout est annulé, y compris la création de ces tables
    récentes. Sans ce filet isolé, ``cadence_steps`` / ``relance_plans`` /
    ``relance_items`` manquent en prod → « relation relance_items does not
    exist » → POST /api/v1/relances/plan/{id} plante en HTTP 500 (« Ajout
    échoué (HTTP 500) » sur la carte « Relances prévues » d'un prospect).
    Même classe de bug que ``ensure_project_corrections_tables``.
    """
    import logging

    log = logging.getLogger("db.ensure_relance_tables")
    try:
        from app.db.base import Base
        from app.models.cadence_step import CadenceStep  # noqa: F401
        from app.models.relance_item import RelanceItem  # noqa: F401
        from app.models.relance_plan import RelancePlan  # noqa: F401

        tables = [
            CadenceStep.__table__,
            RelancePlan.__table__,
            RelanceItem.__table__,
        ]
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=tables)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_relance_tables failed: %s", exc)


async def ensure_role_permissions_tables() -> None:
    """Crée la table `role_permissions` (permissions configurables) dans sa
    PROPRE transaction (filet contre un abort d'``init_db``), puis SÈME les
    défauts = le comportement actuel codé en dur, pour chaque capacité du
    registre. Le seed est idempotent (ON CONFLICT DO NOTHING) : il ne
    réécrase jamais un choix de l'owner, et n'insère que les capacités
    manquantes → aucun changement visible tant que l'owner ne modifie rien.
    Voir app/core/capabilities.py et app/services/permissions_service.py."""
    import logging

    from sqlalchemy import text

    log = logging.getLogger("db.ensure_role_permissions_tables")
    try:
        from app.core.access_registry import PAGE_KEY_PREFIX, PAGES
        from app.core.capabilities import CAPABILITIES
        from app.db.base import Base
        from app.models.role_permission import RolePermission  # noqa: F401
        from app.models.user_access_override import (  # noqa: F401
            UserAccessOverride,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        RolePermission.__table__,
                        UserAccessOverride.__table__,
                    ],
                )
            )
            # Seed des défauts (rôle minimum actuel de chaque capacité).
            for cap in CAPABILITIES:
                await conn.execute(
                    text(
                        "INSERT INTO role_permissions (capability, min_role) "
                        "VALUES (:cap, :role) ON CONFLICT (capability) DO NOTHING"
                    ),
                    {"cap": cap.id, "role": cap.default_min_role},
                )
            # Seed des PAGES du registre central (refonte permissions
            # 2026-07) : même table, clé préfixée `page:`. Défauts = les
            # seuils des sidebars avant la refonte → aucun changement
            # visible tant que l'owner ne modifie rien.
            for page in PAGES:
                await conn.execute(
                    text(
                        "INSERT INTO role_permissions (capability, min_role) "
                        "VALUES (:cap, :role) ON CONFLICT (capability) DO NOTHING"
                    ),
                    {
                        "cap": f"{PAGE_KEY_PREFIX}{page.key}",
                        "role": page.default_min_role,
                    },
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_role_permissions_tables failed: %s", exc)


async def ensure_permissions_defaults_metier() -> None:
    """Migration one-shot (permissions v2, 2026-07-24) : passe les seuils
    SEMÉS des pages immobilier + données financières prospection de
    « employé » à « gestionnaire » (logique métier — la gestion locative
    et les analyses d'achat sont le travail des gestionnaires).

    Le seed initial (ON CONFLICT DO NOTHING) a figé les vieux défauts en
    DB : changer le défaut du registre ne suffit pas. On ne touche QUE
    les lignes encore à l'ancien défaut (un choix explicite de l'owner
    est préservé) et une SENTINELLE garantit une exécution unique — si
    l'owner remet « employé » plus tard, on ne le réécrase jamais."""
    import logging

    from sqlalchemy import text

    log = logging.getLogger("db.ensure_permissions_defaults_metier")
    sentinel = "migration:defaults_metier_2026_07_24"
    upgrades = [
        "page:immobilier.vue_ensemble",
        "page:immobilier.immeubles",
        "page:immobilier.logements",
        "page:immobilier.locataires",
        # « Baux » et « Paiements » : les deux clés (les URL des deux pages
        # ont été remises d'aplomb le 2026-08-14 — la page Paiements est
        # passée de la clé `immobilier.baux` à `immobilier.paiements`).
        "page:immobilier.baux",
        "page:immobilier.paiements",
        "page:immobilier.locations",
        "page:immobilier.modeles_documents",
        "page:immobilier.finances",
        "page:immobilier.renouvellements",
        "page:immobilier.depots",
        "page:immobilier.bons_travail",
        "page:prospection.analyses",
        "page:prospection.pipeline",
        "page:prospection.dashboard",
    ]
    try:
        async with engine.begin() as conn:
            done = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM role_permissions "
                        "WHERE capability = :s"
                    ),
                    {"s": sentinel},
                )
            ).first()
            if done:
                return
            for key in upgrades:
                await conn.execute(
                    text(
                        "UPDATE role_permissions SET min_role = 'manager' "
                        "WHERE capability = :k AND min_role = 'employee'"
                    ),
                    {"k": key},
                )
            await conn.execute(
                text(
                    "INSERT INTO role_permissions (capability, min_role) "
                    "VALUES (:s, 'owner') "
                    "ON CONFLICT (capability) DO NOTHING"
                ),
                {"s": sentinel},
            )
            log.info("Défauts métier appliqués (%d seuils)", len(upgrades))
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_permissions_defaults_metier failed: %s", exc)


async def ensure_contrat_gestion_tables() -> None:
    """Crée les tables du Contrat de gestion (`contrats_gestion` +
    `contrat_gestion_template`) dans leur PROPRE transaction, puis sème
    le gabarit par défaut (singleton id=1) s'il est absent.

    Le seed est idempotent (ON CONFLICT (id) DO NOTHING) : il n'écrase
    jamais un gabarit édité par Phil. Voir app/models/contrat_gestion.py
    et app/services/contrat_gestion_template.py."""
    import logging

    from sqlalchemy import text

    log = logging.getLogger("db.ensure_contrat_gestion_tables")
    try:
        from app.db.base import Base
        from app.models.contrat_gestion import (  # noqa: F401
            ContratGestion,
            ContratGestionTemplate,
        )
        from app.services.contrat_gestion_template import (
            DEFAULT_TEMPLATE_MARKDOWN,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        ContratGestion.__table__,
                        ContratGestionTemplate.__table__,
                    ],
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO contrat_gestion_template (id, corps_markdown) "
                    "VALUES (1, :body) ON CONFLICT (id) DO NOTHING"
                ),
                {"body": DEFAULT_TEMPLATE_MARKDOWN},
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_contrat_gestion_tables failed: %s", exc)


async def ensure_esign_tables() -> None:
    """Crée les tables du module eSign (signature électronique de
    documents, pôle Gestion d'entreprise) dans leur PROPRE transaction :
    `esign_documents`, `esign_signers`, `esign_fields`, `esign_events`,
    plus les tables V2 (`esign_templates`, `esign_template_fields`,
    `esign_observers`, `esign_attachments`) et les colonnes additives
    V2 (expiration, rappels automatiques) sur les tables V1 déjà
    créées en prod.

    Voir app/models/esign.py — pattern identique aux autres ensure_*."""
    import logging

    from sqlalchemy import text

    log = logging.getLogger("db.ensure_esign_tables")
    try:
        from app.db.base import Base
        from app.models.esign import (  # noqa: F401
            EsignAttachment,
            EsignDocument,
            EsignEvent,
            EsignField,
            EsignObserver,
            EsignSigner,
            EsignTemplate,
            EsignTemplateField,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        EsignDocument.__table__,
                        EsignSigner.__table__,
                        EsignField.__table__,
                        EsignEvent.__table__,
                        EsignTemplate.__table__,
                        EsignTemplateField.__table__,
                        EsignObserver.__table__,
                        EsignAttachment.__table__,
                    ],
                )
            )
            # Colonnes V2 additives — create_all n'ALTER jamais une
            # table existante.
            for tbl, col, sql_type in (
                ("esign_documents", "expires_at", "TIMESTAMPTZ"),
                ("esign_documents", "reminder_days", "INTEGER"),
                (
                    "esign_signers",
                    "auto_reminder_count",
                    "INTEGER NOT NULL DEFAULT 0",
                ),
                ("esign_signers", "last_reminder_at", "TIMESTAMPTZ"),
            ):
                await conn.execute(
                    text(
                        f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS "
                        f"{col} {sql_type}"
                    )
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_esign_tables failed: %s", exc)


async def ensure_invest_portal_tables() -> None:
    """Crée les tables du Portail Investisseur v2 (participation par
    compagnie) dans leur PROPRE transaction : `inv_participations`,
    `inv_flux`, `inv_projet_profils`, `inv_documents`, `inv_jalons`.

    Voir app/models/invest_portal.py — pattern identique aux autres."""
    import logging

    log = logging.getLogger("db.ensure_invest_portal_tables")
    try:
        from app.db.base import Base
        from app.models.invest_portal import (  # noqa: F401
            InvestDocument,
            InvestFlux,
            InvestJalon,
            InvestParticipation,
            InvestProjetProfil,
        )

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        InvestParticipation.__table__,
                        InvestFlux.__table__,
                        InvestProjetProfil.__table__,
                        InvestDocument.__table__,
                        InvestJalon.__table__,
                    ],
                )
            )
            # Colonnes additives (create_all n'ALTER jamais une table
            # existante).
            from sqlalchemy import text as _text

            await conn.execute(
                _text(
                    "ALTER TABLE inv_projet_profils ADD COLUMN IF NOT "
                    "EXISTS avances_actionnaires NUMERIC(12,2)"
                )
            )
            await conn.execute(
                _text(
                    "ALTER TABLE inv_projet_profils ADD COLUMN IF NOT "
                    "EXISTS qbo_sync_at TIMESTAMPTZ"
                )
            )
            await conn.execute(
                _text(
                    "ALTER TABLE inv_projet_profils ADD COLUMN IF NOT "
                    "EXISTS qbo_sync_json TEXT"
                )
            )
            await conn.execute(
                _text(
                    "ALTER TABLE inv_projet_profils ADD COLUMN IF NOT "
                    "EXISTS show_budget BOOLEAN NOT NULL DEFAULT TRUE"
                )
            )
            await conn.execute(
                _text(
                    "ALTER TABLE inv_projet_profils ADD COLUMN IF NOT "
                    "EXISTS drive_folder_url VARCHAR(1000)"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_invest_portal_tables failed: %s", exc)


async def init_db() -> None:
    """
    Initialize database tables.

    Note: In production, use Alembic migrations instead.
    This is primarily for development/testing.
    """
    from app.db.base import Base
    from sqlalchemy import text

    log = logging.getLogger("app.db.init_db")

    # create_all dans sa PROPRE transaction → committé indépendamment des
    # ALTER additifs ci-dessous. Sinon, un ALTER raté annule TOUTE la
    # transaction, y compris la création des nouvelles tables (ex.
    # cadence_steps, relance_items…), qui se retrouvent absentes en prod.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.begin() as conn:
        # Idempotent column additions for schema evolutions where a new
        # column is added to an already-existing table. `create_all` only
        # creates missing tables, never alters existing ones, so we patch
        # those gaps here until we introduce Alembic migrations.
        additive_columns = (
            ("sous_traitants", "competence_rating", "INTEGER"),
            ("sous_traitants", "availability_rating", "INTEGER"),
            ("sous_traitants", "punctuality_rating", "INTEGER"),
            ("sous_traitants", "quality_rating", "INTEGER"),
            ("sous_traitants", "region", "VARCHAR(255)"),
            ("bons_travail", "origin", "VARCHAR(32)"),
            ("sous_traitants", "charges_travel_fee", "BOOLEAN"),
            ("sous_traitants", "travel_fee_amount", "NUMERIC(10, 2)"),
            ("sous_traitants", "travel_fee_notes", "TEXT"),
            ("fournisseurs", "payment_terms_days", "INTEGER"),
            ("fournisseurs", "address", "VARCHAR(500)"),
            ("fournisseurs", "qbo_vendor_id", "VARCHAR(64)"),
            ("achats", "due_at", "TIMESTAMP WITH TIME ZONE"),
            ("achats", "qbo_bill_payment_id", "VARCHAR(64)"),
            ("factures", "next_reminder_at", "TIMESTAMP WITH TIME ZONE"),
            ("voice_calls", "verbatim_transcript", "TEXT"),
            ("imm_immeubles", "urgence_phone", "VARCHAR(32)"),
            ("soumissions", "qbo_estimate_id", "VARCHAR(64)"),
            ("soumissions", "qbo_doc_number", "VARCHAR(64)"),
            ("soumissions", "qbo_sync_token", "VARCHAR(32)"),
            (
                "soumissions",
                "client_opened_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "soumissions",
                "client_last_opened_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "soumissions",
                "client_open_count",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "soumissions",
                "contractor_opened_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "soumissions",
                "contractor_last_opened_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "soumissions",
                "contractor_open_count",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            ("factures", "qbo_invoice_id", "VARCHAR(64)"),
            ("factures", "qbo_doc_number", "VARCHAR(64)"),
            ("factures", "qbo_sync_token", "VARCHAR(32)"),
            ("projects", "contact_request_id", "INTEGER"),
            ("projects", "soumission_id", "INTEGER"),
            ("projects", "status", "VARCHAR(32) NOT NULL DEFAULT 'planned'"),
            ("projects", "address", "VARCHAR(500)"),
            ("projects", "description", "TEXT"),
            ("projects", "notes", "TEXT"),
            ("projects", "start_date", "DATE"),
            ("projects", "end_date", "DATE"),
            ("projects", "budget", "NUMERIC(12, 2)"),
            ("projects", "updated_at", "TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()"),
            ("punches", "contact_request_id", "INTEGER"),
            ("clients", "email", "VARCHAR(320)"),
            ("clients", "phone", "VARCHAR(50)"),
            ("clients", "address", "VARCHAR(500)"),
            ("clients", "notes", "TEXT"),
            ("clients", "contact_request_id", "INTEGER"),
            ("clients", "language", "VARCHAR(8) NOT NULL DEFAULT 'fr'"),
            ("clients", "is_company", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("clients", "representative", "VARCHAR(255)"),
            ("project_phase_assignees", "hourly_billed", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("project_phase_assignees", "worker_count", "INTEGER NOT NULL DEFAULT 1"),
            ("achats", "receipt_image", "BYTEA"),
            ("achats", "receipt_image_content_type", "VARCHAR(100)"),
            ("achats", "amount_tps", "NUMERIC(12,2)"),
            ("achats", "amount_tvq", "NUMERIC(12,2)"),
            ("factures", "last_reminder_at", "TIMESTAMP WITH TIME ZONE"),
            ("factures", "reminder_count", "INTEGER NOT NULL DEFAULT 0"),
            # FactureItem.kind — service|extra|rabais|frais. « extra » =
            # hors soumission, ne réduit pas le « reste à facturer ».
            (
                "facture_items",
                "kind",
                "VARCHAR(16) NOT NULL DEFAULT 'service'",
            ),
            ("soumissions", "signature_token", "VARCHAR(64)"),
            ("soumissions", "signed_name", "VARCHAR(255)"),
            ("soumissions", "signed_ip", "VARCHAR(64)"),
            ("bons_travail", "signature_token", "VARCHAR(64)"),
            ("soumissions", "property_address", "VARCHAR(500)"),
            (
                "soumissions",
                "project_skip_backfill",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("soumission_items", "tps_applicable", "BOOLEAN NOT NULL DEFAULT TRUE"),
            ("soumission_items", "tvq_applicable", "BOOLEAN NOT NULL DEFAULT TRUE"),
            ("soumission_items", "kind", "VARCHAR(16) NOT NULL DEFAULT 'service'"),
            ("contact_requests", "kanban_column", "VARCHAR(64)"),
            ("soumissions", "signature_image", "BYTEA"),
            ("soumissions", "signature_image_content_type", "VARCHAR(100)"),
            ("bons_travail", "signature_image", "BYTEA"),
            ("bons_travail", "signature_image_content_type", "VARCHAR(100)"),
            # ── Bon de travail INTERNE (entretien de nos immeubles) ──────
            ("bons_travail", "kind", "VARCHAR(16) NOT NULL DEFAULT 'construction'"),
            ("bons_travail", "owner_entreprise_id", "INTEGER"),
            ("bons_travail", "immeuble_id", "INTEGER"),
            ("bons_travail", "logement_id", "INTEGER"),
            ("bons_travail", "executant_type", "VARCHAR(16)"),
            ("bons_travail", "sous_traitant_id", "INTEGER"),
            ("bons_travail", "marge_pct", "NUMERIC(5, 2) NOT NULL DEFAULT 0"),
            ("bon_items", "item_type", "VARCHAR(16) NOT NULL DEFAULT 'materiel'"),
            ("bon_items", "cost_rate", "NUMERIC(12, 2)"),
            ("bon_items", "bill_rate", "NUMERIC(12, 2)"),
            ("bon_items", "marge_pct", "NUMERIC(5, 2)"),
            ("bon_items", "cost_total", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
            ("bon_items", "employe_id", "INTEGER"),
            ("bon_items", "sous_traitant_id", "INTEGER"),
            ("punches", "bon_travail_id", "INTEGER"),
            ("users", "role", "VARCHAR(16) NOT NULL DEFAULT 'employee'"),
            ("project_tasks", "phase_id", "INTEGER"),
            ("agenda_events", "contact_request_id", "INTEGER"),
            ("agenda_events", "reminder_sent_at", "TIMESTAMP WITH TIME ZONE"),
            ("agenda_events", "confirmation_sent_at", "TIMESTAMP WITH TIME ZONE"),
            ("soumission_items", "cost_per_unit", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
            ("service_templates", "default_cost_per_unit", "NUMERIC(12, 2)"),
            ("service_template_items", "default_cost_per_unit", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
            ("measurement_snapshots", "template_type", "VARCHAR(32)"),
            ("measurement_snapshots", "template_data_json", "TEXT"),
            ("users", "calendar_feed_token", "VARCHAR(64)"),
            (
                "users",
                "must_change_password",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("users", "first_name", "VARCHAR(100)"),
            ("users", "last_name", "VARCHAR(100)"),
            ("users", "avatar_image", "BYTEA"),
            ("users", "avatar_content_type", "VARCHAR(64)"),
            ("users", "profile_color", "VARCHAR(16)"),
            ("soumissions", "client_note", "TEXT"),
            ("factures", "internal_notes", "TEXT"),
            ("factures", "client_note", "TEXT"),
            # Facture finale + signature électronique du client.
            ("factures", "is_final", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("factures", "signature_token", "VARCHAR(64)"),
            ("factures", "signed_name", "VARCHAR(255)"),
            ("factures", "signed_ip", "VARCHAR(64)"),
            ("factures", "signed_at", "TIMESTAMP WITH TIME ZONE"),
            ("factures", "signature_image", "BYTEA"),
            (
                "factures",
                "signature_image_content_type",
                "VARCHAR(100)",
            ),
            ("employes", "address", "VARCHAR(500)"),
            ("employes", "license_number", "VARCHAR(64)"),
            ("employes", "emergency_contact_name", "VARCHAR(255)"),
            ("employes", "emergency_contact_phone", "VARCHAR(50)"),
            (
                "employes",
                "is_ccq",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("employes", "cnesst_rate", "NUMERIC(6, 4)"),
            ("employes", "ccq_rate", "NUMERIC(6, 4)"),
            ("employes", "employeur_d_url", "VARCHAR(500)"),
            ("project_phases", "assignee_employe_id", "INTEGER"),
            ("project_phases", "assignee_sous_traitant_id", "INTEGER"),
            ("project_phases", "start_time", "TIME"),
            ("projects", "estimated_hours_override", "NUMERIC(8, 2)"),
            # Auto-classification des achats QB par fournisseur.
            ("fournisseurs", "qbo_expense_account", "VARCHAR(255)"),
            (
                "leave_requests",
                "kind",
                "VARCHAR(16) NOT NULL DEFAULT 'vacation'",
            ),
            # QBO OAuth: colonnes ajoutées pour le flow /qbo/connect.
            ("qbo_tokens", "realm_id", "VARCHAR(64)"),
            ("qbo_tokens", "environment", "VARCHAR(16)"),
            ("qbo_tokens", "company_name", "VARCHAR(255)"),
            ("qbo_tokens", "connected_by_user_id", "INTEGER"),
            (
                "qbo_tokens",
                "connected_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Liaison client ↔ QBO Customer (push manuel + évite les
            # doublons à chaque re-push).
            ("clients", "qbo_customer_id", "VARCHAR(64)"),
            # Compteur PO + numérotation QBO sur les achats.
            (
                "numbering_counters",
                "next_po_number",
                "INTEGER NOT NULL DEFAULT 1",
            ),
            ("achats", "qbo_bill_id", "VARCHAR(64)"),
            ("achats", "qbo_doc_number", "VARCHAR(64)"),
            ("achats", "qbo_sync_token", "VARCHAR(32)"),
            # Workflow PO complet : assigné à l'employé qui va chercher
            # la marchandise + mode de paiement (routage QB Bill vs
            # Purchase).
            ("achats", "assigned_employe_id", "INTEGER"),
            ("achats", "payment_method", "VARCHAR(32)"),
            # Refonte PO/Achat (Avril 2026) — Achat = vraie transaction.
            ("achats", "purchase_order_id", "INTEGER"),
            ("achats", "supplier_invoice_number", "VARCHAR(64)"),
            ("achats", "invoice_date", "DATE"),
            ("achats", "paid_at", "TIMESTAMP WITH TIME ZONE"),
            # Prospection : scoring auto + tags.
            (
                "prospection_leads",
                "score",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            ("prospection_leads", "tags", "TEXT"),
            # REQ : téléphone du siège social (du CSV REQ).
            ("req_companies", "telephone", "VARCHAR(32)"),
            # CRM : assignation d'un lead à un prospecteur.
            ("contact_requests", "assigned_to_user_id", "INTEGER"),
            # Prospection — données financières et fiscales.
            ("prospection_leads", "purchase_price", "NUMERIC(14, 2)"),
            ("prospection_leads", "purchase_date", "DATE"),
            ("prospection_leads", "mortgage_balance", "NUMERIC(14, 2)"),
            (
                "prospection_leads",
                "tax_delinquent",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("prospection_leads", "tax_year_paid", "INTEGER"),
            ("prospection_leads", "tax_amount", "NUMERIC(10, 2)"),
            ("prospection_leads", "mailing_address", "VARCHAR(500)"),
            ("prospection_leads", "recontact_at", "DATE"),
            (
                "prospection_leads",
                "deal_strategy",
                "VARCHAR(16) NOT NULL DEFAULT 'undecided'",
            ),
            ("prospection_leads", "offer_amount", "NUMERIC(14, 2)"),
            ("prospection_leads", "assignment_price", "NUMERIC(14, 2)"),
            ("prospection_leads", "monday_item_id", "VARCHAR(32)"),
            # Multi-volet : un user peut avoir accès à construction,
            # prospection ou les deux. NULL = backward compat (tous).
            ("users", "volets_json", "TEXT"),
            # Agenda partagé entre volets : scope distingue les events
            # construction (par défaut) des events prospection.
            (
                "agenda_events",
                "scope",
                "VARCHAR(16) NOT NULL DEFAULT 'construction'",
            ),
            ("agenda_events", "lead_id", "INTEGER"),
            ("agenda_events", "phase_id", "INTEGER"),
            ("agenda_events", "assignee_user_id", "INTEGER"),
            # EvalWeb : propriétaires scrapés à la demande, cachés
            # par matricule pour éviter les re-scrapes.
            ("mtl_property_units", "owners_json", "TEXT"),
            ("mtl_property_units", "owners_fetched_at", "TIMESTAMP WITH TIME ZONE"),
            # Comparables loyers : enrichissements parser
            ("rental_listings", "quartier", "VARCHAR(64)"),
            (
                "rental_listings",
                "is_renovated",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            # Agenda : permission spéciale d'assigner des RDV à d'autres
            # users (cas Zachary) + token pour le lien d'auto-confirmation
            # email.
            (
                "users",
                "can_assign_others",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("users", "agenda_invite_token", "VARCHAR(64)"),
            # Réinitialisation de mot de passe en libre-service
            # (2026-08-27) : jeton haché + échéance.
            # Ventilation du coûtant des lignes de soumission
            # (2026-08-27) : main-d'œuvre / matériaux.
            ("soumission_items", "cost_labor_per_unit", "NUMERIC(12,2)"),
            ("soumission_items", "cost_material_per_unit",
             "NUMERIC(12,2)"),
            ("users", "reset_token_hash", "VARCHAR(64)"),
            ("users", "reset_token_expires_at",
             "TIMESTAMP WITH TIME ZONE"),
            # Préférence de thème portail. 'light' (noir sur blanc) =
            # défaut. 'dark' = blanc sur noir (legacy). N'affecte que
            # le portail interne — la landing publique reste dark.
            (
                "users",
                "theme_preference",
                "VARCHAR(8) NOT NULL DEFAULT 'light'",
            ),
            # AgendaEvent : champs pour l'invitation email + confirmation
            (
                "agenda_events",
                "invitation_sent_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "agenda_events",
                "invitation_confirmed_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Région pour distinguer MTL / Laval / Rive-Sud / Rive-Nord
            ("mtl_property_units", "region", "VARCHAR(16)"),
            # Immeuble cover photo en blob (upload direct, pas seulement URL).
            ("imm_immeubles", "cover_photo_blob", "BYTEA"),
            ("imm_immeubles", "cover_photo_content_type", "VARCHAR(64)"),
            # Partenaire externe (sans user_id) ou notes additionnelles.
            ("entreprise_partners", "partner_name", "VARCHAR(255)"),
            ("entreprise_partners", "partner_email", "VARCHAR(320)"),
            ("entreprise_partners", "partner_notes", "TEXT"),
            # Arrondissement (Ville de MTL) — dérivé via cross-référence
            # avec le dataset public « Adresses Civiques de Montréal ».
            ("mtl_property_units", "arrondissement", "VARCHAR(64)"),
            # Priorité côté UI (Monday-style) sur les tâches d'entreprise.
            (
                "entreprise_taches",
                "priority",
                "VARCHAR(16) NOT NULL DEFAULT 'moyenne'",
            ),
            # Ordre d'affichage des entreprises dans la sidebar.
            (
                "entreprises",
                "position",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            # Ordre d'affichage des deals (Pipeline) dans la sidebar
            # Prospection — drag & drop similaire à Mes entreprises.
            (
                "prospection_deals",
                "position",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            # Champs « riches » alignés sur EntrepriseTache pour que la
            # fiche détaillée des tâches soit identique sur les deux
            # volets (Pipeline et Entreprise).
            ("prospection_deal_tasks", "departement", "VARCHAR(64)"),
            ("prospection_deal_tasks", "recurrence", "VARCHAR(16)"),
            ("prospection_deal_tasks", "impact", "INTEGER"),
            ("prospection_deal_tasks", "confidence", "INTEGER"),
            ("prospection_deal_tasks", "effort", "INTEGER"),
            # Position manuelle — pour le drag & drop dans le tableau
            # de tâches d'entreprise (sinon classement par score).
            (
                "entreprise_taches",
                "position",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            # Catalogue immeuble scopé par parent (entreprise OU deal).
            # Quand on crée un immeuble depuis le picker d'une fiche de
            # tâche, il n'apparaît que dans le catalogue de cette même
            # fiche. Les deux sont nullables ; au plus un est rempli à
            # la fois (immeuble appartient à une entreprise OU un deal).
            ("imm_immeubles", "owner_entreprise_id", "INTEGER"),
            ("imm_immeubles", "owner_deal_id", "INTEGER"),
            # Gestion externe : immeuble géré par une compagnie tierce →
            # exclu des flux opérationnels (loyers, renouvellements,
            # dépôts, relances). Nom/contact du gestionnaire tiers.
            ("imm_immeubles", "gestion_externe",
             "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("imm_immeubles", "gestionnaire_externe_nom", "VARCHAR(255)"),
            ("imm_immeubles", "gestionnaire_externe_contact", "VARCHAR(255)"),
            # Finances immobilier 2026-07 : composition des intérêts
            # ('semi'|'mensuelle') persistée sur l'hypothèque, évaluation
            # de référence pour le calcul d'équité, dépenses en % des
            # loyers mensuels + taxables (TPS/TVQ ×1.14975).
            ("imm_hypotheques", "composition_interets", "VARCHAR(16)"),
            ("imm_evaluations", "is_reference",
             "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("immeuble_depenses", "is_pourcentage",
             "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("immeuble_depenses", "taxable",
             "BOOLEAN NOT NULL DEFAULT FALSE"),
            # Drive : URL du dossier Google Drive lié à l'entité.
            # Bouton « Drive » dans le header de la fiche y mène.
            # NULL = pas configuré.
            ("entreprises", "drive_folder_url", "VARCHAR(1024)"),
            ("prospection_leads", "drive_folder_url", "VARCHAR(1024)"),
            ("prospection_deals", "drive_folder_url", "VARCHAR(1024)"),
            # Modèles de tâches récurrentes : statut par défaut +
            # immeubles à attacher à chaque tâche matérialisée.
            (
                "entreprise_tache_templates",
                "default_status",
                "VARCHAR(16) NOT NULL DEFAULT 'todo'",
            ),
            ("entreprise_tache_templates", "immeuble_ids_json", "TEXT"),
            # Capture d'écran optionnelle sur les signalements de bug.
            ("help_requests", "screenshot_blob", "BYTEA"),
            ("help_requests", "screenshot_content_type", "VARCHAR(64)"),
            ("help_requests", "resolution_notes", "TEXT"),
            # Type de soumission (forfaitaire / estime). Défaut
            # "forfaitaire" (95% des cas en construction).
            (
                "soumissions",
                "pricing_kind",
                "VARCHAR(16) NOT NULL DEFAULT 'forfaitaire'",
            ),
            # Contrat d'entreprise APCHQ personnalisé Horizon : un
            # document soumission de type "contract" porte ses champs
            # structurés dans contract_data (JSON), et la signature de
            # l'entrepreneur (chargé de projet) dans contractor_*.
            (
                "soumissions",
                "kind",
                "VARCHAR(16) NOT NULL DEFAULT 'quote'",
            ),
            ("soumissions", "contract_data", "TEXT"),
            ("soumissions", "contractor_signed_name", "VARCHAR(255)"),
            (
                "soumissions",
                "contractor_signed_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            ("soumissions", "contractor_signed_ip", "VARCHAR(64)"),
            ("soumissions", "contractor_signature_image", "BYTEA"),
            (
                "soumissions",
                "contractor_signature_image_content_type",
                "VARCHAR(100)",
            ),
            (
                "soumissions",
                "contractor_signature_token",
                "VARCHAR(64)",
            ),
            ("lead_analyses", "best_refi_program", "VARCHAR(128)"),
            (
                "lead_analyses",
                "tga_pct",
                "NUMERIC(5,3) DEFAULT 4.0",
            ),
            (
                "lead_analyses",
                "taux_interet_achat_pct",
                "NUMERIC(5,3) DEFAULT 4.0",
            ),
            ("lead_analyses", "mdf_preteur_b", "NUMERIC(14,2)"),
            (
                "lead_analyses",
                "mdf_preteur_b_pct",
                "NUMERIC(5,2) DEFAULT 25.0",
            ),
            (
                "lead_analyses",
                "frais_demarrage_overrides_json",
                "TEXT",
            ),
            # Nouveau champ paramétrable (mai 2026) : taux d'intérêt
            # prêteur B pendant la phase chantier. Avant on utilisait
            # le défaut hardcodé 0.08 (dataclass FinanceInputs) ;
            # maintenant l'utilisateur peut surcharger par fiche.
            (
                "lead_analyses",
                "taux_interet_preteur_b_projet_pct",
                "NUMERIC(5,3) DEFAULT 8.0",
            ),
            # Dossier TAL ouvert sur un bail (non-paiement) — coché
            # depuis la page Paiements (2026-08-31).
            ("imm_baux", "tal_dossier_ouvert_le", "DATE"),
            # Stratégies d'acquisition (août 2026, chantier staging) :
            # sélecteur de stratégie + balance de vente + horizon de
            # projection. NULL = comportement historique.
            ("lead_analyses", "strategie_acquisition", "VARCHAR(32)"),
            ("lead_analyses", "programme_achat", "VARCHAR(32)"),
            ("lead_analyses", "refi_retenu", "VARCHAR(32)"),
            ("lead_analyses", "balance_vente_montant", "NUMERIC(14,2)"),
            ("lead_analyses", "balance_vente_taux_pct", "NUMERIC(5,3)"),
            ("lead_analyses", "cashback_montant", "NUMERIC(14,2)"),
            ("lead_analyses", "projection_horizon_annees", "INTEGER"),
            # Phase 3 : optimisation par unité (liste JSON).
            ("lead_analyses", "unites_json", "TEXT"),
            # Kratos : pivot vers le modèle user-driven (problème
            # écrit/dicté par l'utilisateur, solution générée par l'IA).
            ("kratos_problems", "problem_text", "TEXT"),
            ("kratos_problems", "solution_plan", "TEXT"),
            ("kratos_problems", "solution_steps_json", "TEXT"),
            (
                "lead_analyses",
                "frais_demarrage_financables_json",
                "TEXT",
            ),
            (
                "entreprises",
                "is_parent_company",
                "BOOLEAN DEFAULT FALSE NOT NULL",
            ),
            # Organigramme : co-détenteurs d'un nœud entreprise (JSON
            # liste d'IDs org_nodes) — la détention n'est pas un arbre
            # strict, plusieurs entreprises peuvent en posséder une.
            ("org_nodes", "co_owner_node_ids", "TEXT"),
            # Organigramme : position libre sur le canvas type Miro.
            ("org_nodes", "pos_x", "DOUBLE PRECISION"),
            ("org_nodes", "pos_y", "DOUBLE PRECISION"),
            # Organigramme : niveau d'exécution (direction / adjoint /
            # adjoint_virtuel) — qui doit faire ce rôle / cette tâche.
            ("org_nodes", "execution_tier", "VARCHAR(24)"),
            # Refacturation des achats — Phase A.
            # `is_billable` indique si l'achat doit être refacturé au
            # client. `markup_percent` : majoration appliquée à
            # l'import. `invoiced_at` + `facture_item_id` : garde-fous
            # contre la double-facturation.
            (
                "achats",
                "is_billable",
                "BOOLEAN NOT NULL DEFAULT TRUE",
            ),
            # Verrou « l'utilisateur a tranché » : les automatismes de
            # correction du drapeau `is_billable` ignorent ces lignes.
            (
                "achats",
                "billable_manual",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("achats", "markup_percent", "NUMERIC(6, 2)"),
            ("achats", "invoiced_at", "TIMESTAMP WITH TIME ZONE"),
            ("achats", "facture_item_id", "INTEGER"),
            # Sépare taxes payées au fournisseur du HT — pour ne pas
            # appliquer le markup sur des taxes lors de la refacturation.
            ("achats", "amount_taxes", "NUMERIC(12, 2)"),
            # Phase B — taux facturable employés + flag refacturé punches.
            ("employes", "billing_rate", "NUMERIC(10, 2)"),
            ("punches", "invoiced_at", "TIMESTAMP WITH TIME ZONE"),
            ("punches", "facture_item_id", "INTEGER"),
            # Phase C — facture sous-traitant + contrat de projet.
            ("achats", "sous_traitant_id", "INTEGER"),
            (
                "achats",
                "kind",
                "VARCHAR(16) NOT NULL DEFAULT 'material'",
            ),
            ("achats", "hours", "NUMERIC(6, 2)"),
            # Organigramme : suivi de mise en œuvre du plan canonique
            # (planifie | en_cours | fait | bloque | non_applicable).
            ("org_nodes", "state", "VARCHAR(16)"),
            ("org_nodes", "state_note", "TEXT"),
            # DevlogLead — alignement structurel sur ContactRequest pour
            # permettre le clonage 1:1 de la page CRM côté frontend.
            # Soumission rebuild : sections par pôle (Frontend, Backend,
            # Hosting…) avec markup interne et items associés.
            ("devlog_soumission_items", "section_id", "INTEGER"),
            (
                "devlog_soumission_items",
                "cost_per_unit",
                "DOUBLE PRECISION NOT NULL DEFAULT 0",
            ),
            # Refonte devis Dev logiciel (mai 2026) — calcul circulaire
            # mensuel + mise en oeuvre, voir
            # ``app.services.devlog_devis_calc``. ``is_devis_dev``
            # distingue les soumissions nouveau format des soumissions
            # legacy (conservées en lecture seule).
            (
                "devlog_soumissions",
                "is_devis_dev",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("devlog_soumissions", "marge_recurrente_pct", "NUMERIC(5,2)"),
            ("devlog_soumissions", "marge_initiale_pct", "NUMERIC(5,2)"),
            (
                "devlog_soumissions",
                "commission_closer_pct",
                "NUMERIC(5,2)",
            ),
            ("devlog_soumissions", "taux_dev_horaire", "NUMERIC(8,2)"),
            (
                "devlog_soumissions",
                "taux_manager_horaire",
                "NUMERIC(8,2)",
            ),
            ("devlog_soumissions", "heures_manager", "NUMERIC(8,2)"),
            (
                "devlog_soumissions",
                "client_recurring_description",
                "TEXT",
            ),
            # Envoi PDF + signature publique (vague 1, mai 2026) —
            # token opaque + horodatages + audit trail signature.
            ("devlog_soumissions", "signature_token", "VARCHAR(64)"),
            (
                "devlog_soumissions",
                "sent_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "devlog_soumissions",
                "signed_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            ("devlog_soumissions", "signed_name", "VARCHAR(255)"),
            ("devlog_soumissions", "signed_ip", "VARCHAR(64)"),
            # PDF signé — généré à la signature publique, contient l'encart
            # « Signée électroniquement le ... par ... IP : ... » en bas
            # de chaque page. Stocké en BYTEA pour rester self-contained
            # (pas de bucket externe). Récupérable via
            # GET /devlog/soumissions/{id}/signed-pdf (auth admin/owner).
            ("devlog_soumissions", "signed_pdf_blob", "BYTEA"),
            # NDA — PDF signé généré au moment de la signature publique
            # (POST /public/ndas/{token}/sign). Contient le bloc Récepteur
            # rempli (nom, courriel, date, mention « Signée électrique-
            # ment ») + un bandeau emerald-600 « SIGNEE ELECTRONIQUEMENT »
            # en haut de la première page avec horodatage, IP, et hash
            # SHA-256 du document pour intégrité. Récupérable via
            # GET /api/v1/ndas/{id}/signed-pdf (auth admin/owner).
            ("ndas", "signed_pdf_blob", "BYTEA"),
            # Téléphone collecté sur le formulaire public de signature
            # NDA. Le bloc Récepteur du NDA exige Nom + Email +
            # Téléphone + Date + Signature ; l'email est déjà connu
            # (lien envoyé à cette adresse), reste à collecter le
            # téléphone côté formulaire public.
            ("ndas", "signed_phone", "VARCHAR(32)"),
            # Contrat de gestion — signature du Mandataire (MGV) qui
            # signe EN PREMIER, avant relais au Mandant. Colonnes
            # additives ajoutées après la 1re version (mono-signature).
            ("contrats_gestion", "mandataire_nom", "VARCHAR(255)"),
            ("contrats_gestion", "mandataire_courriel", "VARCHAR(320)"),
            (
                "contrats_gestion",
                "mandataire_signature_token",
                "VARCHAR(64)",
            ),
            (
                "contrats_gestion",
                "mandataire_signed_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            ("contrats_gestion", "mandataire_signed_name", "VARCHAR(255)"),
            ("contrats_gestion", "mandataire_signed_ip", "VARCHAR(64)"),
            ("contrats_gestion", "mandataire_signature_image", "BYTEA"),
            (
                "contrats_gestion",
                "mandataire_signature_image_content_type",
                "VARCHAR(100)",
            ),
            # Gabarit propre à un contrat (négociation par immeuble).
            ("contrats_gestion", "corps_template_override", "TEXT"),
            # Envoi PDF + consultation publique des factures devlog
            # (pièce #5 vague 1). `due_date` existe déjà dans le modèle,
            # on ajoute le token public, l'horodatage d'envoi et celui
            # du marquage manuel « payée » (en attendant Stripe).
            ("devlog_invoices", "signature_token", "VARCHAR(64)"),
            (
                "devlog_invoices",
                "sent_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "devlog_invoices",
                "paid_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Relances automatiques des factures Dev logiciel
            # (cron ``app.jobs.devlog_facture_reminders``).
            (
                "devlog_invoices",
                "last_reminder_sent_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "devlog_invoices",
                "reminder_count",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            # Stripe Checkout pour les factures devlog (chantier #4,
            # mai 2026). `stripe_session_id` sert au mapping webhook
            # → facture ; `payment_method` distingue les paiements en
            # ligne ('stripe') des paiements manuels ('virement',
            # 'cheque', 'manuel').
            (
                "devlog_invoices",
                "stripe_session_id",
                "VARCHAR(128)",
            ),
            (
                "devlog_invoices",
                "stripe_payment_intent_id",
                "VARCHAR(128)",
            ),
            (
                "devlog_invoices",
                "payment_method",
                "VARCHAR(32)",
            ),
            (
                "devlog_soumission_items",
                "item_kind",
                "VARCHAR(20) NOT NULL DEFAULT 'feature'",
            ),
            ("devlog_soumission_items", "heures", "NUMERIC(8,2)"),
            # Niveau MODULE (refonte 2026-06) — un item rattaché à un
            # module est une fonctionnalité de ce module. La table
            # ``devlog_soumission_modules`` est créée par create_all ;
            # cette colonne FK (nullable, ON DELETE SET NULL côté modèle)
            # étend la table items existante. Additif et rétrocompatible :
            # NULL pour tous les items legacy.
            ("devlog_soumission_items", "module_id", "INTEGER"),
            # Gratuité conditionnelle « module → module » (refonte
            # 2026-06, Phase 2). Si défini ET module déclencheur
            # sélectionné, ce module devient gratuit côté client. FK
            # auto-référente (ON DELETE SET NULL côté modèle ; l'ALTER
            # ajoute la colonne simple, comme pour ``module_id``).
            # Additif et rétrocompatible : NULL pour tous les modules
            # existants.
            (
                "devlog_soumission_modules",
                "free_when_module_id",
                "INTEGER",
            ),
            ("devlog_leads", "address", "VARCHAR(500)"),
            (
                "devlog_leads",
                "project_type",
                "VARCHAR(32) NOT NULL DEFAULT 'autre'",
            ),
            ("devlog_leads", "kanban_column", "VARCHAR(64)"),
            (
                "devlog_leads",
                "locale",
                "VARCHAR(8) NOT NULL DEFAULT 'fr'",
            ),
            # Notes de rencontre client (texte libre, peut etre tres
            # long). Resume par Gemini via /devlog/leads/{id}/summarize-notes.
            ("devlog_leads", "meeting_notes", "TEXT"),
            # Fiche client unifiee (mai 2026) — quand un prospect est
            # converti en client, on garde le lien bidirectionnel
            # (`devlog_leads.client_id` ↔ `devlog_clients.converted_from_lead_id`)
            # + l'horodatage de la conversion pour afficher le badge
            # "Prospect depuis ... · Converti le ..." sur la fiche client
            # et pour permettre le merge de l'historique (notes, soumissions,
            # attachments) entre les deux entites.
            (
                "devlog_clients",
                "converted_from_lead_id",
                "INTEGER REFERENCES devlog_leads(id) ON DELETE SET NULL",
            ),
            (
                "devlog_clients",
                "converted_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Téléphonie Phase 2 — secrétaire IA. La table CallTurn est
            # créée par create_all ; les colonnes ci-dessous étendent
            # PhoneNumber et Call (créés en Phase 1).
            (
                "voice_phone_numbers",
                "secretary_mode_active",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            (
                "voice_phone_numbers",
                "lead_auto_callback_enabled",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("voice_calls", "lang", "VARCHAR(8) NOT NULL DEFAULT 'fr-CA'"),
            ("voice_calls", "intent", "VARCHAR(64)"),
            ("voice_calls", "lead_name", "VARCHAR(255)"),
            ("voice_calls", "lead_callback_phone", "VARCHAR(50)"),
            ("voice_calls", "lead_reason", "TEXT"),
            ("voice_calls", "contact_request_id", "INTEGER"),
            # Téléphonie Phase 3 — flags routage + voicemail.
            (
                "voice_calls",
                "was_blocked",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("voice_calls", "was_vip", "BOOLEAN NOT NULL DEFAULT FALSE"),
            (
                "voice_calls",
                "was_voicemail",
                "BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            ("voice_calls", "voicemail_transcription", "TEXT"),
            ("voice_calls", "voicemail_summary", "TEXT"),
            # Phase 4 — sortant + lien CRM générique.
            ("voice_calls", "entity_type", "VARCHAR(32)"),
            ("voice_calls", "entity_id", "INTEGER"),
            ("voice_calls", "followup_suggestion", "TEXT"),
            ("voice_calls", "caller_kind", "VARCHAR(32)"),
            # Anti-spam — VoiceUsageDaily peut être créée vide par
            # create_all, mais on ajoute spam_blocked au cas où la
            # table existait sans cette colonne (bootstrap progressif).
            (
                "voice_usage_daily",
                "spam_blocked",
                "INTEGER NOT NULL DEFAULT 0",
            ),
            # Intake téléphonique IA — collecte de besoins en
            # construction par Léa au téléphone, avec validation par
            # le client via lien courriel (page publique).
            ("contact_requests", "intake_data", "TEXT"),
            ("contact_requests", "validation_token", "VARCHAR(64)"),
            (
                "contact_requests",
                "validated_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Phase 8 : cibles de routage par scénario, configurables
            # depuis l'app (au lieu d'env vars Render).
            ("voice_phone_numbers", "urgency_forward_e164", "VARCHAR(20)"),
            ("voice_phone_numbers", "closer_forward_e164", "VARCHAR(20)"),
            ("voice_phone_numbers", "followup_forward_e164", "VARCHAR(20)"),
            # Agenda — type de RV configurable (lien vers
            # appointment_types). Optionnel pour ne pas casser le legacy.
            ("agenda_events", "appointment_type_id", "INTEGER"),
            # État conversationnel JSON sur Call (smart booking : on
            # mémorise les créneaux proposés par Léa pour les
            # retrouver au tour suivant).
            ("voice_calls", "session_state", "TEXT"),
            # Mobile perso de l'utilisateur — click-to-call fait sonner CE
            # numéro (mappé dans le portail) quand l'utilisateur appelle.
            ("users", "phone_e164", "VARCHAR(20)"),
            ("prospection_deals", "lead_analysis_id", "INTEGER"),
            ("lead_analyses", "converted_to_deal_id", "INTEGER"),
            # Phase A2 (tri-couche extraction) : modèle utilisé pour
            # l'extraction (local / gemini / claude-sonnet-4-6).
            ("lead_analyses", "model_used", "VARCHAR(64)"),
            # Phase A3 (validation post-extraction) : liste JSONB des
            # anomalies détectées (bornes hors-limites, divergences
            # local↔gemini). Cf. app.services.lead_validation.
            ("lead_analyses", "validation_warnings", "JSONB"),
            # Chantier "contrat signé + dépôt payé → projet démarré"
            # (mai 2026). Sur DevlogContract : dépôt initial requis +
            # trace du paiement manuel + lien vers le projet provisionné.
            # Sur DevlogProject : horodatage de démarrage effectif.
            (
                "devlog_contracts",
                "deposit_required_cents",
                "INTEGER",
            ),
            (
                "devlog_contracts",
                "deposit_paid_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "devlog_contracts",
                "deposit_paid_amount_cents",
                "INTEGER",
            ),
            ("devlog_contracts", "project_id", "INTEGER"),
            (
                "devlog_projects",
                "started_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Horodatage du passage en status='livre' — sert au cron
            # ``devlog_nps_dispatch`` (envoi NPS 7 jours après livraison).
            # Posé automatiquement par l'event listener du modèle
            # ``DevlogProject``.
            (
                "devlog_projects",
                "delivered_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Hook post-signature contrat (mai 2026, PR Phil). 4 side-effects
            # best-effort déclenchés depuis le endpoint public sign — chaque
            # horodatage marque le succès de l'étape (NULL = pas encore /
            # rate ou skip). github_repo_url contient l'URL HTML du repo
            # provisionné par GITHUB_AUTOMATION_TOKEN.
            (
                "devlog_contracts",
                "github_repo_url",
                "VARCHAR(512)",
            ),
            (
                "devlog_contracts",
                "welcome_email_sent_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "devlog_contracts",
                "qbo_pushed_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            (
                "devlog_contracts",
                "teams_notified_at",
                "TIMESTAMP WITH TIME ZONE",
            ),
            # Mai 2026 : colonne "finançable par défaut" sur la table
            # de défauts d'analyse. Permet à Phil de configurer
            # globalement, pour chaque item MDF (groupes ``mdf_frais``
            # et ``mdf_pct``), si la case "Finançable" doit être
            # pré-cochée à la création d'une nouvelle fiche.
            (
                "prospection_analysis_defaults",
                "financable_par_defaut",
                "BOOLEAN",
            ),
            # Drive page modules : registry par pôle (navigation Settings).
            # Métadonnées seedées (cf. drive_page_modules_seed), nullables
            # pour les modules auto-créés via PATCH.
            ("drive_page_modules", "pole", "VARCHAR(64)"),
            ("drive_page_modules", "label", "VARCHAR(128)"),
            ("drive_page_modules", "route", "VARCHAR(256)"),
            # Portée du module : "entity" (un dossier par fiche — défaut,
            # comportement historique) ou "page" (dossier unique singleton,
            # via DriveEntityLink à entity_id=0). DEFAULT 'entity' garantit
            # que les 22 modules de fiche existants restent en mode entité.
            (
                "drive_page_modules",
                "scope",
                "VARCHAR(16) NOT NULL DEFAULT 'entity'",
            ),
            # Résumé IA de l'enregistrement d'appel (humain) — distinct du
            # voicemail.
            ("voice_calls", "recording_transcription", "TEXT"),
            ("voice_calls", "recording_summary", "TEXT"),
            # Extracteur générique des Drive Conventions : mapping JSON
            # {var_key: field_path} résolu par introspection. NULL =
            # fallback sur l'extracteur hardcodé du registry (rétrocompat
            # des conventions existantes sans mapping).
            ("drive_conventions", "variable_mapping", "JSON"),
            # Signature électronique des baux (volet immobilier, Phase 2.4).
            ("imm_baux", "signature_token", "VARCHAR(64)"),
            ("imm_baux", "sent_to_email", "VARCHAR(320)"),
            ("imm_baux", "sent_at", "TIMESTAMP WITH TIME ZONE"),
            ("imm_baux", "signed_at", "TIMESTAMP WITH TIME ZONE"),
            ("imm_baux", "signed_by_name", "VARCHAR(255)"),
            ("imm_baux", "signature_ip", "VARCHAR(64)"),
            ("imm_baux", "signature_image", "BYTEA"),
            ("imm_baux", "signature_image_content_type", "VARCHAR(100)"),
            ("imm_baux", "signature_opened_at", "TIMESTAMP WITH TIME ZONE"),
            ("imm_locataires", "ancienne_adresse", "VARCHAR(500)"),
            ("imm_location_visites", "locataire_id", "INTEGER"),
            ("imm_location_dossiers", "locataire_cree", "BOOLEAN"),
            # Valeurs par defaut des soumissions devis_dev (juin 2026) :
            # fonctionnalites par defaut (pre-remplissent CHAQUE nouveau
            # module) + taches du charge de projet par defaut (pre-remplies a
            # CHAQUE nouvelle soumission). Listes JSON [{description, heures}].
            # NULL sur les lignes existantes => comportement neutre (retrocompat).
            ("devlog_soumission_defaults", "default_features_json", "JSONB"),
            (
                "devlog_soumission_defaults",
                "default_manager_tasks_json",
                "JSONB",
            ),
            # Permissions par pôle des clés d'API (juin 2026) : liste JSON de
            # scopes « <pole>:<capability> » (ex. devlog:activity:read,
            # prospection:tasks:create). NULL sur les clés existantes =>
            # rétrocompat : lecture de TOUS les pôles (aucune écriture).
            ("api_keys", "scopes_json", "TEXT"),
            # TRI investisseur (juin 2026) : 4 intrants MANUELS du
            # calculateur de rendement (lead_tri_calc) persistes sur la
            # fiche d'analyse. Les 8 autres intrants sont derives a la
            # volee depuis l'analyse financiere (non persistes). NULL =>
            # l'endpoint /tri-inputs renvoie des defauts raisonnables.
            ("lead_analyses", "tri_capital_injecte", "NUMERIC(14, 2)"),
            ("lead_analyses", "tri_pct_investisseur", "NUMERIC(6, 4)"),
            ("lead_analyses", "tri_croissance_loyers", "NUMERIC(6, 4)"),
            ("lead_analyses", "tri_croissance_depenses", "NUMERIC(6, 4)"),
            # Motif de perte d'un lead construction (juin 2026) : renseigné
            # quand le lead passe en `lost` (Refusé). NULL sur les lignes
            # existantes => neutre. Sans cet ALTER, le SELECT sur
            # contact_requests plante (colonne mappée par l'ORM mais absente).
            ("contact_requests", "lost_reason", "VARCHAR(120)"),
            # Responsable d'un projet (juin 2026) : l'employé/user vers qui
            # router un appel de suivi d'un client existant (téléphonie Léa).
            # NULL sur les projets existants => on retombe sur l'ancienne
            # logique (premier membre actif → back-office).
            ("projects", "responsible_user_id", "INTEGER"),
            # Transfert avec musique d'attente (juin 2026) : état JSON des
            # jambes d'appel parallèles (file Twilio <Enqueue>).
            ("voice_calls", "dial_state_json", "TEXT"),
        )
        for table, column, col_type in additive_columns:
            await conn.execute(
                text(
                    f'ALTER TABLE {table} '
                    f'ADD COLUMN IF NOT EXISTS {column} {col_type}'
                )
            )

        # Achats : tout achat NON paye par facture fournisseur
        # (cheque, CC) est considere paye au moment de l'achat.
        # Backfill idempotent : marque paid les achats existants avec
        # payment_method != bill_to_pay encore en status received.
        try:
            await conn.execute(
                text(
                    "UPDATE achats SET status = 'paid', "
                    "paid_at = COALESCE(paid_at, received_at, "
                    "created_at) "
                    "WHERE status = 'received' "
                    "AND payment_method IS NOT NULL "
                    "AND payment_method <> 'bill_to_pay'"
                )
            )
        except Exception as exc:
            # Table peut ne pas exister au tout premier demarrage,
            # ou colonne pas encore la sur ancien schema.
            log.warning("init_db: backfill achats status=paid échouée: %s", exc)

        # Achats : pour les bill_to_pay existants sans due_at, calcule
        # received_at + 30j (defaut) ou + payment_terms_days du
        # fournisseur si defini. Idempotent.
        try:
            await conn.execute(
                text(
                    "UPDATE achats a SET due_at = "
                    "COALESCE(a.received_at, a.created_at) + "
                    "make_interval(days := COALESCE("
                    "(SELECT f.payment_terms_days FROM fournisseurs f "
                    "WHERE f.id = a.fournisseur_id), 30)) "
                    "WHERE a.status = 'received' "
                    "AND a.payment_method = 'bill_to_pay' "
                    "AND a.due_at IS NULL"
                )
            )
        except Exception as exc:
            log.warning("init_db: backfill achats due_at bill_to_pay échouée: %s", exc)

        # DevlogLead : migration des statuts français vers les valeurs
        # ContactRequest (new/contacted/qualified/quoted/won/lost/spam)
        # pour aligner la page CRM Dev logiciel sur Construction.
        # Idempotent : les rows déjà migrées (status déjà en anglais) ne
        # sont pas touchées.
        try:
            for old, new in (
                ("nouveau", "new"),
                ("contacte", "contacted"),
                ("rdv", "qualified"),
                ("presentation", "qualified"),
                ("soumission", "quoted"),
                ("gagne", "won"),
                ("perdu", "lost"),
            ):
                await conn.execute(
                    text(
                        "UPDATE devlog_leads SET status = :new "
                        "WHERE status = :old"
                    ),
                    {"new": new, "old": old},
                )
            # Aligne la longueur de la colonne status à VARCHAR(32) au
            # cas où l'ancienne VARCHAR(20) existerait — silencieux si
            # déjà à la bonne taille.
            await conn.execute(
                text(
                    "ALTER TABLE devlog_leads "
                    "ALTER COLUMN status TYPE VARCHAR(32)"
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Table peut ne pas exister encore au tout premier démarrage
            # ou avoir une autre forme. Migration silencieuse — sera
            # rejouée au prochain redémarrage si nécessaire.
            log.warning("init_db: migration statuts devlog_leads échouée: %s", exc)

        # Kratos : passage à entreprise_id NULLABLE (problème global
        # transverse possible). Idempotent : si déjà nullable, no-op.
        try:
            await conn.execute(
                text(
                    "ALTER TABLE kratos_problems "
                    "ALTER COLUMN entreprise_id DROP NOT NULL"
                )
            )
        except Exception as exc:
            log.warning("init_db: kratos_problems.entreprise_id nullable échouée: %s", exc)

        # Backfill: any pre-existing user with is_admin=TRUE becomes
        # an "owner" so current sign-ins keep full access. Only runs
        # the first time; subsequent runs are harmless (no rows match).
        try:
            await conn.execute(
                text(
                    "UPDATE users SET role='owner' "
                    "WHERE is_admin=TRUE AND role='employee'"
                )
            )
        except Exception as exc:
            log.warning("init_db: backfill users role=owner (is_admin) échouée: %s", exc)

        # Promote Philippe Meuser au rang owner (mêmes accès que
        # Steven). Idempotent — UPDATE n'a aucun effet quand le rôle
        # est déjà 'owner'. On vise les deux variantes de courriel
        # qu'on a vues dans les whitelists.
        try:
            await conn.execute(
                text(
                    "UPDATE users SET role='owner', is_admin=TRUE "
                    "WHERE LOWER(email) IN "
                    "('philippe.meuser@immohorizon.com', "
                    " 'pmeuser@immohorizon.com')"
                )
            )
        except Exception as exc:
            log.warning("init_db: promotion Philippe Meuser owner échouée: %s", exc)

        # Élargit la colonne region de mtl_property_units si elle est
        # encore en VARCHAR(8) (legacy). 'mtl-island' fait 10 chars,
        # 'rive-nord' 9. ALTER COLUMN TYPE est idempotent en Postgres
        # quand la nouvelle taille est >= ancienne.
        try:
            await conn.execute(
                text(
                    "ALTER TABLE mtl_property_units "
                    "ALTER COLUMN region TYPE VARCHAR(32)"
                )
            )
        except Exception as exc:
            log.warning("init_db: élargit mtl_property_units.region échouée: %s", exc)

        # Élargit sous_traitants.region : créée en VARCHAR(32) (une
        # seule région), on accepte désormais une liste séparée par
        # virgules pour permettre plusieurs régions par sous-traitant.
        try:
            await conn.execute(
                text(
                    "ALTER TABLE sous_traitants "
                    "ALTER COLUMN region TYPE VARCHAR(255)"
                )
            )
        except Exception as exc:
            log.warning("init_db: élargit sous_traitants.region échouée: %s", exc)

        # Élargit municipalite et code_utilisation pour accepter les
        # valeurs du rôle provincial MAMH : nom complet de municipalité
        # (« Sainte-Anne-des-Plaines » = 23 chars) au lieu du code 8
        # chars du feed Ville de Montréal, et codes d'utilisation
        # potentiellement alphanumériques.
        for column, new_type in (
            ("municipalite", "VARCHAR(128)"),
            ("code_utilisation", "VARCHAR(16)"),
        ):
            try:
                await conn.execute(
                    text(
                        f"ALTER TABLE mtl_property_units "
                        f"ALTER COLUMN {column} TYPE {new_type}"
                    )
                )
            except Exception as exc:
                log.warning("init_db: élargit mtl_property_units.%s échouée: %s", column, exc)

        # project_phases.duration_days passe de INTEGER → NUMERIC(6,2)
        # pour supporter les phases en heures (ex. 0.5 = ½ journée).
        # ALTER TYPE NUMERIC est idempotent côté PG quand la conversion
        # est implicite (INTEGER → NUMERIC ne perd jamais de données).
        try:
            await conn.execute(
                text(
                    "ALTER TABLE project_phases "
                    "ALTER COLUMN duration_days TYPE NUMERIC(6,2)"
                )
            )
        except Exception as exc:
            log.warning("init_db: project_phases.duration_days TYPE NUMERIC échouée: %s", exc)

        # Relaxations — columns whose nullability changed.
        # ALTER ... DROP NOT NULL is idempotent on PostgreSQL.
        for table, column in (
            ("projects", "client_id"),
            # Le modèle Achat ne sépare plus PO et achat ; le champ
            # reference n'est plus obligatoire.
            ("achats", "reference"),
            # Le nom d'immeuble est désormais facultatif — fallback sur
            # l'adresse si non fourni.
            ("imm_immeubles", "name"),
            # user_id devient optionnel sur entreprise_partners pour
            # permettre des partenaires externes sans compte portail.
            ("entreprise_partners", "user_id"),
        ):
            try:
                await conn.execute(
                    text(f'ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL')
                )
            except Exception as exc:
                # Column may not exist yet on a brand-new DB — harmless.
                log.warning("init_db: DROP NOT NULL %s.%s échouée: %s", table, column, exc)

        # Drop l'unique constraint sur user_calendar_feeds.user_id pour
        # autoriser plusieurs flux ICS par user (perso + travail + équipe).
        # Idempotent — DROP CONSTRAINT IF EXISTS si le nom est trouvé.
        for cstr in (
            "user_calendar_feeds_user_id_key",
            "uq_user_calendar_feeds_user_id",
        ):
            try:
                await conn.execute(
                    text(
                        f"ALTER TABLE user_calendar_feeds "
                        f"DROP CONSTRAINT IF EXISTS {cstr}"
                    )
                )
            except Exception as exc:
                log.warning("init_db: DROP CONSTRAINT user_calendar_feeds %s échouée: %s", cstr, exc)

        # ⚠ DÉSACTIVÉ — était une migration one-shot (Avril 2026) qui
        # déplaçait les anciens « achats » draft/ordered vers la table
        # purchase_orders puis les SUPPRIMAIT de la table achats. Comme
        # le bloc est resté dans init_db, il s'exécutait à CHAQUE
        # démarrage et avalait silencieusement tout achat futur dont
        # le status était draft/ordered ou cancelled+received_at=NULL.
        # Conséquence : les achats annulés non-reçus, créés normalement
        # par les utilisateurs, disparaissaient au prochain cold-start
        # Render. Désactivé en novembre 2026 pour stopper la perte.
        # On garde le bloc commenté pour la mémoire.

        # Backfill des tables de jointure pour les assignations
        # multi-personnes sur phases et tâches. Idempotent : ON CONFLICT
        # DO NOTHING sur la contrainte d'unicité. Migre les assignations
        # historiques (1 personne max) vers le nouveau modèle (N).
        try:
            await conn.execute(
                text(
                    "INSERT INTO project_phase_assignees "
                    "(phase_id, employe_id, sous_traitant_id) "
                    "SELECT id, assignee_employe_id, NULL "
                    "FROM project_phases "
                    "WHERE assignee_employe_id IS NOT NULL "
                    "ON CONFLICT DO NOTHING"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO project_phase_assignees "
                    "(phase_id, employe_id, sous_traitant_id) "
                    "SELECT id, NULL, assignee_sous_traitant_id "
                    "FROM project_phases "
                    "WHERE assignee_sous_traitant_id IS NOT NULL "
                    "ON CONFLICT DO NOTHING"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO project_task_assignees "
                    "(task_id, employe_id, sous_traitant_id) "
                    "SELECT id, assignee_id, NULL "
                    "FROM project_tasks "
                    "WHERE assignee_id IS NOT NULL "
                    "ON CONFLICT DO NOTHING"
                )
            )
        except Exception as exc:
            # Tables ou colonnes absentes lors du tout premier boot
            # (avant create_all) — harmless, le backfill retentera au
            # prochain démarrage.
            log.warning("init_db: backfill assignees phases/tasks (N-N) échouée: %s", exc)

        # Index additionnels — perf des listes /prospection/mtl-properties
        # avec ~900 K-1 M unités. CREATE INDEX IF NOT EXISTS est idempotent.
        # Sans ces index, les filtres déclenchent des seq-scans (plusieurs
        # secondes par requête).
        additive_indexes = (
            # Téléphonie anti-spam : rate-limit compte les appels
            # récents par from_e164 — sans cet index, scan complet de
            # voice_calls à chaque appel entrant.
            (
                "ix_voice_calls_from_started",
                "voice_calls",
                "(from_e164, started_at DESC)",
            ),
            (
                "ix_mtl_units_nombre_logement",
                "mtl_property_units",
                "(nombre_logement)",
            ),
            (
                "ix_mtl_units_code_utilisation",
                "mtl_property_units",
                "(code_utilisation)",
            ),
            (
                "ix_mtl_units_municipalite_lower",
                "mtl_property_units",
                "(LOWER(municipalite))",
            ),
            (
                "ix_mtl_units_annee_construction",
                "mtl_property_units",
                "(annee_construction)",
            ),
            (
                "ix_mtl_units_arrondissement",
                "mtl_property_units",
                "(arrondissement)",
            ),
        )
        for idx_name, table, expr in additive_indexes:
            try:
                await conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {idx_name} "
                        f"ON {table} {expr}"
                    )
                )
            except Exception as exc:
                # Table absente au tout premier boot — sera ré-essayé.
                log.warning("init_db: création index %s échouée: %s", idx_name, exc)

        # P-10c : index UNIQUE PARTIEL — au plus 1 punch OUVERT (ended_at
        # NULL) par employé. Rend le double-punch structurellement
        # impossible (la vérif applicative punch_ops/mobile a une fenêtre
        # de course). SÛR : IF NOT EXISTS (idempotent) + try/except (le boot
        # ne casse jamais) et AUCUNE mutation de données. Si des doublons
        # ouverts existent DÉJÀ en prod, le CREATE échoue → on l'attrape et
        # l'index n'est simplement pas posé (rien n'est touché) ; il se
        # posera au boot suivant une fois les doublons nettoyés à la main.
        try:
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_punches_one_open_per_employe "
                    "ON punches (employe_id) WHERE ended_at IS NULL"
                )
            )
        except Exception as exc:
            log.warning(
                "init_db: index unique 'punch ouvert' non posé "
                "(doublons ouverts pré-existants ?): %s",
                exc,
            )

        # P-13 : index UNIQUE (bail, mois, niveau) sur les relances de loyer
        # → un double-clic concurrent sur « relancer » ne peut pas créer 2
        # lignes au même niveau (donc pas 2 courriels au locataire ; preuve
        # TAL). Même filet que l'index punch : IF NOT EXISTS + try/except,
        # aucune mutation ; si des doublons existent déjà, non posé (à
        # nettoyer à la main) puis posé au boot suivant.
        try:
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_relances_loyer_bail_mois_niveau "
                    "ON imm_relances_loyer (bail_id, mois_couvert, niveau)"
                )
            )
        except Exception as exc:
            log.warning(
                "init_db: index unique relance loyer non posé "
                "(doublons pré-existants ?): %s",
                exc,
            )

        # Reclassification one-shot des tâches d'entreprises importées
        # de Monday qui sont restées en « backlog ». L'utilisateur veut
        # qu'aucune tâche importée ne reste classée backlog : on la
        # ventile dans À faire / En cours / En attente / Terminé selon
        # son `monday_group_title` (le nom du groupe d'origine sur le
        # board Monday). Idempotent — chaque UPDATE ne touche que les
        # lignes encore en backlog, donc une fois reclassifiées elles
        # ne bougent plus aux boots suivants.
        #
        # On limite aux tâches avec monday_item_id NOT NULL pour ne pas
        # toucher les backlogs créés manuellement par les utilisateurs.
        for sql in (
            # Auto-déverrouillage : un achat / une heure (punch) qui
            # pointe vers un facture_item disparu (facture supprimée) ne
            # doit plus être considéré « refacturé ». Le FK n'a pas de
            # contrainte SET NULL réelle (colonne ajoutée par ALTER
            # additif), donc on nettoie les références orphelines ici à
            # chaque boot (idempotent + auto-réparateur).
            """
            UPDATE achats
            SET invoiced_at = NULL, facture_item_id = NULL
            WHERE facture_item_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM facture_items fi
                WHERE fi.id = achats.facture_item_id
              )
            """,
            """
            UPDATE punches
            SET invoiced_at = NULL, facture_item_id = NULL
            WHERE facture_item_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM facture_items fi
                WHERE fi.id = punches.facture_item_id
              )
            """,
            # Terminé : si completed_at est déjà set OU si le titre du
            # groupe matche done/complete/✓/etc.
            """
            UPDATE entreprise_taches SET status = 'done'
            WHERE status = 'backlog'
              AND monday_item_id IS NOT NULL
              AND (
                completed_at IS NOT NULL
                OR LOWER(COALESCE(monday_group_title, '')) ~
                   '(done|complete|fini|termin|✓|✅|achev|fait)'
              )
            """,
            # En cours
            """
            UPDATE entreprise_taches SET status = 'in_progress'
            WHERE status = 'backlog'
              AND monday_item_id IS NOT NULL
              AND LOWER(COALESCE(monday_group_title, '')) ~
                  '(working|en cours|doing|actif|wip|ongoing|en traitement)'
            """,
            # En attente / bloqué
            """
            UPDATE entreprise_taches SET status = 'waiting'
            WHERE status = 'backlog'
              AND monday_item_id IS NOT NULL
              AND LOWER(COALESCE(monday_group_title, '')) ~
                  '(attente|wait|block|stuck|on hold|hold|pause|bloqu|pending review)'
            """,
            # Tout le reste qui venait de Monday → À faire (TODO)
            # par défaut. C'est plus utile que de laisser en backlog,
            # et l'utilisateur pourra raffiner manuellement par la suite.
            """
            UPDATE entreprise_taches SET status = 'todo'
            WHERE status = 'backlog'
              AND monday_item_id IS NOT NULL
            """,
            # NOTE : la migration « waiting → todo » a été retirée
            # car la colonne « En attente » est maintenant un statut
            # actif de l'UI (entre « En traitement » et « Terminé »).
            # Toute valeur historique en `waiting` reprend donc sa
            # signification correcte sans rien faire.
            # Pipeline (deals) — alignement du vocabulaire de statut sur
            # celui des tâches d'entreprise. Le frontend partage un seul
            # task-config (todo / a_faire / in_progress / done) ; les
            # anciennes valeurs (a_venir / en_traitement / termine)
            # doivent être renommées dans `prospection_deal_tasks`.
            # Idempotent — une fois renommées, plus aucune ligne ne
            # matche.
            """
            UPDATE prospection_deal_tasks SET status = 'todo'
            WHERE status = 'a_venir'
            """,
            """
            UPDATE prospection_deal_tasks SET status = 'in_progress'
            WHERE status = 'en_traitement'
            """,
            """
            UPDATE prospection_deal_tasks SET status = 'done'
            WHERE status = 'termine'
            """,
            # Auto-remplissage des scores ICE (impact / confiance /
            # effort) pour toutes les tâches qui n'ont pas encore été
            # évaluées. Idempotent — la WHERE clause ne touche que les
            # lignes où impact IS NULL. L'impact dérive de la priorité
            # manuelle (urgent → 9, eleve → 7, moyenne/non_assigne →
            # 5, faible → 3), la confiance et l'effort sont initialisés
            # à 5 (médian) — l'utilisateur peut affiner par la suite
            # depuis la fiche détaillée.
            """
            UPDATE entreprise_taches
            SET impact = CASE priority
                  WHEN 'urgent' THEN 9
                  WHEN 'eleve'  THEN 7
                  WHEN 'faible' THEN 3
                  ELSE 5
                END,
                confidence = COALESCE(confidence, 5),
                effort     = COALESCE(effort, 5)
            WHERE impact IS NULL
            """,
            """
            UPDATE entreprise_taches
            SET confidence = 5
            WHERE confidence IS NULL
            """,
            """
            UPDATE entreprise_taches
            SET effort = 5
            WHERE effort IS NULL
            """,
            """
            UPDATE prospection_deal_tasks
            SET impact = CASE priority
                  WHEN 'urgent' THEN 9
                  WHEN 'eleve'  THEN 7
                  WHEN 'faible' THEN 3
                  ELSE 5
                END,
                confidence = COALESCE(confidence, 5),
                effort     = COALESCE(effort, 5)
            WHERE impact IS NULL
            """,
            """
            UPDATE prospection_deal_tasks
            SET confidence = 5
            WHERE confidence IS NULL
            """,
            """
            UPDATE prospection_deal_tasks
            SET effort = 5
            WHERE effort IS NULL
            """,
            # Backfill des soumissions acceptées sans `accepted_at`.
            # Cas typique : statut posé via PATCH générique ou import
            # externe sans timestamp. Sans ce timestamp, le KPI
            # « Ventes » du dashboard les ignore — on retombe donc
            # sur `updated_at` (heure de la dernière modif, qui
            # correspond ~ à la transition vers ACCEPTED).
            """
            UPDATE soumissions
            SET accepted_at = updated_at
            WHERE status = 'accepted' AND accepted_at IS NULL
            """,
            # Ventilation TPS/TVQ des achats existants : on répartit
            # `amount_taxes` (somme) selon les taux QC standard
            # (TPS 5 % + TVQ 9,975 % = 14,975 %). Idempotent — ne touche
            # que les lignes pas encore ventilées (amount_tps NULL). La
            # somme tps+tvq reste exactement égale à amount_taxes.
            """
            UPDATE achats
            SET amount_tps = ROUND(COALESCE(amount_taxes, 0) * 5.0 / 14.975, 2),
                amount_tvq = COALESCE(amount_taxes, 0)
                             - ROUND(COALESCE(amount_taxes, 0) * 5.0 / 14.975, 2)
            WHERE amount_tps IS NULL
              AND amount_taxes IS NOT NULL
            """,
            # Rétro-lien projet ↔ soumission : un projet créé manuellement
            # (ou par une ancienne version) peut avoir budget = total de la
            # soumission mais soumission_id NULL → impossible d'importer
            # les items de la soumission dans une facture, et la carte
            # kanban tombe sur le titre au lieu de l'adresse. On relie au
            # devis ACCEPTÉ correspondant (même prospect ou même client ET
            # même montant total). Idempotent : ne touche que soumission_id
            # NULL.
            """
            UPDATE projects p
            SET soumission_id = (
                SELECT s.id FROM soumissions s
                WHERE s.status = 'accepted'
                  AND s.total = p.budget
                  AND (
                    (p.contact_request_id IS NOT NULL
                       AND s.contact_request_id = p.contact_request_id)
                    OR (p.client_id IS NOT NULL
                       AND s.client_id = p.client_id)
                  )
                ORDER BY s.accepted_at DESC NULLS LAST, s.id DESC
                LIMIT 1
            )
            WHERE p.soumission_id IS NULL
              AND p.budget IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM soumissions s
                WHERE s.status = 'accepted'
                  AND s.total = p.budget
                  AND (
                    (p.contact_request_id IS NOT NULL
                       AND s.contact_request_id = p.contact_request_id)
                    OR (p.client_id IS NOT NULL
                       AND s.client_id = p.client_id)
                  )
              )
            """,
            # Table de marqueurs pour les backfills à exécuter UNE seule
            # fois (par opposition aux UPDATE idempotents ci-dessus qui
            # peuvent retourner à chaque boot). Permet d'appliquer une
            # règle rétroactive sans réécraser les choix manuels faits
            # ensuite par l'utilisateur.
            """
            CREATE TABLE IF NOT EXISTS applied_backfills (
                key VARCHAR(120) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            # ─── NEUTRALISATION SUPERVISÉE (P-02) ──────────────────────
            # init_db est resté en panne ~26 j (FK cassée, corrigée dans
            # ce même déploiement) → `applied_backfills` n'existe pas
            # encore en prod, donc les 3 backfills one-shot ci-dessous
            # s'exécuteraient pour la 1re fois AU MÊME BOOT que la remise
            # en route. Deux d'entre eux ont un effet VISIBLE / destructif
            # de choix manuels (rotation des reçus déjà droits ; réécriture
            # du « refacturable » saisi à la main). Pour que ce déploiement
            # ne fasse QUE réparer le schéma (créer imm_immeubles-dépendant
            # `immeuble_depenses` + colonnes manquantes) sans rien changer
            # d'autre, on PRÉ-INSCRIT leurs marqueurs → les 3 one-shots se
            # court-circuitent. Pour rejouer un backfill plus tard, de façon
            # DÉLIBÉRÉE : retirer sa ligne ici ET supprimer sa clé de la
            # table `applied_backfills` en prod. Voir docs/PROPOSITIONS.md P-02.
            """
            INSERT INTO applied_backfills (key) VALUES
                ('achat_is_billable_by_project_type_v1'),
                ('rotate_existing_receipts_cw90_v1'),
                ('retag_extra_facture_items_v1')
            ON CONFLICT (key) DO NOTHING
            """,
            # Rétroactif (one-shot) : défaut « refacturable » des achats
            # selon le type de la soumission du projet. Forfaitaire =
            # non refacturable (décoché) ; estimé / à contrat =
            # refacturable (coché). Les projets sans soumission liée
            # retombent sur forfaitaire (décoché). Garde NOT EXISTS : ne
            # s'exécute qu'au premier boot après déploiement, puis le
            # marqueur empêche d'écraser les ajustements manuels.
            """
            UPDATE achats a
            SET is_billable = CASE
                    WHEN s.kind = 'contract' OR s.pricing_kind = 'estime'
                        THEN TRUE
                    ELSE FALSE
                END
            FROM projects p
            LEFT JOIN soumissions s ON s.id = p.soumission_id
            WHERE a.project_id = p.id
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'achat_is_billable_by_project_type_v1'
              )
            """,
            """
            INSERT INTO applied_backfills (key)
            VALUES ('achat_is_billable_by_project_type_v1')
            ON CONFLICT (key) DO NOTHING
            """,
            # v2 (one-shot) : DÉCOCHE « à refacturer » pour les projets qui
            # NE SONT PAS à contrat (estimé / forfaitaire / sans soumission).
            # Le v1 ci-dessus avait coché les ESTIMÉS (s.pricing_kind =
            # 'estime' → TRUE), à rebours de la règle appliquée partout
            # ailleurs (_is_billable, correct_billable_for_contract_projects)
            # : SEUL un CONTRAT (prix coûtant majoré) est refacturable par
            # défaut. On rétablit ça. Ne touche PAS aux dépenses déjà
            # refacturées (invoiced_at / facture_item_id posés). One-shot
            # (NOT EXISTS) : les cases cochées à la main APRÈS ce backfill
            # ne sont jamais re-décochées → « manuel permis ». Le reflet
            # dans QB (BillableStatus=NotBillable) est fait par le filet QBO.
            """
            UPDATE achats a
            SET is_billable = FALSE
            FROM projects p
            LEFT JOIN soumissions s ON s.id = p.soumission_id
            WHERE a.project_id = p.id
              AND a.is_billable = TRUE
              AND a.invoiced_at IS NULL
              AND a.facture_item_id IS NULL
              AND (s.id IS NULL OR s.kind <> 'contract')
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'achat_unbill_non_contract_v2'
              )
            """,
            """
            INSERT INTO applied_backfills (key)
            VALUES ('achat_unbill_non_contract_v2')
            ON CONFLICT (key) DO NOTHING
            """,
            # One-shot : « dé-refacture » les achats/punches ORPHELINS d'une
            # ligne de facture supprimée AVANT le correctif delete_item :
            # le FK facture_item_id est passé à NULL (SET NULL) mais
            # invoiced_at est resté posé → statut « ✓ Refacturé » verrouillé
            # à tort, réimport impossible (cas 8900 : toutes les lignes
            # supprimées, achats restés refacturés). Garde de récence 60 j :
            # l'import pose TOUJOURS facture_item_id depuis longtemps, donc
            # un lien NULL + invoiced_at RÉCENT = orphelin de ligne
            # supprimée ; les refacturations anciennes (legacy pré-lien) ne
            # sont pas touchées.
            """
            UPDATE achats
            SET invoiced_at = NULL
            WHERE facture_item_id IS NULL
              AND invoiced_at IS NOT NULL
              AND invoiced_at >= NOW() - INTERVAL '60 days'
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'achat_unrefacture_orphans_v1'
              )
            """,
            """
            UPDATE punches
            SET invoiced_at = NULL
            WHERE facture_item_id IS NULL
              AND invoiced_at IS NOT NULL
              AND invoiced_at >= NOW() - INTERVAL '60 days'
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'achat_unrefacture_orphans_v1'
              )
            """,
            """
            INSERT INTO applied_backfills (key)
            VALUES ('achat_unrefacture_orphans_v1')
            ON CONFLICT (key) DO NOTHING
            """,
            # One-shot MÉNAGE : supprime de Kratos les factures fournisseurs
            # IMPORTÉES de QB en masse SANS aucun lien Kratos (ni projet,
            # ni BT, ni client) et jamais touchées par l'utilisateur —
            # l'import « sync-from-qbo » ramenait TOUTES les factures QB.
            # Critères stricts « importé et intact » : lien QB présent,
            # aucun rattachement, pas refacturé, pas de PO, pas de reçu,
            # is_billable FALSE (défaut d'import ; une saisie Kratos est
            # TRUE par défaut et un choix manuel pose billable_manual),
            # référence et notes vides. Les objets QB ne sont PAS touchés
            # (ces factures continuent de vivre dans QuickBooks).
            """
            DELETE FROM achats
            WHERE (qbo_bill_id IS NOT NULL OR qbo_purchase_id IS NOT NULL)
              AND project_id IS NULL
              AND client_id IS NULL
              AND invoiced_at IS NULL
              AND facture_item_id IS NULL
              AND purchase_order_id IS NULL
              AND receipt_image_content_type IS NULL
              AND is_billable = FALSE
              AND billable_manual = FALSE
              AND COALESCE(reference, '') = ''
              AND COALESCE(notes, '') = ''
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'achat_menage_qb_sans_lien_v1'
              )
            """,
            """
            INSERT INTO applied_backfills (key)
            VALUES ('achat_menage_qb_sans_lien_v1')
            ON CONFLICT (key) DO NOTHING
            """,
            # v2 : même ménage pour les factures importées de QB reliées à
            # un CLIENT SEUL (sans projet/BT) — la règle est « client ET
            # projet/BT » : un client seul ne justifie pas l'import. Mêmes
            # critères stricts « importé et intact » ; les reçus saisis
            # DANS Kratos sur un client sont exclus (is_billable posé
            # explicitement à la création → billable_manual TRUE).
            """
            DELETE FROM achats
            WHERE (qbo_bill_id IS NOT NULL OR qbo_purchase_id IS NOT NULL)
              AND project_id IS NULL
              AND client_id IS NOT NULL
              AND invoiced_at IS NULL
              AND facture_item_id IS NULL
              AND purchase_order_id IS NULL
              AND receipt_image_content_type IS NULL
              AND is_billable = FALSE
              AND billable_manual = FALSE
              AND COALESCE(reference, '') = ''
              AND COALESCE(notes, '') = ''
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'achat_menage_qb_client_seul_v2'
              )
            """,
            """
            INSERT INTO applied_backfills (key)
            VALUES ('achat_menage_qb_client_seul_v2')
            ON CONFLICT (key) DO NOTHING
            """,
            # v3 : ménage RENFORCÉ des factures IMPORTÉES de QB sans
            # projet/BT — les v1/v2 rataient celles portant un REÇU (les
            # images Rona viennent de QB elles-mêmes) ou un drapeau
            # is_billable modifié par un automatisme. Règle finale : une
            # facture VENUE de QB sans projet/BT ne vit pas dans Kratos,
            # reliée à un client ou pas. Restent protégées : les saisies
            # Kratos (billable_manual posé au choix explicite de création,
            # référence ou notes remplies), le refacturé, et les achats
            # issus d'un PO. Les objets QuickBooks ne sont pas touchés.
            """
            DELETE FROM achats
            WHERE (qbo_bill_id IS NOT NULL OR qbo_purchase_id IS NOT NULL)
              AND project_id IS NULL
              AND invoiced_at IS NULL
              AND facture_item_id IS NULL
              AND purchase_order_id IS NULL
              AND billable_manual = FALSE
              AND COALESCE(reference, '') = ''
              AND COALESCE(notes, '') = ''
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'achat_menage_qb_sans_projet_v3'
              )
            """,
            """
            INSERT INTO applied_backfills (key)
            VALUES ('achat_menage_qb_sans_projet_v3')
            ON CONFLICT (key) DO NOTHING
            """,
            # One-shot : resynchronise les TOTAUX STOCKÉS des factures avec
            # la somme réelle de leurs items. L'import « Importer du
            # projet » sur une facture existante ne recalculait pas
            # Facture.total → le kanban (total en base) divergeait de
            # l'éditeur (calcul depuis les lignes), ex. 2 618,85 $ vs
            # 6 730,43 $. Même arithmétique que _recompute_facture_totals
            # (TPS 5 % et TVQ 9,975 % arrondies séparément).
            """
            UPDATE factures f
            SET subtotal = s.sub,
                tps = ROUND(s.sub * 0.05, 2),
                tvq = ROUND(s.sub * 0.09975, 2),
                total = s.sub + ROUND(s.sub * 0.05, 2)
                        + ROUND(s.sub * 0.09975, 2)
            FROM (
                SELECT facture_id,
                       ROUND(SUM(COALESCE(total, 0))::numeric, 2) AS sub
                FROM facture_items
                GROUP BY facture_id
            ) s
            WHERE s.facture_id = f.id
              AND COALESCE(f.subtotal, -1) <> s.sub
              AND NOT EXISTS (
                  SELECT 1 FROM applied_backfills
                  WHERE key = 'facture_totals_resync_v1'
              )
            """,
            """
            INSERT INTO applied_backfills (key)
            VALUES ('facture_totals_resync_v1')
            ON CONFLICT (key) DO NOTHING
            """,
        ):
            try:
                await conn.execute(text(sql))
            except Exception as exc:
                # Table absente / colonne pas encore migrée — on
                # passe sans bloquer le boot.
                log.warning("init_db: backfill applied_backfills (batch) échouée: %s", exc)

        # Rétroactif (one-shot) : faire pivoter de 90° HORAIRE tous les
        # reçus d'achat déjà stockés. Ils ont été numérisés avant la
        # correction d'orientation et sont enregistrés de côté. Les
        # nouveaux reçus passent par le recadrage (déjà à l'endroit) et
        # ne sont PAS touchés car le marqueur empêche un second passage.
        # Gardé + exception-safe pour ne jamais bloquer le boot.
        try:
            done = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM applied_backfills WHERE key = :k"
                    ),
                    {"k": "rotate_existing_receipts_cw90_v1"},
                )
            ).first()
        except Exception as exc:
            done = True  # table pas prête — on retentera au prochain boot
            log.warning("init_db: lecture marqueur rotation reçus échouée: %s", exc)
        if not done:
            try:
                n = await _rotate_existing_receipts_cw90(conn)
                await conn.execute(
                    text(
                        "INSERT INTO applied_backfills (key) VALUES (:k) "
                        "ON CONFLICT (key) DO NOTHING"
                    ),
                    {"k": "rotate_existing_receipts_cw90_v1"},
                )
                log.info(f"[init_db] reçus pivotés 90° horaire : {n}")
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[init_db] rotation reçus échouée : {exc}")

        # Rétroactif (one-shot) : re-taguer en « extra » les lignes de
        # facture hors-contrat générées automatiquement AVANT que le code
        # ne pose kind="extra" (heures T&M, matériel, sous-traitant). Ces
        # lignes portent des préfixes système fiables. Sans ça, les
        # factures existantes : (a) gonflent à tort la « cible cumulative »
        # de la soumission de base et bloquent la facturation, (b) faussent
        # l'état de compte (faux trop-payé). Le marqueur empêche d'écraser
        # les ajustements manuels faits ensuite.
        try:
            done = (
                await conn.execute(
                    text("SELECT 1 FROM applied_backfills WHERE key = :k"),
                    {"k": "retag_extra_facture_items_v1"},
                )
            ).first()
        except Exception as exc:
            done = True
            log.warning("init_db: lecture marqueur retag extra échouée: %s", exc)
        if not done:
            try:
                res = await conn.execute(
                    text(
                        "UPDATE facture_items SET kind = 'extra' "
                        "WHERE kind = 'service' AND ("
                        "description LIKE 'Main-d''œuvre — %' "
                        "OR description LIKE 'Matériel — %' "
                        "OR description LIKE 'Sous-traitant — %')"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO applied_backfills (key) VALUES (:k) "
                        "ON CONFLICT (key) DO NOTHING"
                    ),
                    {"k": "retag_extra_facture_items_v1"},
                )
                log.info(
                    f"[init_db] lignes facture re-taguées extra : "
                    f"{res.rowcount}"
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[init_db] retag extra échoué : {exc}")

        # Seed des types de RV par défaut. Idempotent :
        # INSERT ... ON CONFLICT DO NOTHING. L'admin peut modifier
        # depuis l'UI ensuite (couleur, durée, buffer).
        for slug, label, duration, prep, roles, color, travel in (
            (
                "evaluation_soumission",
                "Évaluation soumission (chez le client)",
                90,
                15,
                "closer",
                "0ea5e9",
                True,
            ),
            (
                "visite_chantier",
                "Visite de chantier",
                30,
                0,
                "charge_projet,closer",
                "10b981",
                True,
            ),
            (
                "reunion_interne",
                "Réunion interne (bureau)",
                30,
                0,
                None,
                "a855f7",
                False,
            ),
            (
                "inspection_finale",
                "Inspection finale / livraison",
                45,
                15,
                "closer,charge_projet",
                "f59e0b",
                True,
            ),
            (
                "appel_telephone",
                "Appel téléphonique planifié",
                15,
                0,
                None,
                "64748b",
                False,
            ),
        ):
            try:
                await conn.execute(
                    text(
                        """
                        INSERT INTO appointment_types
                          (slug, label, default_duration_min,
                           prep_buffer_min, allowed_roles_csv, color,
                           requires_travel, active, created_at)
                        VALUES (:slug, :label, :duration, :prep,
                                :roles, :color, :travel, TRUE, NOW())
                        ON CONFLICT (slug) DO NOTHING
                        """
                    ),
                    {
                        "slug": slug,
                        "label": label,
                        "duration": duration,
                        "prep": prep,
                        "roles": roles,
                        "color": color,
                        "travel": travel,
                    },
                )
            except Exception as exc:
                log.warning("init_db: seed appointment_types échouée: %s", exc)

        # Seed des défauts globaux d'analyse financière (mai 2026,
        # étendu mai 2026 pour couvrir TOUS les inputs manuels +
        # frais MDF — PR « extend-analysis-defaults-tous-champs »).
        #
        # Permet à Phil de modifier les valeurs pré-remplies pour les
        # nouvelles fiches d'analyse depuis l'UI (bouton ⚙️ « Modifier
        # les défauts »). Stockés en pourcentage (3.75 = 3.75 %, 25.0
        # = 25 %, 8.0 = 8 %) ou en $ selon le champ. Le `step` permet
        # à l'UI de deviner le format (< 1 → %, >= 1 → $).
        # Idempotent :
        #   - ON CONFLICT (key) DO UPDATE SET group_name pour garder
        #     les renommages de groupes en sync sans toucher aux
        #     valeurs déjà modifiées par Phil.
        #   - INSERT des nouvelles clés via DO NOTHING équivalent.
        # Modifier un défaut ne change que les FUTURES analyses, pas
        # les existantes.
        #
        # Migration douce des anciens noms de groupes :
        #   - 'refi' → 'inputs_manuels' (libellé plus clair)
        #   - 'mdf'  → 'inputs_manuels' (mdf_preteur_b_pct est un
        #              input manuel, pas un frais)
        #   - nouveaux frais MDF → groupe 'mdf_frais'
        #
        # Juin 2026 — Dé-hardcodage du moteur d'analyse de lead
        # (PR « prospection-config-dehardcode-1a »). On externalise vers
        # cette table les constantes encore codées en dur dans
        # ``lead_analysis_finance`` via 3 nouveaux groupes :
        #   - 'depenses_normalisees' : barème SCHL (concierge, entretien,
        #     gestion, wifi, internet, thermopompe, seuil 12 log).
        #   - 'scenarios_financement' : LTV / amortissement / RCD des 4
        #     scénarios (achat, SCHL std, APH 50, APH 100).
        #   - 'baremes_fiscaux' : ratio abordabilité APH (0.40) + barème
        #     progressif des taxes de bienvenue de Montréal (value_json).
        # Les valeurs seedées = EXACTEMENT les constantes actuelles ; le
        # moteur lit la config si présente, sinon retombe sur la
        # constante (fallback ultime). Résultat identique au centime
        # tant que personne ne modifie rien.
        try:
            await conn.execute(
                text(
                    """
                    UPDATE prospection_analysis_defaults
                       SET group_name = 'inputs_manuels'
                     WHERE group_name IN ('refi', 'mdf')
                    """
                )
            )
        except Exception as exc:
            # Table absente — sera créée par create_all + retentée
            # au prochain boot.
            log.warning("init_db: migration groupes prospection_analysis_defaults échouée: %s", exc)

        # Liste exhaustive des défauts.
        # Champs des inputs manuels (groupe 'inputs_manuels') :
        #   - stockés en pct (step < 1) ou unités entières (step >= 1).
        # Frais MDF (groupe 'mdf_frais') :
        #   - frais_* : montants $ one-shot (step = 50).
        #   - pct_courtier_hypothecaire_* : %, appliqué au prix d'achat
        #     ou financement APH (step = 0.05).
        for key, value_float, label_fr, description_fr, mn, mx, step, group in (
                # ── Groupe : Inputs manuels ──────────────────────────
                (
                    "taux_interet_refi",
                    3.75,
                    "Taux d'intérêt refi (%)",
                    "Taux d'intérêt utilisé pour calculer le refinancement "
                    "(SCHL, APH 50, APH 100).",
                    0.0,
                    25.0,
                    0.05,
                    "inputs_manuels",
                ),
                (
                    "taux_interet_preteur_b_projet",
                    8.0,
                    "Taux d'intérêt prêteur B (pendant projet) (%)",
                    "Taux d'intérêt appliqué par le prêteur B pendant la "
                    "phase chantier (typique 8 % en 2024-2025). Utilisé "
                    "pour calculer les intérêts de portage (L17).",
                    0.0,
                    30.0,
                    0.05,
                    "inputs_manuels",
                ),
                (
                    "mdf_preteur_b_pct",
                    25.0,
                    "% MDF prêteur B (%)",
                    "Pourcentage de mise de fonds requis par le prêteur B "
                    "(privé, hypothèque conventionnelle 75 % LTV). Varie "
                    "selon le prêteur (25 % typique, parfois 35 %).",
                    0.0,
                    100.0,
                    0.5,
                    "inputs_manuels",
                ),
                (
                    "tga_pct",
                    4.0,
                    "TGA — Taux global d'actualisation (%)",
                    "Taux d'actualisation utilisé pour calculer la valeur "
                    "économique TGA (R54 dans l'Excel). Défaut marché : 4 %.",
                    0.0,
                    20.0,
                    0.05,
                    "inputs_manuels",
                ),
                (
                    "taux_interet_achat_pct",
                    4.0,
                    "Taux d'intérêt prêt à l'achat (%)",
                    "Taux d'intérêt appliqué au scénario d'achat "
                    "conventionnel (75 % LTV, 25 ans, RCD 1.20).",
                    0.0,
                    25.0,
                    0.05,
                    "inputs_manuels",
                ),
                (
                    "reduction_energie_pct",
                    0.0,
                    "Réduction énergie post-refi (%)",
                    "Réduction estimée de la facture d'énergie après "
                    "travaux d'efficacité (appliquée seulement aux "
                    "scénarios refi).",
                    0.0,
                    100.0,
                    1.0,
                    "inputs_manuels",
                ),
                (
                    "duree_projet_annees",
                    2.0,
                    "Durée du projet (années)",
                    "Durée typique chantier + lease-up avant refi. Utilisée "
                    "pour calculer L17 (intérêts pendant projet) et L18 "
                    "(revenus nets pendant projet).",
                    1.0,
                    10.0,
                    1.0,
                    "inputs_manuels",
                ),
                (
                    "nb_logements_ajoutes",
                    0.0,
                    "Logements ajoutés par défaut",
                    "Nombre de logements créés en moyenne par projet. "
                    "Pré-rempli sur les nouvelles fiches (modifiable).",
                    0.0,
                    50.0,
                    1.0,
                    "inputs_manuels",
                ),
                (
                    "nb_thermopompes_ajoutees",
                    0.0,
                    "Thermopompes ajoutées par défaut",
                    "Nombre de thermopompes installées en moyenne (impacte "
                    "uniquement les scénarios APH — efficacité énergétique).",
                    0.0,
                    50.0,
                    1.0,
                    "inputs_manuels",
                ),
                (
                    "taux_inoccupation_pct",
                    3.0,
                    "Taux d'inoccupation (%)",
                    "Pourcentage de perte de loyer hypothèse SCHL. Varie "
                    "par marché (3 % Montréal, plus en région).",
                    0.0,
                    30.0,
                    0.1,
                    "inputs_manuels",
                ),
                # ── Groupe : Frais MDF (one-shot) ────────────────────
                (
                    "frais_evaluateur",
                    1500.0,
                    "Évaluateur agréé ($)",
                    "Frais d'évaluation principal (un seul rapport).",
                    0.0,
                    20000.0,
                    50.0,
                    "mdf_frais",
                ),
                (
                    "frais_evaluateur_2",
                    1500.0,
                    "Évaluateur agréé 2 ($)",
                    "Deuxième évaluation (ex. refi SCHL exige souvent un "
                    "second évaluateur indépendant).",
                    0.0,
                    20000.0,
                    50.0,
                    "mdf_frais",
                ),
                (
                    "frais_inspection",
                    1700.0,
                    "Inspection ($)",
                    "Inspection préachat (bâtiment + mécanique).",
                    0.0,
                    20000.0,
                    50.0,
                    "mdf_frais",
                ),
                (
                    "frais_avocat",
                    4000.0,
                    "Avocat ($)",
                    "Honoraires juridiques (vérification diligente, "
                    "négociations, contrats).",
                    0.0,
                    50000.0,
                    50.0,
                    "mdf_frais",
                ),
                (
                    "frais_notaire",
                    1600.0,
                    "Notaire ($)",
                    "Frais de notaire pour l'acte d'achat (vente).",
                    0.0,
                    20000.0,
                    50.0,
                    "mdf_frais",
                ),
                (
                    "frais_notaire_2",
                    1600.0,
                    "Notaire 2 ($)",
                    "Frais de notaire pour l'acte de refinancement "
                    "(hypothèque SCHL/APH après projet).",
                    0.0,
                    20000.0,
                    50.0,
                    "mdf_frais",
                ),
                (
                    "frais_rapport_efficacite",
                    4500.0,
                    "Rapport d'efficacité énergétique ($)",
                    "Rapport requis pour les programmes SCHL APH 50/100 "
                    "(efficacité énergétique + abordabilité).",
                    0.0,
                    20000.0,
                    50.0,
                    "mdf_frais",
                ),
                (
                    "pct_courtier_hypothecaire_1",
                    1.0,
                    "Courtier hypothécaire 1 (% × prix d'achat)",
                    "Pourcentage facturé par le courtier hypothécaire sur "
                    "le prêt à l'achat. Défaut 1 %.",
                    0.0,
                    5.0,
                    0.05,
                    "mdf_frais",
                ),
                (
                    "pct_courtier_hypothecaire_2",
                    1.0,
                    "Courtier hypothécaire 2 (% × financement APH)",
                    "Pourcentage facturé par le courtier hypothécaire sur "
                    "le financement refi APH (post-projet). Défaut 1 %.",
                    0.0,
                    5.0,
                    0.05,
                    "mdf_frais",
                ),
                # ── Mai 2026 : Frais de dossier du prêteur B ──────────
                # Pourcentage appliqué au prêt initial du prêteur B
                # (= prix_achat × ltv_achat, 75 % typique). Stocké en
                # pct (2.0 = 2 %) comme les autres %. Non finançable par
                # défaut (Phil paie cash en pratique).
                (
                    "frais_dossier_preteur_pct",
                    2.0,
                    "Frais de dossier du prêteur (% × prêt initial)",
                    "Pourcentage facturé par le prêteur B sur le prêt "
                    "initial (= prix d'achat × LTV à l'achat, 75 % "
                    "typique). Défaut 2 %.",
                    0.0,
                    10.0,
                    0.05,
                    "mdf_frais",
                ),
                # ── Sept. 2026 : frais de dossier — institution
                # traditionnelle. Montant FIXE en $ (retour Phil
                # 2026-09-04 : « 5 000 $ de base au lieu d'un %  »).
                (
                    "frais_dossier_trad",
                    5000.0,
                    "Frais de dossier — institution traditionnelle ($)",
                    "Montant fixe facturé par l'institution (conventionnel / SCHL / APH) à l'achat. Défaut 5 000 $.",
                    0.0,
                    100000.0,
                    100.0,
                    "mdf_frais",
                ),
                # ── Groupe : Dépenses normalisées SCHL (juin 2026) ───
                # Barème ``lead_analysis_finance.BAREME`` externalisé.
                # Valeurs = EXACTEMENT les constantes hardcoded. Les % de
                # gestion sont seedés en pct (4.25, 5.0) et reconvertis en
                # fraction (÷100) au runtime côté loader.
                (
                    "conciergerie_moins_12_log",
                    215.0,
                    "Conciergerie — moins de 12 log ($/log/an)",
                    "Frais de conciergerie normalisés par logement et par "
                    "an pour un immeuble de moins de 12 logements.",
                    0.0,
                    5000.0,
                    5.0,
                    "depenses_normalisees",
                ),
                (
                    "conciergerie_12_log_plus",
                    365.0,
                    "Conciergerie — 12 log et plus ($/log/an)",
                    "Frais de conciergerie normalisés par logement et par "
                    "an pour un immeuble de 12 logements ou plus.",
                    0.0,
                    5000.0,
                    5.0,
                    "depenses_normalisees",
                ),
                (
                    "entretien_par_log",
                    610.0,
                    "Entretien ($/log/an)",
                    "Frais d'entretien normalisés par logement et par an.",
                    0.0,
                    10000.0,
                    10.0,
                    "depenses_normalisees",
                ),
                (
                    "gestion_moins_12_pct",
                    4.25,
                    "Gestion — moins de 12 log (% des revenus)",
                    "Pourcentage des revenus alloué à la gestion pour un "
                    "immeuble de moins de 12 logements.",
                    0.0,
                    20.0,
                    0.05,
                    "depenses_normalisees",
                ),
                (
                    "gestion_12_log_plus_pct",
                    5.0,
                    "Gestion — 12 log et plus (% des revenus)",
                    "Pourcentage des revenus alloué à la gestion pour un "
                    "immeuble de 12 logements ou plus.",
                    0.0,
                    20.0,
                    0.05,
                    "depenses_normalisees",
                ),
                (
                    "wifi_par_log_mois",
                    5.0,
                    "WIFI ($/log/mois)",
                    "Coût WIFI normalisé par logement et par mois (ajouté "
                    "seulement aux scénarios refi si l'option WIFI est "
                    "activée).",
                    0.0,
                    100.0,
                    0.5,
                    "depenses_normalisees",
                ),
                (
                    "internet_batiment_mois",
                    120.0,
                    "Internet du bâtiment ($/mois)",
                    "Coût fixe de la connexion internet du bâtiment par "
                    "mois (ajouté seulement aux scénarios refi si l'option "
                    "WIFI est activée).",
                    0.0,
                    2000.0,
                    5.0,
                    "depenses_normalisees",
                ),
                (
                    "entretien_thermopompe_an",
                    190.0,
                    "Entretien thermopompe ($/thermopompe/an)",
                    "Coût d'entretien annuel par thermopompe ajoutée "
                    "(scénarios APH — efficacité énergétique uniquement).",
                    0.0,
                    5000.0,
                    5.0,
                    "depenses_normalisees",
                ),
                (
                    "autres_normalisations_pct",
                    1.0,
                    "Autres normalisations (% des revenus bruts)",
                    "Provision normalisée pour les autres dépenses "
                    "d'exploitation, en pourcentage des revenus bruts. "
                    "Défaut 1 %.",
                    0.0,
                    20.0,
                    0.05,
                    "depenses_normalisees",
                ),
                (
                    "seuil_bascule_bareme_log",
                    12.0,
                    "Seuil de bascule du barème (nb log)",
                    "Nombre de logements à partir duquel on bascule des "
                    "tarifs « petit immeuble » (conciergerie/gestion) vers "
                    "les tarifs « grand immeuble ». Défaut 12.",
                    1.0,
                    100.0,
                    1.0,
                    "depenses_normalisees",
                ),
                # ── Groupe : Scénarios de financement (juin 2026) ────
                # LTV / amortissement / RCD des 4 scénarios. Valeurs =
                # EXACTEMENT les dataclasses ``SCENARIO_*`` hardcoded.
                # LTV et RCD en décimal (0.75, 1.20) ; amort en années.
                (
                    "scenario_achat_ltv",
                    0.75,
                    "Achat — LTV (ratio prêt/valeur)",
                    "Ratio prêt/valeur du scénario d'achat conventionnel "
                    "(prêteur B). Défaut 0.75 (75 %).",
                    0.0,
                    1.0,
                    0.01,
                    "scenarios_financement",
                ),
                (
                    "scenario_achat_amort",
                    25.0,
                    "Achat — Amortissement (années)",
                    "Période d'amortissement du scénario d'achat "
                    "conventionnel. Défaut 25 ans.",
                    1.0,
                    50.0,
                    1.0,
                    "scenarios_financement",
                ),
                (
                    "scenario_achat_rcd",
                    1.20,
                    "Achat — RCD (ratio couverture de dette)",
                    "Ratio de couverture de dette du scénario d'achat "
                    "conventionnel. Défaut 1.20.",
                    1.0,
                    3.0,
                    0.01,
                    "scenarios_financement",
                ),
                (
                    "scenario_schl_std_ltv",
                    0.85,
                    "SCHL standard — LTV (ratio prêt/valeur)",
                    "Ratio prêt/valeur du scénario refi SCHL standard. "
                    "Défaut 0.85 (85 %).",
                    0.0,
                    1.0,
                    0.01,
                    "scenarios_financement",
                ),
                (
                    "scenario_schl_std_amort",
                    35.0,
                    "SCHL standard — Amortissement (années)",
                    "Période d'amortissement du scénario refi SCHL "
                    "standard. Défaut 35 ans.",
                    1.0,
                    50.0,
                    1.0,
                    "scenarios_financement",
                ),
                (
                    "scenario_schl_std_rcd",
                    1.30,
                    "SCHL standard — RCD (ratio couverture de dette)",
                    "Ratio de couverture de dette du scénario refi SCHL "
                    "standard. Défaut 1.30.",
                    1.0,
                    3.0,
                    0.01,
                    "scenarios_financement",
                ),
                (
                    "scenario_aph50_ltv",
                    0.85,
                    "APH 50 pts — LTV (ratio prêt/valeur)",
                    "Ratio prêt/valeur du scénario refi SCHL Efficacité "
                    "énergétique (50 pts). Défaut 0.85 (85 %).",
                    0.0,
                    1.0,
                    0.01,
                    "scenarios_financement",
                ),
                (
                    "scenario_aph50_amort",
                    40.0,
                    "APH 50 pts — Amortissement (années)",
                    "Période d'amortissement du scénario refi APH 50 pts. "
                    "Défaut 40 ans.",
                    1.0,
                    50.0,
                    1.0,
                    "scenarios_financement",
                ),
                (
                    "scenario_aph50_rcd",
                    1.10,
                    "APH 50 pts — RCD (ratio couverture de dette)",
                    "Ratio de couverture de dette du scénario refi APH 50 "
                    "pts. Défaut 1.10.",
                    1.0,
                    3.0,
                    0.01,
                    "scenarios_financement",
                ),
                (
                    "scenario_aph100_ltv",
                    0.95,
                    "APH 100 pts — LTV (ratio prêt/valeur)",
                    "Ratio prêt/valeur du scénario refi SCHL Abordabilité "
                    "+ Efficacité (100 pts). Défaut 0.95 (95 %).",
                    0.0,
                    1.0,
                    0.01,
                    "scenarios_financement",
                ),
                (
                    "scenario_aph100_amort",
                    50.0,
                    "APH 100 pts — Amortissement (années)",
                    "Période d'amortissement du scénario refi APH 100 pts. "
                    "Défaut 50 ans.",
                    1.0,
                    50.0,
                    1.0,
                    "scenarios_financement",
                ),
                (
                    "scenario_aph100_rcd",
                    1.10,
                    "APH 100 pts — RCD (ratio couverture de dette)",
                    "Ratio de couverture de dette du scénario refi APH 100 "
                    "pts. Défaut 1.10.",
                    1.0,
                    3.0,
                    0.01,
                    "scenarios_financement",
                ),
                # ── Groupe : Barèmes fiscaux (juin 2026) ─────────────
                # Ratio d'abordabilité APH SELECT. Valeur = EXACTEMENT la
                # constante hardcoded ``RATIO_ABORDABILITE_APH`` (0.40).
                # Stocké en décimal (0.40), passé tel quel au moteur.
                # (Le barème des taxes de bienvenue est seedé à part car
                # il utilise ``value_json`` — voir bloc dédié plus bas.)
                (
                    "ratio_abordabilite_aph",
                    0.40,
                    "Ratio d'abordabilité APH (proportion de logements)",
                    "Proportion des logements qui doivent être abordables "
                    "dans le scénario APH 100 pts (nb_abordables = "
                    "plafond(ratio × nb_total)). Défaut 0.40 (40 %).",
                    0.0,
                    1.0,
                    0.01,
                    "baremes_fiscaux",
                ),
                # ── Groupe : Défauts du TRI investisseur (juin 2026) ──
                # Pré-remplissage des 3 intrants MANUELS du calculateur
                # de TRI (onglet TRI de la fiche d'analyse) quand la
                # fiche n'a pas encore de valeur persistée. Stockés en
                # POURCENTAGE (50.0 = 50 %, 3.0 = 3 %) ; convertis en
                # fraction (÷100) au runtime côté ``get_tri_inputs``
                # (pct, cr_loyers, cr_dep). Le ``capital`` n'a PAS de
                # défaut global (propre à chaque deal). Modifiables dans
                # Paramètres (GET ?group=tri_defaults).
                (
                    "tri_pct_investisseur_defaut",
                    50.0,
                    "TRI — % détenu par l'investisseur (défaut)",
                    "Pourcentage des parts détenu par l'investisseur "
                    "minoritaire, pré-rempli dans le calculateur de TRI "
                    "quand la fiche n'a pas de valeur saisie. Défaut 50 %.",
                    0.0,
                    100.0,
                    0.5,
                    "tri_defaults",
                ),
                (
                    "tri_croissance_loyers_defaut",
                    3.0,
                    "TRI — croissance annuelle des loyers (défaut)",
                    "Taux de croissance annuel composé des loyers, "
                    "pré-rempli dans le calculateur de TRI quand la fiche "
                    "n'a pas de valeur saisie. Défaut 3 %.",
                    0.0,
                    20.0,
                    0.1,
                    "tri_defaults",
                ),
                (
                    "tri_croissance_depenses_defaut",
                    3.0,
                    "TRI — croissance annuelle des dépenses (défaut)",
                    "Taux de croissance annuel composé des dépenses "
                    "d'opération, pré-rempli dans le calculateur de TRI "
                    "quand la fiche n'a pas de valeur saisie. Défaut 3 %.",
                    0.0,
                    20.0,
                    0.1,
                    "tri_defaults",
                ),
        ):
            try:
                # UPSERT : on insère si la clé n'existe pas, sinon on
                # met UNIQUEMENT à jour les métadonnées (label, group,
                # bornes) — pas la `value_float` modifiée par Phil.
                await conn.execute(
                    text(
                        """
                        INSERT INTO prospection_analysis_defaults
                          (key, value_float, label_fr, description_fr,
                           min_value, max_value, step, group_name,
                           updated_at)
                        VALUES (:key, :value_float, :label_fr,
                                :description_fr, :mn, :mx, :step, :group,
                                NOW())
                        ON CONFLICT (key) DO UPDATE SET
                            label_fr       = EXCLUDED.label_fr,
                            description_fr = EXCLUDED.description_fr,
                            min_value      = EXCLUDED.min_value,
                            max_value      = EXCLUDED.max_value,
                            step           = EXCLUDED.step,
                            group_name     = EXCLUDED.group_name
                        """
                    ),
                    {
                        "key": key,
                        "value_float": value_float,
                        "label_fr": label_fr,
                        "description_fr": description_fr,
                        "mn": mn,
                        "mx": mx,
                        "step": step,
                        "group": group,
                    },
                )
            except Exception as exc:
                # Table absente au tout premier boot (create_all n'a
                # pas encore tourné) — retentera au prochain démarrage.
                log.warning("init_db: upsert prospection_analysis_defaults (%s) échouée: %s", key, exc)

        # ── Seed du barème des taxes de bienvenue (juin 2026) ────────
        # Défaut à valeur structurée (``value_json``) : barème progressif
        # des taxes de bienvenue de Montréal. Valeur = EXACTEMENT les 7
        # paliers hardcoded dans ``lead_analysis_finance`` (seuils
        # 61500/307800/552300/1104700/2136500/3113000 ; taux
        # 0.5/1.0/1.5/2.0/2.5/3.5/4.0 %). Le dernier palier a ``seuil``
        # null (palier ouvert → inf). ``taux_pct`` en pourcentage,
        # reconverti en fraction (÷100) au runtime côté loader.
        #
        # UPSERT idempotent : INSERT avec ``value_json`` si la clé est
        # absente ; sur conflit on ne met à jour QUE les métadonnées
        # (label/desc/group) — pas ``value_json`` (préserve un barème
        # déjà modifié par l'utilisateur via le PATCH).
        taxes_bienvenue_json = json.dumps([
            {"seuil": 61500, "taux_pct": 0.5},
            {"seuil": 307800, "taux_pct": 1.0},
            {"seuil": 552300, "taux_pct": 1.5},
            {"seuil": 1104700, "taux_pct": 2.0},
            {"seuil": 2136500, "taux_pct": 2.5},
            {"seuil": 3113000, "taux_pct": 3.5},
            {"seuil": None, "taux_pct": 4.0},
        ])
        try:
            await conn.execute(
                text(
                    """
                    INSERT INTO prospection_analysis_defaults
                      (key, value_json, label_fr, description_fr,
                       step, group_name, updated_at)
                    VALUES (:key, CAST(:value_json AS JSONB), :label_fr,
                            :description_fr, :step, :group, NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        label_fr       = EXCLUDED.label_fr,
                        description_fr = EXCLUDED.description_fr,
                        group_name     = EXCLUDED.group_name
                    """
                ),
                {
                    "key": "taxes_bienvenue_mtl",
                    "value_json": taxes_bienvenue_json,
                    "label_fr": "Taxes de bienvenue — Montréal (paliers)",
                    "description_fr": (
                        "Barème progressif des taxes de bienvenue de "
                        "Montréal. Liste de paliers {seuil, taux_pct} ; "
                        "le dernier palier (seuil null) couvre tout au-"
                        "dessus du dernier seuil. taux_pct en pourcentage."
                    ),
                    "step": 0.01,
                    "group": "baremes_fiscaux",
                },
            )
        except Exception as exc:
            # Table absente au tout premier boot — retentera plus tard.
            log.warning("init_db: seed taxes_bienvenue_mtl échouée: %s", exc)

        # ── Seed des frais de démarrage PERSONNALISÉS (juin 2026) ────
        # Défaut à valeur structurée (``value_json``) : LISTE des postes
        # de frais de démarrage personnalisés (ajoutables/retirables par
        # l'admin depuis l'app). Seedé à liste VIDE → aucun poste
        # personnalisé par défaut, donc résultat du moteur STRICTEMENT
        # identique à avant. Chaque item (ajouté via les endpoints CRUD)
        # a la forme ``{id, label_fr, type_montant, valeur,
        # financable_par_defaut}``.
        #
        # UPSERT idempotent : INSERT avec ``value_json = []`` si la clé
        # est absente ; sur conflit on ne met à jour QUE les métadonnées
        # (label/desc/group) — JAMAIS ``value_json`` (préserve la liste
        # déjà construite par l'admin via les endpoints).
        try:
            await conn.execute(
                text(
                    """
                    INSERT INTO prospection_analysis_defaults
                      (key, value_json, label_fr, description_fr,
                       step, group_name, updated_at)
                    VALUES (:key, CAST(:value_json AS JSONB), :label_fr,
                            :description_fr, :step, :group, NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        label_fr       = EXCLUDED.label_fr,
                        description_fr = EXCLUDED.description_fr,
                        group_name     = EXCLUDED.group_name
                    """
                ),
                {
                    "key": "frais_mdf_custom",
                    "value_json": json.dumps([]),
                    "label_fr": "Frais de démarrage personnalisés (liste)",
                    "description_fr": (
                        "Liste des postes de frais de démarrage "
                        "personnalisés ajoutés par l'admin. Chaque poste "
                        "{id, label_fr, type_montant, valeur, "
                        "financable_par_defaut} ; type_montant ∈ {fixe, "
                        "pct_prix_achat, pct_financement}. Vide par "
                        "défaut (aucun impact sur le calcul)."
                    ),
                    "step": 0.01,
                    "group": "mdf_frais",
                },
            )
        except Exception as exc:
            # Table absente au tout premier boot — retentera plus tard.
            log.warning("init_db: seed frais_mdf_custom échouée: %s", exc)

        # ── Seed du REGISTRE unifié des frais de démarrage (juin 2026) ──
        # Défaut à valeur structurée (``value_json``) : LISTE ORDONNÉE des
        # postes de frais de démarrage (composition MDF prêteur B). Chaque
        # entrée ``{key, label_fr, visible}`` : l'ordre de la liste = ordre
        # d'affichage, ``visible:false`` = poste masqué (le moteur le force
        # à 0 $ via ``FinanceInputs.frais_masques``). Le registre est une
        # couche de CONFIG (ordre / label / visibilité) PAR-DESSUS le
        # moteur — il NE CHANGE AUCUNE formule ni montant.
        #
        # Seedé avec les 16 postes FIXES DANS L'ORDRE INTERNE du moteur
        # (``FraisDemarrage``), tous ``visible:true``, labels FR = ceux du
        # PDF (``poste_defs``) / frontend (``buildFraisLabels``). Les postes
        # PERSONNALISÉS ne sont PAS seedés ici : ils sont APPENDUS
        # dynamiquement au registre par les endpoints CRUD ``frais-custom``.
        #
        # UPSERT idempotent : INSERT avec la liste par défaut si la clé est
        # absente ; sur conflit on ne met à jour QUE les métadonnées
        # (label/desc/group) — JAMAIS ``value_json`` (préserve l'ordre, la
        # visibilité et les perso configurés par l'admin via les endpoints).
        mdf_frais_registry_json = json.dumps([
            {"key": "courtier_hypothecaire_1",
             "label_fr": "Courtier hypothécaire 1", "visible": True},
            {"key": "courtier_hypothecaire_2",
             "label_fr": "Courtier hypothécaire 2", "visible": True},
            {"key": "taxes_bienvenue",
             "label_fr": "Taxes de bienvenue (calculées)", "visible": True},
            {"key": "evaluateur",
             "label_fr": "Évaluateur 1", "visible": True},
            {"key": "evaluateur_2",
             "label_fr": "Évaluateur 2", "visible": True},
            {"key": "inspection",
             "label_fr": "Inspection", "visible": True},
            {"key": "avocat",
             "label_fr": "Avocat", "visible": True},
            {"key": "notaire",
             "label_fr": "Notaire 1", "visible": True},
            {"key": "notaire_2",
             "label_fr": "Notaire 2", "visible": True},
            {"key": "rapport_efficacite",
             "label_fr": "Rapport efficacité énergétique", "visible": True},
            {"key": "frais_developpement",
             "label_fr": "Frais de développement", "visible": True},
            {"key": "frais_negociations",
             "label_fr": "Frais de négociations", "visible": True},
            {"key": "frais_travaux",
             "label_fr": "Frais de travaux", "visible": True},
            {"key": "frais_dossier_preteur",
             "label_fr": "Frais de dossier du prêteur", "visible": True},
            {"key": "interets",
             "label_fr": "Intérêts pendant projet (portage)",
             "visible": True},
            {"key": "revenus_nets_pendant_projet",
             "label_fr": "Revenus nets pendant projet", "visible": True},
            {"key": "interets_balance_vente",
             "label_fr": "Intérêts balance de vente", "visible": True},
            {"key": "detention",
             "label_fr": "Détention (institution traditionnelle)",
             "visible": True},
        ])
        try:
            await conn.execute(
                text(
                    """
                    INSERT INTO prospection_analysis_defaults
                      (key, value_json, label_fr, description_fr,
                       step, group_name, updated_at)
                    VALUES (:key, CAST(:value_json AS JSONB), :label_fr,
                            :description_fr, :step, :group, NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        label_fr       = EXCLUDED.label_fr,
                        description_fr = EXCLUDED.description_fr,
                        group_name     = EXCLUDED.group_name
                    """
                ),
                {
                    "key": "mdf_frais_registry",
                    "value_json": mdf_frais_registry_json,
                    "label_fr": (
                        "Registre des frais de démarrage (ordre/visibilité)"
                    ),
                    "description_fr": (
                        "Liste ordonnée des postes de frais de démarrage "
                        "(composition MDF prêteur B). Chaque entrée {key, "
                        "label_fr, visible} : l'ordre de la liste = ordre "
                        "d'affichage, visible:false = poste masqué. Couche "
                        "de config (ordre/label/visibilité) par-dessus le "
                        "moteur de calcul — ne change aucune formule."
                    ),
                    "step": 0.01,
                    "group": "mdf_frais",
                },
            )
        except Exception as exc:
            # Table absente au tout premier boot — retentera plus tard.
            log.warning("init_db: seed mdf_frais_registry échouée: %s", exc)

        # ── Backfill `financable_par_defaut` (mai 2026) ──────────────
        # On ne TOUCHE PAS aux items pour lesquels Phil a déjà
        # configuré explicitement la valeur (NULL → on backfill, NOT
        # NULL → on respecte le choix admin). Idempotent au boot.
        #
        # Choix par défaut (cf. PR « mdf-frais-dossier-preteur-financable-defaut ») :
        #   - frais_evaluateur / _2          : True  (intégré au prêt SCHL)
        #   - frais_inspection               : False (payé hors prêt en pratique)
        #   - frais_avocat                   : True
        #   - frais_notaire / _2             : True
        #   - frais_rapport_efficacite       : True
        #   - pct_courtier_hypothecaire_1/_2 : True
        #   - frais_dossier_preteur_pct      : False (Phil paie cash)
        financable_par_defaut_seed: tuple[tuple[str, bool], ...] = (
            ("frais_evaluateur", True),
            ("frais_evaluateur_2", True),
            ("frais_inspection", False),
            ("frais_avocat", True),
            ("frais_notaire", True),
            ("frais_notaire_2", True),
            ("frais_rapport_efficacite", True),
            ("pct_courtier_hypothecaire_1", True),
            ("pct_courtier_hypothecaire_2", True),
            ("frais_dossier_preteur_pct", False),
        )
        for default_key, default_val in financable_par_defaut_seed:
            try:
                await conn.execute(
                    text(
                        """
                        UPDATE prospection_analysis_defaults
                           SET financable_par_defaut = :val
                         WHERE key = :key
                           AND financable_par_defaut IS NULL
                        """
                    ),
                    {"key": default_key, "val": default_val},
                )
            except Exception as exc:
                # Table/colonne absente au premier boot — silencieux.
                log.warning("init_db: backfill financable_par_defaut (%s) échouée: %s", default_key, exc)

        # ── Seed des valeurs par défaut des soumissions devis_dev (Phase 6,
        # juin 2026) ─────────────────────────────────────────────────────
        # Table singleton `devlog_soumission_defaults` (id=1) créée par
        # `create_all`. On insère la ligne avec les valeurs historiques
        # (75/80/10/50/50, template vide) UNIQUEMENT si elle est absente —
        # ON CONFLICT DO NOTHING préserve les réglages déjà modifiés par
        # Phil depuis l'UI. Idempotent au boot. Plus aucun hard-code côté
        # application : la création d'une soumission lit cette ligne.
        try:
            await conn.execute(
                text(
                    """
                    INSERT INTO devlog_soumission_defaults
                      (id, taux_dev_horaire, taux_manager_horaire,
                       commission_closer_pct, marge_initiale_pct,
                       marge_recurrente_pct, base_modules_json, updated_at)
                    VALUES (1, 75, 80, 10, 50, 50, '[]'::jsonb, NOW())
                    ON CONFLICT (id) DO NOTHING
                    """
                )
            )
        except Exception as exc:
            # Table absente au tout premier boot (create_all n'a pas encore
            # tourné) — retentera au prochain démarrage.
            log.warning("init_db: seed devlog_soumission_defaults échouée: %s", exc)

        # ── Seed des valeurs par défaut coût/refac/marge des bons de travail
        # (Construction) ─────────────────────────────────────────────────
        # Table singleton `construction_bon_defaults` (id=1) créée par
        # `create_all`. On insère les valeurs historiques (coût 35, refac 55,
        # marge 10) UNIQUEMENT si la ligne est absente — ON CONFLICT DO NOTHING
        # préserve les réglages déjà modifiés par Phil depuis l'UI. Idempotent
        # au boot. Lue par la fiche bon, le formulaire de création et le moteur
        # de refacturation (filet).
        try:
            await conn.execute(
                text(
                    """
                    INSERT INTO construction_bon_defaults
                      (id, default_cost_rate, default_bill_rate,
                       default_marge_pct, updated_at)
                    VALUES (1, 35, 55, 10, NOW())
                    ON CONFLICT (id) DO NOTHING
                    """
                )
            )
        except Exception as exc:
            log.warning(
                "init_db: seed construction_bon_defaults échouée: %s", exc
            )


async def close_db() -> None:
    """
    Close database connections.

    Should be called on application shutdown.
    """
    await engine.dispose()



