"""Dossiers TAL + garants/contacts d'un locataire (retour Phil 2026-09-09).

Deux petits chantiers du volet locatif, VOLONTAIREMENT simples :

- **Dossier TAL** (point 5) : la date ``Bail.tal_dossier_ouvert_le``
  devient un MIROIR de la table ``imm_tal_dossiers``. Poser la date par
  le PATCH bail crée un dossier (motif non-paiement) s'il n'y en a pas
  d'en cours ; l'effacer ferme les dossiers en cours. Inversement,
  créer / fermer un dossier recalcule la date. Tout passe par ici pour
  que les deux sens restent cohérents.
- **Garants & contacts** (point 8) : chargement groupé des contacts
  (un seul SELECT, jamais de N+1) + normalisation des chaînes pour la
  recherche accents-insensible (« un virement de Jacques alors que le
  locataire est Sébastien : quand je cherche Jacques, je vois
  Sébastien »).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select

from app.models.immobilier import (
    TAL_STATUT_FERME,
    Bail,
    ImmLocataireContact,
    ImmTalDossier,
    Logement,
)

#: Libellés français des rôles de contact (réutilisés dans ``match_via``).
ROLE_LABELS: dict[str, str] = {
    "garant": "garant",
    "colocataire": "colocataire",
    "occupant": "occupant",
    "urgence": "contact d'urgence",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Normalisation (recherche) ──────────────────────────────────────────


def normaliser(texte: Optional[str]) -> str:
    """Minuscules, sans accents, espaces réduits — pour comparer
    « Sébastien » et « sebastien » sans passer par pg_trgm (volumes
    < 1 000, la simplicité prime)."""
    if not texte:
        return ""
    sans = unicodedata.normalize("NFKD", str(texte))
    sans = "".join(c for c in sans if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sans).strip().lower()


def chiffres(texte: Optional[str]) -> str:
    """Téléphone réduit à ses chiffres (« (514) 555-1234 » → 5145551234)."""
    return re.sub(r"\D", "", texte or "")


# ─── Garants & contacts ─────────────────────────────────────────────────


async def contacts_par_locataire(
    db,
    locataire_ids: Iterable[int],
    *,
    actifs_seulement: bool = True,
) -> dict[int, list[ImmLocataireContact]]:
    """``{locataire_id: [contacts]}`` en UN seul SELECT pour tous les
    locataires demandés (page Paiements, liste Locataires)."""
    ids = list({int(i) for i in locataire_ids if i is not None})
    out: dict[int, list[ImmLocataireContact]] = {i: [] for i in ids}
    if not ids:
        return out
    q = select(ImmLocataireContact).where(
        ImmLocataireContact.locataire_id.in_(ids)
    )
    if actifs_seulement:
        q = q.where(ImmLocataireContact.actif.is_(True))
    q = q.order_by(ImmLocataireContact.id.asc())
    for c in (await db.execute(q)).scalars().all():
        out.setdefault(c.locataire_id, []).append(c)
    return out


def payeur_de(contacts: list[ImmLocataireContact]) -> Optional[str]:
    """Nom du contact ACTIF qui paie le loyer (premier trouvé), sinon None."""
    for c in contacts:
        if c.paie_le_loyer and c.actif:
            return c.full_name
    return None


def contact_qui_matche(
    contacts: list[ImmLocataireContact], needle: str, needle_chiffres: str
) -> Optional[str]:
    """Retourne « rôle : Nom » si un contact correspond à la recherche
    (nom, courriel, téléphone), sinon None."""
    for c in contacts:
        if not c.actif:
            continue
        if needle and (
            needle in normaliser(c.full_name) or needle in normaliser(c.email)
        ):
            return f"{ROLE_LABELS.get(c.role, c.role)} : {c.full_name}"
        if (
            needle_chiffres
            and len(needle_chiffres) >= 3
            and needle_chiffres in chiffres(c.phone)
        ):
            return f"{ROLE_LABELS.get(c.role, c.role)} : {c.full_name}"
    return None


# ─── Dossiers TAL ───────────────────────────────────────────────────────


def est_en_cours(d: ImmTalDossier) -> bool:
    """Tout statut sauf « ferme » = dossier en cours (pastilles, miroir)."""
    return (d.statut or "ouvert") != TAL_STATUT_FERME


async def dossiers_du_bail(db, bail_id: int) -> list[ImmTalDossier]:
    return list(
        (
            await db.execute(
                select(ImmTalDossier)
                .where(ImmTalDossier.bail_id == bail_id)
                .order_by(ImmTalDossier.id.desc())
            )
        ).scalars().all()
    )


async def refleter_dossiers_sur_bail(db, bail: Bail) -> None:
    """Recalcule le MIROIR ``bail.tal_dossier_ouvert_le`` : date
    d'ouverture du dossier en cours le plus récent, sinon NULL."""
    en_cours = [d for d in await dossiers_du_bail(db, bail.id) if est_en_cours(d)]
    if not en_cours:
        bail.tal_dossier_ouvert_le = None
        return
    # Le plus récent (id décroissant) ; à défaut de date, aujourd'hui.
    bail.tal_dossier_ouvert_le = en_cours[0].ouvert_le or date.today()


