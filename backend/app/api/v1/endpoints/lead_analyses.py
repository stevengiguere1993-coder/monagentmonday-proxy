"""API pour les analyses de leads immobiliers.

Endpoints :
    POST   /lead-analyses/extract   multipart : urls + text + files
                                    → crée une (ou plusieurs) fiche(s)
    GET    /lead-analyses           liste paginée + filtres
    GET    /lead-analyses/{id}      détail
    PATCH  /lead-analyses/{id}      MAJ partielle (édition fiche)
    DELETE /lead-analyses/{id}      suppression (cascade attachments)
    POST   /lead-analyses/{id}/convert-to-lead  → ProspectionLead
    POST   /lead-analyses/{id}/convert-to-deal  → ProspectionDeal (Pipeline)
    GET    /lead-analyses/{id}/attachments/{att_id}  blob inline

Restreint au volet `prospection`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession
from app.services.audit import log_action
from app.models.lead_analysis import (
    LeadAnalysis,
    LeadAnalysisAttachment,
    LeadAnalysisStatus,
)
from app.models.prospection_analysis_default import (
    ProspectionAnalysisDefault,
)
from app.models.prospection_lead import (
    ProspectionLead,
    ProspectionLeadKind,
    ProspectionLeadStatus,
    ProspectionOwnerKind,
)
from app.models.prospection_deal import ProspectionDeal
from app.services.lead_extraction import extract_lead_info, _check_tesseract_status
from app.services.lead_validation import (
    validate_extraction,
    summarize_severity,
)


log = logging.getLogger(__name__)
router = APIRouter(prefix="/lead-analyses", tags=["lead-analyses"])


_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB / fichier


def _require_prospection(user: CurrentUser) -> None:
    """Restreint au volet prospection. Backward-compat : volets vide
    → on accepte (anciens users sans volets_json)."""
    volets = getattr(user, "volets", None) or []
    if volets and "prospection" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Prospection » non autorisé.",
        )


# ── Schémas Pydantic ───────────────────────────────────────────────


class AttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    content_type: str
    size_bytes: int


class LeadAnalysisRead(BaseModel):
    """Détail complet d'une analyse — utilisé par la fiche."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    position: int

    address: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    province: Optional[str] = None
    asking_price: Optional[float] = None
    nb_logements: Optional[int] = None
    typology_json: Optional[str] = None
    revenus_bruts: Optional[float] = None
    taxes_municipales: Optional[float] = None
    taxes_scolaires: Optional[float] = None
    assurances: Optional[float] = None
    energie: Optional[float] = None
    depenses_autres: Optional[float] = None
    annee_construction: Optional[int] = None

    superficie_terrain: Optional[float] = None
    superficie_batiment: Optional[float] = None
    evaluation_municipale: Optional[float] = None
    description: Optional[str] = None
    courtier_nom: Optional[str] = None
    courtier_contact: Optional[str] = None
    type_batiment: Optional[str] = None
    nb_stationnements: Optional[int] = None

    source_urls: Optional[str] = None
    source_text: Optional[str] = None

    loyers_projetes_json: Optional[str] = None
    loyers_max_abordabilite_json: Optional[str] = None
    travaux_estimes: Optional[float] = None
    nb_logements_ajoutes: Optional[int] = None
    nb_thermopompes_ajoutees: Optional[int] = None
    ajout_wifi: Optional[bool] = None
    reduction_energie_pct: Optional[float] = None
    taux_interet_refi_pct: Optional[float] = None
    tga_pct: Optional[float] = None
    taux_interet_achat_pct: Optional[float] = None
    duree_projet_annees: Optional[int] = None
    frais_developpement: Optional[float] = None
    frais_negociations: Optional[float] = None

    analysis_results_json: Optional[str] = None
    best_refi_amount: Optional[float] = None
    best_refi_program: Optional[str] = None
    mdf_preteur_b: Optional[float] = None
    mdf_preteur_b_pct: Optional[float] = None
    # Taux d'intérêt prêteur B pendant la phase chantier (défaut 8 %).
    # Stocké en pourcentage (8.0 = 8 %), comme les autres `*_pct`.
    # Utilisé pour calculer les intérêts du projet dans le moteur
    # `lead_analysis_finance` (L17 = (1 - MDF%) × prix × taux × durée).
    taux_interet_preteur_b_projet_pct: Optional[float] = None
    frais_demarrage_overrides_json: Optional[str] = None
    frais_demarrage_financables_json: Optional[str] = None
    # Stratégies d'acquisition (août 2026, chantier staging).
    strategie_acquisition: Optional[str] = None
    programme_achat: Optional[str] = None
    refi_retenu: Optional[str] = None
    balance_vente_montant: Optional[float] = None
    cashback_montant: Optional[float] = None
    balance_vente_taux_pct: Optional[float] = None
    projection_horizon_annees: Optional[int] = None
    # Croissances annuelles (FRACTIONS, 0.03 = 3 %) — partagées avec
    # le TRI (source unique) et la projection des achats directs.
    tri_croissance_loyers: Optional[float] = None
    tri_croissance_depenses: Optional[float] = None
    # Phase 3 — optimisation par unité (liste JSON).
    unites_json: Optional[str] = None
    notes: Optional[str] = None
    converted_to_lead_id: Optional[int] = None
    converted_to_deal_id: Optional[int] = None

    # Modèle d'extraction (cascade tri-couche) — affiché via le
    # badge « Extrait par » dans la fiche et sur les cards kanban.
    model_used: Optional[str] = None

    # Phase A3 — Anomalies détectées par le validator post-extraction
    # (bornes + divergences local↔gemini). Liste structurée :
    # [{field, severity, message, source_local?, source_gemini?,
    #   source_claude?}, …]. None ou [] si rien à signaler.
    validation_warnings: Optional[List[dict]] = None

    created_at: datetime
    updated_at: datetime

    attachments: List[AttachmentRead] = Field(default_factory=list)


class LeadAnalysisListItem(BaseModel):
    """Vue compacte pour la liste/kanban (sans le JSON détaillé)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    position: int
    address: Optional[str]
    city: Optional[str]
    asking_price: Optional[float]
    nb_logements: Optional[int]
    annee_construction: Optional[int]
    best_refi_amount: Optional[float]
    best_refi_program: Optional[str] = None
    mdf_preteur_b: Optional[float] = None
    type_batiment: Optional[str]
    converted_to_lead_id: Optional[int]
    converted_to_deal_id: Optional[int] = None
    # Modèle d'extraction — affiché via badge sur la card kanban.
    model_used: Optional[str] = None
    # Phase A3 : indicateur compact pour la card kanban — sévérité max
    # ("error" / "warning" / "info" / null) + nombre total d'anomalies.
    # Le détail complet est dans /lead-analyses/{id} (fiche).
    validation_severity: Optional[str] = None
    validation_count: int = 0
    created_at: datetime
    attachments_count: int = 0


class LeadAnalysisUpdate(BaseModel):
    """Tous les champs éditables — la fiche envoie un patch partiel."""

    status: Optional[str] = Field(
        default=None,
        pattern=r"^(a_analyser|decision_en_attente|interessant|abandonne)$",
    )
    position: Optional[int] = None

    address: Optional[str] = Field(default=None, max_length=500)
    city: Optional[str] = Field(default=None, max_length=120)
    postal_code: Optional[str] = Field(default=None, max_length=16)
    province: Optional[str] = Field(default=None, max_length=8)
    asking_price: Optional[float] = None
    nb_logements: Optional[int] = None
    typology_json: Optional[str] = None
    revenus_bruts: Optional[float] = None
    taxes_municipales: Optional[float] = None
    taxes_scolaires: Optional[float] = None
    assurances: Optional[float] = None
    energie: Optional[float] = None
    depenses_autres: Optional[float] = None
    annee_construction: Optional[int] = None

    superficie_terrain: Optional[float] = None
    superficie_batiment: Optional[float] = None
    evaluation_municipale: Optional[float] = None
    description: Optional[str] = None
    courtier_nom: Optional[str] = Field(default=None, max_length=255)
    courtier_contact: Optional[str] = Field(default=None, max_length=255)
    type_batiment: Optional[str] = Field(default=None, max_length=64)
    nb_stationnements: Optional[int] = None

    loyers_projetes_json: Optional[str] = None
    loyers_max_abordabilite_json: Optional[str] = None
    travaux_estimes: Optional[float] = None
    nb_logements_ajoutes: Optional[int] = None
    nb_thermopompes_ajoutees: Optional[int] = None
    ajout_wifi: Optional[bool] = None
    reduction_energie_pct: Optional[float] = None
    taux_interet_refi_pct: Optional[float] = None
    tga_pct: Optional[float] = None
    taux_interet_achat_pct: Optional[float] = None
    duree_projet_annees: Optional[int] = None
    frais_developpement: Optional[float] = None
    frais_negociations: Optional[float] = None

    mdf_preteur_b_pct: Optional[float] = None
    taux_interet_preteur_b_projet_pct: Optional[float] = None
    frais_demarrage_overrides_json: Optional[str] = None
    frais_demarrage_financables_json: Optional[str] = None

    # Stratégies d'acquisition (août 2026, chantier staging).
    strategie_acquisition: Optional[str] = Field(
        default=None,
        pattern=(
            r"^(preteur_b|traditionnel|conventionnel|schl_std|"
            r"aph_50|aph_100)$"
        ),
    )
    # Programme retenu du mode traditionnel.
    programme_achat: Optional[str] = Field(
        default=None,
        pattern=r"^(conventionnel|schl_std|aph_50|aph_100)$",
    )
    # Référence de refinancement choisie manuellement (null = meilleur).
    refi_retenu: Optional[str] = Field(
        default=None,
        pattern=(
            r"^(refi_schl|refi_aph_50|refi_aph_100|"
            r"conventionnel|schl_std|aph_50|aph_100)$"
        ),
    )
    balance_vente_montant: Optional[float] = Field(default=None, ge=0)
    # Cashback reçu au notaire (prix déclaré = prix + cashback).
    cashback_montant: Optional[float] = Field(default=None, ge=0)
    balance_vente_taux_pct: Optional[float] = Field(
        default=None, ge=0, le=30
    )
    projection_horizon_annees: Optional[int] = Field(
        default=None, ge=1, le=30
    )
    # Croissances annuelles (FRACTIONS, 0.03 = 3 %) — partagées avec
    # le TRI.
    tri_croissance_loyers: Optional[float] = Field(
        default=None, ge=0, le=0.3
    )
    tri_croissance_depenses: Optional[float] = Field(
        default=None, ge=0, le=0.3
    )
    # Phase 3 — optimisation par unité : liste JSON
    # ``[{typo, loyer_actuel, loyer_cible, optimiser}, …]``.
    unites_json: Optional[str] = Field(default=None, max_length=40_000)

    notes: Optional[str] = None


class ExtractResult(BaseModel):
    """Réponse de l'endpoint extract — peut créer plusieurs leads
    si Claude détecte plusieurs immeubles distincts."""

    created: List[LeadAnalysisListItem]
    warnings: List[str] = Field(default_factory=list)
    model_used: Optional[str] = None


class ConvertResult(BaseModel):
    lead_id: int


class ConvertDealResult(BaseModel):
    deal_id: int


# ── Helpers ────────────────────────────────────────────────────────


def _to_list_item(rec: LeadAnalysis, attachments_count: int) -> LeadAnalysisListItem:
    # Phase A3 : résumé compact des anomalies pour la card kanban.
    vw = rec.validation_warnings or []
    val_severity = summarize_severity(vw) if vw else None
    return LeadAnalysisListItem(
        id=rec.id,
        status=rec.status,
        position=rec.position,
        address=rec.address,
        city=rec.city,
        asking_price=float(rec.asking_price) if rec.asking_price is not None else None,
        nb_logements=rec.nb_logements,
        annee_construction=rec.annee_construction,
        best_refi_amount=(
            float(rec.best_refi_amount)
            if rec.best_refi_amount is not None
            else None
        ),
        best_refi_program=rec.best_refi_program,
        mdf_preteur_b=(
            float(rec.mdf_preteur_b)
            if rec.mdf_preteur_b is not None
            else None
        ),
        type_batiment=rec.type_batiment,
        converted_to_lead_id=rec.converted_to_lead_id,
        converted_to_deal_id=rec.converted_to_deal_id,
        model_used=rec.model_used,
        validation_severity=val_severity,
        validation_count=len(vw),
        created_at=rec.created_at,
        attachments_count=attachments_count,
    )


def _map_extracted_to_lead(data: dict) -> dict:
    """Convertit le JSON renvoyé par Claude en kwargs pour LeadAnalysis."""
    out: dict = {}
    # Fields scalaires directs.
    scalar_fields = (
        "address", "city", "postal_code", "province",
        "asking_price", "nb_logements", "revenus_bruts",
        "taxes_municipales", "taxes_scolaires", "assurances",
        "energie", "depenses_autres", "annee_construction",
        "superficie_terrain", "superficie_batiment",
        "evaluation_municipale", "description",
        "courtier_nom", "courtier_contact", "type_batiment",
        "nb_stationnements",
    )
    for f in scalar_fields:
        v = data.get(f)
        if v == "" or v == "null":
            v = None
        if v is not None:
            out[f] = v
    # Typology sub-dict → JSON serialized.
    typology = data.get("typology")
    if isinstance(typology, dict) and typology:
        out["typology_json"] = json.dumps(typology)
    return out


# ── Endpoints ──────────────────────────────────────────────────────


# Défauts appliqués à la création d'une fiche `LeadAnalysis`
# pour pré-remplir les champs manuels d'analyse financière.
# Modifiables ensuite par l'utilisateur dans la fiche.
def _parse_unites(raw: Optional[str]) -> list[dict]:
    """Phase 3 — liste d'unités ``{typo, loyer_actuel, loyer_cible,
    optimiser}``. Défensif : tout ce qui n'est pas une liste de dicts
    devient une liste vide (comportement historique)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for u in data[:200]:
        if not isinstance(u, dict):
            continue
        try:
            out.append({
                "typo": str(u.get("typo") or ""),
                "loyer_actuel": float(u.get("loyer_actuel") or 0),
                "loyer_cible": float(u.get("loyer_cible") or 0),
                "optimiser": bool(u.get("optimiser", True)),
            })
        except (TypeError, ValueError):
            continue
    return out


def _parse_financables(raw: Optional[str]) -> list[str]:
    """Décode la liste JSON des clés finançables. Si invalide ou
    None, retourne les défauts (rapport efficacité, dev, travaux)."""
    DEFAULT = ["rapport_efficacite", "frais_developpement", "frais_travaux"]
    if not raw:
        return DEFAULT
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x) for x in v if isinstance(x, str)]
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT


# Défauts "en dur" — valeurs initiales si la table
# ``prospection_analysis_defaults`` n'a pas (encore) été seedée ou si
# une clé manque (ex. nouveau défaut ajouté en code sans seed). Le
# pourcentage est stocké en pct (8.0 = 8 %), comme dans ``LeadAnalysis``.
_DEFAULTS_FALLBACK: dict = {
    "nb_logements_ajoutes": 0,
    "nb_thermopompes_ajoutees": 0,
    "reduction_energie_pct": 0,
    "taux_interet_refi_pct": 3.75,
    "duree_projet_annees": 2,
    "tga_pct": 4.0,
    "taux_interet_achat_pct": 4.0,
    "taux_interet_preteur_b_projet_pct": 8.0,
    "ajout_wifi": True,
    "loyers_max_abordabilite_json": json.dumps({"abordable": 1090}),
    "mdf_preteur_b_pct": 25.0,
    # Frais finançables par défaut (modifiable par l'utilisateur) :
    # rapport d'efficacité énergétique, frais de développement,
    # travaux estimés. Les autres postes (courtier, notaire, etc.)
    # sont payés 100 % cash sauf si on les coche.
    "frais_demarrage_financables_json": json.dumps([
        "rapport_efficacite",
        "frais_developpement",
        "frais_travaux",
    ]),
}


