"""Modèles du volet Gestion immobilière.

Inspiré des meilleurs PMS US (AppFolio, Yardi, Buildium) + Plexflow
+ ProprioExpert. Adapté au contexte québécois : matricule MAMH,
TPS/TVQ, baux résidentiels Régie du logement.

Conventions :
- Tous les modèles préfixés `imm_` côté SQL pour les distinguer des
  modèles construction/prospection.
- Ownership multi-entreprises via `imm_immeuble_ownership` (analogue
  à EntreprisePartner pour les entreprises).
- Lien optionnel à `mtl_property_units.matricule` pour le rôle
  d'évaluation déjà importé.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base, TimestampUpdateMixin


# ─── ENUMS ──────────────────────────────────────────────────────────────


class ImmeubleType(str, Enum):
    RESIDENTIEL = "residentiel"        # multi-logements résidentiel
    COMMERCIAL = "commercial"
    MIXTE = "mixte"
    UNIFAMILIAL = "unifamilial"
    AUTRE = "autre"


class LogementStatus(str, Enum):
    OCCUPE = "occupe"
    VACANT = "vacant"
    RESERVE = "reserve"  # bail signé pas encore commencé
    HORS_LOC = "hors_location"  # rénovation, propriétaire-occupé…


class BailStatus(str, Enum):
    ACTIF = "actif"
    TERMINE = "termine"
    RESILIE = "resilie"
    PROPOSE = "propose"  # bail signé pas encore commencé


class BailRenouvellementStatus(str, Enum):
    PROPOSE = "propose"     # avis envoyé au locataire
    ACCEPTE = "accepte"
    REFUSE = "refuse"
    EN_NEGOCIATION = "en_negociation"


class HypothequeStatus(str, Enum):
    ACTIVE = "active"
    REMBOURSEE = "remboursee"
    REFINANCEE = "refinancee"


class EvaluationKind(str, Enum):
    MUNICIPALE = "municipale"     # rôle d'évaluation
    MARCHANDE = "marchande"       # comparables
    APPRAISAL = "appraisal"       # évaluation pro
    AUTO = "auto"                 # estimation interne


class MaintenanceStatus(str, Enum):
    OUVERT = "ouvert"
    EN_COURS = "en_cours"
    EN_ATTENTE = "en_attente"     # pièce, fournisseur, locataire
    TERMINE = "termine"
    ANNULE = "annule"


class MaintenancePriorite(str, Enum):
    URGENCE = "urgence"           # ex. fuite eau, sans chauffage
    HAUTE = "haute"
    NORMALE = "normale"
    BASSE = "basse"


# ─── IMMEUBLE ───────────────────────────────────────────────────────────


class Immeuble(Base, TimestampUpdateMixin):
    """Immeuble locatif (multi-logements ou unifamilial)."""

    __tablename__ = "imm_immeubles"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    address: Mapped[str] = mapped_column(String(500), nullable=False)
    city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        default=ImmeubleType.RESIDENTIEL.value,
        server_default=ImmeubleType.RESIDENTIEL.value,
    )
    annee_construction: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nb_logements: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    superficie_terrain: Mapped[Optional[float]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    superficie_batiment: Mapped[Optional[float]] = mapped_column(
        Numeric(12, 2), nullable=True
    )

    # Lien rôle d'évaluation MAMH (matricule). Permet de récupérer
    # automatiquement valeur municipale + nb logements depuis la table
    # mtl_property_units déjà importée.
    matricule: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    # Acquisition
    purchase_price: Mapped[Optional[float]] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Photo de couverture : soit URL externe, soit blob uploadé directement.
    # Si `cover_photo_blob` est rempli, le frontend utilise l'endpoint de
    # stream pour récupérer l'image ; sinon il fallback sur `cover_photo_url`.
    cover_photo_url: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True
    )
    cover_photo_blob: Mapped[Optional[bytes]] = deferred(
        mapped_column(LargeBinary, nullable=True)
    )
    cover_photo_content_type: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Contact d'urgence de l'immeuble (concierge/gestionnaire). Quand Léa
    # détecte une urgence locataire (dégât d'eau, effraction, feu…), elle
    # transfère ICI d'abord, puis repli sur le numéro de garde global.
    # Colonne additive (cf. db/session.py).
    urgence_phone: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Gestion externe : l'immeuble est géré par une compagnie tierce →
    # exclu des flux opérationnels (paiements de loyers, renouvellements,
    # dépôts, relances). Les finances macro (P&L, prévisionnel) restent
    # incluses. Colonnes additives (cf. db/session.py).
    gestion_externe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    gestionnaire_externe_nom: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    gestionnaire_externe_contact: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    # Gestion externe MAIS maintenance faite par NOS hommes (retour Phil
    # 2026-07-22) : l'onglet Maintenance (bons de travail) reste actif
    # sur la fiche. Colonne additive → ensure_critical_columns.
    maintenance_interne: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Frais de gestion (page /immobilier/frais-gestion, 2026-07-22) :
    # contrat de gestion actif → on facture chaque mois X % des revenus
    # locatifs du mois précédent au propriétaire (client QuickBooks
    # associé). Colonnes additives → ensure_critical_columns.
    frais_gestion_actif: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    frais_gestion_pct: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    # 1er du mois à partir duquel on facture (les mois de revenus AVANT
    # cette date ne comptent pas dans le solde des mois manqués).
    frais_gestion_depuis: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True
    )
    qbo_customer_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    qbo_customer_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    # Frais de RELOCATION prévus au contrat de gestion (2026-07-23) :
    # montant chargé au proprio quand un dossier de relocation aboutit
    # (« reloué »). Deux tarifs : logement complet vs chambre
    # (Logement.location_en_chambres). NULL/0 = pas de frais.
    frais_relocation_logement: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    frais_relocation_chambre: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Scope du catalogue (immeubles créés depuis le picker de tâche).
    # Au plus l'un des deux est rempli. Tous deux NULL = immeuble
    # « global » (legacy ou créé via le CRUD complet du volet immo).
    owner_entreprise_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    owner_deal_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )


class ImmeubleOwnership(Base):
    """Quelle entreprise détient quel immeuble, à quel %.

    Plusieurs entreprises peuvent co-détenir un immeuble (partenariat
    Atelier Boréal 60% + Pivot Conseil 40% par exemple)."""

    __tablename__ = "imm_immeuble_ownerships"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    entreprise_id: Mapped[int] = mapped_column(
        ForeignKey("entreprises.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ownership_pct: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=100.0,
        server_default="100",
    )


# ─── LOGEMENT (unité dans un immeuble) ─────────────────────────────────


class Logement(Base, TimestampUpdateMixin):
    """Unité locative dans un immeuble."""

    __tablename__ = "imm_logements"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    numero: Mapped[str] = mapped_column(String(32), nullable=False)

    # Caractéristiques (nb pièces FR : 3½, 4½, 5½…)
    nb_pieces_decimal: Mapped[Optional[float]] = mapped_column(
        Numeric(3, 1), nullable=True
    )  # 3.5 = 3½
    nb_chambres: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nb_sdb: Mapped[Optional[float]] = mapped_column(
        Numeric(3, 1), nullable=True
    )  # 1, 1.5, 2…
    superficie_pi2: Mapped[Optional[float]] = mapped_column(
        Numeric(8, 1), nullable=True
    )
    # Logement loué EN CHAMBRES (colocation par chambre) — retour Steven
    # 2026-07-20. Colonne additive → ensure_critical_columns.
    location_en_chambres: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    etage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        default=ImmeubleType.RESIDENTIEL.value,
        server_default=ImmeubleType.RESIDENTIEL.value,
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=LogementStatus.VACANT.value,
        server_default=LogementStatus.VACANT.value,
        index=True,
    )

    # Loyer demandé (référence pour les annonces, ne reflète pas
    # forcément le bail courant)
    loyer_demande: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Gestion EXTERNE (retour Phil 2026-09-09) : nom du locataire,
    # FACULTATIF, sans fiche ni bail — pour cocher « payé » avec le
    # nom à côté quand le rapport mensuel du gestionnaire arrive.
    # Effacé quand l'unité redevient vacante. Colonne additive.
    locataire_externe_nom: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ─── LOCATAIRE ──────────────────────────────────────────────────────────


class Locataire(Base, TimestampUpdateMixin):
    """Locataire (personne physique ou morale).

    Pas de NAS stocké en clair — uniquement les 4 derniers chiffres
    pour identification rapide. Le NAS complet va dans un coffre-fort
    documents séparé (à venir).
    """

    __tablename__ = "imm_locataires"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True, index=True
    )
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    nas_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    # Adresse précédente (demandée à la conversion d'un candidat).
    ancienne_adresse: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )

    date_naissance: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    employeur: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    revenu_annuel: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Score interne 0-100 calculé à partir de l'historique paiements
    # (mis à jour automatiquement quand un paiement est enregistré).
    paiement_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Prélèvement préautorisé — colonnes INERTES depuis le
    # 2026-08-19. La documentation « maison » a été retirée : elle ne
    # collectait rien. La perception passera par Rotessa. Les colonnes
    # restent (aucune migration) et seront réutilisées ou remplacées à
    # ce moment-là.
    # Suivi de l'accord de prélèvement du loyer (perception Desjardins) :
    # 'aucun' | 'envoye' (documentation transmise) | 'actif' (accord
    # signé reçu) | 'refuse'. Colonnes additives → ensure_critical_columns.
    dpa_statut: Mapped[str] = mapped_column(
        String(16), nullable=False, default="aucun", server_default="aucun"
    )
    dpa_envoye_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    dpa_signe_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Preuve d'assurance locataire confirmée — à revalider chaque année
    # (retour Steven 2026-07-20). Date de la dernière confirmation ;
    # > 12 mois = à reconfirmer. Colonne additive → ensure_critical_columns.
    assurance_confirmee_le: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True
    )


class LocataireCommunication(Base, TimestampUpdateMixin):
    """Entrée MANUELLE d'historique de communication avec un locataire.

    Demande de Phil (2026-07-10) : pas de lien avec le système de
    téléphonie — l'employé consigne lui-même ses appels/courriels/notes
    depuis la fiche du locataire. Simple journal horodaté.
    """

    __tablename__ = "imm_locataire_communications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    locataire_id: Mapped[int] = mapped_column(
        ForeignKey("imm_locataires.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 'note' | 'appel' | 'courriel' | 'sms' | 'visite' | 'autre'
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="note", server_default="note"
    )
    contenu: Mapped[str] = mapped_column(Text, nullable=False)
    # Nom de l'employé qui consigne (snapshot, pas de FK — l'historique
    # survit à la suppression d'un compte).
    auteur: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class ImmCommunication(Base):
    """Courriel ENVOYÉ à un locataire depuis la page Communications.

    Une ligne PAR courriel (jamais d'envoi groupé avec plusieurs adresses
    visibles) — c'est l'audit demandé par Phil 2026-07-27 : qui a reçu
    quoi, quand, envoyé par qui et depuis quelle adresse. ``group_id``
    relie les courriels d'un même envoi de masse. Le contenu est un
    SNAPSHOT (le gabarit peut changer ensuite). Nouvelle table →
    ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_communications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    #: Regroupe les courriels d'un même clic « Envoyer ».
    group_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    #: 'rappel_paiement' | 'avis_acces' | 'demande_assurance' | 'libre'
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sujet: Mapped[str] = mapped_column(String(255), nullable=False)
    corps: Mapped[str] = mapped_column(Text, nullable=False)
    #: Document transmis par ce courriel, quand il y en a un. Sert au
    #: SUIVI : c'est par lui que l'historique sait si le locataire a
    #: ouvert le lien et s'il a signé (retour Phil 2026-08-19 : « il
    #: faut absolument que j'aie un suivi quelque part »).
    document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    locataire_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_locataires.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    bail_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    immeuble_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    #: Snapshots pour que l'audit survive aux suppressions.
    locataire_nom: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    immeuble_nom: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    destinataire_email: Mapped[str] = mapped_column(
        String(320), nullable=False
    )

    from_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )
    from_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    reply_to: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )

    #: 'envoye' | 'echec'
    statut: Mapped[str] = mapped_column(
        String(16), nullable=False, default="envoye", server_default="envoye"
    )
    erreur: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ─── RELOCATION (pipeline « Locations » / vacances) ─────────────────────


