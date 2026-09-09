"""Volet Gestion immobilière — CRUD + KPIs financiers.

Restreint au volet `immobilier` (whitelist côté User.volets).

Couvre :
- Immeubles + ownership multi-entreprises
- Logements (avec statut : occupé / vacant / réservé / hors-loc)
- Locataires
- Baux + paiements de loyer
- Hypothèques
- Évaluations (municipale, marchande, appraisal)
- Maintenance (ordres de travail)
- KPIs financiers (revenu brut, GRM, cap rate, cash flow, appréciation)
- Import-matricule depuis mtl_property_units pour pré-remplir
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.permissions import visible_immeuble_ids
from app.core.security import decode_token
from app.repositories.user import UserRepository

from app.api.deps import CurrentUser, DBSession
from app.models.user import User
from app.services.locatif_demarrage import get_demarrage, set_demarrage
from app.services.loyer_echeance import paiement_en_retard, seuil_retard
from app.services.tal_garants import (
    appliquer_date_miroir,
    chiffres,
    contact_qui_matche,
    contacts_par_locataire,
    normaliser,
    payeur_de,
)
from app.services.permissions_service import require_capability
from app.models.entreprise import Entreprise
from app.models.bon_travail import BonTravail
from app.models.client import Client
from app.models.employe import Employe
from app.models.project import Project
from app.models.project_phase import ProjectPhase
from app.models.project_photo import ProjectPhoto
from app.models.sous_traitant import SousTraitant
from app.models.immobilier import (
    Bail,
    BailRenouvellement,
    DepenseImmeuble,
    BailStatus,
    Evaluation,
    EvaluationKind,
    Hypotheque,
    HypothequeStatus,
    Immeuble,
    ImmeubleOwnership,
    ImmeubleType,
    Logement,
    PaiementExterne,
    LogementStatus,
    Locataire,
    LocataireCommunication,
    MaintenanceOrdre,
    FraisLocatif,
    PaiementLoyer,
    RelanceLoyer,
)
from app.models.montreal_property_unit import MontrealPropertyUnit
from app.schemas.immobilier import (
    BailCreate,
    BailRead,
    BailUpdate,
    EvaluationCreate,
    EvaluationRead,
    EvaluationUpdate,
    HypothequeCreate,
    HypothequeRead,
    HypothequeUpdate,
    ImmeubleCreate,
    ImmeubleFinancials,
    ImmeubleImportFromMatriculeRequest,
    ImmeubleImportResult,
    ImmeubleListItem,
    ImmeubleOwnershipCreate,
    ImmeubleOwnershipRead,
    ImmeubleRead,
    ImmeubleUpdate,
    LocataireCommunicationCreate,
    LocataireCommunicationRead,
    LocataireCreate,
    LocataireDossier,
    LocataireListItem,
    LocataireRead,
    LocataireUpdate,
    DossierBail,
    DossierPaiement,
    DossierRenouvellement,
    LogementCreate,
    LogementDossier,
    LogementDossierBail,
    LogementDossierBon,
    LogementDossierImmeuble,
    LogementDossierLocataire,
    LogementRead,
    LogementUpdate,
    LoyerPoint,
    MaintenanceOrdreCreate,
    MaintenanceOrdreRead,
    MaintenanceOrdreUpdate,
    MaintenanceOverview,
    MaintenanceOverviewRow,
    PaiementLoyerCreate,
    PaiementLoyerRead,
    PlexImportBuilding,
    PlexImportCompany,
    PlexImportCreated,
    PlexImportRequest,
    PlexImportResult,
    PlexImportUnit,
)
from app.services.plexflow_import import parse_plexflow


log = logging.getLogger(__name__)
router = APIRouter(prefix="/immobilier", tags=["immobilier"])

# Routeur SANS la garde Bearer de niveau routeur (DEP_IMMOBILIER) : les
# endpoints d'images s'authentifient EUX-MÊMES via `?t=<jwt>` parce que
# `<img src>` ne peut pas envoyer de header Authorization. La garde de
# routeur ajoutée par la refonte permissions (P2b) bloquait ces requêtes
# en 401 avant même d'atteindre l'endpoint → photos d'immeubles cassées
# (retour Phil 2026-07-10). La sécurité reste équivalente :
# _resolve_user_for_image() valide le JWT + _require_volet().
router_images = APIRouter(prefix="/immobilier", tags=["immobilier"])


# ── Helpers ─────────────────────────────────────────────────────────────


def _require_volet(user: CurrentUser) -> None:
    """Refuse l'accès si l'utilisateur n'a pas le volet immobilier."""
    volets = getattr(user, "volets", None)
    if volets is None or "immobilier" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Gestion immobilière » non autorisé pour cet utilisateur.",
        )


async def _require_immeuble_visible(db, user, immeuble_id: int) -> None:
    """Refuse l'accès à un immeuble si l'utilisateur (employé) n'y est pas
    affecté. Les rôles manager+ voient tout (visible == None)."""
    visible = await visible_immeuble_ids(db, user)
    if visible is not None and immeuble_id not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès à cet immeuble non autorisé.",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_immeuble_or_404(db, immeuble_id: int) -> Immeuble:
    obj = await db.get(Immeuble, immeuble_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Immeuble {immeuble_id} introuvable.",
        )
    return obj


def _immeuble_to_read(obj: Immeuble) -> ImmeubleRead:
    """Sérialise un Immeuble en exposant `has_cover_photo` sans charger le blob."""
    out = ImmeubleRead.model_validate(obj, from_attributes=True)
    # `cover_photo_blob` est deferred ; si la colonne n'a pas encore été
    # touchée la valeur sera None et on ne déclenche pas de chargement.
    state = getattr(obj, "__dict__", {})
    has_blob = bool(state.get("cover_photo_blob") or obj.cover_photo_content_type)
    out.has_cover_photo = has_blob
    return out


# ── Alertes « À surveiller » (configurables) ────────────────────────────
# Config globale du pôle, stockée dans automation_settings (clé unique,
# JSON) — modifiable depuis l'engrenage de la section « À surveiller »
# de la fiche immeuble. v2 (retour Phil 2026-07-17 : « je ne peux pas en
# rajouter ») : CATALOGUE de types d'alertes — l'utilisateur active,
# retire et règle le seuil de chacune ; les types désactivés restent
# proposés dans « + Ajouter une alerte ».

_ALERTES_KEY = "immo.alertes_surveiller"

# Catalogue : type → (seuil par défaut ou None, borne min, borne max).
# Les seuils s'expriment en jours ou en mois selon le type (le frontend
# porte les libellés) ; None = alerte sans seuil (simple on/off).
_ALERTES_CATALOGUE: dict[str, tuple[Optional[int], int, int]] = {
    "bail_fin": (90, 1, 730),        # bail actif qui échoit dans N jours
    "terme_hypo": (6, 1, 36),        # fin de terme hypothécaire dans N mois
    "logement_vacant": (None, 0, 0),  # logements vacants (on/off)
    "bail_propose": (None, 0, 0),     # baux en attente de signature (on/off)
    "evaluation_agee": (24, 6, 120),  # aucune évaluation depuis N mois
}

_ALERTES_DEFAUT_ACTIFS = {"bail_fin", "terme_hypo"}


class AlerteRegle(BaseModel):
    type: str
    enabled: bool = True
    seuil: Optional[int] = None


class AlertesConfig(BaseModel):
    regles: list[AlerteRegle] = Field(default_factory=list)


def _alertes_defauts() -> AlertesConfig:
    return AlertesConfig(
        regles=[
            AlerteRegle(
                type=t,
                enabled=t in _ALERTES_DEFAUT_ACTIFS,
                seuil=defaut,
            )
            for t, (defaut, _mn, _mx) in _ALERTES_CATALOGUE.items()
        ]
    )


def _alertes_normalise(cfg: dict) -> AlertesConfig:
    """Complète la config stockée avec le catalogue (nouveaux types →
    désactivés par défaut) et migre l'ancien format v1 à clés plates."""
    regles: dict[str, AlerteRegle] = {
        r.type: r for r in _alertes_defauts().regles
    }
    # v1 (PR #1206) : {bail_fin_enabled, bail_fin_jours, terme_hypo_...}
    if "bail_fin_enabled" in cfg or "terme_hypo_enabled" in cfg:
        regles["bail_fin"] = AlerteRegle(
            type="bail_fin",
            enabled=bool(cfg.get("bail_fin_enabled", True)),
            seuil=int(cfg.get("bail_fin_jours", 90) or 90),
        )
        regles["terme_hypo"] = AlerteRegle(
            type="terme_hypo",
            enabled=bool(cfg.get("terme_hypo_enabled", True)),
            seuil=int(cfg.get("terme_hypo_mois", 6) or 6),
        )
    for raw in cfg.get("regles", []):
        try:
            r = AlerteRegle(**raw)
        except Exception:  # noqa: BLE001 — règle corrompue → ignorée
            continue
        if r.type in _ALERTES_CATALOGUE:
            regles[r.type] = r
    # Clamp des seuils sur les bornes du catalogue.
    for t, (defaut, mn, mx) in _ALERTES_CATALOGUE.items():
        r = regles[t]
        if defaut is None:
            r.seuil = None
        else:
            s = r.seuil if r.seuil is not None else defaut
            r.seuil = max(mn, min(mx, int(s)))
    return AlertesConfig(regles=list(regles.values()))


@router.get("/alertes-config", response_model=AlertesConfig)
async def get_alertes_config(db: DBSession, user: CurrentUser) -> AlertesConfig:
    _require_volet(user)
    from app.services.automation_state import get_automation_config

    cfg = await get_automation_config(_ALERTES_KEY)
    try:
        return _alertes_normalise(cfg)
    except Exception:  # noqa: BLE001 — config corrompue → défauts
        return _alertes_defauts()


@router.put("/alertes-config", response_model=AlertesConfig)
async def put_alertes_config(
    payload: AlertesConfig, db: DBSession, user: CurrentUser
) -> AlertesConfig:
    _require_volet(user)
    from app.services.automation_state import set_automation_config

    clean = _alertes_normalise(payload.model_dump())
    await set_automation_config(
        db, _ALERTES_KEY, clean.model_dump(), user_id=user.id
    )
    await db.commit()
    return clean


# ── Démarrage du pôle : d'où partent les soldes ────────────────────────
# Demande Phil 2026-07-27 : « je commence pour vrai — tout ce qui est
# avant le 1er juillet 2026 ne doit plus être d'actualité ». Un locataire
# qui traînait avril+mai+juin impayés repart à 0 $ en juillet. Rien n'est
# supprimé : l'historique reste en base, il est ignoré dans les calculs.


class DemarrageConfig(BaseModel):
    #: 1er du mois à partir duquel l'argent locatif compte.
    date: date


@router.get("/demarrage", response_model=DemarrageConfig)
async def get_demarrage_config(
    db: DBSession, user: CurrentUser
) -> DemarrageConfig:
    _require_volet(user)
    return DemarrageConfig(date=await get_demarrage())


@router.put("/demarrage", response_model=DemarrageConfig)
async def put_demarrage_config(
    payload: DemarrageConfig, db: DBSession, user: CurrentUser
) -> DemarrageConfig:
    _require_volet(user)
    if not user.has_min_role("manager"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Réservé aux gestionnaires.",
        )
    d = await set_demarrage(db, payload.date, user_id=user.id)
    await db.commit()
    log.info("Démarrage locatif réglé au %s par %s", d, user.email)
    return DemarrageConfig(date=d)


# ── Fenêtres des SUIVIS ANNUELS (réglables) ─────────────────────────────
# Retour Phil 2026-07-28 : « à partir de quand tu switch pour à confirmer /
# à produire ? » — renouvellement N mois avant la fin, assurances et
# relevés 31 à partir d'un mois de bascule. Tout réglable ici.


class SuivisConfigIn(BaseModel):
    renouvellement_mois_avant: int = 6
    assurance_bascule_mois: int = 1
    releve31_bascule_mois: int = 11
    #: Inclure les baux au mois (chambres) dans le suivi des assurances.
    assurance_inclut_au_mois: bool = False


@router.get("/suivis-config", response_model=SuivisConfigIn)
async def get_suivis_config(
    db: DBSession, user: CurrentUser
) -> SuivisConfigIn:
    _require_volet(user)
    from app.services.locatif_suivis import get_suivis

    c = await get_suivis()
    return SuivisConfigIn(
        renouvellement_mois_avant=c.renouvellement_mois_avant,
        assurance_bascule_mois=c.assurance_bascule_mois,
        releve31_bascule_mois=c.releve31_bascule_mois,
        assurance_inclut_au_mois=c.assurance_inclut_au_mois,
    )


@router.put("/suivis-config", response_model=SuivisConfigIn)
async def put_suivis_config(
    payload: SuivisConfigIn, db: DBSession, user: CurrentUser
) -> SuivisConfigIn:
    _require_volet(user)
    if not user.has_min_role("manager"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Réservé aux gestionnaires.",
        )
    from app.services.locatif_suivis import SuivisConfig, set_suivis

    c = await set_suivis(
        db,
        SuivisConfig(
            renouvellement_mois_avant=payload.renouvellement_mois_avant,
            assurance_bascule_mois=payload.assurance_bascule_mois,
            releve31_bascule_mois=payload.releve31_bascule_mois,
            assurance_inclut_au_mois=payload.assurance_inclut_au_mois,
        ),
        user_id=user.id,
    )
    await db.commit()
    log.info("Fenêtres de suivis réglées par %s", user.email)
    return SuivisConfigIn(
        renouvellement_mois_avant=c.renouvellement_mois_avant,
        assurance_bascule_mois=c.assurance_bascule_mois,
        releve31_bascule_mois=c.releve31_bascule_mois,
        assurance_inclut_au_mois=c.assurance_inclut_au_mois,
    )


# ── Immeubles : liste + KPIs agrégés ────────────────────────────────────


@router.get("/immeubles/diagnostic", response_model=List[dict])
async def immeubles_diagnostic(db: DBSession, user: CurrentUser) -> List[dict]:
    """Diagnostic anti-doublons (admin) : TOUS les immeubles avec leur
    nombre de logements / baux et leur scope (entreprise / deal / global).
    Permet d'identifier les vrais doublons (ex. deux « Elgin ») avant toute
    fusion/suppression — un immeuble sans logement ni bail créé sans adresse
    via un picker de tâche est typiquement le doublon à nettoyer.
    """
    _require_volet(user)
    if not user.has_min_role("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Diagnostic réservé aux administrateurs.",
        )
    immeubles = (
        await db.execute(select(Immeuble).order_by(Immeuble.name.asc()))
    ).scalars().all()
    if not immeubles:
        return []
    ids = [i.id for i in immeubles]

    log_counts = dict(
        (
            await db.execute(
                select(Logement.immeuble_id, func.count(Logement.id))
                .where(Logement.immeuble_id.in_(ids))
                .group_by(Logement.immeuble_id)
            )
        ).all()
    )
    bail_counts = dict(
        (
            await db.execute(
                select(Logement.immeuble_id, func.count(Bail.id))
                .join(Bail, Bail.logement_id == Logement.id)
                .where(Logement.immeuble_id.in_(ids))
                .group_by(Logement.immeuble_id)
            )
        ).all()
    )

    # Compte les occurrences par nom normalisé pour signaler les doublons.
    from collections import Counter

    name_counts = Counter((i.name or "").strip().lower() for i in immeubles)

    out: List[dict] = []
    for i in immeubles:
        scope = (
            "entreprise"
            if i.owner_entreprise_id
            else ("deal" if i.owner_deal_id else "global")
        )
        out.append(
            {
                "id": i.id,
                "name": i.name,
                "address": i.address,
                "city": i.city,
                "scope": scope,
                "owner_entreprise_id": i.owner_entreprise_id,
                "owner_deal_id": i.owner_deal_id,
                "nb_logements": int(log_counts.get(i.id, 0)),
                "nb_baux": int(bail_counts.get(i.id, 0)),
                "is_duplicate_name": name_counts[(i.name or "").strip().lower()]
                > 1,
                "is_active": i.is_active,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
        )
    return out


@router.get("/immeubles/picker", response_model=List[dict])
async def immeubles_picker(
    db: DBSession,
    _: CurrentUser,
    entreprise_id: Optional[int] = None,
    deal_id: Optional[int] = None,
) -> List[dict]:
    """Liste minimale des immeubles actifs pour les pickers de tâches.

    Le catalogue est **scopé** : un immeuble créé depuis la fiche
    d'une entreprise n'apparaît que dans le picker de cette
    entreprise ; idem pour un deal Pipeline. Ce contexte est passé en
    query string. Sans scope → uniquement les immeubles globaux
    (ni entreprise ni deal) — comportement legacy pour les pickers
    qui n'envoient pas de scope.
    """
    q = (
        select(Immeuble.id, Immeuble.name, Immeuble.address)
        .where(Immeuble.is_active.is_(True))
        .order_by(Immeuble.name.asc())
    )
    if entreprise_id is not None:
        q = q.where(Immeuble.owner_entreprise_id == int(entreprise_id))
    elif deal_id is not None:
        q = q.where(Immeuble.owner_deal_id == int(deal_id))
    else:
        q = q.where(
            Immeuble.owner_entreprise_id.is_(None),
            Immeuble.owner_deal_id.is_(None),
        )

    rows = (await db.execute(q)).all()
    return [
        {"id": int(r[0]), "name": r[1], "address": r[2]} for r in rows
    ]


class _ImmeublePickerCreate(BaseModel):
    """Payload léger pour créer un immeuble depuis un picker de tâche.
    Le but est juste d'enrichir le catalogue des immeubles disponibles
    pour les rattacher aux tâches — pas un CRUD complet (qui reste sur
    /immeubles avec garde de volet)."""

    name: str = Field(..., min_length=1, max_length=255)
    # Adresse optionnelle — le picker des tâches ne la demande pas.
    # Si elle n'est pas fournie, on retombe sur le nom comme adresse
    # affichable (et la colonne `address` côté DB reste NOT NULL).
    address: Optional[str] = Field(default=None, max_length=500)
    # Scope (au plus l'un des deux) — restreint la visibilité du
    # nouvel immeuble à l'entreprise ou au deal cible.
    entreprise_id: Optional[int] = Field(default=None, gt=0)
    deal_id: Optional[int] = Field(default=None, gt=0)


@router.post(
    "/immeubles/picker",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
async def immeubles_picker_create(
    body: _ImmeublePickerCreate,
    db: DBSession,
    _: CurrentUser,
) -> dict:
    """Création rapide d'un immeuble depuis le picker des tâches.
    Pas de garde de volet : tout user authentifié peut enrichir le
    catalogue. Si `entreprise_id` ou `deal_id` est fourni, l'immeuble
    n'est visible que dans ce contexte."""
    name = body.name.strip()
    address = (body.address or "").strip() or name
    obj = Immeuble(
        name=name,
        address=address,
        is_active=True,
        owner_entreprise_id=body.entreprise_id,
        owner_deal_id=(
            body.deal_id if body.entreprise_id is None else None
        ),
    )
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    await db.flush()

    return {"id": int(obj.id), "name": obj.name, "address": obj.address}


@router.delete(
    "/immeubles/picker/{immeuble_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def immeubles_picker_delete(
    immeuble_id: int,
    db: DBSession,
    _: CurrentUser,
) -> None:
    """Retire un immeuble du catalogue (soft delete = is_active=False)
    pour qu'il disparaisse des pickers tout en préservant l'historique
    si jamais il est référencé ailleurs. Idempotent."""
    obj = (
        await db.execute(
            select(Immeuble).where(Immeuble.id == immeuble_id)
        )
    ).scalar_one_or_none()
    if obj is None:
        # Idempotent : pas trouvé = on considère que c'est déjà supprimé.
        return
    obj.is_active = False
    obj.updated_at = _now()
    await db.flush()


@router.get("/immeubles", response_model=List[ImmeubleListItem])
async def list_immeubles(
    db: DBSession,
    user: CurrentUser,
    only_active: bool = True,
    entreprise_id: Optional[int] = None,
) -> List[ImmeubleListItem]:
    """Liste des immeubles. Si `entreprise_id` est fourni, filtre sur
    ceux dont l'entreprise est propriétaire (au moins une ImmeubleOwnership).
    """
    _require_volet(user)
    visible = await visible_immeuble_ids(db, user)
    q = select(Immeuble).order_by(Immeuble.name.asc())
    if visible is not None:
        # Employé : limité aux immeubles affectés (set possiblement vide).
        q = q.where(Immeuble.id.in_(visible))
    if only_active:
        q = q.where(Immeuble.is_active.is_(True))
    if entreprise_id is not None:
        q = q.join(
            ImmeubleOwnership,
            ImmeubleOwnership.immeuble_id == Immeuble.id,
        ).where(ImmeubleOwnership.entreprise_id == entreprise_id)
    immeubles = (await db.execute(q)).scalars().all()
    if not immeubles:
        return []

    # Aggrégats logements par immeuble
    log_rows = (
        await db.execute(
            select(
                Logement.immeuble_id,
                Logement.status,
                func.count(Logement.id),
            )
            .where(Logement.immeuble_id.in_([i.id for i in immeubles]))
            .group_by(Logement.immeuble_id, Logement.status)
        )
    ).all()
    logs_by_imm: dict[int, dict[str, int]] = {}
    for imm_id, st, n in log_rows:
        logs_by_imm.setdefault(imm_id, {})[st] = int(n)

    # Revenu mensuel — hiérarchie du loyer effectif (2026-08-14) :
    # INTERNE = somme des baux actifs + loyer demandé des occupés sans
    # bail ; EXTERNE = le loyer SAISI sur le logement prime (un bail
    # résiduel ne sert que de filet). Les externes se calculent en
    # Python (peu d'immeubles, logique par logement).
    interne_ids = [i.id for i in immeubles if not i.gestion_externe]
    externe_ids = [i.id for i in immeubles if i.gestion_externe]
    rev_by_imm: dict[int, float] = {}
    if interne_ids:
        bail_rows = (
            await db.execute(
                select(
                    Logement.immeuble_id,
                    func.coalesce(func.sum(Bail.loyer_mensuel), 0),
                )
                .join(Bail, Bail.logement_id == Logement.id)
                .where(
                    and_(
                        Logement.immeuble_id.in_(interne_ids),
                        Bail.status == BailStatus.ACTIF.value,
                    )
                )
                .group_by(Logement.immeuble_id)
            )
        ).all()
        rev_by_imm = {r[0]: float(r[1] or 0) for r in bail_rows}

        bail_actif_exists = (
            select(Bail.id)
            .where(
                Bail.logement_id == Logement.id,
                Bail.status == BailStatus.ACTIF.value,
            )
            .exists()
        )
        occ_rows = (
            await db.execute(
                select(
                    Logement.immeuble_id,
                    func.coalesce(func.sum(Logement.loyer_demande), 0),
                )
                .where(
                    and_(
                        Logement.immeuble_id.in_(interne_ids),
                        Logement.status == LogementStatus.OCCUPE.value,
                        ~bail_actif_exists,
                    )
                )
                .group_by(Logement.immeuble_id)
            )
        ).all()
        for imm_id, somme in occ_rows:
            rev_by_imm[imm_id] = (
                rev_by_imm.get(imm_id, 0.0) + float(somme or 0)
            )
    if externe_ids:
        from app.services.loyer_effectif import loyer_effectif_loue

        logs_ext = (
            await db.execute(
                select(Logement).where(
                    Logement.immeuble_id.in_(externe_ids)
                )
            )
        ).scalars().all()
        bail_ext: dict[int, float] = {}
        if logs_ext:
            for b in (
                await db.execute(
                    select(Bail).where(
                        Bail.logement_id.in_([l.id for l in logs_ext]),
                        Bail.status == BailStatus.ACTIF.value,
                    )
                )
            ).scalars().all():
                bail_ext[b.logement_id] = (
                    bail_ext.get(b.logement_id, 0.0)
                    + float(b.loyer_mensuel or 0)
                )
        for lg in logs_ext:
            m = loyer_effectif_loue(
                lg, bail_ext.get(lg.id), gestion_externe=True
            )
            if m is not None:
                rev_by_imm[lg.immeuble_id] = (
                    rev_by_imm.get(lg.immeuble_id, 0.0) + m
                )

    out: List[ImmeubleListItem] = []
    for imm in immeubles:
        sts = logs_by_imm.get(imm.id, {})
        nb_actifs = sum(
            n for st, n in sts.items()
            if st != LogementStatus.HORS_LOC.value
        )
        nb_occ = sts.get(LogementStatus.OCCUPE.value, 0)
        revenu = rev_by_imm.get(imm.id, 0.0)
        taux = (nb_occ / nb_actifs) if nb_actifs > 0 else 0.0
        out.append(
            ImmeubleListItem(
                id=imm.id,
                name=imm.name,
                address=imm.address,
                city=imm.city,
                type=imm.type,
                nb_logements=imm.nb_logements,
                cover_photo_url=imm.cover_photo_url,
                has_cover_photo=bool(imm.cover_photo_content_type),
                is_active=imm.is_active,
                nb_logements_actifs=nb_actifs,
                nb_logements_occupes=nb_occ,
                revenu_mensuel=round(revenu, 2),
                taux_occupation=round(taux, 4),
            )
        )
    return out


@router.post(
    "/immeubles",
    response_model=ImmeubleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_immeuble(
    payload: ImmeubleCreate, db: DBSession, user: CurrentUser
) -> ImmeubleRead:
    _require_volet(user)
    data = payload.model_dump()
    # Champ optionnel non persisté sur Immeuble : on l'extrait avant.
    auto_entreprise_id = data.pop("entreprise_id", None)
    # Nom optionnel : si l'utilisateur n'en fournit pas, on prend l'adresse
    # complète comme nom affichable (cas usuel : un immeuble = une adresse).
    if not data.get("name") or not str(data["name"]).strip():
        addr = data.get("address") or ""
        city = data.get("city")
        data["name"] = f"{addr}, {city}" if city else addr
    obj = Immeuble(**data)
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    await db.flush()  # pour obtenir obj.id avant de créer l'ownership

    # Auto-rattache à l'entreprise active à 100 % si fourni.
    if auto_entreprise_id:
        ownership = ImmeubleOwnership(
            immeuble_id=obj.id,
            entreprise_id=auto_entreprise_id,
            ownership_pct=100.0,
        )
        db.add(ownership)

    await db.commit()
    await db.refresh(obj)
    return _immeuble_to_read(obj)


# ── Upload + stream cover photo ────────────────────────────────────────


_PHOTO_MIME_ALLOWED = {
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
}
_PHOTO_MAX_BYTES = 8 * 1024 * 1024  # 8 Mo


@router.post(
    "/immeubles/{immeuble_id}/cover-photo",
    response_model=ImmeubleRead,
)
async def upload_cover_photo(
    immeuble_id: int,
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> ImmeubleRead:
    _require_volet(user)
    obj = await _get_immeuble_or_404(db, immeuble_id)
    ct = (file.content_type or "").lower()
    if ct not in _PHOTO_MIME_ALLOWED:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Format non supporté (JPG, PNG, WEBP, HEIC).",
        )
    blob = await file.read()
    if not blob:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Fichier vide."
        )
    if len(blob) > _PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Fichier trop gros (> {_PHOTO_MAX_BYTES // (1024*1024)} Mo).",
        )
    obj.cover_photo_blob = blob
    obj.cover_photo_content_type = ct
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return _immeuble_to_read(obj)


async def _resolve_user_for_image(
    request: Request, db, t: Optional[str]
):
    """Lit le JWT depuis le header Authorization OU le query `?t=`,
    valide et retourne l'utilisateur. Permet d'utiliser ces URL dans
    `<img src>` qui ne porte pas de header personnalisé.
    """
    token = t
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1]
    if not token:
        raise HTTPException(401, "Token manquant.")
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(401, "Token invalide.")
    user = await UserRepository(db).get_by_id(int(user_id))
    if user is None:
        raise HTTPException(401, "Utilisateur introuvable.")
    return user


@router_images.get("/immeubles/{immeuble_id}/cover-photo")
async def stream_cover_photo(
    immeuble_id: int,
    db: DBSession,
    request: Request,
    t: Optional[str] = Query(default=None),
) -> Response:
    user = await _resolve_user_for_image(request, db, t)
    _require_volet(user)
    obj = await _get_immeuble_or_404(db, immeuble_id)
    # Force-load le blob deferred.
    await db.refresh(obj, attribute_names=["cover_photo_blob"])
    if not obj.cover_photo_blob:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Aucune photo de couverture.",
        )
    ct = obj.cover_photo_content_type or "application/octet-stream"
    return Response(
        content=bytes(obj.cover_photo_blob),
        media_type=ct,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="cover-{immeuble_id}.{ct.split("/")[-1] or "bin"}"',
        },
    )


