"""Global cross-entity search.

    GET /api/v1/search?q=sam&limit=8

Retourne une liste mixte de résultats pointant vers les différentes
pages internes — clients, prospects, soumissions, factures, projets,
bons, employés. Utilisé par la barre de recherche du topbar admin.

Implémentation volontairement simple : ILIKE sur les champs texte
pertinents de chaque table. Pas de full-text search ni de classement
par pertinence — on trie juste alphabétiquement et on cappe à `limit`
par catégorie. Si un jour le catalogue grossit, passer à pg_trgm ou
tsvector.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import or_, select, func

from app.api.deps import CurrentUser, DBSession
from app.models.bon_travail import BonTravail
from app.models.client import Client
from app.models.contact_request import ContactRequest
from app.models.employe import Employe
from app.models.facture import Facture
from app.models.immobilier import ImmLocataireContact, Locataire
from app.models.project import Project
from app.models.soumission import Soumission


router = APIRouter(prefix="/search", tags=["search"])


ResultKind = Literal[
    "client",
    "prospect",
    "soumission",
    "facture",
    "project",
    "bon",
    "employe",
    "locataire",
]


class SearchHit(BaseModel):
    kind: ResultKind
    id: int
    title: str
    subtitle: Optional[str] = None
    href: str


@router.get("", response_model=List[SearchHit])
async def global_search(
    db: DBSession,
    _: CurrentUser,
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(default=6, ge=1, le=20),
) -> List[SearchHit]:
    needle = f"%{q.strip()}%"
    hits: List[SearchHit] = []

    # Clients
    rows = (
        await db.execute(
            select(Client)
            .where(
                or_(
                    Client.name.ilike(needle),
                    Client.email.ilike(needle),
                    Client.phone.ilike(needle),
                    Client.address.ilike(needle),
                )
            )
            .order_by(Client.name.asc())
            .limit(limit)
        )
    ).scalars().all()
    for c in rows:
        hits.append(
            SearchHit(
                kind="client",
                id=c.id,
                title=c.name,
                subtitle=c.email or c.phone or c.address,
                href=f"/app/clients/{c.id}",
            )
        )

    # Prospects
    rows = (
        await db.execute(
            select(ContactRequest)
            .where(
                or_(
                    ContactRequest.name.ilike(needle),
                    ContactRequest.email.ilike(needle),
                    ContactRequest.phone.ilike(needle),
                    ContactRequest.address.ilike(needle),
                    ContactRequest.message.ilike(needle),
                )
            )
            .order_by(ContactRequest.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for p in rows:
        hits.append(
            SearchHit(
                kind="prospect",
                id=p.id,
                title=p.name,
                subtitle=p.email or p.address or p.phone,
                href=f"/app/crm/{p.id}",
            )
        )

    # Soumissions
    rows = (
        await db.execute(
            select(Soumission)
            .where(
                or_(
                    Soumission.reference.ilike(needle),
                    Soumission.title.ilike(needle),
                    Soumission.property_address.ilike(needle),
                )
            )
            .order_by(Soumission.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for s in rows:
        hits.append(
            SearchHit(
                kind="soumission",
                id=s.id,
                title=f"{s.reference} — {s.title}",
                subtitle=s.property_address,
                href=f"/app/soumissions/{s.id}",
            )
        )

    # Factures
    rows = (
        await db.execute(
            select(Facture)
            .where(Facture.reference.ilike(needle))
            .order_by(Facture.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for f in rows:
        hits.append(
            SearchHit(
                kind="facture",
                id=f.id,
                title=f.reference,
                subtitle=f"Total : {float(f.total_ttc or 0):.2f} $",
                href=f"/app/facturation/{f.id}",
            )
        )

    # Projets
    rows = (
        await db.execute(
            select(Project)
            .where(
                or_(
                    Project.name.ilike(needle),
                    Project.address.ilike(needle),
                    Project.description.ilike(needle),
                )
            )
            .order_by(Project.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for pr in rows:
        hits.append(
            SearchHit(
                kind="project",
                id=pr.id,
                title=pr.name,
                subtitle=pr.address or pr.status,
                href=f"/app/projets/{pr.id}",
            )
        )

    # Bons de travail
    rows = (
        await db.execute(
            select(BonTravail)
            .where(
                or_(
                    BonTravail.reference.ilike(needle),
                    BonTravail.title.ilike(needle),
                )
            )
            .order_by(BonTravail.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for b in rows:
        hits.append(
            SearchHit(
                kind="bon",
                id=b.id,
                title=f"{b.reference} — {b.title}",
                subtitle=None,
                href=f"/app/bons/{b.id}",
            )
        )

    # Employés
    rows = (
        await db.execute(
            select(Employe)
            .where(
                or_(
                    Employe.full_name.ilike(needle),
                    Employe.email.ilike(needle),
                    Employe.phone.ilike(needle),
                )
            )
            .order_by(Employe.full_name.asc())
            .limit(limit)
        )
    ).scalars().all()
    for e in rows:
        hits.append(
            SearchHit(
                kind="employe",
                id=e.id,
                title=e.full_name,
                subtitle=e.email or e.phone or e.role,
                href=f"/app/employes/{e.id}",
            )
        )

    # Locataires — par leur nom/courriel/téléphone ET par leurs garants /
    # contacts (2026-09-09 : « quand je cherche Jacques, je vois
    # Sébastien »). Le sous-titre dit par qui la fiche a été trouvée.
    loc_hits: dict[int, SearchHit] = {}
    rows = (
        await db.execute(
            select(Locataire)
            .where(
                or_(
                    Locataire.full_name.ilike(needle),
                    Locataire.email.ilike(needle),
                    Locataire.phone.ilike(needle),
                )
            )
            .order_by(Locataire.full_name.asc())
            .limit(limit)
        )
    ).scalars().all()
    for lo in rows:
        loc_hits[lo.id] = SearchHit(
            kind="locataire",
            id=lo.id,
            title=lo.full_name,
            subtitle=lo.email or lo.phone,
            href=f"/immobilier/locataires/{lo.id}",
        )
    if len(loc_hits) < limit:
        contacts = (
            await db.execute(
                select(ImmLocataireContact, Locataire)
                .join(
                    Locataire,
                    Locataire.id == ImmLocataireContact.locataire_id,
                )
                .where(
                    ImmLocataireContact.actif.is_(True),
                    or_(
                        ImmLocataireContact.full_name.ilike(needle),
                        ImmLocataireContact.email.ilike(needle),
                        ImmLocataireContact.phone.ilike(needle),
                    ),
                )
                .order_by(Locataire.full_name.asc())
                .limit(limit)
            )
        ).all()
        for c, lo in contacts:
            if lo.id in loc_hits or len(loc_hits) >= limit:
                continue
            loc_hits[lo.id] = SearchHit(
                kind="locataire",
                id=lo.id,
                title=lo.full_name,
                subtitle=f"{c.role} : {c.full_name}",
                href=f"/immobilier/locataires/{lo.id}",
            )
    hits.extend(loc_hits.values())

    # Silence unused-import warnings for helpers reserved for future use.
    _ = func
    return hits