class LocationDossierStatut(str, Enum):
    """Pipeline « Locations » SIMPLIFIÉ (retour Phil 2026-09-09) : quatre
    étapes, plus d'annonces ni de candidats/visites/enquêtes dans Kratos
    — ça se passe ailleurs. Les anciennes valeurs (visites,
    candidat_retenu, bail_a_envoyer) sont traduites, cf.
    ``services.locatif_depart.LOCATION_STATUTS_LEGACY``."""

    AVIS_RECU = "avis_recu"              # « À louer » : départ confirmé / unité libre
    ANNONCE_PUBLIEE = "annonce_publiee"  # « Affiché » : l'annonce est en ligne
    BAIL_ENVOYE = "bail_envoye"          # « Bail en signature » : locataire lié
    RELOUE = "reloue"                    # nouveau bail signé au dossier
    ANNULE = "annule"                    # départ annulé / logement retiré


class LocationDossier(Base, TimestampUpdateMixin):
    """Dossier de RELOCATION d'un logement (un épisode de vacance).

    Créé quand un locataire confirme son départ (bouton sur le bail) ou
    à la main pour un logement déjà vacant. Suit les annonces, les
    visites et l'avancement jusqu'au nouveau bail. Aucun automatisme
    externe (Facebook, etc.) — l'employé consigne tout ici.
    """

    __tablename__ = "imm_location_dossiers"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    logement_id: Mapped[int] = mapped_column(
        ForeignKey("imm_logements.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Bail SORTANT à l'origine du dossier (nullable : logement déjà vide).
    bail_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="SET NULL"), nullable=True
    )

    statut: Mapped[str] = mapped_column(
        String(24), nullable=False,
        default=LocationDossierStatut.AVIS_RECU.value,
        server_default=LocationDossierStatut.AVIS_RECU.value,
        index=True,
    )
    # Date de départ prévue du locataire sortant (souvent = fin du bail).
    date_depart: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # Loyer affiché pour la relocation vs loyer du bail sortant (delta).
    loyer_demande: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    loyer_ancien: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    reloue_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Bail créé par la conversion « candidat retenu → locataire + bail »
    # (colonne ajoutée après création de la table → ensure_critical_columns).
    # La fiche locataire a été CRÉÉE par la conversion (le désistement
    # ne supprime jamais la fiche d'un client existant). Colonne
    # additive → cf. ensure_critical_columns (session.py).
    locataire_cree: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    nouveau_bail_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="SET NULL"), nullable=True
    )


