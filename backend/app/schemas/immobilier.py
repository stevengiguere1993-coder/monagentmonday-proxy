"""Pydantic schemas pour le volet Gestion immobilière."""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.immobilier import CONTACT_ROLES, TAL_MOTIFS, TAL_STATUTS


# ─── Immeuble ──────────────────────────────────────────────────────────


class ImmeubleBase(BaseModel):
    # Nom optionnel : fallback automatique sur l'adresse si non fourni.
    name: Optional[str] = Field(default=None, max_length=255)
    address: str = Field(..., min_length=1, max_length=500)
    city: Optional[str] = Field(default=None, max_length=128)
    postal_code: Optional[str] = Field(default=None, max_length=16)
    type: str = Field(default="residentiel", max_length=32)
    annee_construction: Optional[int] = Field(default=None, ge=1700, le=2100)
    nb_logements: Optional[int] = Field(default=None, ge=0)
    superficie_terrain: Optional[float] = Field(default=None, ge=0)
    superficie_batiment: Optional[float] = Field(default=None, ge=0)
    matricule: Optional[str] = Field(default=None, max_length=64)
    purchase_price: Optional[float] = Field(default=None, ge=0)
    purchase_date: Optional[date] = None
    cover_photo_url: Optional[str] = Field(default=None, max_length=1000)
    description: Optional[str] = None
    # Contact d'urgence (concierge/gestionnaire) appelé en priorité par Léa
    # lors d'une urgence locataire, avant le repli sur le numéro de garde.
    urgence_phone: Optional[str] = Field(default=None, max_length=32)
    is_active: bool = True
    # Gestion externe : immeuble géré par une compagnie tierce → exclu
    # des flux opérationnels (loyers, renouvellements, dépôts, relances).
    gestion_externe: bool = False
    gestionnaire_externe_nom: Optional[str] = Field(
        default=None, max_length=255
    )
    gestionnaire_externe_contact: Optional[str] = Field(
        default=None, max_length=255
    )
    # Gestion externe mais maintenance faite par nos hommes → l'onglet
    # Maintenance (bons de travail) reste actif sur la fiche.
    maintenance_interne: bool = False


class ImmeubleCreate(ImmeubleBase):
    # Si fourni, crée automatiquement un ImmeubleOwnership pour cette
    # entreprise à 100 % au moment de la création.
    entreprise_id: Optional[int] = None


class ImmeubleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    address: Optional[str] = Field(default=None, min_length=1, max_length=500)
    city: Optional[str] = Field(default=None, max_length=128)
    postal_code: Optional[str] = Field(default=None, max_length=16)
    type: Optional[str] = Field(default=None, max_length=32)
    annee_construction: Optional[int] = Field(default=None, ge=1700, le=2100)
    nb_logements: Optional[int] = Field(default=None, ge=0)
    superficie_terrain: Optional[float] = Field(default=None, ge=0)
    superficie_batiment: Optional[float] = Field(default=None, ge=0)
    matricule: Optional[str] = Field(default=None, max_length=64)
    purchase_price: Optional[float] = Field(default=None, ge=0)
    purchase_date: Optional[date] = None
    cover_photo_url: Optional[str] = Field(default=None, max_length=1000)
    description: Optional[str] = None
    urgence_phone: Optional[str] = Field(default=None, max_length=32)
    is_active: Optional[bool] = None
    gestion_externe: Optional[bool] = None
    gestionnaire_externe_nom: Optional[str] = Field(
        default=None, max_length=255
    )
    gestionnaire_externe_contact: Optional[str] = Field(
        default=None, max_length=255
    )
    maintenance_interne: Optional[bool] = None