@router.delete(
    "/immeubles/{immeuble_id}/cover-photo",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_cover_photo(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await _get_immeuble_or_404(db, immeuble_id)
    obj.cover_photo_blob = None
    obj.cover_photo_content_type = None
    obj.updated_at = _now()
    await db.commit()


@router.get("/immeubles/{immeuble_id}", response_model=ImmeubleRead)
async def get_immeuble(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> ImmeubleRead:
    _require_volet(user)
    await _require_immeuble_visible(db, user, immeuble_id)
    obj = await _get_immeuble_or_404(db, immeuble_id)
    return _immeuble_to_read(obj)


@router.patch("/immeubles/{immeuble_id}", response_model=ImmeubleRead)
async def update_immeuble(
    immeuble_id: int,
    payload: ImmeubleUpdate,
    db: DBSession,
    user: CurrentUser,
) -> ImmeubleRead:
    _require_volet(user)
    obj = await _get_immeuble_or_404(db, immeuble_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return _immeuble_to_read(obj)


async def _bail_actif_chevauchant(
    db, logement_id: int, date_debut, date_fin, exclure_bail_id=None
):
    """Bail ACTIF du même logement dont les dates chevauchent —
    JAMAIS deux baux actifs simultanés (audit 2026-07-31)."""
    q = select(Bail).where(
        Bail.logement_id == logement_id,
        Bail.status == BailStatus.ACTIF.value,
        Bail.date_debut <= date_fin,
        Bail.date_fin >= date_debut,
    )
    if exclure_bail_id is not None:
        q = q.where(Bail.id != exclure_bail_id)
    return (await db.execute(q)).scalars().first()


async def _recaler_logement_apres_bail(db, logement_id: int) -> None:
    """Statut du logement recalculé d'après ses baux restants."""
    from app.services.loyer_effectif import refleter_bail_sur_demande

    lg = await db.get(Logement, logement_id)
    if lg is None:
        return
    actif = (
        await db.execute(
            select(Bail).where(
                Bail.logement_id == logement_id,
                Bail.status == BailStatus.ACTIF.value,
            )
        )
    ).scalars().first()
    if actif is not None:
        lg.status = LogementStatus.OCCUPE.value
        # Le « loyer demandé » suit le bail tant que c'est loué (retour
        # client 2026-08-14) — sinon il pourrit (1 000 $ posé à la
        # création vs 1 600 $ payé douze ans plus tard).
        if actif.loyer_mensuel is not None:
            refleter_bail_sur_demande(lg, float(actif.loyer_mensuel))
    elif lg.status == LogementStatus.OCCUPE.value:
        lg.status = LogementStatus.VACANT.value
    lg.updated_at = _now()


async def _consigner_suppression_bail_sortant(db, bail) -> None:
    """M6 (audit 2026-08-13) : la suppression d'un bail référencé comme
    bail SORTANT d'un dossier de relocation (FK SET NULL) laissait un
    dossier orphelin muet. AVANT la suppression : recopie fill-only de
    ``date_depart``/``loyer_ancien`` depuis le bail + note datée."""
    from app.models.immobilier import LocationDossier
    from app.services.locatif_depart import _append_note

    for dsr in (
        await db.execute(
            select(LocationDossier).where(
                LocationDossier.bail_id == bail.id
            )
        )
    ).scalars().all():
        if dsr.date_depart is None:
            dsr.date_depart = bail.date_fin
        if dsr.loyer_ancien is None and bail.loyer_mensuel is not None:
            dsr.loyer_ancien = float(bail.loyer_mensuel)
        dsr.notes = _append_note(
            dsr.notes,
            f"Bail sortant supprimé le {_now().date().isoformat()}",
        )
        dsr.updated_at = _now()


async def _recalc_paiement_score(db, bail) -> None:
    """Score de paiement du locataire recalculé (aussi après une
    SUPPRESSION de paiements — audit 2026-07-31)."""
    paiements = (
        await db.execute(
            select(PaiementLoyer).where(
                PaiementLoyer.bail_id == bail.id,
                PaiementLoyer.mois_couvert >= await get_demarrage(),
            )
        )
    ).scalars().all()
    locataire = await db.get(Locataire, bail.locataire_id)
    if locataire is None:
        return
    if not paiements:
        locataire.paiement_score = None
    else:
        en_retard = sum(1 for p in paiements if p.en_retard)
        locataire.paiement_score = max(
            0, min(100, round((1 - en_retard / len(paiements)) * 100))
        )
    locataire.updated_at = _now()


@router.delete(
    "/immeubles/{immeuble_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_immeuble(
    immeuble_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("immeuble.delete"))],
) -> None:
    _require_volet(user)
    obj = await _get_immeuble_or_404(db, immeuble_id)
    # Garde-fou (audit 2026-07-31, recentré le 2026-08-20) : ce qu'on
    # protège, c'est l'HISTORIQUE FINANCIER — pas les baux en soi.
    # L'ancienne garde comptait les baux : un immeuble de TEST (baux
    # sans un seul paiement) devenait insupprimable, et le chemin
    # manuel menait de 409 en 409 (bail actif → dossier lié → …).
    # Retour Phil : « je peux pas supprimer un immeuble que j'avais en
    # test ». Nouvelle règle : le moindre paiement, frais ou dépôt
    # détenu bloque ; sinon, la suppression emporte proprement baux,
    # dossiers et documents.
    bail_ids = [
        r[0]
        for r in (
            await db.execute(
                select(Bail.id)
                .join(Logement, Logement.id == Bail.logement_id)
                .where(Logement.immeuble_id == immeuble_id)
            )
        ).all()
    ]

    if bail_ids:
        nb_paiements = (
            await db.execute(
                select(func.count(PaiementLoyer.id)).where(
                    PaiementLoyer.bail_id.in_(bail_ids)
                )
            )
        ).scalar_one()
        nb_frais = (
            await db.execute(
                select(func.count(FraisLocatif.id)).where(
                    FraisLocatif.bail_id.in_(bail_ids)
                )
            )
        ).scalar_one()
        nb_depots = (
            await db.execute(
                select(func.count(Bail.id)).where(
                    Bail.id.in_(bail_ids),
                    Bail.depot_garantie.is_not(None),
                    Bail.depot_garantie > 0,
                    Bail.depot_rendu_le.is_(None),
                )
            )
        ).scalar_one()
        if nb_paiements or nb_frais or nb_depots:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cet immeuble a un historique financier "
                    f"({nb_paiements} paiement(s), {nb_frais} frais, "
                    f"{nb_depots} dépôt(s) détenu(s)) — il ne se "
                    "supprime pas. Désactive-le plutôt (is_active)."
                ),
            )

    # Paiements de gestion externe = historique financier aussi.
    log_ids = [
        r[0]
        for r in (
            await db.execute(
                select(Logement.id).where(
                    Logement.immeuble_id == immeuble_id
                )
            )
        ).all()
    ]
    if log_ids:
        from app.models.immobilier import PaiementExterne

        nb_ext = (
            await db.execute(
                select(func.count(PaiementExterne.id)).where(
                    PaiementExterne.logement_id.in_(log_ids)
                )
            )
        ).scalar_one()
        if nb_ext:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cet immeuble a {nb_ext} paiement(s) de gestion "
                    "externe — il ne se supprime pas. Désactive-le "
                    "plutôt (is_active)."
                ),
            )

    # Aucun historique : on emporte les dépendances dans l'ordre. Les
    # locataires créés pour CET immeuble partent aussi — mais jamais
    # ceux qui ont un bail ailleurs.
    from sqlalchemy import delete as _delete, update as _update

    from app.models.immobilier import (
        BailRenouvellement,
        ImmCommunication,
        ImmDocument,
        LocataireCommunication,
        LocationAnnonce,
        LocationDossier,
        LocationVisite,
        RelanceLoyer,
    )

    loc_ids: set[int] = set()
    if bail_ids:
        loc_ids = {
            r[0]
            for r in (
                await db.execute(
                    select(Bail.locataire_id).where(
                        Bail.id.in_(bail_ids),
                        Bail.locataire_id.is_not(None),
                    )
                )
            ).all()
        }
        ailleurs = {
            r[0]
            for r in (
                await db.execute(
                    select(Bail.locataire_id).where(
                        Bail.locataire_id.in_(list(loc_ids)),
                        Bail.id.notin_(bail_ids),
                    )
                )
            ).all()
        }
        loc_ids -= ailleurs

        await db.execute(
            _delete(RelanceLoyer).where(RelanceLoyer.bail_id.in_(bail_ids))
        )
        await db.execute(
            _delete(BailRenouvellement).where(
                BailRenouvellement.bail_id.in_(bail_ids)
            )
        )
        await db.execute(
            _delete(ImmDocument).where(ImmDocument.bail_id.in_(bail_ids))
        )
    await db.execute(
        _delete(ImmDocument).where(ImmDocument.immeuble_id == immeuble_id)
    )
    if log_ids:
        dossier_ids = [
            r[0]
            for r in (
                await db.execute(
                    select(LocationDossier.id).where(
                        LocationDossier.logement_id.in_(log_ids)
                    )
                )
            ).all()
        ]
        if dossier_ids:
            await db.execute(
                _delete(LocationVisite).where(
                    LocationVisite.dossier_id.in_(dossier_ids)
                )
            )
            await db.execute(
                _delete(LocationAnnonce).where(
                    LocationAnnonce.dossier_id.in_(dossier_ids)
                )
            )
            await db.execute(
                _delete(LocationDossier).where(
                    LocationDossier.id.in_(dossier_ids)
                )
            )
    if bail_ids:
        await db.execute(_delete(Bail).where(Bail.id.in_(bail_ids)))
    if loc_ids:
        ids = list(loc_ids)
        await db.execute(
            _delete(LocataireCommunication).where(
                LocataireCommunication.locataire_id.in_(ids)
            )
        )
        await db.execute(
            _delete(ImmDocument).where(ImmDocument.locataire_id.in_(ids))
        )
        # Le journal d'audit est conservé : on délie seulement.
        await db.execute(
            _update(ImmCommunication)
            .where(ImmCommunication.locataire_id.in_(ids))
            .values(locataire_id=None)
        )
        await db.execute(
            _delete(Locataire).where(Locataire.id.in_(ids))
        )
    await db.delete(obj)
    await db.commit()
    log.info(
        "Immeuble %s supprimé par %s (%d bail/baux sans historique, "
        "%d locataire(s) emporté(s))",
        immeuble_id, getattr(user, "email", None), len(bail_ids),
        len(loc_ids),
    )


# ── Ownership ──────────────────────────────────────────────────────────


@router.get(
    "/immeubles/{immeuble_id}/ownerships",
    response_model=List[ImmeubleOwnershipRead],
)
async def list_ownerships(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> List[ImmeubleOwnershipRead]:
    _require_volet(user)
    rows = (
        await db.execute(
            select(ImmeubleOwnership).where(
                ImmeubleOwnership.immeuble_id == immeuble_id
            )
        )
    ).scalars().all()
    return [ImmeubleOwnershipRead.model_validate(r) for r in rows]


@router.post(
    "/immeubles/{immeuble_id}/ownerships",
    response_model=ImmeubleOwnershipRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_ownership(
    immeuble_id: int,
    payload: ImmeubleOwnershipCreate,
    db: DBSession,
    user: CurrentUser,
) -> ImmeubleOwnershipRead:
    _require_volet(user)
    await _get_immeuble_or_404(db, immeuble_id)
    obj = ImmeubleOwnership(
        immeuble_id=immeuble_id,
        entreprise_id=payload.entreprise_id,
        ownership_pct=payload.ownership_pct,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return ImmeubleOwnershipRead.model_validate(obj)


@router.delete(
    "/ownerships/{ownership_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_ownership(
    ownership_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await db.get(ImmeubleOwnership, ownership_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Ownership introuvable.")
    await db.delete(obj)
    await db.commit()


class _SetOwnerRequest(BaseModel):
    entreprise_id: int


@router.put(
    "/immeubles/{immeuble_id}/owner",
    response_model=List[ImmeubleOwnershipRead],
)
async def set_immeuble_owner(
    immeuble_id: int,
    payload: _SetOwnerRequest,
    db: DBSession,
    user: CurrentUser,
) -> List[ImmeubleOwnershipRead]:
    """Réassigne l'immeuble à UNE entreprise propriétaire à 100 %.

    Remplace toutes les ownerships existantes par une seule (cas usuel :
    corriger la compagnie propriétaire d'un immeuble). Atomique."""
    _require_volet(user)
    await _get_immeuble_or_404(db, immeuble_id)
    ent = await db.get(Entreprise, payload.entreprise_id)
    if ent is None:
        raise HTTPException(status_code=404, detail="Entreprise introuvable.")
    existing = (
        await db.execute(
            select(ImmeubleOwnership).where(
                ImmeubleOwnership.immeuble_id == immeuble_id
            )
        )
    ).scalars().all()
    for o in existing:
        await db.delete(o)
    fresh = ImmeubleOwnership(
        immeuble_id=immeuble_id,
        entreprise_id=payload.entreprise_id,
        ownership_pct=100.0,
    )
    db.add(fresh)
    await db.commit()
    await db.refresh(fresh)
    return [ImmeubleOwnershipRead.model_validate(fresh)]


# ── Bon de travail (réparation → volet Construction) ───────────────────


class _BonFromImmeubleRequest(BaseModel):
    titre: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    logement: Optional[str] = None  # n° de logement (affichage, legacy)
    logement_id: Optional[int] = None  # FK logement concerné (optionnel)
    # Urgence posée dès la création (comme côté Construction) : le bon
    # remonte en haut des kanbans des deux pôles.
    is_urgent: bool = False


@router.post("/immeubles/{immeuble_id}/bon-travail")
async def create_bon_from_immeuble(
    immeuble_id: int,
    payload: _BonFromImmeubleRequest,
    db: DBSession,
    user: CurrentUser,
) -> dict:
    """Crée un bon de travail (volet Construction) pour une réparation sur
    cet immeuble. Convertit au passage la compagnie propriétaire en client
    si elle n'en est pas déjà un. Le bon est créé en brouillon — un
    responsable construction le reprend ensuite (estimé, envoi, signature,
    conversion en projet/facture)."""
    _require_volet(user)
    imm = await _get_immeuble_or_404(db, immeuble_id)

    own = (
        await db.execute(
            select(ImmeubleOwnership).where(
                ImmeubleOwnership.immeuble_id == immeuble_id
            )
        )
    ).scalars().first()
    ent = await db.get(Entreprise, own.entreprise_id) if own else None

    client = None
    client_created = False
    if ent is not None:
        client = (
            await db.execute(
                select(Client).where(
                    func.lower(Client.name) == ent.name.strip().lower()
                )
            )
        ).scalars().first()
        if client is None:
            client = Client(
                name=ent.name,
                is_company=True,
                address=imm.address,
                language="fr",
            )
            db.add(client)
            await db.flush()
            client_created = True

    loc_label = payload.logement
    if payload.logement_id and not loc_label:
        lg = await db.get(Logement, payload.logement_id)
        loc_label = lg.numero if lg else None
    loc = f" — logement {loc_label}" if loc_label else ""
    where = f"{imm.address}{(', ' + imm.city) if imm.city else ''}"
    scope = (
        f"Immeuble : {imm.name}{loc}\n"
        f"Adresse : {where}\n"
        "Source : Gestion immobilière (réparation)."
    )
    # Référence auto anti-collision via le helper partagé : préfixe unifié
    # « BT-AA-NNN » (même numérotation que les bons créés côté Construction).
    # Cf. business.py.
    from app.api.v1.endpoints.business import generate_bt_reference

    bon = BonTravail(
        reference=await generate_bt_reference(db, now=_now()),
        title=payload.titre,
        description=payload.description,
        scope_md=scope,
        client_id=client.id if client else None,
        address=f"{where}{loc}",
        status="draft",
        origin="gestion_immo",
        # Bon INTERNE (entretien de nos immeubles) : rattachement complet,
        # aucune signature. Apparaît sur le board Construction ET le miroir
        # Gestion locative.
        kind="interne",
        # Créé côté Gestion locative : l'exécutant (nos hommes vs
        # sous-traitant) n'est PAS encore décidé → "à classifier". Le
        # gestionnaire Construction tranchera depuis la fiche du bon.
        executant_type="a_classifier",
        owner_entreprise_id=own.entreprise_id if own else None,
        immeuble_id=immeuble_id,
        logement_id=payload.logement_id,
        marge_pct=10,
        requires_signature=False,
        is_urgent=bool(payload.is_urgent),
        created_by_user_id=user.id,
    )
    bon.created_at = _now()
    bon.updated_at = _now()
    db.add(bon)
    await db.flush()
    # Trace d'audit alignée sur la création côté Construction (sert
    # aussi au backfill du créateur).
    from app.services.audit import log_action as _log_action

    await _log_action(
        db,
        user=user,
        action="bons-travail.created",
        entity_type="bons-travail",
        entity_id=bon.id,
        details={"reference": bon.reference, "name": bon.title},
    )
    # Notifie les gestionnaires (manager+) — comme côté Construction.
    from app.services.notifications import notify_role

    await notify_role(
        db,
        min_role="manager",
        kind="bon_travail",
        title="Nouveau bon de travail — " + (bon.address or bon.reference),
        body=bon.title,
        href=f"/app/bons/{bon.id}",
    )
    await db.commit()
    await db.refresh(bon)
    return {
        "bon_id": bon.id,
        "reference": bon.reference,
        "client_id": client.id if client else None,
        "client_name": ent.name if ent else None,
        "client_created": client_created,
    }


# ── Miroir lecture seule des bons de travail (gestion immobilière) ─────
#
# Kyle (volet immobilier) crée des bons de travail qui partent en
# Construction. Il doit pouvoir SUIVRE leur avancement depuis sa zone,
# sans pouvoir assigner de personnes ni modifier la planification (ça reste
# du ressort de Construction). Ces deux endpoints servent un miroir en
# lecture seule, gardé par `_require_volet` (immobilier).


def _project_status_progress(status_value: Optional[str]) -> int:
    """Pourcentage indicatif d'avancement à partir du statut projet."""
    return {
        "planned": 5,
        "ready_to_start": 15,
        "in_progress": 60,
        "suspended": 40,
        "delivered": 100,
    }.get(status_value or "", 0)


class _BonAvancementProject(BaseModel):
    id: int
    label: str
    status: Optional[str]
    progress_pct: int
    start_date: Optional[date]
    end_date: Optional[date]
    phase_count: int


class _BonAvancementItem(BaseModel):
    id: int
    reference: str
    title: str
    status: str
    created_at: Optional[datetime]
    sent_at: Optional[datetime]
    signed_at: Optional[datetime]
    client_name: Optional[str]
    project: Optional[_BonAvancementProject]
    address: Optional[str] = None
    amount: Optional[float] = None
    immeuble_id: Optional[int] = None
    logement_id: Optional[int] = None
    is_urgent: bool = False
    #: Qui a créé le bon (retour Phil 2026-08-10).
    created_by_name: Optional[str] = None


@router.get("/bons-travail", response_model=List[_BonAvancementItem])
async def list_gestion_immo_bons(db: DBSession, user: CurrentUser) -> List[_BonAvancementItem]:
    """Liste TOUS les bons de travail issus de la gestion immobilière, avec
    leur avancement (statut du bon + état du chantier lié). Lecture seule."""
    _require_volet(user)
    bons = (
        await db.execute(
            select(BonTravail)
            .where(
                (BonTravail.kind == "interne")
                | (BonTravail.origin == "gestion_immo")
                | (BonTravail.scope_md.ilike("%Gestion immobilière%"))
            )
            .order_by(BonTravail.created_at.desc())
        )
    ).scalars().all()
    if not bons:
        return []

    client_ids = {b.client_id for b in bons if b.client_id}
    clients = {
        c.id: c.name
        for c in (
            await db.execute(select(Client).where(Client.id.in_(client_ids)))
        ).scalars().all()
    } if client_ids else {}

    # Créateurs — noms résolus par lot (même pattern que les clients).
    creator_ids = {
        b.created_by_user_id for b in bons if b.created_by_user_id
    }
    createurs = {
        u.id: u.display_name
        for u in (
            await db.execute(select(User).where(User.id.in_(creator_ids)))
        ).scalars().all()
    } if creator_ids else {}

    project_ids = {b.project_id for b in bons if b.project_id}
    projects = {
        p.id: p
        for p in (
            await db.execute(select(Project).where(Project.id.in_(project_ids)))
        ).scalars().all()
    } if project_ids else {}

    phase_counts: dict[int, int] = {}
    if project_ids:
        rows = (
            await db.execute(
                select(ProjectPhase.project_id, func.count(ProjectPhase.id))
                .where(ProjectPhase.project_id.in_(project_ids))
                .group_by(ProjectPhase.project_id)
            )
        ).all()
        phase_counts = {pid: int(cnt) for pid, cnt in rows}

    out: List[_BonAvancementItem] = []
    for b in bons:
        proj_summary = None
        proj = projects.get(b.project_id) if b.project_id else None
        if proj is not None:
            proj_summary = _BonAvancementProject(
                id=proj.id,
                label=(proj.address or proj.name or f"Projet #{proj.id}"),
                status=proj.status,
                progress_pct=_project_status_progress(proj.status),
                start_date=proj.start_date,
                end_date=proj.end_date,
                phase_count=phase_counts.get(proj.id, 0),
            )
        out.append(
            _BonAvancementItem(
                id=b.id,
                reference=b.reference,
                title=b.title,
                status=b.status,
                created_at=b.created_at,
                sent_at=b.sent_at,
                signed_at=b.signed_at,
                client_name=clients.get(b.client_id) if b.client_id else None,
                project=proj_summary,
                address=b.address,
                amount=float(b.amount) if b.amount is not None else None,
                immeuble_id=b.immeuble_id,
                logement_id=b.logement_id,
                is_urgent=bool(getattr(b, "is_urgent", False)),
                created_by_name=(
                    createurs.get(b.created_by_user_id)
                    if b.created_by_user_id
                    else None
                ),
            )
        )
    return out


# ── Roll-up des dépenses de maintenance (sans profit) ─────────────────────
class _RollupBon(BaseModel):
    id: int
    titre: str
    montant: float
    status: str
    created_at: Optional[datetime] = None


class _RollupLogement(BaseModel):
    logement_id: Optional[int]
    numero: Optional[str]
    total: float
    count: int
    bons: List[_RollupBon] = []


class _RollupImmeuble(BaseModel):
    immeuble_id: int
    name: str
    address: Optional[str]
    total: float
    count: int
    communs_total: float
    communs_count: int = 0
    communs_bons: List[_RollupBon] = []
    logements: List[_RollupLogement]


@router.get("/maintenance-rollup", response_model=List[_RollupImmeuble])
async def maintenance_rollup(
    db: DBSession,
    user: CurrentUser,
    year: Optional[int] = None,
    immeuble_id: Optional[int] = None,
) -> List[_RollupImmeuble]:
    """Dépenses de maintenance ($/an) par immeuble puis par appartement.

    Somme le montant refacturé des bons internes non annulés de l'année.
    Aucune notion de profit (vue propriétaire/locatif). Filtrable sur un
    immeuble précis (pour sa fiche)."""
    _require_volet(user)
    target_year = year if year is not None else _now().year
    # Fenêtre de l'année ciblée bornée en SQL plutôt que filtrée en Python
    # après un fetch complet (évite de charger tous les bons internes de
    # l'historique). created_at est un TIMESTAMPTZ stocké en UTC : on utilise
    # des bornes datetime UTC (minuit 1er janvier ← inclus, minuit 1er janvier
    # suivant → exclu) pour reproduire exactement `created_at.year == year`
    # quelle que soit la timezone de session Postgres. NULL reste exclu par la
    # comparaison (created_at est de toute façon NOT NULL).
    year_start = datetime(target_year, 1, 1, tzinfo=timezone.utc)
    year_end = datetime(target_year + 1, 1, 1, tzinfo=timezone.utc)
    q = select(BonTravail).where(
        BonTravail.kind == "interne",
        BonTravail.status != "cancelled",
        BonTravail.immeuble_id.isnot(None),
        BonTravail.created_at >= year_start,
        BonTravail.created_at < year_end,
    )
    if immeuble_id is not None:
        q = q.where(BonTravail.immeuble_id == int(immeuble_id))
    bons = (await db.execute(q)).scalars().all()
    if not bons:
        return []

    imm_ids = {b.immeuble_id for b in bons}
    immeubles = {
        i.id: i
        for i in (
            await db.execute(select(Immeuble).where(Immeuble.id.in_(imm_ids)))
        ).scalars().all()
    }
    log_ids = {b.logement_id for b in bons if b.logement_id}
    logements = (
        {
            lg.id: lg
            for lg in (
                await db.execute(
                    select(Logement).where(Logement.id.in_(log_ids))
                )
            ).scalars().all()
        }
        if log_ids
        else {}
    )

    def _tri_bons(items: List[_RollupBon]) -> List[_RollupBon]:
        # Plus récents d'abord (created_at desc, NULL en dernier) — clé
        # textuelle ISO pour éviter toute comparaison naive/aware.
        return sorted(
            items,
            key=lambda x: x.created_at.isoformat() if x.created_at else "",
            reverse=True,
        )

    by_imm: dict = {}
    for b in bons:
        amt = float(b.amount) if b.amount is not None else 0.0
        rb = _RollupBon(
            id=b.id,
            titre=b.title,
            montant=amt,
            status=b.status,
            created_at=b.created_at,
        )
        e = by_imm.setdefault(
            b.immeuble_id,
            {
                "total": 0.0,
                "count": 0,
                "communs": 0.0,
                "communs_count": 0,
                "communs_bons": [],
                "logs": {},
            },
        )
        e["total"] += amt
        e["count"] += 1
        if b.logement_id:
            le = e["logs"].setdefault(
                b.logement_id, {"total": 0.0, "count": 0, "bons": []}
            )
            le["total"] += amt
            le["count"] += 1
            le["bons"].append(rb)
        else:
            e["communs"] += amt
            e["communs_count"] += 1
            e["communs_bons"].append(rb)

    out: List[_RollupImmeuble] = []
    for imm_id, e in by_imm.items():
        imm = immeubles.get(imm_id)
        out.append(
            _RollupImmeuble(
                immeuble_id=imm_id,
                name=imm.name if imm else f"Immeuble #{imm_id}",
                address=imm.address if imm else None,
                total=round(e["total"], 2),
                count=e["count"],
                communs_total=round(e["communs"], 2),
                communs_count=int(e["communs_count"]),
                communs_bons=_tri_bons(e["communs_bons"]),
                logements=[
                    _RollupLogement(
                        logement_id=lid,
                        numero=(
                            logements[lid].numero if lid in logements else None
                        ),
                        total=round(lv["total"], 2),
                        count=lv["count"],
                        bons=_tri_bons(lv["bons"]),
                    )
                    for lid, lv in sorted(e["logs"].items())
                ],
            )
        )
    out.sort(key=lambda x: x.total, reverse=True)
    return out


class _BonDemandeEdit(BaseModel):
    titre: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_urgent: Optional[bool] = None
    immeuble_id: Optional[int] = None
    logement_id: Optional[int] = None
    # Sentinelle : True = mettre logement_id à NULL (communs / immeuble entier).
    clear_logement: Optional[bool] = None


@router.patch("/bons-travail/{bon_id}/demande")
async def edit_gestion_immo_bon_demande(
    bon_id: int, payload: _BonDemandeEdit, db: DBSession, user: CurrentUser
) -> dict:
    """Le volet locatif peut corriger la DEMANDE d'un bon interne s'il a fait
    une erreur : titre, description, immeuble, appartement, urgence — pas la
    planification ni la refacturation (réservées à Construction)."""
    _require_volet(user)
    bon = await db.get(BonTravail, bon_id)
    if bon is None or (bon.kind or "construction") != "interne":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bon introuvable")
    if payload.titre is not None:
        bon.title = payload.titre.strip()
    if payload.description is not None:
        bon.description = payload.description or None
    if payload.is_urgent is not None:
        bon.is_urgent = bool(payload.is_urgent)
    if payload.immeuble_id is not None:
        bon.immeuble_id = payload.immeuble_id
        imm = await db.get(Immeuble, payload.immeuble_id)
        if imm is not None:
            # Recale le propriétaire depuis l'ownership de l'immeuble.
            own = (
                await db.execute(
                    select(ImmeubleOwnership).where(
                        ImmeubleOwnership.immeuble_id == payload.immeuble_id
                    )
                )
            ).scalars().first()
            if own is not None:
                bon.owner_entreprise_id = own.entreprise_id
    if payload.clear_logement:
        bon.logement_id = None
    elif payload.logement_id is not None:
        bon.logement_id = payload.logement_id
    # Recompose l'adresse d'affichage (immeuble · appartement).
    imm2 = (
        await db.get(Immeuble, bon.immeuble_id) if bon.immeuble_id else None
    )
    if imm2 is not None:
        base = imm2.address or imm2.name
        if bon.logement_id:
            lg = await db.get(Logement, bon.logement_id)
            bon.address = (
                f"{base} · App {lg.numero}" if lg else base
            )
        else:
            bon.address = f"{base} · Communs / immeuble entier"
    bon.updated_at = _now()
    await db.commit()
    return {"ok": True}


class _BonPhaseRead(BaseModel):
    id: int
    name: str
    start_date: Optional[date]
    end_date: Optional[date]
    duration_days: Optional[float]
    assignee_name: Optional[str]


class _BonPhotoMeta(BaseModel):
    id: int
    caption: Optional[str]
    content_type: str


class _BonAvancementDetail(BaseModel):
    id: int
    reference: str
    title: str
    description: Optional[str]
    scope_md: Optional[str]
    status: str
    created_at: Optional[datetime]
    sent_at: Optional[datetime]
    signed_at: Optional[datetime]
    client_name: Optional[str]
    project: Optional[_BonAvancementProject]
    phases: List[_BonPhaseRead]
    photos: List[_BonPhotoMeta]
    # Notes de l'exécutant — lecture seule côté locatif.
    work_notes: Optional[str] = None
    address: Optional[str] = None
    is_urgent: bool = False
    immeuble_id: Optional[int] = None
    logement_id: Optional[int] = None
    #: Qui a créé le bon (retour Phil 2026-08-10).
    created_by_name: Optional[str] = None


@router.get("/bons-travail/{bon_id}", response_model=_BonAvancementDetail)
async def get_gestion_immo_bon(
    bon_id: int, db: DBSession, user: CurrentUser
) -> _BonAvancementDetail:
    """Détail lecture seule d'un bon de travail gestion immobilière :
    statut + planification du chantier lié (phases, dates, personnes
    assignées affichées mais NON modifiables)."""
    _require_volet(user)
    bon = await db.get(BonTravail, bon_id)
    if bon is None or not (
        (bon.kind or "construction") == "interne"
        or bon.origin == "gestion_immo"
        or (bon.scope_md and "Gestion immobilière" in bon.scope_md)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bon de travail introuvable.",
        )

    client_name = None
    if bon.client_id:
        c = await db.get(Client, bon.client_id)
        client_name = c.name if c else None

    created_by_name = None
    if bon.created_by_user_id:
        u = await db.get(User, bon.created_by_user_id)
        created_by_name = u.display_name if u else None

    proj_summary = None
    phases_out: List[_BonPhaseRead] = []
    photos_out: List[_BonPhotoMeta] = []
    if bon.project_id:
        proj = await db.get(Project, bon.project_id)
        if proj is not None:
            phases = (
                await db.execute(
                    select(ProjectPhase)
                    .where(ProjectPhase.project_id == proj.id)
                    .order_by(ProjectPhase.position.asc(), ProjectPhase.id.asc())
                )
            ).scalars().all()
            proj_summary = _BonAvancementProject(
                id=proj.id,
                label=(proj.address or proj.name or f"Projet #{proj.id}"),
                status=proj.status,
                progress_pct=_project_status_progress(proj.status),
                start_date=proj.start_date,
                end_date=proj.end_date,
                phase_count=len(phases),
            )
            # Pré-charge les noms des personnes assignées (employés + ST).
            emp_ids = {p.assignee_employe_id for p in phases if p.assignee_employe_id}
            st_ids = {
                p.assignee_sous_traitant_id for p in phases if p.assignee_sous_traitant_id
            }
            emps = {
                e.id: e.full_name
                for e in (
                    await db.execute(select(Employe).where(Employe.id.in_(emp_ids)))
                ).scalars().all()
            } if emp_ids else {}
            sts = {
                s.id: s.full_name
                for s in (
                    await db.execute(
                        select(SousTraitant).where(SousTraitant.id.in_(st_ids))
                    )
                ).scalars().all()
            } if st_ids else {}
            for p in phases:
                end_d = None
                if p.start_date is not None and p.duration_days:
                    span = max(int(p.duration_days) - 1, 0)
                    end_d = p.start_date + timedelta(days=span)
                assignee = None
                if p.assignee_employe_id:
                    assignee = emps.get(p.assignee_employe_id)
                elif p.assignee_sous_traitant_id:
                    assignee = sts.get(p.assignee_sous_traitant_id)
                phases_out.append(
                    _BonPhaseRead(
                        id=p.id,
                        name=p.name,
                        start_date=p.start_date,
                        end_date=end_d,
                        duration_days=float(p.duration_days) if p.duration_days else None,
                        assignee_name=assignee,
                    )
                )

            # Métadonnées des photos du chantier (sans charger les blobs).
            prows = (
                await db.execute(
                    select(
                        ProjectPhoto.id,
                        ProjectPhoto.caption,
                        ProjectPhoto.content_type,
                    )
                    .where(ProjectPhoto.project_id == proj.id)
                    .order_by(ProjectPhoto.created_at.desc())
                )
            ).all()
            photos_out = [
                _BonPhotoMeta(id=pid, caption=cap, content_type=ct)
                for pid, cap, ct in prows
            ]

    return _BonAvancementDetail(
        id=bon.id,
        reference=bon.reference,
        title=bon.title,
        description=bon.description,
        scope_md=bon.scope_md,
        status=bon.status,
        created_at=bon.created_at,
        sent_at=bon.sent_at,
        signed_at=bon.signed_at,
        client_name=client_name,
        project=proj_summary,
        phases=phases_out,
        photos=photos_out,
        work_notes=bon.work_notes,
        address=bon.address,
        is_urgent=bool(getattr(bon, "is_urgent", False)),
        immeuble_id=bon.immeuble_id,
        logement_id=bon.logement_id,
        created_by_name=created_by_name,
    )


@router.get("/bons-travail/{bon_id}/photos/{photo_id}")
async def get_gestion_immo_bon_photo(
    bon_id: int, photo_id: int, db: DBSession, user: CurrentUser
) -> Response:
    """Sert l'image d'une photo de chantier, pour un bon gestion immobilière.
    Passe par la porte immobilier : Kyle (sans volet construction) peut voir
    les photos d'avancement du chantier lié à SON bon, en lecture seule."""
    _require_volet(user)
    bon = await db.get(BonTravail, bon_id)
    if (
        bon is None
        or bon.project_id is None
        or not (
            (bon.kind or "construction") == "interne"
            or bon.origin == "gestion_immo"
            or (bon.scope_md and "Gestion immobilière" in bon.scope_md)
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bon introuvable."
        )
    # Logique partagée avec /bons-travail/{id}/photos (2026-08-21) : la
    # photo doit appartenir au chantier de CE bon.
    from app.services.bon_photos import charger_photo_bon

    res = await charger_photo_bon(db, bon, photo_id)
    if res is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Photo introuvable."
        )
    content, ct = res
    return Response(content=content, media_type=ct)


@router.post("/bons-travail/{bon_id}/photos")
async def upload_gestion_immo_bon_photo(
    bon_id: int,
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> dict:
    """Ajoute une photo (problématique « avant », ou « après ») à un bon de
    travail gestion immobilière. La photo est attachée au PROJET lié (mini-
    projet) ; on le crée à la volée si le bon n'en a pas encore. Accepte
    images + PDF."""
    _require_volet(user)
    bon = await db.get(BonTravail, bon_id)
    if bon is None or not (
        (bon.kind or "construction") == "interne"
        or bon.origin == "gestion_immo"
        or (bon.scope_md and "Gestion immobilière" in bon.scope_md)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bon introuvable."
        )

    # Logique partagée avec /bons-travail/{id}/photos (2026-08-21) :
    # validation, mini-projet porteur créé au besoin, enregistrement.
    from app.services.bon_photos import PhotoBonError, enregistrer_photo_bon

    blob = await file.read()
    try:
        photo = await enregistrer_photo_bon(
            db,
            bon,
            content_type=file.content_type,
            blob=blob,
            uploaded_by_email=user.email,
        )
    except PhotoBonError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    await db.commit()
    return {"photo_id": photo.id, "project_id": bon.project_id}


@router.post("/entreprises/{entreprise_id}/retirer-portefeuille")
async def retirer_entreprise_portefeuille(
    entreprise_id: int, db: DBSession, user: CurrentUser
) -> dict:
    """Retire l'entreprise du volet immobilier : supprime uniquement les
    liens de propriété (ImmeubleOwnership) entre cette entreprise et ses
    immeubles. NE touche PAS l'entreprise ni ses tâches côté gestion
    d'entreprise (séparation des volets). Les immeubles eux-mêmes restent
    (sans propriétaire — à réassigner au besoin)."""
    _require_volet(user)
    rows = (
        await db.execute(
            select(ImmeubleOwnership).where(
                ImmeubleOwnership.entreprise_id == entreprise_id
            )
        )
    ).scalars().all()
    for o in rows:
        await db.delete(o)
    await db.commit()
    return {"removed_ownerships": len(rows)}


# ── Signature de bail ──────────────────────────────────────────────────


@router.get("/baux/{bail_id}/document")
async def download_bail_document(
    bail_id: int,
    db: DBSession,
    user: CurrentUser,
) -> Response:
    """Ouvre LE bail courant — c'est la cible du clic sur un bail.

    Priorité au document importé/remplacé (``bail.document_id``, retour
    Phil 2026-07-27) ; sinon le PDF du bail signé EN LIGNE (régénéré à la
    volée). 409 si le bail n'a ni l'un ni l'autre.
    """
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Bail introuvable."
        )

    doc_id = getattr(bail, "document_id", None)
    if doc_id:
        from sqlalchemy.orm import undefer as _undefer

        from app.models.immobilier import ImmDocument as _ImmDocument

        d = (
            await db.execute(
                select(_ImmDocument)
                .options(_undefer(_ImmDocument.pdf_blob))
                .where(_ImmDocument.id == int(doc_id))
            )
        ).scalar_one_or_none()
        if d is not None and d.pdf_blob:
            fname = (getattr(d, "filename", None) or f"Bail_{bail_id}.pdf")
            return Response(
                content=d.pdf_blob,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{fname}"'
                },
            )

    # Les baux sont signés HORS de Kratos (CORPIQ papier/externe) : le
    # seul bail au dossier est celui qu'on IMPORTE. Le rendu d'un bail
    # « signé dans Kratos » a été retiré le 2026-08-19 — il n'a jamais
    # servi (aucune signature en base) et laissait croire qu'un bail
    # pouvait exister sans avoir été joint.
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail=(
            "Aucun bail au dossier — importe le bail signé "
            "(bouton « Importer le bail »)."
        ),
    )


# ── Logements ──────────────────────────────────────────────────────────


@router.get(
    "/immeubles/{immeuble_id}/logements",
    response_model=List[LogementRead],
)
async def list_logements(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> List[LogementRead]:
    _require_volet(user)
    rows = (
        await db.execute(
            select(Logement)
            .where(Logement.immeuble_id == immeuble_id)
            .order_by(Logement.numero.asc())
        )
    ).scalars().all()
    # Hiérarchie du loyer effectif (retour client 2026-08-14) : la liste
    # porte le loyer RÉEL du bail actif pour un logement occupé… SAUF en
    # gestion EXTERNE, où le loyer SAISI sur le logement est la vérité —
    # un bail résiduel (invisible, l'onglet Baux est caché en externe)
    # ne doit pas masquer la saisie. loyer_actuel reste alors vide et
    # toutes les surfaces retombent sur loyer_demande.
    imm = await db.get(Immeuble, immeuble_id)
    externe = bool(getattr(imm, "gestion_externe", False)) if imm else False
    loyer_actif: dict[int, Bail] = {}
    ids = [r.id for r in rows] if not externe else []
    if ids:
        today = _now().date()
        for b in (
            await db.execute(
                select(Bail).where(
                    Bail.logement_id.in_(ids),
                    Bail.status == BailStatus.ACTIF.value,
                )
            )
        ).scalars().all():
            if b.date_debut is not None and b.date_debut > today:
                continue  # actif futur = « prochain », pas courant
            cur = loyer_actif.get(b.logement_id)
            if cur is None or (
                (b.date_debut or date.min) > (cur.date_debut or date.min)
            ):
                loyer_actif[b.logement_id] = b
    # Départs ACTÉS : un logement occupé dont le locataire part le 31
    # août n'est pas dans le même état qu'un logement occupé tout court.
    from app.services.locatif_depart import libere_le

    out: List[LogementRead] = []
    for r in rows:
        lr = LogementRead.model_validate(r)
        b = loyer_actif.get(r.id)
        if b is not None and b.loyer_mensuel is not None:
            lr.loyer_actuel = float(b.loyer_mensuel)
        lr.libre_le = await libere_le(db, r.id)
        out.append(lr)
    return out


@router.post(
    "/logements",
    response_model=LogementRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_logement(
    payload: LogementCreate, db: DBSession, user: CurrentUser,
    force: bool = False,
) -> LogementRead:
    _require_volet(user)
    await _get_immeuble_or_404(db, payload.immeuble_id)
    # Doublons (retour Phil 2026-09-09 : « 8906-C » trois fois) : même
    # numéro (casse/espaces ignorés) dans le même immeuble → 409, sauf
    # ?force=true après un avertissement explicite.
    if not force:
        cle = _cle_numero(payload.numero)
        for lg_exist in (
            await db.execute(
                select(Logement).where(Logement.immeuble_id == payload.immeuble_id)
            )
        ).scalars().all():
            if _cle_numero(lg_exist.numero) == cle:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Le logement « {lg_exist.numero} » existe déjà dans cet "
                        f"immeuble (#{lg_exist.id}). Ouvre-le au lieu d'en créer "
                        "un deuxième."
                    ),
                )
    obj = Logement(**payload.model_dump())
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return LogementRead.model_validate(obj)


def _cle_numero(numero: Optional[str]) -> str:
    """Clé de comparaison d'un numéro de logement : casse, espaces et
    tirets ignorés (« 8906 - C », « 8906-C », « 8906 c » = le même)."""
    return "".join(ch for ch in (numero or "").lower() if ch.isalnum())


class LogementDoublonGroupe(BaseModel):
    immeuble_id: int
    immeuble_name: str
    numero: str
    logements: List[dict]


class FusionLogementsRequest(BaseModel):
    garder_id: int
    supprimer_ids: List[int] = Field(default_factory=list, min_length=1)


@router.get("/logements/doublons", response_model=List[LogementDoublonGroupe])
async def logements_doublons(db: DBSession, user: CurrentUser) -> List[LogementDoublonGroupe]:
    """Diagnostic : logements portant le même numéro dans le même
    immeuble (retour Phil 2026-09-09). Chaque groupe liste, par
    logement, ce qui y est rattaché — pour choisir lequel garder."""
    _require_volet(user)
    logements = (await db.execute(select(Logement))).scalars().all()
    imms = {
        i.id: i for i in (await db.execute(select(Immeuble))).scalars().all()
    }
    groupes: dict[tuple[int, str], list[Logement]] = {}
    for lg in logements:
        groupes.setdefault((lg.immeuble_id, _cle_numero(lg.numero)), []).append(lg)
    out: List[LogementDoublonGroupe] = []
    for (imm_id, _cle), lgs in groupes.items():
        if len(lgs) < 2:
            continue
        ids = [lg.id for lg in lgs]
        nb_baux: dict[int, int] = {}
        for lid, cnt in (
            await db.execute(
                select(Bail.logement_id, func.count(Bail.id))
                .where(Bail.logement_id.in_(ids))
                .group_by(Bail.logement_id)
            )
        ).all():
            nb_baux[int(lid)] = int(cnt)
        nb_ext: dict[int, int] = {}
        for lid, cnt in (
            await db.execute(
                select(PaiementExterne.logement_id, func.count(PaiementExterne.id))
                .where(PaiementExterne.logement_id.in_(ids))
                .group_by(PaiementExterne.logement_id)
            )
        ).all():
            nb_ext[int(lid)] = int(cnt)
        imm = imms.get(imm_id)
        out.append(
            LogementDoublonGroupe(
                immeuble_id=imm_id,
                immeuble_name=imm.name if imm else f"Immeuble #{imm_id}",
                numero=lgs[0].numero,
                logements=[
                    {
                        "id": lg.id,
                        "numero": lg.numero,
                        "status": lg.status,
                        "loyer_demande": float(lg.loyer_demande) if lg.loyer_demande is not None else None,
                        "nb_baux": nb_baux.get(lg.id, 0),
                        "nb_paiements_externes": nb_ext.get(lg.id, 0),
                        "created_at": lg.created_at.isoformat() if lg.created_at else None,
                    }
                    for lg in sorted(lgs, key=lambda x: x.id)
                ],
            )
        )
    out.sort(key=lambda g: (g.immeuble_name, g.numero))
    return out


@router.post("/logements/fusionner", response_model=LogementRead)
async def fusionner_logements(
    payload: FusionLogementsRequest, db: DBSession, user: CurrentUser
) -> LogementRead:
    """Fusionne des logements en double : tout ce qui est rattaché aux
    doublons (baux, paiements externes, documents, dossiers de
    relocation, relevés 31) est re-pointé sur le logement conservé, puis
    les doublons sont supprimés. Rien n'est effacé."""
    _require_volet(user)
    garder = await db.get(Logement, payload.garder_id)
    if garder is None:
        raise HTTPException(status_code=404, detail="Logement à conserver introuvable.")
    ids = [i for i in payload.supprimer_ids if i != payload.garder_id]
    if not ids:
        raise HTTPException(status_code=422, detail="Rien à fusionner.")
    doublons = (
        await db.execute(select(Logement).where(Logement.id.in_(ids)))
    ).scalars().all()
    if len(doublons) != len(set(ids)):
        raise HTTPException(status_code=404, detail="Un des doublons est introuvable.")
    for d in doublons:
        if d.immeuble_id != garder.immeuble_id:
            raise HTTPException(
                status_code=422,
                detail="On ne fusionne que des logements du même immeuble.",
            )
    from app.models.immobilier import ImmDocument, LocationDossier

    for d in doublons:
        await db.execute(
            update(Bail).where(Bail.logement_id == d.id).values(logement_id=garder.id)
        )
        # Paiements externes : le mois est unique par logement → on garde
        # la ligne du conservé si elle existe déjà.
        existants = {
            p.mois_couvert
            for p in (
                await db.execute(
                    select(PaiementExterne).where(PaiementExterne.logement_id == garder.id)
                )
            ).scalars().all()
        }
        for p in (
            await db.execute(
                select(PaiementExterne).where(PaiementExterne.logement_id == d.id)
            )
        ).scalars().all():
            if p.mois_couvert in existants:
                await db.delete(p)
            else:
                p.logement_id = garder.id
                existants.add(p.mois_couvert)
        await db.execute(
            update(ImmDocument).where(ImmDocument.logement_id == d.id).values(logement_id=garder.id)
        )
        await db.execute(
            update(LocationDossier).where(LocationDossier.logement_id == d.id).values(logement_id=garder.id)
        )
        try:
            from app.models.immobilier import Releve31

            await db.execute(
                update(Releve31).where(Releve31.logement_id == d.id).values(logement_id=garder.id)
            )
        except Exception:  # noqa: BLE001 — pas de relevé 31 dans ce déploiement
            pass
        # Le conservé hérite des infos manquantes.
        if garder.loyer_demande is None and d.loyer_demande is not None:
            garder.loyer_demande = d.loyer_demande
        if not garder.locataire_externe_nom and d.locataire_externe_nom:
            garder.locataire_externe_nom = d.locataire_externe_nom
        if garder.status == LogementStatus.VACANT.value and d.status == LogementStatus.OCCUPE.value:
            garder.status = LogementStatus.OCCUPE.value
        await db.flush()
        await db.delete(d)
    garder.updated_at = _now()
    await db.commit()
    await db.refresh(garder)
    return LogementRead.model_validate(garder)


@router.patch("/logements/{logement_id}", response_model=LogementRead)
async def update_logement(
    logement_id: int,
    payload: LogementUpdate,
    db: DBSession,
    user: CurrentUser,
) -> LogementRead:
    _require_volet(user)
    obj = await db.get(Logement, logement_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Logement introuvable.")
    etait_indefini = bool(getattr(obj, "location_en_chambres", False))
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(obj, k, v)
    obj.updated_at = _now()
    # « Louer indéfiniment (chambre) » qu'on COCHE : les baux en cours du
    # logement basculent AU MOIS (même loyer à l'infini, hors des
    # renouvellements). Qu'on DÉCOCHE : on ne touche à rien — le
    # gestionnaire décide bail par bail (retour Phil 2026-08-13).
    if data.get("location_en_chambres") and not etait_indefini:
        await db.execute(
            update(Bail)
            .where(
                Bail.logement_id == logement_id,
                Bail.status.in_(
                    [BailStatus.ACTIF.value, BailStatus.PROPOSE.value]
                ),
                Bail.au_mois.isnot(True),
            )
            .values(au_mois=True, updated_at=_now())
        )
    # M9a : passer le logement à « vacant » À LA MAIN ouvre son dossier
    # de relocation, comme toute mutation qui libère une unité (la
    # création ne vit plus dans le GET /locations/overview).
    if data.get("status") == LogementStatus.VACANT.value:
        from app.services.gestion_externe import (
            logement_est_externe,
            rendre_vacant_externe,
        )

        if await logement_est_externe(db, logement_id):
            # Gestion externe (2026-09-09) : « Départ » = vacant, nom
            # effacé, bail résiduel terminé — rien d'autre.
            await rendre_vacant_externe(db, obj)
        else:
            from app.services.locatif_depart import (
                ouvrir_dossiers_unites_vacantes,
            )

            await ouvrir_dossiers_unites_vacantes(db, [logement_id])
    await db.commit()
    await db.refresh(obj)
    return LogementRead.model_validate(obj)


@router.delete(
    "/logements/{logement_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_logement(
    logement_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("logement.delete"))],
) -> None:
    _require_volet(user)
    obj = await db.get(Logement, logement_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Logement introuvable.")
    # Garde-fou (audit 2026-07-31) : la cascade effacerait baux et
    # paiements — l'UI attendait déjà ce 409.
    nb_baux = (
        await db.execute(
            select(func.count(Bail.id)).where(
                Bail.logement_id == logement_id
            )
        )
    ).scalar_one()
    if nb_baux:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ce logement a {nb_baux} bail(aux) — supprime-les "
                "d'abord (leur historique de paiements partirait avec)."
            ),
        )
    await db.delete(obj)
    await db.commit()


async def _relocation_par_bail(
    db, bail_ids, inclure_sortant: bool = True
) -> dict:
    """bail_id -> dossier de relocation ACTIF lié (kanban Locations) —
    affiché partout où on voit les baux (retour Phil 2026-07-31).

    M1 (audit 2026-08-13) : le lien se fait par le bail ENTRANT
    (``nouveau_bail_id``) ET par le bail SORTANT (``bail_id``) — le
    bail du locataire qui part porte enfin sa pastille dans les fiches,
    comme la page Baux le fait via le logement. Valeur : dict
    ``{"statut": ..., "dossier_id": ...}`` (le dossier_id alimente le
    lien « Ouvrir dans Locations » ciblé)."""
    ids = [i for i in bail_ids if i]
    if not ids:
        return {}
    from app.models.immobilier import LocationDossier
    from app.services.locatif_depart import DOSSIER_STATUTS_REGLES

    out: dict = {}
    if inclure_sortant:
        for d in (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.bail_id.in_(ids),
                    LocationDossier.statut.notin_(
                        list(DOSSIER_STATUTS_REGLES)
                    ),
                )
            )
        ).scalars().all():
            out[d.bail_id] = {"statut": d.statut, "dossier_id": d.id}
    for d in (
        await db.execute(
            select(LocationDossier).where(
                LocationDossier.nouveau_bail_id.in_(ids),
                LocationDossier.statut.notin_(
                    list(DOSSIER_STATUTS_REGLES)
                ),
            )
        )
    ).scalars().all():
        # Le lien ENTRANT garde priorité (comportement historique).
        out[d.nouveau_bail_id] = {"statut": d.statut, "dossier_id": d.id}
    return out