class LocationAnnonce(Base, TimestampUpdateMixin):
    """Annonce publiée pour un dossier de relocation (suivi manuel).

    ⚠️ Plus exposée depuis la Location simplifiée (2026-09-09) : la
    table reste pour les données existantes, aucun endpoint ne l'écrit."""

    __tablename__ = "imm_location_annonces"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    dossier_id: Mapped[int] = mapped_column(
        ForeignKey("imm_location_dossiers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Kijiji, Marketplace, LesPAC, affiche, autre…
    plateforme: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    publiee_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class LocationVisite(Base, TimestampUpdateMixin):
    """Visite planifiée/faite avec un candidat pour un dossier.

    ⚠️ Plus exposée depuis la Location simplifiée (2026-09-09) : la
    table reste pour les données existantes, aucun endpoint ne l'écrit."""

    __tablename__ = "imm_location_visites"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    dossier_id: Mapped[int] = mapped_column(
        ForeignKey("imm_location_dossiers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    quand: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    candidat_nom: Mapped[str] = mapped_column(String(255), nullable=False)
    # Champ legacy (téléphone OU courriel mélangés) — remplacé par les
    # deux champs distincts ci-dessous, gardé pour les données existantes.
    candidat_contact: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    candidat_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )
    candidat_phone: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    # Candidat = LOCATAIRE EXISTANT (déjà client) — la conversion
    # réutilise sa fiche, aucun doublon. Colonne additive → cf.
    # ensure_critical_columns (session.py).
    locataire_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # planifiee | faite | absent | annulee
    statut: Mapped[str] = mapped_column(
        String(16), nullable=False, default="planifiee",
        server_default="planifiee",
    )
    # Le candidat est-il intéressé après la visite ? (null = pas encore su)
    interesse: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Prélocation (enquêtes) — null = pas faite, true = OK, false = KO.
    # Colonnes ajoutées après la création de la table → cf.
    # ensure_critical_columns (session.py).
    enquete_credit: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    enquete_references: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    enquete_emploi: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    enquete_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Candidat RETENU pour le logement (exclusif par dossier).
    retenu: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


# ─── BAIL ───────────────────────────────────────────────────────────────


class Bail(Base, TimestampUpdateMixin):
    """Contrat de bail entre un locataire et un logement.

    Format québécois : 1er juillet au 30 juin par défaut. Renouvellement
    automatique sauf avis 3-6 mois avant (selon durée), géré dans
    BailRenouvellement.
    """

    __tablename__ = "imm_baux"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    logement_id: Mapped[int] = mapped_column(
        ForeignKey("imm_logements.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    locataire_id: Mapped[int] = mapped_column(
        ForeignKey("imm_locataires.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    date_debut: Mapped[date] = mapped_column(Date, nullable=False)
    date_fin: Mapped[date] = mapped_column(Date, nullable=False)

    loyer_mensuel: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False
    )
    depot_garantie: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    #: Date à laquelle le dépôt a été REÇU du locataire. Distincte de
    #: la date de début du bail : un dépôt se verse souvent à la
    #: signature, des mois avant l'entrée.
    depot_recu_le: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True
    )
    #: Qui DÉTIENT l'argent (ex. « MGV Developpement »). Un dépôt est
    #: l'argent du locataire : savoir chez qui il dort n'est pas un
    #: détail au moment de le rendre.
    depot_detenteur: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    # Date de REMISE du dépôt au locataire (bail terminé → dépôt rendu).
    # NULL = toujours détenu (ou à rendre si le bail est terminé/résilié).
    depot_rendu_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    #: Bail AU MOIS (chambres, retour Phil 2026-07-28) : reconduction
    #: automatique au même prix — jamais dans le suivi des renouvellements
    #: (ni « échu »), loyers qui courent sans égard à date_fin, exclu du
    #: suivi d'assurance (réglable). Colonne additive → ensure_critical_columns.
    au_mois: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    #: Jour du mois où le loyer est payable (bail TAL : case « le 1er jour
    #: du mois » OU champ « Ou le ___ »). 1 = le 1er, cas de l'immense
    #: majorité des baux ; ex. le garage du 8900 St-Hubert est payable le
    #: 12. Sert de point de départ au calcul du retard (délai de grâce
    #: appliqué à partir de ce jour). Borné 1..28 (février).
    #: Colonne additive → ensure_critical_columns.
    jour_echeance: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    # Inclusions (chauffage, eau chaude, électricité, internet…)
    chauffage_inclus: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    eau_chaude_inclus: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    electricite_inclus: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    internet_inclus: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=BailStatus.ACTIF.value,
        server_default=BailStatus.ACTIF.value,
        index=True,
    )

    # PDF du bail signé (URL S3 ou vault). Legacy — remplacé par
    # ``document_id`` (le PDF vit dans imm_documents).
    document_url: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True
    )

    #: LE bail courant (imm_documents) — celui qui s'ouvre au clic depuis
    #: la page Baux. Remplacer le bail change ce pointeur ; l'ancien reste
    #: en base et apparaît dans les Documents du logement/locataire
    #: (retour Phil 2026-07-27). Nouvelle colonne → ensure_critical_columns.
    document_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )

    #: EXCEPTION déclarée : ce bail n'aura PAS de document au dossier,
    #: et c'est assumé (retour Phil 2026-08-19 : « il pourrait y avoir
    #: des situations exceptionnelles où il y en a pas, faudrait pas que
    #: ce soit un frein à la création »). Le blocage reste la réponse
    #: par défaut — dans la quasi-totalité des cas c'est un oubli — mais
    #: il se franchit en déclarant un motif, qui reste signé et daté.
    sans_document_motif: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    sans_document_par: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    sans_document_le: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Dossier ouvert au TAL pour ce bail (non-paiement) — coché depuis
    #: la page Paiements pour que toute l'équipe VOIE que le recours est
    #: lancé (retour Phil 2026-08-31 : « pour pas que je relance
    #: toujours notre gars qui s'occupe des paiements »). NULL = aucun
    #: dossier. Colonne additive → ensure_critical_columns.
    tal_dossier_ouvert_le: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True
    )

    # Signature électronique du bail (lien tokenisé envoyé au locataire).
    # Colonnes ajoutées de façon additive via init_db (cf. db/session.py).
    signature_token: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    sent_to_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Première ouverture de la page publique /sign-bail (suivi « ouvert »).
    signature_opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signed_by_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    signature_ip: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    signature_image: Mapped[Optional[bytes]] = deferred(
        mapped_column(LargeBinary, nullable=True)
    )
    signature_image_content_type: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )


class BailRenouvellement(Base, TimestampUpdateMixin):
    """Cycle de renouvellement annuel d'un bail.

    Au Québec : avis au moins 3 mois avant la fin (loyer fixe) ou 6
    mois (HLM/longue durée). On crée une ligne dès l'envoi d'avis,
    statut = propose. Mise à jour quand le locataire répond.
    """

    __tablename__ = "imm_bail_renouvellements"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bail_id: Mapped[int] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    avis_envoye_le: Mapped[date] = mapped_column(Date, nullable=False)
    nouveau_loyer: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    nouvelle_date_debut: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True
    )
    nouvelle_date_fin: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True
    )
    #: v3 (2026-07-30) — réponse du locataire (date + motif de refus)
    #: et date où le nouveau loyer/la période ont été REPORTÉS sur le
    #: bail (application à la date effective). Colonnes additives.
    reponse_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    refus_motif: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    applique_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False,
        default=BailRenouvellementStatus.PROPOSE.value,
        server_default=BailRenouvellementStatus.PROPOSE.value,
    )
    locataire_repondu_le: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True
    )

    #: L'AVIS courant (imm_documents) — celui qui s'ouvre au clic depuis
    #: Suivis annuels. En importer un nouveau change ce pointeur ;
    #: l'ancien reste dans les Documents (retour Phil 2026-07-27).
    #: Nouvelle colonne → ensure_critical_columns.
    document_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ─── PAIEMENT DE LOYER ──────────────────────────────────────────────────