# Mapping clé en BD ↔ champ stocké sur LeadAnalysis.
# Tous les défauts ``inputs_manuels`` qui ont une colonne sur
# ``LeadAnalysis`` sont mappés ici pour pré-remplir une nouvelle
# fiche. Stockés en pourcentage (3.75, 25.0, 8.0) ou unités entières.
# Les autres champs (booléens, JSON figés, ou défauts utilisés
# uniquement au runtime comme ``taux_inoccupation_pct`` /
# ``frais_*`` MDF) ne sont PAS dans ce mapping.
_DB_KEY_TO_FIELD: dict[str, str] = {
    "taux_interet_refi": "taux_interet_refi_pct",
    "mdf_preteur_b_pct": "mdf_preteur_b_pct",
    "taux_interet_preteur_b_projet": "taux_interet_preteur_b_projet_pct",
    "tga_pct": "tga_pct",
    "taux_interet_achat_pct": "taux_interet_achat_pct",
    "reduction_energie_pct": "reduction_energie_pct",
    "duree_projet_annees": "duree_projet_annees",
    "nb_logements_ajoutes": "nb_logements_ajoutes",
    "nb_thermopompes_ajoutees": "nb_thermopompes_ajoutees",
}

# Champs entiers parmi ceux ci-dessus (les autres sont des floats).
# Utilisé pour caster correctement la valeur lue en BD (Float) avant
# de la passer au constructeur ``LeadAnalysis(...)``.
_DB_KEY_INT_FIELDS: set[str] = {
    "duree_projet_annees",
    "nb_logements_ajoutes",
    "nb_thermopompes_ajoutees",
}


# Mapping clé BD → poste FRAIS_FIXES (groupe ``mdf_frais``). Utilisé
# au runtime ``run-financial-analysis`` pour overrider les frais
# hardcoded dans ``lead_analysis_finance.FRAIS_FIXES``.
_DB_KEY_TO_FRAIS_FIXE: dict[str, str] = {
    "frais_evaluateur": "evaluateur",
    "frais_evaluateur_2": "evaluateur_2",
    "frais_inspection": "inspection",
    "frais_avocat": "avocat",
    "frais_notaire": "notaire",
    "frais_notaire_2": "notaire_2",
    "frais_rapport_efficacite": "rapport_efficacite",
}

# Mapping clé BD → % courtier hypothécaire (en fraction). Valeurs en
# BD stockées en pct (1.0 = 1 %), converties en fraction (÷100) ici.
_DB_KEY_TO_PCT_COURTIER: dict[str, str] = {
    "pct_courtier_hypothecaire_1": "courtier_hypothecaire_1",
    "pct_courtier_hypothecaire_2": "courtier_hypothecaire_2",
}

# Mapping clé BD ↔ clé interne du poste FraisDemarrage côté
# ``frais_demarrage_financables_json``. Utilisé pour bâtir la liste
# par défaut des postes finançables à la création d'une fiche, à
# partir des flags ``financable_par_defaut`` configurés en BD.
_DB_KEY_TO_FINANCABLE_KEY: dict[str, str] = {
    "frais_evaluateur": "evaluateur",
    "frais_evaluateur_2": "evaluateur_2",
    "frais_inspection": "inspection",
    "frais_avocat": "avocat",
    "frais_notaire": "notaire",
    "frais_notaire_2": "notaire_2",
    "frais_rapport_efficacite": "rapport_efficacite",
    "pct_courtier_hypothecaire_1": "courtier_hypothecaire_1",
    "pct_courtier_hypothecaire_2": "courtier_hypothecaire_2",
    "frais_dossier_preteur_pct": "frais_dossier_preteur",
}


# ── Juin 2026 : Dé-hardcodage du barème des dépenses normalisées ──────
#
# Mapping clé BD (groupe ``depenses_normalisees``) → clé interne de
# ``lead_analysis_finance.BAREME``. Les valeurs sont chargées au runtime
# de ``run-financial-analysis`` pour overrider le barème hardcoded. Les
# clés ``*_pct`` (pourcentages de gestion) sont stockées en BD en pct
# (4.25 = 4.25 %) et converties en fraction (÷100) pour matcher la
# convention interne de ``BAREME`` (gestion_lt12 = 0.0425). Les autres
# postes ($/log, $/mois) sont passés tels quels.
_DB_KEY_TO_BAREME: dict[str, str] = {
    "conciergerie_moins_12_log": "concierge_lt12",
    "conciergerie_12_log_plus": "concierge_gte12",
    "entretien_par_log": "entretien",
    "gestion_moins_12_pct": "gestion_lt12",
    "gestion_12_log_plus_pct": "gestion_gte12",
    "wifi_par_log_mois": "wifi_par_log",
    "internet_batiment_mois": "internet_fixe",
    "entretien_thermopompe_an": "thermopompe",
    "autres_normalisations_pct": "autres_normalisations_pct",
}

# Clés du barème stockées en BD en pourcentage → fraction (÷100) avant
# de passer au moteur (qui attend des fractions pour la gestion).
_DB_BAREME_PCT_KEYS: set[str] = {
    "gestion_moins_12_pct",
    "gestion_12_log_plus_pct",
    "autres_normalisations_pct",
}


async def _load_bareme_overrides(
    db,
) -> tuple[dict, Optional[int], Optional[float]]:
    """Charge les overrides du barème des dépenses normalisées SCHL +
    le taux d'inoccupation.

    Retourne ``(bareme_overrides, seuil_bascule_bareme_log,
    taux_inoccupation_fraction)`` prêt à passer à ``FinanceInputs``.

    - ``bareme_overrides`` : valeurs BD du groupe ``depenses_normalisees``
      qui écrasent ``lead_analysis_finance.BAREME`` poste par poste ;
      les % de gestion sont convertis pct → fraction.
    - ``seuil_bascule_bareme_log`` (clé ``seuil_bascule_bareme_log``) :
      seuil petit/grand immeuble (12 par défaut). ``None`` si absent →
      le moteur retombe sur ``SEUIL_BASCULE_BAREME_LOG``.
    - ``taux_inoccupation_fraction`` (clé ``taux_inoccupation_pct``,
      groupe ``inputs_manuels``) : stocké en BD en pct (3.0 = 3 %),
      converti en fraction (0.03). ``None`` si absent → le moteur
      retombe sur ``FinanceInputs.taux_inoccupation_pct`` (0.03).
      Juin 2026 : ce défaut était seedé mais JAMAIS relu — fix branché
      ici (cf. PR « prospection-config-dehardcode-1a »).

    Si la table est vide/absente, retourne ``({}, None, None)`` →
    fallback constantes hardcoded.
    """
    bareme: dict[str, float] = {}
    seuil: Optional[int] = None
    inoccupation: Optional[float] = None
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        for row in rows:
            if row.value_float is None:
                continue
            v = float(row.value_float)
            internal = _DB_KEY_TO_BAREME.get(row.key)
            if internal is not None:
                if row.key in _DB_BAREME_PCT_KEYS:
                    v = v / 100.0
                bareme[internal] = v
            elif row.key == "seuil_bascule_bareme_log":
                seuil = int(v)
            elif row.key == "taux_inoccupation_pct":
                # BD en pct (3.0 = 3 %), moteur en fraction (0.03).
                inoccupation = v / 100.0
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load barème overrides from DB: %s", exc)
    return bareme, seuil, inoccupation


# ── Juin 2026 : Dé-hardcodage des scénarios de financement ───────────
#
# Mapping clé BD (groupe ``scenarios_financement``) → (slug interne,
# attribut). Les 12 clés couvrent les 4 scénarios × LTV / amortissement
# / RCD. LTV et RCD sont stockés en BD en décimal (0.75, 1.20), comme
# dans ``lead_analysis_finance.SCENARIO_*`` — aucune conversion. Amort
# est un entier (années). Les valeurs écrasent les dataclasses
# ``SCENARIO_*`` poste par poste ; champ absent → fallback hardcoded.
_DB_KEY_TO_SCENARIO: dict[str, tuple[str, str]] = {
    "scenario_achat_ltv": ("achat", "ltv"),
    "scenario_achat_amort": ("achat", "amort"),
    "scenario_achat_rcd": ("achat", "rcd"),
    "scenario_schl_std_ltv": ("schl_std", "ltv"),
    "scenario_schl_std_amort": ("schl_std", "amort"),
    "scenario_schl_std_rcd": ("schl_std", "rcd"),
    "scenario_aph50_ltv": ("aph50", "ltv"),
    "scenario_aph50_amort": ("aph50", "amort"),
    "scenario_aph50_rcd": ("aph50", "rcd"),
    "scenario_aph100_ltv": ("aph100", "ltv"),
    "scenario_aph100_amort": ("aph100", "amort"),
    "scenario_aph100_rcd": ("aph100", "rcd"),
}


async def _load_scenario_overrides(db) -> dict:
    """Charge les overrides des scénarios de financement (groupe
    ``scenarios_financement``).

    Retourne un dict imbriqué ``{ slug: {"ltv":.., "amort":..,
    "rcd":..} }`` prêt à passer à ``FinanceInputs.scenario_overrides``.
    Les clés/attributs absents en BD → le moteur retombe sur les
    constantes hardcoded ``SCENARIO_*``. Dict vide si table absente.
    """
    overrides: dict[str, dict[str, float]] = {}
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        for row in rows:
            if row.value_float is None:
                continue
            mapped = _DB_KEY_TO_SCENARIO.get(row.key)
            if mapped is None:
                continue
            slug, attr = mapped
            overrides.setdefault(slug, {})[attr] = float(row.value_float)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load scenario overrides from DB: %s", exc)
    return overrides


# ── Juin 2026 : Dé-hardcodage des barèmes fiscaux ────────────────────


def _parse_taxes_bienvenue_brackets(value_json) -> Optional[list]:
    """Convertit le ``value_json`` de la clé ``taxes_bienvenue_mtl`` en
    liste de tuples ``(seuil_haut, taux_fraction)`` attendue par le
    moteur.

    Format BD attendu : ``[{"seuil": <float|null>, "taux_pct": <float>},
    ...]`` trié par seuil croissant, le dernier palier ayant ``seuil``
    null (palier ouvert → ``inf``). ``taux_pct`` est en pourcentage
    (0.5 = 0.5 %), converti en fraction (÷100). Retourne ``None`` si le
    JSON est absent ou invalide → le moteur retombe sur le barème
    hardcoded ``TAXES_BIENVENUE_MTL_BRACKETS``.
    """
    if not value_json:
        return None
    try:
        brackets: list = []
        for tier in value_json:
            raw_seuil = tier.get("seuil")
            seuil = float("inf") if raw_seuil is None else float(raw_seuil)
            taux = float(tier["taux_pct"]) / 100.0
            brackets.append((seuil, taux))
        return brackets or None
    except Exception as exc:  # noqa: BLE001
        log.warning("Invalid taxes_bienvenue_mtl value_json: %s", exc)
        return None


async def _load_fiscal_overrides(db) -> tuple[Optional[float], Optional[list]]:
    """Charge les overrides des barèmes fiscaux (groupe
    ``baremes_fiscaux``).

    Retourne ``(ratio_abordabilite_aph, taxes_bienvenue_brackets)`` :
    - ``ratio_abordabilite_aph`` (clé ``ratio_abordabilite_aph``) :
      stocké en décimal (0.40), passé tel quel. ``None`` si absent.
    - ``taxes_bienvenue_brackets`` (clé ``taxes_bienvenue_mtl``,
      ``value_json``) : liste de tuples ``(seuil, taux_fraction)``.
      ``None`` si absent/invalide.

    Champs absents → le moteur retombe sur ``RATIO_ABORDABILITE_APH`` /
    ``TAXES_BIENVENUE_MTL_BRACKETS`` hardcoded.
    """
    ratio: Optional[float] = None
    taxes_brackets: Optional[list] = None
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        for row in rows:
            if row.key == "ratio_abordabilite_aph":
                if row.value_float is not None:
                    ratio = float(row.value_float)
            elif row.key == "taxes_bienvenue_mtl":
                taxes_brackets = _parse_taxes_bienvenue_brackets(
                    row.value_json
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load fiscal overrides from DB: %s", exc)
    return ratio, taxes_brackets


async def _load_defaults_for_new_analysis(db) -> dict:
    """Charge les défauts depuis la BD et fusionne avec les fallbacks.

    Les valeurs ``ProspectionAnalysisDefault.value_float`` sont stockées
    en pourcentage (3.75, 25.0, 8.0) pour s'aligner sur la convention
    ``LeadAnalysis.*_pct``. Si la table est vide ou manque une clé, on
    retombe sur les défauts en dur (``_DEFAULTS_FALLBACK``).

    Retourne un dict prêt à passer en ``**kwargs`` au constructeur
    ``LeadAnalysis(...)``. Couvre TOUS les inputs manuels pré-remplis
    (taux refi, MDF %, taux prêteur B, TGA, taux achat, réduction
    énergie, durée projet, nb logements/thermopompes ajoutés).

    Mai 2026 : pré-remplit aussi ``frais_demarrage_financables_json``
    selon les flags ``financable_par_defaut`` configurés en BD pour les
    items MDF. Si aucun item n'a de flag (table vierge), retombe sur
    la liste héritée hardcoded ([rapport_efficacite, frais_developpement,
    frais_travaux]) pour préserver le comportement historique.
    """
    out = dict(_DEFAULTS_FALLBACK)
    financables_from_db: list[str] = []
    any_financable_flag_set = False
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        for row in rows:
            # Champs ``LeadAnalysis`` pré-remplis.
            field = _DB_KEY_TO_FIELD.get(row.key)
            if field is not None and row.value_float is not None:
                v = float(row.value_float)
                out[field] = int(v) if field in _DB_KEY_INT_FIELDS else v

            # Flag « finançable par défaut » sur les items MDF.
            financable_key = _DB_KEY_TO_FINANCABLE_KEY.get(row.key)
            if financable_key is not None:
                flag = getattr(row, "financable_par_defaut", None)
                if flag is not None:
                    any_financable_flag_set = True
                    if bool(flag):
                        financables_from_db.append(financable_key)

            # Frais PERSONNALISÉS (clé ``frais_mdf_custom``) : les ids des
            # postes finançables par défaut rejoignent le set initial de
            # la fiche, exactement comme les postes fixes.
            if row.key == "frais_mdf_custom" and isinstance(
                row.value_json, list
            ):
                for _it in row.value_json:
                    if isinstance(_it, dict) and _it.get(
                        "financable_par_defaut"
                    ):
                        _cid = str(_it.get("id", "")).strip()
                        if _cid:
                            any_financable_flag_set = True
                            financables_from_db.append(_cid)
    except Exception as exc:  # noqa: BLE001
        # Table peut ne pas exister au tout premier boot — silencieux.
        log.warning("Failed to load analysis defaults from DB: %s", exc)

    # Si au moins un flag est configuré en BD, on respecte EXCLUSIVEMENT
    # la BD. Sinon, on retombe sur le défaut hardcoded (compat héritée
    # avec ``frais_developpement`` / ``frais_travaux`` qui n'ont pas de
    # ligne dans ``prospection_analysis_defaults``).
    if any_financable_flag_set:
        # On garde frais_developpement et frais_travaux toujours
        # finançables (Phil les a marqués finançables historiquement
        # et il n'y a pas de défaut BD associé pour eux).
        merged = list(dict.fromkeys(
            financables_from_db + ["frais_developpement", "frais_travaux"]
        ))
        out["frais_demarrage_financables_json"] = json.dumps(merged)
    return out


async def _load_frais_mdf_overrides(db) -> tuple[dict, dict, Optional[float]]:
    """Charge les défauts globaux des frais MDF depuis la BD.

    Retourne un tuple ``(frais_fixes_overrides, pct_courtiers_overrides,
    frais_dossier_preteur_pct)`` prêt à passer à ``FinanceInputs``. Les
    valeurs en BD sont :
        - frais_* : montants $ directs (ex. 1500.0)
        - pct_courtier_* : pourcentage (1.0 = 1 %), converti en
          fraction (÷100) pour matcher ``PCT_COURTIERS``.
        - frais_dossier_preteur_pct : pct (2.0 = 2 %) en BD → fraction
          (0.02) pour le moteur.

    Si la table est vide ou manque, retourne `({}, {}, None)` — le
    moteur retombera sur les constantes hardcoded ``FRAIS_FIXES`` /
    ``PCT_COURTIERS`` / ``DEFAULT_FRAIS_DOSSIER_PRETEUR_PCT``.
    """
    frais_fixes: dict[str, float] = {}
    pct_courtiers: dict[str, float] = {}
    frais_dossier_preteur_pct: Optional[float] = None
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        for row in rows:
            if row.value_float is None:
                continue
            v = float(row.value_float)
            if row.key in _DB_KEY_TO_FRAIS_FIXE:
                frais_fixes[_DB_KEY_TO_FRAIS_FIXE[row.key]] = v
            elif row.key in _DB_KEY_TO_PCT_COURTIER:
                # BD stocke en pct (1.0 = 1 %), moteur attend une
                # fraction (0.01 = 1 %).
                pct_courtiers[_DB_KEY_TO_PCT_COURTIER[row.key]] = v / 100.0
            elif row.key == "frais_dossier_preteur_pct":
                # Idem : BD en pct (2.0 = 2 %), moteur en fraction.
                frais_dossier_preteur_pct = v / 100.0
            elif row.key == "frais_dossier_trad":
                # Institution traditionnelle : montant $ FIXE (défaut
                # 5 000 $) — lu par le moteur dans ce même dict.
                frais_fixes["frais_dossier_trad"] = v
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to load frais MDF overrides from DB: %s", exc
        )
    return frais_fixes, pct_courtiers, frais_dossier_preteur_pct


