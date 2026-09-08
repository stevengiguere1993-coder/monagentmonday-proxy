"""Endpoints des défauts globaux pour les inputs d'analyse financière.

Phil veut pouvoir modifier les valeurs par défaut globales (taux d'intérêt
refi, % MDF prêteur B, taux d'intérêt prêteur B chantier) depuis l'UI de
la fiche d'analyse d'un lead — sans devoir éditer le code à chaque fois.

Quand un défaut change, les NOUVELLES analyses créées après le changement
utilisent la nouvelle valeur. Les analyses existantes ne sont PAS
écrasées (override par fiche conservé).

Restreint à admin/owner.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import DBSession, RequireAdminOrOwner
from app.models.prospection_analysis_default import ProspectionAnalysisDefault
from app.services.audit import log_action


router = APIRouter(
    prefix="/prospection/analysis-defaults",
    tags=["prospection-analysis-defaults"],
)


# ── Schémas Pydantic ──────────────────────────────────────────────


class AnalysisDefaultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    value_float: Optional[float] = None
    value_json: Optional[Any] = None
    label_fr: str
    description_fr: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    step: float
    group: Optional[str] = None
    # Mai 2026 : statut « finançable par défaut » pour les items des
    # groupes ``mdf_frais`` / ``mdf_pct``. None pour les autres groupes
    # (non applicable).
    financable_par_defaut: Optional[bool] = None
    updated_at: datetime
    updated_by_user_id: Optional[int] = None


class AnalysisDefaultUpdate(BaseModel):
    value_float: Optional[float] = Field(
        default=None,
        description="Nouvelle valeur scalaire (fraction, ex. 0.04 pour 4%).",
    )
    value_json: Optional[Any] = Field(
        default=None,
        description="Nouvelle valeur structurée (rarement utilisé).",
    )
    financable_par_defaut: Optional[bool] = Field(
        default=None,
        description=(
            "Statut « finançable par défaut » pour les items MDF "
            "(groupes mdf_frais / mdf_pct). Pré-coche la case "
            "« Finançable » sur les nouvelles fiches d'analyse."
        ),
    )


# ── Endpoints ─────────────────────────────────────────────────────


@router.get("", response_model=List[AnalysisDefaultRead])
async def list_analysis_defaults(
    db: DBSession,
    user: RequireAdminOrOwner,
    group: Optional[str] = None,
) -> List[AnalysisDefaultRead]:
    """Liste tous les défauts. Filtrable par `group` (ex. ?group=refi)
    pour ne renvoyer que les défauts pertinents à un modal donné."""
    stmt = select(ProspectionAnalysisDefault).order_by(
        ProspectionAnalysisDefault.id.asc()
    )
    if group is not None:
        stmt = stmt.where(ProspectionAnalysisDefault.group == group)
    rows = (await db.execute(stmt)).scalars().all()
    return [AnalysisDefaultRead.model_validate(r) for r in rows]


@router.patch(
    "/{key}",
    response_model=AnalysisDefaultRead,
)
async def update_analysis_default(
    key: str,
    payload: AnalysisDefaultUpdate,
    db: DBSession,
    user: RequireAdminOrOwner,
) -> AnalysisDefaultRead:
    """Modifie un défaut. Audit log obligatoire (Phil veut tracer qui
    a changé quoi). Validation des bornes min/max si présentes."""
    stmt = select(ProspectionAnalysisDefault).where(
        ProspectionAnalysisDefault.key == key
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Défaut introuvable : {key}",
        )

    old_value_float = rec.value_float
    old_value_json = rec.value_json
    old_financable = rec.financable_par_defaut

    if payload.value_float is not None:
        # Validation bornes (si définies).
        if rec.min_value is not None and payload.value_float < rec.min_value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Valeur < min ({rec.min_value}). "
                    f"Reçu : {payload.value_float}."
                ),
            )
        if rec.max_value is not None and payload.value_float > rec.max_value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Valeur > max ({rec.max_value}). "
                    f"Reçu : {payload.value_float}."
                ),
            )
        rec.value_float = payload.value_float

    if payload.value_json is not None:
        rec.value_json = payload.value_json

    # Mai 2026 : mise à jour du flag « finançable par défaut » pour les
    # items MDF. On accepte `False` explicitement (pas juste `None`) :
    # `model_fields_set` permet de distinguer absence vs envoi de
    # `false` dans le payload JSON.
    financable_touched = "financable_par_defaut" in payload.model_fields_set
    if financable_touched:
        rec.financable_par_defaut = payload.financable_par_defaut

    rec.updated_by_user_id = getattr(user, "id", None)
    await db.flush()
    await db.refresh(rec)

    audit_details: dict[str, Any] = {
        "key": rec.key,
        "old_value_float": old_value_float,
        "new_value_float": rec.value_float,
        "old_value_json": old_value_json,
        "new_value_json": rec.value_json,
    }
    if financable_touched:
        audit_details["old_financable_par_defaut"] = old_financable
        audit_details["new_financable_par_defaut"] = rec.financable_par_defaut

    # Action discriminée : si le toggle « finançable par défaut » est
    # le seul changement, on log avec une action dédiée pour faciliter
    # les recherches ultérieures.
    action = (
        "prospection_analysis_default.financable_default_updated"
        if (
            financable_touched
            and payload.value_float is None
            and payload.value_json is None
        )
        else "prospection_analysis_default.updated"
    )
    await log_action(
        db,
        user=user,
        action=action,
        entity_type="prospection_analysis_default",
        entity_id=rec.id,
        details=audit_details,
    )

    return AnalysisDefaultRead.model_validate(rec)


# ── Frais de démarrage PERSONNALISÉS (dynamiques, juin 2026) ────────
#
# Postes de frais de démarrage ajoutables / retirables par l'admin.
# Stockés dans la LISTE ``value_json`` de la clé ``frais_mdf_custom``
# (groupe ``mdf_frais``). Routes dédiées par item (POST/PATCH/DELETE)
# pour éviter les races d'un PATCH de liste complète. Restreint à
# admin/owner, chaque mutation est tracée (audit log).

_FRAIS_CUSTOM_KEY = "frais_mdf_custom"
_FraisCustomType = Literal["fixe", "pct_prix_achat", "pct_financement"]


class FraisCustomItem(BaseModel):
    """Un poste de frais de démarrage personnalisé."""

    id: str
    label_fr: str
    # "fixe" = montant $ ; "pct_prix_achat" = % du prix d'achat ;
    # "pct_financement" = % du financement refi (best APH).
    type_montant: _FraisCustomType
    # Montant $ (type "fixe") ou taux en POURCENTAGE (5.0 = 5 %) pour
    # les types pct_*. Convention alignée sur les autres % de la table.
    valeur: float
    financable_par_defaut: bool = False


class FraisCustomCreate(BaseModel):
    label_fr: str = Field(min_length=1, max_length=120)
    type_montant: _FraisCustomType
    valeur: float = Field(ge=0)
    financable_par_defaut: bool = False


class FraisCustomUpdate(BaseModel):
    label_fr: Optional[str] = Field(default=None, min_length=1, max_length=120)
    type_montant: Optional[_FraisCustomType] = None
    valeur: Optional[float] = Field(default=None, ge=0)
    financable_par_defaut: Optional[bool] = None


async def _get_frais_custom_row(db) -> ProspectionAnalysisDefault:
    """Récupère (ou crée si absente) la ligne ``frais_mdf_custom``."""
    rec = (
        await db.execute(
            select(ProspectionAnalysisDefault).where(
                ProspectionAnalysisDefault.key == _FRAIS_CUSTOM_KEY
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        # La ligne est normalement seedée au boot ; on la crée à la
        # volée si elle manque (déploiement antérieur au seed).
        rec = ProspectionAnalysisDefault(
            key=_FRAIS_CUSTOM_KEY,
            value_json=[],
            label_fr="Frais de démarrage personnalisés (liste)",
            description_fr=(
                "Liste des postes de frais de démarrage personnalisés "
                "ajoutés par l'admin."
            ),
            step=0.01,
            group="mdf_frais",
        )
        db.add(rec)
        await db.flush()
    return rec


def _current_items(rec: ProspectionAnalysisDefault) -> list[dict]:
    items = rec.value_json
    return list(items) if isinstance(items, list) else []


@router.get(
    "/frais-custom",
    response_model=List[FraisCustomItem],
)
async def list_frais_custom(
    db: DBSession,
    user: RequireAdminOrOwner,
) -> List[FraisCustomItem]:
    """Liste les postes de frais de démarrage personnalisés."""
    rec = await _get_frais_custom_row(db)
    return [FraisCustomItem(**it) for it in _current_items(rec)]


@router.post(
    "/frais-custom",
    response_model=FraisCustomItem,
    status_code=status.HTTP_201_CREATED,
)
async def create_frais_custom(
    payload: FraisCustomCreate,
    db: DBSession,
    user: RequireAdminOrOwner,
) -> FraisCustomItem:
    """Ajoute un poste de frais de démarrage personnalisé. Audit log."""
    rec = await _get_frais_custom_row(db)
    items = _current_items(rec)
    new_item = {
        "id": uuid.uuid4().hex,
        "label_fr": payload.label_fr,
        "type_montant": payload.type_montant,
        "valeur": float(payload.valeur),
        "financable_par_defaut": bool(payload.financable_par_defaut),
    }
    items.append(new_item)
    rec.value_json = items
    flag_modified(rec, "value_json")
    rec.updated_by_user_id = getattr(user, "id", None)
    await db.flush()

    await log_action(
        db,
        user=user,
        action="prospection_analysis_default.frais_custom_created",
        entity_type="prospection_analysis_default",
        entity_id=rec.id,
        details={"key": _FRAIS_CUSTOM_KEY, "item": new_item},
    )
    # Registre unifié : APPEND le nouveau poste (visible) à la fin du
    # registre pour qu'il apparaisse dans l'ordre d'affichage.
    await _registry_append_key(
        db, new_item["id"], new_item["label_fr"], visible=True
    )
    return FraisCustomItem(**new_item)


@router.patch(
    "/frais-custom/{item_id}",
    response_model=FraisCustomItem,
)
async def update_frais_custom(
    item_id: str,
    payload: FraisCustomUpdate,
    db: DBSession,
    user: RequireAdminOrOwner,
) -> FraisCustomItem:
    """Modifie un poste personnalisé existant. Audit log."""
    rec = await _get_frais_custom_row(db)
    items = _current_items(rec)
    idx = next(
        (i for i, it in enumerate(items) if it.get("id") == item_id), None
    )
    if idx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Poste de frais personnalisé introuvable : {item_id}",
        )
    old_item = dict(items[idx])
    updated = dict(old_item)
    fields = payload.model_dump(exclude_unset=True)
    for fld in ("label_fr", "type_montant", "financable_par_defaut"):
        if fld in fields and fields[fld] is not None:
            updated[fld] = fields[fld]
    if "valeur" in fields and fields["valeur"] is not None:
        updated["valeur"] = float(fields["valeur"])
    items[idx] = updated
    rec.value_json = items
    flag_modified(rec, "value_json")
    rec.updated_by_user_id = getattr(user, "id", None)
    await db.flush()

    await log_action(
        db,
        user=user,
        action="prospection_analysis_default.frais_custom_updated",
        entity_type="prospection_analysis_default",
        entity_id=rec.id,
        details={
            "key": _FRAIS_CUSTOM_KEY,
            "item_id": item_id,
            "old_item": old_item,
            "new_item": updated,
        },
    )
    return FraisCustomItem(**updated)


@router.delete(
    "/frais-custom/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_frais_custom(
    item_id: str,
    db: DBSession,
    user: RequireAdminOrOwner,
) -> None:
    """Retire un poste personnalisé. Audit log."""
    rec = await _get_frais_custom_row(db)
    items = _current_items(rec)
    removed = next((it for it in items if it.get("id") == item_id), None)
    if removed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Poste de frais personnalisé introuvable : {item_id}",
        )
    items = [it for it in items if it.get("id") != item_id]
    rec.value_json = items
    flag_modified(rec, "value_json")
    rec.updated_by_user_id = getattr(user, "id", None)
    await db.flush()

    await log_action(
        db,
        user=user,
        action="prospection_analysis_default.frais_custom_deleted",
        entity_type="prospection_analysis_default",
        entity_id=rec.id,
        details={"key": _FRAIS_CUSTOM_KEY, "item": removed},
    )
    # Registre unifié : retire l'entrée du poste supprimé.
    await _registry_remove_key(db, item_id)
    return None


# ── Registre unifié des frais de démarrage (juin 2026) ──────────────
#
# Une couche de CONFIG (ordre / label / visibilité) PAR-DESSUS le moteur
# de calcul. Le registre NE CHANGE AUCUNE formule ni montant : il décrit
# seulement, pour chaque poste de frais de démarrage (composition MDF
# prêteur B), dans quel ORDRE l'afficher, sous quel LABEL, et s'il est
# VISIBLE (``visible:false`` = poste masqué/supprimé → le moteur le force
# à 0 $ via ``FinanceInputs.frais_masques``, cf. ``lead_analysis_finance``).
#
# Stockage : clé ``mdf_frais_registry`` (groupe ``mdf_frais``) dans
# ``ProspectionAnalysisDefault.value_json`` = LISTE ORDONNÉE d'entrées
# ``{"key": str, "label_fr": str, "visible": bool}``. ``key`` = clé
# interne d'un poste FIXE (ex. ``"evaluateur"``) OU ``id`` d'un poste
# PERSONNALISÉ (cf. ``frais_mdf_custom``). L'ORDRE de la liste = ordre
# d'affichage. Seedé idempotemment au boot (``app.db.session``) avec les
# 16 postes fixes, tous ``visible:true`` ; les perso sont APPENDUS
# dynamiquement (POST custom) et retirés à la suppression (DELETE custom).

_REGISTRY_KEY = "mdf_frais_registry"

# Postes FIXES dans l'ORDRE d'affichage canonique (= ordre interne du
# moteur ``FraisDemarrage``). ``(key, label_fr, nature)`` :
#   - nature ``montant_fixe`` : montant $ paramétrable (défaut BD
#     ``frais_<key>`` / fallback ``FRAIS_FIXES``).
#   - nature ``pct`` : % paramétrable (courtiers / frais dossier prêteur).
#   - nature ``formule`` : calculé par le moteur (taxes, intérêts,
#     revenus nets pendant projet) — pas de défaut global éditable.
#   - nature ``input_fiche`` : saisi sur chaque fiche (dév., négos,
#     travaux) — pas de défaut global éditable.
# ``supprimable`` est toujours False pour les fixes (seuls les perso le
# sont). Les labels viennent de ``poste_defs`` (service PDF) /
# ``buildFraisLabels`` (frontend) — source unique de vérité FR.
_FIXED_POSTES: tuple[tuple[str, str, str], ...] = (
    ("courtier_hypothecaire_1", "Courtier hypothécaire 1", "pct"),
    ("courtier_hypothecaire_2", "Courtier hypothécaire 2", "pct"),
    ("taxes_bienvenue", "Taxes de bienvenue (calculées)", "formule"),
    ("evaluateur", "Évaluateur 1", "montant_fixe"),
    ("evaluateur_2", "Évaluateur 2", "montant_fixe"),
    ("inspection", "Inspection", "montant_fixe"),
    ("avocat", "Avocat", "montant_fixe"),
    ("notaire", "Notaire 1", "montant_fixe"),
    ("notaire_2", "Notaire 2", "montant_fixe"),
    ("rapport_efficacite", "Rapport efficacité énergétique", "montant_fixe"),
    ("frais_developpement", "Frais de développement", "input_fiche"),
    ("frais_negociations", "Frais de négociations", "input_fiche"),
    ("frais_travaux", "Frais de travaux", "input_fiche"),
    ("frais_dossier_preteur", "Frais de dossier du prêteur", "pct"),
    ("interets", "Intérêts pendant projet (portage)", "formule"),
    ("revenus_nets_pendant_projet", "Revenus nets pendant projet", "formule"),
)

# Index rapide clé → (label par défaut, nature) pour les postes fixes.
_FIXED_META: dict[str, tuple[str, str]] = {
    k: (label, nature) for (k, label, nature) in _FIXED_POSTES
}

# Mapping clé interne d'un poste fixe → clé BD du défaut qui porte son
# montant $ (nature ``montant_fixe``). Cf. ``FRAIS_FIXES`` (moteur) et le
# seed ``frais_*``. Utilisé pour joindre le « montant par défaut » dans
# la réponse GET.
_FIXED_KEY_TO_DB_AMOUNT: dict[str, str] = {
    "evaluateur": "frais_evaluateur",
    "evaluateur_2": "frais_evaluateur_2",
    "inspection": "frais_inspection",
    "avocat": "frais_avocat",
    "notaire": "frais_notaire",
    "notaire_2": "frais_notaire_2",
    "rapport_efficacite": "frais_rapport_efficacite",
}

# Mapping clé interne d'un poste fixe → clé BD du défaut qui porte son %
# (nature ``pct``, stocké en pourcentage en BD : 1.0 = 1 %, 2.0 = 2 %).
_FIXED_KEY_TO_DB_PCT: dict[str, str] = {
    "courtier_hypothecaire_1": "pct_courtier_hypothecaire_1",
    "courtier_hypothecaire_2": "pct_courtier_hypothecaire_2",
    "frais_dossier_preteur": "frais_dossier_preteur_pct",
}


def _default_registry_entries() -> list[dict]:
    """Les 16 postes fixes, dans l'ordre, tous visibles. Sert de base
    quand le registre n'existe pas encore / est mal formé."""
    return [
        {"key": k, "label_fr": label, "visible": True}
        for (k, label, _nature) in _FIXED_POSTES
    ]