@router.get(
    "/logements/{logement_id}/dossier", response_model=LogementDossier
)
async def logement_dossier(
    logement_id: int, db: DBSession, user: CurrentUser
) -> LogementDossier:
    """Fiche 360 d'un logement : infos + immeuble, tous ses baux (avec
    locataire, document, signature), les bons de travail rattachés
    (rénos / maintenance) et l'historique de loyer dérivé des baux
    (fluctuation de bail en bail)."""
    _require_volet(user)
    lg = await db.get(Logement, logement_id)
    if lg is None:
        raise HTTPException(status_code=404, detail="Logement introuvable.")
    await _require_immeuble_visible(db, user, lg.immeuble_id)
    imm = await db.get(Immeuble, lg.immeuble_id)

    baux = (
        await db.execute(
            select(Bail)
            .where(Bail.logement_id == logement_id)
            .order_by(Bail.date_debut.desc())
        )
    ).scalars().all()

    loc_ids = {b.locataire_id for b in baux if b.locataire_id}
    loc_by_id = {}
    if loc_ids:
        for loc in (
            await db.execute(
                select(Locataire).where(Locataire.id.in_(list(loc_ids)))
            )
        ).scalars().all():
            loc_by_id[loc.id] = loc

    reloc_par_bail = await _relocation_par_bail(db, [b.id for b in baux])
    dossier_baux = []
    for b in baux:
        loc = loc_by_id.get(b.locataire_id)
        reloc = reloc_par_bail.get(b.id)
        dossier_baux.append(
            LogementDossierBail(
                id=b.id,
                locataire=(
                    LogementDossierLocataire(
                        id=loc.id, full_name=loc.full_name
                    )
                    if loc is not None
                    else None
                ),
                loyer_mensuel=float(b.loyer_mensuel or 0),
                date_debut=b.date_debut,
                date_fin=b.date_fin,
                status=b.status,
                relocation_statut=reloc["statut"] if reloc else None,
                relocation_dossier_id=(
                    reloc["dossier_id"] if reloc else None
                ),
                document_url=b.document_url,
                signed_at=b.signed_at,
                document_id=b.document_id,
                au_mois=b.au_mois,
                jour_echeance=b.jour_echeance or 1,
            )
        )

    bons = (
        await db.execute(
            select(BonTravail)
            .where(BonTravail.logement_id == logement_id)
            .order_by(BonTravail.created_at.desc())
        )
    ).scalars().all()
    dossier_bons = [
        LogementDossierBon(
            id=b.id,
            reference=b.reference,
            title=b.title,
            status=b.status,
            montant=float(b.amount) if b.amount is not None else None,
            created_at=b.created_at,
        )
        for b in bons
    ]

    # Fluctuation : loyers de bail en bail, en ordre chronologique.
    historique = [
        LoyerPoint(
            date_debut=b.date_debut,
            loyer_mensuel=float(b.loyer_mensuel or 0),
        )
        for b in sorted(baux, key=lambda x: x.date_debut)
    ]

    return LogementDossier(
        logement=LogementRead.model_validate(lg),
        immeuble=LogementDossierImmeuble(
            id=(imm.id if imm else lg.immeuble_id),
            name=(
                (imm.name or imm.address) if imm
                else f"Immeuble #{lg.immeuble_id}"
            ),
            address=(imm.address if imm else None),
            gestion_externe=bool(
                getattr(imm, "gestion_externe", False)
            ) if imm else False,
        ),
        baux=dossier_baux,
        bons_travail=dossier_bons,
        historique_loyer=historique,
    )


# ── Locataires ─────────────────────────────────────────────────────────


@router.get("/locataires", response_model=List[LocataireListItem])
async def list_locataires(
    db: DBSession, user: CurrentUser, search: Optional[str] = None
) -> List[LocataireListItem]:
    _require_volet(user)
    q = select(Locataire).order_by(Locataire.full_name.asc())
    rows = list((await db.execute(q)).scalars().all())

    # Recherche (2026-09-09) : nom, courriel, téléphone (chiffres) ET les
    # garants/contacts du locataire — accents insensibles, filtrée en
    # Python après chargement (volumes < 1 000 : simplicité avant
    # performance). ``match_via`` dit POURQUOI la fiche remonte quand ce
    # n'est pas son nom (« garant : Jacques Roy »).
    match_via: dict[int, str] = {}
    if search and search.strip():
        needle = normaliser(search)
        needle_chiffres = chiffres(search)
        contacts = await contacts_par_locataire(db, [r.id for r in rows])
        gardes = []
        for r in rows:
            if needle and needle in normaliser(r.full_name):
                gardes.append(r)
                continue
            if needle and r.email and needle in normaliser(r.email):
                match_via[r.id] = f"courriel : {r.email}"
                gardes.append(r)
                continue
            if (
                needle_chiffres
                and len(needle_chiffres) >= 3
                and needle_chiffres in chiffres(r.phone)
            ):
                match_via[r.id] = f"téléphone : {r.phone}"
                gardes.append(r)
                continue
            via = contact_qui_matche(
                contacts.get(r.id, []), needle, needle_chiffres
            )
            if via:
                match_via[r.id] = via
                gardes.append(r)
        rows = gardes

    # Immeuble/logement du bail ACTIF le plus récent de chaque locataire
    # (colonnes cliquables de la page Locataires).
    habite: dict[int, tuple[Logement, Immeuble]] = {}
    if rows:
        baux_actifs = (
            await db.execute(
                select(Bail)
                .where(
                    Bail.locataire_id.in_([r.id for r in rows]),
                    Bail.status == BailStatus.ACTIF.value,
                    # Un bail échu (non au mois) n'héberge plus personne
                    # (Maritza partie le 1er, encore « là » le 2 —
                    # retour Phil 2026-09-09).
                    or_(
                        Bail.au_mois.is_(True),
                        Bail.date_fin.is_(None),
                        Bail.date_fin >= _now().date(),
                    ),
                )
                .order_by(Bail.date_debut.asc())
            )
        ).scalars().all()
        log_ids = {b.logement_id for b in baux_actifs}
        logs = {}
        imms = {}
        if log_ids:
            for lg in (
                await db.execute(
                    select(Logement).where(Logement.id.in_(list(log_ids)))
                )
            ).scalars().all():
                logs[lg.id] = lg
            imm_ids = {lg.immeuble_id for lg in logs.values()}
            for im in (
                await db.execute(
                    select(Immeuble).where(Immeuble.id.in_(list(imm_ids)))
                )
            ).scalars().all():
                imms[im.id] = im
        # Tri ascendant → le plus récent écrase les précédents.
        for b in baux_actifs:
            lg = logs.get(b.logement_id)
            im = imms.get(lg.immeuble_id) if lg else None
            if lg and im:
                habite[b.locataire_id] = (lg, im)

    out: List[LocataireListItem] = []
    for r in rows:
        item = LocataireListItem.model_validate(r)
        item.match_via = match_via.get(r.id)
        pair = habite.get(r.id)
        if pair:
            lg, im = pair
            item.logement_id = lg.id
            item.logement_numero = lg.numero
            item.immeuble_id = im.id
            item.immeuble_name = im.name
        out.append(item)
    return out


def _cle_courriel(v: Optional[str]) -> str:
    """Courriel normalisé pour comparaison : trim + minuscules."""
    return (v or "").strip().lower()


def _cle_telephone(v: Optional[str]) -> str:
    """Téléphone réduit à ses chiffres, 10 derniers retenus.

    Les numéros sont saisis à la main dans tous les formats — « 514
    555-1234 », « (514) 555-1234 », « 5145551234 », parfois avec le 1
    de longue distance. Les 10 derniers chiffres sont le numéro
    canadien ; en dessous de 7, on considère la saisie inutilisable
    (poste interne, numéro tronqué) et on ne compare pas."""
    chiffres = re.sub(r"\D", "", v or "")
    if len(chiffres) < 7:
        return ""
    return chiffres[-10:]


class LocataireDoublon(BaseModel):
    """Fiche existante qui ressemble à celle qu'on est en train de créer."""

    id: int
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    #: Ce qui a matché : 'courriel' | 'téléphone' | 'courriel + téléphone'.
    motif: str
    #: Logement/bail ACTIF le plus récent, s'il y en a un — permet de
    #: reconnaître la fiche du premier coup d'œil.
    immeuble_id: Optional[int] = None
    immeuble_name: Optional[str] = None
    logement_id: Optional[int] = None
    logement_numero: Optional[str] = None
    bail_id: Optional[int] = None