# Types de montant valides pour un poste de frais personnalisé.
_FRAIS_CUSTOM_TYPES: set[str] = {
    "fixe",
    "pct_prix_achat",
    "pct_financement",
}


def _sanitize_frais_custom_defs(value_json) -> list[dict]:
    """Normalise/valide la liste des frais de démarrage personnalisés
    lue depuis le ``value_json`` de la clé ``frais_mdf_custom``.

    Retourne une liste d'items propres ``{id, label_fr, type_montant,
    valeur, financable_par_defaut}``. Les items invalides (pas un dict,
    type_montant inconnu, valeur non numérique) sont ignorés
    silencieusement. ``None`` / liste vide → ``[]`` (aucun poste → calcul
    inchangé)."""
    if not value_json or not isinstance(value_json, list):
        return []
    out: list[dict] = []
    for item in value_json:
        if not isinstance(item, dict):
            continue
        type_montant = item.get("type_montant", "fixe")
        if type_montant not in _FRAIS_CUSTOM_TYPES:
            continue
        try:
            valeur = float(item.get("valeur", 0) or 0)
        except (TypeError, ValueError):
            continue
        item_id = str(item.get("id", "") or "").strip()
        if not item_id:
            continue
        out.append(
            {
                "id": item_id,
                "label_fr": str(item.get("label_fr", "") or ""),
                "type_montant": type_montant,
                "valeur": valeur,
                "financable_par_defaut": bool(
                    item.get("financable_par_defaut", False)
                ),
            }
        )
    return out


async def _load_frais_custom_defs(db) -> list[dict]:
    """Charge la LISTE des frais de démarrage personnalisés (clé
    ``frais_mdf_custom``, groupe ``mdf_frais``, ``value_json``).

    Retourne une liste d'items ``{id, label_fr, type_montant, valeur,
    financable_par_defaut}`` prête à passer à
    ``FinanceInputs.frais_custom_defs``. Liste vide si la clé est
    absente / vide → le moteur n'ajoute aucun poste personnalisé
    (résultat identique à avant)."""
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        for row in rows:
            if row.key == "frais_mdf_custom":
                return _sanitize_frais_custom_defs(row.value_json)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load frais_mdf_custom from DB: %s", exc)
    return []


# Clé BD du registre unifié des frais de démarrage (ordre / label /
# visibilité). Cf. ``prospection_analysis_defaults`` (groupe ``mdf_frais``).
_FRAIS_REGISTRY_KEY = "mdf_frais_registry"

# Postes FIXES ajoutés APRÈS la création du registre : toujours
# appendus (visibles) s'ils manquent — sinon la fiche et le PDF les
# masquaient par oubli (retour Phil 2026-09-04 : « je ne vois toujours
# pas la ligne des intérêts de la balance de vente »).
_POSTES_PERMANENTS: tuple[tuple[str, str], ...] = (
    ("interets_balance_vente", "Intérêts balance de vente"),
    ("detention", "Détention (institution traditionnelle)"),
)


def _sanitize_registry(value_json) -> list[dict]:
    """Normalise la liste ORDONNÉE du registre ``mdf_frais_registry`` en
    ``[{key, label_fr, visible}]``. Robustesse : value_json mal formé →
    liste vide ; jamais d'exception. Une clé vide / dupliquée est
    ignorée."""
    if not isinstance(value_json, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for it in value_json:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key", "") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "key": key,
                "label_fr": str(it.get("label_fr", "") or ""),
                "visible": bool(it.get("visible", True)),
            }
        )
    return out


async def _load_frais_registry(db) -> tuple[list[str], list[dict]]:
    """Charge le registre unifié des frais de démarrage (clé
    ``mdf_frais_registry``, groupe ``mdf_frais``, ``value_json``).

    Retourne ``(frais_masques, registry_ordered)`` :
      - ``frais_masques`` : liste des ``key`` dont ``visible == false``
        (postes masqués) → passée à ``FinanceInputs.frais_masques`` pour
        forcer ces postes à 0 $ dans le calcul.
      - ``registry_ordered`` : liste ORDONNÉE ``[{key, label_fr,
        visible}]`` du registre — exposée dans ``analysis_results_json``
        (``frais_registry``) pour que la fiche + le PDF affichent dans le
        bon ordre / avec les bons labels sans re-fetcher.

    Registre absent / mal formé → ``([], [])`` : AUCUN masquage et aucun
    ordre imposé (le calcul reste STRICTEMENT identique à avant)."""
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        registry: list[dict] = []
        custom_items: list = []
        for row in rows:
            if row.key == _FRAIS_REGISTRY_KEY:
                registry = _sanitize_registry(row.value_json)
            elif row.key == "frais_mdf_custom" and isinstance(
                row.value_json, list
            ):
                custom_items = row.value_json
        if not registry:
            # Pas de registre en BD → fallback (comportement d'avant).
            return [], []
        # Postes PERSONNALISÉS pas encore référencés dans le registre
        # (ex. créés AVANT l'introduction du registre) : on les append à
        # la fin (visibles) pour qu'ils apparaissent dans la fiche + le
        # PDF, comme le fait le GET /mdf-registry pour l'UI Paramètres.
        known = {e["key"] for e in registry}
        for it in custom_items:
            if not isinstance(it, dict):
                continue
            cid = str(it.get("id", "") or "").strip()
            if cid and cid not in known:
                registry.append(
                    {
                        "key": cid,
                        "label_fr": str(it.get("label_fr", "") or ""),
                        "visible": True,
                    }
                )
                known.add(cid)
        for _pk, _plbl in _POSTES_PERMANENTS:
            if _pk not in known:
                registry.append(
                    {"key": _pk, "label_fr": _plbl, "visible": True}
                )
                known.add(_pk)
        masques = [e["key"] for e in registry if not e["visible"]]
        return masques, registry
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load mdf_frais_registry from DB: %s", exc)
    return [], []


@router.post(
    "/extract",
    response_model=ExtractResult,
    status_code=status.HTTP_201_CREATED,
    summary="Extraire et créer une ou plusieurs fiches depuis sources.",
)
async def extract_and_create(
    db: DBSession,
    user: CurrentUser,
    urls: Optional[str] = Form(default=None),
    text: Optional[str] = Form(default=None),
    files: List[UploadFile] = File(default=[]),
) -> ExtractResult:
    """Reçoit un mix d'URLs (séparées par newline), de texte brut et
    de fichiers. Lance l'extraction Claude. Crée une `LeadAnalysis`
    par immeuble distinct détecté (en pratique presque toujours 1)
    + une `LeadAnalysisAttachment` par fichier."""
    _require_prospection(user)

    url_list = [u.strip() for u in (urls or "").splitlines() if u.strip()]

    # Lit les fichiers en bytes (limite 10 MB chacun).
    file_blobs: list[tuple[str, str, bytes]] = []
    for f in files or []:
        if not f or not f.filename:
            continue
        data = await f.read()
        if len(data) > _MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Fichier {f.filename} trop lourd (max 10 MB).",
            )
        file_blobs.append(
            (
                f.filename,
                (f.content_type or "application/octet-stream").lower(),
                data,
            )
        )

    if not url_list and not (text and text.strip()) and not file_blobs:
        raise HTTPException(
            status_code=400, detail="Aucune source fournie."
        )

    try:
        res = await extract_lead_info(
            urls=url_list, text=text, files=file_blobs
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Extraction Claude failed")
        raise HTTPException(
            status_code=502, detail=f"Échec de l'extraction IA : {exc!s}"
        ) from exc

    created_records: list[LeadAnalysis] = []
    # On stocke l'URL et le texte d'origine sur la 1re fiche créée
    # (la fusion intelligente côté Claude regroupe normalement).
    src_url_str = "\n".join(url_list) if url_list else None
    src_text = (text or "").strip() or None

    now = datetime.now(timezone.utc)
    warnings_notes: Optional[str] = None
    if res.warnings:
        wlines = ["Diagnostic de l'extraction :"]
        wlines.extend(f"• {w}" for w in res.warnings)
        warnings_notes = "\n".join(wlines)
    # Charge les défauts globaux depuis la BD (modifiables admin/owner
    # via /api/v1/prospection/analysis-defaults). Une seule lecture
    # pour tous les leads créés dans ce batch d'extraction.
    defaults = await _load_defaults_for_new_analysis(db)
    for idx, item in enumerate(res.data or []):
        kwargs = _map_extracted_to_lead(item)
        # Applique les défauts pour les champs manuels d'analyse
        # avant les valeurs extraites (les extraites ne touchent
        # jamais ces champs de toute façon).
        kwargs = {**defaults, **kwargs}
        # Si warnings et qu'on n'a pas déjà de notes (ie. l'extraction
        # n'a pas mis quelque chose dans `notes` via _map_extracted_to_lead),
        # on stocke les warnings comme notes pour visibilité.
        if warnings_notes and idx == 0 and "notes" not in kwargs:
            kwargs["notes"] = warnings_notes
        rec = LeadAnalysis(
            status=LeadAnalysisStatus.A_ANALYSER.value,
            source_urls=src_url_str if idx == 0 else None,
            source_text=src_text if idx == 0 else None,
            extracted_json=json.dumps(item, ensure_ascii=False)[:50_000],
            model_used=res.model_used,
            created_by_user_id=getattr(user, "id", None),
            **kwargs,
        )
        rec.created_at = now
        rec.updated_at = now
        db.add(rec)
        await db.flush()

        # Phase A3 — Validation post-extraction. Combine les bornes
        # numériques avec les valeurs par couche (local/gemini) pour
        # détecter les divergences. Stocké en JSONB sur la fiche.
        per_src = None
        if res.per_source_values and idx < len(res.per_source_values):
            per_src = res.per_source_values[idx]
        vw = validate_extraction(rec, per_source_values=per_src)
        rec.validation_warnings = vw or None
        if vw:
            # Audit log : on garde une trace quand des anomalies sont
            # détectées (utile pour stats / dashboard santé extraction).
            try:
                await log_action(
                    db,
                    user=user,
                    action="lead_analysis.validation_warnings_updated",
                    entity_type="lead_analysis",
                    entity_id=rec.id,
                    details={
                        "source": "extract",
                        "count": len(vw),
                        "max_severity": summarize_severity(vw),
                        "fields": sorted({w.get("field") for w in vw if w.get("field")}),
                    },
                )
            except Exception:  # noqa: BLE001 — l'audit ne doit pas planter l'extract
                log.exception("Audit log validation_warnings failed")

        # Attache les fichiers seulement sur la 1re fiche (sinon on
        # duplique du gros blob inutilement).
        if idx == 0:
            for filename, content_type, blob in file_blobs:
                att = LeadAnalysisAttachment(
                    lead_analysis_id=rec.id,
                    filename=filename[:255],
                    content_type=content_type[:64],
                    size_bytes=len(blob),
                    blob=blob,
                )
                db.add(att)

        created_records.append(rec)

    # Cas dégénéré : l'extraction n'a rien retourné mais il y avait
    # des sources. On crée quand même une fiche vide pour ne pas
    # perdre l'effort de l'utilisateur (il pourra remplir à la main).
    # On INJECTE les warnings dans `notes` pour que l'utilisateur voie
    # le diagnostic directement dans la fiche (pas juste un panel
    # ambre éphémère qui disparaît quand il ouvre le modal).
    if not created_records and (url_list or src_text or file_blobs):
        warning_lines = []
        if res.warnings:
            warning_lines.append("Diagnostic de l'extraction :")
            warning_lines.extend(f"• {w}" for w in res.warnings)
        else:
            warning_lines.append(
                "Extraction n'a retourné aucun champ — aucun warning détaillé."
            )
        # Statut Tesseract pour diagnostic en cas d'image
        if any("image" in (ct or "").lower() for _, ct, _ in file_blobs):
            from app.services.lead_extraction import _check_tesseract_status
            warning_lines.append("")
            warning_lines.append(f"État serveur OCR : {_check_tesseract_status()}")
        warning_lines.append("")
        warning_lines.append("→ À compléter manuellement.")
        notes_text = "\n".join(warning_lines)

        rec = LeadAnalysis(
            status=LeadAnalysisStatus.A_ANALYSER.value,
            source_urls=src_url_str,
            source_text=src_text,
            extracted_json=None,
            model_used=res.model_used,
            created_by_user_id=getattr(user, "id", None),
            **defaults,
            notes=notes_text,
        )
        rec.created_at = now
        rec.updated_at = now
        db.add(rec)
        await db.flush()
        for filename, content_type, blob in file_blobs:
            att = LeadAnalysisAttachment(
                lead_analysis_id=rec.id,
                filename=filename[:255],
                content_type=content_type[:64],
                size_bytes=len(blob),
                blob=blob,
            )
            db.add(att)
        created_records.append(rec)

    await db.commit()
    for rec in created_records:
        await db.refresh(rec)

    out_items: List[LeadAnalysisListItem] = []
    for rec in created_records:
        cnt = (
            await db.execute(
                select(func.count(LeadAnalysisAttachment.id)).where(
                    LeadAnalysisAttachment.lead_analysis_id == rec.id
                )
            )
        ).scalar_one()
        out_items.append(_to_list_item(rec, int(cnt or 0)))

    return ExtractResult(
        created=out_items,
        warnings=res.warnings,
        model_used=res.model_used,
    )


@router.get(
    "",
    response_model=List[LeadAnalysisListItem],
    summary="Liste des analyses (kanban / tableau).",
)
async def list_analyses(
    db: DBSession,
    user: CurrentUser,
    status_filter: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
) -> List[LeadAnalysisListItem]:
    _require_prospection(user)

    stmt = select(LeadAnalysis).order_by(
        LeadAnalysis.status.asc(),
        LeadAnalysis.position.asc(),
        LeadAnalysis.created_at.desc(),
    )
    if status_filter:
        stmt = stmt.where(LeadAnalysis.status == status_filter)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            (LeadAnalysis.address.ilike(like))
            | (LeadAnalysis.city.ilike(like))
            | (LeadAnalysis.description.ilike(like))
        )
    stmt = stmt.limit(min(max(limit, 1), 500))
    rows = (await db.execute(stmt)).scalars().all()

    # Compte les attachments en 1 query batch (LEFT JOIN count).
    counts_stmt = (
        select(
            LeadAnalysisAttachment.lead_analysis_id,
            func.count(LeadAnalysisAttachment.id),
        )
        .where(
            LeadAnalysisAttachment.lead_analysis_id.in_([r.id for r in rows])
            if rows
            else False
        )
        .group_by(LeadAnalysisAttachment.lead_analysis_id)
    )
    cnt_rows = (await db.execute(counts_stmt)).all() if rows else []
    counts = {lid: int(c) for lid, c in cnt_rows}

    return [_to_list_item(r, counts.get(r.id, 0)) for r in rows]


@router.get(
    "/{analysis_id}",
    response_model=LeadAnalysisRead,
    summary="Détail d'une analyse (fiche complète).",
)
async def get_analysis(
    analysis_id: int, db: DBSession, user: CurrentUser
) -> LeadAnalysisRead:
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")
    atts = (
        await db.execute(
            select(LeadAnalysisAttachment)
            .where(LeadAnalysisAttachment.lead_analysis_id == analysis_id)
            .order_by(LeadAnalysisAttachment.id.asc())
        )
    ).scalars().all()

    out = LeadAnalysisRead.model_validate(rec)
    out.attachments = [AttachmentRead.model_validate(a) for a in atts]
    return out