class PaiementLoyer(Base):
    """Paiement de loyer enregistré.

    Permet le rent-roll et le calcul du score de paiement du locataire.
    Distinct des Payment du module facturation construction.
    """

    __tablename__ = "imm_paiements_loyer"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bail_id: Mapped[int] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    mois_couvert: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # = 1er du mois facturé. Format YYYY-MM-01.

    montant: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    paye_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    methode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Marqueur si paiement en retard (>5 jours après le 1er)
    en_retard: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FraisLocatif(Base):
    """Frais ponctuel facturé au locataire d'un bail (retour Steven
    2026-07-20) : ex. 20 $ de frais de gestion si le loyer est payé
    après le 15. S'AJOUTE au solde dû du bail (vue Baux & paiements) ;
    réglé implicitement quand les paiements couvrent loyers + frais.
    Nouvelle table → ensure_immobilier_aux_tables."""

    __tablename__ = "imm_frais_locatifs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bail_id: Mapped[int] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 1er du mois auquel le frais se rattache (affichage dans la vue).
    mois_couvert: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    montant: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    libelle: Mapped[str] = mapped_column(
        String(128), nullable=False, default="Frais de retard",
        server_default="Frais de retard",
    )
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ─── HYPOTHÈQUE ─────────────────────────────────────────────────────────


class Hypotheque(Base, TimestampUpdateMixin):
    """Prêt hypothécaire sur un immeuble.

    Plusieurs hypothèques possibles par immeuble (1ère, 2e, marge
    hypothécaire). Une alerte cron se déclenche 6 mois avant l'échéance
    pour préparer le refinancement.
    """

    __tablename__ = "imm_hypotheques"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    rang: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )  # 1 = première, 2 = deuxième…
    preteur: Mapped[str] = mapped_column(String(255), nullable=False)
    montant_initial: Mapped[float] = mapped_column(
        Numeric(14, 2), nullable=False
    )
    balance_actuelle: Mapped[Optional[float]] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    taux_pct: Mapped[Optional[float]] = mapped_column(
        Numeric(6, 4), nullable=True
    )  # ex. 5.4500
    type_taux: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # 'fixe' | 'variable'
    amortissement_mois: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )  # ex. 300 = 25 ans

    paiement_mensuel: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Composition des intérêts choisie dans le calculateur de paiement :
    # 'semi' (semi-annuelle, standard hypothèques résidentielles CA) ou
    # 'mensuelle' (prêts commerciaux / variables). Persistée pour que la
    # préférence ne revienne pas au défaut après sauvegarde.
    # Colonne additive (cf. db/session.py).
    composition_interets: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )

    date_debut: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date_fin_terme: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, index=True
    )  # date de renouvellement du terme

    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=HypothequeStatus.ACTIVE.value,
        server_default=HypothequeStatus.ACTIVE.value,
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ─── ÉVALUATION ─────────────────────────────────────────────────────────


class Evaluation(Base):
    """Snapshot de valeur d'un immeuble à une date donnée.

    Plusieurs lignes possibles (municipale annuelle, marchande estimée,
    appraisal pro). Garde l'historique pour les graphiques de
    valorisation et le calcul d'appréciation.
    """

    __tablename__ = "imm_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    kind: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=EvaluationKind.MARCHANDE.value,
    )
    valeur: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    date_evaluation: Mapped[date] = mapped_column(
        Date, nullable=False, index=True
    )

    source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Évaluation de référence pour le calcul d'équité (valeur actuelle).
    # Une seule par immeuble : passer à True remet les autres à False
    # (cf. endpoint PATCH /evaluations/{id}).
    # Colonne additive (cf. db/session.py).
    is_reference: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ─── MAINTENANCE ────────────────────────────────────────────────────────