@router.get("/locataires/doublons", response_model=List[LocataireDoublon])
async def locataires_doublons(
    db: DBSession,
    user: CurrentUser,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    exclure_id: Optional[int] = None,
) -> List[LocataireDoublon]:
    """Fiches existantes portant le MÊME courriel ou le MÊME téléphone.

    Sert d'alerte au moment de créer un locataire (retour Phil
    2026-08-13) : 6 paires de fiches en double ont dû être fusionnées à
    la main la semaine dernière. Purement INFORMATIF — la création n'est
    jamais bloquée, le staff garde le dernier mot.

    La comparaison se fait sur les formes normalisées : le SQL ne sait
    pas retirer la ponctuation d'un téléphone de façon portable, alors
    les fiches avec un téléphone sont ramenées et comparées en Python
    (quelques centaines de lignes — pas de quoi pagner)."""
    _require_volet(user)
    cle_mail = _cle_courriel(email)
    cle_tel = _cle_telephone(phone)
    if not cle_mail and not cle_tel:
        return []

    conditions = []
    if cle_mail:
        conditions.append(
            func.lower(func.coalesce(Locataire.email, "")) == cle_mail
        )
    if cle_tel:
        conditions.append(func.coalesce(Locataire.phone, "") != "")
    query = select(Locataire).where(or_(*conditions))
    if exclure_id is not None:
        query = query.where(Locataire.id != exclure_id)
    candidats = (await db.execute(query)).scalars().all()

    trouves: List[tuple] = []
    for loc in candidats:
        par_mail = bool(cle_mail) and _cle_courriel(loc.email) == cle_mail
        par_tel = bool(cle_tel) and _cle_telephone(loc.phone) == cle_tel
        if not par_mail and not par_tel:
            continue
        if par_mail and par_tel:
            motif = "courriel + téléphone"
        else:
            motif = "courriel" if par_mail else "téléphone"
        trouves.append((loc, motif))
    if not trouves:
        return []

    # Où habitent-ils aujourd'hui ? (bail actif le plus récent) — c'est
    # ce qui permet de dire « ah oui, c'est le 44 Kennedy 101 ».
    habite: dict[int, tuple] = {}
    baux = (
        await db.execute(
            select(Bail)
            .where(
                Bail.locataire_id.in_([lo.id for lo, _m in trouves]),
                Bail.status == BailStatus.ACTIF.value,
            )
            .order_by(Bail.date_debut.asc())
        )
    ).scalars().all()
    if baux:
        logs = {
            lg.id: lg
            for lg in (
                await db.execute(
                    select(Logement).where(
                        Logement.id.in_([b.logement_id for b in baux])
                    )
                )
            ).scalars().all()
        }
        imms = {
            im.id: im
            for im in (
                await db.execute(
                    select(Immeuble).where(
                        Immeuble.id.in_(
                            [lg.immeuble_id for lg in logs.values()]
                        )
                    )
                )
            ).scalars().all()
        }
        # Tri ascendant → le bail le plus récent écrase les précédents.
        for b in baux:
            lg = logs.get(b.logement_id)
            im = imms.get(lg.immeuble_id) if lg else None
            if lg and im:
                habite[b.locataire_id] = (lg, im, b)

    out: List[LocataireDoublon] = []
    for loc, motif in trouves:
        pair = habite.get(loc.id)
        out.append(
            LocataireDoublon(
                id=loc.id,
                full_name=loc.full_name,
                email=loc.email,
                phone=loc.phone,
                motif=motif,
                logement_id=pair[0].id if pair else None,
                logement_numero=pair[0].numero if pair else None,
                immeuble_id=pair[1].id if pair else None,
                immeuble_name=pair[1].name if pair else None,
                bail_id=pair[2].id if pair else None,
            )
        )
    out.sort(key=lambda d: d.full_name.lower())
    return out


@router.post(
    "/locataires",
    response_model=LocataireRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_locataire(
    payload: LocataireCreate, db: DBSession, user: CurrentUser
) -> LocataireRead:
    """Crée la fiche. Volontairement SANS blocage sur doublon : l'UI
    interroge d'abord ``GET /locataires/doublons`` et affiche l'alerte,
    mais le staff peut toujours créer quand même (vrais homonymes,
    colocataires partageant un courriel de ménage…)."""
    _require_volet(user)
    obj = Locataire(**payload.model_dump())
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return LocataireRead.model_validate(obj)


@router.get("/locataires/{locataire_id}", response_model=LocataireRead)
async def get_locataire(
    locataire_id: int, db: DBSession, user: CurrentUser
) -> LocataireRead:
    _require_volet(user)
    obj = await db.get(Locataire, locataire_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    return LocataireRead.model_validate(obj)


@router.patch("/locataires/{locataire_id}", response_model=LocataireRead)
async def update_locataire(
    locataire_id: int,
    payload: LocataireUpdate,
    db: DBSession,
    user: CurrentUser,
) -> LocataireRead:
    _require_volet(user)
    obj = await db.get(Locataire, locataire_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(obj, k, v)
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return LocataireRead.model_validate(obj)


@router.delete(
    "/locataires/{locataire_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_locataire(
    locataire_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("locataire.delete"))],
    force: bool = False,
) -> None:
    """Supprime un locataire. Ses baux (FK RESTRICT) bloquent la
    suppression → 409 avec le compte ; ``force=true`` supprime AUSSI ses
    baux (et en cascade leurs paiements, renouvellements, relances,
    documents) — retour Phil 2026-07-20 (« je ne peux pas deleter »)."""
    _require_volet(user)
    obj = await db.get(Locataire, locataire_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    baux = (
        await db.execute(
            select(Bail).where(Bail.locataire_id == locataire_id)
        )
    ).scalars().all()
    if baux and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ce locataire a {len(baux)} bail(aux) — supprimer aussi "
                "ses baux et documents ? (force=true)"
            ),
        )
    # Garde-fou (audit 2026-07-31) : même sous force, l'historique de
    # PAIEMENTS ne se rase pas — même règle que le désistement.
    if baux:
        nb_paiements = (
            await db.execute(
                select(func.count(PaiementLoyer.id)).where(
                    PaiementLoyer.bail_id.in_([b.id for b in baux])
                )
            )
        ).scalar_one()
        if nb_paiements:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{nb_paiements} paiement(s) sont enregistrés sur "
                    "ses baux — retire-les d'abord (l'historique "
                    "financier ne se supprime pas en bloc)."
                ),
            )
    from app.models.immobilier import LocationDossier, LocationVisite

    bail_ids = [b.id for b in baux]
    logement_ids = {b.logement_id for b in baux if b.logement_id}
    # Dossiers de relocation pointant ces baux : régressés (sinon
    # statut kanban fantôme après le SET NULL de la FK).
    if bail_ids:
        for dsr in (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.nouveau_bail_id.in_(bail_ids)
                )
            )
        ).scalars().all():
            if dsr.statut in (
                "bail_a_envoyer", "bail_envoye", "reloue",
            ):
                dsr.statut = "avis_recu"
            dsr.reloue_le = None
            dsr.updated_at = _now()
    # Visites liées à cette fiche : dé-pointées (sinon 404 à la
    # prochaine conversion du candidat).
    for v in (
        await db.execute(
            select(LocationVisite).where(
                LocationVisite.locataire_id == locataire_id
            )
        )
    ).scalars().all():
        v.locataire_id = None
    for b in baux:
        # M6 : les dossiers de relocation dont ce bail est le SORTANT
        # gardent leurs repères (date de départ, ancien loyer) + note.
        await _consigner_suppression_bail_sortant(db, b)
        await db.delete(b)
    await db.delete(obj)
    await db.flush()
    for lg_id in logement_ids:
        await _recaler_logement_apres_bail(db, lg_id)
    # M9a : logements redevenus vacants sans dossier actif → la
    # mutation ouvre les dossiers (plus de création dans un GET).
    if logement_ids:
        from app.services.locatif_depart import (
            ouvrir_dossiers_unites_vacantes,
        )

        await ouvrir_dossiers_unites_vacantes(db, list(logement_ids))
    await db.commit()


@router.get(
    "/locataires/{locataire_id}/dossier", response_model=LocataireDossier
)
async def locataire_dossier(
    locataire_id: int, db: DBSession, user: CurrentUser
) -> LocataireDossier:
    """Fiche 360 d'un locataire : ses baux (avec immeuble/logement),
    l'historique complet de ses paiements de loyer et des agrégats."""
    _require_volet(user)
    loc = await db.get(Locataire, locataire_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")

    baux = (
        await db.execute(
            select(Bail)
            .where(Bail.locataire_id == locataire_id)
            .order_by(Bail.date_debut.desc())
        )
    ).scalars().all()

    log_ids = {b.logement_id for b in baux if b.logement_id}
    log_by_id = {}
    if log_ids:
        for lg in (
            await db.execute(
                select(Logement).where(Logement.id.in_(list(log_ids)))
            )
        ).scalars().all():
            log_by_id[lg.id] = lg
    imm_ids = {lg.immeuble_id for lg in log_by_id.values()}
    imm_by_id = {}
    if imm_ids:
        for im in (
            await db.execute(
                select(Immeuble).where(Immeuble.id.in_(list(imm_ids)))
            )
        ).scalars().all():
            imm_by_id[im.id] = im

    reloc_par_bail = await _relocation_par_bail(db, [b.id for b in baux])
    dossier_baux = []
    for b in baux:
        lg = log_by_id.get(b.logement_id)
        im = imm_by_id.get(lg.immeuble_id) if lg else None
        reloc = reloc_par_bail.get(b.id)
        dossier_baux.append(
            DossierBail(
                id=b.id,
                immeuble_id=(im.id if im else 0),
                immeuble_name=(im.name if im else "—"),
                logement_id=(lg.id if lg else None),
                logement_numero=(lg.numero if lg else None),
                date_debut=b.date_debut,
                date_fin=b.date_fin,
                loyer_mensuel=float(b.loyer_mensuel or 0),
                depot_garantie=(
                    float(b.depot_garantie)
                    if b.depot_garantie is not None
                    else None
                ),
                status=b.status,
                relocation_statut=reloc["statut"] if reloc else None,
                relocation_dossier_id=(
                    reloc["dossier_id"] if reloc else None
                ),
                document_id=b.document_id,
                signed_at=b.signed_at,
                au_mois=b.au_mois,
                jour_echeance=b.jour_echeance or 1,
            )
        )

    # Historique d'argent : à partir du DÉMARRAGE du pôle seulement — ce
    # qui précède ne compte plus (totaux, retards, solde).
    depuis = await get_demarrage()
    paiements = []
    bail_ids = [b.id for b in baux]
    if bail_ids:
        for pmt in (
            await db.execute(
                select(PaiementLoyer)
                .where(
                    PaiementLoyer.bail_id.in_(bail_ids),
                    PaiementLoyer.mois_couvert >= depuis,
                )
                .order_by(PaiementLoyer.mois_couvert.desc())
            )
        ).scalars().all():
            paiements.append(
                DossierPaiement(
                    id=pmt.id,
                    bail_id=pmt.bail_id,
                    mois_couvert=pmt.mois_couvert,
                    montant=float(pmt.montant or 0),
                    paye_le=pmt.paye_le,
                    methode=pmt.methode,
                    en_retard=bool(pmt.en_retard),
                )
            )

    # Avis d'augmentation / renouvellements de tous ses baux — le hub
    # locataire montre tout ce qui lui a été envoyé et où ça en est.
    renouvellements: list[DossierRenouvellement] = []
    if bail_ids:
        bail_by_id = {b.id: b for b in baux}
        for r in (
            await db.execute(
                select(BailRenouvellement)
                .where(BailRenouvellement.bail_id.in_(bail_ids))
                .order_by(BailRenouvellement.avis_envoye_le.desc())
            )
        ).scalars().all():
            rb = bail_by_id.get(r.bail_id)
            rlg = log_by_id.get(rb.logement_id) if rb else None
            rim = imm_by_id.get(rlg.immeuble_id) if rlg else None
            renouvellements.append(
                DossierRenouvellement(
                    id=r.id,
                    bail_id=r.bail_id,
                    immeuble_name=(rim.name if rim else "—"),
                    logement_numero=(rlg.numero if rlg else None),
                    avis_envoye_le=r.avis_envoye_le,
                    nouveau_loyer=(
                        float(r.nouveau_loyer)
                        if r.nouveau_loyer is not None
                        else None
                    ),
                    nouvelle_date_debut=r.nouvelle_date_debut,
                    nouvelle_date_fin=r.nouvelle_date_fin,
                    status=r.status,
                    locataire_repondu_le=r.locataire_repondu_le,
                    notes=r.notes,
                    document_id=r.document_id,
                )
            )

    communications = [
        LocataireCommunicationRead.model_validate(c)
        for c in (
            await db.execute(
                select(LocataireCommunication)
                .where(LocataireCommunication.locataire_id == locataire_id)
                .order_by(LocataireCommunication.created_at.desc())
            )
        ).scalars().all()
    ]

    actifs = [b for b in baux if b.status == BailStatus.ACTIF.value]
    return LocataireDossier(
        locataire=LocataireRead.model_validate(loc),
        baux=dossier_baux,
        paiements=paiements,
        renouvellements=renouvellements,
        communications=communications,
        nb_baux_actifs=len(actifs),
        loyer_actuel=round(sum(float(b.loyer_mensuel or 0) for b in actifs), 2),
        depot_total=round(
            sum(float(b.depot_garantie or 0) for b in actifs), 2
        ),
        total_paye=round(
            sum(p.montant for p in paiements if p.paye_le is not None), 2
        ),
        nb_paiements=sum(1 for p in paiements if p.paye_le is not None),
        nb_retards=sum(1 for p in paiements if p.en_retard),
    )


# ─── Journal de communications du locataire (manuel) ────────────────────
# Pas de lien téléphonie (demande Phil 2026-07-10) : l'employé consigne
# lui-même appels/courriels/notes depuis la fiche du locataire.


@router.post(
    "/locataires/{locataire_id}/communications",
    response_model=LocataireCommunicationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_locataire_communication(
    locataire_id: int,
    payload: LocataireCommunicationCreate,
    db: DBSession,
    user: CurrentUser,
) -> LocataireCommunicationRead:
    _require_volet(user)
    loc = await db.get(Locataire, locataire_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    contenu = (payload.contenu or "").strip()
    if not contenu:
        raise HTTPException(status_code=422, detail="Contenu requis.")
    kind = payload.kind if payload.kind in (
        "note", "appel", "courriel", "sms", "visite", "autre"
    ) else "note"
    obj = LocataireCommunication(
        locataire_id=locataire_id,
        kind=kind,
        contenu=contenu,
        auteur=getattr(user, "full_name", None) or user.email,
    )
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return LocataireCommunicationRead.model_validate(obj)


@router.delete(
    "/locataires/communications/{comm_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_locataire_communication(
    comm_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await db.get(LocataireCommunication, comm_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Entrée introuvable.")
    await db.delete(obj)
    await db.commit()


# ─── Prélèvement préautorisé ───────────────────────────────────────────
# Retiré le 2026-08-19 : la documentation DPA « maison » (formulaire
# d'accord + envoi manuel + suivi de statut) ne collectait rien — elle
# ne faisait que de la paperasse. La perception réelle passera par
# Rotessa, qui recueille lui-même l'autorisation et les coordonnées
# bancaires (décision Phil et ses partenaires, 2026-08-19). Laisser une
# demi-fonctionnalité en place aurait fait croire à un prélèvement qui
# n'existe pas. Les colonnes dpa_* du modèle restent, inertes.


def _fmt_money_pdf(n) -> str:
    return f"{float(n or 0):,.0f} $".replace(",", "\u00a0")


def _render_etat_de_compte(
    loc, baux, log_by_id, imm_by_id, paiements,
    loyer_actuel, depot_total, total_paye,
):
    """Rend l'état de compte d'un locataire en PDF (reportlab)."""
    import io as _io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=2 * cm, bottomMargin=2 * cm,
        leftMargin=2 * cm, rightMargin=2 * cm,
        title="État de compte",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "h1", parent=styles["Heading1"], fontSize=18,
        textColor=colors.HexColor("#0f172a"),
    )
    h2 = ParagraphStyle(
        "h2", parent=styles["Heading2"], fontSize=12, spaceBefore=10,
        textColor=colors.HexColor("#0369a1"),
    )
    normal = styles["Normal"]
    small = ParagraphStyle(
        "small", parent=normal, fontSize=8,
        textColor=colors.HexColor("#64748b"),
    )
    today = datetime.now(timezone.utc).date()
    flow = [
        Paragraph("État de compte locataire", h1),
        Paragraph(
            f"Horizon Services Immobiliers — généré le {today.isoformat()}",
            small,
        ),
        Spacer(1, 0.4 * cm),
        Paragraph(loc.full_name or "—", h2),
    ]
    coords = [x for x in (loc.email, loc.phone) if x]
    if coords:
        flow.append(Paragraph(" · ".join(coords), normal))
    flow.append(Spacer(1, 0.3 * cm))

    grid = colors.HexColor("#cbd5e1")
    head_bg = colors.HexColor("#0369a1")

    summary = Table(
        [
            ["Loyer mensuel actuel", _fmt_money_pdf(loyer_actuel)],
            ["Dépôt de garantie détenu", _fmt_money_pdf(depot_total)],
            ["Total des loyers encaissés", _fmt_money_pdf(total_paye)],
        ],
        colWidths=[9 * cm, 4 * cm],
    )
    summary.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, grid),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    flow.append(summary)

    # Baux
    flow.append(Paragraph("Baux", h2))
    bail_rows = [["Immeuble / logement", "Période", "Loyer", "Dépôt", "Statut"]]
    for b in baux:
        lg = log_by_id.get(b.logement_id)
        im = imm_by_id.get(lg.immeuble_id) if lg else None
        name = (im.name if im else "—")
        if lg and lg.numero:
            name = f"{name} · {lg.numero}"
        bail_rows.append([
            name,
            f"{b.date_debut} → {b.date_fin}",
            _fmt_money_pdf(b.loyer_mensuel),
            _fmt_money_pdf(b.depot_garantie) if b.depot_garantie else "—",
            b.status,
        ])
    bt = Table(bail_rows, colWidths=[6 * cm, 4 * cm, 2.5 * cm, 2.2 * cm, 2.3 * cm])
    bt.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, grid),
        ("BACKGROUND", (0, 0), (-1, 0), head_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (2, 1), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(bt)

    # Paiements
    flow.append(Paragraph("Historique de paiements", h2))
    pay_rows = [["Mois couvert", "Montant", "Payé le", "Méthode", "État"]]
    for pm in paiements[:36]:
        if pm.paye_le is None:
            etat = "Impayé"
        elif pm.en_retard:
            etat = "Payé en retard"
        else:
            etat = "Payé"
        pay_rows.append([
            pm.mois_couvert.strftime("%Y-%m"),
            _fmt_money_pdf(pm.montant),
            pm.paye_le.isoformat() if pm.paye_le else "—",
            pm.methode or "—",
            etat,
        ])
    if len(pay_rows) == 1:
        pay_rows.append(["—", "—", "—", "—", "Aucun paiement"])
    pt = Table(pay_rows, colWidths=[3 * cm, 2.6 * cm, 3 * cm, 3 * cm, 3.4 * cm])
    pt.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, grid),
        ("BACKGROUND", (0, 0), (-1, 0), head_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 1), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(pt)

    flow.append(Spacer(1, 0.6 * cm))
    flow.append(Paragraph(
        "Document généré automatiquement par Kratos. Pour toute question, "
        "communiquez avec votre gestionnaire.",
        small,
    ))
    doc.build(flow)
    return buf.getvalue()


@router.get("/locataires/{locataire_id}/etat-de-compte.pdf")
async def locataire_etat_de_compte_pdf(
    locataire_id: int, db: DBSession, user: CurrentUser
) -> Response:
    """État de compte du locataire en PDF (baux, paiements, dépôt, solde)."""
    _require_volet(user)
    loc = await db.get(Locataire, locataire_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")

    baux = (
        await db.execute(
            select(Bail)
            .where(Bail.locataire_id == locataire_id)
            .order_by(Bail.date_debut.desc())
        )
    ).scalars().all()
    log_ids = {b.logement_id for b in baux if b.logement_id}
    log_by_id = {}
    if log_ids:
        for lg in (
            await db.execute(
                select(Logement).where(Logement.id.in_(list(log_ids)))
            )
        ).scalars().all():
            log_by_id[lg.id] = lg
    imm_ids = {lg.immeuble_id for lg in log_by_id.values()}
    imm_by_id = {}
    if imm_ids:
        for im in (
            await db.execute(
                select(Immeuble).where(Immeuble.id.in_(list(imm_ids)))
            )
        ).scalars().all():
            imm_by_id[im.id] = im

    bail_ids = [b.id for b in baux]
    paiements = []
    if bail_ids:
        # Même borne que la fiche : rien avant le démarrage du pôle.
        depuis = await get_demarrage()
        paiements = (
            await db.execute(
                select(PaiementLoyer)
                .where(
                    PaiementLoyer.bail_id.in_(bail_ids),
                    PaiementLoyer.mois_couvert >= depuis,
                )
                .order_by(PaiementLoyer.mois_couvert.desc())
            )
        ).scalars().all()

    actifs = [b for b in baux if b.status == BailStatus.ACTIF.value]
    # P-14 « PDF = reflet de la fiche » : on calcule les totaux EXACTEMENT
    # comme la fiche dossier locataire (même filtre baux actifs / paye_le +
    # round(2)), pour qu'aucun montant ne puisse diverger entre l'écran et
    # le PDF remis au locataire.
    loyer_actuel = round(sum(float(b.loyer_mensuel or 0) for b in actifs), 2)
    depot_total = round(sum(float(b.depot_garantie or 0) for b in actifs), 2)
    total_paye = round(
        sum(float(p.montant or 0) for p in paiements if p.paye_le is not None),
        2,
    )

    pdf = _render_etat_de_compte(
        loc, baux, log_by_id, imm_by_id, paiements,
        loyer_actuel, depot_total, total_paye,
    )
    safe = "".join(
        c if c.isalnum() else "-" for c in (loc.full_name or str(locataire_id))
    ).strip("-") or str(locataire_id)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="etat-de-compte-{safe}.pdf"'
        },
    )


# ── Dépôts de garantie ─────────────────────────────────────────────────


class DepotRow(BaseModel):
    bail_id: int
    immeuble_id: int
    immeuble_name: str
    logement_id: Optional[int] = None
    logement_numero: Optional[str] = None
    locataire_id: Optional[int] = None
    locataire_name: Optional[str] = None
    montant: float
    # "detenu" | "a_rendre" | "rendu" | "aucun" (bail actif sans dépôt
    # saisi — permet de le saisir directement depuis la page Dépôts).
    statut: str
    #: Quand le dépôt a été reçu, et chez qui l'argent dort.
    depot_recu_le: Optional[date] = None
    depot_detenteur: Optional[str] = None
    depot_rendu_le: Optional[date] = None
    date_debut: date
    date_fin: date


class DepotOverview(BaseModel):
    rows: List[DepotRow] = []
    total_detenu: float = 0.0
    total_a_rendre: float = 0.0
    nb_a_rendre: int = 0
    total_rendu: float = 0.0
    nb_sans_depot: int = 0


@router.get("/depots/overview", response_model=DepotOverview)
async def depots_overview(
    db: DBSession,
    user: CurrentUser,
    entreprise_id: Optional[int] = None,
    immeuble_id: Optional[int] = None,
) -> DepotOverview:
    """Suivi des dépôts de garantie : détenus vs à rendre.

    Un dépôt n'est « à rendre » que si le locataire est PARTI — la fin
    d'un bail ne suffit pas (au Québec il se reconduit tacitement). On
    le sait de deux façons : le logement a été reloué à quelqu'un
    d'autre, ou son départ était acté et la date est passée.
    """
    _require_volet(user)
    today = _now().date()

    # Gestion externe : dépôts suivis par le gestionnaire tiers → exclu
    # (isnot(True) couvre les NULL legacy).
    imm_q = select(Immeuble).where(Immeuble.gestion_externe.isnot(True))
    if entreprise_id is not None:
        imm_q = imm_q.where(Immeuble.owner_entreprise_id == int(entreprise_id))
    if immeuble_id is not None:
        imm_q = imm_q.where(Immeuble.id == int(immeuble_id))
    immeubles = (await db.execute(imm_q)).scalars().all()
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = [i for i in immeubles if i.id in visible]
    imm_by_id = {i.id: i for i in immeubles}
    if not imm_by_id:
        return DepotOverview(rows=[])

    logements = (
        await db.execute(
            select(Logement).where(
                Logement.immeuble_id.in_(list(imm_by_id.keys()))
            )
        )
    ).scalars().all()
    log_by_id = {l.id: l for l in logements}
    if not log_by_id:
        return DepotOverview(rows=[])

    # TOUS les baux visibles — y compris les actifs SANS dépôt saisi
    # (statut « aucun ») pour permettre la saisie depuis la page Dépôts.
    baux = (
        await db.execute(
            select(Bail).where(
                Bail.logement_id.in_(list(log_by_id.keys())),
            )
        )
    ).scalars().all()

    loc_ids = {b.locataire_id for b in baux if b.locataire_id}
    loc_by_id = {}
    if loc_ids:
        for lo in (
            await db.execute(
                select(Locataire).where(Locataire.id.in_(list(loc_ids)))
            )
        ).scalars().all():
            loc_by_id[lo.id] = lo

    a_rendre_status = {
        BailStatus.TERMINE.value,
        BailStatus.RESILIE.value,
    }
    # Départs ACTÉS dont la date est passée : le locataire est parti,
    # qu'on ait reloué ou non.
    baux_partis: set[int] = set()
    if baux:
        from app.models.immobilier import LocationDossier
        from app.services.locatif_depart import DOSSIER_STATUTS_REGLES

        dossiers = (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.logement_id.in_(
                        {b.logement_id for b in baux}
                    )
                )
            )
        ).scalars().all()
        for d in dossiers:
            depart = d.date_depart
            if depart is None or depart > today:
                continue
            if d.statut in DOSSIER_STATUTS_REGLES and (
                d.statut != "reloue"
            ):
                continue  # dossier annulé : le locataire est resté
            for b in baux:
                if b.logement_id == d.logement_id and (
                    d.bail_id is None or d.bail_id == b.id
                ):
                    baux_partis.add(b.id)
    # Baux ACTIFS par logement — pour savoir si un logement a été REMIS
    # EN LOCATION (c'est ça, et pas la fin du bail, qui déclenche le
    # remboursement du dépôt de l'ancien locataire).
    actifs_par_logement: Dict[int, List[Bail]] = {}
    for b in baux:
        if b.status == BailStatus.ACTIF.value:
            actifs_par_logement.setdefault(b.logement_id, []).append(b)
    rows: List[DepotRow] = []
    total_detenu = 0.0
    total_a_rendre = 0.0
    total_rendu = 0.0
    for b in baux:
        lg = log_by_id.get(b.logement_id)
        im = imm_by_id.get(lg.immeuble_id) if lg else None
        if im is None:
            continue
        montant = float(b.depot_garantie or 0)
        if montant <= 0:
            # Bail sans dépôt : on n'affiche que les ACTIFS, comme ligne
            # « à saisir » — les baux passés sans dépôt n'apportent rien.
            if b.status != BailStatus.ACTIF.value:
                continue
            statut = "aucun"
        elif b.depot_rendu_le is not None:
            statut = "rendu"
            total_rendu += montant
        elif b.status in a_rendre_status and (
            any(
                nb.id != b.id and nb.locataire_id != b.locataire_id
                for nb in actifs_par_logement.get(b.logement_id, [])
            )
            or b.id in baux_partis
        ):
            # Retour Phil 2026-07-30 : la fin du bail ne suffit pas —
            # c'est le DÉPART qui veut dire que l'ancien locataire est
            # parti. Deux façons de le savoir :
            #   - le logement a été reloué à quelqu'un d'autre ;
            #   - ou son départ était ACTÉ et la date est passée.
            #
            # Le second cas a été ajouté le 2026-08-19 : attendre la
            # relocation retardait l'alerte de plusieurs semaines, alors
            # que le dépôt est dû au locataire dès son départ. C'est
            # précisément l'oubli que Phil voulait attraper — « il
            # oublie tout le temps de venir l'enlever à la fin ».
            statut = "a_rendre"
            total_a_rendre += montant
        else:
            # Bail actif, ou terminé sans relocation (le locataire est
            # probablement encore là) : l'argent est toujours détenu.
            statut = "detenu"
            total_detenu += montant
        loc = loc_by_id.get(b.locataire_id)
        rows.append(DepotRow(
            bail_id=b.id,
            immeuble_id=im.id,
            immeuble_name=im.name,
            logement_id=(lg.id if lg else None),
            logement_numero=(lg.numero if lg else None),
            locataire_id=loc.id if loc else None,
            locataire_name=loc.full_name if loc else None,
            montant=montant,
            statut=statut,
            depot_recu_le=b.depot_recu_le,
            depot_detenteur=b.depot_detenteur,
            depot_rendu_le=b.depot_rendu_le,
            date_debut=b.date_debut,
            date_fin=b.date_fin,
        ))

    rank = {"a_rendre": 0, "detenu": 1, "aucun": 2, "rendu": 3}

    def _cle_tri(r: DepotRow):
        # Les rendus vont tout en bas, du plus récemment rendu au plus
        # ancien ; le reste garde l'ordre par statut puis immeuble.
        if r.statut == "rendu" and r.depot_rendu_le is not None:
            return (rank["rendu"], -r.depot_rendu_le.toordinal(), r.immeuble_name)
        return (rank.get(r.statut, 9), 0, r.immeuble_name)

    rows.sort(key=_cle_tri)
    return DepotOverview(
        rows=rows,
        total_detenu=round(total_detenu, 2),
        total_a_rendre=round(total_a_rendre, 2),
        nb_a_rendre=sum(1 for r in rows if r.statut == "a_rendre"),
        total_rendu=round(total_rendu, 2),
        nb_sans_depot=sum(1 for r in rows if r.statut == "aucun"),
    )


# ── Baux ───────────────────────────────────────────────────────────────