@router.patch(
    "/{analysis_id}",
    response_model=LeadAnalysisRead,
    summary="Mise à jour partielle (édition fiche).",
)
async def update_analysis(
    analysis_id: int,
    payload: LeadAnalysisUpdate,
    db: DBSession,
    user: CurrentUser,
) -> LeadAnalysisRead:
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")
    touched_validated_field = False
    validated_fields = {
        "asking_price", "nb_logements", "revenus_bruts",
        "taxes_municipales", "taxes_scolaires", "assurances",
        "energie",
    }
    _ancienne_strategie = rec.strategie_acquisition
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(rec, k, v)
        if k in validated_fields:
            touched_validated_field = True
    # Changement de stratégie (retour Phil 2026-09-04) : les coches
    # « finançable » ne se transportent pas d'un mode à l'autre — rien
    # de coché en institution traditionnelle, défauts en prêteur B.
    if (
        "strategie_acquisition" in payload.model_fields_set
        and rec.strategie_acquisition != _ancienne_strategie
    ):
        rec.frais_demarrage_financables_json = None
    rec.updated_at = datetime.now(timezone.utc)

    # Phase A3 — si l'utilisateur a édité un champ borné, on
    # re-valide la fiche pour synchroniser les warnings (on garde
    # le `source_*` existant — la révalidation ne connaît plus
    # les valeurs des couches d'origine, mais les bornes restent
    # cohérentes).
    if touched_validated_field:
        # On préserve les sources_* connues : on reconstruit per_src
        # à partir des warnings actuels (utile pour ne pas perdre la
        # mémoire local/gemini en cas de tooltip).
        old_vw = rec.validation_warnings or []
        per_src: Dict[str, Dict[str, Any]] = {}
        for w in old_vw:
            f = w.get("field")
            if not f:
                continue
            sub: Dict[str, Any] = {}
            if w.get("source_local") is not None:
                sub["local"] = w["source_local"]
            if w.get("source_gemini") is not None:
                sub["gemini"] = w["source_gemini"]
            if w.get("source_claude") is not None:
                sub["claude"] = w["source_claude"]
            if sub:
                per_src[f] = sub
        new_vw = validate_extraction(rec, per_source_values=per_src)
        rec.validation_warnings = new_vw or None

    # Recalcul auto : si un intrant du calcul a changé ET que l'analyse a
    # déjà été calculée une fois, on relance le moteur pour garder les
    # résultats (MDF, prêt prêteur B, scénarios) synchronisés. Sans ça,
    # décocher un poste « finançable prêteur B » sur une analyse déjà
    # faite ne mettait rien à jour.
    patched = set(payload.model_dump(exclude_unset=True).keys())
    if (patched & RECALC_INPUT_FIELDS) and rec.analysis_results_json:
        try:
            await _compute_and_store(rec, db)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "recalcul auto après patch échoué (analyse %s): %s",
                analysis_id, exc,
            )

    await db.commit()
    await db.refresh(rec)
    return await get_analysis(analysis_id, db, user)


@router.delete(
    "/{analysis_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_analysis(
    analysis_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        return None
    await db.delete(rec)
    await db.commit()


@router.post(
    "/{analysis_id}/convert-to-lead",
    response_model=ConvertResult,
    summary="Convertit l'analyse en ProspectionLead (pipeline officiel).",
)
async def convert_to_lead(
    analysis_id: int, db: DBSession, user: CurrentUser
) -> ConvertResult:
    """Crée un `ProspectionLead` à partir de la fiche d'analyse.
    Idempotent : si déjà converti, retourne l'id existant."""
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")
    if rec.converted_to_lead_id:
        return ConvertResult(lead_id=rec.converted_to_lead_id)

    lead = ProspectionLead(
        name=rec.address or f"Lead #{rec.id}",
        kind=ProspectionLeadKind.MULTILOGEMENT.value,
        status=ProspectionLeadStatus.A_CONTACTER.value,
        address=rec.address,
        city=rec.city,
        postal_code=rec.postal_code,
        notes=(
            f"Créé depuis l'analyse #{rec.id}\n"
            + (rec.description or "")
        )[:5000] or None,
        nb_logements=rec.nb_logements,
        annee_construction=rec.annee_construction,
        valeur_fonciere=(
            float(rec.evaluation_municipale)
            if rec.evaluation_municipale is not None
            else None
        ),
        owner_kind=ProspectionOwnerKind.INCONNU.value,
        created_by_user_id=getattr(user, "id", None),
    )
    db.add(lead)
    await db.flush()
    rec.converted_to_lead_id = lead.id
    rec.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return ConvertResult(lead_id=lead.id)


@router.post(
    "/{analysis_id}/convert-to-deal",
    response_model=ConvertDealResult,
    summary="Convertit l'analyse en ProspectionDeal (Pipeline).",
)
async def convert_to_deal(
    analysis_id: int, db: DBSession, user: CurrentUser
) -> ConvertDealResult:
    """Crée un `ProspectionDeal` à partir de la fiche d'analyse.
    Idempotent : si déjà converti, retourne l'id existant.

    Le deal créé est lié à la fiche d'analyse via `lead_analysis_id`
    pour que la page detail du deal puisse afficher la fiche
    complète."""
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")
    if rec.converted_to_deal_id:
        return ConvertDealResult(deal_id=rec.converted_to_deal_id)

    deal = ProspectionDeal(
        address=rec.address or f"Deal #{rec.id}",
        priority="moyenne",
        lead_analysis_id=rec.id,
    )
    db.add(deal)
    await db.flush()
    rec.converted_to_deal_id = deal.id
    rec.updated_at = datetime.now(timezone.utc)
    await db.commit()

    # Phase 5 — hook Drive Conventions. Best-effort : si Drive est down
    # ou si le user n'a pas connecté Drive, le deal reste créé.
    try:
        from app.services.drive_conventions_hooks import on_entity_created

        await on_entity_created(
            entity_type="ProspectionDeal",
            entity_id=deal.id,
            user_id=user.id,
            db=db,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "drive hook 'created' a echoue pour ProspectionDeal #%s "
            "(non bloquant)",
            deal.id,
        )

    return ConvertDealResult(deal_id=deal.id)


@router.get(
    "/{analysis_id}/attachments/{attachment_id}",
    summary="Sert un fichier joint en inline.",
)
async def get_attachment(
    analysis_id: int,
    attachment_id: int,
    db: DBSession,
    user: CurrentUser,
):
    _require_prospection(user)
    att = await db.get(LeadAnalysisAttachment, attachment_id)
    if att is None or att.lead_analysis_id != analysis_id:
        raise HTTPException(404, "Fichier introuvable.")
    return Response(
        content=att.blob,
        media_type=att.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                f'inline; filename="{att.filename}"'
            )
        },
    )


@router.get(
    "/{analysis_id}/pdf",
    summary="Génère le PDF complet d'une fiche d'analyse (export).",
)
async def export_pdf(
    analysis_id: int, db: DBSession, user: CurrentUser
):
    """Génère à la volée le PDF complet de la fiche d'analyse —
    identité, financier, typologie, inputs manuels, frais de démarrage,
    4 scénarios, meilleur refi (RCI/PVI), validation, sources/attachments.

    L'export reflète toujours l'état courant — aucune persistance.
    Audit log `lead_analysis.pdf_exported`.
    """
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")

    from app.services.lead_analysis_pdf import (
        generate_lead_analysis_pdf,
        lead_analysis_pdf_filename,
    )

    try:
        pdf_bytes = await generate_lead_analysis_pdf(db, analysis_id)
    except ValueError as exc:
        log.exception("Génération PDF fiche %s échouée", analysis_id)
        raise HTTPException(502, f"Génération PDF échouée : {exc}") from exc

    filename = lead_analysis_pdf_filename(rec)

    try:
        await log_action(
            db,
            user=user,
            action="lead_analysis.pdf_exported",
            entity_type="lead_analysis",
            entity_id=rec.id,
            details={
                "filename": filename,
                "size_bytes": len(pdf_bytes),
                "has_results": bool(rec.analysis_results_json),
            },
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("Audit log lead_analysis.pdf_exported échoué")

    # Phase 6 — auto-classement Drive (best-effort, NON bloquant). Si la
    # fiche est convertie en deal et qu'une règle est active, dépose le
    # PDF dans le dossier Drive du deal. N'altère jamais la réponse.
    try:
        from app.services.drive_auto_upload_dispatcher import (
            dispatch_auto_upload,
        )

        await dispatch_auto_upload(
            "fiche_analyse",
            "ProspectionDeal",
            rec.converted_to_deal_id,
            user.id,
            pdf_bytes,
            db,
            {"filename": filename},
            mime_type="application/pdf",
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("Auto-upload Drive fiche d'analyse non bloquant")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Content-Length": str(len(pdf_bytes)),
        },
    )


# ── Offre d'investissement PPTX (Phase Offre 2026) ─────────────────


class OffreInvestissementPhotoIn(BaseModel):
    """Photo brute pour l'offre (base64 ou ID d'attachment existant)."""
    model_config = ConfigDict(extra="forbid")

    base64_data: Optional[str] = Field(
        default=None,
        description=(
            "Photo encodée base64 (sans préfixe data:image/...). "
            "Alternative : `attachment_id`."
        ),
    )
    attachment_id: Optional[int] = Field(
        default=None,
        description=(
            "Si fourni, on utilise le blob d'un `LeadAnalysisAttachment` "
            "déjà uploadé sur cette fiche."
        ),
    )


class OffreInvestissementRequest(BaseModel):
    """Inputs du wizard frontend."""
    model_config = ConfigDict(extra="forbid")

    value_add_strategy: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Inputs humains : tagline, bullets, flags value-add, programme "
            "SCHL, etc. Schéma libre — interprété par "
            "`offre_investissement_pptx.ValueAddStrategy.from_dict`."
        ),
    )
    photos: Optional[List[OffreInvestissementPhotoIn]] = Field(
        default=None,
        description=(
            "Liste ordonnée des photos (cover, exterieur, carte). MVP : "
            "3 photos max. Si vide, les photos par défaut du template sont "
            "conservées."
        ),
    )


