"""Recalage quotidien du pôle locatif (retour Phil 2026-09-09).

Avant : ``reconduire_tacitement_baux_echus`` et
``recaler_tous_les_statuts_logements`` ne tournaient qu'à la consultation
de trois pages (et au démarrage du serveur) → un locataire parti le 1er
restait « occupant » tant que personne n'ouvrait ces pages. Ici, une
seule fonction, idempotente, appelée par le mega-cron ``/run/all-daily``
ET au démarrage. AUCUN courriel n'en sort.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.immobilier import Bail, BailStatus

log = logging.getLogger(__name__)


async def recalage_quotidien(db: AsyncSession) -> dict:
    from app.services.gestion_externe import (
        annuler_dossiers_externes,
        refermer_dossiers_reloues,
    )
    from app.services.locatif_depart import (
        recaler_tous_les_statuts_logements,
        reconduire_tacitement_baux_echus,
    )

    today = date.today()
    baux = (
        await db.execute(
            select(Bail).where(
                Bail.status == BailStatus.ACTIF.value,
                Bail.date_fin < today,
                Bail.au_mois.isnot(True),
            )
        )
    ).scalars().all()
    modifie = await reconduire_tacitement_baux_echus(db, baux)
    n_ext = await annuler_dossiers_externes(db)
    n_ref = await refermer_dossiers_reloues(db)
    n_statuts = await recaler_tous_les_statuts_logements(db)
    await db.commit()
    out = {
        "baux_echus_examines": len(baux),
        "baux_modifies": bool(modifie),
        "dossiers_externes_annules": n_ext,
        "dossiers_refermes": n_ref,
        "statuts_recales": int(n_statuts or 0),
    }
    log.info("Recalage locatif : %s", out)
    return out