@router.get(
    "/immeubles/{immeuble_id}/baux", response_model=List[BailRead]
)
async def list_baux_for_immeuble(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> List[BailRead]:
    _require_volet(user)
    rows = (
        await db.execute(
            select(Bail)
            .join(Logement, Logement.id == Bail.logement_id)
            .where(Logement.immeuble_id == immeuble_id)
            .order_by(Bail.date_debut.desc())
        )
    ).scalars().all()
    # Dernier avis de renouvellement par bail (pastille + bouton
    # « Avis » sur la fiche immeuble) — chargé groupé, une requête.
    last_ren_by_bail: dict = {}
    bail_ids = [r.id for r in rows]
    if bail_ids:
        for ren in (
            await db.execute(
                select(BailRenouvellement).where(
                    BailRenouvellement.bail_id.in_(bail_ids)
                )
            )
        ).scalars().all():
            cur = last_ren_by_bail.get(ren.bail_id)
            if cur is None or (ren.avis_envoye_le, ren.id) > (
                cur.avis_envoye_le,
                cur.id,
            ):
                last_ren_by_bail[ren.bail_id] = ren
    out: List[BailRead] = []
    for r in rows:
        br = BailRead.model_validate(r)
        ren = last_ren_by_bail.get(r.id)
        if ren is not None:
            br.renouvellement_status = ren.status
            br.renouvellement_avis_document_id = ren.document_id
        out.append(br)
    return out


@router.post(
    "/baux", response_model=BailRead, status_code=status.HTTP_201_CREATED
)
async def create_bail(
    payload: BailCreate, db: DBSession, user: CurrentUser
) -> BailRead:
    _require_volet(user)
    log_obj = await db.get(Logement, payload.logement_id)
    if log_obj is None:
        raise HTTPException(status_code=404, detail="Logement introuvable.")
    loc_obj = await db.get(Locataire, payload.locataire_id)
    if loc_obj is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    # Gestion EXTERNE (2026-09-09) : pas de bail dans Kratos — c'est
    # par cette porte qu'un bail « actif » écrasait le loyer attendu et
    # le statut saisis à la main sur les unités externes.
    from app.services.gestion_externe import erreur_externe, immeuble_est_externe

    if await immeuble_est_externe(db, log_obj.immeuble_id):
        raise erreur_externe("pas de bail dans Kratos.")
    if payload.status not in {s.value for s in BailStatus}:
        raise HTTPException(
            status_code=422, detail="Statut de bail invalide."
        )
    # Jamais deux baux ACTIFS qui se chevauchent (audit 2026-07-31).
    if payload.status == BailStatus.ACTIF.value:
        # Garde-fou C4 (2026-08-13) : un bail ACTIF déjà ÉCHU (fin avant
        # le début du nouveau) est terminé automatiquement au lieu de
        # coexister — un chevauchement réel garde le 409 ci-dessous.
        from app.services.locatif_depart import terminer_baux_echus_avant

        await terminer_baux_echus_avant(
            db, payload.logement_id, payload.date_debut
        )
        chev = await _bail_actif_chevauchant(
            db, payload.logement_id, payload.date_debut, payload.date_fin
        )
        if chev is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Un bail ACTIF chevauche ces dates sur ce logement "
                    f"(fin le {chev.date_fin}) — termine-le d'abord ou "
                    "corrige les dates."
                ),
            )

    obj = Bail(**payload.model_dump())
    # « Louer indéfiniment (chambre) » : le logement impose le bail AU
    # MOIS — même loyer à l'infini, aucun avis d'augmentation, hors des
    # renouvellements (retour Phil 2026-08-13).
    if getattr(log_obj, "location_en_chambres", False):
        obj.au_mois = True
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)

    # Met à jour le statut du logement automatiquement
    if obj.status == BailStatus.ACTIF.value:
        log_obj.status = LogementStatus.OCCUPE.value
        # Le « loyer demandé » suit le bail tant que c'est loué (retour
        # client 2026-08-14) — le prix de la prochaine location se
        # décide à la relocation, prérempli avec le loyer courant.
        if obj.loyer_mensuel is not None:
            from app.services.loyer_effectif import (
                refleter_bail_sur_demande,
            )

            refleter_bail_sur_demande(log_obj, float(obj.loyer_mensuel))
        log_obj.updated_at = _now()
        # Un bail ACTIF assigné directement REFERME le dossier de
        # relocation du logement (2026-09-09 : sinon « libre le … »
        # restait collé à vie — seul l'import du bail signé le faisait).
        await db.flush()
        from app.models.immobilier import LocationDossier
        from app.services.locatif_depart import DOSSIER_STATUTS_REGLES

        _d = (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.logement_id == obj.logement_id,
                    LocationDossier.statut.notin_(list(DOSSIER_STATUTS_REGLES)),
                )
            )
        ).scalars().first()
        if _d is not None:
            _d.statut = "reloue"
            if _d.reloue_le is None:
                _d.reloue_le = _now().date()
            if _d.nouveau_bail_id is None:
                _d.nouveau_bail_id = obj.id
            _d.updated_at = _now()
    elif obj.status == BailStatus.PROPOSE.value:
        log_obj.status = LogementStatus.RESERVE.value
        log_obj.updated_at = _now()

    # Interconnexion kanban Locations (v16) : un bail « proposé » crée
    # ou rattache le dossier de relocation du logement — la page Baux
    # et le kanban vivent sur la MÊME donnée.
    if obj.status == BailStatus.PROPOSE.value:
        await db.flush()
        from app.models.immobilier import LocationDossier
        from app.services.locatif_depart import DOSSIER_STATUTS_REGLES

        dossier = (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.logement_id == obj.logement_id,
                    LocationDossier.statut.notin_(
                        list(DOSSIER_STATUTS_REGLES)
                    ),
                )
            )
        ).scalars().first()
        if dossier is None:
            dossier = LocationDossier(
                logement_id=obj.logement_id,
                statut="bail_a_envoyer",
                notes=(
                    "Créé automatiquement — bail préparé depuis la "
                    "page Baux."
                ),
            )
            dossier.created_at = _now()
            db.add(dossier)
        if dossier.nouveau_bail_id is None:
            dossier.nouveau_bail_id = obj.id
            if dossier.statut in (
                "avis_recu",
                "annonce_publiee",
                "visites",
                "candidat_retenu",
            ):
                dossier.statut = "bail_a_envoyer"
            # M9b : préparer un bail est un geste HUMAIN — un dossier
            # auto-créé (unité vacante) est pris en charge : ses frais
            # de relocation redeviennent facturables une fois reloué.
            from app.services.locatif_depart import (
                marquer_prise_en_charge_humaine,
            )

            marquer_prise_en_charge_humaine(dossier)
        dossier.updated_at = _now()

    await db.commit()
    await db.refresh(obj)
    result = BailRead.model_validate(obj)

    # Consentement aux communications électroniques : le PDF est généré et
    # ARCHIVÉ au dossier du bail. Aucun courriel n'est envoyé — l'envoi pour
    # signature reste un geste manuel (règle « zéro envoi auto au locataire »).
    # Best-effort : un échec ne bloque jamais la création du bail.
    try:
        from app.api.v1.endpoints.immobilier_extras import (
            preparer_consentement_communications,
        )

        await preparer_consentement_communications(db, obj.id, user)
    except Exception:  # noqa: BLE001 — la création prime
        log.exception(
            "Préparation du consentement communications échouée (bail %s)", result.id
        )

    return result


@router.patch("/baux/{bail_id}", response_model=BailRead)
async def update_bail(
    bail_id: int,
    payload: BailUpdate,
    db: DBSession,
    user: CurrentUser,
) -> BailRead:
    _require_volet(user)
    obj = await db.get(Bail, bail_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    old_status = obj.status
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] != old_status:
        if data["status"] not in {s.value for s in BailStatus}:
            raise HTTPException(
                status_code=422, detail="Statut de bail invalide."
            )
        if (
            data["status"] == BailStatus.ACTIF.value
            and old_status == BailStatus.PROPOSE.value
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Un bail proposé s'active par l'IMPORT du bail "
                    "signé (page Baux) — le circuit met aussi à jour "
                    "le kanban et le logement."
                ),
            )
        if data["status"] == BailStatus.ACTIF.value:
            # Garde-fou C4 (2026-08-13) : même règle qu'à la création —
            # l'ancien bail actif échu se termine automatiquement.
            from app.services.locatif_depart import (
                terminer_baux_echus_avant,
            )

            await terminer_baux_echus_avant(
                db,
                obj.logement_id,
                data.get("date_debut", obj.date_debut),
                exclure_bail_id=obj.id,
            )
            chev = await _bail_actif_chevauchant(
                db,
                obj.logement_id,
                data.get("date_debut", obj.date_debut),
                data.get("date_fin", obj.date_fin),
                exclure_bail_id=obj.id,
            )
            if chev is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Un bail ACTIF chevauche ces dates sur ce "
                        f"logement (fin le {chev.date_fin})."
                    ),
                )
    # `jour_echeance` est NOT NULL en base : un `null` explicite dans le
    # payload signifie « ne touche pas », pas « efface » (sinon 500).
    if data.get("jour_echeance") is None:
        data.pop("jour_echeance", None)
    # Dossier TAL (2026-09-09) : la date est un MIROIR des dossiers
    # imm_tal_dossiers — poser une date ouvre un dossier non-paiement s'il
    # n'y en a pas d'en cours ; null les ferme. Traité à part, APRÈS les
    # autres champs, par le service qui tient les deux sens cohérents.
    tal_miroir_present = "tal_dossier_ouvert_le" in data
    tal_miroir = data.pop("tal_dossier_ouvert_le", None)
    for k, v in data.items():
        setattr(obj, k, v)
    obj.updated_at = _now()
    if tal_miroir_present:
        await appliquer_date_miroir(
            db, obj, tal_miroir, getattr(user, "email", None)
        )

    # Sync statut logement si bail terminé/résilié
    if (
        old_status == BailStatus.ACTIF.value
        and obj.status in (BailStatus.TERMINE.value, BailStatus.RESILIE.value)
    ):
        log_obj = await db.get(Logement, obj.logement_id)
        if log_obj is not None:
            log_obj.status = LogementStatus.VACANT.value
            # Miroir « loyer demandé » (2026-08-13) : logement VACANT →
            # le prix affiché pour la relocation (dossier Locations)
            # fait foi s'il existe ; sinon on garde le dernier loyer.
            from app.services.locatif_depart import (
                dossier_relocation_actif,
                ouvrir_dossiers_unites_vacantes,
            )

            dossier = await dossier_relocation_actif(db, obj.logement_id)
            if dossier is not None and dossier.loyer_demande is not None:
                log_obj.loyer_demande = dossier.loyer_demande
            log_obj.updated_at = _now()
            # M9a : la mutation qui rend le logement vacant ouvre le
            # dossier de relocation (plus de création dans un GET).
            await ouvrir_dossiers_unites_vacantes(
                db, [obj.logement_id]
            )
    elif (
        old_status != BailStatus.ACTIF.value
        and obj.status == BailStatus.ACTIF.value
    ):
        # Réactivation (termine→actif) : le logement redevient occupé
        # (audit 2026-07-31).
        log_obj = await db.get(Logement, obj.logement_id)
        if log_obj is not None:
            log_obj.status = LogementStatus.OCCUPE.value
            log_obj.updated_at = _now()

    # Le « loyer demandé » suit le bail tant que c'est loué (retour
    # client 2026-08-14) : corriger le loyer du bail ACTIF réaligne le
    # logement — le prix de la prochaine location se décide à la
    # relocation.
    if obj.status == BailStatus.ACTIF.value and obj.loyer_mensuel is not None:
        from app.services.gestion_externe import immeuble_est_externe
        from app.services.loyer_effectif import refleter_bail_sur_demande

        log_obj = await db.get(Logement, obj.logement_id)
        if log_obj is not None and not await immeuble_est_externe(
            db, log_obj.immeuble_id
        ):
            refleter_bail_sur_demande(log_obj, float(obj.loyer_mensuel))

    await db.commit()
    await db.refresh(obj)
    return BailRead.model_validate(obj)


@router.delete("/baux/{bail_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bail(
    bail_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("bail.delete"))],
) -> None:
    """Supprime un bail SAISI PAR ERREUR. Garde-fous (audit
    2026-07-31) : refuse si des paiements/frais existent ou si le
    dépôt de garantie n'a pas été rendu ; recale le logement et le
    dossier de relocation lié. Pour une vraie fin de bail : la
    résiliation (page Baux → « Mettre fin au bail »)."""
    _require_volet(user)
    obj = await db.get(Bail, bail_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    nb_paiements = (
        await db.execute(
            select(func.count(PaiementLoyer.id)).where(
                PaiementLoyer.bail_id == bail_id
            )
        )
    ).scalar_one()
    nb_frais = (
        await db.execute(
            select(func.count(FraisLocatif.id)).where(
                FraisLocatif.bail_id == bail_id
            )
        )
    ).scalar_one()
    if nb_paiements or nb_frais:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ce bail a {nb_paiements} paiement(s) et {nb_frais} "
                "frais — retire-les d'abord, ou utilise la résiliation "
                "pour une vraie fin de bail (l'historique est conservé)."
            ),
        )
    if (
        obj.depot_garantie is not None
        and float(obj.depot_garantie) > 0
        and obj.depot_rendu_le is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Le dépôt de garantie de ce bail n'a pas été rendu — "
                "règle-le dans la page Dépôts avant de supprimer."
            ),
        )
    # Recalages AVANT la suppression : dossier de relocation lié
    # régressé (plus de bail = retour « candidat retenu ») et statut
    # du logement recalculé.
    from app.models.immobilier import LocationDossier

    for dsr in (
        await db.execute(
            select(LocationDossier).where(
                LocationDossier.nouveau_bail_id == bail_id
            )
        )
    ).scalars().all():
        if dsr.statut in ("bail_a_envoyer", "bail_envoye", "reloue"):
            dsr.statut = "candidat_retenu"
        dsr.reloue_le = None
        dsr.updated_at = _now()
    # M6 (audit 2026-08-13) : un dossier dont ce bail est le SORTANT
    # perdrait silencieusement ses repères (FK SET NULL). AVANT la
    # suppression, on recopie les infos utiles qu'il n'a pas encore
    # (fill-only) + une note de traçabilité.
    await _consigner_suppression_bail_sortant(db, obj)
    logement_id = obj.logement_id
    await db.delete(obj)
    await db.flush()
    await _recaler_logement_apres_bail(db, logement_id)
    # M9a : si le logement vient de redevenir vacant sans dossier de
    # relocation actif, la mutation en ouvre un (plus de GET créateur).
    from app.services.locatif_depart import ouvrir_dossiers_unites_vacantes

    await ouvrir_dossiers_unites_vacantes(db, [logement_id])
    await db.commit()


# ── Paiements de loyer ─────────────────────────────────────────────────


@router.get(
    "/baux/{bail_id}/paiements", response_model=List[PaiementLoyerRead]
)
async def list_paiements(
    bail_id: int, db: DBSession, user: CurrentUser
) -> List[PaiementLoyerRead]:
    _require_volet(user)
    # Rien avant le démarrage du pôle (l'historique d'essai ne compte plus).
    depuis = await get_demarrage()
    rows = (
        await db.execute(
            select(PaiementLoyer)
            .where(
                PaiementLoyer.bail_id == bail_id,
                PaiementLoyer.mois_couvert >= depuis,
            )
            .order_by(PaiementLoyer.mois_couvert.desc())
        )
    ).scalars().all()
    return [PaiementLoyerRead.model_validate(r) for r in rows]


@router.post(
    "/paiements",
    response_model=PaiementLoyerRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_paiement(
    payload: PaiementLoyerCreate, db: DBSession, user: CurrentUser
) -> PaiementLoyerRead:
    """Enregistre un paiement de loyer. TROP-PAYÉ (retour Phil
    2026-07-22) : si le montant dépasse le restant dû du mois (loyer +
    frais − déjà reçu), le SURPLUS est réparti automatiquement sur les
    mois SUIVANTS (une ligne par mois) — un locataire qui paie 3 mois
    d'un coup voit les 3 mois marqués payés."""
    _require_volet(user)
    bail = await db.get(Bail, payload.bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    # Garde-fou (audit 2026-07-31, assoupli M7 2026-08-13) : pas de
    # paiement sur un bail « proposé ». Un bail RESILIE/TERMINE accepte
    # encore un paiement pour un MOIS QU'IL COUVRAIT — sa ligne reste
    # visible dans le suivi des loyers de ce mois (dernier loyer,
    # solde de départ).
    if bail.status == BailStatus.PROPOSE.value:
        raise HTTPException(
            status_code=400,
            detail=(
                "Ce bail n'est pas actif — un paiement ne peut être "
                "enregistré que sur un bail actif."
            ),
        )
    if bail.status in (
        BailStatus.RESILIE.value,
        BailStatus.TERMINE.value,
    ):
        m_paiement = payload.mois_couvert.replace(day=1)
        couvre = (
            bail.date_fin is not None
            and bail.date_debut is not None
            and bail.date_debut.replace(day=1)
            <= m_paiement
            <= bail.date_fin.replace(day=1)
        )
        if not couvre:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Ce bail est terminé — un paiement ne peut viser "
                    "qu'un mois couvert par le bail."
                ),
            )

    async def _du_restant(mois: date) -> float:
        """Loyer + frais du mois − paiements déjà enregistrés."""
        loyer = float(bail.loyer_mensuel or 0)
        frais = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(FraisLocatif.montant), 0))
                    .where(
                        FraisLocatif.bail_id == bail.id,
                        FraisLocatif.mois_couvert == mois,
                    )
                )
            ).scalar() or 0
        )
        deja = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(PaiementLoyer.montant), 0))
                    .where(
                        PaiementLoyer.bail_id == bail.id,
                        PaiementLoyer.mois_couvert == mois,
                    )
                )
            ).scalar() or 0
        )
        return round(loyer + frais - deja, 2)

    def _mois_suivant(m: date) -> date:
        return date(m.year + (1 if m.month == 12 else 0),
                    1 if m.month == 12 else m.month + 1, 1)

    montant_total = round(float(payload.montant or 0), 2)
    mois = payload.mois_couvert.replace(day=1)
    loyer_ref = float(bail.loyer_mensuel or 0)
    obj: Optional[PaiementLoyer] = None
    derniere: Optional[PaiementLoyer] = None
    # Répartition : chaque itération couvre au plus le restant du mois
    # courant, le surplus glisse au mois suivant (borné à 36 mois — un
    # montant aberrant ne crée pas des années de paiements).
    for _ in range(36):
        if montant_total <= 0.005:
            break
        # Le trop-payé ne déborde JAMAIS après la fin du bail (audit
        # 2026-07-31) : le reliquat se colle au dernier mois du bail.
        if (
            not bail.au_mois
            and bail.date_fin
            and mois > bail.date_fin.replace(day=1)
        ):
            break
        restant = await _du_restant(mois)
        if restant <= 0:
            if loyer_ref <= 0:
                # Pas de loyer de référence : tout mettre sur ce mois.
                restant = montant_total
            else:
                mois = _mois_suivant(mois)
                continue
        tranche = min(montant_total, restant)
        row = PaiementLoyer(
            bail_id=bail.id,
            mois_couvert=mois,
            montant=tranche,
            paye_le=payload.paye_le,
            methode=payload.methode,
            reference=payload.reference,
            notes=payload.notes if obj is None else None,
        )
        row.created_at = _now()
        # Marquer en retard si payé > 5 jours après l'ÉCHÉANCE du bail
        # (le 1er du mois par défaut, « Ou le ___ » du bail TAL sinon).
        if paiement_en_retard(mois, row.paye_le, bail.jour_echeance):
            row.en_retard = True
        db.add(row)
        if obj is None:
            obj = row
        derniere = row
        montant_total = round(montant_total - tranche, 2)
        mois = _mois_suivant(mois)
    # Bail TERMINÉ : la répartition ci-dessus ne peut pas déborder sa
    # fin. Si rien n'a été alloué (les mois visés étaient déjà couverts)
    # alors que la dette vient de mois ANTÉRIEURS impayés, on redescend
    # vers eux — c'est l'encaissement du solde d'un locataire parti
    # (retour client 2026-08-14 : la ligne « dette » doit s'encaisser).
    if (
        obj is None
        and montant_total > 0.005
        and bail.status
        in (BailStatus.RESILIE.value, BailStatus.TERMINE.value)
        and bail.date_debut is not None
    ):
        def _mois_precedent(m: date) -> date:
            return date(m.year - (1 if m.month == 1 else 0),
                        12 if m.month == 1 else m.month - 1, 1)

        plancher = bail.date_debut.replace(day=1)
        mois = payload.mois_couvert.replace(day=1)
        for _ in range(36):
            mois = _mois_precedent(mois)
            if mois < plancher:
                break
            if montant_total <= 0.005:
                break
            restant = await _du_restant(mois)
            if restant <= 0:
                continue
            tranche = min(montant_total, restant)
            row = PaiementLoyer(
                bail_id=bail.id,
                mois_couvert=mois,
                montant=tranche,
                paye_le=payload.paye_le,
                methode=payload.methode,
                reference=payload.reference,
                notes=payload.notes if obj is None else None,
            )
            row.created_at = _now()
            if paiement_en_retard(mois, row.paye_le, bail.jour_echeance):
                row.en_retard = True
            db.add(row)
            if obj is None:
                obj = row
            derniere = row
            montant_total = round(montant_total - tranche, 2)

    # Reliquat après la borne : collé au dernier mois créé plutôt que
    # silencieusement perdu.
    if montant_total > 0.005 and derniere is not None:
        derniere.montant = round(
            float(derniere.montant or 0) + montant_total, 2
        )
    if obj is None:
        raise HTTPException(status_code=400, detail="Montant invalide.")

    # Mettre à jour le score du locataire (basique : % paiements à temps)
    # — uniquement sur l'historique qui compte (depuis le démarrage).
    paiements = (
        await db.execute(
            select(PaiementLoyer).where(
                PaiementLoyer.bail_id == bail.id,
                PaiementLoyer.mois_couvert >= await get_demarrage(),
            )
        )
    ).scalars().all()
    total = len(paiements) + 1
    en_retard = sum(1 for p in paiements if p.en_retard) + (
        1 if obj.en_retard else 0
    )
    score = max(0, min(100, round((1 - en_retard / total) * 100)))
    locataire = await db.get(Locataire, bail.locataire_id)
    if locataire is not None:
        locataire.paiement_score = score
        locataire.updated_at = _now()

    await db.commit()
    await db.refresh(obj)
    return PaiementLoyerRead.model_validate(obj)