@router.post(
    "/{analysis_id}/offre-investissement",
    summary=(
        "Génère un .pptx d'offre d'investissement Horizon "
        "(template horizon_v1)."
    ),
)
async def export_offre_investissement(
    analysis_id: int,
    body: OffreInvestissementRequest,
    db: DBSession,
    user: CurrentUser,
):
    """Génère à la volée le `.pptx` d'offre d'investissement pour la fiche.

    Combine :
      * Variables auto (~30 champs depuis la `LeadAnalysis`)
      * Variables hybrides (résultats du moteur d'analyse financière le
        plus récent)
      * Inputs humains du wizard (tagline, bullets, flags value-add)
      * Jusqu'à 3 photos (uploadées ou choisies parmi les attachments)

    Aucune persistance. Audit log : `lead_analysis.offre_investissement_generated`.
    """
    import base64 as _b64

    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")

    from app.services.offre_investissement_pptx import (
        generate_offre_investissement_pptx,
        offre_investissement_pptx_filename,
    )

    # Resolve photos
    photo_bytes: list[bytes] = []
    photo_attachment_ids: list[int] = []
    if body.photos:
        for p in body.photos[:3]:  # MVP : max 3 photos
            if p.base64_data:
                try:
                    photo_bytes.append(_b64.b64decode(p.base64_data))
                except Exception as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Photo base64 invalide : {exc}",
                    ) from exc
            elif p.attachment_id is not None:
                photo_attachment_ids.append(p.attachment_id)

    try:
        pptx_bytes, template_version = await generate_offre_investissement_pptx(
            db=db,
            analysis_id=analysis_id,
            value_add_strategy=body.value_add_strategy,
            photos=photo_bytes if photo_bytes else None,
            photo_attachment_ids=(
                photo_attachment_ids if photo_attachment_ids else None
            ),
        )
    except ValueError as exc:
        log.exception(
            "Génération offre PPTX fiche %s échouée", analysis_id
        )
        raise HTTPException(502, f"Génération PPTX échouée : {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "Erreur inattendue lors de la génération de l'offre PPTX %s",
            analysis_id,
        )
        raise HTTPException(
            500, f"Erreur inattendue : {exc}"
        ) from exc

    filename = offre_investissement_pptx_filename(rec)

    try:
        await log_action(
            db,
            user=user,
            action="lead_analysis.offre_investissement_generated",
            entity_type="lead_analysis",
            entity_id=rec.id,
            details={
                "filename": filename,
                "size_bytes": len(pptx_bytes),
                "template_version": template_version,
                # Version logique du service de génération. Bump à
                # chaque PR qui change la sémantique de substitution
                # (charts, dates auto-calculées, nouveaux champs
                # wizard, etc.). v3 = corrections slide-par-slide
                # (présentation projet, charts dynamiques slides 4/12/13,
                # dates échéancier auto, totaux rénos depuis fiche,
                # titre/chart Tendances dynamiques, estimation ROI).
                # v4 = perfectionnements slides 3-6 (PR #544).
                # v5a = fixes slides 8/9/10 : nb logements dynamique,
                # frais autres réels (overrides + total recalculé),
                # condensation tableau rénos, format équité unifié et
                # cohérence cellule/callout en équité négative.
                "service_version": "v5a",
                "value_add_keys": sorted(
                    body.value_add_strategy.keys()
                )
                if body.value_add_strategy
                else [],
                "n_photos": len(photo_bytes) + len(photo_attachment_ids),
            },
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception(
            "Audit log lead_analysis.offre_investissement_generated échoué"
        )

    # Phase 6 — auto-classement Drive (best-effort, NON bloquant). Dépose
    # l'offre PPTX dans le sous-dossier « Dossier investisseur » du deal
    # lié, si une règle est active. N'altère jamais la réponse.
    try:
        from app.services.drive_auto_upload_dispatcher import (
            dispatch_auto_upload,
        )

        await dispatch_auto_upload(
            "offre_pptx",
            "ProspectionDeal",
            rec.converted_to_deal_id,
            user.id,
            pptx_bytes,
            db,
            {"filename": filename},
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("Auto-upload Drive offre PPTX non bloquant")

    return Response(
        content=pptx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Content-Length": str(len(pptx_bytes)),
            "X-Template-Version": template_version,
        },
    )


@router.get(
    "/offre-investissement/catalogue-renovations",
    summary=(
        "Retourne le catalogue de rénovations cochables pour la slide 9 "
        "du template v2 (utilisé par le wizard frontend)."
    ),
)
async def get_offre_renovations_catalogue(user: CurrentUser) -> dict:
    """Endpoint statique : expose la liste ``RENOVATIONS_CATALOGUE`` du
    service ``offre_investissement_pptx``. Le wizard l'appelle au mount
    pour afficher les checkboxes de la section value-add."""
    _require_prospection(user)
    from app.services.offre_investissement_pptx import (
        get_renovations_catalogue,
    )
    return {"items": get_renovations_catalogue()}


# ── Analyse financière (Phase 3b) ──────────────────────────────────


class RunAnalysisResult(BaseModel):
    """Réponse de l'endpoint run-financial-analysis."""
    best_refi_amount: float
    best_refi_program: str
    analysis_results: dict


# Intrants qui, modifiés via PATCH, doivent re-déclencher le moteur de
# calcul (recalcul auto) si l'analyse a déjà été calculée une fois.
RECALC_INPUT_FIELDS = {
    "asking_price", "nb_logements", "typology_json", "revenus_bruts",
    "taxes_municipales", "taxes_scolaires", "assurances", "energie",
    "depenses_autres", "loyers_projetes_json",
    "loyers_max_abordabilite_json", "travaux_estimes",
    "nb_logements_ajoutes", "nb_thermopompes_ajoutees", "ajout_wifi",
    "reduction_energie_pct", "taux_interet_refi_pct", "tga_pct",
    "taux_interet_achat_pct", "duree_projet_annees",
    "frais_developpement", "frais_negociations", "mdf_preteur_b_pct",
    "taux_interet_preteur_b_projet_pct",
    "frais_demarrage_overrides_json", "frais_demarrage_financables_json",
    "strategie_acquisition", "programme_achat", "refi_retenu",
    "balance_vente_montant", "cashback_montant",
    "balance_vente_taux_pct", "projection_horizon_annees",
    "tri_croissance_loyers", "tri_croissance_depenses", "unites_json",
}


async def _compute_and_store(rec, db) -> dict:
    """Construit les intrants depuis ``rec`` (+ overrides globaux),
    lance ``compute_all`` et PERSISTE les champs dérivés sur ``rec``
    (``analysis_results_json``, ``best_refi_amount``,
    ``best_refi_program``, ``mdf_preteur_b``). Ne commit PAS et ne
    touche pas au statut — au caller de le faire. Partagé par le bouton
    Calculer ET par le PATCH (recalcul auto quand un intrant change sur
    une analyse déjà calculée)."""
    from app.services.lead_analysis_finance import FinanceInputs, compute_all

    # Désérialise les loyers projetés (typologie_prix) depuis le JSON
    # stocké dans `loyers_projetes_json` : { "3.5": 1400, "4.5": 1600 }
    loyers_projetes: dict = {}
    if rec.loyers_projetes_json:
        try:
            loyers_projetes = json.loads(rec.loyers_projetes_json) or {}
        except Exception:  # noqa: BLE001
            loyers_projetes = {}

    typologie: dict = {}
    if rec.typology_json:
        try:
            raw = json.loads(rec.typology_json)
            if isinstance(raw, dict):
                typologie = {str(k): int(v or 0) for k, v in raw.items()}
        except Exception:  # noqa: BLE001
            typologie = {}

    # Loyer abordable (APH SELECT) — stocké comme premier item du JSON
    # `loyers_max_abordabilite_json` (clé arbitraire « abordable »).
    loyer_abord = 0.0
    if rec.loyers_max_abordabilite_json:
        try:
            d = json.loads(rec.loyers_max_abordabilite_json) or {}
            loyer_abord = float(d.get("abordable", 0) or 0)
        except Exception:  # noqa: BLE001
            loyer_abord = 0.0

    # Overrides manuels des frais de démarrage (saisis par l'utilisateur
    # depuis l'UI). Dict { "evaluateur": 1800, ... }.
    frais_overrides: dict = {}
    if rec.frais_demarrage_overrides_json:
        try:
            j = json.loads(rec.frais_demarrage_overrides_json) or {}
            if isinstance(j, dict):
                frais_overrides = {
                    k: float(v)
                    for k, v in j.items()
                    if v is not None and isinstance(v, (int, float, str))
                    and str(v).replace(".", "", 1).replace("-", "", 1).isdigit()
                }
        except Exception:  # noqa: BLE001
            frais_overrides = {}

    # Charge les overrides GLOBAUX des frais MDF (groupe ``mdf_frais``).
    # Si Phil a modifié « Évaluateur 1 » de 1500 → 1800 dans la table
    # de défauts, c'est appliqué ici à TOUTES les analyses (y compris
    # cette fiche-ci si elle n'a pas d'override par fiche pour
    # ``evaluateur``). Les overrides PAR FICHE (champ
    # ``frais_demarrage_overrides_json``) restent prioritaires.
    frais_fixes_overrides, pct_courtiers_overrides, frais_dossier_preteur_pct_global = (
        await _load_frais_mdf_overrides(db)
    )

    # Juin 2026 : frais de démarrage PERSONNALISÉS (dynamiques, groupe
    # ``mdf_frais``, clé ``frais_mdf_custom``). Liste vide → aucun poste
    # personnalisé (calcul inchangé).
    frais_custom_defs_global = await _load_frais_custom_defs(db)

    # Juin 2026 : overrides du barème des dépenses normalisées SCHL
    # (groupe ``depenses_normalisees``), seuil de bascule petit/grand
    # immeuble, et taux d'inoccupation (fix : était seedé mais jamais
    # relu). Si absents en BD, le moteur retombe sur les constantes.
    bareme_overrides_global, seuil_bascule_global, taux_inoccupation_global = (
        await _load_bareme_overrides(db)
    )

    # Juin 2026 : overrides des scénarios de financement (LTV / amort /
    # RCD des 4 scénarios). Si absents en BD, fallback ``SCENARIO_*``.
    scenario_overrides_global = await _load_scenario_overrides(db)

    # Juin 2026 : overrides des barèmes fiscaux (ratio abordabilité APH
    # + taxes de bienvenue de Montréal). Si absents, fallback constantes.
    ratio_abordabilite_global, taxes_bienvenue_brackets_global = (
        await _load_fiscal_overrides(db)
    )

    # Juin 2026 : registre unifié des frais de démarrage (ordre / label /
    # visibilité). ``frais_masques`` = clés des postes masqués
    # (visible:false) → forcés à 0 $ dans le moteur. ``frais_registry`` =
    # liste ordonnée [{key, label_fr, visible}] exposée dans le JSON de
    # résultats pour l'affichage (fiche + PDF). Registre absent →
    # ([], []) : aucun masquage, calcul identique à avant.
    frais_masques_global, frais_registry_global = (
        await _load_frais_registry(db)
    )

    inputs = FinanceInputs(
        adresse=rec.address or "",
        prix_achat=float(rec.asking_price or 0),
        nombre_logements=int(rec.nb_logements or 0),
        revenus_annuels=float(rec.revenus_bruts or 0),
        taxes_municipales=float(rec.taxes_municipales or 0),
        taxes_scolaires=float(rec.taxes_scolaires or 0),
        assurances=float(rec.assurances or 0),
        energie=float(rec.energie or 0),
        depenses_autres=float(rec.depenses_autres or 0),
        tga=float(rec.tga_pct or 4.0) / 100.0,
        taux_interet_achat=float(rec.taux_interet_achat_pct or 4.0) / 100.0,
        nb_logements_ajoutes=int(rec.nb_logements_ajoutes or 0),
        nb_thermopompes_ajoutees=int(rec.nb_thermopompes_ajoutees or 0),
        wifi_ajoute=bool(rec.ajout_wifi) if rec.ajout_wifi is not None else True,
        reduction_energie_pct=float(rec.reduction_energie_pct or 0) / 100.0,
        taux_interet_refi=float(rec.taux_interet_refi_pct or 0) / 100.0,
        typologie=typologie,
        typologie_prix=loyers_projetes,
        duree_projet_annees=int(rec.duree_projet_annees or 2),
        frais_developpement=float(rec.frais_developpement or 0),
        frais_negociations=float(rec.frais_negociations or 0),
        frais_travaux=float(rec.travaux_estimes or 0),
        nouveau_loyer_abordable=loyer_abord,
        mdf_preteur_b_pct=(
            float(rec.mdf_preteur_b_pct) / 100.0
            if rec.mdf_preteur_b_pct is not None
            else 0.25
        ),
        taux_interet_preteur_b_projet=(
            float(rec.taux_interet_preteur_b_projet_pct) / 100.0
            if rec.taux_interet_preteur_b_projet_pct is not None
            else 0.08
        ),
        # Stratégies d'acquisition (août 2026) — défauts = comportement
        # historique intégral tant que la fiche n'a rien choisi.
        # ``chantier_actif`` (2026-09-02) active la croissance
        # organique des loyers non optimisés et l'indexation des
        # dépenses réelles au refi — seulement quand une stratégie est
        # explicitement choisie (prod = NULL = calcul historique).
        strategie=rec.strategie_acquisition or "preteur_b",
        programme_achat=rec.programme_achat,
        refi_retenu=rec.refi_retenu,
        chantier_actif=rec.strategie_acquisition is not None,
        balance_vente_montant=float(rec.balance_vente_montant or 0),
        balance_vente_taux_pct=float(rec.balance_vente_taux_pct or 0)
        / 100.0,
        cashback_montant=float(rec.cashback_montant or 0),
        # Phase 2 — détention + refi an N (achats directs). Les
        # croissances réutilisent celles du TRI (source unique).
        projection_horizon_annees=int(rec.projection_horizon_annees or 5),
        croissance_loyers=float(rec.tri_croissance_loyers or 0.03),
        croissance_depenses=float(rec.tri_croissance_depenses or 0.03),
        # Phase 3 — optimisation par unité (liste vide = comportement
        # historique).
        unites=_parse_unites(rec.unites_json),
        frais_demarrage_overrides=frais_overrides,
        frais_demarrage_financables=(
            []
            if (
                rec.strategie_acquisition
                and rec.strategie_acquisition != "preteur_b"
                and not rec.frais_demarrage_financables_json
            )
            else _parse_financables(rec.frais_demarrage_financables_json)
        ),
        frais_fixes_overrides=frais_fixes_overrides,
        pct_courtiers_overrides=pct_courtiers_overrides,
        # Juin 2026 : frais de démarrage personnalisés (groupe
        # ``mdf_frais``, clé ``frais_mdf_custom``). Liste vide →
        # aucun poste personnalisé (calcul inchangé).
        frais_custom_defs=frais_custom_defs_global,
        # Juin 2026 : barème dépenses normalisées SCHL (groupe
        # ``depenses_normalisees``). Dict vide → fallback ``BAREME``.
        bareme_overrides=bareme_overrides_global,
        # Juin 2026 : scénarios de financement (groupe
        # ``scenarios_financement``). Dict vide → fallback ``SCENARIO_*``.
        scenario_overrides=scenario_overrides_global,
        # Juin 2026 : barèmes fiscaux (groupe ``baremes_fiscaux``).
        # ``None`` → fallback ``TAXES_BIENVENUE_MTL_BRACKETS``.
        taxes_bienvenue_brackets=taxes_bienvenue_brackets_global,
        # Juin 2026 : registre unifié — postes masqués (visible:false).
        # Forcés à 0 $ dans le moteur. Liste vide → aucun masquage
        # (calcul identique à avant).
        frais_masques=frais_masques_global,
        # Mai 2026 : nouveau frais MDF, surchargé globalement via le
        # défaut ``frais_dossier_preteur_pct``. Si la BD n'a pas (encore)
        # de ligne pour cette clé, on laisse ``FinanceInputs`` retomber
        # sur sa valeur par défaut (2 %).
        **(
            {"frais_dossier_preteur_pct": frais_dossier_preteur_pct_global}
            if frais_dossier_preteur_pct_global is not None
            else {}
        ),
        # Juin 2026 : seuil de bascule du barème (12 par défaut).
        **(
            {"seuil_bascule_bareme_log": seuil_bascule_global}
            if seuil_bascule_global is not None
            else {}
        ),
        # Juin 2026 — FIX : taux d'inoccupation, seedé mais jamais relu
        # auparavant. Brancher la config si présente (sinon défaut 3 %).
        **(
            {"taux_inoccupation_pct": taux_inoccupation_global}
            if taux_inoccupation_global is not None
            else {}
        ),
        # Juin 2026 : ratio d'abordabilité APH (groupe ``baremes_fiscaux``).
        # Présent → override ; absent → défaut ``RATIO_ABORDABILITE_APH``.
        **(
            {"ratio_abordabilite_aph": ratio_abordabilite_global}
            if ratio_abordabilite_global is not None
            else {}
        ),
    )

    use_aph = loyer_abord > 0 and inputs.nombre_logements > 0
    results = compute_all(inputs, use_aph_select=use_aph)
    results_dict = results.to_dict()

    # Juin 2026 : expose l'ORDRE et les LABELS du registre dans le JSON
    # de résultats pour que la fiche + le PDF affichent les postes dans
    # le bon ordre, avec les bons labels et l'état de visibilité, SANS
    # re-fetcher le registre. Format : [{key, label_fr, visible}] ordonné.
    # Registre absent → liste vide (aucun ordre/label imposé côté front).
    results_dict["frais_registry"] = frais_registry_global

    rec.analysis_results_json = json.dumps(results_dict)[:80_000]
    rec.best_refi_amount = results.best_refi_amount
    rec.best_refi_program = results.best_refi_program
    rec.mdf_preteur_b = results.mdf_preteur_b
    # Stratégie « institution traditionnelle » : la carte kanban
    # reflète le CASH à sortir à l'achat (programme retenu) et
    # l'argent dégagé au refi de l'an N.
    trad = results_dict.get("traditionnel")
    if trad:
        rec.mdf_preteur_b = trad["mdf_cash"]
        rec.best_refi_amount = trad["best_refi"]["argent_dispo"]
        rec.best_refi_program = (
            f"{trad['best_refi']['label']} · refi an {trad['horizon']}"
        )
    return results_dict


@router.post(
    "/{analysis_id}/run-financial-analysis",
    response_model=RunAnalysisResult,
    summary="Lance le moteur de calcul financier (réplique Excel).",
)
async def run_financial_analysis(
    analysis_id: int, db: DBSession, user: CurrentUser
) -> RunAnalysisResult:
    """Lit les champs du `LeadAnalysis`, lance le moteur de calcul
    (réplique exacte des 2 calculateurs Excel), persiste le résultat,
    et bascule le statut en `decision_en_attente` (1er calcul)."""
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")

    results_dict = await _compute_and_store(rec, db)

    # Auto-bascule en « Décision en attente » comme spécifié.
    if rec.status == LeadAnalysisStatus.A_ANALYSER.value:
        rec.status = LeadAnalysisStatus.DECISION_EN_ATTENTE.value
    rec.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return RunAnalysisResult(
        best_refi_amount=rec.best_refi_amount,
        best_refi_program=rec.best_refi_program,
        analysis_results=results_dict,
    )


# ─── Estimation IA des dépenses manquantes ─────────────────────────


class EstimateExpensesResponse(BaseModel):
    """Estimations heuristiques (avec IA si disponible) des dépenses
    d'opération d'un immeuble pour pré-remplir les champs manquants."""

    taxes_municipales: Optional[float] = None
    taxes_scolaires: Optional[float] = None
    assurances: Optional[float] = None
    source: str  # "ai" | "heuristic"
    note: Optional[str] = None


def _heuristic_estimate_expenses(
    *,
    asking_price: Optional[float],
    nb_logements: Optional[int],
    revenus_bruts: Optional[float],
    city: Optional[str],
) -> EstimateExpensesResponse:
    """Estimation pure-code basée sur des ratios marché Québec usuels.
    Sert de fallback si Claude est indisponible.

    Ratios appliqués (immeubles à logements Québec, 2024-2025) :
      - Taxes municipales ≈ 0.75 % du prix d'achat (Montréal+couronne)
      - Taxes scolaires    ≈ 0.10 % du prix d'achat (Centre de services)
      - Assurances         ≈ 250 $/logement/an, plancher 1 200 $/an
    """
    price = float(asking_price or 0)
    nb = int(nb_logements or 0)
    tm = round(price * 0.0075, 2) if price > 0 else None
    ts = round(price * 0.0010, 2) if price > 0 else None
    if nb > 0:
        ass = round(max(1_200.0, nb * 250.0), 2)
    elif price > 0:
        ass = round(max(1_200.0, price * 0.0015), 2)
    else:
        ass = None
    return EstimateExpensesResponse(
        taxes_municipales=tm,
        taxes_scolaires=ts,
        assurances=ass,
        source="heuristic",
        note=(
            "Estimations ratios marché Québec : taxes muni 0.75 % du prix, "
            "taxes scolaires 0.10 %, assurances 250 $/logement (plancher "
            "1 200 $/an). À valider avec le rôle d'évaluation."
        ),
    )


def _estimate_prompt(rec: LeadAnalysis) -> str:
    return (
        "Tu es un expert en immobilier locatif Québec. Estime les "
        "dépenses d'opération annuelles MANQUANTES pour cet immeuble "
        "(en CAD/an). Retourne UNIQUEMENT un JSON strict avec les "
        "clés taxes_municipales (float|null), taxes_scolaires "
        "(float|null), assurances (float|null), note (string).\n\n"
        "Règles : utilise les barèmes municipaux de la ville fournie "
        "(Montréal ≈ 0.7-0.85 % du prix, banlieue ≈ 0.6-0.75 %, "
        "Québec-ville 1 %), taxes scolaires ≈ 0.10-0.13 % du prix, "
        "assurances ≈ 200-300 $/logement avec plancher 1 200 $. Si une "
        "info est déjà fournie, ne la remplace pas (retourne null pour "
        "elle).\n\n"
        f"Adresse : {rec.address or 'inconnue'}, "
        f"{rec.city or 'ville inconnue'}\n"
        f"Prix demandé : {rec.asking_price or 'inconnu'} $\n"
        f"Nombre de logements : {rec.nb_logements or 'inconnu'}\n"
        f"Revenus bruts annuels : {rec.revenus_bruts or 'inconnu'} $\n"
        f"Évaluation municipale : {rec.evaluation_municipale or 'inconnue'} $\n"
        f"Année construction : {rec.annee_construction or 'inconnue'}\n"
        "Déjà saisis (ne pas remplacer) : "
        f"taxes_muni={rec.taxes_municipales}, "
        f"taxes_scolaires={rec.taxes_scolaires}, "
        f"assurances={rec.assurances}"
    )


def _parse_estimate(raw: str) -> Optional[EstimateExpensesResponse]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    return EstimateExpensesResponse(
        taxes_municipales=_num(parsed.get("taxes_municipales")),
        taxes_scolaires=_num(parsed.get("taxes_scolaires")),
        assurances=_num(parsed.get("assurances")),
        source="ai",
        note=str(parsed.get("note") or "")[:500],
    )


async def _ai_estimate_expenses(
    rec: LeadAnalysis,
) -> Optional[EstimateExpensesResponse]:
    """Demande à Gemini une estimation des dépenses manquantes en
    fonction de l'adresse, du prix, du nombre de logements et de
    l'évaluation municipale. Retourne None si Gemini indisponible
    (le caller fera le fallback heuristique).

    Migration Claude → Gemini : tier gratuit Google AI Studio
    (1500 req/jour) — couvre largement les besoins d'estimation."""
    if not settings.gemini_api_key:
        return None
    import google.generativeai as genai

    prompt = _estimate_prompt(rec)

    estimate_model = os.environ.get(
        "LEAD_EXTRACTION_MODEL", "gemini-2.0-flash"
    )

    try:
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(
            estimate_model,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                max_output_tokens=500,
            ),
        )
        response = await model.generate_content_async(prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("AI estimate expenses failed: %s", exc)
        return None

    return _parse_estimate(response.text or "")


@router.post(
    "/{lead_id}/estimate-expenses",
    response_model=EstimateExpensesResponse,
    summary="Estime taxes muni/scol/assurances manquantes (IA + fallback)",
)
async def estimate_expenses(
    lead_id: int,
    db: DBSession,
    user: CurrentUser,
) -> EstimateExpensesResponse:
    _require_prospection(user)
    rec = (
        await db.execute(
            select(LeadAnalysis).where(LeadAnalysis.id == lead_id)
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead introuvable.")

    # « Chacun son IA » : si l'utilisateur a branché son IA
    # personnelle, l'estimation passe par SA clé (peu importe le
    # fournisseur). Sinon, comportement historique (Gemini maison puis
    # heuristique) — le mode strict s'activera au GO de Phil.
    try:
        from app.services.user_ai import complete_for_user

        res = await complete_for_user(
            db,
            user,
            prompt=_estimate_prompt(rec),
            max_tokens=500,
            temperature=0.2,
            avec_brief=False,
        )
        if res is not None:
            perso = _parse_estimate(res.text)
            if perso is not None:
                perso.note = (
                    f"[{res.provider}] {perso.note or ''}".strip()[:500]
                )
                return perso
    except Exception as exc:  # noqa: BLE001 — IA perso en panne ≠ bloqué
        log.warning("Estimation via IA personnelle échouée: %s", exc)

    ai = await _ai_estimate_expenses(rec)
    if ai is not None:
        return ai
    return _heuristic_estimate_expenses(
        asking_price=float(rec.asking_price) if rec.asking_price else None,
        nb_logements=rec.nb_logements,
        revenus_bruts=float(rec.revenus_bruts) if rec.revenus_bruts else None,
        city=rec.city,
    )


# ─── Endpoint diagnostic d'extraction URL ────────────────────────


class DebugExtractRequest(BaseModel):
    url: str = Field(..., max_length=2000)


@router.post(
    "/debug-extract-url",
    summary=(
        "Diagnostic : montre ce que l'extracteur envoie à Gemini pour "
        "une URL, et la réponse JSON brute"
    ),
)
async def debug_extract_url(
    data: DebugExtractRequest,
    user: CurrentUser,
):
    """Renvoie le texte stripé envoyé à Gemini + la réponse brute,
    pour comprendre pourquoi une URL ne s'extrait pas."""
    _require_prospection(user)
    from app.services.lead_extraction import (
        _fetch_url_text,
        SYSTEM_PROMPT,
        SCHEMA_GUIDE,
        EXTRACTION_MODEL,
    )

    fetched = await _fetch_url_text(data.url)
    # Coupé pour ne pas exploser la réponse JSON.
    preview = fetched[:8000]

    if not settings.gemini_api_key:
        return {
            "preview_text_first_8k": preview,
            "fetched_total_len": len(fetched),
            "model_used": None,
            "raw_response": "(IA non configurée)",
        }

    import google.generativeai as genai

    try:
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(
            EXTRACTION_MODEL,
            system_instruction=SYSTEM_PROMPT,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                max_output_tokens=4000,
            ),
        )
        response = await model.generate_content_async(
            [
                fetched,
                (
                    "Extrais maintenant les infos selon le schéma "
                    "ci-dessous.\n\n" + SCHEMA_GUIDE
                ),
            ]
        )
        raw = (response.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        return {
            "preview_text_first_8k": preview,
            "fetched_total_len": len(fetched),
            "model_used": EXTRACTION_MODEL,
            "raw_response": f"ERREUR : {type(exc).__name__}: {exc}",
        }

    return {
        "preview_text_first_8k": preview,
        "fetched_total_len": len(fetched),
        "model_used": EXTRACTION_MODEL,
        "raw_response": raw,
    }


# ─── Ré-extraction manuelle avec Claude Sonnet 4.6 (Couche 3) ─────
#
# Phase A2 du pipeline tri-couche d'extraction.
#  - Couche 1 : parser local (regex + heuristiques) — gratuit, rapide
#  - Couche 2 : Gemini Flash 2.0 — gratuit (tier Google AI Studio)
#  - Couche 3 : Claude Sonnet 4.6 (CET endpoint) — payant ~3 ¢ /call,
#               déclenché manuellement par l'utilisateur sur une fiche
#               existante quand les couches 1+2 n'ont pas suffi.
#
# L'endpoint réutilise les sources déjà attachées à la `LeadAnalysis`
# (URLs collées, texte brut, attachments PDF/image). Claude renvoie un
# patch des champs qu'il a pu extraire. On n'écrase JAMAIS un champ
# déjà rempli par l'utilisateur — on remplit seulement les trous.


class ReExtractClaudeResponse(BaseModel):
    """Réponse de /re-extract-with-claude. Le frontend reload la fiche
    via GET /lead-analyses/{id} pour voir tous les nouveaux champs ;
    on retourne quand même la liste des champs modifiés pour le toast
    et pour les tests."""

    fields_patched: List[str]
    model_used: str
    cost_usd_estimate: float = 0.03


# Tool schema aligné sur LeadAnalysis (mêmes noms de champs). Plus
# large que le _EXTRACT_TOOL de l'endpoint orphelin (qui était pour
# le calculateur Excel historique).
_RE_EXTRACT_TOOL = {
    "name": "save_lead_fields",
    "description": (
        "Sauvegarde les champs extraits d'une fiche d'analyse d'un "
        "immeuble multi-logements québécois. Ne fournis QUE les "
        "champs explicitement présents dans les sources."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Adresse civique complète."},
            "city": {"type": "string", "description": "Ville."},
            "postal_code": {"type": "string", "description": "Code postal canadien (A1A 1A1)."},
            "province": {"type": "string", "description": "Province (ex. QC)."},
            "asking_price": {"type": "number", "description": "Prix demandé en CAD (sans symbole)."},
            "nb_logements": {"type": "integer", "description": "Nombre total de logements."},
            "typology_json": {
                "type": "string",
                "description": (
                    "Répartition par typologie au format JSON string, ex. "
                    "'{\"3.5\": 4, \"4.5\": 2}'. Inclus seulement les types présents."
                ),
            },
            "revenus_bruts": {"type": "number", "description": "Revenus bruts annuels en CAD."},
            "taxes_municipales": {"type": "number", "description": "Taxes municipales annuelles en CAD."},
            "taxes_scolaires": {"type": "number", "description": "Taxes scolaires annuelles en CAD."},
            "assurances": {"type": "number", "description": "Prime d'assurance annuelle en CAD."},
            "energie": {"type": "number", "description": "Coût annuel d'énergie commune en CAD."},
            "depenses_autres": {"type": "number", "description": "Autres dépenses annuelles en CAD."},
            "annee_construction": {"type": "integer", "description": "Année de construction."},
            "superficie_terrain": {"type": "number", "description": "Superficie terrain (pi² ou m², pris tel quel)."},
            "superficie_batiment": {"type": "number", "description": "Superficie bâtiment (pi² ou m², pris tel quel)."},
            "evaluation_municipale": {"type": "number", "description": "Évaluation municipale en CAD."},
            "description": {"type": "string", "description": "Description / commentaire du courtier."},
            "courtier_nom": {"type": "string", "description": "Nom du courtier inscripteur."},
            "courtier_contact": {"type": "string", "description": "Téléphone ou courriel du courtier."},
            "type_batiment": {"type": "string", "description": "Type de bâtiment (ex. 6-plex, immeuble à appartements)."},
            "nb_stationnements": {"type": "integer", "description": "Nombre de stationnements."},
        },
        "required": [],
    },
}


_RE_EXTRACT_SYSTEM = """\
Tu es un assistant spécialisé dans l'extraction de données immobilières \
québécoises (multi-logements 4+ portes). Tu reçois plusieurs sources \
sur le même immeuble : URLs (texte HTML extrait), texte brut collé, \
et fichiers PDF/image.

Règles :
1. Extrais UNIQUEMENT ce qui est explicitement présent. N'invente pas.
2. Convertis les chiffres en valeurs numériques pures (sans $, sans \
virgules de milliers). Ex: "2 450,75 $" → 2450.75
3. Pour les % exprimés, divise par 100 si tu retournes un taux (TGA, etc.).
4. Si plusieurs valeurs candidates pour un même champ (ex. 2 années de \
taxes), prends la plus récente.
5. Pour `typology_json`, retourne une chaîne JSON valide, ex. \
'{"3.5": 4, "4.5": 2}'.

Appelle TOUJOURS l'outil `save_lead_fields` avec ce que tu trouves, \
même si tu ne trouves qu'un seul champ.
"""


# Champs qu'on accepte de patcher depuis la ré-extraction Claude
# (mêmes que ceux scalaires de _map_extracted_to_lead).
_PATCHABLE_FIELDS = {
    "address", "city", "postal_code", "province",
    "asking_price", "nb_logements", "revenus_bruts",
    "taxes_municipales", "taxes_scolaires", "assurances",
    "energie", "depenses_autres", "annee_construction",
    "superficie_terrain", "superficie_batiment",
    "evaluation_municipale", "description",
    "courtier_nom", "courtier_contact", "type_batiment",
    "nb_stationnements", "typology_json",
}


@router.post(
    "/{analysis_id}/re-extract-with-claude",
    response_model=ReExtractClaudeResponse,
    summary=(
        "Couche 3 : ré-extraction manuelle d'une fiche via Claude "
        "Sonnet 4.6 (multimodal, payant ~3 ¢)."
    ),
)
async def re_extract_with_claude(
    analysis_id: int,
    db: DBSession,
    user: CurrentUser,
) -> ReExtractClaudeResponse:
    """Relance l'extraction sur une `LeadAnalysis` existante en
    utilisant Claude Sonnet 4.6 (multimodal). Reprend les sources
    déjà attachées à la fiche (URLs, texte, attachments). Patch les
    champs où Claude trouve une valeur — ne casse JAMAIS un champ
    déjà saisi par l'utilisateur (sauf adresse/ville où on remplace
    si Claude a une valeur plus précise).
    """
    _require_prospection(user)

    if not getattr(settings, "claude_reextract_enabled", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Ré-extraction Claude désactivée par feature flag "
                "(CLAUDE_REEXTRACT_ENABLED=false). Le bouton « Ré-"
                "extraire avec Groq » est gratuit et remplace Claude. "
                "Si tu veux vraiment Claude (~3 ¢/appel), mets "
                "CLAUDE_REEXTRACT_ENABLED=true dans les env vars "
                "Render."
            ),
        )

    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Ré-extraction Claude désactivée : ANTHROPIC_API_KEY "
                "n'est pas configuré sur le serveur."
            ),
        )

    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")

    # Récupère les attachments (blob inclus).
    atts = (
        await db.execute(
            select(LeadAnalysisAttachment)
            .where(LeadAnalysisAttachment.lead_analysis_id == analysis_id)
            .order_by(LeadAnalysisAttachment.id.asc())
        )
    ).scalars().all()

    url_lines = [
        u.strip()
        for u in (rec.source_urls or "").splitlines()
        if u.strip()
    ]
    src_text = (rec.source_text or "").strip() or None

    if not url_lines and not src_text and not atts:
        raise HTTPException(
            status_code=400,
            detail=(
                "Aucune source originale sur cette fiche — rien à "
                "ré-extraire. Recolle des URLs/texte ou ajoute des "
                "fichiers d'abord."
            ),
        )

    # Fetch URLs (best-effort, on continue même si certaines échouent).
    url_texts: List[str] = []
    if url_lines:
        from app.services.lead_extraction import _fetch_url_text
        for u in url_lines:
            try:
                t = await _fetch_url_text(u)
            except Exception as exc:  # noqa: BLE001
                log.warning("Re-extract: fetch URL %s failed: %s", u, exc)
                t = None
            if t:
                url_texts.append(f"=== URL: {u} ===\n{t}")

    # Build the content blocks for Claude (multimodal).
    import base64 as _b64
    content_blocks: list = []

    # 1) Files first (PDFs as document blocks, images as image blocks).
    for att in atts:
        ct = (att.content_type or "").lower()
        if ct == "application/pdf":
            content_blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": _b64.standard_b64encode(att.blob).decode("ascii"),
                },
            })
        elif ct in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg" if ct == "image/jpg" else ct,
                    "data": _b64.standard_b64encode(att.blob).decode("ascii"),
                },
            })
        # Autres types : on les saute (Claude ne les supporte pas en
        # multimodal). Tesseract a déjà tourné côté Couche 1/2.

    # 2) Concatène le texte (URLs + source_text + extracted_json brut).
    text_parts: List[str] = []
    if url_texts:
        text_parts.append("\n\n".join(url_texts))
    if src_text:
        text_parts.append(f"=== Texte brut collé par l'utilisateur ===\n{src_text}")
    if rec.extracted_json:
        # Donne le contexte de ce que les couches précédentes ont vu
        # (utile pour que Claude raffine plutôt que repartir de zéro).
        text_parts.append(
            "=== Extraction des couches précédentes (référence) ===\n"
            + rec.extracted_json[:8000]
        )
    if text_parts:
        content_blocks.append({
            "type": "text",
            "text": (
                "Voici les sources originales de cette fiche. "
                "Ré-extrais tous les champs que tu peux identifier "
                "de façon fiable.\n\n"
                + "\n\n".join(text_parts)
            ),
        })
    else:
        # Pas de texte (que des fichiers) — ajoute juste l'instruction.
        content_blocks.append({
            "type": "text",
            "text": (
                "Voici les fichiers attachés à cette fiche. "
                "Ré-extrais tous les champs que tu peux identifier."
            ),
        })

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3072,
            system=[
                {
                    "type": "text",
                    "text": _RE_EXTRACT_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_RE_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "save_lead_fields"},
            messages=[{"role": "user", "content": content_blocks}],
        )
    except anthropic.APIError as e:
        msg_detail = f"Claude API : {getattr(e, 'message', str(e))[:200]}"
        try:
            await log_action(
                db,
                user=user,
                action="lead_analysis.re_extract_failed",
                entity_type="lead_analysis",
                entity_id=rec.id,
                details={
                    "reason": "anthropic_api_error",
                    "type": type(e).__name__,
                    "message": str(e)[:500],
                    "status_code": getattr(e, "status_code", None),
                },
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("Audit log re_extract_failed (api_error) failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=msg_detail,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Re-extract Claude failed")
        try:
            await log_action(
                db,
                user=user,
                action="lead_analysis.re_extract_failed",
                entity_type="lead_analysis",
                entity_id=rec.id,
                details={
                    "reason": "unexpected",
                    "type": type(e).__name__,
                    "message": str(e)[:500],
                },
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("Audit log re_extract_failed (unexpected) failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erreur ré-extraction : {str(e)[:200]}",
        )

    # Extrait le tool_use block.
    extracted: dict = {}
    found_tool_use = False
    for block in msg.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "save_lead_fields"
        ):
            extracted = getattr(block, "input", None) or {}
            found_tool_use = True
            break

    # Cas dégénéré : Claude n'a pas appelé l'outil (rare avec
    # `tool_choice` forcé, mais possible si la réponse a été tronquée
    # par max_tokens ou refusée pour cause de safety).
    if not found_tool_use:
        stop_reason = getattr(msg, "stop_reason", None)
        text_preview = ""
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                text_preview = (getattr(block, "text", "") or "")[:200]
                break
        try:
            await log_action(
                db,
                user=user,
                action="lead_analysis.re_extract_failed",
                entity_type="lead_analysis",
                entity_id=rec.id,
                details={
                    "reason": "no_tool_use",
                    "stop_reason": str(stop_reason),
                    "text_preview": text_preview,
                },
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("Audit log re_extract_failed (no_tool_use) failed")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Claude n'a pas pu extraire de champs (stop_reason="
                f"{stop_reason!s}). Réessaie ou ajoute plus de sources."
            ),
        )

    # Patch « doux » : on remplit les champs vides + on remplace
    # l'adresse/ville (champs identifiants où Claude est souvent plus
    # précis que les couches précédentes). Pour le reste, on ne touche
    # PAS un champ déjà saisi (l'utilisateur a peut-être corrigé).
    REPLACE_ALWAYS = {"address", "city", "postal_code", "province"}
    fields_patched: List[str] = []
    for k, v in (extracted or {}).items():
        if k not in _PATCHABLE_FIELDS:
            continue
        if v in (None, "", "null"):
            continue
        current = getattr(rec, k, None)
        if k in REPLACE_ALWAYS or current is None or current == "":
            setattr(rec, k, v)
            fields_patched.append(k)

    # MAJ model_used : on garde la trace que la ré-extraction Claude
    # a été utilisée (peu importe ce qu'il y avait avant).
    rec.model_used = "claude-sonnet-4-6 (manual)"
    rec.updated_at = datetime.now(timezone.utc)

    # Phase A3 — Re-valide la fiche après le patch Claude. On
    # construit un `per_source_values` qui réinjecte les valeurs
    # Claude pour les champs qu'il a écrits (utile pour le tooltip
    # côté UI). Les anciennes valeurs local/gemini ne sont pas
    # rejouées ici (elles étaient déjà dans extracted_json mais
    # pas typées) — Claude prime sur la divergence.
    claude_src: Dict[str, Dict[str, Any]] = {}
    for k in fields_patched:
        v = extracted.get(k) if extracted else None
        if v not in (None, "", "null"):
            claude_src[k] = {"claude": v}
    new_vw = validate_extraction(rec, per_source_values=claude_src)
    rec.validation_warnings = new_vw or None
    if new_vw:
        try:
            await log_action(
                db,
                user=user,
                action="lead_analysis.validation_warnings_updated",
                entity_type="lead_analysis",
                entity_id=rec.id,
                details={
                    "source": "re_extract_claude",
                    "count": len(new_vw),
                    "max_severity": summarize_severity(new_vw),
                    "fields": sorted(
                        {w.get("field") for w in new_vw if w.get("field")}
                    ),
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Audit log validation_warnings (claude) failed")

    # Audit log
    await log_action(
        db,
        user=user,
        action="lead_analysis.re_extracted_with_claude",
        entity_type="lead_analysis",
        entity_id=rec.id,
        details={
            "fields_patched": fields_patched,
            "model": "claude-sonnet-4-6",
            "n_attachments": len(atts),
            "n_urls": len(url_lines),
            "has_text": bool(src_text),
        },
    )

    await db.commit()

    return ReExtractClaudeResponse(
        fields_patched=fields_patched,
        model_used="claude-sonnet-4-6 (manual)",
    )


# ─── Ré-extraction manuelle avec Groq Llama 3.3 70B (Couche 3 v2) ───
#
# Remplaçant gratuit de l'endpoint Claude. Tier free Groq : 14 400
# req/jour sur llama-3.3-70b-versatile. Comme Llama n'est pas
# multi-modal natif, on OCR-ise les PDFs/images avant l'appel (les
# fonctions OCR de la Couche 1 sont réutilisées : Tesseract + pypdf).


class ReExtractGroqResponse(BaseModel):
    """Réponse de /re-extract-with-groq. Le frontend reload la fiche
    via GET /lead-analyses/{id} pour voir tous les nouveaux champs ;
    on retourne quand même la liste des champs modifiés pour le toast
    et pour les tests."""

    fields_patched: List[str]
    model_used: str
    cost_usd_estimate: float = 0.0


@router.post(
    "/{analysis_id}/re-extract-with-groq",
    response_model=ReExtractGroqResponse,
    summary=(
        "Couche 3 (gratuit) : ré-extraction manuelle d'une fiche via "
        "Groq Llama 3.3 70B. PDF/images OCR-isés (Llama non "
        "multi-modal). Tier free 14 400 req/jour."
    ),
)
async def re_extract_with_groq(
    analysis_id: int,
    db: DBSession,
    user: CurrentUser,
) -> ReExtractGroqResponse:
    """Relance l'extraction sur une `LeadAnalysis` existante en
    utilisant Groq Llama 3.3 70B. Reprend les sources déjà attachées
    à la fiche (URLs, texte, attachments OCR-isés).

    Patch « doux » identique à Claude : adresse/ville/code postal/
    province TOUJOURS remplacés si Groq propose une valeur (champs
    identifiants — Groq voit en général la version la plus propre),
    les autres champs uniquement si vides en base.
    """
    _require_prospection(user)

    if not getattr(settings, "groq_api_key", None):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Ré-extraction Groq désactivée : GROQ_API_KEY n'est "
                "pas configurée sur le serveur. Crée une clé gratuite "
                "sur https://console.groq.com et ajoute-la dans les "
                "env vars Render (Dashboard → h2-0 → Environment)."
            ),
        )

    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")

    atts = (
        await db.execute(
            select(LeadAnalysisAttachment)
            .where(LeadAnalysisAttachment.lead_analysis_id == analysis_id)
            .order_by(LeadAnalysisAttachment.id.asc())
        )
    ).scalars().all()

    url_lines = [
        u.strip()
        for u in (rec.source_urls or "").splitlines()
        if u.strip()
    ]
    src_text = (rec.source_text or "").strip() or None

    if not url_lines and not src_text and not atts:
        raise HTTPException(
            status_code=400,
            detail=(
                "Aucune source originale sur cette fiche — rien à "
                "ré-extraire. Recolle des URLs/texte ou ajoute des "
                "fichiers d'abord."
            ),
        )

    from app.services.lead_extraction_groq import reextract_with_groq

    try:
        result = await reextract_with_groq(rec, list(atts), force_ocr=True)
    except Exception as exc:
        log.exception("Re-extract Groq failed")
        try:
            await log_action(
                db,
                user=user,
                action="lead_analysis.re_extract_failed",
                entity_type="lead_analysis",
                entity_id=rec.id,
                details={
                    "reason": "unexpected",
                    "provider": "groq",
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )
            await db.commit()
        except Exception:
            log.exception("Audit log re_extract_failed (groq) failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erreur ré-extraction Groq : {str(exc)[:200]}",
        )

    if result.error:
        try:
            await log_action(
                db,
                user=user,
                action="lead_analysis.re_extract_failed",
                entity_type="lead_analysis",
                entity_id=rec.id,
                details={
                    "reason": result.error_reason or "groq_api_error",
                    "message": result.error[:500],
                },
            )
            await db.commit()
        except Exception:
            log.exception("Audit log re_extract_failed (groq api) failed")
        # Mapping status code par error_reason :
        #   - no_source / no_extract / ocr_empty : 422 (input client à corriger)
        #   - ocr_unavailable : 503 (problème serveur, admin à contacter)
        #   - no_api_key : 503 (problème serveur)
        #   - groq_api / autre : 502 (bad gateway upstream Groq)
        client_422_reasons = {"no_source", "no_extract", "ocr_empty"}
        server_503_reasons = {"ocr_unavailable", "no_api_key"}
        reason = result.error_reason or ""
        if reason in client_422_reasons:
            sc = status.HTTP_422_UNPROCESSABLE_ENTITY
        elif reason in server_503_reasons:
            sc = status.HTTP_503_SERVICE_UNAVAILABLE
        elif "n'a pas pu extraire" in result.error:
            # Compat : ancien chemin sans error_reason.
            sc = status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            sc = status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=sc, detail=result.error)

    rec.model_used = result.model_used
    rec.updated_at = datetime.now(timezone.utc)

    # Re-valide la fiche après le patch Groq. On réinjecte les
    # valeurs Groq pour les champs patchés (utile pour le tooltip
    # côté UI). On réutilise la clé "claude" du validator pour
    # conserver la compatibilité avec le validator existant.
    groq_src: Dict[str, Dict[str, Any]] = {}
    for k in result.fields_patched:
        v = result.extracted.get(k) if result.extracted else None
        if v not in (None, "", "null"):
            groq_src[k] = {"claude": v}
    new_vw = validate_extraction(rec, per_source_values=groq_src)
    rec.validation_warnings = new_vw or None
    if new_vw:
        try:
            await log_action(
                db,
                user=user,
                action="lead_analysis.validation_warnings_updated",
                entity_type="lead_analysis",
                entity_id=rec.id,
                details={
                    "source": "re_extract_groq",
                    "count": len(new_vw),
                    "max_severity": summarize_severity(new_vw),
                    "fields": sorted(
                        {w.get("field") for w in new_vw if w.get("field")}
                    ),
                },
            )
        except Exception:
            log.exception("Audit log validation_warnings (groq) failed")

    await log_action(
        db,
        user=user,
        action="lead_analysis.re_extracted_with_groq",
        entity_type="lead_analysis",
        entity_id=rec.id,
        details={
            "fields_patched": result.fields_patched,
            "model": result.model_used,
            "n_attachments": len(atts),
            "n_urls": len(url_lines),
            "has_text": bool(src_text),
        },
    )

    await db.commit()

    return ReExtractGroqResponse(
        fields_patched=result.fields_patched,
        model_used=result.model_used,
    )


