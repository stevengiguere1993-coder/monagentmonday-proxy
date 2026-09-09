"""Dossiers TAL + garants/contacts d'un locataire (retour Phil 2026-09-09).

    GET    /immobilier/baux/{bail_id}/tal-dossiers
    POST   /immobilier/baux/{bail_id}/tal-dossiers
    GET    /immobilier/tal-dossiers?statut=&immeuble_id=&locataire_id=&logement_id=
    GET    /immobilier/tal-dossiers/{id}          (+ ses documents)
    PATCH  /immobilier/tal-dossiers/{id}

    GET    /immobilier/locataires/{id}/contacts
    POST   /immobilier/locataires/{id}/contacts
    PATCH  /immobilier/locataire-contacts/{id}
    DELETE /immobilier/locataire-contacts/{id}

Simple par design (Phil : « rien de compliqué ») : pas de chronologie,
pas de champs juridiques. Les pièces d'un dossier TAL sont des
``imm_documents`` rattachés par ``tal_dossier_id`` (import via
POST /immobilier/documents/import). La date ``Bail.tal_dossier_ouvert_le``
reste un MIROIR (services/tal_garants.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession
from app.api.v1.endpoints.immobilier_documents import DocumentRead, _doc_read
from app.models.immobilier import (
    TAL_STATUT_FERME,
    TAL_STATUTS,
    Bail,
    ImmDocument,
    ImmLocataireContact,
    ImmTalDossier,
    Immeuble,
    Locataire,
    Logement,
)
from app.schemas.immobilier import (
    LocataireContactCreate,
    LocataireContactRead,
    LocataireContactUpdate,
    TalDossierCreate,
    TalDossierRead,
    TalDossierUpdate,
)
from app.services.tal_garants import ouvrir_dossier, refleter_dossiers_sur_bail

log = logging.getLogger(__name__)

router = APIRouter(prefix="/immobilier", tags=["immobilier-tal-garants"])


def _require_volet(user: CurrentUser) -> None:
    volets = getattr(user, "volets", None)
    if volets is None or "immobilier" not in volets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Volet « Gestion immobilière » non autorisé.",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Dossiers TAL ───────────────────────────────────────────────────────


class TalDossierDetail(TalDossierRead):
    """Dossier + ses pièces (documents rattachés par ``tal_dossier_id``)."""

    documents: List[DocumentRead] = Field(default_factory=list)


async def _enrichir(db, rows: List[ImmTalDossier]) -> List[TalDossierRead]:
    """Ajoute nom du locataire, immeuble, numéro de logement et nombre de
    pièces — chargements groupés (pas de N+1)."""
    if not rows:
        return []
    loc_ids = {d.locataire_id for d in rows if d.locataire_id}
    log_ids = {d.logement_id for d in rows if d.logement_id}
    imm_ids = {d.immeuble_id for d in rows if d.immeuble_id}
    locs: dict[int, Locataire] = {}
    if loc_ids:
        for loc in (
            await db.execute(select(Locataire).where(Locataire.id.in_(loc_ids)))
        ).scalars().all():
            locs[loc.id] = loc
    logs: dict[int, Logement] = {}
    if log_ids:
        for lg in (
            await db.execute(select(Logement).where(Logement.id.in_(log_ids)))
        ).scalars().all():
            logs[lg.id] = lg
    imms: dict[int, Immeuble] = {}
    if imm_ids:
        for im in (
            await db.execute(select(Immeuble).where(Immeuble.id.in_(imm_ids)))
        ).scalars().all():
            imms[im.id] = im
    nb_docs: dict[int, int] = {}
    for did, n in (
        await db.execute(
            select(ImmDocument.tal_dossier_id, func.count(ImmDocument.id))
            .where(ImmDocument.tal_dossier_id.in_([d.id for d in rows]))
            .group_by(ImmDocument.tal_dossier_id)
        )
    ).all():
        nb_docs[int(did)] = int(n)
    out: List[TalDossierRead] = []
    for d in rows:
        item = TalDossierRead.model_validate(d)
        loc = locs.get(d.locataire_id) if d.locataire_id else None
        lg = logs.get(d.logement_id) if d.logement_id else None
        im = imms.get(d.immeuble_id) if d.immeuble_id else None
        item.locataire_name = loc.full_name if loc else None
        item.logement_numero = lg.numero if lg else None
        item.immeuble_name = im.name if im else None
        item.nb_documents = nb_docs.get(d.id, 0)
        out.append(item)
    return out


@router.get("/baux/{bail_id}/tal-dossiers", response_model=List[TalDossierRead])
async def list_tal_dossiers_bail(
    bail_id: int, db: DBSession, user: CurrentUser
) -> List[TalDossierRead]:
    _require_volet(user)
    if await db.get(Bail, bail_id) is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    rows = (
        await db.execute(
            select(ImmTalDossier)
            .where(ImmTalDossier.bail_id == bail_id)
            .order_by(ImmTalDossier.id.desc())
        )
    ).scalars().all()
    return await _enrichir(db, list(rows))


@router.post(
    "/baux/{bail_id}/tal-dossiers",
    response_model=TalDossierRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_tal_dossier(
    bail_id: int,
    payload: TalDossierCreate,
    db: DBSession,
    user: CurrentUser,
) -> TalDossierRead:
    """Ouvre un dossier TAL sur ce bail. La date miroir du bail
    (``tal_dossier_ouvert_le``) suit automatiquement."""
    _require_volet(user)
    bail = await db.get(Bail, bail_id)
    if bail is None:
        raise HTTPException(status_code=404, detail="Bail introuvable.")
    d = await ouvrir_dossier(
        db, bail,
        motif=payload.motif,
        statut=payload.statut,
        numero_dossier=payload.numero_dossier,
        ouvert_le=payload.ouvert_le,
        audience_le=payload.audience_le,
        decision_le=payload.decision_le,
        notes=payload.notes,
        user_email=getattr(user, "email", None),
    )
    await db.commit()
    await db.refresh(d)
    log.info(
        "Dossier TAL #%s ouvert sur bail #%s par %s", d.id, bail_id, user.email
    )
    return (await _enrichir(db, [d]))[0]


@router.get("/tal-dossiers", response_model=List[TalDossierRead])
async def list_tal_dossiers(
    db: DBSession,
    user: CurrentUser,
    statut: Optional[str] = None,
    immeuble_id: Optional[int] = None,
    locataire_id: Optional[int] = None,
    logement_id: Optional[int] = None,
) -> List[TalDossierRead]:
    """Liste filtrable. ``statut=en_cours`` = tout sauf « ferme »
    (pastilles des fiches logement / immeuble)."""
    _require_volet(user)
    q = select(ImmTalDossier)
    if statut:
        st = statut.strip().lower()
        if st == "en_cours":
            q = q.where(ImmTalDossier.statut != TAL_STATUT_FERME)
        elif st in TAL_STATUTS:
            q = q.where(ImmTalDossier.statut == st)
        else:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Statut inconnu — attendu : en_cours, "
                    + ", ".join(TAL_STATUTS)
                    + "."
                ),
            )
    if immeuble_id is not None:
        q = q.where(ImmTalDossier.immeuble_id == immeuble_id)
    if locataire_id is not None:
        q = q.where(ImmTalDossier.locataire_id == locataire_id)
    if logement_id is not None:
        q = q.where(ImmTalDossier.logement_id == logement_id)
    rows = (
        await db.execute(q.order_by(ImmTalDossier.id.desc()).limit(500))
    ).scalars().all()
    return await _enrichir(db, list(rows))


@router.get("/tal-dossiers/{dossier_id}", response_model=TalDossierDetail)
async def get_tal_dossier(
    dossier_id: int, db: DBSession, user: CurrentUser
) -> TalDossierDetail:
    _require_volet(user)
    d = await db.get(ImmTalDossier, dossier_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Dossier TAL introuvable.")
    base = (await _enrichir(db, [d]))[0]
    docs = (
        await db.execute(
            select(ImmDocument)
            .where(ImmDocument.tal_dossier_id == d.id)
            .order_by(ImmDocument.created_at.desc(), ImmDocument.id.desc())
        )
    ).scalars().all()
    detail = TalDossierDetail(**base.model_dump())
    detail.documents = [_doc_read(x) for x in docs]
    return detail


@router.patch("/tal-dossiers/{dossier_id}", response_model=TalDossierRead)
async def update_tal_dossier(
    dossier_id: int,
    payload: TalDossierUpdate,
    db: DBSession,
    user: CurrentUser,
) -> TalDossierRead:
    """Édition en place (statut, motif, numéro, dates, notes). Fermer le
    dossier (statut « ferme ») efface la date miroir du bail s'il ne
    reste aucun dossier en cours."""
    _require_volet(user)
    d = await db.get(ImmTalDossier, dossier_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Dossier TAL introuvable.")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k == "numero_dossier" and v is not None:
            v = v.strip()[:64] or None
        setattr(d, k, v)
    d.updated_at = _now()
    bail = await db.get(Bail, d.bail_id)
    if bail is not None:
        await refleter_dossiers_sur_bail(db, bail)
        bail.updated_at = _now()
    await db.commit()
    await db.refresh(d)
    return (await _enrichir(db, [d]))[0]


# ─── Garants & contacts ─────────────────────────────────────────────────


@router.get(
    "/locataires/{locataire_id}/contacts",
    response_model=List[LocataireContactRead],
)
async def list_locataire_contacts(
    locataire_id: int,
    db: DBSession,
    user: CurrentUser,
    inclure_inactifs: bool = False,
) -> List[LocataireContactRead]:
    _require_volet(user)
    if await db.get(Locataire, locataire_id) is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    q = select(ImmLocataireContact).where(
        ImmLocataireContact.locataire_id == locataire_id
    )
    if not inclure_inactifs:
        q = q.where(ImmLocataireContact.actif.is_(True))
    rows = (
        await db.execute(q.order_by(ImmLocataireContact.id.asc()))
    ).scalars().all()
    return [LocataireContactRead.model_validate(c) for c in rows]


@router.post(
    "/locataires/{locataire_id}/contacts",
    response_model=LocataireContactRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_locataire_contact(
    locataire_id: int,
    payload: LocataireContactCreate,
    db: DBSession,
    user: CurrentUser,
) -> LocataireContactRead:
    _require_volet(user)
    if await db.get(Locataire, locataire_id) is None:
        raise HTTPException(status_code=404, detail="Locataire introuvable.")
    data = payload.model_dump()
    data["full_name"] = data["full_name"].strip()
    c = ImmLocataireContact(
        locataire_id=locataire_id,
        created_by_email=getattr(user, "email", None),
        **data,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return LocataireContactRead.model_validate(c)


@router.patch(
    "/locataire-contacts/{contact_id}", response_model=LocataireContactRead
)
async def update_locataire_contact(
    contact_id: int,
    payload: LocataireContactUpdate,
    db: DBSession,
    user: CurrentUser,
) -> LocataireContactRead:
    _require_volet(user)
    c = await db.get(ImmLocataireContact, contact_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contact introuvable.")
    data = payload.model_dump(exclude_unset=True)
    if data.get("full_name") is not None:
        data["full_name"] = data["full_name"].strip()
    for k, v in data.items():
        setattr(c, k, v)
    c.updated_at = _now()
    await db.commit()
    await db.refresh(c)
    return LocataireContactRead.model_validate(c)


@router.delete(
    "/locataire-contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_locataire_contact(
    contact_id: int, db: DBSession, user: CurrentUser
) -> None:
    """Retire un contact (suppression franche : pas de fiche, pas
    d'historique à préserver)."""
    _require_volet(user)
    c = await db.get(ImmLocataireContact, contact_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contact introuvable.")
    await db.delete(c)
    await db.commit()