@router.delete(
    "/paiements/{paiement_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_paiement(
    paiement_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("paiement_loyer.delete"))],
) -> None:
    _require_volet(user)
    obj = await db.get(PaiementLoyer, paiement_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Paiement introuvable.")
    await db.delete(obj)
    await db.commit()


@router.delete(
    "/baux/{bail_id}/paiements-mois",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def annuler_paiements_mois(
    bail_id: int,
    mois: str,
    db: DBSession,
    user: Annotated[
        User, Depends(require_capability("paiement_loyer.delete"))
    ],
) -> None:
    """Annule TOUS les paiements d'un mois pour un bail (correction d'une
    erreur de saisie — retour Steven 2026-07-22). Même capability que la
    suppression unitaire (audit 2026-07-31)."""
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    try:
        month_start = datetime.strptime(mois + "-01", "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Format mois attendu : YYYY-MM."
        )
    rows = (
        await db.execute(
            select(PaiementLoyer).where(
                PaiementLoyer.bail_id == bail_id,
                PaiementLoyer.mois_couvert == month_start,
            )
        )
    ).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.flush()
    await _recalc_paiement_score(db, bail)
    await db.commit()


# ── Vue transversale « Loyers & retards » ─────────────────────────────
# Tous les baux actifs du portefeuille croisés avec les paiements d'un
# mois donné — LA vue quotidienne du gestionnaire (qui a payé, qui est
# en retard, marquer payé en 1 clic depuis la page Baux & paiements).

#: Point de départ des soldes = date de DÉMARRAGE du pôle, réglable dans
#: Paramètres → Gestion locative (défaut 1er juillet 2026). Tout ce qui
#: précède (loyers échus, paiements, frais) est IGNORÉ : les baux
#: existants repartent à zéro. Voir services/locatif_demarrage.py.


class FraisRow(BaseModel):
    id: int
    montant: float
    libelle: str


class LoyerOverviewRow(BaseModel):
    #: 0 pour une ligne de logement VACANT (aucun bail — même
    #: convention que les lignes de gestion externe côté frontend).
    bail_id: int
    #: Statut du logement pour les lignes vacantes ("vacant" ou
    #: "reserve") — étiquette exacte côté UI.
    logement_statut: Optional[str] = None
    #: Dossier ouvert au TAL sur ce bail (non-paiement) — badge +
    #: coche sur la ligne, pour que l'équipe voie le recours lancé.
    tal_dossier_ouvert_le: Optional[date] = None
    #: Garants / contacts ACTIFS du locataire (noms) — cherchables depuis
    #: la page Paiements (« un virement de Jacques alors que le locataire
    #: est Sébastien » — retour Phil 2026-09-09).
    garants: List[str] = []
    #: Contact qui PAIE le loyer (case « paie le loyer »), sinon None —
    #: affiché « paie : Jacques Roy » sous le nom du locataire.
    payeur_nom: Optional[str] = None
    #: Le mois affiché est réglé mais un mois ANTÉRIEUR du bail ne
    #: l'est pas — la ligne remonte avec un badge « Solde antérieur »
    #: au lieu de dormir en vert (retour Phil 2026-08-26).
    solde_anterieur: bool = False
    immeuble_id: int
    immeuble_name: str
    logement_id: Optional[int] = None
    logement_numero: Optional[str] = None
    locataire_id: Optional[int] = None
    locataire_name: Optional[str] = None
    locataire_phone: Optional[str] = None
    #: Sert l'aperçu affiché avant une relance de loyer : le
    #: gestionnaire doit voir À QUELLE adresse le rappel partira avant
    #: de l'envoyer (retour Phil 2026-08-19).
    locataire_email: Optional[str] = None
    #: Départ ACTÉ : ce locataire part à cette date. « Les départs
    #: confirmés devraient être un peu plus présents ailleurs » —
    #: savoir qu'un locataire s'en va change la façon de lire son
    #: retard de loyer.
    libre_le: Optional[date] = None
    loyer_mensuel: float
    #: Jour d'échéance du loyer (bail TAL « Ou le ___ ») — affiché
    #: « payable le X » à côté du loyer quand ce n'est pas le 1er, et
    #: seuil de retard de la ligne.
    jour_echeance: int = 1
    paiement_id: Optional[int] = None
    #: SOMME des paiements du mois (plusieurs paiements partiels possibles
    #: — retour Steven 2026-07-20).
    montant_paye: Optional[float] = None
    paye_le: Optional[date] = None
    # "paye" | "partiel" | "retard" | "attente"
    etat: str
    #: Statut du bail (M7, audit 2026-08-13) : un bail RESILIE/TERMINE
    #: en cours de mois reste dans le mois qu'il couvrait (loyer du
    #: mois entamé + solde), avec un badge « Bail terminé le X ».
    bail_statut: str = "actif"
    bail_termine_le: Optional[date] = None
    #: LE bail courant (imm_documents) — clic sur la ligne = l'ouvrir.
    document_id: Optional[int] = None
    #: Frais ponctuels du MOIS affiché (retard, etc.) — supprimables.
    frais_mois: List[FraisRow] = []
    #: SOLDE CUMULATIF dû sur le bail (loyers échus + tous les frais −
    #: tous les paiements), borné à 0. Ex.: juin + juillet impayés → le
    #: solde d'août affiche les 3 mois.
    solde_total: float = 0.0
    nb_relances: int = 0
    derniere_relance_le: Optional[date] = None
    #: Prochain locataire (transition) : bail futur ou en préparation
    #: sur le même logement — affiché en petit sous la ligne, avec le
    #: statut du kanban Locations (retour Phil 2026-07-31).
    prochain_nom: Optional[str] = None
    prochain_loyer: Optional[float] = None
    prochain_debut: Optional[date] = None
    prochain_statut: Optional[str] = None


class LoyerOverview(BaseModel):
    mois: str
    rows: List[LoyerOverviewRow]
    total_attendu: float
    total_recu: float
    nb_payes: int
    nb_retards: int
    nb_attente: int
    #: Logements sans bail ce mois-ci (vacants/réservés) — lignes
    #: informatives en bas de liste (retour Phil 2026-08-31).
    nb_vacants: int = 0
    #: Somme des soldes dus (tuile KPI).
    total_solde_du: float = 0.0
    #: Date de démarrage du pôle : les soldes ne remontent pas avant
    #: (affichée dans l'UI pour qu'on sache d'où part le cumul).
    solde_depuis: Optional[date] = None


@router.get("/loyers/overview", response_model=LoyerOverview)
async def loyers_overview(
    db: DBSession,
    user: CurrentUser,
    mois: Optional[str] = None,
    entreprise_id: Optional[int] = None,
) -> LoyerOverview:
    """Croisement baux actifs × paiements pour un mois (def. courant).

    Un bail sans paiement pour le mois est « retard » passé son ÉCHÉANCE
    + le délai de grâce (le 1er → le 5 pour l'immense majorité des baux ;
    un bail payable le 12 bascule le 16). Même ancrage que le flag
    ``en_retard`` à la création d'un paiement. Sinon « attente ».
    """
    _require_volet(user)

    today = datetime.now(timezone.utc).date()
    solde_depuis = await get_demarrage()
    if mois:
        try:
            month_start = datetime.strptime(mois + "-01", "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Format mois attendu : YYYY-MM."
            )
    else:
        month_start = today.replace(day=1)
    month_label = month_start.strftime("%Y-%m")

    # Périmètre immeubles : filtre entreprise + visibilité employé.
    # Gestion externe : les loyers sont perçus par le gestionnaire tiers →
    # hors du suivi opérationnel. isnot(True) couvre aussi les NULL
    # (lignes créées avant le backfill du default).
    imm_q = select(Immeuble).where(
        Immeuble.is_active.is_(True),
        Immeuble.gestion_externe.isnot(True),
    )
    if entreprise_id is not None:
        imm_q = imm_q.where(
            Immeuble.owner_entreprise_id == int(entreprise_id)
        )
    immeubles = (await db.execute(imm_q)).scalars().all()
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = [i for i in immeubles if i.id in visible]
    imm_by_id = {i.id: i for i in immeubles}
    if not imm_by_id:
        return LoyerOverview(
            mois=month_label,
            rows=[],
            total_attendu=0.0,
            total_recu=0.0,
            nb_payes=0,
            nb_retards=0,
            nb_attente=0,
            solde_depuis=solde_depuis,
        )

    logements = (
        await db.execute(
            select(Logement).where(
                Logement.immeuble_id.in_(list(imm_by_id.keys()))
            )
        )
    ).scalars().all()
    log_by_id = {l.id: l for l in logements}

    # Un bail actif qui COMMENCE APRÈS le mois affiché (import du bail
    # signé pendant la transition) n'a rien à collecter ce mois-ci —
    # il apparaît en « Prochain » sous la ligne du bail courant.
    month_end = (
        month_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1) - timedelta(days=1)
    # Baux RESILIE/TERMINE (règle affinée 2026-08-14, retour client) :
    # - mois que le bail COUVRAIT (date_fin >= 1er du mois affiché) :
    #   la ligne reste, toujours (M7 : loyer du mois entamé + badge) ;
    # - mois APRÈS la fin : la ligne n'apparaît QUE si le solde du bail
    #   est encore > 0 (dette à percevoir) — le tri se fait plus bas,
    #   une fois le solde calculé. Le SQL ramène donc aussi les baux
    #   terminés AVANT le mois affiché (bornés au démarrage du pôle,
    #   d'où aucun solde ne peut remonter).
    from sqlalchemy import or_

    fin_minimale = min(month_start, solde_depuis)
    baux = (
        await db.execute(
            select(Bail).where(
                Bail.logement_id.in_(list(log_by_id.keys())),
                Bail.date_debut <= month_end,
                or_(
                    Bail.status == BailStatus.ACTIF.value,
                    and_(
                        Bail.status.in_(
                            [
                                BailStatus.RESILIE.value,
                                BailStatus.TERMINE.value,
                            ]
                        ),
                        Bail.date_fin.is_not(None),
                        Bail.date_fin >= fin_minimale,
                    ),
                ),
            )
        )
    ).scalars().all()

    def _visible_ce_mois(b: Bail) -> bool:
        # Un bail tout juste terminé par la reconduction lazy suit la
        # même règle : mois couvert → visible ; mois d'après → seulement
        # si dette (décidé au calcul du solde, plus bas).
        if b.status == BailStatus.ACTIF.value:
            return True
        return (
            b.status
            in (BailStatus.RESILIE.value, BailStatus.TERMINE.value)
            and b.date_fin is not None
            and b.date_fin >= fin_minimale
        )

    # Reconduction tacite AUTOMATIQUE (lazy, 2026-08-13) : un bail échu
    # sans réponse s'étire d'un cycle ; un bail échu dont le départ
    # était annoncé (dossier de relocation actif) se termine — plus de
    # baux zombies dans le suivi des loyers. Exécutée AVANT la
    # construction des lignes (M8 : plus de faux « retard » sur un bail
    # actif échu) ; un bail terminé à l'instant qui couvrait le mois
    # affiché reste visible avec son badge.
    from app.services.locatif_depart import reconduire_tacitement_baux_echus

    if await reconduire_tacitement_baux_echus(db, baux):
        baux = [b for b in baux if _visible_ce_mois(b)]

    locataires = {}
    loc_ids = {b.locataire_id for b in baux if b.locataire_id}
    if loc_ids:
        for loc in (
            await db.execute(
                select(Locataire).where(Locataire.id.in_(list(loc_ids)))
            )
        ).scalars().all():
            locataires[loc.id] = loc
    # Garants / contacts actifs de TOUS ces locataires — UN seul SELECT
    # (jamais de N+1 sur la vue quotidienne).
    contacts_by_loc = await contacts_par_locataire(db, loc_ids)

    # Paiements du mois : PLUSIEURS lignes possibles par bail (paiements
    # partiels — retour Steven 2026-07-20) → on garde la liste et on somme.
    paiements_mois: dict[int, list] = {}
    bail_ids = [b.id for b in baux]
    if bail_ids:
        for p in (
            await db.execute(
                select(PaiementLoyer).where(
                    PaiementLoyer.bail_id.in_(bail_ids),
                    PaiementLoyer.mois_couvert == month_start,
                )
            )
        ).scalars().all():
            paiements_mois.setdefault(p.bail_id, []).append(p)

    # Frais ponctuels du mois (retard…) + agrégats VIE DU BAIL pour le
    # solde cumulatif : total payé et total des frais depuis le début.
    frais_mois_by_bail: dict[int, list] = {}
    paye_total_by_bail: dict[int, float] = {}
    frais_total_by_bail: dict[int, float] = {}
    if bail_ids:
        for f in (
            await db.execute(
                select(FraisLocatif).where(
                    FraisLocatif.bail_id.in_(bail_ids),
                    FraisLocatif.mois_couvert == month_start,
                )
            )
        ).scalars().all():
            frais_mois_by_bail.setdefault(f.bail_id, []).append(f)
        for bid, total in (
            await db.execute(
                select(
                    PaiementLoyer.bail_id, func.sum(PaiementLoyer.montant)
                )
                .where(
                    PaiementLoyer.bail_id.in_(bail_ids),
                    PaiementLoyer.mois_couvert >= solde_depuis,
                )
                .group_by(PaiementLoyer.bail_id)
            )
        ).all():
            paye_total_by_bail[bid] = float(total or 0)
        for bid, total in (
            await db.execute(
                select(
                    FraisLocatif.bail_id, func.sum(FraisLocatif.montant)
                )
                .where(
                    FraisLocatif.bail_id.in_(bail_ids),
                    FraisLocatif.mois_couvert >= solde_depuis,
                    FraisLocatif.mois_couvert <= month_start,
                )
                .group_by(FraisLocatif.bail_id)
            )
        ).all():
            frais_total_by_bail[bid] = float(total or 0)

    # « Prochain locataire » pendant la transition (retour Phil
    # 2026-07-31) : bail futur (date_debut > aujourd'hui) ou en
    # préparation (« proposé », relocation CORPIQ) sur le même
    # logement — le plus proche par date de début.
    prochains: dict = {}
    if log_by_id:
        futurs = (
            await db.execute(
                select(Bail)
                .where(
                    Bail.logement_id.in_(list(log_by_id.keys())),
                    or_(
                        Bail.date_debut > today,
                        Bail.status == BailStatus.PROPOSE.value,
                    ),
                )
                .order_by(Bail.date_debut.asc())
            )
        ).scalars().all()
        deja = set(bail_ids)
        futurs = [fb for fb in futurs if fb.id not in deja]
        # Statut ENTRANT seulement : « Prochain » décrit le bail qui
        # s'en vient, pas le cycle de départ d'un autre locataire.
        reloc_fut = await _relocation_par_bail(
            db, [fb.id for fb in futurs], inclure_sortant=False
        )
        fut_loc_ids = {
            fb.locataire_id for fb in futurs if fb.locataire_id
        }
        fut_locs = {}
        if fut_loc_ids:
            for loc in (
                await db.execute(
                    select(Locataire).where(
                        Locataire.id.in_(list(fut_loc_ids))
                    )
                )
            ).scalars().all():
                fut_locs[loc.id] = loc
        for fb in futurs:
            if fb.logement_id in prochains:
                continue
            floc = fut_locs.get(fb.locataire_id)
            reloc_f = reloc_fut.get(fb.id)
            prochains[fb.logement_id] = {
                "nom": floc.full_name if floc else None,
                "loyer": float(fb.loyer_mensuel or 0),
                "debut": fb.date_debut,
                "statut": reloc_f["statut"] if reloc_f else "a_venir",
            }

    def _mois_echus(b: Bail) -> int:
        """Nombre de 1ers de mois couverts par le bail jusqu'au mois
        affiché inclus (borné à aujourd'hui) — pour le solde cumulatif.
        Ne remonte jamais avant la date de démarrage du pôle."""
        debut = max(b.date_debut.replace(day=1), solde_depuis)
        fin = min(month_start, today.replace(day=1))
        # Bail AU MOIS : reconduction auto — les loyers courent sans
        # egard a la date de fin (retour Phil 2026-07-28)… sauf s'il
        # est terminé/résilié (M7) : la fin borne alors le cumul.
        if b.date_fin and (
            not b.au_mois or b.status != BailStatus.ACTIF.value
        ):
            fin = min(fin, b.date_fin.replace(day=1))
        if fin < debut:
            return 0
        return (fin.year - debut.year) * 12 + (fin.month - debut.month) + 1

    # Départs ACTÉS : savoir qu'un locataire s'en va le 31 change la
    # façon de lire son retard de loyer, et évite de le relancer pour un
    # mois qu'il n'occupera pas (« les départs confirmés devraient être
    # un peu plus présents ailleurs », Phil 2026-08-19).
    from app.services.locatif_depart import libere_le as _libere_le

    liberations: dict[int, date] = {}
    for lg_id in {b.logement_id for b in baux}:
        d = await _libere_le(db, lg_id)
        if d is not None:
            liberations[lg_id] = d

    rows: List[LoyerOverviewRow] = []
    # Logements avec un bail qui COUVRE le mois affiché — le reste des
    # logements loués/louables remonte en ligne « Vacant » plus bas.
    logements_couverts: set[int] = set()
    total_attendu = 0.0
    total_recu = 0.0
    total_solde_du = 0.0
    nb_payes = nb_retards = nb_attente = 0

    for b in baux:
        logement = log_by_id.get(b.logement_id)
        imm = imm_by_id.get(logement.immeuble_id) if logement else None
        if imm is None:
            continue
        loc = locataires.get(b.locataire_id)
        ps = paiements_mois.get(b.id) or []
        loyer_bail_ref = float(b.loyer_mensuel or 0)
        # Mois APRÈS la fin d'un bail terminé (retour client 2026-08-14) :
        # rien n'est attendu pour le mois affiché lui-même — la ligne
        # n'existe que pour la DETTE (solde > 0) du locataire parti.
        apres_fin = (
            b.status
            in (BailStatus.RESILIE.value, BailStatus.TERMINE.value)
            and b.date_fin is not None
            and b.date_fin < month_start
        )
        loyer = 0.0 if apres_fin else loyer_bail_ref
        if not apres_fin and b.logement_id:
            logements_couverts.add(b.logement_id)
        # DÛ du mois = loyer + frais ponctuels du mois (retour Phil
        # 2026-07-22 : « Marquer payé » doit couvrir 650 + 20 = 670, et
        # le mois n'est « payé » que si les frais sont couverts aussi).
        frais_mois_total = round(
            sum(
                float(f.montant or 0)
                for f in (frais_mois_by_bail.get(b.id) or [])
            ),
            2,
        )
        du_mois = round(loyer + frais_mois_total, 2)
        paye_mois = round(sum(float(p.montant or 0) for p in ps), 2)
        dernier = max(ps, key=lambda p: (p.paye_le or month_start, p.id)) if ps else None

        # Solde cumulatif du bail : loyers échus + frais − payé, borné
        # à 0. Calculé AVANT l'état : il décide de la visibilité des
        # mois post-fin (le solde des baux terminés reste borné par leur
        # date de fin via _mois_echus — fix du 2026-08-12, préservé).
        solde = round(
            _mois_echus(b) * loyer_bail_ref
            + frais_total_by_bail.get(b.id, 0.0)
            - paye_total_by_bail.get(b.id, 0.0),
            2,
        )
        solde = max(0.0, solde)
        mois_futur = month_start > today.replace(day=1)
        if apres_fin and (solde <= 0 or mois_futur):
            # Dette réglée → le locataire parti disparaît des mois que
            # son bail ne couvrait pas. Et une dette se réclame AU
            # PRÉSENT : on ne la projette pas dans les mois à venir
            # (retour Phil 2026-08-26 : « pourquoi ces deux éléments en
            # rouge en septembre ? »).
            continue

        total_attendu += du_mois
        total_recu += paye_mois
        if apres_fin:
            # Dette à percevoir : « partiel » si un versement est entré
            # ce mois-ci, sinon « retard » — jamais « attente », la fin
            # du bail est passée.
            etat = "partiel" if ps else "retard"
            nb_retards += 1
        elif ps and paye_mois >= du_mois - 0.005:
            if solde > 0.005 and not mois_futur:
                # Le mois affiché est payé, mais le COMPTE du bail ne
                # l'est pas : un mois antérieur traîne. La ligne ne
                # doit pas dormir en vert au bas de la liste (retour
                # Phil 2026-08-26 : la dette de juillet de Mouad était
                # invisible en août parce qu'août était « payé »).
                etat = "partiel"
                nb_retards += 1
            else:
                etat = "paye"
                nb_payes += 1
        elif ps:
            # Payé en partie seulement — compté comme retard dans les KPI
            # (il manque de l'argent), mais badge distinct dans l'UI.
            etat = "partiel"
            nb_retards += 1
        # Seuil de retard PAR BAIL : échéance du bail (le 1er par défaut,
        # « Ou le ___ » du bail TAL sinon) + délai de grâce. Un bail
        # payable le 12 n'est donc plus « en retard » du 5 au 12.
        elif today > seuil_retard(month_start, b.jour_echeance):
            etat = "retard"
            nb_retards += 1
        else:
            etat = "attente"
            nb_attente += 1
        total_solde_du += solde

        pro = prochains.get(b.logement_id) if b.logement_id else None
        contacts_loc = contacts_by_loc.get(loc.id, []) if loc else []
        rows.append(
            LoyerOverviewRow(
                bail_id=b.id,
                tal_dossier_ouvert_le=b.tal_dossier_ouvert_le,
                garants=[c.full_name for c in contacts_loc],
                payeur_nom=payeur_de(contacts_loc),
                solde_anterieur=bool(
                    ps
                    and paye_mois >= du_mois - 0.005
                    and solde > 0.005
                    and not mois_futur
                ),
                bail_statut=b.status,
                bail_termine_le=(
                    b.date_fin
                    if b.status
                    in (
                        BailStatus.RESILIE.value,
                        BailStatus.TERMINE.value,
                    )
                    else None
                ),
                prochain_nom=pro["nom"] if pro else None,
                prochain_loyer=pro["loyer"] if pro else None,
                prochain_debut=pro["debut"] if pro else None,
                prochain_statut=pro["statut"] if pro else None,
                immeuble_id=imm.id,
                immeuble_name=imm.name,
                logement_id=(
                    logement.id if logement is not None else None
                ),
                logement_numero=(
                    logement.numero if logement is not None else None
                ),
                locataire_id=loc.id if loc else None,
                locataire_name=loc.full_name if loc else None,
                locataire_phone=loc.phone if loc else None,
                locataire_email=loc.email if loc else None,
                libre_le=liberations.get(b.logement_id),
                loyer_mensuel=loyer,
                jour_echeance=b.jour_echeance or 1,
                paiement_id=dernier.id if dernier else None,
                montant_paye=paye_mois if ps else None,
                paye_le=dernier.paye_le if dernier else None,
                etat=etat,
                document_id=getattr(b, "document_id", None),
                frais_mois=[
                    FraisRow(
                        id=f.id,
                        montant=float(f.montant or 0),
                        libelle=f.libelle or "Frais",
                    )
                    for f in (frais_mois_by_bail.get(b.id) or [])
                ],
                solde_total=solde,
            )
        )

    # ── Logements VACANTS (retour Phil 2026-08-31 : « j'aimerais voir
    # les vacants aussi avec la mention vacant ») : aucun bail ne
    # couvre le mois affiché → ligne informative en bas de liste, avec
    # le loyer demandé et le prochain locataire s'il y en a un en
    # préparation. Les logements hors-location (réno, proprio-occupé)
    # restent exclus. Aucun impact sur les totaux d'argent.
    nb_vacants = 0
    for lg in logements:
        if lg.id in logements_couverts:
            continue
        if lg.status == LogementStatus.HORS_LOC.value:
            continue
        imm = imm_by_id.get(lg.immeuble_id)
        if imm is None:
            continue
        pro = prochains.get(lg.id)
        nb_vacants += 1
        rows.append(
            LoyerOverviewRow(
                bail_id=0,
                logement_statut=lg.status,
                immeuble_id=imm.id,
                immeuble_name=imm.name,
                logement_id=lg.id,
                logement_numero=lg.numero,
                loyer_mensuel=float(lg.loyer_demande or 0),
                etat="vacant",
                prochain_nom=pro["nom"] if pro else None,
                prochain_loyer=pro["loyer"] if pro else None,
                prochain_debut=pro["debut"] if pro else None,
                prochain_statut=pro["statut"] if pro else None,
                nb_relances=0,
                derniere_relance_le=None,
            )
        )

    # Relances de loyer envoyées ce mois (compteur + dernière) par bail.
    if rows:
        rel_rows = (
            await db.execute(
                select(RelanceLoyer).where(
                    RelanceLoyer.bail_id.in_(
                        [r.bail_id for r in rows if r.bail_id]
                    ),
                    RelanceLoyer.mois_couvert == month_start,
                )
            )
        ).scalars().all()
        rel_by_bail: dict[int, list] = {}
        for rl in rel_rows:
            rel_by_bail.setdefault(rl.bail_id, []).append(rl)
        for r in rows:
            rls = rel_by_bail.get(r.bail_id) or []
            r.nb_relances = len(rls)
            if rls:
                r.derniere_relance_le = max(x.sent_at for x in rls).date()

    # Retards d'abord, puis attente, puis payés ; tri secondaire par
    # immeuble + logement pour une lecture stable.
    order = {"retard": 0, "attente": 1, "paye": 2, "vacant": 3}
    rows.sort(
        key=lambda r: (
            order.get(r.etat, 3),
            r.immeuble_name,
            r.logement_numero or "",
        )
    )

    return LoyerOverview(
        mois=month_label,
        rows=rows,
        total_attendu=round(total_attendu, 2),
        total_recu=round(total_recu, 2),
        nb_payes=nb_payes,
        nb_retards=nb_retards,
        nb_attente=nb_attente,
        nb_vacants=nb_vacants,
        total_solde_du=round(total_solde_du, 2),
        solde_depuis=solde_depuis,
    )


# ── Frais locatifs ponctuels (retard, etc.) ───────────────────────────


class FraisCreate(BaseModel):
    mois_couvert: date
    #: Positif = frais (retard…), NÉGATIF = crédit qui réduit le loyer
    #: dû (retour Phil 2026-08-31 : « le + frais devrait être
    #: frais/crédit, où je peux réduire le loyer »).
    montant: float
    libelle: str = Field(default="Frais de retard", max_length=128)

    @field_validator("montant")
    @classmethod
    def _montant_non_nul(cls, v: float) -> float:
        if abs(v) < 0.01:
            raise ValueError("Le montant ne peut pas être 0.")
        if abs(v) > 100_000:
            raise ValueError("Montant démesuré — vérifie la saisie.")
        return v


@router.post(
    "/baux/{bail_id}/frais",
    response_model=FraisRow,
    status_code=status.HTTP_201_CREATED,
)
async def create_frais(
    bail_id: int,
    payload: FraisCreate,
    db: DBSession,
    user: CurrentUser,
) -> FraisRow:
    """Ajoute un frais ponctuel (ex. 20 $ si payé après le 15) OU un
    crédit (montant négatif — ex. réduction de loyer entendue) au bail :
    s'ajoute au solde dû, qui reste borné à 0 (retours Steven
    2026-07-20 et Phil 2026-08-31)."""
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    # Garde-fou (audit 2026-07-31) : un frais sur un bail non actif
    # serait invisible dans toutes les vues (dette jamais réclamée).
    if bail.status != BailStatus.ACTIF.value:
        raise HTTPException(
            status_code=400,
            detail="Un frais ne s'ajoute que sur un bail actif.",
        )
    obj = FraisLocatif(
        bail_id=bail_id,
        mois_couvert=payload.mois_couvert.replace(day=1),
        montant=payload.montant,
        libelle=payload.libelle.strip()
        or ("Crédit" if payload.montant < 0 else "Frais de retard"),
        created_by_email=getattr(user, "email", None),
        created_at=_now(),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return FraisRow(
        id=obj.id, montant=float(obj.montant), libelle=obj.libelle
    )


@router.delete(
    "/frais/{frais_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_frais(
    frais_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await db.get(FraisLocatif, frais_id)
    if obj is not None:
        await db.delete(obj)
        await db.commit()


class RelanceLoyerRequest(BaseModel):
    bail_id: int
    mois: Optional[str] = None  # YYYY-MM, défaut = mois courant


class RelanceLoyerResult(BaseModel):
    niveau: int
    destinataire: str
    mois: str


@router.post("/loyers/relance", response_model=RelanceLoyerResult)
async def relancer_loyer(
    payload: RelanceLoyerRequest, db: DBSession, user: CurrentUser
) -> RelanceLoyerResult:
    """Envoie une relance de loyer par courriel au locataire + la journalise.

    Le niveau s'incrémente par bail + mois (1er rappel, 2e rappel…). Sert de
    preuve avant un recours (mise en demeure TAL disponible à part).
    """
    _require_volet(user)
    bail = await db.get(Bail, payload.bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    loc = (
        await db.get(Locataire, bail.locataire_id)
        if bail.locataire_id
        else None
    )
    if loc is None or not (loc.email or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Ce locataire n'a pas de courriel — ajoute-le à sa fiche.",
        )

    today = datetime.now(timezone.utc).date()
    if payload.mois:
        try:
            month_start = datetime.strptime(
                payload.mois + "-01", "%Y-%m-%d"
            ).date()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Format mois attendu : YYYY-MM."
            )
    else:
        month_start = today.replace(day=1)
    month_label = month_start.strftime("%Y-%m")
    mois_fr = month_start.strftime("%m/%Y")

    logement = (
        await db.get(Logement, bail.logement_id)
        if bail.logement_id
        else None
    )
    immeuble = (
        await db.get(Immeuble, logement.immeuble_id) if logement else None
    )

    existing = (
        await db.execute(
            select(RelanceLoyer).where(
                RelanceLoyer.bail_id == bail.id,
                RelanceLoyer.mois_couvert == month_start,
            )
        )
    ).scalars().all()
    niveau = len(existing) + 1

    # P-13 anti double-clic : si une relance a déjà été envoyée pour ce
    # bail + ce mois il y a moins de 5 min, on refuse (évite le double
    # courriel au locataire — preuve TAL — et l'escalade de niveau sur un
    # double-clic ou une relecture du bouton).
    now_dt = datetime.now(timezone.utc)
    for r in existing:
        last = r.sent_at
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now_dt - last < timedelta(minutes=5):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Une relance vient d'être envoyée pour ce loyer (il y a "
                    "moins de 5 minutes)."
                ),
            )

    loyer = float(bail.loyer_mensuel or 0)
    adresse = immeuble.name if immeuble else ""
    if logement and logement.numero:
        adresse = f"{adresse} — logement {logement.numero}"
    label = {1: "Premier rappel", 2: "Deuxième rappel"}.get(
        niveau, f"Rappel n°{niveau}"
    )
    dest = loc.email.strip()
    html = (
        f"<p>Bonjour {loc.full_name or ''},</p>"
        f"<p>{label} : nous n'avons pas encore reçu votre loyer de "
        f"<strong>{loyer:,.0f} $</strong> pour "
        f"<strong>{mois_fr}</strong>"
        + (f" ({adresse})" if adresse else "")
        + ".</p>"
        "<p>Si le paiement a déjà été effectué, "
        "merci d'ignorer ce message. Sinon, nous vous remercions de "
        "régulariser dans les meilleurs délais.</p>"
        "<p>Cordialement,<br/>Horizon Services Immobiliers</p>"
    )

    from app.services.locatif_mail import (
        EnvoiLocataireError,
        envoyer_au_locataire,
    )

    # P-13 : on PERSISTE la relance (commit) AVANT d'envoyer le courriel —
    # la ligne sert de garde d'idempotence. L'index unique (bail, mois,
    # niveau) fait échouer un 2e insert simultané (double-clic concurrent)
    # → 409 au lieu d'un 2e courriel au locataire.
    rl = RelanceLoyer(
        bail_id=bail.id,
        mois_couvert=month_start,
        niveau=niveau,
        canal="courriel",
        destinataire=dest,
        sent_at=_now(),
        sent_by_email=getattr(user, "email", None),
    )
    db.add(rl)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Une relance pour ce loyer vient d'être enregistrée. "
                "Réessaie plus tard si nécessaire."
            ),
        )

    # Envoi best-effort APRÈS le commit. En cas d'échec, la ligne reste (le
    # gestionnaire voit l'erreur ; l'anti double-clic + l'index évitent tout
    # renvoi accidentel) — jamais de double courriel au locataire.
    try:
        await envoyer_au_locataire(
            db,
            destinataires=[dest],
            sujet=f"Rappel de loyer — {mois_fr}",
            corps_html=html,
            type_envoi="relance_loyer",
            locataire_id=loc.id,
            locataire_nom=loc.full_name,
            bail_id=bail.id,
            immeuble_id=immeuble.id if immeuble else None,
            immeuble_nom=immeuble.name if immeuble else None,
            auteur_email=getattr(user, "email", None),
            resume_fiche=f"{label} de loyer — {mois_fr} ({loyer:,.0f} $).",
        )
    except EnvoiLocataireError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Relance enregistrée, mais l'envoi est impossible : {exc}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(
                "Relance enregistrée, mais l'envoi du courriel a échoué "
                f"({exc}). Vérifie la config courriel ou réessaie plus tard."
            ),
        )

    # La ligne RelanceLoyer est déjà commitée (garde d'idempotence) ; il
    # reste à valider les DEUX traces ajoutées par le service (journal
    # d'audit + fil de la fiche du locataire). Best-effort : une trace
    # perdue ne doit pas transformer un envoi réussi en erreur.
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()

    return RelanceLoyerResult(niveau=niveau, destinataire=dest, mois=month_label)


# ── Exception « aucun bail à joindre » ────────────────────────────────


class ExceptionBailIn(BaseModel):
    #: Pourquoi ce bail n'aura pas de document. Obligatoire : une
    #: exception sans raison est un oubli déguisé.
    motif: str = Field(..., min_length=3, max_length=255)


class ExceptionBailOut(BaseModel):
    bail_id: int
    motif: Optional[str] = None
    par: Optional[str] = None
    le: Optional[datetime] = None


@router.post(
    "/baux/{bail_id}/exception-document", response_model=ExceptionBailOut
)
async def declarer_exception_bail(
    bail_id: int, payload: ExceptionBailIn, db: DBSession, user: CurrentUser
) -> ExceptionBailOut:
    """Déclare qu'il n'y a AUCUN bail à joindre à ce dossier.

    Le bail au dossier reste la règle : sans lui, aucune preuve du loyer
    ni des conditions convenues. Mais il existe des cas réels sans
    document — et bloquer sec ferait perdre plus qu'il ne protège
    (retour Phil 2026-08-19). L'exception se déclare donc, avec un motif
    obligatoire, et reste signée et datée : elle sort de la liste des
    manquants sans disparaître du dossier.
    """
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bail introuvable.")
    if bail.document_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ce bail a déjà un document au dossier — aucune exception "
            "n'est nécessaire.",
        )
    bail.sans_document_motif = payload.motif.strip()[:255]
    bail.sans_document_par = getattr(user, "email", None)
    bail.sans_document_le = _now()
    await db.commit()
    return ExceptionBailOut(
        bail_id=bail.id,
        motif=bail.sans_document_motif,
        par=bail.sans_document_par,
        le=bail.sans_document_le,
    )


@router.delete(
    "/baux/{bail_id}/exception-document", response_model=ExceptionBailOut
)
async def retirer_exception_bail(
    bail_id: int, db: DBSession, user: CurrentUser
) -> ExceptionBailOut:
    """Retire l'exception — le bail redevient « manquant » et
    réapparaît dans l'alerte (ex. le document a finalement été
    retrouvé, ou l'exception était une erreur)."""
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bail introuvable.")
    bail.sans_document_motif = None
    bail.sans_document_par = None
    bail.sans_document_le = None
    await db.commit()
    return ExceptionBailOut(bail_id=bail.id)


# ── Baux sans bail au dossier ─────────────────────────────────────────


class BailSansDocRow(BaseModel):
    bail_id: int
    immeuble: str
    immeuble_id: int
    #: Cible du clic : c'est sur la fiche du LOGEMENT que le bail se
    #: trouve et que son import se fait. Une alerte ne doit pas porter
    #: l'action, elle doit y mener (règle Phil 2026-08-19).
    logement_id: Optional[int] = None
    logement: str
    locataire: str
    date_debut: date
    #: Jours écoulés depuis l'entrée — plus c'est vieux, plus ça presse.
    jours: int
    #: Rempli seulement pour les exceptions assumées.
    motif: Optional[str] = None
    motif_par: Optional[str] = None


class BailSansDocOverview(BaseModel):
    rows: List[BailSansDocRow]
    nb: int
    #: Baux dont l'absence de document est ASSUMÉE. Ils sortent de la
    #: liste actionnable — sinon l'alerte crie pour rien et on finit par
    #: ne plus la lire — mais restent comptés, et leurs motifs sont
    #: consultables.
    nb_exceptions: int = 0
    exceptions: List[BailSansDocRow] = []