@router.get(
    "/check-groq-health",
    summary=(
        "Diagnostic : vérifie GROQ_API_KEY + ping Groq (utile "
        "quand le bouton « Ré-extraire avec Groq » ne fonctionne pas)."
    ),
)
async def check_groq_health(user: CurrentUser) -> dict:
    """Endpoint diagnostic pour la stack Groq. Réponse :
      {
        "configured": bool,    # GROQ_API_KEY est-elle définie ?
        "sdk_works": bool,     # un mini appel Groq réussit-il ?
        "model": str,
        "error": str|null
      }
    """
    _require_prospection(user)
    model = (
        getattr(settings, "groq_model", None) or "llama-3.3-70b-versatile"
    )
    out: dict = {
        "configured": bool(getattr(settings, "groq_api_key", None)),
        "sdk_works": False,
        "model": model,
        "error": None,
    }
    if not out["configured"]:
        out["error"] = (
            "GROQ_API_KEY n'est pas configurée sur le serveur. "
            "Crée une clé gratuite sur https://console.groq.com et "
            "ajoute-la dans les env vars Render (Dashboard → h2-0 → "
            "Environment)."
        )
        return out
    try:
        import httpx as _httpx
        api_key = settings.groq_api_key.strip()
        async with _httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "user", "content": "Réponds juste 'ok'."}
                    ],
                    "max_tokens": 10,
                    "temperature": 0,
                },
            )
        if resp.status_code >= 400:
            out["error"] = (
                f"Groq HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return out
        body = resp.json()
        choices = body.get("choices") or []
        out["sdk_works"] = bool(
            choices and (choices[0].get("message") or {}).get("content")
        )
        if not out["sdk_works"]:
            out["error"] = "Groq a répondu sans contenu utilisable."
    except Exception as exc:
        out["sdk_works"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        log.exception("check_groq_health failed")
    return out


@router.get(
    "/check-claude-health",
    summary=(
        "Diagnostic : vérifie ANTHROPIC_API_KEY + ping Claude (utile "
        "quand le bouton « Re-extraire avec Claude » ne fonctionne pas)."
    ),
)
async def check_claude_health(user: CurrentUser) -> dict:
    """Endpoint diagnostic indépendant du frontend, pour valider que
    la ré-extraction Claude est opérationnelle côté serveur.

    Réponse :
      {
        "configured": bool,    # ANTHROPIC_API_KEY est-elle définie ?
        "sdk_works": bool,     # un mini appel Claude réussit-il ?
        "model": str,          # modèle testé
        "error": str|null      # détail si sdk_works=false
      }

    Si `configured=false` : Phil doit ajouter ANTHROPIC_API_KEY dans
    les env vars Render (Dashboard → h2-0 → Environment).
    Si `configured=true` et `sdk_works=false` : la clé est présente
    mais invalide / expirée / quota dépassé.
    """
    _require_prospection(user)
    model = "claude-sonnet-4-6"
    enabled = bool(getattr(settings, "claude_reextract_enabled", False))
    out: dict = {
        "configured": bool(settings.anthropic_api_key),
        "enabled": enabled,
        "sdk_works": False,
        "model": model,
        "error": None,
    }
    if not enabled:
        # Feature flag OFF : Claude est désactivé indépendamment de
        # la clé. Le frontend cache le bouton Claude dans ce cas et
        # ne montre que Groq.
        out["error"] = (
            "Ré-extraction Claude désactivée par feature flag "
            "(CLAUDE_REEXTRACT_ENABLED=false). Utilise Groq (gratuit) "
            "à la place, ou mets le flag à true pour réactiver Claude."
        )
        return out
    if not out["configured"]:
        out["error"] = (
            "ANTHROPIC_API_KEY n'est pas configurée sur le serveur. "
            "Ajoute-la dans les env vars Render (Dashboard → h2-0 → "
            "Environment)."
        )
        return out
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=20,
            messages=[{"role": "user", "content": "Réponds juste 'ok'."}],
        )
        # On considère que ça marche si on a au moins un block text.
        has_text = any(
            getattr(b, "type", None) == "text" for b in (msg.content or [])
        )
        out["sdk_works"] = has_text
        if not has_text:
            out["error"] = (
                "Claude a répondu mais sans bloc texte (stop_reason="
                f"{getattr(msg, 'stop_reason', None)!s})."
            )
    except Exception as exc:  # noqa: BLE001
        out["sdk_works"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        log.exception("check_claude_health failed")
    return out


def _ocr_health_payload() -> dict:
    """Payload réutilisé par /ocr-health et /check-ocr-health.

    Lance `subprocess.run(["tesseract", "--version"])` pour récupérer
    la version exacte + le chemin du binaire (`shutil.which`). Retourne
    aussi le statut des deps Python OCR (pytesseract, pdf2image,
    pillow_heif). Utile après auto-deploy pour valider que le buildpack
    apt a bien installé Tesseract/poppler côté Render."""
    import shutil
    import subprocess

    result: dict = {
        "installed": False,
        "version": None,
        "error": None,
        "path": None,
        # Backward-compat avec l'ancien payload (clé "tesseract" lisible).
        "tesseract": _check_tesseract_status(),
    }
    tess_path = shutil.which("tesseract")
    result["path"] = tess_path
    if tess_path:
        try:
            proc = subprocess.run(
                ["tesseract", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Tesseract écrit la version sur stderr (convention legacy).
            raw = (proc.stderr or proc.stdout or "").strip()
            first_line = raw.splitlines()[0] if raw else ""
            result["installed"] = True
            result["version"] = first_line or None
        except Exception as exc:  # noqa: BLE001
            result["error"] = (
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )
    else:
        result["error"] = (
            "tesseract binary not found on PATH — buildpack apt "
            "non activé sur Render ou Aptfile non pris en compte. "
            "Va dans Render Dashboard → service h2-0 → Manual Deploy "
            "→ « Clear build cache & deploy »."
        )
    for pkg in ("pytesseract", "pdf2image", "pillow_heif", "PIL"):
        try:
            __import__(pkg)
            result[f"{pkg}_installed"] = True
        except ImportError as exc:
            result[f"{pkg}_installed"] = f"NON installe : {exc}"
    return result


@router.get(
    "/ocr-health",
    summary="Diagnostic Tesseract serveur (utile si extraction d'image vide).",
)
async def ocr_health(user: CurrentUser) -> dict:
    """Renvoie le statut du binaire Tesseract installé sur le serveur.
    Format de réponse :
      { "installed": bool, "version": str|null, "error": str|null,
        "path": str|null, "tesseract": "OK (vX.Y.Z)" (legacy),
        "pytesseract_installed": bool, "pdf2image_installed": bool, ... }
    Si l'extraction d'images retourne vide, hit cet endpoint dans le
    navigateur ou via Postman pour confirmer si Tesseract est bien là."""
    _require_prospection(user)
    return _ocr_health_payload()


@router.get(
    "/check-ocr-health",
    summary="Alias diagnostic Tesseract (post-deploy Render).",
)
async def check_ocr_health(user: CurrentUser) -> dict:
    """Alias de /ocr-health. Format documenté : {installed: bool,
    version?: str, error?: str, path?: str}. À hit après un
    auto-deploy Render pour confirmer que le buildpack apt a bien
    installé tesseract/poppler."""
    _require_prospection(user)
    return _ocr_health_payload()


# ── TRI investisseur ───────────────────────────────────────────────
#
# Calculateur de rendement (taux de rendement interne) pour la fiche
# d'analyse. 12 intrants : 8 dérivés de l'analyse financière Kratos
# (éditables côté front) + 4 manuels (capital, % parts, croissances)
# persistés sur la fiche. Voir `app.services.lead_tri_calc`.


# Mapping AUTO retenu (champ de `analysis_results_json` → intrant TRI) :
#   prix        = prix_achat
#   rpv_achat   = 1 − mdf_preteur_b_pct           (ex. MDF 20 % → 0.80)
#   pret_constr = Σ frais_financables × (1 − mdf_pct)   (« dont financé
#                 par prêteur B » de la composition MDF)
#   mdf         = mdf_preteur_b                    (« Total — MDF prêteur B »)
#   loyers2     = best_refi.revenus_totaux
#   dep2        = best_refi.depenses_total
#   valeur2     = best_refi.valeur_retenue
#   rpv_refi    = best_refi.ltv


# Défauts des 4 intrants manuels si la fiche n'a rien de persisté.
_TRI_DEFAULTS = {
    "capital": None,
    "pct": 0.5,
    "cr_loyers": 0.03,
    "cr_dep": 0.03,
}


class TriInputs(BaseModel):
    """Les 12 intrants du calculateur de TRI."""

    prix: float = 0.0
    rpv_achat: float = 0.0
    pret_constr: float = 0.0
    mdf: float = 0.0
    capital: float = 0.0
    pct: float = 0.0
    loyers2: float = 0.0
    dep2: float = 0.0
    valeur2: float = 0.0
    rpv_refi: float = 0.0
    cr_loyers: float = 0.0
    cr_dep: float = 0.0
    # Année du refinancement (retour Phil 2026-09-04) : horizons =
    # n, n+5, n+10. Repris de l'analyse, modifiable.
    annee_refi: int = 2


class TriInputsResponse(BaseModel):
    """Réponse de GET /tri-inputs : 8 intrants auto pré-remplis +
    4 manuels (persistés ou défauts). ``analysis_ready`` est False si
    l'analyse financière n'a pas encore tourné (auto à 0, l'utilisateur
    doit lancer l'analyse ou saisir à la main)."""

    inputs: TriInputs
    analysis_ready: bool
    auto_fields: List[str]
    manual_fields: List[str]


def _best_refi_scenario(results: dict) -> Optional[dict]:
    """Retrouve le scénario refi gagnant dans ``analysis_results_json``.

    On matche d'abord par ``best_refi.program`` (= ``label`` du scénario
    gagnant) ; à défaut, on prend le scénario refi avec la plus grande
    ``equite_a_la_fin``. Retourne ``None`` si aucun scénario exploitable."""
    scenarios = (results or {}).get("scenarios") or {}
    refis = [
        s
        for k, s in scenarios.items()
        if k.startswith("refi_") and isinstance(s, dict)
    ]
    if not refis:
        return None
    best = (results.get("best_refi") or {}).get("program")
    if best:
        for s in refis:
            if s.get("label") == best:
                return s
    # Fallback : meilleure équité à la fin.
    return max(refis, key=lambda s: s.get("equite_a_la_fin") or 0.0)


def _derive_tri_auto_inputs(results: dict) -> dict:
    """Dérive les 8 intrants AUTO depuis ``analysis_results_json``.

    Cf. mapping documenté en tête de section. Depuis 2026-09-04 les
    deux stratégies sont couvertes : en institution traditionnelle,
    la dette initiale = prêt retenu + balance de vente, la MDF = cash à
    l'achat et le refi de référence est celui de l'onglet
    Refinancement. En prêteur B, le ratio prêt-valeur se lit sur le
    prêt réellement accordé (cashback → plus gros prêt sur le prix
    réel) + balance de vente. Retourne un dict des 8 clés auto."""
    prix = float(results.get("prix_achat") or 0)

    trad = results.get("traditionnel")
    if isinstance(trad, dict) and isinstance(trad.get("refi"), dict):
        best_key = (trad.get("best_refi") or {}).get("key")
        best = (trad.get("refi") or {}).get(best_key) or {}
        dette = float(trad.get("pret_retenu") or 0) + float(
            trad.get("balance_vente") or 0
        )
        total = float(trad.get("frais_demarrage_total") or 0)
        cash = trad.get("frais_demarrage_cash")
        pret_constr = (
            max(0.0, total - float(cash)) if cash is not None else 0.0
        )
        return {
            "prix": prix,
            "rpv_achat": (dette / prix) if prix > 0 else 0.0,
            # Frais roulés dans le prêt de l'institution (postes cochés).
            "pret_constr": pret_constr,
            "mdf": float(trad.get("mdf_cash") or 0),
            "loyers2": float(best.get("revenus_totaux") or 0),
            "dep2": float(best.get("depenses_total") or 0),
            "valeur2": float(best.get("valeur_retenue") or 0),
            "rpv_refi": float(best.get("ltv") or 0),
        }

    mdf_pct = float(results.get("mdf_preteur_b_pct") or 0)
    mdf = float(results.get("mdf_preteur_b") or 0)

    # pret_constr = portion des frais finançables prise en charge par le
    # prêteur B = Σ frais[k] × (1 − mdf_pct) sur les postes finançables.
    frais = results.get("frais_demarrage") or {}
    financables = set(results.get("frais_demarrage_financables") or [])
    pret_constr = 0.0
    for k, v in frais.items():
        if k == "frais_custom":
            continue  # liste de postes perso — traités juste après
        if k in financables:
            try:
                pret_constr += float(v or 0) * (1.0 - mdf_pct)
            except (TypeError, ValueError):
                continue
    # Postes personnalisés finançables (même logique).
    for c in (frais.get("frais_custom") or []):
        if isinstance(c, dict) and c.get("financable"):
            try:
                pret_constr += float(c.get("montant") or 0) * (1.0 - mdf_pct)
            except (TypeError, ValueError):
                continue

    # Dette initiale = prêt B sur le prix DÉCLARÉ + balance de vente ;
    # sans cashback ni BV = (1 − mdf_pct) × prix, comme avant.
    pret_b = (results.get("pret_preteur_b") or {}).get("sur_prix")
    bv = float((results.get("balance_vente") or {}).get("montant") or 0)
    if pret_b is not None and prix > 0:
        rpv_achat = (float(pret_b) + bv) / prix
    else:
        rpv_achat = max(0.0, 1.0 - mdf_pct)

    # Scénario refi GAGNANT (best refi) — matché par
    # ``best_refi.program == scenario.label``. Les revenus, dépenses,
    # valeur et LTV des intrants TRI proviennent TOUS de ce même
    # scénario : les scénarios APH (50/100) ont des revenus/dépenses
    # différents (thermopompes, split abordable/PDM), donc on ne doit
    # PAS prendre un scénario générique. Cohérent avec ``valeur2`` et
    # ``rpv_refi`` (déjà issus du best refi).
    best = _best_refi_scenario(results) or {}
    return {
        "prix": prix,
        "rpv_achat": rpv_achat,
        "pret_constr": pret_constr,
        "mdf": mdf,
        # loyers2 / dep2 = revenus & dépenses d'opération DU best refi
        # (mêmes que valeur2 / rpv_refi ci-dessous → cohérence garantie).
        "loyers2": float(best.get("revenus_totaux") or 0),
        "dep2": float(best.get("depenses_total") or 0),
        "valeur2": float(best.get("valeur_retenue") or 0),
        "rpv_refi": float(best.get("ltv") or 0),
    }


def _annee_refi_de(rec) -> int:
    """Année du refinancement de la fiche (retour Phil 2026-09-04) :
    durée du projet en prêteur B, horizon de détention en institution
    traditionnelle ; 2 (Excel) tant que la fiche n'a pas de stratégie."""
    strat = getattr(rec, "strategie_acquisition", None)
    if not strat:
        return 2
    if strat == "preteur_b":
        return max(1, int(getattr(rec, "duree_projet_annees", None) or 2))
    return max(1, int(getattr(rec, "projection_horizon_annees", None) or 5))


async def _load_tri_defaults(db) -> dict:
    """Charge les défauts des 3 intrants manuels du TRI configurables
    (groupe ``tri_defaults``) et les convertit en FRACTION.

    Les valeurs en BD sont stockées en POURCENTAGE (50.0 = 50 %, 3.0 =
    3 %) pour s'aligner sur la convention des autres défauts ; le moteur
    de TRI attend des fractions (0.5, 0.03). On convertit donc ÷100.

    Retourne ``{"pct":.., "cr_loyers":.., "cr_dep":..}``. Toute clé
    absente / vide retombe sur ``_TRI_DEFAULTS`` (déjà en fraction) pour
    préserver le comportement historique. Le ``capital`` n'a PAS de
    défaut global (propre à chaque deal)."""
    out = {
        "pct": _TRI_DEFAULTS["pct"],
        "cr_loyers": _TRI_DEFAULTS["cr_loyers"],
        "cr_dep": _TRI_DEFAULTS["cr_dep"],
    }
    key_to_field = {
        "tri_pct_investisseur_defaut": "pct",
        "tri_croissance_loyers_defaut": "cr_loyers",
        "tri_croissance_depenses_defaut": "cr_dep",
    }
    try:
        rows = (
            await db.execute(select(ProspectionAnalysisDefault))
        ).scalars().all()
        for row in rows:
            field = key_to_field.get(row.key)
            if field is not None and row.value_float is not None:
                # BD en pct (50.0, 3.0) → fraction (0.5, 0.03).
                out[field] = float(row.value_float) / 100.0
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load TRI defaults from DB: %s", exc)
    return out


def _persisted_manual_inputs(
    rec: LeadAnalysis, tri_defaults: Optional[dict] = None
) -> dict:
    """Lit les 4 intrants manuels persistés sur la fiche, ou défauts.

    Quand un intrant n'a PAS de valeur persistée sur la fiche, on
    pré-remplit depuis ``tri_defaults`` (chargé du groupe BD
    ``tri_defaults``, déjà converti en fraction). Si ``tri_defaults`` est
    None ou qu'une clé manque, on retombe sur ``_TRI_DEFAULTS`` (en dur).
    Le ``capital`` reste vide par défaut (propre à chaque deal)."""
    d = {**_TRI_DEFAULTS, **(tri_defaults or {})}
    cap = (
        float(rec.tri_capital_injecte)
        if rec.tri_capital_injecte is not None
        else _TRI_DEFAULTS["capital"]
    )
    pct = (
        float(rec.tri_pct_investisseur)
        if rec.tri_pct_investisseur is not None
        else d["pct"]
    )
    cr_l = (
        float(rec.tri_croissance_loyers)
        if rec.tri_croissance_loyers is not None
        else d["cr_loyers"]
    )
    cr_d = (
        float(rec.tri_croissance_depenses)
        if rec.tri_croissance_depenses is not None
        else d["cr_dep"]
    )
    return {"capital": cap, "pct": pct, "cr_loyers": cr_l, "cr_dep": cr_d}


@router.get(
    "/{analysis_id}/tri-inputs",
    response_model=TriInputsResponse,
    summary="Intrants pré-remplis du calculateur de TRI investisseur.",
)
async def get_tri_inputs(
    analysis_id: int, db: DBSession, user: CurrentUser
) -> TriInputsResponse:
    """Renvoie les 8 intrants AUTO dérivés de l'analyse financière
    (éditables côté front) + les 4 intrants MANUELS persistés sur la
    fiche, ou pré-remplis depuis les défauts configurables du groupe BD
    ``tri_defaults`` (pct, croissances loyers/dépenses) — le capital
    reste vide (propre à chaque deal).

    Si l'analyse financière n'a pas encore tourné
    (``analysis_results_json`` absent), les 8 auto valent 0 et
    ``analysis_ready`` est False (l'utilisateur lance l'analyse d'abord,
    ou saisit les intrants à la main)."""
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")

    auto = {
        "prix": 0.0, "rpv_achat": 0.0, "pret_constr": 0.0, "mdf": 0.0,
        "loyers2": 0.0, "dep2": 0.0, "valeur2": 0.0, "rpv_refi": 0.0,
    }
    analysis_ready = False
    if rec.analysis_results_json:
        try:
            results = json.loads(rec.analysis_results_json) or {}
            auto = _derive_tri_auto_inputs(results)
            analysis_ready = True
        except Exception:  # noqa: BLE001
            analysis_ready = False

    # Défauts configurables (Paramètres → groupe ``tri_defaults``) pour
    # pré-remplir les intrants manuels non encore saisis sur la fiche.
    tri_defaults = await _load_tri_defaults(db)
    manual = _persisted_manual_inputs(rec, tri_defaults)
    inputs = TriInputs(
        **auto,
        capital=manual["capital"] or 0.0,
        pct=manual["pct"],
        cr_loyers=manual["cr_loyers"],
        cr_dep=manual["cr_dep"],
        annee_refi=_annee_refi_de(rec),
    )
    return TriInputsResponse(
        inputs=inputs,
        analysis_ready=analysis_ready,
        auto_fields=[
            "prix", "rpv_achat", "pret_constr", "mdf",
            "loyers2", "dep2", "valeur2", "rpv_refi", "annee_refi",
        ],
        manual_fields=["capital", "pct", "cr_loyers", "cr_dep"],
    )


@router.post(
    "/{analysis_id}/tri",
    summary="Calcule le TRI investisseur (12 intrants → dict riche).",
)
async def compute_tri_endpoint(
    analysis_id: int, payload: TriInputs, db: DBSession, user: CurrentUser
) -> dict:
    """Calcule le TRI investisseur à partir des 12 intrants fournis
    (8 auto possiblement édités + 4 manuels), **persiste les 4 intrants
    manuels** sur la fiche, et renvoie le dict riche complet du moteur.

    Le calcul est purement fonctionnel : il n'utilise que les 12 valeurs
    du body (les auto peuvent avoir été ajustés côté front)."""
    _require_prospection(user)
    rec = await db.get(LeadAnalysis, analysis_id)
    if rec is None:
        raise HTTPException(404, "Analyse introuvable.")

    from app.services.lead_tri_calc import compute_tri

    result = compute_tri(
        prix=payload.prix,
        rpv_achat=payload.rpv_achat,
        pret_constr=payload.pret_constr,
        mdf=payload.mdf,
        capital=payload.capital,
        pct=payload.pct,
        loyers2=payload.loyers2,
        dep2=payload.dep2,
        valeur2=payload.valeur2,
        rpv_refi=payload.rpv_refi,
        cr_loyers=payload.cr_loyers,
        cr_dep=payload.cr_dep,
        annee_refi=payload.annee_refi,
    )

    # Persiste les 4 intrants MANUELS sur la fiche (les 8 auto sont
    # dérivés à la volée, on ne les stocke pas).
    rec.tri_capital_injecte = payload.capital
    rec.tri_pct_investisseur = payload.pct
    rec.tri_croissance_loyers = payload.cr_loyers
    rec.tri_croissance_depenses = payload.cr_dep
    rec.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return result