async def ouvrir_dossier(
    db,
    bail: Bail,
    *,
    motif: str = "non_paiement",
    statut: str = "ouvert",
    numero_dossier: Optional[str] = None,
    ouvert_le: Optional[date] = None,
    audience_le: Optional[date] = None,
    decision_le: Optional[date] = None,
    notes: Optional[str] = None,
    user_email: Optional[str] = None,
) -> ImmTalDossier:
    """Crée un dossier pour ce bail (dénormalise locataire / logement /
    immeuble) et met la date miroir du bail à jour. Ne commit pas."""
    logement = await db.get(Logement, bail.logement_id)
    d = ImmTalDossier(
        bail_id=bail.id,
        locataire_id=bail.locataire_id,
        logement_id=bail.logement_id,
        immeuble_id=logement.immeuble_id if logement is not None else None,
        motif=motif or "non_paiement",
        statut=statut or "ouvert",
        numero_dossier=(numero_dossier or "").strip()[:64] or None,
        ouvert_le=ouvert_le or date.today(),
        audience_le=audience_le,
        decision_le=decision_le,
        notes=notes,
        created_by_email=user_email,
    )
    db.add(d)
    await db.flush()
    await refleter_dossiers_sur_bail(db, bail)
    bail.updated_at = _now()
    return d


async def fermer_dossiers_en_cours(db, bail: Bail) -> int:
    """Passe à « ferme » tous les dossiers en cours du bail ; retourne le
    nombre fermé. Met la date miroir à NULL."""
    n = 0
    for d in await dossiers_du_bail(db, bail.id):
        if est_en_cours(d):
            d.statut = TAL_STATUT_FERME
            d.updated_at = _now()
            n += 1
    bail.tal_dossier_ouvert_le = None
    return n


async def appliquer_date_miroir(
    db, bail: Bail, valeur: Optional[date], user_email: Optional[str]
) -> None:
    """Rétro-compat du PATCH ``/baux/{id}`` (bouton TAL des pages
    Paiements / fiche immeuble) :

    - date posée → un dossier motif non-paiement, statut ouvert, est
      créé s'il n'y en a AUCUN en cours (sinon on garde l'existant) ;
    - ``null`` → les dossiers en cours passent à « ferme ».
    Dans les deux cas la date du bail reflète l'état des dossiers."""
    if valeur is None:
        await fermer_dossiers_en_cours(db, bail)
        return
    en_cours = [d for d in await dossiers_du_bail(db, bail.id) if est_en_cours(d)]
    if en_cours:
        bail.tal_dossier_ouvert_le = en_cours[0].ouvert_le or valeur
        return
    await ouvrir_dossier(
        db, bail, motif="non_paiement", statut="ouvert",
        ouvert_le=valeur, user_email=user_email,
    )
