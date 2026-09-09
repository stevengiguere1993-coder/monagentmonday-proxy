"""Service UNIQUE du cycle « départ d'un locataire » (vague 1, 2026-08-13).

Avant : 9 portes déclaraient un départ, chacune à sa façon — « Départ » /
« Non renouvelé » créaient un dossier complet mais ne fermaient jamais le
bail ; « Mettre fin au bail » fermait le bail mais laissait un dossier
vide et ne touchait pas au statut du logement ; la réponse publique
« je quitte » laissait le bail actif. Résultat : baux zombies, logements
« occupés » à vie, faux « réputé accepté » un mois plus tard.

Désormais TOUTES les portes qui connaissent le bail sortant passent par
``declarer_depart`` :
  1. ferme le bail (résilié si la date est passée, sinon la date de fin
     est posée et le recalage lazy le terminera à l'échéance) ;
  2. recale le statut du logement (occupe/reserve/vacant, jamais
     hors_location) ;
  3. crée OU COMPLÈTE le dossier de relocation (fill-only : on ne
     remplit que les champs vides d'un dossier existant, sauf
     ``date_depart`` qui est resynchronisée si la résiliation la
     change) ;
  4. passe le cycle de renouvellement courant à « depart » + reponse_le
     (tue le faux « réputé accepté »).

S'y ajoutent :
  - ``reconduire_tacitement_baux_echus`` : reconduction tacite AUTOMATIQUE
    (décision Phil 2026-08-13, aligne l'art. 1941 C.c.Q.) — lazy, à la
    consultation des vues, jamais de cron ni d'envoi au locataire ;
  - ``terminer_baux_echus_avant`` : garde-fou « deux baux actifs » — un
    bail actif ÉCHU est terminé automatiquement à l'arrivée du bail
    suivant (une seule ligne dans le suivi des loyers).

AUCUN envoi de courriel ici — règle absolue du pôle.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.immobilier import (
    Bail,
    BailRenouvellement,
    BailStatus,
    Immeuble,
    LocationDossier,
    LocationDossierStatut,
    Logement,
    LogementStatus,
)

log = logging.getLogger(__name__)

#: Statuts de dossier de relocation « réglés » — tout le reste est ACTIF
#: (même garde-fou que POST /immobilier/locations).
DOSSIER_STATUTS_REGLES = (
    LocationDossierStatut.ANNULE.value,
    LocationDossierStatut.RELOUE.value,
)

NOTE_RECONDUCTION_AUTO = (
    "Reconduction tacite automatique (aucune réponse à l'échéance)"
)
#: Préfixe des dossiers créés par un AUTOMATISME (détection d'unité
#: vacante, bail préparé depuis la page Baux). Tant qu'un humain ne les
#: a pas pris en charge, ils ne génèrent pas de frais de relocation
#: facturables (M9, audit 2026-08-13).
NOTE_AUTO_PREFIX = "Créé automatiquement"
NOTE_AUTO_UNITE_VACANTE = "Créé automatiquement — unité vacante."
#: Marqueur ajouté quand un HUMAIN fait avancer un dossier auto-créé
#: (kanban, conversion) — il redevient facturable.
NOTE_PRISE_EN_CHARGE = "Pris en charge manuellement"
NOTE_TERMINE_BAIL_SUIVANT = (
    "Terminé automatiquement à l'arrivée du bail suivant"
)
NOTE_TERMINE_DEPART_ANNONCE = (
    "Terminé automatiquement à l'échéance — départ annoncé "
    "(dossier de relocation actif)"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append_note(existant: Optional[str], ajout: str) -> str:
    """Ajoute une ligne aux notes sans écraser ni dupliquer."""
    if not existant:
        return ajout
    if ajout in existant:
        return existant
    return f"{existant}\n{ajout}"


async def dossier_relocation_actif(
    db: AsyncSession, logement_id: int
) -> Optional[LocationDossier]:
    """Dossier de relocation ACTIF du logement (garde-fou existant)."""
    return (
        await db.execute(
            select(LocationDossier).where(
                LocationDossier.logement_id == logement_id,
                LocationDossier.statut.notin_(list(DOSSIER_STATUTS_REGLES)),
            )
        )
    ).scalars().first()


def dossier_auto_sans_prise_en_charge(dossier: LocationDossier) -> bool:
    """Dossier créé par un automatisme et jamais pris en charge par un
    humain → exclu des frais de relocation facturables (M9b)."""
    notes = dossier.notes or ""
    return notes.startswith(NOTE_AUTO_PREFIX) and (
        NOTE_PRISE_EN_CHARGE not in notes
    )


def marquer_prise_en_charge_humaine(dossier: LocationDossier) -> None:
    """Un HUMAIN vient de faire avancer ce dossier (kanban, conversion) :
    si le dossier avait été créé automatiquement, le marqueur le rend à
    nouveau facturable (frais de relocation)."""
    if dossier_auto_sans_prise_en_charge(dossier):
        dossier.notes = _append_note(dossier.notes, NOTE_PRISE_EN_CHARGE)


async def ouvrir_dossiers_unites_vacantes(
    db: AsyncSession,
    logement_ids: Optional[Iterable[int]] = None,
    limite: int = 500,
) -> int:
    """Chaque logement VACANT sans dossier de relocation ACTIF obtient
    son dossier automatiquement (invariant v16 « toutes les unités
    libres sont au kanban »).

    Déplacé HORS du GET /locations/overview (M9a, audit 2026-08-13) :
    appelé par les MUTATIONS qui rendent un logement vacant et par le
    backfill de démarrage du backend (borné par ``limite``). Immeubles
    en gestion externe ou inactifs exclus. Ne committe pas — le geste
    appelant garde la main sur sa transaction.
    """
    await db.flush()  # les dossiers/logements en attente comptent
    q = (
        select(Logement)
        .join(Immeuble, Immeuble.id == Logement.immeuble_id)
        .where(
            Logement.status == LogementStatus.VACANT.value,
            Immeuble.gestion_externe.isnot(True),
            Immeuble.is_active.is_(True),
        )
    )
    ids = [i for i in (logement_ids or []) if i]
    if logement_ids is not None:
        if not ids:
            return 0
        q = q.where(Logement.id.in_(ids))
    vacants = (await db.execute(q)).scalars().all()
    if not vacants:
        return 0
    deja = {
        d.logement_id
        for d in (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.logement_id.in_(
                        [lg.id for lg in vacants]
                    ),
                    LocationDossier.statut.notin_(
                        list(DOSSIER_STATUTS_REGLES)
                    ),
                )
            )
        ).scalars().all()
    }
    crees = 0
    for lg in vacants:
        if lg.id in deja or crees >= limite:
            continue
        nd = LocationDossier(
            logement_id=lg.id,
            statut=LocationDossierStatut.AVIS_RECU.value,
            notes=NOTE_AUTO_UNITE_VACANTE,
        )
        nd.created_at = _now()
        nd.updated_at = _now()
        db.add(nd)
        crees += 1
    if crees:
        log.info("Dossiers de relocation auto-créés : %d", crees)
    return crees


async def recaler_statut_logement(
    db: AsyncSession, logement_id: int
) -> None:
    """Statut du logement recalculé d'après ses baux :

    - OCCUPE si un bail ACTIF couvre aujourd'hui (un bail AU MOIS court
      sans égard à sa date de fin) ;
    - RESERVE si un bail proposé/actif commence plus tard ;
    - VACANT sinon.

    Ne touche JAMAIS un logement HORS_LOC (rénovation, proprio-occupé…).
    Le filtrage se fait en mémoire pour voir les changements de statut
    non encore flushés (autoflush désactivé sur la session).
    """
    lg = await db.get(Logement, logement_id)
    if lg is None or lg.status == LogementStatus.HORS_LOC.value:
        return
    # ⚠️ GESTION EXTERNE : le statut est saisi À LA MAIN — les baux de
    # ces immeubles ne sont pas dans Kratos, donc la règle « pas de bail
    # actif → vacant » y est un contresens. L'oubli de cette garde le
    # 2026-08-20 a mis « vacant » les 19 logements de la Place Sapinière
    # (tous occupés, leurs paiements d'août le prouvaient) et la moitié
    # d'Elgin. Jamais plus.
    imm = await db.get(Immeuble, lg.immeuble_id)
    if imm is not None and bool(getattr(imm, "gestion_externe", False)):
        return
    today = date.today()
    baux = (
        await db.execute(
            select(Bail).where(Bail.logement_id == logement_id)
        )
    ).scalars().all()
    occupe = any(
        b.status == BailStatus.ACTIF.value
        and b.date_debut is not None
        and b.date_debut <= today
        and (b.au_mois or (b.date_fin is not None and b.date_fin >= today))
        for b in baux
    )
    reserve = any(
        b.status in (BailStatus.ACTIF.value, BailStatus.PROPOSE.value)
        and b.date_debut is not None
        and b.date_debut > today
        for b in baux
    )
    if occupe:
        nouveau = LogementStatus.OCCUPE.value
        # Le « loyer demandé » suit le bail tant que c'est loué (retour
        # client 2026-08-14) — voir refleter_bail_sur_demande.
        courant = next(
            (
                b for b in baux
                if b.status == BailStatus.ACTIF.value
                and b.date_debut is not None
                and b.date_debut <= today
                and (
                    b.au_mois
                    or (b.date_fin is not None and b.date_fin >= today)
                )
            ),
            None,
        )
        if courant is not None and courant.loyer_mensuel is not None:
            from app.services.loyer_effectif import (
                refleter_bail_sur_demande,
            )

            refleter_bail_sur_demande(lg, float(courant.loyer_mensuel))
    elif reserve:
        nouveau = LogementStatus.RESERVE.value
    else:
        nouveau = LogementStatus.VACANT.value
    if lg.status != nouveau:
        lg.status = nouveau
        lg.updated_at = _now()


async def _basculer_cycle_en_depart(db: AsyncSession, bail: Bail) -> None:
    """Cycle de renouvellement COURANT du bail → « depart » + reponse_le
    (le locataire a annoncé son départ : plus jamais de « réputé
    accepté » sur ce cycle)."""
    from app.services.bail_renouvellement import est_cycle_courant

    today = date.today()
    rows = (
        await db.execute(
            select(BailRenouvellement)
            .where(BailRenouvellement.bail_id == bail.id)
            .order_by(BailRenouvellement.avis_envoye_le.desc())
        )
    ).scalars().all()
    for r in rows:
        if not est_cycle_courant(r, bail.date_fin, today):
            continue
        if r.status != "depart":
            r.status = "depart"
            if r.reponse_le is None:
                r.reponse_le = today
            r.updated_at = _now()
        return


async def declarer_depart(
    db: AsyncSession,
    bail_id: int,
    *,
    date_depart: Optional[date] = None,
    loyer_demande: Optional[float] = None,
    source: str,
    ouvrir_dossier: bool = True,
) -> Optional[LocationDossier]:
    """Point d'entrée UNIQUE d'un départ de locataire (bail connu).

    ``date_depart`` : défaut = la fin du bail (le locataire part à
    l'échéance). Passée ou aujourd'hui → le bail est résilié tout de
    suite ; future → le bail reste actif jusqu'à cette date (posée en
    ``date_fin``), le recalage lazy le terminera à l'échéance.

    ``source`` : consigné dans les notes du dossier (traçabilité —
    ex. « Résiliation immédiate », « Réponse signée du locataire »).

    Retourne le dossier de relocation (créé ou complété), ou None si le
    bail est introuvable / ``ouvrir_dossier=False``.
    """
    bail = await db.get(Bail, bail_id)
    if bail is None:
        return None
    # Gestion EXTERNE (2026-09-09) : un départ = l'unité redevient
    # vacante, rien d'autre (pas de dossier de relocation).
    if bail.logement_id:
        from app.services.gestion_externe import (
            logement_est_externe,
            rendre_vacant_externe,
        )

        if await logement_est_externe(db, bail.logement_id):
            lg_ext = await db.get(Logement, bail.logement_id)
            if lg_ext is not None:
                await rendre_vacant_externe(db, lg_ext)
            return None
    today = date.today()
    if date_depart is None:
        date_depart = bail.date_fin

    # 1) Fermer le bail (seul un bail ACTIF se ferme — on ne ressuscite
    #    ni ne re-date un bail déjà terminé/résilié).
    if bail.status == BailStatus.ACTIF.value and date_depart is not None:
        if bail.date_fin != date_depart:
            bail.date_fin = date_depart
        if date_depart <= today:
            bail.status = BailStatus.RESILIE.value
        bail.updated_at = _now()

    # 2) Cycle de renouvellement courant → « depart ».
    await _basculer_cycle_en_depart(db, bail)

    # 3) Dossier de relocation : créer OU compléter (fill-only).
    dossier: Optional[LocationDossier] = None
    if ouvrir_dossier and bail.logement_id:
        loyer_ancien = (
            float(bail.loyer_mensuel)
            if bail.loyer_mensuel is not None
            else None
        )
        loyer_dem = (
            float(loyer_demande) if loyer_demande is not None else loyer_ancien
        )
        dossier = await dossier_relocation_actif(db, bail.logement_id)
        if dossier is None:
            dossier = LocationDossier(
                logement_id=bail.logement_id,
                bail_id=bail.id,
                statut=LocationDossierStatut.AVIS_RECU.value,
                date_depart=date_depart,
                loyer_ancien=loyer_ancien,
                loyer_demande=loyer_dem,
                notes=source or None,
            )
            dossier.created_at = _now()
            dossier.updated_at = _now()
            db.add(dossier)
        else:
            # Fill-only : ne remplir QUE les champs vides — sauf
            # date_depart, resynchronisée si la résiliation la change
            # (trou M5 : dossier ouvert « à l'échéance », puis entente
            # de départ anticipé → la date du dossier doit suivre).
            if dossier.bail_id is None:
                dossier.bail_id = bail.id
            if dossier.loyer_ancien is None:
                dossier.loyer_ancien = loyer_ancien
            if dossier.loyer_demande is None:
                dossier.loyer_demande = loyer_dem
            if date_depart is not None and dossier.date_depart != date_depart:
                dossier.date_depart = date_depart
            if source:
                dossier.notes = _append_note(dossier.notes, source)
            dossier.updated_at = _now()

    # 4) Recaler le logement + miroir « loyer demandé » (2026-08-13) :
    #    logement VACANT → le prix affiché pour la relocation (porté par
    #    le dossier) fait foi sur la fiche.
    if bail.logement_id:
        await recaler_statut_logement(db, bail.logement_id)
        if dossier is not None and dossier.loyer_demande is not None:
            lg = await db.get(Logement, bail.logement_id)
            if (
                lg is not None
                and lg.status == LogementStatus.VACANT.value
            ):
                lg.loyer_demande = dossier.loyer_demande

    log.info(
        "Départ déclaré (bail %s, date %s, source « %s ») — bail %s",
        bail_id, date_depart, source, bail.status,
    )
    return dossier


def _fin_reconduite_apres(
    date_debut: Optional[date], date_fin: date, today: date
) -> date:
    """Fin de la période reconduite, répétée jusqu'à couvrir aujourd'hui
    (bail échu depuis plusieurs cycles → UNE ligne de rattrapage).
    Art. 1941 C.c.Q. : 12 mois et plus → reconduit 12 mois ; plus court
    → même durée."""
    duree = (date_fin - date_debut).days if date_debut else 365

    def _suivante(fin: date) -> date:
        if duree >= 360:
            try:
                return fin.replace(year=fin.year + 1)
            except ValueError:  # 29 février
                return fin.replace(year=fin.year + 1, day=28)
        return fin + timedelta(days=max(duree, 27) + 1)

    nouvelle = _suivante(date_fin)
    for _ in range(200):  # garde anti-boucle (données dégénérées)
        if nouvelle >= today:
            break
        nouvelle = _suivante(nouvelle)
    return nouvelle


async def reconduire_tacitement_baux_echus(
    db: AsyncSession, baux: Iterable[Bail]
) -> bool:
    """Reconduction tacite AUTOMATIQUE des baux échus — LAZY, déclenchée
    à la consultation (loyers, page Baux, renouvellements). Aucun cron,
    aucun envoi.

    Un bail ACTIF dont la fin est passée :
    - avec un dossier de relocation ACTIF sur son logement → le départ
      était annoncé : le bail se TERMINE à sa date de fin, logement
      recalé ;
    - sans réponse « depart »/« refuse » et sans cycle de renouvellement
      en cours → reconduit tel quel (cycle « reconduit », date de fin
      étirée — même logique que le bouton « Reconduire tel quel ») ;
    - avec un cycle en cours (avis envoyé, négociation, refus…) → on ne
      touche à rien, la machine à états des renouvellements décide.

    Exclusions : baux AU MOIS (reconduits par nature), logements « louer
    indéfiniment (chambres) », immeubles en gestion externe.

    Retourne True (et commit) si quelque chose a changé.
    """
    from app.services.bail_renouvellement import est_cycle_courant

    today = date.today()
    candidats = [
        b
        for b in baux
        if b.status == BailStatus.ACTIF.value
        and b.date_fin is not None
        and b.date_fin < today
        and not b.au_mois
    ]
    if not candidats:
        return False

    log_ids = {b.logement_id for b in candidats if b.logement_id}
    log_by_id = {
        lg.id: lg
        for lg in (
            await db.execute(
                select(Logement).where(Logement.id.in_(list(log_ids)))
            )
        ).scalars().all()
    } if log_ids else {}
    imm_ids = {lg.immeuble_id for lg in log_by_id.values() if lg.immeuble_id}
    imm_by_id = {
        im.id: im
        for im in (
            await db.execute(
                select(Immeuble).where(Immeuble.id.in_(list(imm_ids)))
            )
        ).scalars().all()
    } if imm_ids else {}

    # Dossier de relocation ACTIF par logement.
    dossier_by_log: dict[int, LocationDossier] = {}
    if log_ids:
        for d in (
            await db.execute(
                select(LocationDossier).where(
                    LocationDossier.logement_id.in_(list(log_ids)),
                    LocationDossier.statut.notin_(
                        list(DOSSIER_STATUTS_REGLES)
                    ),
                )
            )
        ).scalars().all():
            dossier_by_log[d.logement_id] = d

    # Dernier renouvellement par bail (même sémantique que les vues).
    last_ren_by_bail: dict[int, BailRenouvellement] = {}
    bail_ids = [b.id for b in candidats]
    for r in (
        await db.execute(
            select(BailRenouvellement).where(
                BailRenouvellement.bail_id.in_(bail_ids)
            )
        )
    ).scalars().all():
        cur = last_ren_by_bail.get(r.bail_id)
        if cur is None or (r.avis_envoye_le, r.id) > (
            cur.avis_envoye_le,
            cur.id,
        ):
            last_ren_by_bail[r.bail_id] = r

    dirty = False
    for b in candidats:
        lg = log_by_id.get(b.logement_id)
        if lg is None or getattr(lg, "location_en_chambres", False):
            continue  # « louer indéfiniment » — jamais de cycle
        im = imm_by_id.get(lg.immeuble_id)
        if im is not None and getattr(im, "gestion_externe", None):
            continue  # gestion externe — le tiers décide

        dossier = dossier_by_log.get(b.logement_id)
        if dossier is not None:
            # Le départ était annoncé : le bail se termine à sa fin.
            b.status = BailStatus.TERMINE.value
            b.notes = _append_note(b.notes, NOTE_TERMINE_DEPART_ANNONCE)
            b.updated_at = _now()
            await recaler_statut_logement(db, b.logement_id)
            if (
                dossier.loyer_demande is not None
                and lg.status == LogementStatus.VACANT.value
            ):
                lg.loyer_demande = dossier.loyer_demande
            dirty = True
            log.info(
                "Bail %s terminé à l'échéance (départ annoncé, "
                "dossier %s)", b.id, dossier.id,
            )
            continue

        last_ren = last_ren_by_bail.get(b.id)
        if last_ren is not None and est_cycle_courant(
            last_ren, b.date_fin, today
        ):
            if last_ren.status == "depart":
                # Départ ANNONCÉ (réponse « depart ») et fin passée,
                # mais dossier de relocation déjà fermé (unité relouée,
                # par ex.) : sans cette branche le bail restait ACTIF à
                # jamais — ligne zombie dans le suivi des loyers
                # (retour client 2026-08-14). Même sort que le départ
                # avec dossier actif : le bail se termine à sa fin.
                b.status = BailStatus.TERMINE.value
                b.notes = _append_note(b.notes, NOTE_TERMINE_DEPART_ANNONCE)
                b.updated_at = _now()
                await recaler_statut_logement(db, b.logement_id)
                dirty = True
                log.info(
                    "Bail %s terminé à l'échéance (départ annoncé via "
                    "le cycle de renouvellement)", b.id,
                )
                continue
            # Avis en cours, refus, négociation… : la machine à états
            # des renouvellements garde la main — pas de reconduction.
            continue

        ancienne = b.date_fin
        nouvelle = _fin_reconduite_apres(b.date_debut, ancienne, today)
        b.date_fin = nouvelle
        b.updated_at = _now()
        ren = BailRenouvellement(
            bail_id=b.id,
            avis_envoye_le=today,
            nouveau_loyer=b.loyer_mensuel,
            nouvelle_date_debut=ancienne + timedelta(days=1),
            nouvelle_date_fin=nouvelle,
            status="reconduit",
            notes=NOTE_RECONDUCTION_AUTO,
        )
        ren.created_at = _now()
        ren.updated_at = _now()
        db.add(ren)
        dirty = True
        log.info(
            "Bail %s reconduit tacitement (auto) : %s → %s",
            b.id, ancienne, nouvelle,
        )

    if dirty:
        await db.commit()
    return dirty


#: Note apposée par la réactivation ci-dessous — sert aussi de marqueur
#: d'idempotence (un bail déjà réactivé ne rematche plus le WHERE).
NOTE_REACTIVE_IMPORT = (
    "Réactivé automatiquement (2026-08-17) — terminé par erreur à "
    "l'import, locataire en place."
)


async def reactiver_baux_termines_a_tort(db: AsyncSession) -> int:
    """Backfill 2026-08-17 : l'import des données réelles du 12 août a
    marqué « termine » des baux placeholder PlexFlow encore VIVANTS
    (locataire en place, loyers marqués payés chaque mois, aucun bail
    successeur) — le suivi des loyers affichait « Bail terminé le
    2027-06-01 » sur des locataires en place (retour Phil 2026-08-17).

    Réactive un bail « termine » qui cumule TOUS ces signes de vie :
    date de fin FUTURE, note d'import PlexFlow, aucun autre bail actif
    sur le logement, et un loyer marqué payé ce mois-ci ou le mois
    dernier. Idempotent : une fois « actif », il ne matche plus.
    Retourne le nombre de baux réactivés (commit à la charge de
    l'appelant).
    """
    from app.models.immobilier import PaiementLoyer

    today = date.today()
    seuil = today.replace(day=1) - timedelta(days=1)
    seuil = seuil.replace(day=1)  # 1er du mois précédent
    candidats = (
        await db.execute(
            select(Bail).where(
                Bail.status == BailStatus.TERMINE.value,
                Bail.date_fin.is_not(None),
                Bail.date_fin > today,
                Bail.notes.like("%PlexFlow%"),
            )
        )
    ).scalars().all()
    n = 0
    for b in candidats:
        # Un dossier de RELOCATION actif = le départ est ACTÉ (avis
        # reçu, annonce, visites…) et le logement se vide : on ne
        # ressuscite pas le bail, même s'il reste un dernier loyer
        # encaissé (retour Phil 2026-08-17 : « j'ai des unités
        # vacantes, mais encore présentes dans les baux »).
        if await dossier_relocation_actif(db, b.logement_id) is not None:
            continue
        successeur = (
            await db.execute(
                select(Bail.id)
                .where(
                    Bail.logement_id == b.logement_id,
                    Bail.id != b.id,
                    Bail.status == BailStatus.ACTIF.value,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if successeur is not None:
            continue  # le locataire est vraiment parti, remplacé
        paiement_recent = (
            await db.execute(
                select(PaiementLoyer.id)
                .where(
                    PaiementLoyer.bail_id == b.id,
                    PaiementLoyer.mois_couvert >= seuil,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if paiement_recent is None:
            continue  # plus d'encaissement — rien ne prouve la vie
        b.status = BailStatus.ACTIF.value
        b.notes = _append_note(b.notes, NOTE_REACTIVE_IMPORT)
        b.updated_at = _now()
        await recaler_statut_logement(db, b.logement_id)
        n += 1
        log.info(
            "Bail %s réactivé (terminé par erreur à l'import, "
            "locataire en place)", b.id,
        )
    return n


#: Note du correctif ci-dessous (garde la trace de l'aller-retour).
NOTE_REACTIVATION_ANNULEE = (
    "Réactivation annulée (2026-08-17) — le logement est en cours de "
    "relocation : le locataire était bien parti."
)


async def annuler_reactivations_erronees(db: AsyncSession) -> int:
    """Correctif 2026-08-17 : le backfill de réactivation a ressuscité
    des baux dont le locataire était en réalité PARTI — leur dernier
    loyer encaissé avait été pris pour une preuve de vie. Effet de
    bord : le logement repassait « occupé » alors qu'il est vacant
    (retour Phil : « j'ai des unités vacantes, mais encore présentes
    dans les baux »).

    Un bail ACTIF portant la note de réactivation, dont le logement a
    un dossier de relocation ACTIF, est re-terminé : sa date de fin est
    ramenée à la fin du dernier mois encaissé (sinon à la veille de
    l'ouverture du dossier) — sans quoi il porterait un loyer fantôme —
    et le statut du logement est recalculé (→ vacant).

    Idempotent : une fois re-terminé, le bail n'est plus ACTIF donc il
    ne rematche pas. La note de réactivation reste en place : elle
    documente l'aller-retour.
    """
    from app.models.immobilier import PaiementLoyer

    candidats = (
        await db.execute(
            select(Bail).where(
                Bail.status == BailStatus.ACTIF.value,
                Bail.notes.like("%Réactivé automatiquement%"),
            )
        )
    ).scalars().all()
    n = 0
    for b in candidats:
        dossier = await dossier_relocation_actif(db, b.logement_id)
        if dossier is None:
            continue  # réactivation légitime : le locataire est là
        dernier_mois = (
            await db.execute(
                select(func.max(PaiementLoyer.mois_couvert)).where(
                    PaiementLoyer.bail_id == b.id
                )
            )
        ).scalar_one_or_none()
        if dernier_mois is not None:
            # Dernier jour du mois encaissé.
            fin = (dernier_mois + timedelta(days=32)).replace(
                day=1
            ) - timedelta(days=1)
        else:
            ouvert_le = getattr(dossier, "created_at", None)
            base = ouvert_le.date() if ouvert_le else date.today()
            fin = base - timedelta(days=1)
        b.status = BailStatus.TERMINE.value
        if b.date_fin is None or fin < b.date_fin:
            b.date_fin = fin
        b.notes = _append_note(b.notes, NOTE_REACTIVATION_ANNULEE)
        b.updated_at = _now()
        await recaler_statut_logement(db, b.logement_id)
        n += 1
        log.info(
            "Bail %s : réactivation ANNULÉE (dossier de relocation %s "
            "actif) — fin ramenée au %s, logement recalé",
            b.id, dossier.id, fin,
        )
    return n


#: Notes apposées par le recalage ci-dessous (aussi marqueurs de trace).
NOTE_FIN_RECALEE = (
    "Date de fin recalée automatiquement (2026-08-17) sur l'arrivée du "
    "locataire suivant — la date d'origine venait de l'import."
)
NOTE_PAIEMENT_REDATE = (
    "Mois corrigé automatiquement (2026-08-17) : dernier mois réel du "
    "bail (la date d'origine venait de l'import)."
)


async def recaler_fins_baux_placeholder(db: AsyncSession) -> int:
    """Backfill 2026-08-17 (décision Phil) : les baux placeholder de
    l'import PlexFlow gardaient leur date de fin par défaut (2027-06-01)
    même une fois TERMINÉS et remplacés — Kratos leur comptait donc un
    loyer chaque mois jusqu'en juin 2027 (soldes fantômes de 315 $ /
    371 $, deux lignes pour le même logement).

    Pour un bail terminé/résilié à date de fin FUTURE, portant la note
    d'import PlexFlow, dont le logement a un bail ACTIF **déjà
    commencé** : la fin est ramenée à la veille de l'arrivée du
    successeur. Un paiement resté sur un mois que le bail ne couvre
    plus est ré-imputé au DERNIER mois réel — seulement si ce mois n'a
    pas déjà son paiement (sinon on n'invente rien : la ligne est
    laissée telle quelle et journalisée, à arbitrer par un humain).

    ⚠ La note PlexFlow ET le successeur déjà commencé sont des
    garde-fous : une résiliation LÉGITIME avec date de fin future
    (départ annoncé pour plus tard) ne doit jamais être touchée.

    Idempotent : après recalage la fin est passée, le WHERE ne matche
    plus. Retourne le nombre de baux recalés (commit à l'appelant).
    """
    from app.models.immobilier import PaiementLoyer

    today = date.today()
    candidats = (
        await db.execute(
            select(Bail).where(
                Bail.status.in_(
                    [
                        BailStatus.TERMINE.value,
                        BailStatus.RESILIE.value,
                    ]
                ),
                Bail.date_fin.is_not(None),
                Bail.date_fin > today,
                Bail.notes.like("%PlexFlow%"),
            )
        )
    ).scalars().all()
    n = 0
    for b in candidats:
        debut_successeur = (
            await db.execute(
                select(func.min(Bail.date_debut)).where(
                    Bail.logement_id == b.logement_id,
                    Bail.id != b.id,
                    Bail.status == BailStatus.ACTIF.value,
                    Bail.date_debut.is_not(None),
                    Bail.date_debut > b.date_debut,
                    Bail.date_debut <= today,  # déjà en place
                )
            )
        ).scalar_one_or_none()
        if debut_successeur is None:
            continue
        nouvelle_fin = debut_successeur - timedelta(days=1)
        if nouvelle_fin <= b.date_debut or nouvelle_fin >= b.date_fin:
            continue
        b.date_fin = nouvelle_fin
        b.notes = _append_note(b.notes, NOTE_FIN_RECALEE)
        b.updated_at = _now()
        n += 1
        log.info(
            "Bail %s : fin recalée %s (arrivée du successeur le %s)",
            b.id, nouvelle_fin, debut_successeur,
        )

        # Paiements restés sur un mois que le bail ne couvre plus.
        dernier_mois = nouvelle_fin.replace(day=1)
        orphelins = (
            await db.execute(
                select(PaiementLoyer).where(
                    PaiementLoyer.bail_id == b.id,
                    PaiementLoyer.mois_couvert > dernier_mois,
                )
            )
        ).scalars().all()
        for p in orphelins:
            deja = (
                await db.execute(
                    select(func.count())
                    .select_from(PaiementLoyer)
                    .where(
                        PaiementLoyer.bail_id == b.id,
                        PaiementLoyer.mois_couvert == dernier_mois,
                    )
                )
            ).scalar_one()
            if deja:
                # Le dernier mois a déjà son paiement : on n'invente
                # rien (trop-payé à arbitrer par un humain).
                log.info(
                    "Bail %s : paiement %s (mois %s) LAISSÉ tel quel — "
                    "le mois %s est déjà payé",
                    b.id, p.id, p.mois_couvert, dernier_mois,
                )
                continue
            log.info(
                "Bail %s : paiement %s ré-imputé %s → %s",
                b.id, p.id, p.mois_couvert, dernier_mois,
            )
            p.mois_couvert = dernier_mois
            p.notes = _append_note(p.notes, NOTE_PAIEMENT_REDATE)
    return n


async def terminer_baux_echus_avant(
    db: AsyncSession,
    logement_id: int,
    date_debut: Optional[date],
    exclure_bail_id: Optional[int] = None,
) -> int:
    """Garde-fou « deux baux actifs » (C4) : à l'arrivée d'un bail actif
    sur un logement, tout bail ACTIF déjà ÉCHU (sa fin précède le début
    du nouveau) passe « termine » automatiquement — sinon les deux
    coexistent et le suivi des loyers double la ligne.

    Un bail actif NON échu qui chevauche reste bloqué par le 409
    existant (ce n'est pas le rôle de ce helper). Baux AU MOIS et
    logements « louer indéfiniment » exclus (ils courent sans égard à
    leur date de fin).
    """
    if date_debut is None:
        return 0
    lg = await db.get(Logement, logement_id)
    if lg is not None and getattr(lg, "location_en_chambres", False):
        return 0
    q = select(Bail).where(
        Bail.logement_id == logement_id,
        Bail.status == BailStatus.ACTIF.value,
        Bail.date_fin < date_debut,
    )
    if exclure_bail_id is not None:
        q = q.where(Bail.id != exclure_bail_id)
    n = 0
    for b in (await db.execute(q)).scalars().all():
        if b.au_mois:
            continue
        b.status = BailStatus.TERMINE.value
        b.notes = _append_note(b.notes, NOTE_TERMINE_BAIL_SUIVANT)
        b.updated_at = _now()
        n += 1
        log.info(
            "Bail %s (échu le %s) terminé automatiquement — bail "
            "suivant au %s", b.id, b.date_fin, date_debut,
        )
    return n


async def recaler_tous_les_statuts_logements(db: AsyncSession) -> int:
    """Recalcule le statut de TOUS les logements. Retourne le nombre
    de corrections.

    Le statut d'un logement est dérivé de ses baux — mais il est stocké,
    donc il se périme dès qu'une transition oublie de le recalculer.
    Constat du 2026-08-19 : un logement affichait « réservé » alors que
    le bail proposé qui le réservait avait une date de début PASSÉE et
    que le candidat avait été retiré. La règle donnait « vacant » ;
    personne ne l'avait rejouée.

    D'où ce recalage au démarrage : il coûte une requête par logement
    (une centaine), il est idempotent, et il garantit qu'un statut
    périmé ne survit pas au prochain déploiement. Ce n'est PAS une
    excuse pour oublier l'appel au bon moment — c'est le filet.
    """
    # La gestion externe est exclue ICI AUSSI (défense en profondeur —
    # recaler_statut_logement la refuse déjà) : parcourir des logements
    # qu'on refusera de toucher ne sert qu'à masquer une erreur future.
    ids = [
        r[0]
        for r in (
            await db.execute(
                select(Logement.id)
                .join(Immeuble, Immeuble.id == Logement.immeuble_id)
                .where(
                    Logement.status != LogementStatus.HORS_LOC.value,
                    Immeuble.gestion_externe.isnot(True),
                )
            )
        ).all()
    ]
    corriges = 0
    for logement_id in ids:
        lg = await db.get(Logement, logement_id)
        if lg is None:
            continue
        avant = lg.status
        await recaler_statut_logement(db, logement_id)
        if lg.status != avant:
            corriges += 1
            log.info(
                "Statut du logement %s recalé : %s → %s",
                logement_id, avant, lg.status,
            )
    if corriges:
        await db.commit()
    return corriges


async def libere_le(
    db: AsyncSession, logement_id: int
) -> Optional[date]:
    """Date à laquelle le logement se libère, si un départ est ACTÉ.

    Retour Phil 2026-08-19 : « le statut du logement ne devrait plus
    être juste occupé mais comme occupé et le statut après le bail qui
    va être vacant ». Un logement occupé dont le locataire part le 31
    août n'est pas dans le même état qu'un logement occupé tout court —
    et c'est cette différence qui permet de préparer la relocation.

    ⚠️ Un bail qui arrive à échéance n'est PAS un départ : au Québec il
    se reconduit tacitement. Seul un départ acté (dossier de relocation
    ouvert) libère le logement. La date vient du dossier ; à défaut, de
    la fin du bail qu'il vise.
    """
    dossier = await dossier_relocation_actif(db, logement_id)
    if dossier is None:
        return None
    if dossier.date_depart is not None:
        return dossier.date_depart
    if dossier.bail_id is not None:
        bail = await db.get(Bail, dossier.bail_id)
        if bail is not None and bail.date_fin is not None:
            return bail.date_fin
    return None