@router.get("/baux/sans-document", response_model=BailSansDocOverview)
async def baux_sans_document(
    db: DBSession,
    user: CurrentUser,
    entreprise_id: Optional[int] = None,
    immeuble_id: Optional[int] = None,
) -> BailSansDocOverview:
    """Baux ACTIFS dont le bail signé n'a jamais été importé.

    Les baux sont signés HORS de Kratos : le seul exemplaire au dossier
    est celui qu'on importe à l'entrée du locataire. Un garde-fou existe
    déjà — un dossier de relocation ne passe pas à « Reloué » sans son
    bail — mais il ne couvre QUE ce chemin : un bail créé directement
    « déjà en vigueur » y échappe. L'audit du 2026-08-19 a trouvé 8 baux
    actifs sans bail au dossier, tous récents (avril à août 2026).

    D'où cette liste : le garde-fou bloque ce qu'il peut, celle-ci rend
    visible ce qui est déjà passé à travers.

    Gestion externe exclue : leurs baux ne sont pas chez nous.
    """
    _require_volet(user)
    today = datetime.now(timezone.utc).date()

    imm_q = select(Immeuble).where(
        Immeuble.is_active.is_(True),
        Immeuble.gestion_externe.isnot(True),
    )
    if entreprise_id is not None:
        imm_q = imm_q.where(
            Immeuble.owner_entreprise_id == int(entreprise_id)
        )
    if immeuble_id is not None:
        imm_q = imm_q.where(Immeuble.id == int(immeuble_id))
    immeubles = (await db.execute(imm_q)).scalars().all()
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = [i for i in immeubles if i.id in visible]
    imm_by_id = {i.id: i for i in immeubles}
    if not imm_by_id:
        return BailSansDocOverview(rows=[], nb=0)

    logements = (
        await db.execute(
            select(Logement).where(
                Logement.immeuble_id.in_(list(imm_by_id.keys()))
            )
        )
    ).scalars().all()
    log_by_id = {lg.id: lg for lg in logements}
    if not log_by_id:
        return BailSansDocOverview(rows=[], nb=0)

    baux = (
        await db.execute(
            select(Bail).where(
                Bail.logement_id.in_(list(log_by_id.keys())),
                Bail.status == BailStatus.ACTIF.value,
                Bail.document_id.is_(None),
            )
        )
    ).scalars().all()
    if not baux:
        return BailSansDocOverview(rows=[], nb=0)

    locs = (
        await db.execute(
            select(Locataire).where(
                Locataire.id.in_([b.locataire_id for b in baux])
            )
        )
    ).scalars().all()
    loc_by_id = {lo.id: lo for lo in locs}

    rows: List[BailSansDocRow] = []
    exceptions: List[BailSansDocRow] = []
    for b in baux:
        lg = log_by_id.get(b.logement_id)
        im = imm_by_id.get(lg.immeuble_id) if lg else None
        if im is None or lg is None:
            continue
        lo = loc_by_id.get(b.locataire_id)
        debut = b.date_debut or today
        motif = (b.sans_document_motif or "").strip() or None
        ligne = BailSansDocRow(
            bail_id=b.id,
            immeuble=im.name,
            immeuble_id=im.id,
            logement_id=lg.id,
            logement=lg.numero or "—",
            locataire=(lo.full_name if lo else "—"),
            date_debut=debut,
            jours=(today - debut).days,
            motif=motif,
            motif_par=b.sans_document_par if motif else None,
        )
        (exceptions if motif else rows).append(ligne)
    # Le plus ancien d'abord : c'est celui qu'on risque de ne jamais
    # retrouver.
    rows.sort(key=lambda r: r.date_debut)
    exceptions.sort(key=lambda r: r.date_debut)
    return BailSansDocOverview(
        rows=rows,
        nb=len(rows),
        nb_exceptions=len(exceptions),
        exceptions=exceptions,
    )


# ── Échéances de bail (avis de renouvellement) ─────────────────────────


class EcheanceRow(BaseModel):
    bail_id: int
    immeuble: str
    logement: str
    locataire: str
    date_fin: date
    fenetre_debut: date  # avis au plus tôt (≈ 6 mois avant la fin)
    fenetre_fin: date    # avis au plus tard (≈ 3 mois avant la fin)
    statut: str          # a_envoyer | en_retard | a_venir
    jours: int           # jours avant l'ouverture (a_venir) ou avant la fin
    loyer_mensuel: float


class EcheanceOverview(BaseModel):
    rows: List[EcheanceRow]
    nb_a_envoyer: int
    nb_en_retard: int
    nb_a_venir: int


@router.get("/baux/echeances", response_model=EcheanceOverview)
async def baux_echeances(
    db: DBSession,
    user: CurrentUser,
    entreprise_id: Optional[int] = None,
    immeuble_id: Optional[int] = None,
    horizon_jours: int = 45,
) -> EcheanceOverview:
    """Baux actifs dont la fenêtre d'avis de renouvellement approche.

    Au Québec, l'avis de modification d'un bail de 12 mois doit être
    transmis entre 6 et 3 mois avant la fin. On expose les baux dont la
    fenêtre s'ouvre bientôt (« à venir »), est ouverte (« à envoyer »),
    ou est dépassée mais le bail pas encore terminé (« en retard »). Les
    baux pour lesquels un avis a déjà été enregistré dans le cycle sont
    écartés.

    `immeuble_id` restreint les alertes à UN immeuble (bandeau de la
    page Baux quand elle sert de sous-page de la fiche immeuble) —
    simple filtre de portée, aucune règle métier ne change.
    """
    _require_volet(user)
    today = datetime.now(timezone.utc).date()
    # MÊME fenêtre que la page Suivis annuels : le réglage « renouvellement
    # N mois avant » pilote l'ouverture ici aussi (avant, 183 j en dur →
    # le bandeau de la page Paiements contredisait la page Renouvellements).
    from app.services.locatif_suivis import get_suivis

    suivis_cfg = await get_suivis()
    ouverture_jours = suivis_cfg.renouvellement_mois_avant * 30

    # Gestion externe : les avis de renouvellement relèvent du
    # gestionnaire tiers → exclu (isnot(True) couvre les NULL legacy).
    imm_q = select(Immeuble).where(
        Immeuble.is_active.is_(True),
        Immeuble.gestion_externe.isnot(True),
    )
    if entreprise_id is not None:
        imm_q = imm_q.where(
            Immeuble.owner_entreprise_id == int(entreprise_id)
        )
    if immeuble_id is not None:
        imm_q = imm_q.where(Immeuble.id == int(immeuble_id))
    immeubles = (await db.execute(imm_q)).scalars().all()
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = [i for i in immeubles if i.id in visible]
    imm_by_id = {i.id: i for i in immeubles}
    if not imm_by_id:
        return EcheanceOverview(
            rows=[], nb_a_envoyer=0, nb_en_retard=0, nb_a_venir=0
        )

    logements = (
        await db.execute(
            select(Logement).where(
                Logement.immeuble_id.in_(list(imm_by_id.keys()))
            )
        )
    ).scalars().all()
    log_by_id = {l.id: l for l in logements}

    baux = (
        await db.execute(
            select(Bail).where(
                Bail.logement_id.in_(list(log_by_id.keys())),
                Bail.status == BailStatus.ACTIF.value,
            )
        )
    ).scalars().all()

    loc_by_id: dict = {}
    loc_ids = {b.locataire_id for b in baux if b.locataire_id}
    if loc_ids:
        for loc in (
            await db.execute(
                select(Locataire).where(Locataire.id.in_(list(loc_ids)))
            )
        ).scalars().all():
            loc_by_id[loc.id] = loc

    # Avis déjà envoyés (par bail).
    renouv_by_bail: dict = {}
    bail_ids = [b.id for b in baux]
    if bail_ids:
        for r in (
            await db.execute(
                select(BailRenouvellement).where(
                    BailRenouvellement.bail_id.in_(bail_ids)
                )
            )
        ).scalars().all():
            renouv_by_bail.setdefault(r.bail_id, []).append(r.avis_envoye_le)

    rows: List[EcheanceRow] = []
    for b in baux:
        if not b.date_fin:
            continue
        logement = log_by_id.get(b.logement_id)
        # « Louer indéfiniment (chambre) » / bail AU MOIS : reconduction
        # automatique au même loyer, aucun avis d'augmentation attendu →
        # jamais dans les échéances (retour Phil 2026-08-13). Le flag du
        # logement sert de filet pour les baux legacy créés avant le lien.
        if b.au_mois or (
            logement is not None
            and getattr(logement, "location_en_chambres", False)
        ):
            continue
        # Ouverture réglable (défaut 6 mois) ; fermeture au plancher légal
        # de 3 mois (art. 1942 C.c.Q.), qui lui ne se règle pas.
        window_start = b.date_fin - timedelta(days=ouverture_jours)
        window_end = b.date_fin - timedelta(days=91)
        # Avis déjà transmis dans ce cycle ?
        if any(
            d and d >= window_start for d in renouv_by_bail.get(b.id, [])
        ):
            continue
        if today >= b.date_fin:
            continue  # bail terminé (reconduit automatiquement)
        if today < window_start - timedelta(days=horizon_jours):
            continue  # trop loin pour alerter

        if today < window_start:
            statut, jours = "a_venir", (window_start - today).days
        elif today <= window_end:
            statut, jours = "a_envoyer", (window_end - today).days
        else:
            statut, jours = "en_retard", (b.date_fin - today).days

        immeuble = (
            imm_by_id.get(logement.immeuble_id) if logement else None
        )
        locataire = loc_by_id.get(b.locataire_id)
        rows.append(
            EcheanceRow(
                bail_id=b.id,
                immeuble=(immeuble.name if immeuble else "—"),
                logement=(logement.numero if logement else "—"),
                locataire=(
                    locataire.full_name if locataire else "—"
                ),
                date_fin=b.date_fin,
                fenetre_debut=window_start,
                fenetre_fin=window_end,
                statut=statut,
                jours=jours,
                loyer_mensuel=float(b.loyer_mensuel or 0),
            )
        )

    order = {"en_retard": 0, "a_envoyer": 1, "a_venir": 2}
    rows.sort(key=lambda r: (order.get(r.statut, 9), r.date_fin))
    return EcheanceOverview(
        rows=rows,
        nb_a_envoyer=sum(1 for r in rows if r.statut == "a_envoyer"),
        nb_en_retard=sum(1 for r in rows if r.statut == "en_retard"),
        nb_a_venir=sum(1 for r in rows if r.statut == "a_venir"),
    )


# ── Dépenses d'immeuble + P&L ──────────────────────────────────────────


class DepenseRead(BaseModel):
    id: int
    immeuble_id: int
    categorie: str
    libelle: str
    montant: float
    frequence: str
    # montant = % des loyers mensuels (ex. gestion à 5 %) au lieu d'un $.
    is_pourcentage: bool = False
    # taxable = appliquer TPS+TVQ Québec (×1.14975) dans les calculs.
    taxable: bool = False
    date_depense: Optional[date] = None
    notes: Optional[str] = None


class DepenseCreate(BaseModel):
    categorie: str = "autre"
    libelle: str
    montant: float = Field(..., ge=0)
    frequence: str = "ponctuel"
    is_pourcentage: bool = False
    taxable: bool = False
    date_depense: Optional[date] = None
    notes: Optional[str] = None


class DepenseUpdate(BaseModel):
    categorie: Optional[str] = None
    libelle: Optional[str] = None
    montant: Optional[float] = Field(default=None, ge=0)
    frequence: Optional[str] = None
    is_pourcentage: Optional[bool] = None
    taxable: Optional[bool] = None
    date_depense: Optional[date] = None
    notes: Optional[str] = None


def _depense_to_read(d: DepenseImmeuble) -> DepenseRead:
    return DepenseRead(
        id=d.id,
        immeuble_id=d.immeuble_id,
        categorie=d.categorie,
        libelle=d.libelle,
        montant=float(d.montant or 0),
        frequence=d.frequence,
        is_pourcentage=bool(d.is_pourcentage),
        taxable=bool(d.taxable),
        date_depense=d.date_depense,
        notes=d.notes,
    )