class ImmeubleRead(ImmeubleBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    has_cover_photo: bool = False
    created_at: datetime
    updated_at: datetime


# Liste : version allégée + KPIs calculés
class ImmeubleListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    address: str
    city: Optional[str] = None
    type: str
    nb_logements: Optional[int] = None
    cover_photo_url: Optional[str] = None
    has_cover_photo: bool = False
    is_active: bool
    # KPIs agrégés
    nb_logements_actifs: int = 0
    nb_logements_occupes: int = 0
    revenu_mensuel: float = 0.0
    taux_occupation: float = 0.0  # 0..1


# ─── Ownership ──────────────────────────────────────────────────────────


class ImmeubleOwnershipBase(BaseModel):
    entreprise_id: int
    ownership_pct: float = Field(default=100.0, ge=0, le=100)


class ImmeubleOwnershipCreate(ImmeubleOwnershipBase):
    pass


class ImmeubleOwnershipRead(ImmeubleOwnershipBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    immeuble_id: int


# ─── Logement ───────────────────────────────────────────────────────────


class LogementBase(BaseModel):
    immeuble_id: int
    numero: str = Field(..., min_length=1, max_length=32)
    nb_pieces_decimal: Optional[float] = Field(default=None, ge=0)
    nb_chambres: Optional[int] = Field(default=None, ge=0)
    nb_sdb: Optional[float] = Field(default=None, ge=0)
    superficie_pi2: Optional[float] = Field(default=None, ge=0)
    location_en_chambres: bool = False
    etage: Optional[int] = None
    type: str = Field(default="residentiel", max_length=32)
    status: str = Field(default="vacant", max_length=16)
    loyer_demande: Optional[float] = Field(default=None, ge=0)
    #: Gestion externe : nom du locataire (facultatif, sans fiche).
    locataire_externe_nom: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = None


class LogementCreate(LogementBase):
    pass


class LogementUpdate(BaseModel):
    numero: Optional[str] = Field(default=None, min_length=1, max_length=32)
    nb_pieces_decimal: Optional[float] = Field(default=None, ge=0)
    nb_chambres: Optional[int] = Field(default=None, ge=0)
    nb_sdb: Optional[float] = Field(default=None, ge=0)
    superficie_pi2: Optional[float] = Field(default=None, ge=0)
    location_en_chambres: Optional[bool] = None
    etage: Optional[int] = None
    type: Optional[str] = Field(default=None, max_length=32)
    status: Optional[str] = Field(default=None, max_length=16)
    loyer_demande: Optional[float] = Field(default=None, ge=0)
    locataire_externe_nom: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = None


class LogementRead(LogementBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime
    #: Loyer RÉEL du bail actif (lecture seule, rempli par la liste des
    #: logements) — un logement OCCUPÉ affiche ce loyer, pas le
    #: « loyer demandé » qui ne vaut que pour la relocation (vacant).
    loyer_actuel: Optional[float] = None
    #: Date à laquelle le logement se libère, quand un départ est ACTÉ
    #: (dossier de relocation ouvert). « Occupé » et « occupé mais libre
    #: le 31 août » ne sont pas le même état — retour Phil 2026-08-19.
    #: ⚠️ Une fin de bail seule ne remplit PAS ce champ : au Québec un
    #: bail se reconduit tacitement, échéance ≠ départ.
    libre_le: Optional[date] = None


# ─── Locataire ──────────────────────────────────────────────────────────


class LocataireBase(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=255)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=50)
    nas_last4: Optional[str] = Field(default=None, max_length=4, min_length=4)
    ancienne_adresse: Optional[str] = Field(default=None, max_length=500)
    date_naissance: Optional[date] = None
    employeur: Optional[str] = Field(default=None, max_length=255)
    revenu_annuel: Optional[float] = Field(default=None, ge=0)
    # Dernière confirmation de la preuve d'assurance (à refaire chaque année).
    assurance_confirmee_le: Optional[date] = None
    notes: Optional[str] = None


class LocataireCreate(LocataireBase):
    pass


class LocataireUpdate(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=50)
    nas_last4: Optional[str] = Field(default=None, max_length=4, min_length=4)
    ancienne_adresse: Optional[str] = Field(default=None, max_length=500)
    date_naissance: Optional[date] = None
    employeur: Optional[str] = Field(default=None, max_length=255)
    revenu_annuel: Optional[float] = Field(default=None, ge=0)
    assurance_confirmee_le: Optional[date] = None
    notes: Optional[str] = None


class LocataireRead(LocataireBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    paiement_score: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class LocataireListItem(LocataireRead):
    """Ligne de la page Locataires : + immeuble/logement du bail ACTIF
    le plus récent (colonnes cliquables — retour Phil 2026-07-20)."""

    immeuble_id: Optional[int] = None
    immeuble_name: Optional[str] = None
    logement_id: Optional[int] = None
    logement_numero: Optional[str] = None
    #: Ce qui a fait matcher la recherche quand ce n'est PAS le nom du
    #: locataire (ex. « garant : Jacques Roy », « courriel », « téléphone »)
    #: — retour Phil 2026-09-09 : « quand je cherche Jacques, je vois
    #: Sébastien ». None quand le nom lui-même correspond.
    match_via: Optional[str] = None


# ─── Garants & contacts d'un locataire (2026-09-09) ─────────────────────


def _valider_role(v: str) -> str:
    r = (v or "garant").strip().lower()
    if r not in CONTACT_ROLES:
        raise ValueError(
            "Rôle invalide — attendu : " + ", ".join(CONTACT_ROLES) + "."
        )
    return r


class LocataireContactBase(BaseModel):
    #: garant | colocataire | occupant | urgence
    role: str = "garant"
    full_name: str = Field(..., min_length=1, max_length=255)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=50)
    #: Lien avec le locataire (« père », « conjointe »…).
    relation: Optional[str] = Field(default=None, max_length=80)
    #: C'est cette personne qui paie le loyer (virements à son nom).
    paie_le_loyer: bool = False
    notes: Optional[str] = None
    actif: bool = True

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _valider_role(v)


class LocataireContactCreate(LocataireContactBase):
    pass


class LocataireContactUpdate(BaseModel):
    role: Optional[str] = None
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=50)
    relation: Optional[str] = Field(default=None, max_length=80)
    paie_le_loyer: Optional[bool] = None
    notes: Optional[str] = None
    actif: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _valider_role(v)


class LocataireContactRead(LocataireContactBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    locataire_id: int
    created_by_email: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ─── Dossier TAL (2026-09-09) ────────────────────────────────────────────


def _valider_motif(v: str) -> str:
    m = (v or "non_paiement").strip().lower()
    if m not in TAL_MOTIFS:
        raise ValueError(
            "Motif invalide — attendu : " + ", ".join(TAL_MOTIFS) + "."
        )
    return m


def _valider_statut_tal(v: str) -> str:
    st = (v or "ouvert").strip().lower()
    if st not in TAL_STATUTS:
        raise ValueError(
            "Statut invalide — attendu : " + ", ".join(TAL_STATUTS) + "."
        )
    return st


class TalDossierCreate(BaseModel):
    motif: str = "non_paiement"
    statut: str = "ouvert"
    numero_dossier: Optional[str] = Field(default=None, max_length=64)
    #: Défaut : aujourd'hui (posé côté serveur).
    ouvert_le: Optional[date] = None
    audience_le: Optional[date] = None
    decision_le: Optional[date] = None
    notes: Optional[str] = None

    @field_validator("motif")
    @classmethod
    def _check_motif(cls, v: str) -> str:
        return _valider_motif(v)

    @field_validator("statut")
    @classmethod
    def _check_statut(cls, v: str) -> str:
        return _valider_statut_tal(v)


class TalDossierUpdate(BaseModel):
    motif: Optional[str] = None
    statut: Optional[str] = None
    numero_dossier: Optional[str] = Field(default=None, max_length=64)
    ouvert_le: Optional[date] = None
    audience_le: Optional[date] = None
    decision_le: Optional[date] = None
    notes: Optional[str] = None

    @field_validator("motif")
    @classmethod
    def _check_motif(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _valider_motif(v)

    @field_validator("statut")
    @classmethod
    def _check_statut(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _valider_statut_tal(v)


class TalDossierRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bail_id: int
    locataire_id: Optional[int] = None
    logement_id: Optional[int] = None
    immeuble_id: Optional[int] = None
    motif: str
    statut: str
    numero_dossier: Optional[str] = None
    ouvert_le: Optional[date] = None
    audience_le: Optional[date] = None
    decision_le: Optional[date] = None
    notes: Optional[str] = None
    created_by_email: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    #: Enrichissements d'affichage (fiche locataire : « Immeuble · 3 »).
    locataire_name: Optional[str] = None
    immeuble_name: Optional[str] = None
    logement_numero: Optional[str] = None
    #: Nombre de pièces rattachées (liste) — le détail les renvoie.
    nb_documents: int = 0


# ─── Bail ───────────────────────────────────────────────────────────────


#: Message unique pour le jour d'échéance du loyer (bail TAL « Ou le ___ »).
#: Borné à 28 : au-delà, le jour n'existe pas tous les mois (février).
JOUR_ECHEANCE_ERREUR = (
    "Le jour d'échéance du loyer doit être entre 1 et 28 "
    "(28 max pour que le jour existe aussi en février)."
)


def _valider_jour_echeance(v: Optional[int]) -> Optional[int]:
    if v is None:
        return v
    if not 1 <= int(v) <= 28:
        raise ValueError(JOUR_ECHEANCE_ERREUR)
    return int(v)


class BailBase(BaseModel):
    logement_id: int
    locataire_id: int
    date_debut: date
    date_fin: date
    loyer_mensuel: float = Field(..., ge=0)
    depot_garantie: Optional[float] = Field(default=None, ge=0)
    #: Date de RÉCEPTION du dépôt et détenteur — saisissables dès la
    #: création (retour Phil 2026-09-09 : « date inconnue » partout
    #: parce qu'aucun formulaire ne les écrivait).
    depot_recu_le: Optional[date] = None
    depot_detenteur: Optional[str] = Field(default=None, max_length=120)
    chauffage_inclus: bool = False
    eau_chaude_inclus: bool = False
    electricite_inclus: bool = False
    internet_inclus: bool = False
    status: str = Field(default="actif", max_length=16)
    document_url: Optional[str] = Field(default=None, max_length=1000)
    notes: Optional[str] = None
    #: Bail AU MOIS (chambres) : reconduction auto, hors du suivi des
    #: renouvellements, loyers sans égard à date_fin.
    au_mois: Optional[bool] = None
    #: Jour du mois où le loyer est payable (bail TAL : « le 1er jour du
    #: mois » OU « Ou le ___ »). 1 par défaut.
    jour_echeance: int = Field(
        default=1,
        description=(
            "Jour du mois où le loyer est payable (1 à 28). "
            "Habituellement le 1er."
        ),
    )

    @field_validator("jour_echeance")
    @classmethod
    def _check_jour_echeance(cls, v: int) -> int:
        return _valider_jour_echeance(v)  # type: ignore[return-value]


class BailCreate(BailBase):
    pass


class BailUpdate(BaseModel):
    date_debut: Optional[date] = None
    date_fin: Optional[date] = None
    loyer_mensuel: Optional[float] = Field(default=None, ge=0)
    depot_garantie: Optional[float] = Field(default=None, ge=0)
    #: Date de RÉCEPTION du dépôt, et qui détient l'argent.
    depot_recu_le: Optional[date] = None
    depot_detenteur: Optional[str] = Field(default=None, max_length=120)
    # Date de remise du dépôt (page Dépôts → « Marquer rendu »).
    depot_rendu_le: Optional[date] = None
    chauffage_inclus: Optional[bool] = None
    eau_chaude_inclus: Optional[bool] = None
    electricite_inclus: Optional[bool] = None
    internet_inclus: Optional[bool] = None
    status: Optional[str] = Field(default=None, max_length=16)
    document_url: Optional[str] = Field(default=None, max_length=1000)
    notes: Optional[str] = None
    au_mois: Optional[bool] = None
    #: Jour du mois où le loyer est payable (bail TAL « Ou le ___ »).
    jour_echeance: Optional[int] = None
    #: Dossier TAL ouvert (non-paiement) — coché depuis Paiements ;
    #: envoyer explicitement null pour le décocher.
    tal_dossier_ouvert_le: Optional[date] = None

    @field_validator("jour_echeance")
    @classmethod
    def _check_jour_echeance(cls, v: Optional[int]) -> Optional[int]:
        return _valider_jour_echeance(v)


class BailRead(BailBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    depot_rendu_le: Optional[date] = None
    #: Dossier TAL ouvert (non-paiement).
    tal_dossier_ouvert_le: Optional[date] = None
    created_at: datetime
    updated_at: datetime
    signed_at: Optional[datetime] = None
    signed_by_name: Optional[str] = None
    #: LE bail courant (imm_documents) — s'ouvre au clic depuis la page
    #: Baux ; « Remplacer le bail » change ce pointeur.
    document_id: Optional[int] = None
    #: Dernier avis de renouvellement du bail (pastille + bouton
    #: « Avis ») — rempli par les endpoints qui en ont besoin.
    renouvellement_status: Optional[str] = None
    renouvellement_avis_document_id: Optional[int] = None


# ─── Hypothèque ─────────────────────────────────────────────────────────


class HypothequeBase(BaseModel):
    immeuble_id: int
    rang: int = Field(default=1, ge=1, le=9)
    preteur: str = Field(..., min_length=1, max_length=255)
    montant_initial: float = Field(..., ge=0)
    balance_actuelle: Optional[float] = Field(default=None, ge=0)
    taux_pct: Optional[float] = Field(default=None, ge=0, le=100)
    type_taux: Optional[str] = Field(default=None, max_length=32)
    amortissement_mois: Optional[int] = Field(default=None, ge=1)
    paiement_mensuel: Optional[float] = Field(default=None, ge=0)
    # 'semi' (composition semi-annuelle, standard CA) | 'mensuelle'.
    composition_interets: Optional[str] = Field(default=None, max_length=16)
    date_debut: Optional[date] = None
    date_fin_terme: Optional[date] = None
    status: str = Field(default="active", max_length=16)
    notes: Optional[str] = None


class HypothequeCreate(HypothequeBase):
    pass


class HypothequeUpdate(BaseModel):
    rang: Optional[int] = Field(default=None, ge=1, le=9)
    preteur: Optional[str] = Field(default=None, min_length=1, max_length=255)
    montant_initial: Optional[float] = Field(default=None, ge=0)
    balance_actuelle: Optional[float] = Field(default=None, ge=0)
    taux_pct: Optional[float] = Field(default=None, ge=0, le=100)
    type_taux: Optional[str] = Field(default=None, max_length=32)
    amortissement_mois: Optional[int] = Field(default=None, ge=1)
    paiement_mensuel: Optional[float] = Field(default=None, ge=0)
    composition_interets: Optional[str] = Field(default=None, max_length=16)
    date_debut: Optional[date] = None
    date_fin_terme: Optional[date] = None
    status: Optional[str] = Field(default=None, max_length=16)
    notes: Optional[str] = None


class HypothequeRead(HypothequeBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    # Balance THÉORIQUE au jour J (tableau d'amortissement) — calculée
    # à la volée, jamais stockée. La balance saisie prime toujours.
    balance_calculee: Optional[float] = None
    created_at: datetime
    updated_at: datetime


# ─── Évaluation ─────────────────────────────────────────────────────────


class EvaluationBase(BaseModel):
    immeuble_id: int
    kind: str = Field(default="marchande", max_length=16)
    valeur: float = Field(..., ge=0)
    date_evaluation: date
    source: Optional[str] = Field(default=None, max_length=128)
    notes: Optional[str] = None
    # Évaluation de référence pour le calcul d'équité (une seule par
    # immeuble — l'API remet les autres à False quand on passe à True).
    is_reference: bool = False


class EvaluationCreate(EvaluationBase):
    pass


class EvaluationUpdate(BaseModel):
    # Tous les champs éditables (retour Phil 2026-07-16 : « permets-moi
    # de modifier une évaluation ») — exclude_unset côté endpoint.
    kind: Optional[str] = Field(default=None, max_length=16)
    valeur: Optional[float] = Field(default=None, ge=0)
    date_evaluation: Optional[date] = None
    source: Optional[str] = Field(default=None, max_length=128)
    notes: Optional[str] = None
    is_reference: Optional[bool] = None


class EvaluationRead(EvaluationBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


# ─── Paiement de loyer ──────────────────────────────────────────────────


class PaiementLoyerBase(BaseModel):
    bail_id: int
    mois_couvert: date
    montant: float = Field(..., ge=0)
    paye_le: Optional[date] = None
    methode: Optional[str] = Field(default=None, max_length=32)
    reference: Optional[str] = Field(default=None, max_length=128)
    notes: Optional[str] = None


class PaiementLoyerCreate(PaiementLoyerBase):
    pass


class PaiementLoyerRead(PaiementLoyerBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    en_retard: bool
    created_at: datetime


# ─── Maintenance ────────────────────────────────────────────────────────


class MaintenanceOrdreBase(BaseModel):
    immeuble_id: int
    logement_id: Optional[int] = None
    titre: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    priorite: str = Field(default="normale", max_length=16)
    status: str = Field(default="ouvert", max_length=16)
    fournisseur: Optional[str] = Field(default=None, max_length=255)
    cout_estime: Optional[float] = Field(default=None, ge=0)
    cout_reel: Optional[float] = Field(default=None, ge=0)
    plannifie_pour: Optional[date] = None
    complete_le: Optional[date] = None
    notes: Optional[str] = None


class MaintenanceOrdreCreate(MaintenanceOrdreBase):
    pass


class MaintenanceOrdreUpdate(BaseModel):
    logement_id: Optional[int] = None
    titre: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    priorite: Optional[str] = Field(default=None, max_length=16)
    status: Optional[str] = Field(default=None, max_length=16)
    fournisseur: Optional[str] = Field(default=None, max_length=255)
    cout_estime: Optional[float] = Field(default=None, ge=0)
    cout_reel: Optional[float] = Field(default=None, ge=0)
    plannifie_pour: Optional[date] = None
    complete_le: Optional[date] = None
    notes: Optional[str] = None


class MaintenanceOrdreRead(MaintenanceOrdreBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class MaintenanceOverviewRow(BaseModel):
    """Ligne de la vue maintenance transversale (tous immeubles)."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    immeuble_id: int
    immeuble_name: str
    logement_id: Optional[int] = None
    logement_numero: Optional[str] = None
    titre: str
    description: Optional[str] = None
    priorite: str
    status: str
    fournisseur: Optional[str] = None
    cout_estime: Optional[float] = None
    cout_reel: Optional[float] = None
    plannifie_pour: Optional[date] = None
    complete_le: Optional[date] = None
    created_at: datetime
    jours_ouverts: Optional[int] = None  # depuis la création, si actif


class MaintenanceOverview(BaseModel):
    """Agrégat maintenance sur l'ensemble du portefeuille visible."""

    rows: List[MaintenanceOverviewRow] = Field(default_factory=list)
    nb_total: int = 0
    nb_ouvert: int = 0
    nb_en_cours: int = 0
    nb_en_attente: int = 0
    nb_termine: int = 0
    nb_annule: int = 0
    nb_urgences_actives: int = 0
    total_cout_estime_actif: float = 0.0
    total_cout_reel: float = 0.0


class DossierBail(BaseModel):
    """Bail tel qu'affiché dans la fiche 360 d'un locataire."""

    id: int
    immeuble_id: int
    immeuble_name: str
    logement_id: Optional[int] = None
    logement_numero: Optional[str] = None
    date_debut: date
    date_fin: date
    loyer_mensuel: float
    depot_garantie: Optional[float] = None
    status: str
    #: Statut du dossier de relocation ACTIF lié (kanban Locations) :
    #: par le bail ENTRANT (bail_a_envoyer | bail_envoye) OU par le
    #: bail SORTANT (avis_recu, visites… — M1, audit 2026-08-13).
    relocation_statut: Optional[str] = None
    #: Id du dossier lié — lien « Ouvrir dans Locations » ciblé.
    relocation_dossier_id: Optional[int] = None
    #: LE bail courant (imm_documents) — bouton « Bail » de la fiche.
    document_id: Optional[int] = None
    signed_at: Optional[datetime] = None
    au_mois: Optional[bool] = None
    #: Jour d'échéance du loyer (affiché « payable le X » si ≠ 1).
    jour_echeance: Optional[int] = None


class DossierPaiement(BaseModel):
    id: int
    bail_id: int
    mois_couvert: date
    montant: float
    paye_le: Optional[date] = None
    methode: Optional[str] = None
    en_retard: bool = False


class LocataireCommunicationCreate(BaseModel):
    """Entrée manuelle du journal de communications (fiche locataire)."""

    kind: str = "note"  # note | appel | courriel | sms | visite | autre
    contenu: str


class LocataireCommunicationRead(BaseModel):
    id: int
    locataire_id: int
    kind: str
    contenu: str
    auteur: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DossierRenouvellement(BaseModel):
    """Avis/renouvellement de bail tel qu'affiché dans la fiche locataire."""

    id: int
    bail_id: int
    immeuble_name: str
    logement_numero: Optional[str] = None
    avis_envoye_le: date
    nouveau_loyer: Optional[float] = None
    nouvelle_date_debut: Optional[date] = None
    nouvelle_date_fin: Optional[date] = None
    status: str
    locataire_repondu_le: Optional[date] = None
    notes: Optional[str] = None
    #: L'AVIS courant (imm_documents) — bouton « Avis » de la fiche.
    document_id: Optional[int] = None


class LocataireDossier(BaseModel):
    """Vue 360 d'un locataire : baux, historique de paiements, agrégats."""

    locataire: LocataireRead
    baux: List[DossierBail] = Field(default_factory=list)
    paiements: List[DossierPaiement] = Field(default_factory=list)
    renouvellements: List[DossierRenouvellement] = Field(default_factory=list)
    communications: List[LocataireCommunicationRead] = Field(
        default_factory=list
    )
    nb_baux_actifs: int = 0
    loyer_actuel: float = 0.0
    depot_total: float = 0.0
    total_paye: float = 0.0
    nb_paiements: int = 0
    nb_retards: int = 0


# ─── Dossier logement (fiche 360) ───────────────────────────────────────


class LogementDossierLocataire(BaseModel):
    """Locataire tel qu'affiché dans la fiche 360 d'un logement."""

    id: int
    full_name: str


class LogementDossierBail(BaseModel):
    """Bail tel qu'affiché dans la fiche 360 d'un logement."""

    id: int
    locataire: Optional[LogementDossierLocataire] = None
    loyer_mensuel: float
    date_debut: date
    date_fin: date
    status: str
    #: Statut du dossier de relocation ACTIF lié (kanban Locations) :
    #: par le bail ENTRANT (bail_a_envoyer | bail_envoye) OU par le
    #: bail SORTANT (avis_recu, visites… — M1, audit 2026-08-13).
    relocation_statut: Optional[str] = None
    #: Id du dossier lié — lien « Ouvrir dans Locations » ciblé.
    relocation_dossier_id: Optional[int] = None
    document_url: Optional[str] = None
    signed_at: Optional[datetime] = None
    #: LE bail courant (imm_documents) — bouton « Bail » de la fiche.
    document_id: Optional[int] = None
    au_mois: Optional[bool] = None
    #: Jour d'échéance du loyer (affiché « payable le X » si ≠ 1).
    jour_echeance: Optional[int] = None


class LogementDossierBon(BaseModel):
    """Bon de travail (réno / maintenance) rattaché au logement."""

    id: int
    reference: str
    title: str
    status: str
    montant: Optional[float] = None
    created_at: Optional[datetime] = None


class LogementDossierImmeuble(BaseModel):
    id: int
    name: str
    address: Optional[str] = None
    #: La fiche 360 adapte l'affichage du loyer : en gestion EXTERNE,
    #: le loyer saisi sur le logement est la vérité (pas de bail chez
    #: nous) — retour client 2026-08-14.
    gestion_externe: bool = False


class LoyerPoint(BaseModel):
    """Point d'historique de loyer, dérivé des baux (ordre chronologique)."""

    date_debut: date
    loyer_mensuel: float


class LogementDossier(BaseModel):
    """Vue 360 d'un logement : infos + immeuble, baux (avec locataire),
    bons de travail et historique de loyer (fluctuation)."""

    logement: LogementRead
    immeuble: LogementDossierImmeuble
    baux: List[LogementDossierBail] = Field(default_factory=list)
    bons_travail: List[LogementDossierBon] = Field(default_factory=list)
    historique_loyer: List[LoyerPoint] = Field(default_factory=list)


# ─── KPIs financiers (calculés) ─────────────────────────────────────────


class ImmeubleFinancials(BaseModel):
    """Snapshot financier d'un immeuble.

    Calculé à la volée depuis baux + hypothèques + évaluations.
    """

    immeuble_id: int
    nb_logements_actifs: int = 0
    nb_logements_occupes: int = 0
    taux_occupation: float = 0.0  # 0..1

    # Revenus. Le montant PRINCIPAL = unités louées seulement (bail actif,
    # ou statut « occupé » en gestion externe). toutes_unites = potentiel
    # incluant les vacantes au loyer demandé (hors location exclu).
    revenu_brut_mensuel: float = 0.0
    revenu_brut_annuel: float = 0.0
    revenu_brut_mensuel_toutes_unites: float = 0.0

    # Hypothèque
    paiement_hypotheque_mensuel: float = 0.0
    balance_hypothecaire: float = 0.0

    # Valeurs
    valeur_actuelle: Optional[float] = None
    valeur_municipale: Optional[float] = None
    purchase_price: Optional[float] = None

    # Ratios. Cap rate : NOI réel (revenus − dépenses d'exploitation
    # récurrentes, sans hypothèque) si ≥1 dépense récurrente est saisie ;
    # sinon fallback heuristique NOI ≈ 50 % du revenu brut.
    grm: Optional[float] = None         # Gross Rent Multiplier = valeur / revenu_annuel
    cap_rate: Optional[float] = None    # NOI / valeur
    cap_rate_estime: bool = True        # True = heuristique 50 %, False = NOI réel
    cash_flow_mensuel: Optional[float] = None
    appreciation_pct: Optional[float] = None  # vs purchase_price


# ─── Imports en batch ───────────────────────────────────────────────────


class ImmeubleImportFromMatriculeRequest(BaseModel):
    """Crée un immeuble en pré-remplissant depuis le rôle d'évaluation MAMH."""

    matricule: str = Field(..., min_length=1, max_length=64)
    name: Optional[str] = None
    create_logements: bool = True


class ImmeubleImportResult(BaseModel):
    immeuble: ImmeubleRead
    nb_logements_crees: int = 0
    matched_unit_id: Optional[int] = None


# ─── Import « rent roll » PlexFlow (copier-coller) ──────────────────────


class PlexImportRequest(BaseModel):
    """Texte brut copié depuis PlexFlow. `dry_run` = aperçu sans écrire.

    `company_overrides` : mapping explicite nom de compagnie (tel que
    parsé) → entreprise_id, pour les cas où le nom PlexFlow ne
    correspond pas au nom Kratos (ex. « 9510-7520 Québec inc. » = BGV).
    """

    raw_text: str = Field(..., min_length=1)
    dry_run: bool = True
    company_overrides: dict[str, int] = Field(default_factory=dict)


class PlexImportUnit(BaseModel):
    numero: str
    tenant: Optional[str] = None
    rent: Optional[float] = None
    status: str
    will_create_lease: bool = False
    warnings: List[str] = Field(default_factory=list)


class PlexImportBuilding(BaseModel):
    address: str
    city: Optional[str] = None
    postal_code: Optional[str] = None
    nb_units: int = 0
    nb_leases: int = 0
    already_exists: bool = False
    units: List[PlexImportUnit] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class PlexImportCompany(BaseModel):
    name: str
    entreprise_id: Optional[int] = None
    matched: bool = False
    buildings: List[PlexImportBuilding] = Field(default_factory=list)


class PlexImportCreated(BaseModel):
    immeubles: int = 0
    logements: int = 0
    locataires: int = 0
    baux: int = 0
    buildings_skipped: int = 0


class PlexImportResult(BaseModel):
    dry_run: bool
    companies: List[PlexImportCompany] = Field(default_factory=list)
    totals: dict = Field(default_factory=dict)
    created: Optional[PlexImportCreated] = None
    warnings: List[str] = Field(default_factory=list)