def _coerce_registry(value_json) -> list[dict]:
    """Normalise le ``value_json`` du registre en liste d'entrées
    ``{key, label_fr, visible}``. Robustesse : value_json mal formé
    (None, pas une liste, items non-dict, key absente) → entrées
    ignorées ; jamais d'exception. Si AUCUNE entrée valide → on retombe
    sur les 16 postes fixes par défaut (auto-réparation)."""
    out: list[dict] = []
    if isinstance(value_json, list):
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
    if not out:
        return _default_registry_entries()
    return out


async def _get_registry_row(db) -> ProspectionAnalysisDefault:
    """Récupère (ou crée si absente) la ligne ``mdf_frais_registry``.

    Crée la ligne avec les 16 postes fixes (tous visibles) si elle
    manque — filet de sécurité pour les déploiements antérieurs au seed.
    """
    rec = (
        await db.execute(
            select(ProspectionAnalysisDefault).where(
                ProspectionAnalysisDefault.key == _REGISTRY_KEY
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        rec = ProspectionAnalysisDefault(
            key=_REGISTRY_KEY,
            value_json=_default_registry_entries(),
            label_fr="Registre des frais de démarrage (ordre/visibilité)",
            description_fr=(
                "Liste ordonnée des postes de frais de démarrage "
                "(composition MDF prêteur B). Chaque entrée {key, "
                "label_fr, visible} : ordre d'affichage = ordre de la "
                "liste, visible:false = poste masqué."
            ),
            step=0.01,
            group="mdf_frais",
        )
        db.add(rec)
        await db.flush()
    return rec


def _registry_entries(rec: ProspectionAnalysisDefault) -> list[dict]:
    return _coerce_registry(rec.value_json)


async def _registry_append_key(
    db, key: str, label_fr: str = "", visible: bool = True
) -> None:
    """APPEND une entrée au registre si la ``key`` n'y est pas déjà.
    No-op si déjà présente. Utilisé à la création d'un poste perso."""
    key = str(key or "").strip()
    if not key:
        return
    rec = await _get_registry_row(db)
    entries = _registry_entries(rec)
    if any(e["key"] == key for e in entries):
        return
    entries.append(
        {"key": key, "label_fr": str(label_fr or ""), "visible": bool(visible)}
    )
    rec.value_json = entries
    flag_modified(rec, "value_json")
    await db.flush()


async def _registry_remove_key(db, key: str) -> None:
    """Retire l'entrée ``key`` du registre. No-op si absente. Utilisé à
    la suppression d'un poste perso."""
    key = str(key or "").strip()
    if not key:
        return
    rec = await _get_registry_row(db)
    entries = _registry_entries(rec)
    new_entries = [e for e in entries if e["key"] != key]
    if len(new_entries) == len(entries):
        return
    rec.value_json = new_entries
    flag_modified(rec, "value_json")
    await db.flush()


# ── Schémas Pydantic du registre ───────────────────────────────────


class RegistryPosteRead(BaseModel):
    """Un poste du registre, ENRICHI pour l'UI (GET /mdf-registry)."""

    key: str
    label_fr: str
    # ``montant_fixe`` | ``pct`` | ``formule`` | ``input_fiche`` | ``perso``
    nature: str
    visible: bool
    supprimable: bool
    financable_par_defaut: Optional[bool] = None
    # Montant $ par défaut (nature ``montant_fixe`` / poste perso ``fixe``)
    # OU None si non applicable.
    montant_defaut: Optional[float] = None
    # Pourcentage par défaut (nature ``pct`` / poste perso ``pct_*``) en
    # POURCENTAGE (1.0 = 1 %, 2.0 = 2 %) OU None si non applicable.
    pct_defaut: Optional[float] = None


class RegistryOrderUpdate(BaseModel):
    order: List[str] = Field(
        description=(
            "Liste ordonnée des clés (key d'un poste fixe ou id d'un "
            "poste perso). Réécrit l'ordre du registre ; les clés "
            "absentes du body restent à la fin (ordre courant)."
        )
    )


class RegistryEntryPatch(BaseModel):
    label_fr: Optional[str] = Field(default=None, max_length=255)
    visible: Optional[bool] = None


def _build_defaults_index(
    rows: list[ProspectionAnalysisDefault],
) -> tuple[dict[str, float], dict[str, float], dict[str, Optional[bool]]]:
    """Indexe les défauts BD : (montants $ par clé BD, pourcentages par
    clé BD, financable par clé BD). Sert à enrichir la réponse GET."""
    amounts: dict[str, float] = {}
    pcts: dict[str, float] = {}
    financables: dict[str, Optional[bool]] = {}
    for row in rows:
        if row.value_float is not None:
            amounts[row.key] = float(row.value_float)
            pcts[row.key] = float(row.value_float)
        financables[row.key] = getattr(row, "financable_par_defaut", None)
    return amounts, pcts, financables


@router.get(
    "/mdf-registry",
    response_model=List[RegistryPosteRead],
)
async def get_mdf_registry(
    db: DBSession,
    user: RequireAdminOrOwner,
) -> List[RegistryPosteRead]:
    """Liste ORDONNÉE des postes de frais de démarrage, enrichie pour
    l'UI. Pour chaque entrée du registre : label (registre, sinon
    défaut), nature, montant/% par défaut, ``financable_par_defaut``,
    ``visible`` et ``supprimable`` (True uniquement pour les perso). Les
    postes perso (``frais_mdf_custom``) absents du registre sont APPENDUS
    à la fin (auto-réparation)."""
    # Import local du moteur : fallback des montants/% hardcoded quand la
    # BD n'a pas (encore) de défaut pour un poste fixe.
    from app.services.lead_analysis_finance import (
        DEFAULT_FRAIS_DOSSIER_PRETEUR_PCT,
        FRAIS_FIXES,
        PCT_COURTIERS,
    )

    rows = (
        await db.execute(select(ProspectionAnalysisDefault))
    ).scalars().all()
    db_amounts, db_pcts, db_financables = _build_defaults_index(rows)

    # Définitions des postes perso (clé → item) pour label / montant /
    # nature / financable.
    custom_defs: dict[str, dict] = {}
    for row in rows:
        if row.key == _FRAIS_CUSTOM_KEY and isinstance(row.value_json, list):
            for it in row.value_json:
                if isinstance(it, dict):
                    cid = str(it.get("id", "") or "").strip()
                    if cid:
                        custom_defs[cid] = it

    reg_rec = await _get_registry_row(db)
    entries = _registry_entries(reg_rec)

    out: list[RegistryPosteRead] = []
    seen: set[str] = set()

    def _emit(key: str, reg_label: str, visible: bool) -> None:
        seen.add(key)
        fixed = _FIXED_META.get(key)
        if fixed is not None:
            default_label, nature = fixed
            label = reg_label or default_label
            montant_defaut: Optional[float] = None
            pct_defaut: Optional[float] = None
            financable = None
            if nature == "montant_fixe":
                db_key = _FIXED_KEY_TO_DB_AMOUNT.get(key)
                if db_key and db_key in db_amounts:
                    montant_defaut = db_amounts[db_key]
                    financable = db_financables.get(db_key)
                else:
                    montant_defaut = FRAIS_FIXES.get(key)
            elif nature == "pct":
                db_key = _FIXED_KEY_TO_DB_PCT.get(key)
                if db_key and db_key in db_pcts:
                    pct_defaut = db_pcts[db_key]
                    financable = db_financables.get(db_key)
                elif key in ("courtier_hypothecaire_1", "courtier_hypothecaire_2"):
                    # Fallback fraction → pourcentage.
                    pct_defaut = PCT_COURTIERS.get(key, 0.0) * 100.0
                elif key == "frais_dossier_preteur":
                    pct_defaut = DEFAULT_FRAIS_DOSSIER_PRETEUR_PCT * 100.0
            elif nature == "input_fiche":
                # frais_developpement / frais_travaux finançables
                # historiquement par défaut.
                financable = key in ("frais_developpement", "frais_travaux")
            out.append(
                RegistryPosteRead(
                    key=key,
                    label_fr=label,
                    nature=nature,
                    visible=visible,
                    supprimable=False,
                    financable_par_defaut=financable,
                    montant_defaut=montant_defaut,
                    pct_defaut=pct_defaut,
                )
            )
            return
        # Poste personnalisé.
        it = custom_defs.get(key)
        if it is not None:
            type_montant = it.get("type_montant", "fixe")
            label = reg_label or str(it.get("label_fr", "") or "")
            try:
                valeur = float(it.get("valeur", 0) or 0)
            except (TypeError, ValueError):
                valeur = 0.0
            montant_defaut = valeur if type_montant == "fixe" else None
            pct_defaut = valeur if type_montant != "fixe" else None
            out.append(
                RegistryPosteRead(
                    key=key,
                    label_fr=label,
                    nature="perso",
                    visible=visible,
                    supprimable=True,
                    financable_par_defaut=bool(
                        it.get("financable_par_defaut", False)
                    ),
                    montant_defaut=montant_defaut,
                    pct_defaut=pct_defaut,
                )
            )
            return
        # Clé orpheline (poste perso supprimé hors-bande, ou clé inconnue) :
        # on l'expose quand même (supprimable) pour que l'UI puisse la
        # retirer — mais sans défaut.
        out.append(
            RegistryPosteRead(
                key=key,
                label_fr=reg_label or key,
                nature="perso",
                visible=visible,
                supprimable=True,
                financable_par_defaut=None,
                montant_defaut=None,
                pct_defaut=None,
            )
        )

    for e in entries:
        _emit(e["key"], e.get("label_fr", ""), bool(e.get("visible", True)))

    # Append les perso non encore référencés dans le registre (créés
    # avant l'existence du registre, ou désynchronisés).
    for cid in custom_defs:
        if cid not in seen:
            _emit(cid, "", True)

    return out


@router.put(
    "/mdf-registry/order",
    response_model=List[RegistryPosteRead],
)
async def update_mdf_registry_order(
    payload: RegistryOrderUpdate,
    db: DBSession,
    user: RequireAdminOrOwner,
) -> List[RegistryPosteRead]:
    """Réécrit l'ORDRE du registre. ``label_fr`` / ``visible`` existants
    sont CONSERVÉS par key ; les keys présentes dans le registre mais
    absentes du body restent à la fin (ordre courant). Audit log."""
    rec = await _get_registry_row(db)
    entries = _registry_entries(rec)
    by_key = {e["key"]: e for e in entries}

    ordered: list[dict] = []
    placed: set[str] = set()
    for key in payload.order:
        k = str(key or "").strip()
        if not k or k in placed:
            continue
        placed.add(k)
        if k in by_key:
            ordered.append(by_key[k])
        else:
            # Key inconnue du registre (ex. poste fixe jamais seedé) :
            # on l'ajoute avec son label par défaut si fixe, sinon brut.
            default_label = _FIXED_META.get(k, ("", ""))[0]
            ordered.append(
                {"key": k, "label_fr": default_label, "visible": True}
            )
    # Conserve les keys du registre absentes du body, à la fin.
    for e in entries:
        if e["key"] not in placed:
            ordered.append(e)

    old_order = [e["key"] for e in entries]
    rec.value_json = ordered
    flag_modified(rec, "value_json")
    rec.updated_by_user_id = getattr(user, "id", None)
    await db.flush()

    await log_action(
        db,
        user=user,
        action="prospection_analysis_default.mdf_registry_reordered",
        entity_type="prospection_analysis_default",
        entity_id=rec.id,
        details={
            "key": _REGISTRY_KEY,
            "old_order": old_order,
            "new_order": [e["key"] for e in ordered],
        },
    )
    return await get_mdf_registry(db, user)


@router.patch(
    "/mdf-registry/{key}",
    response_model=List[RegistryPosteRead],
)
async def patch_mdf_registry_entry(
    key: str,
    payload: RegistryEntryPatch,
    db: DBSession,
    user: RequireAdminOrOwner,
) -> List[RegistryPosteRead]:
    """Met à jour l'entrée du registre pour ``key`` (label_fr et/ou
    visible). Crée l'entrée si absente (à la fin, avec le label par
    défaut si c'est un poste fixe). Audit log."""
    key = str(key or "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Clé de poste manquante.",
        )
    rec = await _get_registry_row(db)
    entries = _registry_entries(rec)

    idx = next((i for i, e in enumerate(entries) if e["key"] == key), None)
    if idx is None:
        # Création de l'entrée à la fin.
        default_label = _FIXED_META.get(key, ("", ""))[0]
        entry = {"key": key, "label_fr": default_label, "visible": True}
        entries.append(entry)
        idx = len(entries) - 1

    old_entry = dict(entries[idx])
    fields = payload.model_dump(exclude_unset=True)
    if "label_fr" in fields and fields["label_fr"] is not None:
        entries[idx]["label_fr"] = str(fields["label_fr"])
    if "visible" in fields and fields["visible"] is not None:
        entries[idx]["visible"] = bool(fields["visible"])

    rec.value_json = entries
    flag_modified(rec, "value_json")
    rec.updated_by_user_id = getattr(user, "id", None)
    await db.flush()

    await log_action(
        db,
        user=user,
        action="prospection_analysis_default.mdf_registry_entry_updated",
        entity_type="prospection_analysis_default",
        entity_id=rec.id,
        details={
            "key": _REGISTRY_KEY,
            "entry_key": key,
            "old_entry": old_entry,
            "new_entry": entries[idx],
        },
    )
    return await get_mdf_registry(db, user)
