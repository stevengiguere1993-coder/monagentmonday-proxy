"""Gestion EXTERNE — ce que Kratos fait (et ne fait pas) pour un
immeuble dont la perception est déléguée à une compagnie de gestion.

Règle posée avec Phil le 2026-09-09 : pour un immeuble externe, Kratos
connaît des unités, un NOM de locataire facultatif, un loyer attendu et
une case « payé » par mois d'après le rapport du gestionnaire. Point.
Pas de kanban de relocation, pas de bail, pas de dépôt, pas de relance,
pas de TAL ; « Départ » = l'unité devient vacante et le nom s'efface.

Toutes les portes qui traitaient une unité externe comme une interne
passent par ici (c'est ce qui a produit « Occupé · libre le 30 juin »
sur le 3 Elgin : un dossier de relocation ouvert sur une unité externe).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.immobilier import (
    Bail,
    BailStatus,
    Immeuble,
    LocationDossier,
    Logement,
    LogementStatus,
)

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def immeuble_est_externe(db: AsyncSession, immeuble_id: Optional[int]) -> bool:
    if immeuble_id is None:
        return False
    imm = await db.get(Immeuble, immeuble_id)
    return bool(imm is not None and getattr(imm, "gestion_externe", False))


async def logement_est_externe(db: AsyncSession, logement_id: Optional[int]) -> bool:
    if logement_id is None:
        return False
    lg = await db.get(Logement, logement_id)
    if lg is None:
        return False
    return await immeuble_est_externe(db, lg.immeuble_id)


def erreur_externe(quoi: str) -> HTTPException:
    """409 explicite : dit quoi faire à la place."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"Immeuble en gestion externe : {quoi} Indique simplement le nom "
            "du locataire sur le logement et marque le mois payé d'après le "
            "rapport du gestionnaire."
        ),
    )


async def rendre_vacant_externe(db: AsyncSession, lg: Logement) -> None:
    """« Départ » en gestion externe : l'unité devient vacante, le nom
    s'efface, un bail résiduel (créé avant la règle) est terminé. Aucun
    dossier de relocation, aucun courriel."""
    today = date.today()
    lg.status = LogementStatus.VACANT.value
    lg.locataire_externe_nom = None
    lg.updated_at = _now()
    for b in (
        await db.execute(
            select(Bail).where(
                Bail.logement_id == lg.id,
                Bail.status.in_([BailStatus.ACTIF.value, BailStatus.PROPOSE.value]),
            )
        )
    ).scalars().all():
        b.status = BailStatus.TERMINE.value
        if b.date_fin is None or b.date_fin > today:
            b.date_fin = today
        b.updated_at = _now()
    log.info("Gestion externe : logement %s rendu vacant", lg.id)


async def annuler_dossiers_externes(db: AsyncSession) -> int:
    """Filet : un dossier de relocation ACTIF sur une unité d'immeuble
    externe n'a pas lieu d'être → annulé (note explicite)."""
    from app.services.locatif_depart import DOSSIER_STATUTS_REGLES

    rows = (
        await db.execute(
            select(LocationDossier, Logement, Immeuble)
            .join(Logement, Logement.id == LocationDossier.logement_id)
            .join(Immeuble, Immeuble.id == Logement.immeuble_id)
            .where(
                Immeuble.gestion_externe.is_(True),
                LocationDossier.statut.notin_(list(DOSSIER_STATUTS_REGLES)),
            )
        )
    ).all()
    n = 0
    for dossier, lg, _imm in rows:
        dossier.statut = "annule"
        note = (
            "Annulé automatiquement : immeuble en gestion externe (pas de "
            "relocation dans Kratos)."
        )
        dossier.notes = f"{dossier.notes}\n{note}" if dossier.notes else note
        dossier.updated_at = _now()
        n += 1
    if n:
        log.info("Gestion externe : %d dossier(s) de relocation annulé(s)", n)
    return n


async def refermer_dossiers_reloues(db: AsyncSession) -> int:
    """Filet : un dossier de relocation ACTIF dont le logement porte un
    bail ACTIF (autre que le bail sortant) couvrant aujourd'hui est en
    réalité RELOUÉ — le bail a été assigné directement sans refermer le
    dossier (cause du « libre le … » qui restait collé)."""
    from app.services.locatif_depart import DOSSIER_STATUTS_REGLES

    today = date.today()
    dossiers = (
        await db.execute(
            select(LocationDossier).where(
                LocationDossier.statut.notin_(list(DOSSIER_STATUTS_REGLES)),
            )
        )
    ).scalars().all()
    n = 0
    for d in dossiers:
        baux = (
            await db.execute(
                select(Bail).where(
                    Bail.logement_id == d.logement_id,
                    Bail.status == BailStatus.ACTIF.value,
                    Bail.date_debut <= today,
                )
            )
        ).scalars().all()
        courant = None
        for b in baux:
            if d.bail_id is not None and b.id == d.bail_id:
                continue  # le bail SORTANT ne reloue pas
            if b.au_mois or b.date_fin is None or b.date_fin >= today:
                courant = b
        if courant is None:
            continue
        d.statut = "reloue"
        if d.reloue_le is None:
            d.reloue_le = today
        if d.nouveau_bail_id is None:
            d.nouveau_bail_id = courant.id
        d.updated_at = _now()
        n += 1
    if n:
        log.info("Relocation : %d dossier(s) refermé(s) (bail actif en place)", n)
    return n