class MaintenanceOrdre(Base, TimestampUpdateMixin):
    """Ordre de travail d'entretien sur un immeuble ou logement.

    Lié soit à l'immeuble (entretien commun, ex. déneigement) soit à un
    logement spécifique (ex. réparation lavabo).
    """

    __tablename__ = "imm_maintenance_ordres"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    logement_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_logements.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    titre: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    priorite: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=MaintenancePriorite.NORMALE.value,
        server_default=MaintenancePriorite.NORMALE.value,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=MaintenanceStatus.OUVERT.value,
        server_default=MaintenanceStatus.OUVERT.value,
        index=True,
    )

    fournisseur: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cout_estime: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    cout_reel: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    plannifie_pour: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    complete_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class DepenseImmeuble(Base, TimestampUpdateMixin):
    """Dépense d'exploitation d'un immeuble (taxes, assurances,
    entretien, déneigement, énergie…).

    ``frequence`` pilote l'annualisation dans le P&L :
      - "ponctuel" : compté dans l'année de ``date_depense`` ;
      - "mensuel"  : montant × 12 (coût courant) ;
      - "annuel"   : montant × 1 (coût courant).
    """

    __tablename__ = "immeuble_depenses"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        # La table des immeubles s'appelle `imm_immeubles` (cf. Immeuble
        # plus haut). Un `immeubles.id` erroné faisait échouer TOUT
        # `Base.metadata.create_all()` (NoReferencedTableError) → init_db
        # plantait en silence à chaque boot. Voir docs/PROPOSITIONS.md P-02.
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # taxes_municipales / taxes_scolaires / assurances / energie /
    # entretien / deneigement / conciergerie / gestion / autre
    categorie: Mapped[str] = mapped_column(
        String(48), nullable=False, default="autre"
    )
    libelle: Mapped[str] = mapped_column(String(255), nullable=False)
    montant: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    frequence: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ponctuel",
        server_default="ponctuel",
    )
    date_depense: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )

    # Le montant est un % des loyers mensuels plutôt qu'un montant fixe
    # (ex. frais de gestion à 5 % des loyers). Colonne additive
    # (cf. db/session.py).
    is_pourcentage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Dépense taxable : appliquer TPS+TVQ Québec (×1.14975) dans les
    # calculs (cashflow, NOI). Colonne additive (cf. db/session.py).
    taxable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class RelanceLoyer(Base, TimestampUpdateMixin):
    """Relance d'un loyer en retard envoyée à un locataire (courriel).

    ``niveau`` = ordre de la relance pour ce bail + ce mois (1, 2, …).
    Sert de journal/preuve avant un recours (mise en demeure TAL).
    """

    __tablename__ = "imm_relances_loyer"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bail_id: Mapped[int] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mois_couvert: Mapped[date] = mapped_column(
        Date, nullable=False, index=True
    )
    niveau: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    canal: Mapped[str] = mapped_column(
        String(16), nullable=False, default="courriel",
        server_default="courriel",
    )
    destinataire: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sent_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )


class ImmDocument(Base, TimestampUpdateMixin):
    """Document locatif GÉNÉRÉ et CONSERVÉ (avis TAL, trousse bail, DPA…).

    Chaque génération depuis « Générer ▾ » enregistre le
    PDF + ses paramètres : l'utilisateur peut le revoir, le modifier
    (régénération = nouvelle ligne) et l'envoyer pour SIGNATURE via un
    lien public tokenisé — retour Phil 2026-07-17 (« ces documents-là,
    ils sont où ? »). Nouvelle table → ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_documents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bail_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    locataire_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_locataires.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    immeuble_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    #: Rattachement direct à un logement (document importé sur la fiche
    #: du logement, sans passer par un bail). Nouvelle colonne →
    #: ensure_critical_columns.
    logement_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )

    type: Mapped[str] = mapped_column(String(48), nullable=False)
    titre: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 'genere' (produit par Kratos) | 'importe' (téléversé à la main).
    #: Sépare le DOSSIER (signé/importé) des simples communications
    #: (retour Phil 2026-07-27). Nouvelle colonne.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="genere", server_default="genere"
    )
    #: Nom du fichier d'origine (import) — affiché tel quel dans la liste.
    filename: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    #: Document que celui-ci REMPLACE (bail/avis/relevé changé) : garde la
    #: chaîne des versions pour l'historique.
    remplace_document_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    # Paramètres de génération (JSON) — permet « Modifier » = rouvrir le
    # formulaire prérempli puis régénérer une nouvelle version.
    params_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # PDF généré. deferred : jamais chargé dans les listes.
    pdf_blob = deferred(mapped_column(LargeBinary, nullable=True))
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )

    # Signature en ligne (lien public /document/{token})
    signature_token: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    envoye_le: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    envoye_a: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )
    ouvert_le: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signed_by_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    signature_ip: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    signature_image = deferred(
        mapped_column(LargeBinary, nullable=True)
    )
    #: REFUS du locataire. Jusqu'au 2026-08-19, seul un avis de
    #: modification pouvait être refusé (via son cycle de
    #: renouvellement) : un locataire à qui on demandait son
    #: consentement aux communications électroniques n'avait aucun
    #: moyen de dire non — alors que c'est précisément un consentement,
    #: donc refusable par nature. Colonnes additives.
    refuse_le: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refuse_par: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    signature_image_content_type: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    #: Copie Drive du document (dépôt auto à la signature) — supprimée
    #: en corbeille avec le document. Nouvelle colonne →
    #: ensure_critical_columns.
    drive_file_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    #: Pièce rattachée à un dossier TAL (mise en demeure, avis
    #: d'audience, décision…) — PAS de second stockage : le document
    #: vit ici, le dossier ne fait que pointer dessus (retour Phil
    #: 2026-09-09, point 5). Colonne additive nullable → ajoutée au
    #: démarrage par schema_check.
    tal_dossier_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_tal_dossiers.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )


class Releve31(Base, TimestampUpdateMixin):
    """Suivi des RELEVÉS 31 (Revenu Québec) par logement et par année.

    Obligation annuelle : produire un RL-31 pour chaque logement OCCUPÉ
    au 31 décembre et en remettre copie au(x) locataire(s) avant le
    dernier jour de février. Kratos ne produit PAS le relevé officiel
    (service en ligne Revenu Québec) — il prépare les données, suit le
    statut, conserve la copie PDF téléversée (imm_documents) et l'envoie
    au locataire. Nouvelle table → ensure_immobilier_aux_tables.

    Une ligne = UN relevé = UN locataire (``bail_id``) : deux locataires
    successifs dans la même année sur le même logement = deux lignes.
    """

    __tablename__ = "imm_releves31"
    # Unicité = (année, logement, BAIL) et non (année, logement) : au
    # 44 Kennedy, les logements 101/105/107 ont eu deux locataires
    # successifs dans la même année et chacun a légalement droit à SON
    # relevé (Revenu Québec). ``bail_id`` étant nullable — et Postgres ne
    # considérant jamais deux NULL comme égaux — la contrainte vit en
    # INDEX UNIQUE sur ``(annee, logement_id, COALESCE(bail_id, 0))``,
    # créé dans ``ensure_immobilier_aux_tables`` (session.py), qui
    # supprime au passage l'ancienne ``uq_releve31_annee_logement``.

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    annee: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    logement_id: Mapped[int] = mapped_column(
        ForeignKey("imm_logements.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    immeuble_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    bail_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="SET NULL"), nullable=True
    )
    locataire_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_locataires.id", ondelete="SET NULL"), nullable=True
    )
    # 'a_produire' | 'produit' | 'remis'
    statut: Mapped[str] = mapped_column(
        String(16), nullable=False, default="a_produire",
        server_default="a_produire",
    )
    # Numéro du relevé émis par Revenu Québec (collé par l'utilisateur).
    numero_releve: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Copie PDF du relevé (téléversée) — vit dans imm_documents.
    document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_documents.id", ondelete="SET NULL"), nullable=True
    )


class PaiementExterne(Base):
    """Suivi des loyers d'un immeuble en GESTION EXTERNE (retour Phil
    2026-07-22, pt 10) : la compagnie de gestion perçoit les loyers et
    envoie son rapport — on coche ici, PAR LOGEMENT (pas de locataire
    connu), payé ou non pour le mois. 1 ligne = payé ; absence = impayé.
    Nouvelle table → ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_paiements_externes"
    __table_args__ = (
        UniqueConstraint(
            "logement_id", "mois_couvert", name="uq_paiement_externe_mois"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    logement_id: Mapped[int] = mapped_column(
        ForeignKey("imm_logements.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    mois_couvert: Mapped[date] = mapped_column(Date, nullable=False)
    montant: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    #: Loyer attendu FIGÉ au moment où le mois est marqué payé (retour
    #: client 2026-08-14) : un changement de loyer sur le logement ne
    #: réécrit plus l'historique des mois déjà réglés. NULL sur les
    #: lignes d'avant → fallback lecture sur le loyer courant.
    #: Colonne additive → ensure_critical_columns.
    loyer_attendu: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    paye_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FactureGestion(Base):
    """Facture mensuelle de FRAIS DE GESTION d'un immeuble sous contrat
    (page /immobilier/frais-gestion, 2026-07-22) : X % des revenus
    locatifs du mois couvert, créée dans QuickBooks. Un mois est
    « facturé » quand la somme des ``revenus`` de ses lignes couvre les
    revenus enregistrés ; un loyer payé en retard après la facture crée
    une ligne COMPLÉMENT (``est_complement``) sur le même mois — d'où
    plusieurs lignes possibles par (immeuble, mois), l'ancienne
    contrainte unique est supprimée dans ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_factures_gestion"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    #: 1er du mois DONT les revenus sont facturés (ex. 2026-08-01).
    mois_couvert: Mapped[date] = mapped_column(Date, nullable=False)
    revenus: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    montant: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    #: True = facture des loyers arrivés APRÈS la facturation du mois
    #: (``revenus`` = seulement le delta). Colonne → ensure_critical_columns.
    est_complement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: 'gestion' (X % des revenus du mois), 'relocation' (frais fixe au
    #: contrat quand un dossier de relocation aboutit) ou 'manuel'
    #: (frais libre ajouté au panier). Colonnes → ensure_critical_columns.
    type_ligne: Mapped[str] = mapped_column(
        String(16), nullable=False, default="gestion",
        server_default="gestion",
    )
    #: Dossier de relocation facturé (type 'relocation') — sert à marquer
    #: le dossier comme facturé.
    relocation_dossier_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    #: Description de la ligne (relocation : unité reloué ; manuel :
    #: texte saisi par le gestionnaire).
    libelle: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    qbo_invoice_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    qbo_doc_number: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FraisManuelGestion(Base):
    """Frais MANUEL de gestion EN ATTENTE de facturation (page
    /immobilier/frais-gestion, retour Phil 2026-08-10) : saisi pendant
    le mois, il persiste jusqu'à son ajout à la facture QuickBooks —
    il devient alors une ligne ``FactureGestion`` (type 'manuel') et
    la ligne d'attente est supprimée. Nouvelle table →
    ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_frais_manuels_gestion"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    libelle: Mapped[str] = mapped_column(String(255), nullable=False)
    montant: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FactureExterne(Base, TimestampUpdateMixin):
    """FACTURE PONCTUELLE d'un immeuble en gestion externe (retour Phil
    2026-07-22, pt 11) : ex. la compagnie de gestion refacture 350 $ de
    plomberie pour l'app. 3. PAS un bon de travail, PAS une dépense
    récurrente (déneigement…) — un coût unique rattaché (optionnellement)
    à un logement, pour suivre combien chaque appartement coûte à
    l'année. Nouvelle table → ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_factures_externes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    immeuble_id: Mapped[int] = mapped_column(
        ForeignKey("imm_immeubles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    logement_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("imm_logements.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    date_facture: Mapped[date] = mapped_column(Date, nullable=False)
    montant: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    fournisseur: Mapped[Optional[str]] = mapped_column(
        String(160), nullable=True
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )


class ImmDocPersoModele(Base, TimestampUpdateMixin):
    """MODÈLE de document personnalisé (retour Steven 2026-07-20, pt 5).

    Règlement d'immeuble, contrat de chambreur… créés depuis Paramètres →
    Modèles de documents. Deux formes :
    - texte (``corps`` = paragraphes séparés par une ligne vide, avec
      {variables} remplies depuis le bail, **gras** supporté) ;
    - ou PDF téléversé (``pdf_blob``), utilisé tel quel.
    Généré depuis un bail → ImmDocument type ``personnalise`` (signature
    en ligne) ou ``personnalise_info`` (courriel simple + suivi
    d'ouverture). Nouvelle table → ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_doc_perso_modeles"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    nom: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True, index=True
    )
    titre: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    corps: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signature_requise: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    pdf_filename: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    pdf_blob = deferred(mapped_column(LargeBinary, nullable=True))
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )


class ImmDocTemplate(Base, TimestampUpdateMixin):
    """PDF MODÈLE remplaçant un formulaire officiel embarqué.

    Les formulaires TAL livrés avec l'app (assets/tal/*.pdf) peuvent être
    remplacés depuis Paramètres → Modèles de documents quand le TAL publie
    une nouvelle version (retour Phil 2026-07-17 : « il va falloir pouvoir
    modifier ces documents au besoin »). Une ligne par type ; absence de
    ligne = PDF d'origine. Nouvelle table → ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_doc_templates"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    type: Mapped[str] = mapped_column(
        String(48), nullable=False, unique=True, index=True
    )
    filename: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    pdf_blob = deferred(mapped_column(LargeBinary, nullable=False))
    uploaded_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )


# ─── Dossier TAL (point 5, retour Phil 2026-09-09) ──────────────────────

#: Motifs de recours au Tribunal administratif du logement — volontairement
#: courts, pas de champs juridiques (Phil : « rien de compliqué »).
TAL_MOTIFS: tuple[str, ...] = (
    "non_paiement",
    "retards",
    "reprise",
    "travaux",
    "non_reconduction",
    "troubles",
    "autre",
)

#: Statuts d'un dossier. Tout ce qui n'est pas « ferme » compte comme un
#: dossier EN COURS (pastilles, miroir ``Bail.tal_dossier_ouvert_le``).
TAL_STATUTS: tuple[str, ...] = (
    "a_ouvrir",
    "ouvert",
    "audience",
    "decision",
    "ferme",
)
TAL_STATUT_FERME = "ferme"


class ImmTalDossier(Base, TimestampUpdateMixin):
    """Dossier ouvert au TAL pour un bail — le SUIVI simple de l'équipe.

    Remplace la seule date ``Bail.tal_dossier_ouvert_le`` (qui reste en
    MIROIR : posée/effacée par les endpoints de ce dossier, et
    inversement le PATCH bail qui pose la date crée un dossier). Les
    pièces (mise en demeure, décision…) sont des ``imm_documents``
    rattachés par ``tal_dossier_id``. Nouvelle table →
    ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_tal_dossiers"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bail_id: Mapped[int] = mapped_column(
        ForeignKey("imm_baux.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Dénormalisés depuis le bail pour les filtres (fiche locataire,
    # pastilles logement/immeuble) sans jointure.
    locataire_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    logement_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    immeuble_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )

    motif: Mapped[str] = mapped_column(
        String(32), nullable=False,
        default="non_paiement", server_default="non_paiement",
    )
    statut: Mapped[str] = mapped_column(
        String(24), nullable=False,
        default="ouvert", server_default="ouvert", index=True,
    )
    numero_dossier: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    ouvert_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    audience_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    decision_le: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )


# ─── Garants & contacts d'un locataire (point 8, 2026-09-09) ────────────

#: Rôles d'un contact : garant, colocataire, occupant, contact d'urgence.
CONTACT_ROLES: tuple[str, ...] = ("garant", "colocataire", "occupant", "urgence")


class ImmLocataireContact(Base, TimestampUpdateMixin):
    """Personne liée à un locataire SANS fiche complète (garant,
    colocataire, occupant, contact d'urgence).

    Retour Phil 2026-09-09 : « un virement de Jacques alors que le
    locataire est Sébastien : quand je cherche Jacques, je vois
    Sébastien ». D'où ``paie_le_loyer`` (affiché sur la ligne Paiements)
    et l'indexation du nom dans les recherches. Nouvelle table →
    ensure_immobilier_aux_tables.
    """

    __tablename__ = "imm_locataire_contacts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    locataire_id: Mapped[int] = mapped_column(
        ForeignKey("imm_locataires.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(
        String(24), nullable=False, default="garant", server_default="garant"
    )
    full_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    #: Lien avec le locataire (« père », « conjointe », « colocataire »…).
    relation: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    #: C'est LUI qui paie le loyer (virements à son nom).
    paie_le_loyer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actif: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by_email: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