@router.get(
    "/immeubles/{immeuble_id}/depenses",
    response_model=List[DepenseRead],
)
async def list_depenses(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> List[DepenseRead]:
    _require_volet(user)
    await _require_immeuble_visible(db, user, immeuble_id)
    rows = (
        await db.execute(
            select(DepenseImmeuble)
            .where(DepenseImmeuble.immeuble_id == immeuble_id)
            .order_by(DepenseImmeuble.id.desc())
        )
    ).scalars().all()
    return [_depense_to_read(d) for d in rows]


@router.post(
    "/immeubles/{immeuble_id}/depenses",
    response_model=DepenseRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_depense(
    immeuble_id: int,
    payload: DepenseCreate,
    db: DBSession,
    user: CurrentUser,
) -> DepenseRead:
    _require_volet(user)
    await _require_immeuble_visible(db, user, immeuble_id)
    await _get_immeuble_or_404(db, immeuble_id)
    obj = DepenseImmeuble(
        immeuble_id=immeuble_id,
        categorie=payload.categorie or "autre",
        libelle=payload.libelle.strip(),
        montant=payload.montant,
        frequence=(
            payload.frequence
            if payload.frequence in ("ponctuel", "mensuel", "annuel")
            else "ponctuel"
        ),
        is_pourcentage=payload.is_pourcentage,
        taxable=payload.taxable,
        date_depense=payload.date_depense,
        notes=payload.notes,
        created_by_email=user.email,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _depense_to_read(obj)


@router.put("/depenses/{depense_id}", response_model=DepenseRead)
async def update_depense(
    depense_id: int,
    payload: DepenseUpdate,
    db: DBSession,
    user: CurrentUser,
) -> DepenseRead:
    _require_volet(user)
    obj = await db.get(DepenseImmeuble, depense_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Dépense introuvable.")
    await _require_immeuble_visible(db, user, obj.immeuble_id)
    data = payload.model_dump(exclude_unset=True)
    if "frequence" in data and data["frequence"] not in (
        "ponctuel",
        "mensuel",
        "annuel",
    ):
        data.pop("frequence")
    for k, v in data.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return _depense_to_read(obj)


@router.delete(
    "/depenses/{depense_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_depense(
    depense_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("depense.delete"))],
) -> None:
    _require_volet(user)
    obj = await db.get(DepenseImmeuble, depense_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Dépense introuvable.")
    await _require_immeuble_visible(db, user, obj.immeuble_id)
    await db.delete(obj)
    await db.commit()


class PnlRow(BaseModel):
    immeuble_id: int
    immeuble_name: str
    loyers_annualises: float
    revenus_recus: float
    depenses: float
    dette_annuelle: float
    cashflow_potentiel: float
    cashflow_reel: float
    nb_baux_actifs: int


class PnlOverview(BaseModel):
    annee: int
    rows: List[PnlRow]
    totaux: PnlRow


@router.get("/finances/pnl", response_model=PnlOverview)
async def finances_pnl(
    db: DBSession,
    user: CurrentUser,
    annee: Optional[int] = None,
    entreprise_id: Optional[int] = None,
) -> PnlOverview:
    """P&L annuel par immeuble.

    - revenus_recus : paiements de loyer enregistrés dans l'année ;
    - loyers_annualises : loyers des baux ACTIFS × 12 (potentiel) ;
    - depenses : ponctuelles datées dans l'année + récurrentes
      annualisées (mensuel × 12, annuel × 1) ;
    - dette_annuelle : paiements mensuels des hypothèques ACTIVES × 12 ;
    - cashflow_potentiel = loyers_annualises − depenses − dette ;
    - cashflow_reel = revenus_recus − depenses − dette.
    """
    _require_volet(user)
    year = annee or datetime.now(timezone.utc).year
    # Les revenus encaissés ne remontent pas avant le démarrage du pôle
    # (l'année du démarrage compte donc à partir de ce mois-là).
    y_start = max(date(year, 1, 1), await get_demarrage())
    y_end = date(year, 12, 31)

    imm_q = select(Immeuble).where(Immeuble.is_active.is_(True))
    if entreprise_id is not None:
        imm_q = imm_q.where(
            Immeuble.owner_entreprise_id == int(entreprise_id)
        )
    immeubles = (await db.execute(imm_q)).scalars().all()
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = [i for i in immeubles if i.id in visible]
    imm_ids = [i.id for i in immeubles]

    rows: List[PnlRow] = []
    if imm_ids:
        logements = (
            await db.execute(
                select(Logement).where(Logement.immeuble_id.in_(imm_ids))
            )
        ).scalars().all()
        log_to_imm = {l.id: l.immeuble_id for l in logements}
        baux = []
        if log_to_imm:
            baux = (
                await db.execute(
                    select(Bail).where(
                        Bail.logement_id.in_(list(log_to_imm.keys())),
                        Bail.status == BailStatus.ACTIF.value,
                    )
                )
            ).scalars().all()
        bail_to_imm = {b.id: log_to_imm.get(b.logement_id) for b in baux}
        paiements = []
        if bail_to_imm:
            paiements = (
                await db.execute(
                    select(PaiementLoyer).where(
                        PaiementLoyer.bail_id.in_(list(bail_to_imm.keys())),
                        PaiementLoyer.mois_couvert >= y_start,
                        PaiementLoyer.mois_couvert <= y_end,
                    )
                )
            ).scalars().all()
        depenses = (
            await db.execute(
                select(DepenseImmeuble).where(
                    DepenseImmeuble.immeuble_id.in_(imm_ids)
                )
            )
        ).scalars().all()
        # ⚠️ le statut est stocké en minuscules ("active") — l'ancien
        # filtre "ACTIVE" ne matchait rien → dette toujours à 0 $.
        hypos = (
            await db.execute(
                select(Hypotheque).where(
                    Hypotheque.immeuble_id.in_(imm_ids),
                    Hypotheque.status == HypothequeStatus.ACTIVE.value,
                )
            )
        ).scalars().all()

        # Loyers mensuels LOUÉS par immeuble — hiérarchie du loyer
        # effectif (2026-08-14) : interne = bail actif d'abord ; externe
        # = loyer SAISI sur le logement d'abord (un bail résiduel ne
        # masque plus la saisie). Même définition que la fiche.
        from app.services.loyer_effectif import loyer_effectif_loue

        externes_pnl = {i.id for i in immeubles if i.gestion_externe}
        loyer_bail_par_logement: dict[int, float] = {}
        for b in baux:
            loyer_bail_par_logement[b.logement_id] = (
                loyer_bail_par_logement.get(b.logement_id, 0.0)
                + float(b.loyer_mensuel or 0)
            )
        loyers_mensuels_par_imm: dict[int, float] = {}
        for lg in logements:
            m = loyer_effectif_loue(
                lg,
                loyer_bail_par_logement.get(lg.id),
                gestion_externe=lg.immeuble_id in externes_pnl,
            )
            if m is None:
                continue
            loyers_mensuels_par_imm[lg.immeuble_id] = (
                loyers_mensuels_par_imm.get(lg.immeuble_id, 0.0) + m
            )

        for imm in immeubles:
            loyers_mensuels = loyers_mensuels_par_imm.get(imm.id, 0.0)
            loyers = loyers_mensuels * 12
            recus = sum(
                float(p.montant or 0)
                for p in paiements
                if bail_to_imm.get(p.bail_id) == imm.id
            )
            # Dépenses EFFECTIVES : % des loyers converti en $, puis
            # fréquence (mensuel ×12 / annuel ×1 / ponctuel dans
            # l'année), puis taxes (×1.14975 si taxable) — mêmes règles
            # que l'onglet Cashflow de la fiche.
            dep = 0.0
            for d in depenses:
                if d.immeuble_id != imm.id:
                    continue
                base = float(d.montant or 0)
                if d.is_pourcentage:
                    base = loyers_mensuels * base / 100.0
                if d.frequence == "mensuel":
                    val = base * 12
                elif d.frequence == "annuel":
                    val = base
                elif d.date_depense and y_start <= d.date_depense <= y_end:
                    val = base
                else:
                    continue
                if d.taxable:
                    val *= 1.14975
                dep += val
            dette = sum(
                float(h.paiement_mensuel or 0) * 12
                for h in hypos
                if h.immeuble_id == imm.id
            )
            rows.append(
                PnlRow(
                    immeuble_id=imm.id,
                    immeuble_name=imm.name,
                    loyers_annualises=round(loyers, 2),
                    revenus_recus=round(recus, 2),
                    depenses=round(dep, 2),
                    dette_annuelle=round(dette, 2),
                    cashflow_potentiel=round(loyers - dep - dette, 2),
                    cashflow_reel=round(recus - dep - dette, 2),
                    nb_baux_actifs=sum(
                        1
                        for b in baux
                        if bail_to_imm.get(b.id) == imm.id
                    ),
                )
            )

    rows.sort(key=lambda r: r.cashflow_potentiel)
    tot = PnlRow(
        immeuble_id=0,
        immeuble_name="TOTAL",
        loyers_annualises=round(sum(r.loyers_annualises for r in rows), 2),
        revenus_recus=round(sum(r.revenus_recus for r in rows), 2),
        depenses=round(sum(r.depenses for r in rows), 2),
        dette_annuelle=round(sum(r.dette_annuelle for r in rows), 2),
        cashflow_potentiel=round(
            sum(r.cashflow_potentiel for r in rows), 2
        ),
        cashflow_reel=round(sum(r.cashflow_reel for r in rows), 2),
        nb_baux_actifs=sum(r.nb_baux_actifs for r in rows),
    )
    return PnlOverview(annee=year, rows=rows, totaux=tot)


# ── Prévisionnel (cashflow projeté) ────────────────────────────────────


class PrevisionnelMois(BaseModel):
    mois: str  # "YYYY-MM"
    revenus: float
    depenses_courantes: float
    hypotheque: float
    maintenance: float
    cashflow_net: float
    cashflow_cumule: float
    # Baux actifs dont la date de fin tombe dans ce mois (revenus à
    # risque si non renouvelés) — la projection suppose le renouvellement.
    nb_baux_echeant: int = 0
    loyers_echeant: float = 0.0


class PrevisionnelOut(BaseModel):
    rows: List[PrevisionnelMois] = []
    revenus_mensuels: float = 0.0
    depenses_mensuelles: float = 0.0
    hypotheque_mensuelle: float = 0.0
    cashflow_mensuel_base: float = 0.0
    total_maintenance_planifiee: float = 0.0
    cashflow_horizon: float = 0.0


@router.get("/finances/previsionnel", response_model=PrevisionnelOut)
async def finances_previsionnel(
    db: DBSession,
    user: CurrentUser,
    mois: int = Query(default=12, ge=1, le=24),
    entreprise_id: Optional[int] = None,
    immeuble_id: Optional[int] = None,
) -> PrevisionnelOut:
    """Projection du cashflow sur N mois (déf. 12), à partir des données
    réelles : loyers des baux ACTIFS, dépenses récurrentes (mensuel ×1,
    annuel /12) + ponctuelles datées, paiements d'hypothèque, et maintenance
    PLANIFIÉE (cout_estime imputé au mois de sa date)."""
    _require_volet(user)

    imm_q = select(Immeuble).where(Immeuble.is_active.is_(True))
    if entreprise_id is not None:
        imm_q = imm_q.where(Immeuble.owner_entreprise_id == int(entreprise_id))
    if immeuble_id is not None:
        imm_q = imm_q.where(Immeuble.id == int(immeuble_id))
    immeubles = (await db.execute(imm_q)).scalars().all()
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = [i for i in immeubles if i.id in visible]
    imm_ids = [i.id for i in immeubles]
    if not imm_ids:
        return PrevisionnelOut()

    logements = (
        await db.execute(
            select(Logement).where(Logement.immeuble_id.in_(imm_ids))
        )
    ).scalars().all()
    log_ids = [lg.id for lg in logements]
    baux = []
    if log_ids:
        baux = (
            await db.execute(
                select(Bail).where(
                    Bail.logement_id.in_(log_ids),
                    Bail.status == BailStatus.ACTIF.value,
                )
            )
        ).scalars().all()

    # Revenus mensuels LOUÉS par immeuble — hiérarchie du loyer effectif
    # (2026-08-14) : interne = bail actif d'abord ; externe = loyer
    # SAISI sur le logement d'abord (bail résiduel en simple filet).
    from app.services.loyer_effectif import loyer_effectif_loue

    externes_prev = {i.id for i in immeubles if i.gestion_externe}
    loyer_bail_par_logement: dict[int, float] = {}
    for b in baux:
        loyer_bail_par_logement[b.logement_id] = (
            loyer_bail_par_logement.get(b.logement_id, 0.0)
            + float(b.loyer_mensuel or 0)
        )
    loyers_par_imm: dict[int, float] = {}
    for lg in logements:
        m = loyer_effectif_loue(
            lg,
            loyer_bail_par_logement.get(lg.id),
            gestion_externe=lg.immeuble_id in externes_prev,
        )
        if m is None:
            continue
        loyers_par_imm[lg.immeuble_id] = (
            loyers_par_imm.get(lg.immeuble_id, 0.0) + m
        )
    revenus_mensuels = sum(loyers_par_imm.values())

    # Échéances de baux par mois — donne du relief à la projection (la
    # ligne « X baux échoient » signale où les revenus sont à risque).
    echeances_by_month: dict[str, tuple[int, float]] = {}
    for b in baux:
        if not b.date_fin:
            continue
        key = b.date_fin.strftime("%Y-%m")
        n, s = echeances_by_month.get(key, (0, 0.0))
        echeances_by_month[key] = (n + 1, s + float(b.loyer_mensuel or 0))

    depenses = (
        await db.execute(
            select(DepenseImmeuble).where(
                DepenseImmeuble.immeuble_id.in_(imm_ids)
            )
        )
    ).scalars().all()
    dep_mensuelles = 0.0
    pon_by_month: dict = {}
    for d in depenses:
        # % des loyers → $ (loyers de L'IMMEUBLE de la dépense), puis
        # taxes — mêmes règles que le Cashflow de la fiche.
        m = float(d.montant or 0)
        if d.is_pourcentage:
            m = loyers_par_imm.get(d.immeuble_id, 0.0) * m / 100.0
        if d.taxable:
            m *= 1.14975
        if d.frequence == "mensuel":
            dep_mensuelles += m
        elif d.frequence == "annuel":
            dep_mensuelles += m / 12.0
        elif d.date_depense:
            key = d.date_depense.strftime("%Y-%m")
            pon_by_month[key] = pon_by_month.get(key, 0.0) + m

    hypos = (
        await db.execute(
            select(Hypotheque).where(
                Hypotheque.immeuble_id.in_(imm_ids),
                Hypotheque.status.notin_(
                    ["remboursee", "refinancee", "REMBOURSEE", "REFINANCEE"]
                ),
            )
        )
    ).scalars().all()
    hyp_mensuelle = sum(float(h.paiement_mensuel or 0) for h in hypos)

    maints = (
        await db.execute(
            select(MaintenanceOrdre).where(
                MaintenanceOrdre.immeuble_id.in_(imm_ids),
                MaintenanceOrdre.plannifie_pour.is_not(None),
                MaintenanceOrdre.cout_estime.is_not(None),
            )
        )
    ).scalars().all()
    active_status = {"ouvert", "en_cours", "en_attente"}
    maint_by_month: dict = {}
    for mo in maints:
        if mo.status not in active_status:
            continue
        key = mo.plannifie_pour.strftime("%Y-%m")
        maint_by_month[key] = maint_by_month.get(key, 0.0) + float(
            mo.cout_estime or 0
        )

    today = datetime.now(timezone.utc).date()
    cur = today.replace(day=1)
    rows: List[PrevisionnelMois] = []
    cumule = 0.0
    total_maint = 0.0
    for _i in range(mois):
        key = cur.strftime("%Y-%m")
        maint = maint_by_month.get(key, 0.0)
        pon = pon_by_month.get(key, 0.0)
        dep_courantes = dep_mensuelles + pon
        cf = revenus_mensuels - dep_courantes - hyp_mensuelle - maint
        cumule += cf
        total_maint += maint
        nb_ech, loyers_ech = echeances_by_month.get(key, (0, 0.0))
        rows.append(
            PrevisionnelMois(
                mois=key,
                revenus=round(revenus_mensuels, 2),
                depenses_courantes=round(dep_courantes, 2),
                hypotheque=round(hyp_mensuelle, 2),
                maintenance=round(maint, 2),
                cashflow_net=round(cf, 2),
                cashflow_cumule=round(cumule, 2),
                nb_baux_echeant=nb_ech,
                loyers_echeant=round(loyers_ech, 2),
            )
        )
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)

    return PrevisionnelOut(
        rows=rows,
        revenus_mensuels=round(revenus_mensuels, 2),
        depenses_mensuelles=round(dep_mensuelles, 2),
        hypotheque_mensuelle=round(hyp_mensuelle, 2),
        cashflow_mensuel_base=round(
            revenus_mensuels - dep_mensuelles - hyp_mensuelle, 2
        ),
        total_maintenance_planifiee=round(total_maint, 2),
        cashflow_horizon=round(cumule, 2),
    )


# ── Hypothèques ────────────────────────────────────────────────────────


def _hyp_read(obj: Hypotheque) -> HypothequeRead:
    """HypothequeRead + balance THÉORIQUE du jour (amortissement)."""
    from app.services.hypotheque_calc import balance_calculee_de

    r = HypothequeRead.model_validate(obj)
    r.balance_calculee = balance_calculee_de(obj)
    return r


@router.get(
    "/immeubles/{immeuble_id}/hypotheques",
    response_model=List[HypothequeRead],
)
async def list_hypotheques(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> List[HypothequeRead]:
    _require_volet(user)
    rows = (
        await db.execute(
            select(Hypotheque)
            .where(Hypotheque.immeuble_id == immeuble_id)
            .order_by(Hypotheque.rang.asc())
        )
    ).scalars().all()
    return [_hyp_read(r) for r in rows]


def _pmt_hypotheque(obj: Hypotheque) -> float | None:
    """Paiement mensuel calculé depuis taux/amortissement/composition.

    Même math que le frontend (fiche immeuble) : composition
    semi-annuelle (standard résidentiel CA) ou mensuelle (commercial/
    variable), selon ``composition_interets``. Retourne None si les
    intrants manquent.
    """
    taux = float(obj.taux_pct) if obj.taux_pct is not None else None
    n = int(obj.amortissement_mois or 0)
    principal = float(
        obj.balance_actuelle
        if obj.balance_actuelle is not None
        else (obj.montant_initial or 0)
    )
    if taux is None or n <= 0 or principal <= 0:
        return None
    if (obj.composition_interets or "semi") == "mensuelle":
        i = taux / 100.0 / 12.0
    else:
        i = (1.0 + taux / 100.0 / 2.0) ** (2.0 / 12.0) - 1.0
    if i <= 0:
        return round(principal / n, 2)
    return round(principal * i / (1.0 - (1.0 + i) ** (-n)), 2)


def _maybe_recompute_pmt(obj: Hypotheque) -> None:
    """Si aucun paiement mensuel n'est fourni (créations API/MCP), le
    serveur le calcule — la fiche, le cashflow et les financials lisent
    tous la valeur persistée."""
    if obj.paiement_mensuel is None:
        pmt = _pmt_hypotheque(obj)
        if pmt is not None:
            obj.paiement_mensuel = pmt


@router.post(
    "/hypotheques",
    response_model=HypothequeRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_hypotheque(
    payload: HypothequeCreate, db: DBSession, user: CurrentUser
) -> HypothequeRead:
    _require_volet(user)
    await _get_immeuble_or_404(db, payload.immeuble_id)
    obj = Hypotheque(**payload.model_dump())
    _maybe_recompute_pmt(obj)
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _hyp_read(obj)


@router.patch("/hypotheques/{hyp_id}", response_model=HypothequeRead)
async def update_hypotheque(
    hyp_id: int,
    payload: HypothequeUpdate,
    db: DBSession,
    user: CurrentUser,
) -> HypothequeRead:
    _require_volet(user)
    obj = await db.get(Hypotheque, hyp_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Hypothèque introuvable.")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(obj, k, v)
    # Un intrant du calcul change sans paiement explicite → on recalcule
    # pour que la liste/cashflow/financials reflètent la modification.
    calc_keys = {
        "taux_pct", "amortissement_mois", "composition_interets",
        "balance_actuelle", "montant_initial",
    }
    if calc_keys & data.keys() and "paiement_mensuel" not in data:
        pmt = _pmt_hypotheque(obj)
        if pmt is not None:
            obj.paiement_mensuel = pmt
    else:
        _maybe_recompute_pmt(obj)
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return _hyp_read(obj)


@router.delete(
    "/hypotheques/{hyp_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_hypotheque(
    hyp_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("hypotheque.delete"))],
) -> None:
    _require_volet(user)
    obj = await db.get(Hypotheque, hyp_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Hypothèque introuvable.")
    await db.delete(obj)
    await db.commit()


# ── Évaluations ────────────────────────────────────────────────────────


@router.get(
    "/immeubles/{immeuble_id}/evaluations",
    response_model=List[EvaluationRead],
)
async def list_evaluations(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> List[EvaluationRead]:
    _require_volet(user)
    rows = (
        await db.execute(
            select(Evaluation)
            .where(Evaluation.immeuble_id == immeuble_id)
            .order_by(Evaluation.date_evaluation.desc())
        )
    ).scalars().all()
    return [EvaluationRead.model_validate(r) for r in rows]


@router.post(
    "/evaluations",
    response_model=EvaluationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_evaluation(
    payload: EvaluationCreate, db: DBSession, user: CurrentUser
) -> EvaluationRead:
    _require_volet(user)
    await _get_immeuble_or_404(db, payload.immeuble_id)
    if payload.is_reference:
        # Une seule évaluation de référence par immeuble.
        await db.execute(
            update(Evaluation)
            .where(Evaluation.immeuble_id == payload.immeuble_id)
            .values(is_reference=False)
        )
    obj = Evaluation(**payload.model_dump())
    obj.created_at = _now()
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return EvaluationRead.model_validate(obj)


@router.patch("/evaluations/{eval_id}", response_model=EvaluationRead)
async def update_evaluation(
    eval_id: int,
    payload: EvaluationUpdate,
    db: DBSession,
    user: CurrentUser,
) -> EvaluationRead:
    """Modifie une évaluation (valeur, type, date, source, notes) et/ou
    la marque comme référence pour le calcul d'équité.

    Passer ``is_reference=True`` remet automatiquement à False les
    autres évaluations du même immeuble (une seule référence)."""
    _require_volet(user)
    obj = await db.get(Evaluation, eval_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Évaluation introuvable.")
    data = payload.model_dump(exclude_unset=True)
    if data.get("is_reference") is True:
        await db.execute(
            update(Evaluation)
            .where(
                and_(
                    Evaluation.immeuble_id == obj.immeuble_id,
                    Evaluation.id != obj.id,
                )
            )
            .values(is_reference=False)
        )
    for k, v in data.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return EvaluationRead.model_validate(obj)


@router.delete(
    "/evaluations/{eval_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_evaluation(
    eval_id: int,
    db: DBSession,
    user: Annotated[User, Depends(require_capability("evaluation.delete"))],
) -> None:
    _require_volet(user)
    obj = await db.get(Evaluation, eval_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Évaluation introuvable.")
    await db.delete(obj)
    await db.commit()


# ── Maintenance ────────────────────────────────────────────────────────


@router.get(
    "/immeubles/{immeuble_id}/maintenance",
    response_model=List[MaintenanceOrdreRead],
)
async def list_maintenance(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> List[MaintenanceOrdreRead]:
    _require_volet(user)
    rows = (
        await db.execute(
            select(MaintenanceOrdre)
            .where(MaintenanceOrdre.immeuble_id == immeuble_id)
            .order_by(MaintenanceOrdre.created_at.desc())
        )
    ).scalars().all()
    return [MaintenanceOrdreRead.model_validate(r) for r in rows]


@router.post(
    "/maintenance",
    response_model=MaintenanceOrdreRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_maintenance(
    payload: MaintenanceOrdreCreate, db: DBSession, user: CurrentUser
) -> MaintenanceOrdreRead:
    _require_volet(user)
    await _get_immeuble_or_404(db, payload.immeuble_id)
    obj = MaintenanceOrdre(**payload.model_dump())
    obj.created_at = _now()
    obj.updated_at = _now()
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return MaintenanceOrdreRead.model_validate(obj)


@router.patch(
    "/maintenance/{ordre_id}", response_model=MaintenanceOrdreRead
)
async def update_maintenance(
    ordre_id: int,
    payload: MaintenanceOrdreUpdate,
    db: DBSession,
    user: CurrentUser,
) -> MaintenanceOrdreRead:
    _require_volet(user)
    obj = await db.get(MaintenanceOrdre, ordre_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Ordre introuvable.")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.updated_at = _now()
    await db.commit()
    await db.refresh(obj)
    return MaintenanceOrdreRead.model_validate(obj)


@router.delete(
    "/maintenance/{ordre_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_maintenance(
    ordre_id: int, db: DBSession, user: CurrentUser
) -> None:
    _require_volet(user)
    obj = await db.get(MaintenanceOrdre, ordre_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Ordre introuvable.")
    await db.delete(obj)
    await db.commit()


_MAINT_ACTIVE = {"ouvert", "en_cours", "en_attente"}
_MAINT_STATUS_RANK = {
    "ouvert": 0, "en_cours": 1, "en_attente": 2, "termine": 3, "annule": 4,
}
_MAINT_PRIO_RANK = {"urgence": 0, "haute": 1, "normale": 2, "basse": 3}


@router.get("/maintenance/overview", response_model=MaintenanceOverview)
async def maintenance_overview(
    db: DBSession,
    user: CurrentUser,
    statut: Optional[str] = None,
    priorite: Optional[str] = None,
    immeuble_id: Optional[int] = None,
    inclure_termines: bool = False,
) -> MaintenanceOverview:
    """Vue transversale des ordres de maintenance sur tout le portefeuille.

    Les KPIs reflètent l'ensemble des ordres visibles ; les filtres
    (statut / priorité / immeuble) ne s'appliquent qu'aux lignes affichées.
    Tri : actifs d'abord, puis par priorité, puis du plus ancien (le plus
    en retard) au plus récent.
    """
    _require_volet(user)

    immeubles = (await db.execute(select(Immeuble))).scalars().all()
    visible = await visible_immeuble_ids(db, user)
    if visible is not None:
        immeubles = [i for i in immeubles if i.id in visible]
    if immeuble_id is not None:
        immeubles = [i for i in immeubles if i.id == int(immeuble_id)]
    imm_by_id = {i.id: i for i in immeubles}
    if not imm_by_id:
        return MaintenanceOverview(rows=[])

    ordres = (
        await db.execute(
            select(MaintenanceOrdre).where(
                MaintenanceOrdre.immeuble_id.in_(list(imm_by_id.keys()))
            )
        )
    ).scalars().all()

    log_ids = {o.logement_id for o in ordres if o.logement_id}
    log_by_id = {}
    if log_ids:
        for lg in (
            await db.execute(
                select(Logement).where(Logement.id.in_(list(log_ids)))
            )
        ).scalars().all():
            log_by_id[lg.id] = lg

    today = datetime.now(timezone.utc).date()
    kpi = {"ouvert": 0, "en_cours": 0, "en_attente": 0, "termine": 0, "annule": 0}
    nb_urg = 0
    tot_est = 0.0
    tot_reel = 0.0
    rows: List[MaintenanceOverviewRow] = []

    for o in ordres:
        if o.status in kpi:
            kpi[o.status] += 1
        active = o.status in _MAINT_ACTIVE
        if active and o.priorite == "urgence":
            nb_urg += 1
        if active and o.cout_estime is not None:
            tot_est += float(o.cout_estime)
        if o.cout_reel is not None:
            tot_reel += float(o.cout_reel)

        # Filtres d'affichage.
        if statut and o.status != statut:
            continue
        if priorite and o.priorite != priorite:
            continue
        if not inclure_termines and not statut and not active:
            continue

        jours = (
            (today - o.created_at.date()).days
            if (o.created_at and active)
            else None
        )
        lg = log_by_id.get(o.logement_id) if o.logement_id else None
        rows.append(
            MaintenanceOverviewRow(
                id=o.id,
                immeuble_id=o.immeuble_id,
                immeuble_name=imm_by_id[o.immeuble_id].name,
                logement_id=o.logement_id,
                logement_numero=(lg.numero if lg else None),
                titre=o.titre,
                description=o.description,
                priorite=o.priorite,
                status=o.status,
                fournisseur=o.fournisseur,
                cout_estime=(
                    float(o.cout_estime) if o.cout_estime is not None else None
                ),
                cout_reel=(
                    float(o.cout_reel) if o.cout_reel is not None else None
                ),
                plannifie_pour=o.plannifie_pour,
                complete_le=o.complete_le,
                created_at=o.created_at,
                jours_ouverts=jours,
            )
        )

    rows.sort(
        key=lambda r: (
            _MAINT_STATUS_RANK.get(r.status, 9),
            _MAINT_PRIO_RANK.get(r.priorite, 9),
            -(r.jours_ouverts or 0),
        )
    )
    return MaintenanceOverview(
        rows=rows,
        nb_total=len(rows),
        nb_ouvert=kpi["ouvert"],
        nb_en_cours=kpi["en_cours"],
        nb_en_attente=kpi["en_attente"],
        nb_termine=kpi["termine"],
        nb_annule=kpi["annule"],
        nb_urgences_actives=nb_urg,
        total_cout_estime_actif=round(tot_est, 2),
        total_cout_reel=round(tot_reel, 2),
    )


# ── Cockpit « À traiter » ──────────────────────────────────────────────


class ATraiterOut(BaseModel):
    loyers_retard_nb: int = 0
    loyers_retard_total: float = 0.0
    baux_a_renouveler_nb: int = 0
    maintenance_urgente_nb: int = 0
    depots_a_rendre_nb: int = 0
    depots_a_rendre_total: float = 0.0


@router.get("/a-traiter", response_model=ATraiterOut)
async def a_traiter(db: DBSession, user: CurrentUser) -> ATraiterOut:
    """Cockpit « À traiter » : agrège tout ce qui demande une action —
    loyers en retard, baux à renouveler, maintenance urgente, dépôts à
    rendre. Un coup d'œil pour ne rien échapper.

    Les immeubles en gestion externe sont exclus des compteurs loyers /
    renouvellements / dépôts via les overviews délégués ci-dessous."""
    _require_volet(user)
    # Appels internes : on passe TOUS les paramètres optionnels
    # explicitement (en appel direct, les défauts Query() ne valent pas None).
    lo = await loyers_overview(
        db=db, user=user, mois=None, entreprise_id=None
    )
    ech = await baux_echeances(
        db=db, user=user, entreprise_id=None, immeuble_id=None,
        horizon_jours=45
    )
    maint = await maintenance_overview(
        db=db,
        user=user,
        statut=None,
        priorite=None,
        immeuble_id=None,
        inclure_termines=False,
    )
    dep = await depots_overview(db=db, user=user, entreprise_id=None)

    retard_total = sum(
        float(r.loyer_mensuel or 0) for r in lo.rows if r.etat == "retard"
    )
    return ATraiterOut(
        loyers_retard_nb=lo.nb_retards,
        loyers_retard_total=round(retard_total, 2),
        baux_a_renouveler_nb=ech.nb_a_envoyer + ech.nb_en_retard,
        maintenance_urgente_nb=maint.nb_urgences_actives,
        depots_a_rendre_nb=dep.nb_a_rendre,
        depots_a_rendre_total=dep.total_a_rendre,
    )


# ── KPIs financiers d'un immeuble ──────────────────────────────────────


@router.get(
    "/immeubles/{immeuble_id}/financials",
    response_model=ImmeubleFinancials,
)
async def get_financials(
    immeuble_id: int, db: DBSession, user: CurrentUser
) -> ImmeubleFinancials:
    _require_volet(user)
    imm = await _get_immeuble_or_404(db, immeuble_id)

    # Logements par statut
    log_rows = (
        await db.execute(
            select(Logement.status, func.count(Logement.id))
            .where(Logement.immeuble_id == immeuble_id)
            .group_by(Logement.status)
        )
    ).all()
    sts = {st: int(n) for st, n in log_rows}
    nb_actifs = sum(
        n for st, n in sts.items() if st != LogementStatus.HORS_LOC.value
    )
    nb_occ = sts.get(LogementStatus.OCCUPE.value, 0)
    taux = (nb_occ / nb_actifs) if nb_actifs > 0 else 0.0

    # Revenu brut mensuel PAR LOGEMENT : loyer du bail actif s'il existe,
    # SINON loyer demandé du logement (retour Phil 2026-07-10 : un
    # immeuble en gestion externe n'a pas de baux dans Kratos mais ses
    # loyers sont connus — le revenu ne doit pas afficher 0). Les
    # logements hors location sont exclus du fallback.
    logements_imm = (
        await db.execute(
            select(Logement).where(Logement.immeuble_id == immeuble_id)
        )
    ).scalars().all()
    baux_actifs_par_logement: dict[int, float] = {}
    for b in (
        await db.execute(
            select(Bail)
            .join(Logement, Logement.id == Bail.logement_id)
            .where(
                and_(
                    Logement.immeuble_id == immeuble_id,
                    Bail.status == BailStatus.ACTIF.value,
                )
            )
        )
    ).scalars().all():
        baux_actifs_par_logement[b.logement_id] = baux_actifs_par_logement.get(
            b.logement_id, 0.0
        ) + float(b.loyer_mensuel or 0)
    # `revenu` = unités LOUÉES seulement — hiérarchie du loyer effectif
    # (2026-08-14) : interne = bail actif d'abord ; externe = loyer
    # SAISI sur le logement d'abord (un bail résiduel ne masque plus la
    # saisie). `revenu_toutes_unites` = potentiel : + loyer demandé des
    # vacantes (retour Phil 2026-07-16 : le montant principal doit
    # refléter ce qui rentre vraiment ; le potentiel en petit à côté).
    from app.services.loyer_effectif import loyer_effectif

    imm_externe = bool(getattr(imm, "gestion_externe", False))
    revenu = 0.0
    revenu_toutes_unites = 0.0
    for lg in logements_imm:
        loyer_bail = baux_actifs_par_logement.get(lg.id)
        m_eff = loyer_effectif(lg, loyer_bail, imm_externe)
        if loyer_bail is not None or (
            lg.status == LogementStatus.OCCUPE.value and m_eff is not None
        ):
            revenu += m_eff or 0.0
            revenu_toutes_unites += m_eff or 0.0
        elif (
            lg.status != LogementStatus.HORS_LOC.value
            and m_eff is not None
        ):
            revenu_toutes_unites += m_eff

    # Hypothèques actives. Balance EFFECTIVE par hypothèque : la balance
    # saisie prime, sinon la balance CALCULÉE au jour J (tableau
    # d'amortissement — « s'update toute seule », retour Phil 2026-07-10),
    # sinon le montant initial.
    from app.services.hypotheque_calc import balance_effective

    hyps_actives = (
        await db.execute(
            select(Hypotheque).where(
                and_(
                    Hypotheque.immeuble_id == immeuble_id,
                    Hypotheque.status == HypothequeStatus.ACTIVE.value,
                )
            )
        )
    ).scalars().all()
    paiement_hyp = sum(float(h.paiement_mensuel or 0) for h in hyps_actives)
    balance_hyp = round(sum(balance_effective(h) for h in hyps_actives), 2)

    # Valeur actuelle : l'évaluation marquée « référence » prime ;
    # sinon fallback sur la plus récente (toutes catégories).
    val_row = (
        await db.execute(
            select(Evaluation.valeur)
            .where(
                and_(
                    Evaluation.immeuble_id == immeuble_id,
                    Evaluation.is_reference.is_(True),
                )
            )
            .order_by(Evaluation.date_evaluation.desc())
            .limit(1)
        )
    ).scalar()
    if val_row is None:
        val_row = (
            await db.execute(
                select(Evaluation.valeur)
                .where(Evaluation.immeuble_id == immeuble_id)
                .order_by(Evaluation.date_evaluation.desc())
                .limit(1)
            )
        ).scalar()
    valeur_actuelle = float(val_row) if val_row is not None else None

    # Valeur municipale (la plus récente du kind=municipale)
    val_muni = (
        await db.execute(
            select(Evaluation.valeur)
            .where(
                and_(
                    Evaluation.immeuble_id == immeuble_id,
                    Evaluation.kind == EvaluationKind.MUNICIPALE.value,
                )
            )
            .order_by(Evaluation.date_evaluation.desc())
            .limit(1)
        )
    ).scalar()
    valeur_municipale = float(val_muni) if val_muni is not None else None

    # Si pas d'évaluation, fallback sur prix d'achat ou valeur municipale
    valeur_pour_ratios = (
        valeur_actuelle
        or valeur_municipale
        or (float(imm.purchase_price) if imm.purchase_price else None)
    )

    # Dépenses récurrentes mensualisées (même logique que l'onglet
    # Cashflow frontend) : mensuel → montant ; annuel → montant / 12 ;
    # ponctuel → exclu du flux récurrent. is_pourcentage → le montant
    # est un % des loyers mensuels. taxable → TPS+TVQ Québec ×1.14975.
    dep_rows = (
        await db.execute(
            select(
                DepenseImmeuble.montant,
                DepenseImmeuble.frequence,
                DepenseImmeuble.is_pourcentage,
                DepenseImmeuble.taxable,
            ).where(
                and_(
                    DepenseImmeuble.immeuble_id == immeuble_id,
                    DepenseImmeuble.frequence.in_(("mensuel", "annuel")),
                )
            )
        )
    ).all()
    depenses_mensuelles = 0.0
    for montant, frequence, is_pct, taxable in dep_rows:
        m = float(montant or 0)
        if is_pct:
            m = revenu * m / 100.0
        if frequence == "annuel":
            m = m / 12.0
        if taxable:
            m *= 1.14975
        depenses_mensuelles += m

    revenu_annuel = revenu * 12
    grm = (
        round(valeur_pour_ratios / revenu_annuel, 2)
        if valeur_pour_ratios and revenu_annuel > 0
        else None
    )
    # NOI : réel si ≥1 dépense récurrente saisie (revenus − dépenses
    # d'exploitation annualisées, SANS hypothèque), sinon fallback
    # heuristique NOI ≈ 50 % du revenu brut (règle du 50 %). Le flag
    # cap_rate_estime permet au front d'adapter le libellé.
    cap_rate_estime = len(dep_rows) == 0
    if cap_rate_estime:
        noi_annuel = revenu_annuel * 0.5
    else:
        noi_annuel = revenu_annuel - depenses_mensuelles * 12
    cap_rate = (
        round((noi_annuel / valeur_pour_ratios) * 100, 2)
        if valeur_pour_ratios and valeur_pour_ratios > 0
        else None
    )
    # Cash flow mensuel récurrent = loyers actifs − dépenses récurrentes
    # mensualisées − paiements hypothécaires actifs (aligné sur l'onglet
    # Cashflow frontend — le KPI du haut doit raconter la même histoire).
    cash_flow = round(revenu - depenses_mensuelles - paiement_hyp, 2)
    appreciation = None
    if imm.purchase_price and valeur_pour_ratios and float(imm.purchase_price) > 0:
        appreciation = round(
            ((valeur_pour_ratios - float(imm.purchase_price))
             / float(imm.purchase_price)) * 100,
            2,
        )

    return ImmeubleFinancials(
        immeuble_id=immeuble_id,
        nb_logements_actifs=nb_actifs,
        nb_logements_occupes=nb_occ,
        taux_occupation=round(taux, 4),
        revenu_brut_mensuel=round(revenu, 2),
        revenu_brut_annuel=round(revenu_annuel, 2),
        revenu_brut_mensuel_toutes_unites=round(revenu_toutes_unites, 2),
        paiement_hypotheque_mensuel=round(paiement_hyp, 2),
        balance_hypothecaire=round(balance_hyp, 2),
        valeur_actuelle=valeur_actuelle,
        valeur_municipale=valeur_municipale,
        purchase_price=float(imm.purchase_price) if imm.purchase_price else None,
        grm=grm,
        cap_rate=cap_rate,
        cap_rate_estime=cap_rate_estime,
        cash_flow_mensuel=cash_flow,
        appreciation_pct=appreciation,
    )


# ── Import depuis le rôle d'évaluation MAMH ─────────────────────────────


@router.post(
    "/immeubles/import-matricule",
    response_model=ImmeubleImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_immeuble_from_matricule(
    payload: ImmeubleImportFromMatriculeRequest,
    db: DBSession,
    user: CurrentUser,
) -> ImmeubleImportResult:
    """Crée un immeuble à partir d'un matricule MAMH déjà importé.

    Récupère depuis mtl_property_units :
    - adresse, code postal, municipalité
    - nb_logements
    - année de construction
    - superficies
    - valeur municipale (création d'une Evaluation kind=municipale)

    Si create_logements=True, crée des shells de logements (Apt 1..N)
    sans loyer ni statut — à compléter manuellement.
    """
    _require_volet(user)

    unit = (
        await db.execute(
            select(MontrealPropertyUnit).where(
                MontrealPropertyUnit.matricule == payload.matricule
            )
        )
    ).scalar_one_or_none()
    if unit is None:
        raise HTTPException(
            status_code=404,
            detail=f"Matricule {payload.matricule!r} introuvable dans le rôle d'évaluation.",
        )

    # Adresse complète depuis le rôle d'évaluation
    parts: List[str] = []
    civique = unit.civique_debut or ""
    if unit.civique_fin and unit.civique_fin != civique:
        civique = f"{civique}-{unit.civique_fin}" if civique else unit.civique_fin
    if civique:
        parts.append(str(civique))
    if unit.nom_rue:
        parts.append(str(unit.nom_rue))
    address = " ".join(parts) or "Adresse à compléter"

    name = payload.name or address
    nb_logements = unit.nombre_logement
    superficie_terrain = (
        float(unit.superficie_terrain) if unit.superficie_terrain else None
    )
    superficie_batiment = (
        float(unit.superficie_batiment) if unit.superficie_batiment else None
    )

    imm = Immeuble(
        name=name,
        address=address,
        city=unit.municipalite,
        type=ImmeubleType.RESIDENTIEL.value,
        annee_construction=unit.annee_construction,
        nb_logements=nb_logements,
        superficie_terrain=superficie_terrain,
        superficie_batiment=superficie_batiment,
        matricule=payload.matricule,
        is_active=True,
    )
    imm.created_at = _now()
    imm.updated_at = _now()
    db.add(imm)
    await db.flush()  # pour récupérer imm.id

    nb_crees = 0
    if payload.create_logements and nb_logements:
        for i in range(1, int(nb_logements) + 1):
            log_obj = Logement(
                immeuble_id=imm.id,
                numero=f"Apt {i}",
                type=ImmeubleType.RESIDENTIEL.value,
                status=LogementStatus.VACANT.value,
            )
            log_obj.created_at = _now()
            log_obj.updated_at = _now()
            db.add(log_obj)
            nb_crees += 1

    await db.commit()
    await db.refresh(imm)
    return ImmeubleImportResult(
        immeuble=_immeuble_to_read(imm),
        nb_logements_crees=nb_crees,
        matched_unit_id=getattr(unit, "id", None),
    )


# ── Import « rent roll » PlexFlow (copier-coller) ──────────────────────


def _norm_company(name: str) -> str:
    """Normalise un nom de compagnie pour le matching : minuscules, sans
    ponctuation, et sans les suffixes juridiques (« inc », « québec inc »,
    « ltée », etc.) que PlexFlow ajoute mais pas forcément Kratos.
    Ainsi « 9417-1287 Québec Inc. » et « 9417-1287 » correspondent."""
    s = (name or "").lower()
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(
        r"\b(inc|québec|quebec|ltée|ltee|enr|senc|cie|co)\b", " ", s
    )
    return re.sub(r"\s+", " ", s).strip()


def _norm_address(addr: str) -> str:
    return re.sub(r"\s+", " ", (addr or "").strip().lower()).rstrip(",.")


@router.post("/import-plexflow", response_model=PlexImportResult)
async def import_plexflow(
    payload: PlexImportRequest, db: DBSession, user: CurrentUser
) -> PlexImportResult:
    """Parse un rent roll collé depuis PlexFlow et (si `dry_run=False`)
    crée immeubles + logements + locataires + baux, rattachés à la
    compagnie correspondante (match par nom). `dry_run=True` retourne
    seulement l'aperçu sans rien écrire."""
    _require_volet(user)
    companies, warnings = parse_plexflow(payload.raw_text)

    ent_rows = (await db.execute(select(Entreprise))).scalars().all()
    by_norm: dict[str, Entreprise] = {}
    for e in ent_rows:
        by_norm.setdefault(_norm_company(e.name), e)

    created = PlexImportCreated()
    out_companies: list[PlexImportCompany] = []

    # PlexFlow ne fournit pas les dates de bail : valeurs par défaut.
    today = _now().date()
    default_debut = today.replace(day=1)
    default_fin = default_debut + timedelta(days=365)
    import_note = (
        f"Importé de PlexFlow le {today.isoformat()} — dates à confirmer."
    )

    for comp in companies:
        # 1) override explicite fourni par l'utilisateur, sinon 2) match
        #    automatique par nom normalisé.
        ent = None
        override_id = payload.company_overrides.get(comp.name)
        if override_id:
            ent = await db.get(Entreprise, override_id)
        if ent is None:
            ent = by_norm.get(_norm_company(comp.name))
        oc = PlexImportCompany(
            name=comp.name,
            entreprise_id=ent.id if ent else None,
            matched=ent is not None,
        )

        existing_addr: set[str] = set()
        if ent is not None:
            rows = (
                await db.execute(
                    select(Immeuble.address)
                    .join(
                        ImmeubleOwnership,
                        ImmeubleOwnership.immeuble_id == Immeuble.id,
                    )
                    .where(ImmeubleOwnership.entreprise_id == ent.id)
                )
            ).scalars().all()
            existing_addr = {_norm_address(a) for a in rows if a}

        for b in comp.buildings:
            dup = _norm_address(b.address) in existing_addr
            units_out: list[PlexImportUnit] = []
            leases = 0
            for u in b.units:
                will_lease = bool(
                    u.tenant and u.rent and u.status in ("active", "scheduled")
                )
                if will_lease:
                    leases += 1
                units_out.append(
                    PlexImportUnit(
                        numero=u.numero,
                        tenant=u.tenant,
                        rent=u.rent,
                        status=u.status,
                        will_create_lease=will_lease,
                        warnings=list(u.warnings),
                    )
                )
            ob = PlexImportBuilding(
                address=b.address,
                city=b.city,
                postal_code=b.postal_code,
                nb_units=len(b.units),
                nb_leases=leases,
                already_exists=dup,
                units=units_out,
                warnings=list(b.warnings),
            )

            if not payload.dry_run and ent is not None and not dup:
                imm = Immeuble(
                    name=f"{b.address}, {b.city}" if b.city else b.address,
                    address=b.address,
                    city=b.city,
                    postal_code=b.postal_code,
                    type=ImmeubleType.RESIDENTIEL.value,
                    nb_logements=len(b.units),
                    is_active=True,
                )
                imm.created_at = _now()
                imm.updated_at = _now()
                db.add(imm)
                await db.flush()
                db.add(
                    ImmeubleOwnership(
                        immeuble_id=imm.id,
                        entreprise_id=ent.id,
                        ownership_pct=100.0,
                    )
                )
                created.immeubles += 1
                existing_addr.add(_norm_address(b.address))

                for u, pu in zip(b.units, units_out):
                    if pu.will_create_lease and u.status == "active":
                        lstatus = LogementStatus.OCCUPE.value
                    elif pu.will_create_lease and u.status == "scheduled":
                        lstatus = LogementStatus.RESERVE.value
                    else:
                        lstatus = LogementStatus.VACANT.value
                    log_obj = Logement(
                        immeuble_id=imm.id,
                        numero=(u.numero or "—")[:32],
                        type=ImmeubleType.RESIDENTIEL.value,
                        status=lstatus,
                        loyer_demande=u.rent,
                    )
                    log_obj.created_at = _now()
                    log_obj.updated_at = _now()
                    db.add(log_obj)
                    await db.flush()
                    created.logements += 1

                    if pu.will_create_lease:
                        loc = Locataire(full_name=(u.tenant or "")[:255])
                        loc.created_at = _now()
                        loc.updated_at = _now()
                        db.add(loc)
                        await db.flush()
                        created.locataires += 1
                        bail = Bail(
                            logement_id=log_obj.id,
                            locataire_id=loc.id,
                            date_debut=default_debut,
                            date_fin=default_fin,
                            loyer_mensuel=u.rent,
                            status=(
                                BailStatus.ACTIF.value
                                if u.status == "active"
                                else BailStatus.PROPOSE.value
                            ),
                            notes=import_note,
                        )
                        bail.created_at = _now()
                        bail.updated_at = _now()
                        db.add(bail)
                        created.baux += 1
            elif not payload.dry_run and dup:
                created.buildings_skipped += 1

            oc.buildings.append(ob)

        if not oc.matched:
            warnings.append(
                f"Compagnie « {comp.name} » introuvable dans Kratos — "
                "ses immeubles n'ont pas été importés."
            )
        out_companies.append(oc)

    if not payload.dry_run:
        await db.commit()

    totals = {
        "companies": len(out_companies),
        "companies_matched": sum(1 for c in out_companies if c.matched),
        "buildings": sum(len(c.buildings) for c in out_companies),
        "buildings_duplicate": sum(
            1 for c in out_companies for b in c.buildings if b.already_exists
        ),
        "units": sum(b.nb_units for c in out_companies for b in c.buildings),
        "leases": sum(b.nb_leases for c in out_companies for b in c.buildings),
    }

    return PlexImportResult(
        dry_run=payload.dry_run,
        companies=out_companies,
        totals=totals,
        created=None if payload.dry_run else created,
        warnings=warnings,
    )
